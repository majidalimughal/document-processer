import pytest
from sqlalchemy.exc import DataError, IntegrityError

from models import Document
from outbox_models import OutboxEvent


def test_document_defaults(db_session):
    document = Document(filename="report.pdf", content="Some content.")
    db_session.add(document)
    db_session.commit()
    db_session.refresh(document)

    assert document.id is not None
    assert document.status == "pending"
    assert document.retry_count == 0
    assert document.summary is None
    assert document.last_error is None
    assert document.processing_claimed_at is None


@pytest.mark.parametrize("missing_field", ["filename", "content"])
def test_document_required_fields(db_session, missing_field):
    fields = {"filename": "report.pdf", "content": "Some content."}
    fields[missing_field] = None
    db_session.add(Document(**fields))

    with pytest.raises(IntegrityError):
        db_session.commit()


def test_document_filename_max_length(db_session):
    db_session.add(Document(filename="a" * 256, content="Some content."))

    with pytest.raises(DataError):
        db_session.commit()


def test_outbox_event_defaults(db_session):
    event = OutboxEvent(
        event_type="document_processing_started",
        payload={"doc_id": 1},
        idempotency_key="document.process:1:0",
    )
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)

    assert event.id is not None
    assert event.status == "pending"
    assert event.created_at is not None
    assert event.created_at.tzinfo is not None
    assert event.published_at is None
    assert event.payload == {"doc_id": 1}


def test_outbox_event_idempotency_key_is_unique(db_session):
    for _ in range(2):
        db_session.add(
            OutboxEvent(
                event_type="document_processing_started",
                payload={"doc_id": 1},
                idempotency_key="document.process:1:0",
            )
        )

    with pytest.raises(IntegrityError):
        db_session.commit()


# payload is left out: the JSON type stores Python None as JSON null, not SQL NULL.
@pytest.mark.parametrize("missing_field", ["event_type", "idempotency_key"])
def test_outbox_event_required_fields(db_session, missing_field):
    fields = {
        "event_type": "document_processing_started",
        "payload": {"doc_id": 1},
        "idempotency_key": "document.process:1:0",
    }
    fields[missing_field] = None
    db_session.add(OutboxEvent(**fields))

    with pytest.raises(IntegrityError):
        db_session.commit()
