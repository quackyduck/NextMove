#!/usr/bin/env python3
"""Upload a revised resume to an existing job without replacing earlier versions.

python scripts/agent_example.py JOB_UUID resume.pdf --token-file /private/token
Uses Add roles tokens (NEXTMOVE_INGEST_TOKEN or a private file); legacy
JOBTRACKER_AGENT_TOKEN is also supported. Each retry adds another version.
Only uses the standard library. The default target is the local Flask server.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, Request


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Redirect refused. Use the exact API base URL.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id")
    parser.add_argument("resume", type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001")
    parser.add_argument("--token-file", type=Path, help="Private file containing an Add roles token")
    args = parser.parse_args()
    target = urlsplit(args.base_url)
    if (not target.hostname or target.username is not None or target.password is not None
            or target.query or target.fragment or not (
                target.scheme == 'https' or (target.scheme == 'http' and target.hostname in {'127.0.0.1', 'localhost', '::1'}))):
        parser.error('Use an HTTPS API base URL (HTTP is allowed only for local development)')
    try:
        if args.token_file and args.token_file.stat().st_mode & 0o077:
            parser.error('Token file must be private: chmod 600 the file first')
        token = args.token_file.read_text().strip() if args.token_file else (
            os.environ.get('NEXTMOVE_INGEST_TOKEN') or os.environ.get('JOBTRACKER_AGENT_TOKEN', ''))
    except OSError:
        parser.error('Token file could not be read')
    if not 32 <= len(token) <= 256 or not token.isascii() or any(char.isspace() for char in token):
        parser.error('Provide --token-file or NEXTMOVE_INGEST_TOKEN (legacy: JOBTRACKER_AGENT_TOKEN)')
    if not args.resume.is_file():
        parser.error("Resume file does not exist")
    if args.resume.stat().st_size > 10_000_000:
        parser.error("Resume exceeds 10 MB (leave room for multipart overhead)")
    # Never interpolate a user-provided filename into multipart headers.
    extension = args.resume.suffix.lower()
    media_types = {".pdf": "application/pdf", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".txt": "text/plain"}
    if extension not in media_types:
        parser.error("Use a PDF, DOCX, or TXT file")
    from uuid import UUID
    try:
        job_id = str(UUID(args.job_id))
    except ValueError:
        parser.error("job_id must be a UUID")
    boundary = "jobtracker-" + secrets.token_hex(24)
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="revised-resume{extension}"\r\n'
        f'Content-Type: {media_types[extension]}\r\n\r\n'
    ).encode() + args.resume.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    request = Request(
        f"{args.base_url.rstrip('/')}/api/v1/jobs/{job_id}/resumes",
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with build_opener(NoRedirect()).open(request, timeout=30) as response:
            print(json.dumps(json.load(response), indent=2))
    except HTTPError as error:
        print(f"Upload failed ({error.code}). Check the role ID, token permission, and file format/size.", file=sys.stderr)
        return 1
    except (URLError, OSError, ValueError):
        print('Upload did not complete. Check the role in the browser before retrying: a retry may create another version.', file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
