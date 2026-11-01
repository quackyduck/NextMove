#!/usr/bin/env python3
"""Submit one URL using a limited token from a private file or NEXTMOVE_INGEST_TOKEN.

Reuse the printed Idempotency-Key when retrying an uncertain submission. The
client refuses redirects so the Authorization header cannot follow another URL.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlencode
from urllib.request import build_opener, HTTPRedirectHandler, Request
from uuid import uuid4


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Redirect refused. Use the exact API base URL.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url', nargs='?', help='Job posting URL')
    parser.add_argument('--test-connection', action='store_true', help='Verify the token without creating a role')
    parser.add_argument('--search', action='store_true', help='Search saved roles without submitting')
    parser.add_argument('--company', help='Company search signal')
    parser.add_argument('--title', help='Title search signal')
    parser.add_argument('--base-url', default='https://mynextmoov.com')
    parser.add_argument('--token-file', type=Path, help='Mode-0600 file containing only the token')
    parser.add_argument('--idempotency-key', default=None, help='Reuse this key for retries; default: new UUID')
    parser.add_argument('--wait', action='store_true', help='Poll until the role is saved or ingestion fails')
    args = parser.parse_args()
    if not args.url and not args.test_connection and not (args.search and (args.company or args.title)):
        parser.error('Provide a posting URL or --test-connection')
    target = urlsplit(args.base_url)
    if (not target.hostname or target.username is not None or target.password is not None
            or target.query or target.fragment or not (
                target.scheme == 'https' or (target.scheme == 'http' and target.hostname in {'127.0.0.1', 'localhost', '::1'}))):
        parser.error('Use an HTTPS API base URL (HTTP is allowed only for local development)')
    try:
        if args.token_file and args.token_file.stat().st_mode & 0o077:
            parser.error('Token file must be private: chmod 600 the file first')
        token = args.token_file.read_text().strip() if args.token_file else os.environ.get('NEXTMOVE_INGEST_TOKEN', '')
    except OSError:
        parser.error('Token file could not be read')
    if not 32 <= len(token) <= 256 or not token.isascii() or any(char.isspace() for char in token):
        parser.error('Provide a token using --token-file or NEXTMOVE_INGEST_TOKEN')
    key = args.idempotency_key or str(uuid4())
    if not args.test_connection and not args.search:
        print('Idempotency-Key: ' + key, file=sys.stderr)
    endpoint = args.base_url.rstrip('/') + '/api/v1/ingestions'
    opener = build_opener(NoRedirect())

    def request(url, body=None):
        headers = {'Authorization': 'Bearer ' + token}
        if body is not None:
            headers['Content-Type'] = 'application/json'
            headers['Idempotency-Key'] = key
        req = Request(url, data=json.dumps(body).encode() if body is not None else None, headers=headers)
        with opener.open(req, timeout=10) as response:
            return json.load(response)

    try:
        if args.search:
            query = {key: value for key, value in {'url': args.url, 'company': args.company, 'title': args.title}.items() if value}
            matches = []
            offset = 0
            while True:
                result = request(args.base_url.rstrip('/') + '/api/v1/jobs/search?' + urlencode({**query, 'offset': offset, 'limit': 100}))
                matches.extend(result['matches'])
                if result['pagination_capped']:
                    raise ValueError('Refine the search: pagination is capped')
                if result['next_offset'] is None:
                    break
                offset = result['next_offset']
            print(json.dumps({'matches': matches, 'total': len(matches)}, indent=2))
            return 0
        if args.test_connection:
            result = request(args.base_url.rstrip('/') + '/api/v1/agent-connection')
            print(json.dumps(result, indent=2))
            return 0
        result = request(endpoint, {'url': args.url})
        deadline = time.monotonic() + 70
        while args.wait and result['status'] == 'running':
            if time.monotonic() >= deadline:
                raise ValueError('Still running. Reuse the same Idempotency-Key to check again.')
            time.sleep(1)
            # Build the polling URL ourselves rather than trusting a redirected location.
            from uuid import UUID
            result = request(endpoint + '/' + str(UUID(result['id'])))
        print(json.dumps(result, indent=2))
        return 1 if result['status'] == 'failed' else 0
    except HTTPError as error:
        hint = 'Check the token, expiry and revocation status.' if args.test_connection else 'Check the request and reuse the same Idempotency-Key when retrying.'
        print(f'API request failed ({error.code}). {hint}', file=sys.stderr)
    except (URLError, OSError, ValueError, KeyError):
        hint = 'Search/check failed; do not submit until it succeeds.' if args.search or args.test_connection else 'Reuse the same Idempotency-Key when retrying.'
        print('Request did not complete. Check the base URL/network or refine the search. ' + hint, file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
