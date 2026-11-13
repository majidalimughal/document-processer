from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from document_tasks import process_document_sync
from models import Document
from outbox_dispatcher import dispatch_pending_events
from outbox_models import OutboxEvent
from recovery_service import MAX_ATTEMPTS, recover_stuck_documents
from tasks.recovery_tasks import recover_stuck_documents_task
from tests.conftest import TestingSessionLocal

WORKER_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def minutes_ago(minutes):
    return datetime.now(timezone.utc) - timedelta(minutes=minutes)


def get_saved_document(db_session, doc_id):
    db_session.expire_all()
    return db_session.get(Document, doc_id)


def get_outbox_events(db_session):
    db_session.expire_all()
    return db_session.scalars(select(OutboxEvent).order_by(OutboxEvent.id)).all()


@pytest.fixture
def create_stuck_document(create_document_in_db):
    def _create(claimed_minutes_ago=15, **fields):
        return create_document_in_db(
            status=fields.pop("status", "processing"),
            processing_claimed_at=minutes_ago(claimed_minutes_ago),
            claim_token=fields.pop("claim_token", WORKER_A),
            **fields,
        )

    return _create


# ---------- Recovering stuck documents ----------


def test_recovers_expired_claim(db_session, create_stuck_document):
    document = create_stuck_document(retry_count=1, last_error="Worker crashed.")

    recovered = recover_stuck_documents()

    assert [dict(row) for row in recovered] == [
        {"id": document.id, "retry_count": 2, "last_error": "Worker crashed."}
    ]

    saved_document = get_saved_document(db_session, document.id)
    assert saved_document.status == "processing"
    assert saved_document.retry_count == 2
    assert saved_document.processing_claimed_at is None
    assert saved_document.claim_token is None


def test_recovery_writes_outbox_event(db_session, create_stuck_document):
    document = create_stuck_document(retry_count=1, last_error="Worker crashed.")

    recover_stuck_documents()

    [event] = get_outbox_events(db_session)
    assert event.idempotency_key == f"document.process:{document.id}:2"
    assert event.event_type == "document_processing_started"
    assert event.status == "pending"
    assert event.payload == {
        "doc_id": document.id,
        "retry_count": 2,
        "last_error": "Worker crashed.",
    }


def test_recovers_many_documents_in_one_run(db_session, create_stuck_document):
    documents = [create_stuck_document(filename=f"{i}.pdf") for i in range(3)]

    recovered = recover_stuck_documents()

    assert sorted(row["id"] for row in recovered) == [d.id for d in documents]
    assert len(get_outbox_events(db_session)) == 3


@pytest.mark.parametrize("claimed_minutes_ago", [0, 5, 9])
def test_leaves_recent_claims_alone(
    db_session, create_stuck_document, claimed_minutes_ago
):
    document = create_stuck_document(claimed_minutes_ago=claimed_minutes_ago)

    assert recover_stuck_documents() == []

    saved_document = get_saved_document(db_session, document.id)
    assert saved_document.status == "processing"
    assert saved_document.claim_token == WORKER_A
    assert get_outbox_events(db_session) == []


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "pending"},
        {"status": "processed", "summary": "Done."},
        {"status": "failed"},
    ],
    ids=["pending", "processed", "failed"],
)
def test_ignores_documents_that_are_not_processing(
    db_session, create_stuck_document, fields
):
    document = create_stuck_document(**fields)

    assert recover_stuck_documents() == []

    saved_document = get_saved_document(db_session, document.id)
    assert saved_document.status == fields["status"]
    assert saved_document.claim_token == WORKER_A


def test_nothing_to_recover(db_session):
    assert recover_stuck_documents() == []
    assert get_outbox_events(db_session) == []


# ---------- Attempt limit ----------


def test_last_attempt_is_still_recovered(db_session, create_stuck_document):
    document = create_stuck_document(retry_count=MAX_ATTEMPTS - 2)

    recover_stuck_documents()

    saved_document = get_saved_document(db_session, document.id)
    assert saved_document.status == "processing"
    assert saved_document.retry_count == MAX_ATTEMPTS - 1


@pytest.mark.parametrize("retry_count", [MAX_ATTEMPTS - 1, MAX_ATTEMPTS, MAX_ATTEMPTS + 3])
def test_exhausted_documents_are_marked_failed(
    db_session, create_stuck_document, retry_count
):
    document = create_stuck_document(retry_count=retry_count)

    recovered = recover_stuck_documents()

    assert recovered == []

    saved_document = get_saved_document(db_session, document.id)
    assert saved_document.status == "failed"
    assert saved_document.retry_count == retry_count
    assert saved_document.processing_claimed_at is None
    assert saved_document.claim_token is None
    assert get_outbox_events(db_session) == []


