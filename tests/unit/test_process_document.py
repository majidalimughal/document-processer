from unittest.mock import MagicMock

import pytest

import document_tasks
from models import Document
from tests.anthropic_errors import rate_limit_error


@pytest.fixture
def mock_db(monkeypatch):
    db = MagicMock()
    monkeypatch.setattr(document_tasks, "SessionLocal", MagicMock(return_value=db))
    return db


@pytest.fixture
def mock_summarize(monkeypatch):
    mock = MagicMock(return_value="A short summary.")
    monkeypatch.setattr(document_tasks, "summarize_document", mock)
    return mock


def make_document(**fields):
    return Document(
        id=1,
        filename="report.pdf",
        content=fields.pop("content", "Some content."),
        status=fields.pop("status", "processing"),
        **fields,
    )


def claim_succeeds(mock_db, document):
    mock_db.execute.return_value.scalar_one_or_none.return_value = document.id
    mock_db.get.return_value = document


# ---------- process_document_sync ----------


def test_process_document_sync_success(mock_db, mock_summarize):
    document = make_document(last_error="Previous failure.")
    claim_succeeds(mock_db, document)

    document_tasks.process_document_sync(1)

    mock_summarize.assert_called_once_with("Some content.")
    assert document.summary == "A short summary."
    assert document.status == "processed"
    assert document.last_error is None
    assert document.processing_claimed_at is None
    # One commit for the claim, one for the result.
    assert mock_db.commit.call_count == 2
    mock_db.close.assert_called_once()


def test_process_document_sync_not_claimed_rolls_back(mock_db, mock_summarize):
    mock_db.execute.return_value.scalar_one_or_none.return_value = None

    document_tasks.process_document_sync(1)

    mock_summarize.assert_not_called()
    mock_db.rollback.assert_called_once()
    mock_db.commit.assert_not_called()
    mock_db.close.assert_called_once()


def test_process_document_sync_document_deleted_after_claim(mock_db, mock_summarize):
    mock_db.execute.return_value.scalar_one_or_none.return_value = 1
    mock_db.get.return_value = None

    document_tasks.process_document_sync(1)

    mock_summarize.assert_not_called()
    mock_db.close.assert_called_once()


def test_process_document_sync_empty_content_raises(mock_db, mock_summarize):
    claim_succeeds(mock_db, make_document(content=""))

    with pytest.raises(ValueError, match="Document content is empty"):
        document_tasks.process_document_sync(1)

    mock_summarize.assert_not_called()
    mock_db.close.assert_called_once()


def test_process_document_sync_ai_error_propagates(mock_db, mock_summarize):
    # The Celery task decides what to do with AI errors.
    document = make_document()
    claim_succeeds(mock_db, document)
    error = rate_limit_error()
    mock_summarize.side_effect = error

    with pytest.raises(type(error)):
        document_tasks.process_document_sync(1)

    assert document.status == "processing"
    assert document.summary is None
    # Only the claim was committed.
    mock_db.commit.assert_called_once()
    mock_db.close.assert_called_once()


def test_process_document_sync_claim_error_closes_session(mock_db, mock_summarize):
    mock_db.execute.side_effect = RuntimeError("database is down")

    with pytest.raises(RuntimeError, match="database is down"):
        document_tasks.process_document_sync(1)

    mock_summarize.assert_not_called()
    mock_db.close.assert_called_once()


def test_process_document_sync_commit_error_rolls_back_and_raises(
    mock_db, mock_summarize
):
    claim_succeeds(mock_db, make_document())
    mock_db.commit.side_effect = [None, RuntimeError("database is down")]

    with pytest.raises(RuntimeError, match="database is down"):
        document_tasks.process_document_sync(1)

    mock_db.rollback.assert_called_once()
    mock_db.close.assert_called_once()


# ---------- mark_document_failed ----------


def test_mark_document_failed(mock_db):
    document = make_document()
    mock_db.get.return_value = document

    document_tasks.mark_document_failed(1, "Something went wrong.")

    assert document.status == "failed"
    assert document.last_error == "Something went wrong."
    assert document.processing_claimed_at is None
    mock_db.commit.assert_called_once()
    mock_db.close.assert_called_once()


def test_mark_document_failed_missing_document(mock_db):
    mock_db.get.return_value = None

    document_tasks.mark_document_failed(1, "Something went wrong.")

    mock_db.commit.assert_not_called()
    mock_db.close.assert_called_once()


def test_mark_document_failed_commit_error_rolls_back_and_raises(mock_db):
    mock_db.get.return_value = make_document()
    mock_db.commit.side_effect = RuntimeError("database is down")

    with pytest.raises(RuntimeError, match="database is down"):
        document_tasks.mark_document_failed(1, "Something went wrong.")

    mock_db.rollback.assert_called_once()
    mock_db.close.assert_called_once()


# ---------- release_document_claim ----------


def test_release_document_claim_commits(mock_db):
    document_tasks.release_document_claim(1)

    mock_db.execute.assert_called_once()
    mock_db.commit.assert_called_once()
    mock_db.close.assert_called_once()


def test_release_document_claim_error_rolls_back_and_raises(mock_db):
    mock_db.execute.side_effect = RuntimeError("database is down")

    with pytest.raises(RuntimeError, match="database is down"):
        document_tasks.release_document_claim(1)

    mock_db.rollback.assert_called_once()
    mock_db.commit.assert_not_called()
    mock_db.close.assert_called_once()
