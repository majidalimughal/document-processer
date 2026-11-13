from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

import database
from celery_app import celery_app
from config import Settings

REQUIRED_SETTINGS = {
    "DATABASE_URL": "postgresql://user:pass@localhost/app",
    "TEST_DATABASE_URL": "postgresql://user:pass@localhost/app_test",
    "REDIS_URL": "redis://localhost:6379/0",
    "ANTHROPIC_API_KEY": "test-key",
    "ANTHROPIC_MODEL": "test-model",
}


# ---------- config.py ----------


def test_settings_read_environment_variables(monkeypatch):
    for name, value in REQUIRED_SETTINGS.items():
        monkeypatch.setenv(name, value)

    settings = Settings(_env_file=None)

    assert settings.database_url == REQUIRED_SETTINGS["DATABASE_URL"]
    assert settings.test_database_url == REQUIRED_SETTINGS["TEST_DATABASE_URL"]
    assert settings.redis_url == REQUIRED_SETTINGS["REDIS_URL"]
    assert settings.anthropic_api_key == REQUIRED_SETTINGS["ANTHROPIC_API_KEY"]
    assert settings.anthropic_model == REQUIRED_SETTINGS["ANTHROPIC_MODEL"]


@pytest.mark.parametrize("missing", list(REQUIRED_SETTINGS))
def test_settings_require_every_value(monkeypatch, missing):
    for name, value in REQUIRED_SETTINGS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(missing)

    with pytest.raises(ValidationError, match=missing.lower()):
        Settings(_env_file=None)


# ---------- database.py ----------


def test_get_db_yields_session_and_closes_it(monkeypatch):
    session = MagicMock()
    monkeypatch.setattr(database, "SessionLocal", MagicMock(return_value=session))

    dependency = database.get_db()
    assert next(dependency) is session
    session.close.assert_not_called()

    with pytest.raises(StopIteration):
        next(dependency)

    session.close.assert_called_once()


def test_get_db_closes_session_on_error(monkeypatch):
    session = MagicMock()
    monkeypatch.setattr(database, "SessionLocal", MagicMock(return_value=session))

    dependency = database.get_db()
    next(dependency)

    with pytest.raises(RuntimeError):
        dependency.throw(RuntimeError("request failed"))

    session.close.assert_called_once()


def test_session_factory_does_not_autoflush():
    assert database.SessionLocal.kw["autoflush"] is False


# ---------- celery_app.py ----------


def test_celery_app_uses_json_and_utc():
    assert celery_app.main == "document_processor"
    assert celery_app.conf.task_serializer == "json"
    assert celery_app.conf.result_serializer == "json"
    assert celery_app.conf.accept_content == ["json"]
    assert celery_app.conf.timezone == "UTC"
    assert celery_app.conf.enable_utc is True


def test_celery_app_registers_document_task():
    assert "document_tasks" in celery_app.conf.include
    assert "documents.process_document" in celery_app.tasks
