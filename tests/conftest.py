import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

import database
import document_tasks
import outbox_dispatcher
from celery_app import celery_app
from config import settings
from database import Base, get_db
from main import app
from models import Document
from outbox_models import OutboxEvent

# Run Celery tasks in-process and never talk to the real Redis broker.
celery_app.conf.update(
    broker_url="memory://",
    task_always_eager=True,
    task_eager_propagates=False,
)

# Never let the tests touch the development database.
if make_url(settings.test_database_url).database == make_url(settings.database_url).database:
    raise RuntimeError("TEST_DATABASE_URL must point to a separate database.")

# Creating the engine does not connect, so unit tests run without PostgreSQL.
test_engine = create_engine(
    settings.test_database_url,
    pool_size=5,
    max_overflow=5,
)

TestingSessionLocal = sessionmaker(
    bind=test_engine,
    autoflush=False,
    autocommit=False,
)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


def clear_tables():
    db = TestingSessionLocal()
    try:
        db.execute(delete(OutboxEvent))
        db.execute(delete(Document))
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def use_test_database(monkeypatch):
    # Everything that opens a session must use the test database.
    monkeypatch.setitem(app.dependency_overrides, get_db, override_get_db)
    monkeypatch.setattr(database, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(document_tasks, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(outbox_dispatcher, "SessionLocal", TestingSessionLocal)


@pytest.fixture(scope="session")
def test_database():
    Base.metadata.create_all(bind=test_engine)
    yield test_engine
    test_engine.dispose()


@pytest.fixture
def test_client(test_database):
    clear_tables()
    try:
        with TestClient(app) as client:
            yield client
    finally:
        clear_tables()


@pytest.fixture
def db_session(test_database):
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.rollback()
        db.close()
