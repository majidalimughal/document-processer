import anthropic
from config import settings


def summarize_document(text: str) -> str:

    try:
        # Initialize the Anthropic client using settings
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

        # Call Claude's Messages API
        response = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=300,
            messages=[
                {
                    "role": "user",
                    "content": f"Summarize this document in a few clear sentences:\n\n{text}",
                }
            ],
        )

        # Extract the generated text from the response content blocks
        # response.content is a list of blocks, and the first text block contains our summary
        return response.content[0].text
    except anthropic.APIStatusError:
        raise 
    except anthropic.APIConnectionError:
        raise 