import anthropic
import httpx

REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def make_status_error(error_class, status_code, message="Error from the AI provider."):
    response = httpx.Response(status_code, request=REQUEST)
    return error_class(message, response=response, body=None)


def rate_limit_error():
    return make_status_error(anthropic.RateLimitError, 429, "Rate limit exceeded.")


def bad_request_error():
    return make_status_error(
        anthropic.BadRequestError, 400, "Your credit balance is too low."
    )


def authentication_error():
    return make_status_error(anthropic.AuthenticationError, 401, "Invalid API key.")


def internal_server_error():
    return make_status_error(anthropic.InternalServerError, 500, "Internal server error.")


def connection_error():
    return anthropic.APIConnectionError(request=REQUEST)
