"""SQLAlchemy extension with transactional, numbered schema upgrades."""
from pathlib import Path

from flask import current_app
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, scoped_session, sessionmaker


class Base(DeclarativeBase):
    pass


from .migrations import SCHEMA_VERSION, migrate


def init_app(app):
    from . import models  # Register metadata before bootstrapping.

    url = app.config["DATABASE_URL"]
    if url.startswith("sqlite:///") and not url.endswith(":memory:"):
        Path(url.removeprefix("sqlite:///")).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, connect_args={"check_same_thread": False} if url.startswith("sqlite:") else {})
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def configure_sqlite(connection, _):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    with engine.begin() as connection:
        if engine.dialect.name == "sqlite":
            # Explicit BEGIN also makes DDL transactional on legacy sqlite3 drivers.
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        tables = inspect(connection).get_table_names()
        if "schema_version" not in tables:
            if tables:
                raise RuntimeError("Database has unversioned tables; back up and migrate explicitly.")
            Base.metadata.create_all(connection)
            connection.execute(text("CREATE TABLE schema_version (version INTEGER NOT NULL)"))
            connection.execute(text("INSERT INTO schema_version (version) VALUES (:version)"), {"version": SCHEMA_VERSION})
        else:
            versions = connection.execute(text("SELECT version FROM schema_version")).scalars().all()
            migrate(connection, versions)

    sessions = scoped_session(sessionmaker(bind=engine, expire_on_commit=False))
    app.extensions["jobtracker_db"] = {"engine": engine, "sessions": sessions}

    @app.teardown_appcontext
    def remove_session(_error=None):
        sessions.remove()


def get_session():
    return current_app.extensions["jobtracker_db"]["sessions"]()
