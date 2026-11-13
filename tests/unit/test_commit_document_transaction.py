from unittest.mock import MagicMock

import pytest

from main import commit_document_transaction
from models import Document


def test_commit_document_transaction_commits_and_refreshes():
    db = MagicMock()
    document = Document(filename="report.pdf", content="Some content.")

    result = commit_document_transaction(document, db)

    assert result is document
    db.commit.assert_called_once()
    db.refresh.assert_called_once_with(document)
    db.rollback.assert_not_called()


def test_commit_document_transaction_rolls_back_on_error():
    db = MagicMock()
    db.commit.side_effect = RuntimeError("database is down")
    document = Document(filename="report.pdf", content="Some content.")

    with pytest.raises(RuntimeError, match="database is down"):
        commit_document_transaction(document, db)

    db.rollback.assert_called_once()
    db.refresh.assert_not_called()
