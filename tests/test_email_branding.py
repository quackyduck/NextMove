"""Branded HTML emails: escaping, no remote resources but the logo, unchanged plain text, links, size, logo route."""
from email.message import EmailMessage
from html.parser import HTMLParser
import re

import pytest
from werkzeug.security import generate_password_hash

from jobtracker import confirmations, create_app, mailer, password_reset
from jobtracker.email_content import EMAILS

BASE = 'https://example.test/nextmove'
LOGO = f'{BASE}/static/email/mascot-96-v1.png'
TOKEN_LINK = f'{BASE}/verify-email#token=abcdefghijklmnopqrstuvwxyz0123456789ABCD'
FACTS = {'minutes': 60, 'at': '2026-11-06 20:15', 'date': '2026-11-06'}
LINKED = {'signup', 'activation', 'existing', 'password_reset'}


@pytest.fixture
def app(tmp_path):
    mailer.outbox.clear()
    return create_app({
        'TESTING': True, 'APP_ENV': 'development', 'SECRET_KEY': 'branding-test-secret-0123456789abcdef',
        'APP_PASSWORD_HASH': generate_password_hash('test-password', method='pbkdf2:sha256:1000'),
        'DATABASE_URL': f'sqlite:///{tmp_path / "branding.sqlite3"}', 'UPLOAD_FOLDER': str(tmp_path / 'uploads'),
        'LOGIN_THROTTLE_DATABASE': str(tmp_path / 'throttle.sqlite3'), 'SESSION_COOKIE_SECURE': False,
        'PUBLIC_BASE_URL': BASE, 'MAIL_REPLY_TO': 'My Next Moov <Support@Example.test>',
    })


class Parts(HTMLParser):
    """Collect tags with their attributes, and visible text."""
    def __init__(self):
        super().__init__()
        self.tags, self.text, self._skip = [], [], 0

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag in ('style', 'title'):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ('style', 'title'):
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.text.append(data)


def parse(html):
    parts = Parts()
    parts.feed(html)
    return parts


def render(app, kind, email='person@example.com', link=TOKEN_LINK, text='Plain body\n', **facts):
    with app.test_request_context('/'):
        return mailer.render(kind, subject='Subject', text=text, email=email,
                             link=link if kind in LINKED else None, **{**FACTS, **facts})


@pytest.mark.parametrize('kind', sorted(EMAILS))
def test_every_kind_renders_small_self_contained_html(app, kind):
    subject, text, html = render(app, kind)
    assert (subject, text) == ('Subject', 'Plain body\n')  # The plain-text part is passed through untouched.
    assert len(html.encode()) < 30 * 1024
    parts = parse(html)
    images = [attrs for tag, attrs in parts.tags if tag == 'img']
    assert images == [{'src': LOGO, 'width': '48', 'height': '48', 'alt': 'My Next Moov cow mascot',
                       'style': 'display:block; width:48px; height:48px;'}]
    assert not [tag for tag, _ in parts.tags if tag in ('link', 'script', 'iframe', 'object', 'form')]
    assert 'url(' not in html and '@import' not in html and '@font-face' not in html
    for tag, attrs in parts.tags:
        for name in ('href', 'src', 'background', 'action'):
            if name in attrs:
                value = attrs[name]
                assert value.startswith(BASE + '/') or value == BASE + '/' or value == 'mailto:support@example.test', value
    for leftover in ('{{', '{%', 'None', '{masked}', '{minutes}', '{at}', '{date}'):
        assert leftover not in html
    visible = ' '.join(parts.text)
    assert 'p•••@example.com' in visible and 'person@example.com' not in html
    assert 'My Next Moov' in visible and 'Privacy' in visible and 'support@example.test' in visible
    assert 'support@mynextmoov.com' not in html  # Only the configured reply-to address appears.


@pytest.mark.parametrize('kind', sorted(LINKED))
def test_token_links_are_the_button_and_visible_text(app, kind):
    _, _, html = render(app, kind)
    parts = parse(html)
    hrefs = [attrs['href'] for tag, attrs in parts.tags if tag == 'a' and 'href' in attrs]
    assert hrefs.count(TOKEN_LINK) == 2  # The button, and the copyable link under it.
    assert TOKEN_LINK in ' '.join(parts.text)


def test_kinds_without_a_link_refuse_to_render_a_token_button(app):
    with app.test_request_context('/'), pytest.raises(ValueError):
        mailer.render('signup', subject='s', text='t', email='a@example.com', link=None)


@pytest.mark.parametrize('kind', sorted(EMAILS))
def test_everything_interpolated_is_escaped(app, kind):
    hostile = '<script>alert(1)</script>"\'&</td>'
    _, _, html = render(app, kind, email=f'<{hostile}@example.com', **{key: hostile for key in FACTS})
    assert '<script>' not in html and '</td>"' not in html and '<•' not in html
    assert '&lt;•••@example.com' in html  # The masked address keeps its first character, escaped.
    uses_facts = any('{' + key + '}' in str(value) for key in FACTS for value in EMAILS[kind].values())
    assert ('&lt;script&gt;alert(1)&lt;/script&gt;&#34;&#39;&amp;&lt;/td&gt;' in html) is uses_facts


def test_hostile_links_cannot_break_out_of_the_attribute(app):
    link = BASE + '/verify-email#token="><script>x</script>'
    _, _, html = render(app, 'signup', link=link)
    assert '"><script>' not in html and '&#34;&gt;&lt;script&gt;' in html


