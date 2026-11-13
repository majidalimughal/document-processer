from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from config import settings
from database import Base

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def empty_database_url(monkeypatch):
    # Migrations run against a throwaway database, never the dev or test one.
    test_url = make_url(settings.test_database_url)
    database_name = f"ai_documents_migrations_{uuid4().hex[:8]}"

    admin_engine = create_engine(
        test_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))

    url = test_url.set(database=database_name)
    # alembic/env.py reads the URL from settings when called with -x db=test.
    monkeypatch.setattr(
        settings, "test_database_url", url.render_as_string(hide_password=False)
    )

    try:
        yield url
    finally:
        with admin_engine.connect() as connection:
            connection.execute(
                text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
            )
        admin_engine.dispose()


def alembic_config():
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.cmd_opts = type("CmdOpts", (), {"x": ["db=test"]})()
    return config


def test_migrations_upgrade_to_match_models(empty_database_url):
    command.upgrade(alembic_config(), "head")

    engine = create_engine(empty_database_url)
    try:
        with engine.connect() as connection:
            differences = compare_metadata(
                MigrationContext.configure(connection), Base.metadata
            )
    finally:
        engine.dispose()

    # A difference means a model changed without a matching migration.
    assert differences == []


def test_migrations_downgrade_and_upgrade_again(empty_database_url):
    config = alembic_config()

    command.upgrade(config, "head")
    command.downgrade(config, "base")

    engine = create_engine(empty_database_url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert tables <= {"alembic_version"}

    command.upgrade(config, "head")