def test_recovers_and_fails_in_the_same_run(db_session, create_stuck_document):
    recoverable = create_stuck_document(filename="recoverable.pdf", retry_count=0)
    exhausted = create_stuck_document(
        filename="exhausted.pdf", retry_count=MAX_ATTEMPTS - 1
    )

    recovered = recover_stuck_documents()

    assert [row["id"] for row in recovered] == [recoverable.id]
    assert get_saved_document(db_session, recoverable.id).status == "processing"
    assert get_saved_document(db_session, exhausted.id).status == "failed"


def test_exhausted_documents_are_not_failed_before_their_claim_expires(
    db_session, create_stuck_document
):
    document = create_stuck_document(claimed_minutes_ago=1, retry_count=MAX_ATTEMPTS)

    recover_stuck_documents()

    assert get_saved_document(db_session, document.id).status == "processing"


# ---------- Idempotency and ownership ----------


def test_existing_event_is_not_duplicated(
    db_session, create_stuck_document, create_outbox_event_in_db
):
    document = create_stuck_document(retry_count=0)
    create_outbox_event_in_db(
        document.id, idempotency_key=f"document.process:{document.id}:1"
    )

    recover_stuck_documents()

    assert len(get_outbox_events(db_session)) == 1
    assert get_saved_document(db_session, document.id).status == "processing"


def test_running_recovery_twice_recovers_once(db_session, create_stuck_document):
    document = create_stuck_document()

    first = recover_stuck_documents()
    second = recover_stuck_documents()

    assert len(first) == 1
    assert second == []
    assert get_saved_document(db_session, document.id).retry_count == 1
    assert len(get_outbox_events(db_session)) == 1


def test_worker_that_lost_its_claim_to_recovery_cannot_save_result(
    db_session, create_document_in_db, mock_summarize_document
):
    document = create_document_in_db(status="processing")

    def recovery_runs_during_ai_call(_content):
        # Pretend the claim is old, then let recovery take the document back.
        session = TestingSessionLocal()
        try:
            session.get(Document, document.id).processing_claimed_at = minutes_ago(15)
            session.commit()
        finally:
            session.close()

        assert len(recover_stuck_documents()) == 1
        return "Late summary from worker A."

    mock_summarize_document.side_effect = recovery_runs_during_ai_call

    process_document_sync(document.id, WORKER_A)

    saved_document = get_saved_document(db_session, document.id)
    assert saved_document.summary is None
    assert saved_document.status == "processing"
    assert saved_document.claim_token is None


# ---------- Celery task ----------


def test_recovery_task_returns_count(create_stuck_document):
    create_stuck_document(filename="a.pdf")
    create_stuck_document(filename="b.pdf")

    result = recover_stuck_documents_task.apply()

    assert result.successful()
    assert result.result == {"recovered_count": 2}


# ---------- Full flow: recovery -> outbox -> dispatcher -> worker ----------


def test_recovered_document_is_processed_after_dispatch(
    db_session, create_stuck_document, mock_summarize_document
):
    document = create_stuck_document(retry_count=1)

    recover_stuck_documents()
    dispatch_pending_events()

    mock_summarize_document.assert_called_once()

    saved_document = get_saved_document(db_session, document.id)
    assert saved_document.status == "processed"
    assert saved_document.summary == "This is a mocked summary."
    assert saved_document.retry_count == 2
    assert saved_document.claim_token is None
    assert saved_document.processing_claimed_at is None


def test_user_cannot_summarize_document_that_recovery_requeued(
    test_client, db_session, create_stuck_document
):
    document = create_stuck_document()
    recover_stuck_documents()

    response = test_client.post(f"/documents/{document.id}/summarize")

    assert response.status_code == 409
    assert response.json() == {
        "detail": "Document is already being processed or is not eligible."
    }
    # Only recovery's event exists; the request did not queue a second one.
    assert len(get_outbox_events(db_session)) == 1


def test_recovered_document_can_be_recovered_again_if_worker_crashes_again(
    db_session, create_stuck_document, mock_summarize_document
):
    document = create_stuck_document(retry_count=0)

    recover_stuck_documents()

    # The new worker claims it and then crashes too, leaving an old claim.
    session = TestingSessionLocal()
    try:
        crashed = session.get(Document, document.id)
        crashed.processing_claimed_at = minutes_ago(15)
        crashed.claim_token = "second-crashed-worker"
        session.commit()
    finally:
        session.close()

    recover_stuck_documents()

    saved_document = get_saved_document(db_session, document.id)
    assert saved_document.retry_count == 2
    assert [event.idempotency_key for event in get_outbox_events(db_session)] == [
        f"document.process:{document.id}:1",
        f"document.process:{document.id}:2",
    ]
