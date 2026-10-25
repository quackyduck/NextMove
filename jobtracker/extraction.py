"""Bounded public-page fetching and best-effort JobPosting extraction.

No page scripts execute. Connections use validated IPs directly, retaining the
original hostname for Host and TLS verification, including after redirects.
"""
import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from .services import ServiceError

MAX_BYTES = 2 * 1024 * 1024
FETCH_SECONDS = 20


def validate_url(url):
    if not isinstance(url, str) or not url.strip() or len(url) > 8192:
        raise ServiceError('Enter a public HTTP or HTTPS posting URL')
    url = url.strip()
    try:
        if re.search(r'[\x00-\x20\x7f\\]', url):
            raise ValueError()
        parts = urlsplit(url)
        if (parts.scheme not in {'http', 'https'} or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.port not in {None, 80 if parts.scheme == 'http' else 443}):
            raise ValueError()
        hostname = parts.hostname.encode('idna').decode('ascii')
    except (ValueError, UnicodeError, OSError) as exc:
        raise ServiceError('Use a public HTTP or HTTPS URL on its standard port; private addresses are unavailable') from exc
    return parts, hostname


def public_target(url):
    parts, hostname = validate_url(url)
    try:
        addresses = socket.getaddrinfo(hostname, parts.port or (443 if parts.scheme == 'https' else 80),
                                       type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise ValueError()
    except (ValueError, OSError) as exc:
        raise ServiceError('Use a public HTTP or HTTPS URL; private addresses are unavailable') from exc
    return parts, hostname, addresses[0]


def fetch_page(url):
    deadline = time.monotonic() + FETCH_SECONDS
    for _ in range(5):
        parts, hostname, address = public_target(url)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ServiceError('The posting took too long to load. Try again or enter details manually.', 422)
        connection = http.client.HTTPConnection(hostname, port=parts.port or (443 if parts.scheme == 'https' else 80))
        sock = None
        try:
            # Do not resolve again during connect (DNS rebinding protection).
            family, socktype, proto, _, sockaddr = address
            sock = socket.socket(family, socktype, proto)
            sock.settimeout(min(5, remaining))
            sock.connect(sockaddr)
            if parts.scheme == 'https':
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=hostname)
            connection.sock = sock
            path = quote(urlunsplit(('', '', parts.path or '/', parts.query, '')), safe="/%?:=&+;,@!$'()*[]~-")
            connection.request('GET', path, headers={
                'User-Agent': 'NextMove/1.0 (job posting preview)',
                'Accept': 'text/html,application/xhtml+xml', 'Accept-Encoding': 'identity',
            })
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader('Location')
                if not location:
                    raise ServiceError('The posting returned an invalid redirect', 422)
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise ServiceError('The site could not be read. It may require sign-in or block automated access. Enter details manually.', 422)
            media_type = response.getheader('Content-Type', '').split(';')[0].strip().lower()
            if media_type not in {'text/html', 'application/xhtml+xml'}:
                raise ServiceError('This URL does not return an HTML job posting', 422)
            if response.getheader('Content-Encoding', 'identity').lower() != 'identity':
                raise ServiceError('The site returned an unsupported encoding. Enter details manually.', 422)
            body = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                sock.settimeout(min(5, remaining))
                chunk = response.read1(min(65536, MAX_BYTES + 1 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > MAX_BYTES:
                    raise ServiceError('This page is too large to import. Enter details manually.', 422)
            charset = response.headers.get_content_charset() or 'utf-8'
            try:
                return body.decode(charset, errors='replace')
            except LookupError:
                return body.decode('utf-8', errors='replace')
        except (OSError, http.client.HTTPException) as exc:
            raise ServiceError('The posting could not be loaded. Try again or enter details manually.', 422) from exc
        finally:
            connection.close()
            if sock is not None:
                sock.close()
    raise ServiceError('The posting redirected too many times. Use the direct posting URL.', 422)


BLOCK_TAGS = {'p', 'div', 'li', 'section', 'h1', 'h2', 'h3', 'h4', 'ul', 'ol'}


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text = []
        self.scripts = []
        self.metadata = {}
        self.heading = []
        self.title = []
        self.stack = []
        self.script = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta':
            self.metadata[(attrs.get('property') or attrs.get('name') or '').lower()] = attrs.get('content', '')
        if tag in {'script', 'style', 'noscript', 'template'}:
            self.stack.append(tag)
            if tag == 'script' and attrs.get('type', '').lower().split(';')[0] == 'application/ld+json':
                self.script = []
        if not self.stack and tag in BLOCK_TAGS | {'br'}:
            self.text.append('\n')
        if tag in {'h1', 'title'} and not self.stack:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()
            if tag == 'script' and self.script is not None:
                self.scripts.append(''.join(self.script))
                self.script = None
        if not self.stack and tag in BLOCK_TAGS:
            self.text.append('\n')

    def handle_data(self, data):
        if self.stack:
            if self.stack[-1] == 'script' and self.script is not None:
                self.script.append(data)
            elif self.stack[-1] == 'h1':
                self.heading.append(data)
                self.text.append(data)
            elif self.stack[-1] == 'title':
                self.title.append(data)
        else:
            self.text.append(data)


def clean(value):
    if isinstance(value, list):
        return '\n'.join(filter(None, (clean(item) for item in value)))
    if isinstance(value, dict):
        return clean(value.get('name') or value.get('text') or value.get('value') or value.get('credentialCategory'))
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ''
    parser = PageParser()
    parser.feed(str(value))
    return '\n'.join(' '.join(line.split()) for line in ''.join(parser.text).splitlines() if line.strip())


def postings(value):
    # Iterative traversal also tolerates arrays and nested @graph objects.
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, list):
            pending.extend(reversed(item))
        elif isinstance(item, dict):
            types = item.get('@type', [])
            if isinstance(types, str):
                types = [types]
            if isinstance(types, list) and any(isinstance(t, str) and t.rstrip('/').split('/')[-1] == 'JobPosting' for t in types):
                yield item
            pending.extend(reversed(list(item.values())))


def salary_text(value, currency=''):
    if isinstance(value, list):
        return '; '.join(filter(None, (salary_text(item, currency) for item in value)))
    if not isinstance(value, dict):
        return ' '.join(filter(None, [currency, clean(value)]))
    currency = clean(value.get('currency')) or currency
    amount = value.get('value', value)
    if isinstance(amount, dict):
        low, high = clean(amount.get('minValue')), clean(amount.get('maxValue'))
        number = f'{low}–{high}' if low and high else (f'From {low}' if low else f'Up to {high}' if high else clean(amount.get('value')))
        unit = clean(amount.get('unitText') or value.get('unitText'))
    else:
        number, unit = clean(amount), clean(value.get('unitText'))
    return ' '.join(filter(None, [currency, number, f'/ {unit.lower()}' if unit else ''])) if number else ''


QUALIFICATION_HEADING = re.compile(r"^(?:(?:key|minimum|basic|preferred|required)\s+)?(?:qualifications|requirements|skills)(?:\s+(?:and|&)\s+\w+)?\s*:?$|^(?:what you(?:'|’)ll (?:bring|need)|what we(?:'|’)re looking for|about you|you (?:have|bring))\s*:?$", re.I)


def qualification_section(text):
    result, collecting = [], False
    for line in text.splitlines():
        if QUALIFICATION_HEADING.match(line):
            collecting = True
        elif re.match(r'^(?:responsibilities|benefits|compensation|salary|about (?:us|the company)|what (?:we offer|you(?:\x27|’)ll do)|equal opportunity)\b', line, re.I):
            collecting = False
        if collecting:
            result.append(line)
    return '\n'.join(result)[:20000]


def parse_posting(html, url):
    parser = PageParser()
    parser.feed(html)
    jobs = []
    for script in parser.scripts:
        try:
            jobs.extend(postings(json.loads(script)))
        except (ValueError, RecursionError):
            continue
    if len(jobs) > 1:
        raise ServiceError('This page contains multiple roles. Use a URL for one specific job posting.', 422)
    job = jobs[0] if jobs else {}
    description = clean(job.get('description'))
    visible = clean(''.join(parser.text))
    title = clean(job.get('title')) or clean(' '.join(parser.heading)) or clean(parser.metadata.get('og:title')) or clean(' '.join(parser.title))
    company = clean(job.get('hiringOrganization'))
    # Common ATS titles: “Engineer at Acme”, “Engineer - Acme”, etc.
    if not company:
        match = re.match(r'^(.+?)\s+(?:at|@|[-–—|])\s+(.+)$', title)
        if match:
            title, company = match.groups()
    salary = salary_text(job.get('baseSalary'), clean(job.get('salaryCurrency')))
    if not salary:
        match = re.search(r'(?:USD\s*|[$€£])\s*\d[\d,.]*(?:\s*[kK])?(?:\s*(?:[-–—]|to)\s*(?:USD\s*|[$€£])?\s*\d[\d,.]*(?:\s*[kK])?)?(?:\s*(?:/|per|a)\s*(?:year|annum|hour|month|yr|hr))?', description or visible)
        salary = match.group() if match else ''
    qualification_parts = [clean(job.get(field)) for field in ('qualifications', 'skills', 'experienceRequirements', 'educationRequirements')]
    qualifications = '\n'.join(dict.fromkeys(filter(None, qualification_parts))) or qualification_section(description or visible)
    if not title:
        raise ServiceError('No role details were found. The site may need JavaScript or sign-in. Enter details manually.', 422)
    fields = {
        'title': title[:500], 'company': company[:500], 'url': url,
        'salary': salary[:500], 'notes': f'Key qualifications\n{qualifications}' if qualifications else '',
        'description': (description or visible)[:100000],
    }
    missing = [name for name in ('company', 'salary') if not fields[name]]
    if not qualifications:
        missing.append('key qualifications')
    warnings = []
    if not job:
        warnings.append('This page has no structured job data. Check the extracted details carefully.')
    if missing:
        warnings.append('Not found on the page: ' + ', '.join(missing) + '. Add these if available.')
    return {'fields': fields, 'warnings': warnings}


def extract_posting(url):
    html = fetch_page(url)
    try:
        return parse_posting(html, url.strip())
    except (ValueError, RecursionError) as exc:
        raise ServiceError('The page could not be parsed. Enter details manually.', 422) from exc
