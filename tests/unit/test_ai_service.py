from unittest.mock import MagicMock

import anthropic
import pytest

import ai_service
from config import settings
from tests.anthropic_errors import (
    authentication_error,
    bad_request_error,
    connection_error,
    rate_limit_error,
)


@pytest.fixture
def mock_client(monkeypatch):
    client = MagicMock()
    anthropic_class = MagicMock(return_value=client)
    monkeypatch.setattr(ai_service.anthropic, "Anthropic", anthropic_class)
    client.anthropic_class = anthropic_class
    return client


def test_summarize_document_returns_summary_text(mock_client):
    mock_client.messages.create.return_value.content = [
        MagicMock(text="A short summary.")
    ]

    summary = ai_service.summarize_document("Long document text.")

    assert summary == "A short summary."


def test_summarize_document_sends_text_to_configured_model(mock_client):
    mock_client.messages.create.return_value.content = [MagicMock(text="Summary")]

    ai_service.summarize_document("Long document text.")

    mock_client.anthropic_class.assert_called_once_with(
        api_key=settings.anthropic_api_key
    )

    kwargs = mock_client.messages.create.call_args.kwargs
    assert kwargs["model"] == settings.anthropic_model
    assert kwargs["max_tokens"] == 300
    assert kwargs["messages"][0]["role"] == "user"
    assert "Long document text." in kwargs["messages"][0]["content"]


# The Celery task decides whether to retry based on the error type,
# so the original Anthropic errors must reach it unchanged.
@pytest.mark.parametrize(
    "make_error, error_class",
    [
        (rate_limit_error, anthropic.RateLimitError),
        (bad_request_error, anthropic.BadRequestError),
        (authentication_error, anthropic.AuthenticationError),
        (connection_error, anthropic.APIConnectionError),
    ],
)
def test_summarize_document_propagates_anthropic_errors(
    mock_client, make_error, error_class
):
    error = make_error()
    mock_client.messages.create.side_effect = error

    with pytest.raises(error_class) as raised:
        ai_service.summarize_document("Long document text.")

    assert raised.value is error
