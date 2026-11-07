"""dist/ is served publicly by frontend(): it must hold no source maps, Sentry config, env files or DSNs."""
import re
from pathlib import Path

import pytest

DIST = Path(__file__).resolve().parent.parent / 'frontend' / 'dist'


@pytest.mark.skipif(not (DIST / 'index.html').is_file(), reason='frontend not built (npm run build)')
def test_dist_holds_no_maps_config_or_dsn():
    files = [path for path in DIST.rglob('*') if path.is_file()]
    names = {path.name for path in files}
    assert not [name for name in names if name.endswith('.map') or name == '.sentryclirc' or name.startswith('.env')]
    for path in files:
        if path.suffix in ('.js', '.html', '.css'):
            text = path.read_text(errors='replace')
            assert not re.search(r'[0-9a-f]{16,64}@o\d+\.ingest', text), path.name  # no baked-in DSN
            assert 'sourceMappingURL' not in text, path.name
            for marker in ('rrweb', 'replayIntegration', 'feedbackIntegration', 'browserTracingIntegration',
                           'captureConsoleIntegration'):
                assert marker not in text, (path.name, marker)
