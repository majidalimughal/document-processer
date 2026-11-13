from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from document_tasks import (
    mark_document_failed,
    process_document,
    process_document_sync,
    release_document_claim,
)
from models import Document
from tests.anthropic_errors import (
    authentication_error,
    bad_request_error,
    connection_error,
    internal_server_error,
    rate_limit_error,
)

WORKER_TOKEN = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def get_saved_document(db_session, doc_id):
    db_session.expire_all()
    return db_session.get(Document, doc_id)


@pytest.fixture
def retry_spy(monkeypatch):
    # Eager mode re-runs retries immediately, so record the requested countdowns.
    spy = MagicMock(wraps=process_document.retry)
    monkeypatch.setattr(process_document, "retry", spy)
    return spy


def requested_countdowns(retry_spy):
    return [call.kwargs["countdown"] for call in retry_spy.call_args_list]


# ---------- process_document_sync ----------


def test_process_document_sync_saves_summary(
    db_session, create_document_in_db, mock_summarize_document
):
    document = create_document_in_db(
        status="processing", last_error="Previous failure."
    )

    process_document_sync(document.id, WORKER_TOKEN)

    mock_summarize_document.assert_called_once_with("This is a test document.")

    saved_document = get_saved_document(db_session, document.id)
    assert saved_document.summary == "This is a mocked summary."
    assert saved_document.status == "processed"
    assert saved_document.last_error is None
    assert saved_document.processing_claimed_at is None
    assert saved_document.claim_token is None


def test_process_document_sync_keeps_claim_while_ai_runs(
    db_session, create_document_in_db, mock_summarize_document
):
    document = create_document_in_db(status="processing")
    claimed_at_during_ai_call = []

    def check_claim(_content):
        claimed_at_during_ai_call.append(
            get_saved_document(db_session, document.id).processing_claimed_at
        )
        return "Summary"

    mock_summarize_document.side_effect = check_claim

    process_document_sync(document.id, WORKER_TOKEN)

    # The claim is committed before the AI call, so other workers can see it.
    assert claimed_at_during_ai_call[0] is not None


def test_process_document_sync_ai_error_keeps_claim(
    db_session, create_document_in_db, mock_summarize_document
):
    mock_summarize_document.side_effect = rate_limit_error()
    document = create_document_in_db(status="processing")

    with pytest.raises(type(mock_summarize_document.side_effect)):
        process_document_sync(document.id, WORKER_TOKEN)

    # The Celery task decides whether to release the claim or fail the document.
    saved_document = get_saved_document(db_session, document.id)
    assert saved_document.status == "processing"
    assert saved_document.processing_claimed_at is not None
    assert saved_document.claim_token == WORKER_TOKEN


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "pending"},
        {"status": "failed"},
        {"status": "processed", "summary": "Existing summary."},
        {"status": "processing", "summary": "Existing summary."},
        {
            "status": "processing",
            "processing_claimed_at": datetime.now(timezone.utc),
            "claim_token": "another-worker",
        },
    ],
    ids=["pending", "failed", "processed", "has-summary", "already-claimed"],
)
def test_process_document_sync_skips_documents_it_cannot_claim(
    db_session, create_document_in_db, mock_summarize_document, fields
):
    document = create_document_in_db(**fields)

    process_document_sync(document.id, WORKER_TOKEN)

    mock_summarize_document.assert_not_called()

    saved_document = get_saved_document(db_session, document.id)
    assert saved_document.status == fields["status"]
    assert saved_document.summary == fields.get("summary")


def test_process_document_sync_missing_document(db_session, mock_summarize_document):
    process_document_sync(9999, WORKER_TOKEN)

    mock_summarize_document.assert_not_called()
    assert db_session.query(Document).count() == 0


def test_process_document_sync_empty_content_raises(
    db_session, create_document_in_db, mock_summarize_document
):
    document = create_document_in_db(content="", status="processing")

    with pytest.raises(ValueError, match="Document content is empty"):
        process_document_sync(document.id, WORKER_TOKEN)

    mock_summarize_document.assert_not_called()


# ---------- mark_document_failed ----------


def test_mark_document_failed(db_session, create_document_in_db):
    document = create_document_in_db(
        status="processing",
        processing_claimed_at=datetime.now(timezone.utc),
        claim_token=WORKER_TOKEN,
    )

    mark_document_failed(document.id, WORKER_TOKEN, "Something went wrong.")

    saved_document = get_saved_document(db_session, document.id)
    assert saved_document.status == "failed"
    assert saved_document.last_error == "Something went wrong."
    assert saved_document.processing_claimed_at is None
    assert saved_document.claim_token is None


def test_mark_document_failed_missing_document(db_session):
    mark_document_failed(9999, WORKER_TOKEN, "Something went wrong.")

    assert db_session.query(Document).count() == 0


# ---------- release_document_claim ----------


