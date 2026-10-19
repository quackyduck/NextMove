#!/usr/bin/env python3
"""Upload a revised resume to an existing job without replacing earlier versions.

JOBTRACKER_AGENT_TOKEN=... python scripts/agent_example.py JOB_UUID resume.pdf
Only uses the standard library. The default target is the local Flask server.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id")
    parser.add_argument("resume", type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001")
    args = parser.parse_args()
    token = os.environ.get("JOBTRACKER_AGENT_TOKEN")
    if not token:
        parser.error("Set JOBTRACKER_AGENT_TOKEN to the server's configured agent token")
    if not args.resume.is_file():
        parser.error("Resume file does not exist")
    if args.resume.stat().st_size > 10 * 1024 * 1024:
        parser.error("Resume exceeds 10 MiB")
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
        with urlopen(request, timeout=30) as response:
            print(json.dumps(json.load(response), indent=2))
    except HTTPError as error:
        print(f"Upload failed ({error.code}): {error.read(4096).decode(errors='replace')}")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
