"""Named, expiring agent credentials. Only hashes persist; management is browser-only."""
from datetime import timedelta, timezone
import hashlib
import secrets

from flask import current_app, g
from sqlalchemy import insert, select, update
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.security import check_password_hash

from . import services, throttle
from .models import AgentToken, WorkspaceCredential, new_id, now

PERMISSIONS = ['roles:search', 'roles:ingest']


def metadata(row):
    result = {key: row[key] for key in ('id', 'name', 'prefix', 'permissions', 'created_at', 'expires_at', 'last_used_at', 'revoked_at')}
    expiry = row['expires_at'].replace(tzinfo=timezone.utc)
    result['status'] = 'revoked' if row['revoked_at'] else 'expired' if expiry <= now() else 'active'
    return {key: value.replace(tzinfo=timezone.utc).isoformat() if hasattr(value, 'isoformat') else value
            for key, value in result.items()}


def list_tokens():
    with current_app.extensions['jobtracker_db']['engine'].connect() as connection:
        return [metadata(row) for row in connection.execute(
            select(AgentToken.__table__).order_by(AgentToken.created_at.desc())).mappings()]


def create_token(data):
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
    try:
        if not throttle.reserve_attempt():
            raise services.ServiceError('Too many verification attempts. Try again later.', 429)
    except throttle.Unavailable as exc:
        raise services.ServiceError('Password verification temporarily unavailable', 503) from exc
    observed = g.credential
    if not check_password_hash(observed.password_hash, password):
        raise services.ServiceError('Current password is incorrect')
    raw = 'nm_ingest_' + secrets.token_urlsafe(32)
    values = dict(id=new_id(), name=name, prefix=raw[:18], token_hash=hashlib.sha256(raw.encode()).hexdigest(),
                  permissions=permissions, created_at=now(), expires_at=now() + timedelta(days=days),
                  last_used_at=None, revoked_at=None)
    with current_app.extensions['jobtracker_db']['engine'].begin() as connection:
        if connection.dialect.name == 'sqlite':
            connection.exec_driver_sql('BEGIN IMMEDIATE')
        credential = connection.execute(select(WorkspaceCredential.__table__)).mappings().one()
        if (credential['session_version'], credential['password_hash']) != (observed.session_version, observed.password_hash):
            raise services.ServiceError('Your sign-in changed. Sign in again before creating a token.', 409)
        count = connection.execute(select(AgentToken.id).where(
            AgentToken.revoked_at.is_(None), AgentToken.expires_at > now())).all()
        if len(count) >= 20:
            raise services.ServiceError('Revoke an unused token before creating another (20 active tokens maximum)', 409)
        connection.execute(insert(AgentToken).values(**values))
    return {'token': raw, 'agent_token': metadata(values)}


def revoke_token(identifier):
    with current_app.extensions['jobtracker_db']['engine'].begin() as connection:
        connection.execute(update(AgentToken).where(AgentToken.id == identifier, AgentToken.revoked_at.is_(None))
                           .values(revoked_at=now()))
        row = connection.execute(select(AgentToken.__table__).where(AgentToken.id == identifier)).mappings().first()
        if row is None:
            raise services.ServiceError('Token not found', 404)
        return metadata(row)


def authenticate(raw, permission='roles:ingest'):
    if not isinstance(raw, str) or not 32 <= len(raw) <= 256:
        return None
    digest = hashlib.sha256(raw.encode()).hexdigest()
    try:
        with current_app.extensions['jobtracker_db']['engine'].begin() as connection:
            if connection.dialect.name == 'sqlite':
                connection.exec_driver_sql('BEGIN IMMEDIATE')
            row = connection.execute(select(AgentToken.__table__).where(
                AgentToken.token_hash == digest, AgentToken.revoked_at.is_(None), AgentToken.expires_at > now()
            )).mappings().first()
            if row is None or not set(row['permissions']).intersection(PERMISSIONS) or (permission is not None and permission not in row['permissions']):
                return None
            used = now()
            connection.execute(update(AgentToken).where(AgentToken.id == row['id']).values(last_used_at=used))
            return metadata({**row, 'last_used_at': used})
    except SQLAlchemyError as exc:
        raise services.ServiceError('Agent access temporarily unavailable', 503) from exc
