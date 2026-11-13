from celery import Celery

from config import settings

celery_app = Celery(
    "document_processor",
    broker=settings.redis_url,
    include=[
        "document_tasks",
        "tasks.recovery_tasks",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
)

celery_app.conf.beat_schedule = {
    "recover-stuck-documents-every-two-minutes": {
        "task": "tasks.recover_stuck_documents",
        "schedule": 120.0,
    },
}