def test_confirmation_emails_keep_their_plain_text_and_add_the_html_part(app):
    from jobtracker import taskqueue
    client = app.test_client()
    from accounts import solve_proof
    client.post('/api/v1/auth/signup', json={'email': 'new@example.com', 'password': 'new-password-123',
                                             'password_confirmation': 'new-password-123', 'invite_code': None,
                                             'proof': solve_proof(client, 'signup')})
    app.config['SIGNUP_REQUIRES_INVITE'] = False
    client.post('/api/v1/auth/signup', json={'email': 'new@example.com', 'password': 'new-password-123',
                                             'password_confirmation': 'new-password-123',
                                             'proof': solve_proof(client, 'signup')})
    taskqueue.drain(app)
    message = mailer.outbox[-1]
    assert isinstance(message, EmailMessage) and message.get_content_type() == 'multipart/alternative'
    plain, html = message.get_body(('plain',)).get_content(), message.get_body(('html',)).get_content()
    link = re.search(r'https://\S+#token=[A-Za-z0-9_-]+', plain).group(0)
    with app.test_request_context('/'):
        expected = confirmations._message('signup', link, email='new@example.com')[1]
    assert plain == expected  # Byte for byte the existing text body.
    assert plain.startswith('Someone, hopefully you, asked to create a My Next Moov account')
    assert html.count(f'href="{link}"') == 2 and LOGO in html


def test_reset_and_notice_texts_are_unchanged(app):
    with app.test_request_context('/'):
        subject, text, html = password_reset._messages('reset', TOKEN_LINK, 60, email='a@example.com')
        assert subject == 'Reset your My Next Moov password'
        assert text == ('Someone, hopefully you, asked to reset the password of the My Next Moov account for this '
                        f'address.\n\nChoose a new password: {TOKEN_LINK}\n\nThe link works once and expires in 60 '
                        "minutes. If you didn't ask for this, ignore this email; your password hasn't changed.\n")
        assert html.count(f'href="{TOKEN_LINK}"') == 2
        subject, text, html = password_reset._messages('notice', at='2026-11-06 20:15', email='a@example.com')
        assert text == ('The password of your My Next Moov account was changed at 2026-11-06 20:15 (UTC).\n'
                        'Every browser was signed out.\n\n'
                        f"If this wasn't you, reset your password now: {BASE}/?forgot\n")
        assert '2026-11-06 20:15' in html and f'href="{BASE}/?forgot"' in html


def test_the_logo_route_serves_only_the_allowlisted_image(app):
    client = app.test_client()
    response = client.get('/static/email/mascot-96-v1.png')
    assert response.status_code == 200 and response.mimetype == 'image/png'
    assert response.data.startswith(b'\x89PNG') and len(response.data) < 10 * 1024
    assert response.headers['Cache-Control'] == 'public, max-age=31536000, immutable'
    assert response.headers['X-Content-Type-Options'] == 'nosniff'
    for path in ('/static/email/other.png', '/static/email/__init__.py', '/static/email/MASCOT-96-v1.png',
                 '/static/email/.%2e', '/static/email/mascot-96-v1.png%00'):
        assert client.get(path).status_code == 404, path
    # Anything with a slash in the name never reaches the route (the SPA catch-all answers); no file leaks.
    for path in ('/static/email/..%2f..%2f__init__.py', '/static/email/../__init__.py', '/static/email/x/y.png'):
        response = client.get(path)
        assert b'create_app' not in response.data and not response.data.startswith(b'\x89PNG'), path


def test_the_account_deleted_email_carries_the_branded_part(app):
    from types import SimpleNamespace
    from jobtracker import deletion
    with app.app_context():
        assert deletion.run_account_deleted_email(app, SimpleNamespace(payload={'email': 'gone@example.com'})) == 'sent'
    message = mailer.outbox[-1]
    plain, html = message.get_body(('plain',)).get_content(), message.get_body(('html',)).get_content()
    assert message['Subject'] == 'Your My Next Moov account was deleted'
    assert plain.startswith('Your My Next Moov account and everything in it were deleted on ')
    assert 'Your account was deleted' in html and LOGO in html and 'g•••@example.com' in html


def test_the_support_address_follows_mail_reply_to_in_both_parts(app):
    from types import SimpleNamespace
    from jobtracker import deletion
    for configured, expected in (('My Next Moov <Support@Example.test>', 'support@example.test'),
                                 (None, 'support@mynextmoov.com'), ('not an address', 'support@mynextmoov.com')):
        app.config['MAIL_REPLY_TO'] = configured
        with app.app_context():
            assert mailer.support_address() == expected
            deletion.run_account_deleted_email(app, SimpleNamespace(payload={'email': 'gone@example.com'}))
            _, _, changed = render(app, 'password_changed')
        message = mailer.outbox[-1]
        plain, html = message.get_body(('plain',)).get_content(), message.get_body(('html',)).get_content()
        assert f'write to {expected}.' in plain
        assert f'href="mailto:{expected}"' in html and f'write to {expected}.' in html
        assert f'href="mailto:{expected}"' in changed and f'email {expected}.' in changed
        other = 'support@example.test' if expected == 'support@mynextmoov.com' else 'support@mynextmoov.com'
        assert other not in plain and other not in html and other not in changed
