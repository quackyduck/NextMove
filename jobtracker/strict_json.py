"""Parse untrusted JSON into text that SQLite and responses can always encode as UTF-8."""
import json
import re

from flask.json.provider import DefaultJSONProvider

# json joins an escaped surrogate pair ("😀") into one code point, so any surrogate left is unpaired.
SURROGATE = re.compile('[\ud800-\udfff]')


def loads(text, **kwargs):
    """json.loads that treats an unpaired UTF-16 surrogate in any key or value as malformed JSON (ValueError)."""
    try:
        value = json.loads(text, **kwargs)
    except RecursionError:
        raise ValueError('JSON nested too deeply') from None
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            if SURROGATE.search(item):
                raise ValueError('JSON text contains an unpaired UTF-16 surrogate')
        elif isinstance(item, dict):
            pending.extend(item)
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return value


class JSONProvider(DefaultJSONProvider):
    """Every request.get_json() goes through here, so bad text is rejected like any other malformed body."""
    def loads(self, s, **kwargs):
        return loads(s, **kwargs)
