import time
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from sqlalchemy import select

from document_tasks import process_document, process_document_sync
from main import app
from models import Document
from outbox_dispatcher import dispatch_pending_events
from outbox_models import OutboxEvent
from recovery_service import recover_stuck_documents


def run_at_the_same_time(function, times=2):
    barrier = Barrier(times)

    def run():
        barrier.wait()
        return function()

    with ThreadPoolExecutor(max_workers=times) as executor:
        futures = [executor.submit(run) for _ in range(times)]
        return [future.result() for future in futures]


def test_only_one_request_can_claim_document(db_session, create_document_in_db):
    # Arrange
    document = create_document_in_db(status="pending")

    def send_request():
        with TestClient(app) as client:
            return client.post(f"/documents/{document.id}/summarize")

    # Act
    responses = run_at_the_same_time(send_request)

    # Assert
    statuses = sorted(response.status_code for response in responses)

    assert statuses == [202, 409]

    # Only one outbox event should be written.
    events = db_session.scalars(select(OutboxEvent)).all()
    assert len(events) == 1
    assert events[0].payload == {"doc_id": document.id}


def test_only_one_worker_can_process_document(
    db_session, create_document_in_db, mock_summarize_document
):
    document = create_document_in_db(status="processing")

    def slow_summary(_content):
        # Keep the first worker busy while the second one tries to claim.
        time.sleep(0.2)
        return "Summary"

    mock_summarize_document.side_effect = slow_summary

    tokens = iter(["worker-a", "worker-b"])
    run_at_the_same_time(lambda: process_document_sync(document.id, next(tokens)))

    mock_summarize_document.assert_called_once()

    # The winner finished and handed the document back without an owner.
    db_session.expire_all()
    saved_document = db_session.get(Document, document.id)
    assert saved_document.status == "processed"
    assert saved_document.claim_token is None


def test_concurrent_dispatchers_publish_each_event_once(
    create_document_in_db, create_outbox_event_in_db, monkeypatch
):
    delay = MagicMock()
    monkeypatch.setattr(process_document, "delay", delay)

    document_ids = []
    for index in range(5):
        document = create_document_in_db(filename=f"{index}.pdf")
        create_outbox_event_in_db(document.id)
        document_ids.append(document.id)

    published_counts = run_at_the_same_time(dispatch_pending_events)

    assert sum(published_counts) == 5
    assert sorted(call.args[0] for call in delay.call_args_list) == document_ids


def test_concurrent_recovery_runs_recover_each_document_once(
    db_session, create_document_in_db
):
    document_ids = [
        create_document_in_db(
            filename=f"{index}.pdf",
            status="processing",
            processing_claimed_at=datetime.now(timezone.utc) - timedelta(minutes=15),
            claim_token="crashed-worker",
        ).id
        for index in range(5)
    ]

    results = run_at_the_same_time(recover_stuck_documents)

    recovered_ids = sorted(row["id"] for result in results for row in result)
    assert recovered_ids == document_ids

    db_session.expire_all()
    assert len(db_session.scalars(select(OutboxEvent)).all()) == 5
    for doc_id in document_ids:
        assert db_session.get(Document, doc_id).retry_count == 1
