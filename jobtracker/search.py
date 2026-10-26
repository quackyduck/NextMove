"""Bounded, explainable candidate search; never loads private job fields."""
import re
import unicodedata
from urllib.parse import parse_qsl, urlsplit

from sqlalchemy import case, func, select

from .db import get_session
from .models import Job
from .services import ServiceError

MAX_ROLES = 5000
GENERIC = {'engineer', 'engineering', 'software', 'developer', 'manager', 'specialist',
           'associate', 'analyst', 'senior', 'junior', 'staff', 'principal', 'lead'}
LEVELS = {'sr': 'senior', 'jr': 'junior', 'senior': 'senior', 'junior': 'junior',
          'staff': 'staff', 'principal': 'principal', 'lead': 'lead',
          'i': '1', 'ii': '2', 'iii': '3', 'iv': '4', '1': '1', '2': '2', '3': '3', '4': '4'}
TRACKING = {'gclid', 'fbclid', 'msclkid'}


def words(value):
    return re.findall(r'[^\W_]+(?:\+\+|#)?', unicodedata.normalize('NFKC', value).casefold())


def similarity(left, right):
    a, b = set(left), set(right)
    return len(a & b) / len(a | b) if a and b else 0.0


def posting_url(value):
    """Ignore tracking, retain unknown query keys as potential posting identity."""
    try:
        if len(value) > 4096 or '\\' in value or any(c.isspace() or ord(c) < 32 for c in value):
            raise ValueError()
        parsed = urlsplit(value)
        if (parsed.scheme.lower() not in {'http', 'https'} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            raise ValueError()
        if '%' in parsed.hostname:
            raise ValueError()
        host = parsed.hostname.encode('idna').decode('ascii').lower().removeprefix('www.')
        if ':' in host:
            host = '[' + host + ']'
        port = parsed.port
        if port is not None and not (parsed.scheme.lower(), port) in {('http', 80), ('https', 443)}:
            host += ':' + str(port)
        query = tuple(sorted((key, val) for key, val in parse_qsl(
            parsed.query, keep_blank_values=True, max_num_fields=100)
            if not key.lower().startswith('utm_') and key.lower() not in TRACKING))
        return (host, parsed.path.rstrip('/') or '/'), query
    except (ValueError, UnicodeError):
        raise ServiceError('url must be an absolute HTTP(S) URL without credentials (max 4096 characters)') from None


def parameters(args):
    allowed = {'company', 'title', 'url', 'limit', 'offset'}
    if set(args) - allowed or any(len(args.getlist(key)) != 1 for key in args):
        raise ServiceError('Unknown or repeated search parameters')
    values = {}
    for key in ('company', 'title', 'url'):
        value = args.get(key, '').strip()
        if key in args and (not value or len(value) > (4096 if key == 'url' else 500)):
            raise ServiceError(f'{key} must be nonempty and within its length limit')
        if value:
            if key != 'url' and not words(value):
                raise ServiceError(f'{key} must contain letters or numbers')
            values[key] = posting_url(value) if key == 'url' else words(value)
    if not values:
        raise ServiceError('Supply company, title, or url')
    for key, default, maximum in (('limit', 20, 100), ('offset', 0, 1000)):
        value = args.get(key, str(default))
        if not re.fullmatch(r'\d{1,4}', value) or not (0 if key == 'offset' else 1) <= int(value) <= maximum:
            raise ServiceError(f'{key} is outside its allowed range')
        values[key] = int(value)
    return values


def evidence(row, query):
    reasons, score, duplicate = [], 0, False
    if row.url_omitted:
        reasons.append('stored_url_exceeds_limit')
    if 'url' in query and row.url:
        try:
            base, identity = posting_url(row.url)
        except ServiceError:
            base, identity = None, None
        if base == query['url'][0]:
            url_ambiguous = identity != query['url'][1]
            duplicate = not url_ambiguous
            score = 65 if url_ambiguous else 100
            reasons.append('same_base_url_query_ambiguous' if url_ambiguous else 'same_normalized_posting_url')
    company = similarity(query.get('company', []), words(row.company))
    title = similarity(query.get('title', []), words(row.title))
    if company:
        reasons.append('company_exact' if company == 1 else 'company_overlap')
    if title:
        reasons.append('title_exact' if title == 1 else 'title_overlap')
    if company >= .5 or title >= .4:
        score = max(score, round(45 * company + 40 * title))
    if company >= .8 and title >= .4:
        supplied = query['title']
        saved = words(row.title)
        level = lambda tokens: {LEVELS[t] for t in tokens if t in LEVELS}
        if level(supplied) != level(saved):
            reasons.append('seniority_differs')
        elif not (set(supplied) - GENERIC - set(LEVELS)):
            reasons.append('generic_title_requires_review')
        elif company == 1 and title == 1:
            duplicate = True
            score = max(score, 90)
            reasons.append('company_and_specific_title_exact')
    if not score:
        return None
    return dict(id=row.id, title=row.title, company=row.company, url=row.url,
                stage=row.stage, outcome=row.outcome, score=score,
                classification='duplicate' if duplicate else 'possible_match', reasons=reasons,
                evidence={'company_similarity': round(company, 3), 'title_similarity': round(title, 3)})


def search(args):
    query = parameters(args)
    rows = get_session().execute(select(
        Job.id, Job.title, Job.company,
        case((func.length(Job.url) <= 4096, Job.url), else_='').label('url'),
        (func.length(Job.url) > 4096).label('url_omitted'), Job.stage, Job.outcome
    ).order_by(Job.id).limit(MAX_ROLES + 1)).all()
    if len(rows) > MAX_ROLES:
        raise ServiceError('Search capacity exceeded; indexed search is required before continuing', 503)
    matches = [match for row in rows if (match := evidence(row, query))]
    matches.sort(key=lambda item: (-item['score'], item['id']))
    start, limit = query['offset'], query['limit']
    end = start + limit
    return dict(matches=matches[start:end], total=len(matches), limit=limit, offset=start,
                next_offset=end if end < len(matches) and end <= 1000 else None,
                pagination_capped=end < len(matches) and end > 1000)
