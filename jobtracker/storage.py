"""Per-user private file storage under UPLOAD_FOLDER/<user_id>/."""
import os
from pathlib import Path

from sqlalchemy import select

from .models import Resume


def user_directory(root, user_id, create=False):
    if type(user_id) is not int or user_id < 1:
        raise ValueError('Invalid storage owner')
    directory = Path(root).resolve() / str(user_id)
    if create:
        directory.mkdir(mode=0o700, exist_ok=True)
    return directory


def owned_path(root, user_id, stored_filename):
    """Path of a stored file, or None if it would escape the owner's directory."""
    directory = user_directory(root, user_id)
    target = (directory / stored_filename).resolve()
    return target if target.parent == directory else None


def relocate_legacy_files(app):
    """Move pre-schema-9 files from the upload root into their owner's directory.

    Idempotent and restart-safe: runs at every startup, after migrations commit,
    and only renames within the same filesystem (os.replace is atomic). Rows
    whose file is already in place, or missing, are left alone.
    """
    root = Path(app.config['UPLOAD_FOLDER']).resolve()
    if not root.is_dir():
        return 0
    legacy = {path.name for path in root.iterdir() if path.is_file() and not path.is_symlink()}
    if not legacy:
        return 0
    engine = app.extensions['jobtracker_db']['engine']
    with engine.connect() as connection:
        rows = connection.execute(select(Resume.user_id, Resume.stored_filename)
                                  .where(Resume.stored_filename.in_(legacy))).all()
    moved = 0
    for user_id, stored in rows:
        target = owned_path(root, user_id, stored)
        if target is None or target.exists():
            continue
        user_directory(root, user_id, create=True)
        try:
            os.replace(root / stored, target)
        except FileNotFoundError:
            continue  # Another process (web or worker) moved it first.
        moved += 1
    return moved
