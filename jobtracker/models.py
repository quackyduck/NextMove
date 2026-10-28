"""Persistence records; business changes belong in services.py."""
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import text, Boolean, CheckConstraint, Date, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


STAGES = ("needs_review", "interesting", "resume_updated", "outreach", "applied", "recruiter_interview", "hiring_manager_interview", "panel_interview", "offer")
OUTCOMES = ("active", "rejected", "withdrawn", "closed", "accepted", "not_interested")
PRIORITIES = ("low", "medium", "high")


def now():
    return datetime.now(timezone.utc)


def new_id():
    return str(uuid4())


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint("stage IN ('needs_review','interesting','resume_updated','outreach','applied','recruiter_interview','hiring_manager_interview','panel_interview','offer')"),
        CheckConstraint("outcome IN ('active','rejected','withdrawn','closed','accepted','not_interested')"),
        CheckConstraint("priority IN ('low','medium','high')"),
        CheckConstraint("version > 0"),
        # Children reference (id, user_id) so a row can never cross tenants.
        UniqueConstraint("id", "user_id", name="uq_jobs_id_user"),
        Index("ix_jobs_user_updated", "user_id", "updated_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(500))
    company: Mapped[str] = mapped_column(String(500), default="")
    url: Mapped[str] = mapped_column(Text, default="")
    found_date: Mapped[object] = mapped_column(Date)
    description: Mapped[str] = mapped_column(Text, default="")
    location: Mapped[str] = mapped_column(String(500), default="")
    salary: Mapped[str] = mapped_column(String(500), default="")
    lane: Mapped[str] = mapped_column(String(500), default="")
    priority: Mapped[str] = mapped_column(String(20), default="medium")
    stage: Mapped[str] = mapped_column(String(40), default="interesting")
    outreach_completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    outcome: Mapped[str] = mapped_column(String(20), default="active")
    notes: Mapped[str] = mapped_column(Text, default="")
    next_action: Mapped[str] = mapped_column(Text, default="")
    next_action_date: Mapped[object | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    __mapper_args__ = {"version_id_col": version}


class Contact(Base):
    __tablename__ = "contacts"
    __table_args__ = (ForeignKeyConstraint(["job_id", "user_id"], ["jobs.id", "jobs.user_id"], name="fk_contacts_job_owner"),
        Index("ix_contacts_job_owner_created", "job_id", "user_id", "created_at", "id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    job_id: Mapped[str] = mapped_column(String(36))
    name: Mapped[str] = mapped_column(String(500), default="")
    email: Mapped[str] = mapped_column(String(500), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Resume(Base):
    __tablename__ = "resumes"
    __table_args__ = (CheckConstraint("size >= 0"), ForeignKeyConstraint(["job_id", "user_id"], ["jobs.id", "jobs.user_id"], name="fk_resumes_job_owner"),
        Index("ix_resumes_job_owner_created", "job_id", "user_id", "created_at", "id"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    job_id: Mapped[str] = mapped_column(String(36))
    original_filename: Mapped[str] = mapped_column(String(500))
    stored_filename: Mapped[str] = mapped_column(String(100), unique=True)
    media_type: Mapped[str] = mapped_column(String(200))
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Activity(Base):
    __tablename__ = "activities"
    __table_args__ = (ForeignKeyConstraint(["job_id", "user_id"], ["jobs.id", "jobs.user_id"], name="fk_activities_job_owner"),
        Index("ix_activities_job_owner_created", "job_id", "user_id", "created_at", "id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    job_id: Mapped[str] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(50))
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class User(Base):
    """An account. Replaces the schema-3 singleton workspace credential (user #1)."""
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("session_version > 0"),
        # At most one account may answer to the deprecated environment tokens.
        Index("uq_users_legacy_owner", "owns_legacy_tokens", unique=True, sqlite_where=text("owns_legacy_tokens = 1")),
        # AUTOINCREMENT: a deleted account's ID (and its uploads/<id>/, cookies) is never reissued.
        {"sqlite_autoincrement": True},
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Normalized (trimmed, lowercase) at every boundary; unique per account.
    email: Mapped[str] = mapped_column(String(254), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    session_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # The migrated/bootstrapped workspace owner: AGENT_API_TOKEN and
    # ROLE_SEARCH_TOKEN_SHA256 act for this account only. Never set by sign-up.
    owns_legacy_tokens: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class InviteCode(Base):
    """Single-use, expiring signup invitation. Only a SHA-256 of the code persists."""
    __tablename__ = "invite_codes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    prefix: Mapped[str] = mapped_column(String(12), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    used_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class ExtractionTask(Base):
    __tablename__ = "extraction_tasks"
    __table_args__ = (
        Index("ix_extraction_tasks_user_status", "user_id", "status"),
        Index("ix_extraction_tasks_status_created", "status", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class IngestionRequest(Base):
    __tablename__ = "ingestion_requests"
    __table_args__ = (
        # Keys are scoped per account: another tenant's key is never an oracle.
        UniqueConstraint("user_id", "idempotency_key", name="uq_ingestion_requests_user_key"),
        Index("ix_ingestion_requests_user_created", "user_id", "created_at"),
        Index("ix_ingestion_requests_user_status", "user_id", "status"),
        # expire_tasks: running rows past the deadline, without scanning history.
        Index("ix_ingestion_requests_status_created", "status", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    token_id: Mapped[str | None] = mapped_column(ForeignKey("agent_tokens.id"), nullable=True, index=True)
    url: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AgentToken(Base):
    __tablename__ = "agent_tokens"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(80))
    prefix: Mapped[str] = mapped_column(String(18))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    permissions: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


QUEUE_STATUSES = ("queued", "running", "succeeded", "failed")


class QueuedTask(Base):
    """Durable work item for the separate worker process (see queue.py)."""
    __tablename__ = "queued_tasks"
    __table_args__ = (
        CheckConstraint("status IN ('queued','running','succeeded','failed')"),
        CheckConstraint("attempts >= 0 AND max_attempts > 0"),
        Index("ix_queued_tasks_claim", "status", "run_after", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now)
    # A claim is a lease: a crashed worker's task is reclaimed after expiry.
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
