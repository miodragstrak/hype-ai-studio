import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from backend.app.config import settings

REDACTED = "[REDACTED]"
_BEARER = re.compile(r"(?i)(authorization\s*[:=]\s*)?bearer\s+[^\s,;]+")
_SENSITIVE_QUERY = re.compile(
    r"(?i)([?&](?:key|api_key|apikey|token|access_token|signature|sig)=)[^&\s]+"
)
_RUNWAY_URI = re.compile(r"runway://[^\s,;'\"]+")
_SENSITIVE_QUERY_KEYS = {"key", "api_key", "apikey", "token", "access_token", "signature", "sig"}


def sanitize_text(value: object) -> str:
    text = str(value)
    secret = settings.runwayml_api_secret
    if secret and secret.get_secret_value():
        text = text.replace(secret.get_secret_value(), REDACTED)
    openai_secret = settings.openai_api_key
    if openai_secret and openai_secret.get_secret_value():
        text = text.replace(openai_secret.get_secret_value(), REDACTED)
    text = _BEARER.sub(lambda match: f"{match.group(1) or ''}{REDACTED}", text)
    text = _SENSITIVE_QUERY.sub(lambda match: f"{match.group(1)}{REDACTED}", text)
    text = _RUNWAY_URI.sub(REDACTED, text)
    try:
        parsed = urlsplit(text)
        if parsed.scheme and parsed.query:
            query = [
                (key, REDACTED if key.lower() in _SENSITIVE_QUERY_KEYS else item)
                for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            ]
            text = urlunsplit(
                (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
            )
    except ValueError:
        pass
    return text


def sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): REDACTED
            if str(key).lower()
            in {
                "authorization",
                "runwayml_api_secret",
                "openai_api_key",
                "api_key",
                "apikey",
                "x-api-key",
            }
            else sanitize(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        return sanitize_text(value)
    return value
