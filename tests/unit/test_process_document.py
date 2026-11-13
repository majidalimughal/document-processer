from unittest.mock import MagicMock

import pytest

import document_tasks
from models import Document
from tests.anthropic_errors import rate_limit_error

WORKER_TOKEN = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


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


def returning(value):
    # Result of an UPDATE ... RETURNING id.
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


def statement_values(statement):
    return {column.key: value.value for column, value in statement._values.items()}


# ---------- process_document_sync ----------


def test_process_document_sync_success(mock_db, mock_summarize):
    mock_db.execute.side_effect = [returning(1), returning(1)]
    mock_db.get.return_value = make_document()

    document_tasks.process_document_sync(1, WORKER_TOKEN)

    mock_summarize.assert_called_once_with("Some content.")
    # One commit for the claim, one for the result.
    assert mock_db.commit.call_count == 2
    mock_db.close.assert_called_once()


def test_process_document_sync_claim_stores_worker_token(mock_db, mock_summarize):
    mock_db.execute.side_effect = [returning(1), returning(1)]
    mock_db.get.return_value = make_document()

    document_tasks.process_document_sync(1, WORKER_TOKEN)

    claim_values = statement_values(mock_db.execute.call_args_list[0].args[0])
    assert claim_values["claim_token"] == WORKER_TOKEN
    assert claim_values["processing_claimed_at"] is not None


def test_process_document_sync_saves_result_and_clears_ownership(
    mock_db, mock_summarize
):
    mock_db.execute.side_effect = [returning(1), returning(1)]
    mock_db.get.return_value = make_document()

    document_tasks.process_document_sync(1, WORKER_TOKEN)

    result_values = statement_values(mock_db.execute.call_args_list[1].args[0])
    assert result_values == {
        "summary": "A short summary.",
        "status": "processed",
        "last_error": None,
        "processing_claimed_at": None,
        "claim_token": None,
    }


def test_process_document_sync_not_claimed_rolls_back(mock_db, mock_summarize):
    mock_db.execute.return_value = returning(None)

    document_tasks.process_document_sync(1, WORKER_TOKEN)

    mock_summarize.assert_not_called()
    mock_db.rollback.assert_called_once()
    mock_db.commit.assert_not_called()
    mock_db.close.assert_called_once()


def test_process_document_sync_lost_ownership_discards_result(mock_db, mock_summarize):
    # The final UPDATE matches no row because another worker owns the document.
    mock_db.execute.side_effect = [returning(1), returning(None)]
    mock_db.get.return_value = make_document()

    document_tasks.process_document_sync(1, WORKER_TOKEN)

    mock_summarize.assert_called_once()
    # Only the claim was committed; the result was rolled back.
    mock_db.commit.assert_called_once()
    mock_db.rollback.assert_called_once()
    mock_db.close.assert_called_once()


def test_process_document_sync_document_deleted_after_claim(mock_db, mock_summarize):
    mock_db.execute.return_value = returning(1)
    mock_db.get.return_value = None

    document_tasks.process_document_sync(1, WORKER_TOKEN)

    mock_summarize.assert_not_called()
    mock_db.close.assert_called_once()


def test_process_document_sync_empty_content_raises(mock_db, mock_summarize):
    mock_db.execute.return_value = returning(1)
    mock_db.get.return_value = make_document(content="")

    with pytest.raises(ValueError, match="Document content is empty"):
        document_tasks.process_document_sync(1, WORKER_TOKEN)

    mock_summarize.assert_not_called()
    mock_db.close.assert_called_once()


def test_process_document_sync_ai_error_propagates(mock_db, mock_summarize):
    # The Celery task decides what to do with AI errors.
    mock_db.execute.return_value = returning(1)
    mock_db.get.return_value = make_document()
    error = rate_limit_error()
    mock_summarize.side_effect = error

    with pytest.raises(type(error)):
        document_tasks.process_document_sync(1, WORKER_TOKEN)

    # Only the claim ran and was committed.
    mock_db.execute.assert_called_once()
    mock_db.commit.assert_called_once()
    mock_db.close.assert_called_once()


def test_process_document_sync_claim_error_closes_session(mock_db, mock_summarize):
    mock_db.execute.side_effect = RuntimeError("database is down")

    with pytest.raises(RuntimeError, match="database is down"):
        document_tasks.process_document_sync(1, WORKER_TOKEN)

    mock_summarize.assert_not_called()
    mock_db.close.assert_called_once()


def test_process_document_sync_commit_error_rolls_back_and_raises(
    mock_db, mock_summarize
):
    mock_db.execute.side_effect = [returning(1), returning(1)]
    mock_db.get.return_value = make_document()
    mock_db.commit.side_effect = [None, RuntimeError("database is down")]

    with pytest.raises(RuntimeError, match="database is down"):
        document_tasks.process_document_sync(1, WORKER_TOKEN)

    mock_db.rollback.assert_called_once()
    mock_db.close.assert_called_once()


# ---------- mark_document_failed ----------


def test_mark_document_failed(mock_db):
    document_tasks.mark_document_failed(1, WORKER_TOKEN, "Something went wrong.")

    values = statement_values(mock_db.execute.call_args.args[0])
    assert values == {
        "status": "failed",
        "last_error": "Something went wrong.",
        "processing_claimed_at": None,
        "claim_token": None,
    }
    mock_db.commit.assert_called_once()
    mock_db.close.assert_called_once()


def test_mark_document_failed_commit_error_rolls_back_and_raises(mock_db):
    mock_db.commit.side_effect = RuntimeError("database is down")

    with pytest.raises(RuntimeError, match="database is down"):
        document_tasks.mark_document_failed(1, WORKER_TOKEN, "Something went wrong.")

    mock_db.rollback.assert_called_once()
    mock_db.close.assert_called_once()


# ---------- release_document_claim ----------


def test_release_document_claim_clears_ownership(mock_db):
    document_tasks.release_document_claim(1, WORKER_TOKEN)

    values = statement_values(mock_db.execute.call_args.args[0])
    assert values == {"processing_claimed_at": None, "claim_token": None}
    mock_db.commit.assert_called_once()
    mock_db.close.assert_called_once()


def test_release_document_claim_error_rolls_back_and_raises(mock_db):
    mock_db.execute.side_effect = RuntimeError("database is down")

    with pytest.raises(RuntimeError, match="database is down"):
        document_tasks.release_document_claim(1, WORKER_TOKEN)

    mock_db.rollback.assert_called_once()
    mock_db.commit.assert_not_called()
    mock_db.close.assert_called_once()
