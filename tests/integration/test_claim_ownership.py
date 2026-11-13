from datetime import datetime, timezone

import pytest

from document_tasks import (
    mark_document_failed,
    process_document,
    process_document_sync,
    release_document_claim,
)
from models import Document
from tests.conftest import TestingSessionLocal

WORKER_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
WORKER_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def get_saved_document(db_session, doc_id):
    db_session.expire_all()
    return db_session.get(Document, doc_id)


@pytest.fixture
def document_owned_by_worker_a(create_document_in_db):
    return create_document_in_db(
        status="processing",
        processing_claimed_at=datetime.now(timezone.utc),
        claim_token=WORKER_A,
    )


def assert_still_owned_by_worker_a(db_session, document):
    saved_document = get_saved_document(db_session, document.id)
    assert saved_document.status == "processing"
    assert saved_document.claim_token == WORKER_A
    assert saved_document.processing_claimed_at == document.processing_claimed_at
    assert saved_document.summary is None
    assert saved_document.last_error is None


# ---------- Worker B cannot take over Worker A's document ----------


def test_worker_b_cannot_claim_document_owned_by_worker_a(
    db_session, document_owned_by_worker_a, mock_summarize_document
):
    process_document_sync(document_owned_by_worker_a.id, WORKER_B)

    mock_summarize_document.assert_not_called()
    assert_still_owned_by_worker_a(db_session, document_owned_by_worker_a)


def test_worker_b_cannot_claim_while_worker_a_is_summarizing(
    db_session, create_document_in_db, mock_summarize_document
):
    document = create_document_in_db(status="processing")
    worker_b_attempts = []

    def worker_a_summary(_content):
        # While A waits for the AI, B receives the same task and tries to claim.
        saved_document = get_saved_document(db_session, document.id)
        assert saved_document.claim_token == WORKER_A

        worker_b_attempts.append(process_document_sync(document.id, WORKER_B))
        return "Summary from worker A."

    mock_summarize_document.side_effect = worker_a_summary

    process_document_sync(document.id, WORKER_A)

    # B tried once and gave up without calling the AI.
    assert worker_b_attempts == [None]
    mock_summarize_document.assert_called_once()

    saved_document = get_saved_document(db_session, document.id)
    assert saved_document.status == "processed"
    assert saved_document.summary == "Summary from worker A."
    assert saved_document.claim_token is None
    assert saved_document.processing_claimed_at is None


def test_worker_b_cannot_mark_worker_a_document_failed(
    db_session, document_owned_by_worker_a
):
    mark_document_failed(document_owned_by_worker_a.id, WORKER_B, "Worker B failed.")

    assert_still_owned_by_worker_a(db_session, document_owned_by_worker_a)


def test_worker_b_cannot_release_worker_a_claim(db_session, document_owned_by_worker_a):
    release_document_claim(document_owned_by_worker_a.id, WORKER_B)

    assert_still_owned_by_worker_a(db_session, document_owned_by_worker_a)


def test_celery_task_leaves_owned_document_alone(
    db_session, document_owned_by_worker_a, mock_summarize_document
):
    # A duplicate delivery of the task is a different worker with a new token.
    result = process_document.apply(args=[document_owned_by_worker_a.id])

    # Losing the claim race is not an error, so nothing is marked failed.
    assert result.successful()
    mock_summarize_document.assert_not_called()
    assert_still_owned_by_worker_a(db_session, document_owned_by_worker_a)


# ---------- Worker A loses ownership while summarizing ----------


def test_worker_a_cannot_save_result_after_losing_ownership(
    db_session, create_document_in_db, mock_summarize_document
):
    document = create_document_in_db(status="processing")

    def ownership_moves_to_worker_b(_content):
        # Simulate the claim being taken over while A waits for the AI.
        other_session = TestingSessionLocal()
        try:
            other_document = other_session.get(Document, document.id)
            other_document.claim_token = WORKER_B
            other_session.commit()
        finally:
            other_session.close()
        return "Late summary from worker A."

    mock_summarize_document.side_effect = ownership_moves_to_worker_b

    process_document_sync(document.id, WORKER_A)

    saved_document = get_saved_document(db_session, document.id)
    assert saved_document.summary is None
    assert saved_document.status == "processing"
    assert saved_document.claim_token == WORKER_B


# ---------- The owner keeps full control ----------


def test_worker_a_can_mark_its_document_failed(db_session, document_owned_by_worker_a):
    mark_document_failed(document_owned_by_worker_a.id, WORKER_A, "Worker A failed.")

    saved_document = get_saved_document(db_session, document_owned_by_worker_a.id)
    assert saved_document.status == "failed"
    assert saved_document.last_error == "Worker A failed."
    assert saved_document.claim_token is None
    assert saved_document.processing_claimed_at is None


def test_worker_b_can_claim_after_worker_a_releases(
    db_session, document_owned_by_worker_a, mock_summarize_document
):
    release_document_claim(document_owned_by_worker_a.id, WORKER_A)

    released = get_saved_document(db_session, document_owned_by_worker_a.id)
    assert released.status == "processing"
    assert released.claim_token is None
    assert released.processing_claimed_at is None

    process_document_sync(document_owned_by_worker_a.id, WORKER_B)

    mock_summarize_document.assert_called_once()
    saved_document = get_saved_document(db_session, document_owned_by_worker_a.id)
    assert saved_document.status == "processed"
    assert saved_document.claim_token is None


def test_claim_records_worker_token(
    db_session, create_document_in_db, mock_summarize_document
):
    document = create_document_in_db(status="processing")
    tokens_during_ai_call = []

    def record_owner(_content):
        saved_document = get_saved_document(db_session, document.id)
        tokens_during_ai_call.append(
            (saved_document.claim_token, saved_document.processing_claimed_at)
        )
        return "Summary"

    mock_summarize_document.side_effect = record_owner

    process_document_sync(document.id, WORKER_A)

    [(token, claimed_at)] = tokens_during_ai_call
    assert token == WORKER_A
    assert claimed_at is not None
