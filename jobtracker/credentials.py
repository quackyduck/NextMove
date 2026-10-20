"""Persistent workspace credential and atomic session-generation changes."""
from dataclasses import dataclass

import click
from flask import current_app
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError


class Unavailable(Exception):
    pass


@dataclass(frozen=True)
class Credential:
    password_hash: str
    session_version: int


def bootstrap(connection, password_hash):
    """Called only within fresh-schema / pre-v3 migration transactions."""
    connection.execute(text(
        'INSERT INTO workspace_credentials (id,password_hash,session_version) '
        'VALUES (1,:password_hash,1)'
    ), {'password_hash': password_hash})


def init_app(app):
    # An initialized database missing its credential must never reinstate an old
    # environment verifier. Recovery requires restoring the credential/database.
    engine = app.extensions['jobtracker_db']['engine']
    with engine.connect() as connection:
        row = connection.execute(text('SELECT id FROM workspace_credentials WHERE id=1')).first()
    if row is None:
        raise RuntimeError('Workspace credential is missing; restore the database before starting')

    @app.cli.command('reset-workspace-password')
    def reset_workspace_password():
        """Administrator recovery; never accepts the password as a shell argument."""
        from werkzeug.security import generate_password_hash
        password = click.prompt('New workspace password', hide_input=True, confirmation_prompt=True)
        if not 8 <= len(password) <= 128:
            raise click.ClickException('Choose a password between 8 and 128 characters')
        try:
            observed = read()
            changed = replace(observed, generate_password_hash(password))
        except Unavailable:
            raise click.ClickException('Credential store unavailable') from None
        if not changed:
            raise click.ClickException('Password changed concurrently; retry recovery')
        click.echo('Workspace password reset. All browser sessions have been signed out.')



def read():
    try:
        with current_app.extensions['jobtracker_db']['engine'].connect() as connection:
            row = connection.execute(text(
                'SELECT password_hash,session_version FROM workspace_credentials WHERE id=1'
            )).first()
        if row is None:
            raise Unavailable()
        return Credential(row.password_hash, row.session_version)
    except SQLAlchemyError:
        raise Unavailable() from None


def replace(expected, password_hash):
    # Hash outside the transaction, then compare both captured values at write
    # time. A concurrent change must never be overwritten by an older request.
    try:
        with current_app.extensions['jobtracker_db']['engine'].begin() as connection:
            changed = connection.execute(text(
                'UPDATE workspace_credentials SET password_hash=:new_hash, '
                'session_version=session_version+1 '
                'WHERE id=1 AND password_hash=:old_hash AND session_version=:version'
            ), {'new_hash': password_hash, 'old_hash': expected.password_hash,
                'version': expected.session_version}).rowcount
        return changed == 1
    except SQLAlchemyError:
        raise Unavailable() from None
