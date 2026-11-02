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
    # Both are NULL for a messaging-first account until dashboard activation
    # (schema 11): such an account has no browser sign-in at all.
    email: Mapped[str | None] = mapped_column(String(254), nullable=True, unique=True)
    password_hash: Mapped[str | None] = mapped_column(String(512), nullable=True)
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


class OnboardingStep(Base):
    """Per-user progress for a registered step; absent rows mean pending."""
    __tablename__ = "onboarding_steps"
    __table_args__ = (
        CheckConstraint("status IN ('pending','done','skipped')"),
        CheckConstraint("version > 0"),
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    step_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now)


class ResumeTemplate(Base):
    """A named master resume for one kind of role; its files are MasterResume versions. One per account is the default."""
    __tablename__ = "resume_templates"
    __table_args__ = (
        UniqueConstraint("id", "user_id", name="uq_resume_templates_id_user"),
        # ASCII case-insensitive; resume_templates also compares casefolded names.
        Index("uq_resume_templates_user_name", "user_id", text("lower(name)"), unique=True),
        Index("uq_resume_templates_default", "user_id", unique=True, sqlite_where=text("is_default = 1")),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now)


class MasterResume(Base):
    """One version of a resume template, independent of any job. Versions are numbered per template."""
    __tablename__ = "master_resumes"
    __table_args__ = (
        CheckConstraint("size >= 0"),
        CheckConstraint("version > 0"),
        UniqueConstraint("template_id", "version", name="uq_master_resumes_template_version"),
        # A version can never belong to another account's template.
        ForeignKeyConstraint(["template_id", "user_id"], ["resume_templates.id", "resume_templates.user_id"],
                             name="fk_master_resumes_template_owner"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    template_id: Mapped[str] = mapped_column(String(36), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(500))
    stored_filename: Mapped[str] = mapped_column(String(100), unique=True)
    media_type: Mapped[str] = mapped_column(String(200))
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


HARNESS_SCOPES = ("resumes:read", "resumes:write", "jobs:read", "jobs:write", "drafts:write")


class HarnessGrant(Base):
    """A user's delegation to the Resume Agent harness. Only a SHA-256 of the token persists."""
    __tablename__ = "harness_grants"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    prefix: Mapped[str] = mapped_column(String(20), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    scopes: Mapped[list] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ActivationToken(Base):
    """Single-use, expiring dashboard activation for a messaging-first account (hash only)."""
    __tablename__ = "activation_tokens"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TailoredDraft(Base):
    """A harness-generated resume draft. Never exported until approved, which files it as a job resume."""
    __tablename__ = "tailored_drafts"
    __table_args__ = (
        CheckConstraint("revision > 0"),
        ForeignKeyConstraint(["job_id", "user_id"], ["jobs.id", "jobs.user_id"], name="fk_tailored_drafts_job_owner"),
        # A redelivered harness request returns the existing draft instead of a duplicate.
        UniqueConstraint("user_id", "request_id", "revision", name="uq_tailored_drafts_request"),
        Index("ix_tailored_drafts_job_owner", "job_id", "user_id"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    job_id: Mapped[str] = mapped_column(String(36), nullable=False)
    master_resume_id: Mapped[str] = mapped_column(ForeignKey("master_resumes.id"), nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    draft: Mapped[dict] = mapped_column(JSON, nullable=False)
    docx_filename: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    pdf_filename: Mapped[str | None] = mapped_column(String(100), nullable=True, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# OAuth 2.1 for third-party agents (MCP clients and API callers); see oauth.py.
OAUTH_SCOPES = ("jobs:read", "jobs:write", "resumes:read", "resumes:write")


class OAuthClient(Base):
    """A dynamically registered public client (PKCE, no secret)."""
    __tablename__ = "oauth_clients"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    redirect_uris: Mapped[list] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class OAuthGrant(Base):
    """One user's consent to one client: the connection they see and revoke."""
    __tablename__ = "oauth_grants"
    __table_args__ = (Index("ix_oauth_grants_user_client", "user_id", "client_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    client_id: Mapped[str] = mapped_column(ForeignKey("oauth_clients.id"), nullable=False)
    scopes: Mapped[list] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OAuthCode(Base):
    """Single-use authorization code bound to its PKCE challenge and redirect URI (hash only)."""
    __tablename__ = "oauth_codes"
    code_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    grant_id: Mapped[str] = mapped_column(ForeignKey("oauth_grants.id"), nullable=False, index=True)
    redirect_uri: Mapped[str] = mapped_column(Text, nullable=False)
    code_challenge: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OAuthToken(Base):
    """Access or refresh token of a grant (hash only). Refresh tokens rotate on use."""
    __tablename__ = "oauth_tokens"
    __table_args__ = (CheckConstraint("kind IN ('access','refresh')"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    grant_id: Mapped[str] = mapped_column(ForeignKey("oauth_grants.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
