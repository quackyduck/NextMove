"""Test-server deploy files: logs are kept about 7 days, and nginx writes no access log for /nextmove."""
import re
from pathlib import Path

DEPLOY = Path(__file__).resolve().parent.parent / 'deploy'


def test_units_log_to_the_retention_limited_namespace():
    for unit in ('next-move.service', 'next-move-worker.service'):
        assert re.search(r'^LogNamespace=nextmove$', (DEPLOY / unit).read_text(), re.MULTILINE), unit


def test_the_namespace_keeps_about_seven_days():
    settings = dict(re.findall(r'^(\w+)=(\S+)$', (DEPLOY / 'journald-nextmove.conf').read_text(), re.MULTILINE))
    assert settings['MaxRetentionSec'] == '7day' and settings['MaxFileSec'] == '1day'
    assert settings['Storage'] == 'persistent' and settings['SystemMaxUse']


def test_every_nginx_location_turns_its_access_log_off():
    snippet = (DEPLOY / 'nginx-nextmove.conf').read_text()
    blocks = re.findall(r'^location [^{]+\{(.*?)^\}', snippet, re.MULTILINE | re.DOTALL)
    assert blocks
    for block in blocks:
        assert 'access_log off;' in block, block


def test_gunicorn_writes_no_access_log():
    conf = (DEPLOY / 'gunicorn.conf.py').read_text()
    assert re.search(r'^accesslog = None$', conf, re.MULTILINE)
