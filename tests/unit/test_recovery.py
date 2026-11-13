from unittest.mock import MagicMock

import pytest
from sqlalchemy.dialects import postgresql

import recovery_scheduler
import recovery_service
from celery_app import celery_app
from tasks import recovery_tasks


@pytest.fixture
def mock_db(monkeypatch):
    db = MagicMock()
    monkeypatch.setattr(recovery_service, "SessionLocal", MagicMock(return_value=db))
    return db


def recovered(rows):
    result = MagicMock()
    result.mappings.return_value.all.return_value = rows
    return result


def compiled(statement):
    return statement.compile(dialect=postgresql.dialect())


# ---------- recovery_service.py ----------


def test_max_attempts():
    assert recovery_service.MAX_ATTEMPTS == 5


def test_recovery_runs_in_one_transaction(mock_db):
    rows = [{"id": 1, "retry_count": 2, "last_error": "Worker crashed."}]
    mock_db.execute.side_effect = [recovered(rows), MagicMock(), MagicMock()]

    assert recovery_service.recover_stuck_documents() == rows

    # Recover, insert events, fail exhausted documents, then one commit.
    assert mock_db.execute.call_count == 3
    mock_db.commit.assert_called_once()
    mock_db.rollback.assert_not_called()
    mock_db.close.assert_called_once()


def test_recovery_builds_one_event_per_document(mock_db):
    rows = [
        {"id": 1, "retry_count": 1, "last_error": None},
        {"id": 2, "retry_count": 3, "last_error": "Timeout."},
    ]
    mock_db.execute.side_effect = [recovered(rows), MagicMock(), MagicMock()]

    recovery_service.recover_stuck_documents()

    insert_sql = compiled(mock_db.execute.call_args_list[1].args[0])
    assert "ON CONFLICT (idempotency_key) DO NOTHING" in str(insert_sql)
    params = insert_sql.params
    assert params["idempotency_key_m0"] == "document.process:1:1"
    assert params["idempotency_key_m1"] == "document.process:2:3"
    assert params["payload_m1"] == {
        "doc_id": 2,
        "retry_count": 3,
        "last_error": "Timeout.",
    }


def test_recovery_skips_insert_when_nothing_recovered(mock_db):
    mock_db.execute.side_effect = [recovered([]), MagicMock()]

    assert recovery_service.recover_stuck_documents() == []

    # Only the recover and fail-exhausted updates ran.
    assert mock_db.execute.call_count == 2
    mock_db.commit.assert_called_once()


def test_recovery_rolls_back_everything_on_error(mock_db):
    rows = [{"id": 1, "retry_count": 1, "last_error": None}]
    mock_db.execute.side_effect = [recovered(rows), RuntimeError("database is down")]

    with pytest.raises(RuntimeError, match="database is down"):
        recovery_service.recover_stuck_documents()

    # The documents must not be reset without their events.
    mock_db.rollback.assert_called_once()
    mock_db.commit.assert_not_called()
    mock_db.close.assert_called_once()


def test_recovery_commit_error_rolls_back(mock_db):
    mock_db.execute.side_effect = [recovered([]), MagicMock()]
    mock_db.commit.side_effect = RuntimeError("database is down")

    with pytest.raises(RuntimeError, match="database is down"):
        recovery_service.recover_stuck_documents()

    mock_db.rollback.assert_called_once()
    mock_db.close.assert_called_once()


# ---------- tasks/recovery_tasks.py ----------


def test_recovery_task_returns_recovered_count(monkeypatch):
    monkeypatch.setattr(
        recovery_tasks, "recover_stuck_documents", MagicMock(return_value=[{}, {}, {}])
    )

    assert recovery_tasks.recover_stuck_documents_task.run() == {"recovered_count": 3}


def test_recovery_task_propagates_errors(monkeypatch):
    monkeypatch.setattr(
        recovery_tasks,
        "recover_stuck_documents",
        MagicMock(side_effect=RuntimeError("database is down")),
    )

    with pytest.raises(RuntimeError, match="database is down"):
        recovery_tasks.recover_stuck_documents_task.run()


# ---------- celery_app.py beat schedule ----------


def test_recovery_task_is_registered():
    assert "tasks.recovery_tasks" in celery_app.conf.include
    assert "tasks.recover_stuck_documents" in celery_app.tasks


def test_beat_runs_recovery_every_two_minutes():
    entry = celery_app.conf.beat_schedule["recover-stuck-documents-every-two-minutes"]

    assert entry["schedule"] == 120.0
    # The schedule must point at a task that actually exists.
    assert entry["task"] == recovery_tasks.recover_stuck_documents_task.name


# ---------- recovery_scheduler.py ----------


def test_scheduler_job_configuration():
    job = recovery_scheduler.scheduler.get_job("document_recovery")

    assert job is not None
    assert job.func is recovery_scheduler.run_recovery
    assert job.trigger.interval.total_seconds() == 120
    assert job.max_instances == 1
    assert job.coalesce is True
    assert str(recovery_scheduler.scheduler.timezone) == "UTC"


def test_scheduler_is_not_started_on_import():
    assert recovery_scheduler.scheduler.running is False


def test_run_recovery_reports_count(monkeypatch, capsys):
    monkeypatch.setattr(
        recovery_scheduler, "recover_stuck_documents", MagicMock(return_value=[{}, {}])
    )

    recovery_scheduler.run_recovery()

    assert capsys.readouterr().out == "Recovery completed. Documents recovered: 2\n"


def test_run_recovery_survives_errors(monkeypatch, capsys):
    # An exception would kill the scheduled job, so it must be caught.
    monkeypatch.setattr(
        recovery_scheduler,
        "recover_stuck_documents",
        MagicMock(side_effect=RuntimeError("database is down")),
    )

    recovery_scheduler.run_recovery()

    assert capsys.readouterr().out == "Recovery failed: database is down\n"