def test_release_document_claim(db_session, create_document_in_db):
    document = create_document_in_db(
        status="processing",
        processing_claimed_at=datetime.now(timezone.utc),
        claim_token=WORKER_TOKEN,
    )

    release_document_claim(document.id, WORKER_TOKEN)

    saved_document = get_saved_document(db_session, document.id)
    assert saved_document.status == "processing"
    assert saved_document.processing_claimed_at is None
    assert saved_document.claim_token is None


@pytest.mark.parametrize("status", ["pending", "processed", "failed"])
def test_release_document_claim_ignores_other_statuses(
    db_session, create_document_in_db, status
):
    claimed_at = datetime.now(timezone.utc)
    document = create_document_in_db(
        status=status, processing_claimed_at=claimed_at, claim_token=WORKER_TOKEN
    )

    release_document_claim(document.id, WORKER_TOKEN)

    saved_document = get_saved_document(db_session, document.id)
    assert saved_document.processing_claimed_at == claimed_at
    assert saved_document.claim_token == WORKER_TOKEN


# ---------- Celery task (eager mode) ----------


def test_task_success(db_session, create_document_in_db, retry_spy):
    document = create_document_in_db(status="processing")

    result = process_document.apply(args=[document.id])

    assert result.successful()
    retry_spy.assert_not_called()
    assert get_saved_document(db_session, document.id).status == "processed"


def test_task_runs_only_once_for_duplicate_deliveries(
    db_session, create_document_in_db, mock_summarize_document
):
    document = create_document_in_db(status="processing")

    process_document.apply(args=[document.id])
    process_document.apply(args=[document.id])

    mock_summarize_document.assert_called_once()
    assert get_saved_document(db_session, document.id).status == "processed"


def test_rate_limit_retries_with_backoff_then_fails(
    db_session, create_document_in_db, mock_summarize_document, retry_spy
):
    error = rate_limit_error()
    mock_summarize_document.side_effect = error
    document = create_document_in_db(status="processing")

    result = process_document.apply(args=[document.id])

    # 1 first attempt + 3 retries, waiting 5s, 10s, then 20s.
    # Each retry can only run because the previous attempt released its claim.
    assert mock_summarize_document.call_count == 4
    assert requested_countdowns(retry_spy) == [5, 10, 20]

    # Celery serializes the exception, so compare the message, not the subclass.
    assert result.failed()
    assert str(result.result) == str(error)

    saved_document = get_saved_document(db_session, document.id)
    assert saved_document.status == "failed"
    assert saved_document.last_error == str(error)
    assert saved_document.summary is None
    assert saved_document.processing_claimed_at is None
    assert saved_document.claim_token is None


def test_rate_limit_then_success_on_retry(
    db_session, create_document_in_db, mock_summarize_document, retry_spy
):
    mock_summarize_document.side_effect = [
        rate_limit_error(),
        rate_limit_error(),
        "Summary after retrying.",
    ]
    document = create_document_in_db(status="processing")

    result = process_document.apply(args=[document.id])

    assert result.successful()
    assert mock_summarize_document.call_count == 3
    assert requested_countdowns(retry_spy) == [5, 10]

    saved_document = get_saved_document(db_session, document.id)
    assert saved_document.status == "processed"
    assert saved_document.summary == "Summary after retrying."
    assert saved_document.last_error is None
    assert saved_document.processing_claimed_at is None


@pytest.mark.parametrize("make_error", [connection_error, internal_server_error])
def test_other_transient_errors_are_retried(
    db_session, create_document_in_db, mock_summarize_document, retry_spy, make_error
):
    mock_summarize_document.side_effect = make_error()
    document = create_document_in_db(status="processing")

    process_document.apply(args=[document.id])

    assert mock_summarize_document.call_count == 4
    assert requested_countdowns(retry_spy) == [5, 10, 20]
    assert get_saved_document(db_session, document.id).status == "failed"


@pytest.mark.parametrize("make_error", [bad_request_error, authentication_error])
def test_permanent_errors_fail_immediately(
    db_session, create_document_in_db, mock_summarize_document, retry_spy, make_error
):
    error = make_error()
    mock_summarize_document.side_effect = error
    document = create_document_in_db(status="processing")

    result = process_document.apply(args=[document.id])

    mock_summarize_document.assert_called_once()
    retry_spy.assert_not_called()

    assert result.failed()
    assert str(result.result) == str(error)

    saved_document = get_saved_document(db_session, document.id)
    assert saved_document.status == "failed"
    assert saved_document.last_error == str(error)
    assert saved_document.processing_claimed_at is None


def test_empty_content_fails_without_retry(
    db_session, create_document_in_db, mock_summarize_document, retry_spy
):
    document = create_document_in_db(content="", status="processing")

    result = process_document.apply(args=[document.id])

    assert result.failed()
    retry_spy.assert_not_called()
    mock_summarize_document.assert_not_called()

    saved_document = get_saved_document(db_session, document.id)
    assert saved_document.status == "failed"
    assert saved_document.last_error == "Document content is empty"
