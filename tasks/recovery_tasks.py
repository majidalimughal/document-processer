from celery_app import celery_app
from recovery_service import recover_stuck_documents


@celery_app.task(name="tasks.recover_stuck_documents")
def recover_stuck_documents_task():
    recovered_documents = recover_stuck_documents()

    return {
        "recovered_count": len(recovered_documents)
    }