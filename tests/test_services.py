from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from flask import Flask
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.exc import StaleDataError

from jobtracker import db, services
from jobtracker.models import Job


@pytest.fixture
def app(tmp_path):
    app = Flask(__name__)
    app.config["DATABASE_URL"] = "sqlite:///" + str(tmp_path / "tracker.sqlite")
    db.init_app(app)
    return app


def test_defaults_persistence_and_sqlite_settings(app):
    with app.app_context():
        job = services.create_job(1, {"url": "https://example.com/roles/123"})
        assert job["title"] == "Untitled role"
        assert job["found_date"] == datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
        assert job["stage"] == "interesting"
        assert job["version"] == 1
        assert job["created_at"].endswith("Z")
        session = db.get_session()
        assert session.scalar(text("PRAGMA foreign_keys")) == 1
        assert session.scalar(text("PRAGMA journal_mode")) == "wal"
        assert session.scalar(text("PRAGMA busy_timeout")) == 5000
    with app.app_context():
        assert services.get_job(1, job["id"]) == job
        assert services.list_activities(1, job["id"])[0]["kind"] == "created"


def test_stage_and_outcome_independent_with_atomic_history(app):
    with app.app_context():
        job = services.create_job(1, {"title": "Engineer"})
        changed = services.update_job(1, job["id"], {"stage": "offer", "outcome": "rejected", "version": 1})
        assert changed["version"] == 2
        assert changed["stage"] == "offer"
        assert changed["outcome"] == "rejected"
        activity = services.list_activities(1, job["id"])[-1]
        assert activity["details"]["stage"] == {"from": "interesting", "to": "offer"}
        with pytest.raises(services.ServiceError) as error:
            services.update_job(1, job["id"], {"stage": "applied", "version": 1})
        assert error.value.status_code == 409
        assert services.get_job(1, job["id"]) == changed
        assert len(services.list_activities(1, job["id"])) == 2


def test_history_failure_rolls_back_job_update(app, monkeypatch):
    with app.app_context():
        job = services.create_job(1, {"description": "Pasted posting"})
        def fail(*args, **kwargs):
            raise RuntimeError("Simulated history failure")
        monkeypatch.setattr(services, "_activity", fail)
        with pytest.raises(RuntimeError):
            services.update_job(1, job["id"], {"stage": "applied"})
        assert services.get_job(1, job["id"])["stage"] == "interesting"
        assert len(services.list_activities(1, job["id"])) == 1


@pytest.mark.parametrize("data", [
    {}, {"title": None}, {"title": "a", "stage": "bogus"},
    {"title": "a", "found_date": "2026-02-30"},
    {"title": "a", "found_date": "20261019"},
    {"url": "javascript:alert(1)"}, {"url": "https://user:pass@example.com"},
    {"title": "a", "id": "client-chosen"}, {"title": "a", "priority": []},
])
def test_validation_leaves_no_rows(app, data):
    with app.app_context():
        with pytest.raises(services.ServiceError):
            services.create_job(1, data)
        assert services.list_jobs(1) == []


def test_contacts_and_versioned_resume_metadata(app):
    with app.app_context():
        job = services.create_job(1, {"title": "Engineer", "found_date": "2026-01-01"})
        contact = services.create_contact(1, job["id"], {"name": "Recruiter", "email": "recruiter@example.com"})
        assert services.list_contacts(1, job["id"]) == [contact]
        metadata = {"original_filename": "CV.pdf", "stored_filename": "opaque-1.pdf", "media_type": "application/pdf", "size": 42, "sha256": "a" * 64}
        first = services.create_resume(1, job["id"], metadata)
        second = services.create_resume(1, job["id"], {**metadata, "stored_filename": "opaque-2.pdf"})
        assert services.list_resumes(1, job["id"]) == [first, second]
        assert services.get_resume(1, first["id"]) == first
        assert [item["kind"] for item in services.list_activities(1, job["id"])] == ["created", "contact_added", "resume_added", "resume_added"]
        with pytest.raises(services.ServiceError):
            services.create_resume(1, job["id"], {**metadata, "stored_filename": "../../escape"})
        with pytest.raises(services.ServiceError):
            services.create_contact(1, job["id"], {"email": "invalid"})


def test_missing_related_records_and_foreign_keys(app):
    with app.app_context():
        with pytest.raises(services.ServiceError) as error:
            services.create_contact(1, "missing", {"name": "Person"})
        assert error.value.status_code == 404
        with pytest.raises(IntegrityError):
            db.get_session().execute(text("INSERT INTO contacts (id,job_id,name,email,notes,created_at) VALUES ('id','missing','','','','2026-01-01')"))
        db.get_session().rollback()


def test_schema_version_rejects_unsupported_database(app):
    with app.app_context():
        db.get_session().execute(text("UPDATE schema_version SET version=999"))
        db.get_session().commit()
    with pytest.raises(RuntimeError, match="Unsupported database schema"):
        db.init_app(app)


@pytest.mark.parametrize("blank", [None, ""])
def test_optional_found_date_defaults_when_blank(app, blank):
    with app.app_context():
        job = services.create_job(1, {"title": "Engineer", "found_date": blank})
        assert job["found_date"] == datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()


def test_independent_sessions_detect_concurrent_writes(app):
    with app.app_context():
        job = services.create_job(1, {"title": "Engineer"})
        factory = sessionmaker(bind=app.extensions["jobtracker_db"]["engine"])
        with factory() as first, factory() as second:
            first_copy = first.get(Job, job["id"])
            second_copy = second.get(Job, job["id"])
            first_copy.stage = "applied"
            first.commit()
            second_copy.stage = "offer"
            with pytest.raises(StaleDataError):
                second.commit()
            second.rollback()
        db.get_session().expire_all()
        assert services.get_job(1, job["id"])["stage"] == "applied"
