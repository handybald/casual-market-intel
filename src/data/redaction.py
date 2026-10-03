"""Last-line-of-defense credential redaction for free-form text.

Applied at every boundary where provider/exception text leaves the
process's control: log calls in the fetch layer, manifest `error` fields
(`Manifest.record`), and CLI summaries. Fetchers should never put
credentials in messages, but provider responses and exception chains are
not under our control (e.g. a malformed payload echoing a header, or a
`requests` error embedding the full request URL), so redaction is done
here, once, on the final text.

Two independent mechanisms:
  1. exact-value redaction of every credential this process actually
     holds (the `CREDENTIAL_ENV_VARS` values present in os.environ) --
     catches a secret wherever and however it appears;
  2. pattern redaction of credential-shaped key/value text: query/form
     parameters (incl. URL-encoded `=`), header lines, JSON / Python-repr
     pairs, Authorization/Bearer/Basic values.

Percent-encoding. A credential can also arrive URL-encoded -- an encoded
field name (`%61%70%69%4B%65%79=`), an encoded value, both, partial or
double encoding, in upper- or lower-case hex. Every whitespace/quote-
delimited token containing a `%XX` escape is decoded (up to
`_MAX_DECODE_LAYERS` layers) and both mechanisms are run on the decoded
form; ONLY a token whose decoded form contained credential material is
replaced (by its decoded, redacted text). Tokens without credentials keep
their original encoding, so ordinary logs are not rewritten. A final
check guarantees the security property that URL-decoding the sanitized
text cannot reconstruct a held credential (see `_recoverable`).

Patterns key off credential-NAMED fields only (api key, secret, token,
password, authorization, APCA headers, ...); a bare `key=` is only
treated as a credential in query-string position (`?key=` / `&key=`), so
ordinary words containing "key" (`monkey`, `sort_key`, `key_figure`) and
plain numbers are left alone.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Iterable, List
from urllib.parse import unquote, unquote_plus

REDACTED = "REDACTED"

# Every credential env var the pipeline reads. Values of these are redacted
# verbatim from any sanitized text.
CREDENTIAL_ENV_VARS = (
    "MASSIVE_API_KEY",
    "APCA_API_KEY_ID",
    "APCA_API_SECRET_KEY",
    "FRED_API_KEY",
    "BLS_API_KEY",
)
_MIN_SECRET_LEN = 6  # never blank out trivially short values

_NAME = (
    r"(?:api[_-]?key|apikey|api[_-]?secret|api[_-]?token|access[_-]?token|refresh[_-]?token|auth[_-]?token|"
    r"client[_-]?secret|secret[_-]?key|secret|password|passwd|token|"
    r"apca[_-]api[_-]key[_-]id|apca[_-]api[_-]secret[_-]key|x[_-]api[_-]key|key[_-]?id|registrationkey)"
)
_VALUE_STOP = r"""[^\s&'",;}\])]+"""

_PATTERNS = [
    # Authorization header / bearer / basic credentials
    (re.compile(r"(?i)(\bauthorization\b[\"']?\s*[:=]\s*[\"']?)(?:(bearer|basic|token)\s+)?" + _VALUE_STOP),
     lambda m: f"{m.group(1)}{(m.group(2) + ' ') if m.group(2) else ''}{REDACTED}"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), lambda m: f"{m.group(1)} {REDACTED}"),
    # JSON / Python-repr pairs: "apiKey": "v", 'APCA-API-SECRET-KEY': 'v'
    (re.compile(r"(?i)([\"']" + _NAME + r"[\"']\s*:\s*[\"'])([^\"']*)([\"'])"),
     lambda m: f"{m.group(1)}{REDACTED}{m.group(3)}"),
    # query/form/header-ish pairs: apiKey=v, apiKey%3Dv, APCA-API-KEY-ID: v
    # (%3F / %26 = URL-encoded "?" / "&": a word char precedes the name there)
    (re.compile(r"(?i)((?:(?<=%3F)|(?<=%26)|\b)" + _NAME + r"\b\s*(?:=|%3D|:)\s*)" + _VALUE_STOP),
     lambda m: f"{m.group(1)}{REDACTED}"),
    # bare `key=` only in query-string position
    (re.compile(r"(?i)((?:[?&]|%3F|%26)key(?:=|%3D))" + _VALUE_STOP), lambda m: f"{m.group(1)}{REDACTED}"),
]


