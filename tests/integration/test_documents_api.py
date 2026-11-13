from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from document_tasks import process_document
from models import Document
from outbox_dispatcher import dispatch_pending_events
from outbox_models import OutboxEvent
from tests.anthropic_errors import bad_request_error, rate_limit_error


def get_saved_document(db_session, doc_id):
    # The API commits through its own session, so drop anything cached here.
    db_session.expire_all()
    return db_session.get(Document, doc_id)


def get_outbox_events(db_session):
    db_session.expire_all()
    return db_session.scalars(select(OutboxEvent).order_by(OutboxEvent.id)).all()


# ---------- General endpoints ----------


def test_health_check(test_client):
    response = test_client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_root(test_client):
    response = test_client.get("/")

    assert response.status_code == 200
    assert response.json() == {"message": "Welcome to AI Document API"}


# ---------- POST /documents ----------


def test_create_document_returns_id_and_hides_content(test_client, db_session):
    response = test_client.post(
        "/documents",
        json={"filename": "report.pdf", "content": "This is a test document."},
    )

    assert response.status_code == 200

    data = response.json()
    assert set(data) == {"id", "filename", "status"}
    assert data["filename"] == "report.pdf"
    assert data["status"] == "pending"

    saved_document = get_saved_document(db_session, data["id"])
    assert saved_document is not None
    assert saved_document.content == "This is a test document."
    assert saved_document.summary is None
    assert saved_document.retry_count == 0
    assert saved_document.last_error is None


def test_create_document_strips_whitespace(test_client, db_session):
    response = test_client.post(
        "/documents",
        json={"filename": "  report.pdf  ", "content": "  Padded content.  "},
    )

    assert response.status_code == 200
    assert response.json()["filename"] == "report.pdf"

    saved_document = get_saved_document(db_session, response.json()["id"])
    assert saved_document.filename == "report.pdf"
    assert saved_document.content == "Padded content."


@pytest.mark.parametrize(
    "payload",
    [
        {"filename": "", "content": "This is a test document."},
        {"filename": "   ", "content": "This is a test document."},
        {"filename": "report.pdf", "content": ""},
        {"filename": "report.pdf", "content": "   "},
        {"filename": "report.pdf"},
        {"content": "This is a test document."},
        {"filename": 123, "content": "This is a test document."},
        {},
    ],
)
def test_create_document_rejects_invalid_payload(test_client, db_session, payload):
    response = test_client.post("/documents", json=payload)

    assert response.status_code == 422
    assert db_session.query(Document).count() == 0


# ---------- GET /documents ----------


def test_get_all_documents_empty(test_client):
    response = test_client.get("/documents")

    assert response.status_code == 200
    assert response.json() == []


def test_get_all_documents(test_client, create_document_in_db):
    first = create_document_in_db(filename="a.pdf", content="First document.")
    second = create_document_in_db(
        filename="b.pdf",
        content="Second document.",
        summary="Already summarized.",
        status="processed",
    )

    response = test_client.get("/documents")

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": first.id,
            "filename": "a.pdf",
            "content": "First document.",
            "summary": None,
            "status": "pending",
        },
        {
            "id": second.id,
            "filename": "b.pdf",
            "content": "Second document.",
            "summary": "Already summarized.",
            "status": "processed",
        },
    ]


# ---------- GET /documents/{doc_id} ----------


def test_get_document(test_client, create_document_in_db):
    document = create_document_in_db()

    response = test_client.get(f"/documents/{document.id}")

    assert response.status_code == 200
    assert response.json() == {
        "id": document.id,
        "filename": "report.pdf",
        "content": "This is a test document.",
        "summary": None,
        "status": "pending",
    }


def test_get_document_not_found(test_client):
    response = test_client.get("/documents/9999")

    assert response.status_code == 404
    assert response.json() == {"detail": "Document not found"}


def test_get_document_invalid_id(test_client):
    response = test_client.get("/documents/not-a-number")

    assert response.status_code == 422




# ---------- POST /documents/{doc_id}/summarize ----------


def test_summarize_document_claims_and_writes_outbox_event(
    test_client, db_session, create_document_in_db, mock_summarize_document
):
    document = create_document_in_db(last_error="Previous failure.")

    response = test_client.post(f"/documents/{document.id}/summarize")

    assert response.status_code == 202
    data = response.json()
    assert data["id"] == document.id
    assert data["status"] == "processing"
    assert data["summary"] is None

    saved_document = get_saved_document(db_session, document.id)
    assert saved_document.status == "processing"
    assert saved_document.last_error is None
    assert saved_document.retry_count == 0

    [event] = get_outbox_events(db_session)
    assert event.event_type == "document_processing_started"
    assert event.payload == {"doc_id": document.id}
    assert event.status == "pending"
    assert event.idempotency_key == f"document.process:{document.id}:0"
    assert event.published_at is None

    # The endpoint only records the event; the dispatcher queues the task.
    mock_summarize_document.assert_not_called()


def test_summarize_document_does_not_queue_celery_directly(
    test_client, create_document_in_db, monkeypatch
):
    delay = MagicMock()
    monkeypatch.setattr(process_document, "delay", delay)
    document = create_document_in_db()

    response = test_client.post(f"/documents/{document.id}/summarize")

    assert response.status_code == 202
    delay.assert_not_called()


def test_summarize_failed_document_increments_retry_count(
    test_client, db_session, create_document_in_db
):
    document = create_document_in_db(
        status="failed", retry_count=2, last_error="Previous failure."
    )

    response = test_client.post(f"/documents/{document.id}/summarize")

    assert response.status_code == 202

    saved_document = get_saved_document(db_session, document.id)
    assert saved_document.status == "processing"
    assert saved_document.retry_count == 3
    assert saved_document.last_error is None

    [event] = get_outbox_events(db_session)
    assert event.idempotency_key == f"document.process:{document.id}:3"


