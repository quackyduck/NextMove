#!/usr/bin/env python3
"""Browser-test helper: print the newest email for an address from MAIL_FILE_DIR (the `file` mail backend).

  e2e_mail.py link EMAIL      the confirmation link (…/verify-email#token=…), or exit 1
  e2e_mail.py body EMAIL      the plain-text body
  e2e_mail.py count EMAIL     how many messages that address received

Only for disposable test servers; the `file` backend is refused in production.
"""
from email import policy
from email.parser import BytesParser
import os
from pathlib import Path
import re
import sys


def messages(address):
    folder = Path(os.environ['MAIL_FILE_DIR'])
    found = []
    for path in sorted(folder.glob('*.eml')):
        message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
        if message['To'] == address.strip().lower():
            found.append(message)
    return found


def main(command, address):
    found = messages(address)
    if command == 'count':
        print(len(found))
        return 0
    if not found:
        return 1
    body = found[-1].get_body(('plain',)).get_content()
    if command == 'body':
        print(body)
        return 0
    link = re.search(r'https?://\S+#token=[A-Za-z0-9_-]+', body)
    if not link:
        return 1
    print(link.group(0))
    return 0


if __name__ == '__main__':
    sys.exit(main(*sys.argv[1:3]))
