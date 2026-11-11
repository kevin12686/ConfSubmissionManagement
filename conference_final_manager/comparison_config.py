"""Standard-library-only, secret-safe compute gateway configuration."""
import json
import re


SETTING = "CFM_COMPARISON_REMOTE_HEADERS"
_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}\Z")
_RESERVED = {
    "authorization", "proxy-authorization", "proxy-authenticate", "host",
    "accept", "content-type", "content-length", "content-encoding",
    "connection", "transfer-encoding", "te", "trailer", "upgrade", "expect",
    "keep-alive", "proxy-connection",
}


def parse_remote_headers(raw, *, url):
    """Return optional JSON headers without exposing input in validation errors."""
    message = (
        f"Invalid {SETTING}: use a JSON object of unique HTTP header names and "
        "printable ASCII string values; worker/protocol headers cannot be overridden."
    )
    if not isinstance(raw, str) or len(raw) > 16384:
        raise ValueError(message)
    if not raw.strip():
        return {}

    def unique(pairs):
        values = {}
        seen = set()
        for name, value in pairs:
            lower = name.lower()
            if lower in seen:
                raise ValueError(message)
            seen.add(lower)
            values[name] = value
        return values

    try:
        values = json.loads(raw, object_pairs_hook=unique)
    except (ValueError, RecursionError):
        raise ValueError(message) from None
    if not isinstance(values, dict) or len(values) > 32:
        raise ValueError(message)
    for name, value in values.items():
        if (
            not _NAME.fullmatch(name) or name.lower() in _RESERVED
            or name.lower().startswith("x-cfm-") or not isinstance(value, str)
            or len(value) > 4096 or any(ord(c) < 32 or ord(c) > 126 for c in value)
        ):
            raise ValueError(message)
    if values and not url.strip():
        raise ValueError(f"{SETTING} requires CFM_COMPARISON_REMOTE_URL.")
    return values
