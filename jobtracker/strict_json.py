"""Parse untrusted JSON into text that SQLite and responses can always encode as UTF-8."""
import json
import re

from flask.json.provider import DefaultJSONProvider

# json joins an escaped surrogate pair ("😀") into one code point, so any surrogate left is unpaired.
SURROGATE = re.compile('[\ud800-\udfff]')
# Far deeper than any request needs. Python's own limit varies by version (3.12 raises RecursionError near 1000
# levels, 3.14 parses 100000), so enforce one explicitly for the same answer everywhere.
MAX_DEPTH = 64


def loads(text, **kwargs):
    """json.loads that treats an unpaired UTF-16 surrogate in any key or value, or nesting deeper than MAX_DEPTH,
    as malformed JSON (ValueError)."""
    try:
        value = json.loads(text, **kwargs)
    except RecursionError:
        raise ValueError('JSON nested too deeply') from None
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if isinstance(item, str):
            if SURROGATE.search(item):
                raise ValueError('JSON text contains an unpaired UTF-16 surrogate')
        elif isinstance(item, (dict, list)):
            if depth == MAX_DEPTH:
                raise ValueError('JSON nested too deeply')
            children = [*item, *item.values()] if isinstance(item, dict) else item
            pending.extend((child, depth + 1) for child in children)
    return value


class JSONProvider(DefaultJSONProvider):
    """Every request.get_json() goes through here, so bad text is rejected like any other malformed body."""
    def loads(self, s, **kwargs):
        return loads(s, **kwargs)
