from datetime import datetime, timezone

from sqlalchemy import select

from database import SessionLocal
from document_tasks import process_document
from outbox_models import OutboxEvent


def dispatch_pending_events(batch_size: int = 10):
    db = SessionLocal()
    published_count = 0

    try:
        for _ in range(batch_size):
            event = db.execute(
                select(OutboxEvent)
                .where(OutboxEvent.status == "pending")
                .order_by(OutboxEvent.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            ).scalar_one_or_none()

            if event is None:
                db.rollback()
                break

            doc_id = event.payload["doc_id"]

            # Publish the task to Celery
            process_document.delay(doc_id)

            # Mark event as published only after successful publication
            event.status = "published"
            event.published_at = datetime.now(timezone.utc)

            db.commit()
            published_count += 1

        return published_count

    except Exception:
        db.rollback()
        raise

    finally:
        db.close()
