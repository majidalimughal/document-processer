import uuid
from unittest.mock import ANY, MagicMock

import pytest
from celery.exceptions import Retry

import document_tasks
from document_tasks import process_document
from tests.anthropic_errors import (
    authentication_error,
    bad_request_error,
    connection_error,
    internal_server_error,
    rate_limit_error,
)

RETRYABLE_ERRORS = [rate_limit_error, connection_error, internal_server_error]
PERMANENT_ERRORS = [bad_request_error, authentication_error]


@pytest.fixture
def mock_sync(monkeypatch):
    mock = MagicMock()
    monkeypatch.setattr(document_tasks, "process_document_sync", mock)
    return mock


@pytest.fixture
def mock_mark_failed(monkeypatch):
    mock = MagicMock()
    monkeypatch.setattr(document_tasks, "mark_document_failed", mock)
    return mock


@pytest.fixture(autouse=True)
def mock_release_claim(monkeypatch):
    mock = MagicMock()
    monkeypatch.setattr(document_tasks, "release_document_claim", mock)
    return mock


@pytest.fixture
def mock_retry(monkeypatch):
    # Celery's real retry() raises Retry, so the mock does the same.
    mock = MagicMock(side_effect=Retry())
    monkeypatch.setattr(process_document, "retry", mock)
    return mock


def run_task(doc_id, retries):
    # Simulate the task running as attempt number `retries`.
    process_document.push_request(retries=retries)
    try:
        return process_document.run(doc_id)
    finally:
        process_document.pop_request()


def token_used(mock_sync):
    # The claim token the task generated for this run.
    return mock_sync.call_args.args[1]


def test_task_is_configured_for_three_retries():
    assert process_document.name == "documents.process_document"
    assert process_document.max_retries == 3


def test_task_success_does_not_retry(mock_sync, mock_mark_failed, mock_retry):
    run_task(1, retries=0)

    mock_sync.assert_called_once_with(1, ANY)
    mock_retry.assert_not_called()
    mock_mark_failed.assert_not_called()


@pytest.mark.parametrize("retries, expected_countdown", [(0, 5), (1, 10), (2, 20)])
def test_rate_limit_error_retries_with_exponential_backoff(
    mock_sync, mock_mark_failed, mock_retry, retries, expected_countdown
):
    error = rate_limit_error()
    mock_sync.side_effect = error

    with pytest.raises(Retry):
        run_task(1, retries=retries)

    mock_retry.assert_called_once_with(exc=error, countdown=expected_countdown)
    # The document stays "processing" while a retry is pending.
    mock_mark_failed.assert_not_called()


@pytest.mark.parametrize("make_error", RETRYABLE_ERRORS)
def test_transient_errors_are_retried(mock_sync, mock_mark_failed, mock_retry, make_error):
    error = make_error()
    mock_sync.side_effect = error

    with pytest.raises(Retry):
        run_task(1, retries=0)

    mock_retry.assert_called_once_with(exc=error, countdown=5)
    mock_mark_failed.assert_not_called()


@pytest.mark.parametrize("make_error", RETRYABLE_ERRORS)
def test_document_fails_after_retries_are_exhausted(
    mock_sync, mock_mark_failed, mock_retry, make_error
):
    error = make_error()
    mock_sync.side_effect = error

    with pytest.raises(type(error)) as raised:
        run_task(1, retries=3)

    assert raised.value is error
    mock_retry.assert_not_called()
    mock_mark_failed.assert_called_once_with(1, token_used(mock_sync), str(error))


@pytest.mark.parametrize("make_error", PERMANENT_ERRORS)
def test_permanent_errors_never_retry(mock_sync, mock_mark_failed, mock_retry, make_error):
    error = make_error()
    mock_sync.side_effect = error

    with pytest.raises(type(error)) as raised:
        run_task(1, retries=0)

    assert raised.value is error
    mock_retry.assert_not_called()
    mock_mark_failed.assert_called_once_with(1, token_used(mock_sync), str(error))


def test_unexpected_error_marks_failed_without_retry(
    mock_sync, mock_mark_failed, mock_retry
):
    mock_sync.side_effect = ValueError("Unexpected problem.")

    with pytest.raises(ValueError, match="Unexpected problem."):
        run_task(1, retries=0)

    mock_retry.assert_not_called()
    mock_mark_failed.assert_called_once_with(
        1, token_used(mock_sync), "Unexpected problem."
    )


def test_retry_releases_claim_before_retrying(
    mock_sync, mock_mark_failed, mock_retry, mock_release_claim
):
    calls = []
    mock_release_claim.side_effect = lambda doc_id, token: calls.append("release")
    mock_retry.side_effect = lambda **kwargs: calls.append("retry") or Retry()
    mock_sync.side_effect = rate_limit_error()

    with pytest.raises(Retry):
        run_task(1, retries=0)

    # The retried task must be able to claim the document again.
    mock_release_claim.assert_called_once_with(1, token_used(mock_sync))
    assert calls == ["release", "retry"]


def test_exhausted_retries_do_not_release_claim(
    mock_sync, mock_mark_failed, mock_retry, mock_release_claim
):
    mock_sync.side_effect = rate_limit_error()

    with pytest.raises(type(mock_sync.side_effect)):
        run_task(1, retries=3)

    # mark_document_failed clears the claim instead.
    mock_release_claim.assert_not_called()


@pytest.mark.parametrize("make_error", PERMANENT_ERRORS)
def test_permanent_errors_do_not_release_claim(
    mock_sync, mock_mark_failed, mock_retry, mock_release_claim, make_error
):
    mock_sync.side_effect = make_error()

    with pytest.raises(type(mock_sync.side_effect)):
        run_task(1, retries=0)

    mock_release_claim.assert_not_called()


def test_each_run_uses_a_new_uuid_claim_token(mock_sync, mock_mark_failed, mock_retry):
    run_task(1, retries=0)
    run_task(1, retries=1)

    first_token, second_token = (call.args[1] for call in mock_sync.call_args_list)

    assert str(uuid.UUID(first_token)) == first_token
    # A retried run must not reuse the token of the run it replaces.
    assert first_token != second_token
