from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from database import get_db
from main import app
from models import Document


@pytest.fixture
def mock_db(monkeypatch):
    db = MagicMock()
    db.get.return_value = Document(
        id=1,
        filename="report.pdf",
        content="Some content.",
        summary=None,
        status="processing",
    )

    def override_get_db():
        yield db

    monkeypatch.setitem(app.dependency_overrides, get_db, override_get_db)
    return db


def claim_result(retry_count):
    result = MagicMock()
    result.scalar_one_or_none.return_value = retry_count
    return result


def event_result(rowcount):
    result = MagicMock()
    result.rowcount = rowcount
    return result


def test_claim_error_rolls_back(mock_db):
    mock_db.execute.side_effect = RuntimeError("database is down")

    with pytest.raises(RuntimeError, match="database is down"):
        TestClient(app).post("/documents/1/summarize")

    mock_db.rollback.assert_called_once()
    mock_db.commit.assert_not_called()


def test_unclaimed_document_rolls_back_before_checking_why(mock_db):
    mock_db.execute.return_value = claim_result(None)
    mock_db.get.return_value = None

    response = TestClient(app).post("/documents/1/summarize")

    assert response.status_code == 404
    mock_db.rollback.assert_called_once()
    mock_db.commit.assert_not_called()
    # Only the claim ran; no outbox event was attempted.
    mock_db.execute.assert_called_once()


def test_claim_and_event_are_committed_together(mock_db):
    mock_db.execute.side_effect = [claim_result(0), event_result(1)]

    response = TestClient(app).post("/documents/1/summarize")

    assert response.status_code == 202
    assert mock_db.execute.call_count == 2
    mock_db.commit.assert_called_once()


def test_event_conflict_does_not_commit_claim(mock_db):
    mock_db.execute.side_effect = [claim_result(0), event_result(0)]

    response = TestClient(app).post("/documents/1/summarize")

    assert response.status_code == 409
    assert response.json() == {"detail": "Processing event already exists."}
    mock_db.commit.assert_not_called()


def test_event_insert_error_rolls_back(mock_db):
    mock_db.execute.side_effect = [claim_result(0), RuntimeError("database is down")]

    with pytest.raises(RuntimeError, match="database is down"):
        TestClient(app).post("/documents/1/summarize")

    mock_db.rollback.assert_called_once()
    mock_db.commit.assert_not_called()


def test_commit_error_rolls_back(mock_db):
    mock_db.execute.side_effect = [claim_result(0), event_result(1)]
    mock_db.commit.side_effect = RuntimeError("database is down")

    with pytest.raises(RuntimeError, match="database is down"):
        TestClient(app).post("/documents/1/summarize")

    mock_db.rollback.assert_called_once()


@pytest.mark.parametrize("retry_count", [0, 4])
def test_idempotency_key_uses_retry_count(mock_db, retry_count):
    mock_db.execute.side_effect = [claim_result(retry_count), event_result(1)]

    TestClient(app).post("/documents/7/summarize")

    insert_statement = mock_db.execute.call_args_list[1].args[0]
    values = insert_statement.compile(dialect=postgresql.dialect()).params
    assert values["idempotency_key"] == f"document.process:7:{retry_count}"
    assert values["payload"] == {"doc_id": 7}
    assert values["event_type"] == "document_processing_started"
