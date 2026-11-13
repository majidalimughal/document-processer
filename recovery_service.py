from datetime import datetime, timedelta, timezone

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert

from database import SessionLocal
from models import Document
from outbox_models import OutboxEvent

MAX_ATTEMPTS = 5


def recover_stuck_documents():
    db = SessionLocal()

    try:
        expiry_time = datetime.now(timezone.utc) - timedelta(minutes=10)

        # Recover documents that still have attempts available.
        recovered_document_statement = (
            update(Document)
            .where(
                Document.status == "processing",
                Document.processing_claimed_at < expiry_time,
                Document.retry_count < MAX_ATTEMPTS - 1,
            )
            .values(
                # Stay "processing" so the queued task can claim it again.
                status="processing",
                retry_count=Document.retry_count + 1,
                processing_claimed_at=None,
                claim_token=None,
            )
            .returning(
                Document.id,
                Document.retry_count,
                Document.last_error,
            )
        )

        recovered_documents = db.execute(recovered_document_statement).mappings().all()

        # Build one event per recovered document.
        outbox_events = [
            {
                "idempotency_key": (
                    f"document.process:{doc['id']}:{doc['retry_count']}"
                ),
                "event_type": "document_processing_started",
                "payload": {
                    "doc_id": doc["id"],
                    "retry_count": doc["retry_count"],
                    "last_error": doc["last_error"],
                },
                "status": "pending",
            }
            for doc in recovered_documents
        ]

        # Insert all events in one bulk statement.
        if outbox_events:
            insert_outbox_events_statement = (
                insert(OutboxEvent)
                .values(outbox_events)
                .on_conflict_do_nothing(index_elements=[OutboxEvent.idempotency_key])
            )

            db.execute(insert_outbox_events_statement)

        # Mark documents with exhausted attempts as failed.
        exhausted_documents_statement = (
            update(Document)
            .where(
                Document.status == "processing",
                Document.processing_claimed_at < expiry_time,
                Document.retry_count >= MAX_ATTEMPTS - 1,
            )
            .values(
                status="failed",
                processing_claimed_at=None,
                claim_token=None,
            )
        )

        db.execute(exhausted_documents_statement)

        db.commit()

        return recovered_documents

    except Exception:
        db.rollback()
        raise

    finally:
        db.close()