def test_summarize_document_not_found(test_client, db_session):
    response = test_client.post("/documents/9999/summarize")

    assert response.status_code == 404
    assert response.json() == {"detail": "Document not found"}
    assert get_outbox_events(db_session) == []


def test_summarize_document_already_summarized(
    test_client, db_session, create_document_in_db
):
    document = create_document_in_db(summary="Existing summary.", status="processed")

    response = test_client.post(f"/documents/{document.id}/summarize")

    assert response.status_code == 409
    assert response.json() == {"detail": "Document has already been summarized."}
    assert get_outbox_events(db_session) == []


def test_summarize_document_with_summary_is_rejected_whatever_the_status(
    test_client, db_session, create_document_in_db
):
    document = create_document_in_db(summary="Existing summary.", status="pending")

    response = test_client.post(f"/documents/{document.id}/summarize")

    assert response.status_code == 409
    assert response.json() == {"detail": "Document has already been summarized."}
    assert get_saved_document(db_session, document.id).status == "pending"
    assert get_outbox_events(db_session) == []


@pytest.mark.parametrize("status", ["processing", "processed"])
def test_summarize_document_not_eligible(
    test_client, db_session, create_document_in_db, status
):
    document = create_document_in_db(status=status)

    response = test_client.post(f"/documents/{document.id}/summarize")

    assert response.status_code == 409
    assert response.json() == {
        "detail": "Document is already being processed or is not eligible."
    }
    assert get_saved_document(db_session, document.id).status == status
    assert get_outbox_events(db_session) == []


def test_summarize_document_existing_event_rejects_and_keeps_document_unclaimed(
    test_client, db_session, create_document_in_db, create_outbox_event_in_db
):
    document = create_document_in_db()
    create_outbox_event_in_db(document.id)

    response = test_client.post(f"/documents/{document.id}/summarize")

    assert response.status_code == 409
    assert response.json() == {"detail": "Processing event already exists."}

    # The claim must not be committed without its event.
    saved_document = get_saved_document(db_session, document.id)
    assert saved_document.status == "pending"
    assert len(get_outbox_events(db_session)) == 1


def test_summarize_document_second_request_while_processing_is_rejected(
    test_client, db_session, create_document_in_db
):
    document = create_document_in_db()

    first = test_client.post(f"/documents/{document.id}/summarize")
    second = test_client.post(f"/documents/{document.id}/summarize")

    assert first.status_code == 202
    assert second.status_code == 409
    assert len(get_outbox_events(db_session)) == 1
    assert get_saved_document(db_session, document.id).retry_count == 0


# ---------- Full flow: API -> outbox -> dispatcher -> Celery task ----------


def test_full_flow_summarizes_document(
    test_client, db_session, create_document_in_db, mock_summarize_document
):
    document = create_document_in_db()

    test_client.post(f"/documents/{document.id}/summarize")
    published = dispatch_pending_events()

    assert published == 1
    mock_summarize_document.assert_called_once_with("This is a test document.")

    response = test_client.get(f"/documents/{document.id}")
    assert response.json()["status"] == "processed"
    assert response.json()["summary"] == "This is a mocked summary."

    [event] = get_outbox_events(db_session)
    assert event.status == "published"
    assert event.published_at is not None


def test_full_flow_permanent_ai_error(
    test_client, db_session, create_document_in_db, mock_summarize_document
):
    error = bad_request_error()
    mock_summarize_document.side_effect = error
    document = create_document_in_db()

    test_client.post(f"/documents/{document.id}/summarize")
    dispatch_pending_events()

    mock_summarize_document.assert_called_once()

    saved_document = get_saved_document(db_session, document.id)
    assert saved_document.status == "failed"
    assert saved_document.last_error == str(error)
    assert saved_document.summary is None
    assert saved_document.processing_claimed_at is None


def test_full_flow_rate_limit_exhausts_retries(
    test_client, db_session, create_document_in_db, mock_summarize_document
):
    mock_summarize_document.side_effect = rate_limit_error()
    document = create_document_in_db()

    test_client.post(f"/documents/{document.id}/summarize")
    dispatch_pending_events()

    assert mock_summarize_document.call_count == 4

    saved_document = get_saved_document(db_session, document.id)
    assert saved_document.status == "failed"
    # Celery retries don't count as user retries.
    assert saved_document.retry_count == 0


def test_full_flow_fails_then_succeeds_on_user_retry(
    test_client, db_session, create_document_in_db, mock_summarize_document
):
    document = create_document_in_db()

    mock_summarize_document.side_effect = bad_request_error()
    first = test_client.post(f"/documents/{document.id}/summarize")
    dispatch_pending_events()
    assert first.status_code == 202
    assert get_saved_document(db_session, document.id).status == "failed"

    mock_summarize_document.side_effect = None
    second = test_client.post(f"/documents/{document.id}/summarize")
    dispatch_pending_events()
    assert second.status_code == 202

    saved_document = get_saved_document(db_session, document.id)
    assert saved_document.status == "processed"
    assert saved_document.retry_count == 1
    assert saved_document.last_error is None
    assert mock_summarize_document.call_count == 2

    # Each attempt has its own event.
    assert [event.idempotency_key for event in get_outbox_events(db_session)] == [
        f"document.process:{document.id}:0",
        f"document.process:{document.id}:1",
    ]

    # A processed document cannot be summarized again.
    third = test_client.post(f"/documents/{document.id}/summarize")
    assert third.status_code == 409


def test_patch_endpoint_is_disabled(test_client, create_document_in_db):
    document = create_document_in_db()

    response = test_client.patch(f"/documents/{document.id}", json={"status": "processed"})

    assert response.status_code == 405
