from apscheduler.schedulers.blocking import BlockingScheduler

from recovery_service import recover_stuck_documents

scheduler = BlockingScheduler(timezone="UTC")


def run_recovery():
    try:
        recovered = recover_stuck_documents()
        print(f"Recovery completed. Documents recovered: {len(recovered)}")
    # Catch everything so one failed run doesn't stop the scheduler.
    except Exception as e:  # noqa: BLE001
        print(f"Recovery failed: {e}")


scheduler.add_job(
    run_recovery,
    trigger="interval",
    minutes=2,
    id="document_recovery",
    max_instances=1,
    coalesce=True,
)

if __name__ == "__main__":
    print("Document recovery scheduler started...")
    scheduler.start()
