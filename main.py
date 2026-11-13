from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel, Field, field_validator, ConfigDict
from database import get_db
from models import Document
from outbox_models import OutboxEvent
from sqlalchemy.orm import Session
from typing import Literal
from sqlalchemy import update, case
from sqlalchemy.dialects.postgresql import insert

app = FastAPI(title="AI Document API")


class DocumentCreate(BaseModel):
    filename: str = Field(..., min_length=1)
    content: str = Field(..., min_length=1)

    @field_validator("filename", "content", mode="before")
    @classmethod
    def strip_and_validate_not_empty(cls, value: str) -> str:
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                raise ValueError("Field cannot be empty or consist only of whitespace")
            return stripped
        return value


class DocumentStatusUpdate(BaseModel):
    status: Literal["pending", "processing", "processed", "failed"]


class DocumentResponseDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    filename: str
    status: str


class DocumentDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    filename: str
    content: str
    summary: str | None
    status: str


@app.get("/health")
def health_check():
    return {"status": "healthy"}


@app.get("/")
def root():
    return {"message": "Welcome to AI Document API"}


@app.post("/documents", response_model=DocumentResponseDTO)
def create_document(document: DocumentCreate, db: Session = Depends(get_db)):
    new_doc = Document(
        filename=document.filename, content=document.content, status="pending"
    )
    db.add(new_doc)
    try:
        db.commit()
        db.refresh(new_doc)
        return new_doc
    except Exception:
        db.rollback()
        raise


@app.get("/documents", response_model=list[DocumentDTO])
def get_all_documents(db: Session = Depends(get_db)):
    return db.query(Document).all()


@app.get("/documents/{doc_id}", response_model=DocumentDTO)
def get_document(doc_id: int, db: Session = Depends(get_db)):
    document = db.get(Document, doc_id)

    if document is None:
        raise HTTPException(
            status_code=404,
            detail="Document not found",
        )

    return document


# @app.patch("/documents/{doc_id}", response_model=DocumentDTO)
# def patch_document(
#     doc_id: int, document_update: DocumentStatusUpdate, db: Session = Depends(get_db)
# ):
#     document = db.get(Document, doc_id)

#     if document is None:
#         raise HTTPException(
#             status_code=404,
#             detail="Document not found",
#         )

#     document.status = document_update.status
#     return commit_document_transaction(document, db)


@app.post(
    "/documents/{doc_id}/summarize",
    response_model=DocumentDTO,
    status_code=202,
)
def summarize_document(
    doc_id: int,
    db: Session = Depends(get_db),
):

    
    claim_statement = (
        update(Document)
        .where(
            Document.id == doc_id,
            Document.status.in_(["pending", "failed"]),
            Document.summary.is_(None),
        )
        .values(
            status="processing",
            last_error=None,
            retry_count=case(
                (Document.status == "failed", Document.retry_count + 1),
                else_=Document.retry_count,
            ),
        ).returning(Document.retry_count)
    )

    try:
        result = db.execute(claim_statement)

        document_retry_number = result.scalar_one_or_none()

        if document_retry_number is None:
            db.rollback()

            document = db.get(Document, doc_id)

            if document is None:
                raise HTTPException(
                    status_code=404,
                    detail="Document not found",
                )

            if document.summary is not None:
                raise HTTPException(
                    status_code=409,
                    detail="Document has already been summarized.",
                )

            raise HTTPException(
                status_code=409,
                detail="Document is already being processed or is not eligible.",
            )

        # 2. Create idempotency key
        event_idempotency_key = f"document.process:{doc_id}:{document_retry_number}"

        # 3. Insert outbox event
        insert_outbox_event_statement = (
            insert(OutboxEvent)
            .values(
                idempotency_key=event_idempotency_key,
                event_type="document_processing_started",
                payload={"doc_id": doc_id},
                status="pending",
            )
            .on_conflict_do_nothing(
                index_elements=[OutboxEvent.idempotency_key]
            )
        )

        event_result = db.execute(insert_outbox_event_statement)

        if event_result.rowcount == 0:
            raise HTTPException(
                status_code=409,
                detail="Processing event already exists.",
            )


        db.commit()

        document = db.get(Document, doc_id)

        return document

    except HTTPException:
        raise
    except Exception:
        db.rollback()
        raise



def commit_document_transaction(document: Document, db: Session) -> Document:
    try:
        db.commit()
        db.refresh(document)
        return document
    except Exception:
        db.rollback()
        raise
