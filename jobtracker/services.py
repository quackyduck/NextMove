"""Transactional domain operations shared by HTTP and the queue worker.

Every public function takes the acting account's `user_id` first and touches only
that account's rows. Another account's IDs behave exactly like missing IDs (404).
"""
from datetime import date, datetime, timedelta, timezone
from functools import wraps
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.orm.exc import StaleDataError

from .db import get_session
from .models import Activity, Contact, IngestionRequest, Job, Resume, OUTCOMES, PRIORITIES, STAGES, now


class ServiceError(Exception):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def transaction(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        session = get_session()
        try:
            result = function(*args, **kwargs)
            session.commit()
            return result
        except StaleDataError as exc:
            session.rollback()
            raise ServiceError("This job changed. Reload it before saving.", 409) from exc
        except Exception:
            session.rollback()
            raise
    return wrapped


def serialize(record):
    result = {}
    for column in record.__table__.columns:
        if column.name == "user_id":
            continue  # Ownership is implied by the authenticated request.
        value = getattr(record, column.name)
        if isinstance(value, datetime):
            value = value.replace(tzinfo=value.tzinfo or timezone.utc).isoformat().replace("+00:00", "Z")
        elif isinstance(value, date):
            value = value.isoformat()
        result[column.name] = value
    return result


def _record(model, user_id, identifier):
    record = get_session().scalar(select(model).where(model.id == identifier, model.user_id == user_id)) \
        if isinstance(identifier, str) else None
    if record is None:
        raise ServiceError(f"{model.__name__} not found", 404)
    return record


def _payload(data, allowed):
    if not isinstance(data, dict):
        raise ServiceError("Expected a JSON object")
    unknown = set(data) - set(allowed)
    if unknown:
        raise ServiceError("Unknown fields: " + ", ".join(sorted(unknown)))


TEXT_FIELDS = {"title", "company", "url", "description", "location", "salary", "lane", "notes", "next_action"}
JOB_FIELDS = TEXT_FIELDS | {"found_date", "next_action_date", "priority", "stage", "outcome", "outreach_completed"}


def _text(value, field, limit=100000):
    if not isinstance(value, str):
        raise ServiceError(f"{field} must be text")
    if len(value) > limit:
        raise ServiceError(f"{field} exceeds {limit} characters")
    return value.strip()


def _date(value, field, optional=False):
    if optional and (value is None or value == ""):
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ServiceError(f"{field} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ServiceError(f"{field} must be a valid date") from exc


def _job_values(data):
    values = {}
    for field, value in data.items():
        if field == "version":
            continue
        if field in TEXT_FIELDS:
            values[field] = _text(value, field, 500 if field in {"title", "company", "location", "salary", "lane"} else 100000)
        elif field == "outreach_completed":
            if type(value) is not bool:
                raise ServiceError("outreach_completed must be true or false")
            values[field] = value
        elif field in {"found_date", "next_action_date"}:
            values[field] = _date(value, field, optional=field == "next_action_date")
        else:
            choices = {"stage": STAGES, "outcome": OUTCOMES, "priority": PRIORITIES}[field]
            if value not in choices:
                raise ServiceError(f"Invalid {field}")
            values[field] = value
    if values.get("url"):
        try:
            parsed = urlsplit(values["url"])
            if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError()
            parsed.port
        except ValueError as exc:
            raise ServiceError("url must be an HTTP or HTTPS URL without credentials") from exc
    return values


def _activity(user_id, job_id, kind, message, details=None):
    get_session().add(Activity(user_id=user_id, job_id=job_id, kind=kind, message=message, details=details or {}))


def _create_job(user_id, data):
    _payload(data, JOB_FIELDS)
    if data.get("found_date") in (None, ""):
        data = {key: value for key, value in data.items() if key != "found_date"}
    values = _job_values(data)
    if not any(values.get(field) for field in ("title", "url", "description")):
        raise ServiceError("Provide a title, posting URL, or pasted description")
    values["title"] = values.get("title") or "Untitled role"
    values.setdefault("found_date", datetime.now(ZoneInfo("America/Los_Angeles")).date())
    if values.get("stage") == "outreach":
        values["outreach_completed"] = True
    job = Job(user_id=user_id, **values)
    get_session().add(job)
    get_session().flush()
    _activity(user_id, job.id, "created", "Job added")
    return serialize(job)


@transaction
def create_job(user_id, data):
    return _create_job(user_id, data)


@transaction
def complete_ingestion(user_id, identifier, fields, warnings):
    from .extraction_tasks import TASK_SECONDS
    session = get_session()
    # Claim under the write lock: an expired task cannot create a late role.
    claimed = session.execute(update(IngestionRequest).where(
        IngestionRequest.id == identifier, IngestionRequest.user_id == user_id, IngestionRequest.status == 'running',
        IngestionRequest.created_at >= now() - timedelta(seconds=TASK_SECONDS)
    ).values(status='saving')).rowcount
    if not claimed:
        return
    task = session.get(IngestionRequest, identifier)
    job = _create_job(user_id, {**fields, 'stage': 'needs_review'})
    task.status = 'succeeded'
    task.result = {'job_id': job['id'], 'warnings': warnings}
    # Role, activity, and idempotent success are committed together.


def list_jobs(user_id):
    return [serialize(job) for job in get_session().scalars(select(Job).where(Job.user_id == user_id).order_by(Job.updated_at.desc(), Job.id))]


def get_job(user_id, job_id):
    return serialize(_record(Job, user_id, job_id))


@transaction
def update_job(user_id, job_id, data):
    _payload(data, JOB_FIELDS | {"version"})
    job = _record(Job, user_id, job_id)
    if "version" in data:
        if type(data["version"]) is not int or data["version"] < 1:
            raise ServiceError("version must be a positive integer")
        if data["version"] != job.version:
            raise ServiceError("This job changed. Reload it before saving.", 409)
    values = _job_values(data)
    if "title" in values and not values["title"]:
        raise ServiceError("title cannot be empty")
    if values.get("stage") == "outreach" and job.stage != "outreach":
        values["outreach_completed"] = True
    changes = {}
    for field, value in values.items():
        if getattr(job, field) != value:
            before = getattr(job, field)
            changes[field] = {"from": before.isoformat() if isinstance(before, date) else before, "to": value.isoformat() if isinstance(value, date) else value}
            setattr(job, field, value)
    if changes:
        job.updated_at = now()
        _activity(user_id, job.id, "updated", "Job updated: " + ", ".join(changes), changes)
        get_session().flush()
    return serialize(job)


def list_activities(user_id, job_id):
    _record(Job, user_id, job_id)
    return [serialize(item) for item in get_session().scalars(select(Activity).where(Activity.job_id == job_id, Activity.user_id == user_id).order_by(Activity.created_at, Activity.id))]


def list_contacts(user_id, job_id):
    _record(Job, user_id, job_id)
    return [serialize(item) for item in get_session().scalars(select(Contact).where(Contact.job_id == job_id, Contact.user_id == user_id).order_by(Contact.created_at, Contact.id))]


@transaction
def create_contact(user_id, job_id, data):
    _record(Job, user_id, job_id)
    _payload(data, {"name", "email", "notes"})
    values = {field: _text(value, field, 100000 if field == "notes" else 500) for field, value in data.items()}
    if not (values.get("name") or values.get("email")):
        raise ServiceError("Provide a contact name or email")
    if values.get("email") and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", values["email"]):
        raise ServiceError("email must be a valid email address")
    contact = Contact(user_id=user_id, job_id=job_id, **values)
    get_session().add(contact)
    get_session().flush()
    _activity(user_id, job_id, "contact_added", "Contact added", {"contact_id": contact.id})
    return serialize(contact)


def list_resumes(user_id, job_id):
    _record(Job, user_id, job_id)
    return [serialize(item) for item in get_session().scalars(select(Resume).where(Resume.job_id == job_id, Resume.user_id == user_id).order_by(Resume.created_at, Resume.id))]


def get_resume(user_id, resume_id):
    return serialize(_record(Resume, user_id, resume_id))


@transaction
def create_resume(user_id, job_id, data):
    _record(Job, user_id, job_id)
    required = {"original_filename", "stored_filename", "media_type", "size", "sha256"}
    _payload(data, required)
    if set(data) != required:
        raise ServiceError("Incomplete resume metadata")
    values = {field: _text(data[field], field, 500) for field in required - {"size"}}
    if not values["original_filename"] or not values["media_type"]:
        raise ServiceError("Resume filename and media type are required")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", values["stored_filename"]) or values["stored_filename"] in {".", ".."}:
        raise ServiceError("Invalid resume storage name")
    if not re.fullmatch(r"[0-9a-f]{64}", values["sha256"]):
        raise ServiceError("Invalid resume SHA-256")
    if type(data["size"]) is not int or data["size"] < 0:
        raise ServiceError("Resume size must be a nonnegative integer")
    resume = Resume(user_id=user_id, job_id=job_id, size=data["size"], **values)
    get_session().add(resume)
    get_session().flush()
    _activity(user_id, job_id, "resume_added", "Resume version added", {"resume_id": resume.id, "original_filename": resume.original_filename})
    return serialize(resume)
