import uuid
from datetime import datetime, timezone

import anthropic
from sqlalchemy import update

from ai_service import summarize_document
from celery_app import celery_app
from database import SessionLocal
from models import Document


def process_document_sync(doc_id: int, claim_token: str):
    db = SessionLocal()

    try:
        claim_statement = (
            update(Document)
            .where(
                Document.id == doc_id,
                Document.status == "processing",
                Document.summary.is_(None),
                Document.processing_claimed_at.is_(None),
            )
            .values(
                processing_claimed_at=datetime.now(timezone.utc),
                claim_token=claim_token,
            )
            .returning(Document.id)
        )

        result = db.execute(claim_statement)
        claimed_document_id = result.scalar_one_or_none()

        if claimed_document_id is None:
            db.rollback()
            return

        db.commit()

        document = db.get(Document, claimed_document_id)

        if document is None:
            return

        if not document.content:
            raise ValueError("Document content is empty")

        summary = summarize_document(document.content)

        update_statement = (
            update(Document)
            .where(Document.id == doc_id, Document.claim_token == claim_token)
            .values(
                summary=summary,
                status="processed",
                last_error=None,
                processing_claimed_at=None,
                claim_token=None,
            )
            .returning(Document.id)
        )

        update_statement_result = db.execute(update_statement)
        document_id = update_statement_result.scalar_one_or_none()

        if document_id is None:
            db.rollback()
            return

        try:
            db.commit()
        except Exception:
            db.rollback()
            raise

    finally:
        db.close()


def mark_document_failed(doc_id: int, claim_token: str, error_message: str):
    db = SessionLocal()
    try:
        db.execute(
            update(Document)
            .where(
                Document.id == doc_id,
                Document.status == "processing",
                Document.claim_token == claim_token,
            )
            .values(
                status="failed",
                last_error=error_message,
                processing_claimed_at=None,
                claim_token=None,
            )
        )

        try:
            db.commit()
        except Exception:
            db.rollback()
            raise

    finally:
        db.close()


@celery_app.task(
    bind=True,
    name="documents.process_document",
    max_retries=3,
)
def process_document(self, doc_id: int):

    claim_token = str(uuid.uuid4())

    try:
        process_document_sync(doc_id, claim_token)

    except (
        anthropic.APIConnectionError,
        anthropic.RateLimitError,
        anthropic.InternalServerError,
    ) as error:
        if self.request.retries >= self.max_retries:
            mark_document_failed(doc_id, claim_token, str(error))
            raise

        release_document_claim(doc_id, claim_token)

        # Celery's documented retry pattern; retry() chains exc itself.
        raise self.retry(  # noqa: B904
            exc=error,
            countdown=5 * (2**self.request.retries),
        )

    except anthropic.APIStatusError as error:
        mark_document_failed(doc_id, claim_token, str(error))
        raise

    except Exception as error:
        mark_document_failed(doc_id, claim_token, str(error))
        raise


def release_document_claim(doc_id: int, claim_token: str):
    db = SessionLocal()

    try:
        db.execute(
            update(Document)
            .where(
                Document.id == doc_id,
                Document.status == "processing",
                Document.claim_token == claim_token,
            )
            .values(processing_claimed_at=None, claim_token=None)
        )
        db.commit()

    except Exception:
        db.rollback()
        raise

    finally:
        db.close()
