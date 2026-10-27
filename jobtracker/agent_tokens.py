"""Named, expiring agent credentials. Only hashes persist; management is browser-only."""
from datetime import timedelta, timezone
import hashlib
import secrets

from flask import current_app, g
from sqlalchemy import insert, select, update
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.security import check_password_hash

from . import services, throttle
from .models import AgentToken, User, new_id, now

PERMISSIONS = ['roles:search', 'roles:ingest']


def metadata(row):
    result = {key: row[key] for key in ('id', 'name', 'prefix', 'permissions', 'created_at', 'expires_at', 'last_used_at', 'revoked_at')}
    expiry = row['expires_at'].replace(tzinfo=timezone.utc)
    result['status'] = 'revoked' if row['revoked_at'] else 'expired' if expiry <= now() else 'active'
    return {key: value.replace(tzinfo=timezone.utc).isoformat() if hasattr(value, 'isoformat') else value
            for key, value in result.items()}


def list_tokens(user_id):
    with current_app.extensions['jobtracker_db']['engine'].connect() as connection:
        return [metadata(row) for row in connection.execute(
            select(AgentToken.__table__).where(AgentToken.user_id == user_id)
            .order_by(AgentToken.created_at.desc())).mappings()]


def create_token(user_id, data):
    services._payload(data, {'name', 'expires_in_days', 'current_password', 'permissions'})
    name = services._text(data.get('name', ''), 'name', 80)
    days = data.get('expires_in_days', 90)
    if not name:
        raise services.ServiceError('Give this token a name')
    if type(days) is not int or not 1 <= days <= 365:
        raise services.ServiceError('Expiry must be 1–365 days')
    permissions = data.get('permissions', ['roles:ingest'])
    if not isinstance(permissions, list) or not permissions or any(p not in PERMISSIONS for p in permissions):
        raise services.ServiceError('Choose search and/or ingestion permission')
    permissions = sorted(set(permissions))
    password = data.get('current_password')
    if not isinstance(password, str) or not 1 <= len(password) <= 128:
        raise services.ServiceError('Enter your current workspace password')
    observed = g.credential
    try:
        if not throttle.reserve_attempt(observed.email):
            raise services.ServiceError('Too many verification attempts. Try again later.', 429)
    except throttle.Unavailable as exc:
        raise services.ServiceError('Password verification temporarily unavailable', 503) from exc
    if not check_password_hash(observed.password_hash, password):
        raise services.ServiceError('Current password is incorrect')
    raw = 'nm_ingest_' + secrets.token_urlsafe(32)
    values = dict(id=new_id(), user_id=user_id, name=name, prefix=raw[:18], token_hash=hashlib.sha256(raw.encode()).hexdigest(),
                  permissions=permissions, created_at=now(), expires_at=now() + timedelta(days=days),
                  last_used_at=None, revoked_at=None)
    with current_app.extensions['jobtracker_db']['engine'].begin() as connection:
        if connection.dialect.name == 'sqlite':
            connection.exec_driver_sql('BEGIN IMMEDIATE')
        credential = connection.execute(select(User.__table__).where(User.id == user_id)).mappings().first()
        if credential is None or (credential['session_version'], credential['password_hash']) != (
                observed.session_version, observed.password_hash):
            raise services.ServiceError('Your sign-in changed. Sign in again before creating a token.', 409)
        count = connection.execute(select(AgentToken.id).where(
            AgentToken.user_id == user_id, AgentToken.revoked_at.is_(None), AgentToken.expires_at > now())).all()
        if len(count) >= 20:
            raise services.ServiceError('Revoke an unused token before creating another (20 active tokens maximum)', 409)
        connection.execute(insert(AgentToken).values(**values))
    return {'token': raw, 'agent_token': metadata(values)}


def revoke_token(user_id, identifier):
    owned = (AgentToken.id == identifier, AgentToken.user_id == user_id)
    with current_app.extensions['jobtracker_db']['engine'].begin() as connection:
        connection.execute(update(AgentToken).where(*owned, AgentToken.revoked_at.is_(None))
                           .values(revoked_at=now()))
        row = connection.execute(select(AgentToken.__table__).where(*owned)).mappings().first()
        if row is None:
            raise services.ServiceError('Token not found', 404)
        return metadata(row)


def authenticate(raw, permission='roles:ingest'):
    """(metadata, owner user_id) for a valid token with the permission, else None."""
    if not isinstance(raw, str) or not 32 <= len(raw) <= 256:
        return None
    digest = hashlib.sha256(raw.encode()).hexdigest()
    table = AgentToken.__table__
    valid = (table.c.token_hash == digest, table.c.revoked_at.is_(None))
    engine = current_app.extensions['jobtracker_db']['engine']
    try:
        # Invalid credentials need no writer lock. Scopes are immutable after creation.
        with engine.connect() as connection:
            row = connection.execute(select(table).where(*valid, table.c.expires_at > now())).mappings().first()
        if row is None:
            return None
        allowed = set(row['permissions']).intersection(PERMISSIONS)
        if not allowed or (permission is not None and permission not in allowed):
            return None
        with engine.begin() as connection:
            # Recheck revocation/expiry atomically: a revoke between read and write wins.
            row = connection.execute(update(table).where(*valid, table.c.expires_at > now())
                                     .values(last_used_at=now()).returning(table)).mappings().first()
            return (metadata(row), row['user_id']) if row else None
    except SQLAlchemyError as exc:
        raise services.ServiceError('Agent access temporarily unavailable', 503) from exc
