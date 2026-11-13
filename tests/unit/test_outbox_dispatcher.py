from unittest.mock import MagicMock

import pytest

import outbox_dispatcher
from outbox_models import OutboxEvent


@pytest.fixture
def mock_db(monkeypatch):
    db = MagicMock()
    monkeypatch.setattr(outbox_dispatcher, "SessionLocal", MagicMock(return_value=db))
    return db


@pytest.fixture
def mock_delay(monkeypatch):
    mock = MagicMock()
    monkeypatch.setattr(outbox_dispatcher.process_document, "delay", mock)
    return mock


def make_event(doc_id):
    return OutboxEvent(
        id=doc_id,
        event_type="document_processing_started",
        payload={"doc_id": doc_id},
        idempotency_key=f"document.process:{doc_id}:0",
        status="pending",
    )


def queue_events(mock_db, events):
    # Each loop iteration selects one event; None means nothing is left.
    mock_db.execute.return_value.scalar_one_or_none.side_effect = [*events, None]


def test_dispatch_publishes_then_marks_published(mock_db, mock_delay):
    event = make_event(1)
    queue_events(mock_db, [event])

    published = outbox_dispatcher.dispatch_pending_events()

    assert published == 1
    mock_delay.assert_called_once_with(1)
    assert event.status == "published"
    assert event.published_at is not None
    mock_db.commit.assert_called_once()
    mock_db.close.assert_called_once()


def test_dispatch_commits_after_each_event(mock_db, mock_delay):
    queue_events(mock_db, [make_event(1), make_event(2)])

    assert outbox_dispatcher.dispatch_pending_events() == 2
    assert mock_db.commit.call_count == 2


def test_dispatch_stops_at_batch_size(mock_db, mock_delay):
    queue_events(mock_db, [make_event(1), make_event(2), make_event(3)])

    assert outbox_dispatcher.dispatch_pending_events(batch_size=2) == 2
    assert mock_delay.call_count == 2


def test_dispatch_with_no_events_rolls_back_and_closes(mock_db, mock_delay):
    queue_events(mock_db, [])

    assert outbox_dispatcher.dispatch_pending_events() == 0
    mock_delay.assert_not_called()
    mock_db.rollback.assert_called_once()
    mock_db.close.assert_called_once()


def test_dispatch_publish_error_does_not_mark_published(mock_db, mock_delay):
    event = make_event(1)
    queue_events(mock_db, [event])
    mock_delay.side_effect = ConnectionError("Redis is down")

    with pytest.raises(ConnectionError, match="Redis is down"):
        outbox_dispatcher.dispatch_pending_events()

    assert event.status == "pending"
    assert event.published_at is None
    mock_db.commit.assert_not_called()
    mock_db.rollback.assert_called_once()
    mock_db.close.assert_called_once()
