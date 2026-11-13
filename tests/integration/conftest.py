from unittest.mock import MagicMock

import pytest

import document_tasks
from models import Document
from outbox_models import OutboxEvent
from tests.conftest import clear_tables


@pytest.fixture(autouse=True)
def clean_tables(test_database):
    # Tests that skip test_client still need empty tables.
    clear_tables()
    yield
    clear_tables()


@pytest.fixture
def create_document_in_db(db_session):
    def _create(**fields):
        document = Document(
            filename=fields.pop("filename", "report.pdf"),
            content=fields.pop("content", "This is a test document."),
            **fields,
        )
        db_session.add(document)
        db_session.commit()
        db_session.refresh(document)
        return document

    return _create


@pytest.fixture
def create_outbox_event_in_db(db_session):
    def _create(doc_id, **fields):
        event = OutboxEvent(
            event_type=fields.pop("event_type", "document_processing_started"),
            payload=fields.pop("payload", {"doc_id": doc_id}),
            idempotency_key=fields.pop(
                "idempotency_key", f"document.process:{doc_id}:0"
            ),
            **fields,
        )
        db_session.add(event)
        db_session.commit()
        db_session.refresh(event)
        return event

    return _create


@pytest.fixture(autouse=True)
def mock_summarize_document(monkeypatch):
    # Never call the real AI provider from integration tests.
    mock = MagicMock(return_value="This is a mocked summary.")
    monkeypatch.setattr(document_tasks, "summarize_document", mock)
    return mock