def _held_secrets(extra: Iterable[str] = ()) -> list:
    values = [os.environ.get(name) for name in CREDENTIAL_ENV_VARS]
    values.extend(extra)
    return sorted({v for v in values if v and len(v) >= _MIN_SECRET_LEN}, key=len, reverse=True)


_MAX_DECODE_LAYERS = 3
_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
# A URL/query-ish run: delimited by whitespace, quotes and brackets, so an
# encoded URL embedded in JSON or an exception repr is one token.
_TOKEN = re.compile(r"""[^\s"'<>{}\[\]()|`,;]+""")
_UNSANITIZABLE = f"[{REDACTED}: text contained an encoded credential that could not be isolated]"


def _redact_plain(text: str, secrets: List[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, REDACTED)
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


def _decode_fully(token: str) -> str:
    for _ in range(_MAX_DECODE_LAYERS):
        decoded = unquote(token)
        if decoded == token:
            break
        token = decoded
    return token


def _redact_encoded_tokens(text: str, secrets: List[str]) -> str:
    def repl(m: "re.Match") -> str:
        token = m.group(0)
        if not _PERCENT_ESCAPE.search(token):
            return token
        decoded = _decode_fully(token)
        clean = _redact_plain(decoded, secrets)
        return clean if clean != decoded else token  # untouched unless it hid a credential
    return _TOKEN.sub(repl, text)


def _recoverable(text: str, secrets: List[str]) -> bool:
    """Would URL-decoding (plain or form-style, up to the layer limit)
    reconstruct any held credential from `text`?"""
    for decode in (unquote, unquote_plus):
        current = text
        for _ in range(_MAX_DECODE_LAYERS + 1):
            if any(secret in current for secret in secrets):
                return True
            decoded = decode(current)
            if decoded == current:
                break
            current = decoded
    return False


def redact_secrets(text, extra_secrets: Iterable[str] = ()) -> str:
    """Return `text` (any object, str()-ed) with credentials removed,
    including percent-encoded representations (see module docstring)."""
    out = "" if text is None else str(text)
    secrets = _held_secrets(extra_secrets)
    out = _redact_plain(out, secrets)
    out = _redact_encoded_tokens(out, secrets)
    if secrets and _recoverable(out, secrets):
        # A held credential is still reconstructible (e.g. encoded across
        # token delimiters): drop every percent-encoded token, then, if
        # that is still not enough, the whole text. Never emit it.
        out = _TOKEN.sub(lambda m: REDACTED if _PERCENT_ESCAPE.search(m.group(0)) else m.group(0), out)
        if _recoverable(out, secrets):
            return _UNSANITIZABLE
    return out


class RedactingLogFilter(logging.Filter):
    """Rewrites a record's final message through `redact_secrets`."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - never let logging itself fail
            return True
        clean = redact_secrets(message)
        if record.exc_info and record.exc_info[1] is not None:
            # Pre-render the traceback so it is redacted too.
            clean += "\n" + redact_secrets(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
            record.exc_text = None
        record.msg, record.args = clean, None
        return True


def install_log_redaction(logger: logging.Logger = None) -> None:
    """Attach `RedactingLogFilter` to every handler of `logger` (root by
    default). Handler-level, so records propagated from any module logger
    are covered. Idempotent."""
    target = logger or logging.getLogger()
    for handler in target.handlers:
        if not any(isinstance(f, RedactingLogFilter) for f in handler.filters):
            handler.addFilter(RedactingLogFilter())
