import pytest
from pydantic import ValidationError

from main import DocumentCreate, DocumentDTO, DocumentResponseDTO, DocumentStatusUpdate
from models import Document


def test_document_create_strips_whitespace():
    document = DocumentCreate(filename="  report.pdf  ", content="\n  Some content.\t")

    assert document.filename == "report.pdf"
    assert document.content == "Some content."


@pytest.mark.parametrize("field", ["filename", "content"])
@pytest.mark.parametrize("value", ["", "   ", "\n\t "])
def test_document_create_rejects_empty_or_whitespace(field, value):
    payload = {"filename": "report.pdf", "content": "Some content."}
    payload[field] = value

    with pytest.raises(ValidationError):
        DocumentCreate(**payload)


@pytest.mark.parametrize("missing_field", ["filename", "content"])
def test_document_create_requires_fields(missing_field):
    payload = {"filename": "report.pdf", "content": "Some content."}
    del payload[missing_field]

    with pytest.raises(ValidationError):
        DocumentCreate(**payload)


def test_document_create_rejects_non_string_values():
    with pytest.raises(ValidationError):
        DocumentCreate(filename=123, content="Some content.")


@pytest.mark.parametrize("status", ["pending", "processing", "processed", "failed"])
def test_status_update_accepts_known_statuses(status):
    assert DocumentStatusUpdate(status=status).status == status


@pytest.mark.parametrize("status", ["done", "PENDING", "", None])
def test_status_update_rejects_unknown_statuses(status):
    with pytest.raises(ValidationError):
        DocumentStatusUpdate(status=status)


def test_response_dto_reads_from_model():
    document = Document(id=1, filename="report.pdf", content="Secret", status="pending")

    data = DocumentResponseDTO.model_validate(document).model_dump()

    # The create response must not leak the content.
    assert data == {"id": 1, "filename": "report.pdf", "status": "pending"}


def test_document_dto_reads_from_model():
    document = Document(
        id=1,
        filename="report.pdf",
        content="Some content.",
        summary=None,
        status="pending",
    )

    data = DocumentDTO.model_validate(document).model_dump()

    assert data == {
        "id": 1,
        "filename": "report.pdf",
        "content": "Some content.",
        "summary": None,
        "status": "pending",
    }
