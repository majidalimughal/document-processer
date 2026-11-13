from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from document_tasks import process_document
from outbox_dispatcher import dispatch_pending_events
from outbox_models import OutboxEvent
from tests.conftest import TestingSessionLocal


@pytest.fixture
def mock_delay(monkeypatch):
    mock = MagicMock()
    monkeypatch.setattr(process_document, "delay", mock)
    return mock


def get_events(db_session):
    db_session.expire_all()
    return db_session.scalars(select(OutboxEvent).order_by(OutboxEvent.id)).all()


def test_dispatch_with_no_events(mock_delay):
    assert dispatch_pending_events() == 0
    mock_delay.assert_not_called()


def test_dispatch_publishes_pending_event(
    db_session, create_document_in_db, create_outbox_event_in_db, mock_delay
):
    document = create_document_in_db(status="processing")
    create_outbox_event_in_db(document.id)

    published = dispatch_pending_events()

    assert published == 1
    mock_delay.assert_called_once_with(document.id)

    [event] = get_events(db_session)
    assert event.status == "published"
    assert event.published_at is not None


def test_dispatch_publishes_oldest_events_first(
    create_document_in_db, create_outbox_event_in_db, mock_delay
):
    now = datetime.now(timezone.utc)
    newer = create_document_in_db(filename="newer.pdf")
    older = create_document_in_db(filename="older.pdf")
    create_outbox_event_in_db(newer.id, created_at=now)
    create_outbox_event_in_db(older.id, created_at=now - timedelta(minutes=5))

    dispatch_pending_events()

    assert [call.args[0] for call in mock_delay.call_args_list] == [older.id, newer.id]


def test_dispatch_respects_batch_size(
    db_session, create_document_in_db, create_outbox_event_in_db, mock_delay
):
    for index in range(3):
        document = create_document_in_db(filename=f"{index}.pdf")
        create_outbox_event_in_db(document.id)

    assert dispatch_pending_events(batch_size=2) == 2
    assert [event.status for event in get_events(db_session)].count("pending") == 1

    # The next run picks up what is left.
    assert dispatch_pending_events(batch_size=2) == 1
    assert mock_delay.call_count == 3


def test_dispatch_skips_published_events(
    create_document_in_db, create_outbox_event_in_db, mock_delay
):
    document = create_document_in_db()
    create_outbox_event_in_db(
        document.id, status="published", published_at=datetime.now(timezone.utc)
    )

    assert dispatch_pending_events() == 0
    mock_delay.assert_not_called()


def test_dispatch_keeps_event_pending_when_publish_fails(
    db_session, create_document_in_db, create_outbox_event_in_db, mock_delay
):
    mock_delay.side_effect = ConnectionError("Redis is down")
    document = create_document_in_db()
    create_outbox_event_in_db(document.id)

    with pytest.raises(ConnectionError, match="Redis is down"):
        dispatch_pending_events()

    # The event is retried on the next run instead of being lost.
    [event] = get_events(db_session)
    assert event.status == "pending"
    assert event.published_at is None


def test_dispatch_keeps_earlier_events_published_when_a_later_one_fails(
    db_session, create_document_in_db, create_outbox_event_in_db, mock_delay
):
    first = create_document_in_db(filename="first.pdf")
    second = create_document_in_db(filename="second.pdf")
    create_outbox_event_in_db(first.id)
    create_outbox_event_in_db(second.id)
    mock_delay.side_effect = [None, ConnectionError("Redis is down")]

    with pytest.raises(ConnectionError):
        dispatch_pending_events()

    assert [event.status for event in get_events(db_session)] == ["published", "pending"]


def test_dispatch_skips_events_locked_by_another_dispatcher(
    db_session, create_document_in_db, create_outbox_event_in_db, mock_delay
):
    locked_document = create_document_in_db(filename="locked.pdf")
    free_document = create_document_in_db(filename="free.pdf")
    locked_event = create_outbox_event_in_db(locked_document.id)
    create_outbox_event_in_db(free_document.id)

    other_dispatcher = TestingSessionLocal()
    try:
        # Another dispatcher holds a row lock on the first event.
        other_dispatcher.execute(
            select(OutboxEvent)
            .where(OutboxEvent.id == locked_event.id)
            .with_for_update()
        ).scalar_one()

        published = dispatch_pending_events()
    finally:
        other_dispatcher.rollback()
        other_dispatcher.close()

    assert published == 1
    mock_delay.assert_called_once_with(free_document.id)
