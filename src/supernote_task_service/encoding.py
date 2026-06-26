"""Encoding and conversion helpers for the Supernote MariaDB schema.

The Supernote database stores timestamps as Unix epoch milliseconds, encodes
emoji as ``[U+XXXX]`` sequences (its ``utf8`` columns are 3-byte only), and
stores document links as Base64-encoded JSON in the ``links`` column.
"""

from __future__ import annotations

import base64
import json
import re
import time
import uuid
from datetime import UTC, datetime

_EMOJI_DECODE_RE = re.compile(r"\[U\+([0-9A-Fa-f]+)\]")
_HEX_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# The Supernote ``detail`` column is varchar(255); truncate after encoding.
DETAIL_MAX_LEN = 255


def now_ms() -> int:
    """Current time as a Unix epoch timestamp in milliseconds."""
    return int(time.time() * 1000)


def ms_to_datetime(ms: int | None) -> datetime | None:
    """Convert a millisecond timestamp to an aware UTC datetime (None if 0/NULL)."""
    if not ms or int(ms) <= 0:
        return None
    return datetime.fromtimestamp(int(ms) / 1000, tz=UTC)


def datetime_to_ms(dt: datetime) -> int:
    """Convert a datetime to a millisecond Unix timestamp.

    Naive datetimes are interpreted as UTC.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def new_id() -> str:
    """Generate a 32-character lowercase hex identifier."""
    return uuid.uuid4().hex


def is_valid_id(value: str) -> bool:
    """Return True if ``value`` is a 32-char lowercase hex identifier."""
    return bool(_HEX_ID_RE.match(value))


def encode_emoji(text: str) -> str:
    """Encode characters above U+FFFF as ``[U+XXXX]`` for Supernote's utf8 columns."""
    if not text:
        return text or ""
    out: list[str] = []
    for ch in text:
        if ord(ch) > 0xFFFF:
            out.append(f"[U+{ord(ch):X}]")
        else:
            out.append(ch)
    return "".join(out)


def decode_emoji(text: str | None) -> str:
    """Decode ``[U+XXXX]`` sequences back into their characters."""
    if not text or "[U+" not in text:
        return text or ""

    def _replace(match: re.Match[str]) -> str:
        try:
            code = int(match.group(1), 16)
        except ValueError:
            return match.group(0)
        # Leave lone surrogate-range tokens untouched: chr() would produce an
        # unencodable surrogate that breaks JSON/UTF-8 serialization.
        if 0xD800 <= code <= 0xDFFF or code > 0x10FFFF:
            return match.group(0)
        return chr(code)

    return _EMOJI_DECODE_RE.sub(_replace, text)


def encode_detail(detail: str) -> str:
    """Emoji-encode the detail field and truncate to the column limit.

    Truncation happens on character boundaries so a multi-character ``[U+XXXX]``
    token is never split in half.
    """
    if not detail:
        return detail or ""
    out: list[str] = []
    length = 0
    for ch in detail:
        token = f"[U+{ord(ch):X}]" if ord(ch) > 0xFFFF else ch
        if length + len(token) > DETAIL_MAX_LEN:
            break
        out.append(token)
        length += len(token)
    return "".join(out)


def decode_document_link(links_value: str | None) -> dict[str, object] | None:
    """Decode the Base64 JSON document link stored in the ``links`` column."""
    if not links_value:
        return None
    try:
        decoded = base64.b64decode(links_value).decode("utf-8")
        parsed = json.loads(decoded)
    except (ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def encode_document_link(link: dict[str, object]) -> str:
    """Encode a document-link mapping as Base64 JSON for the ``links`` column."""
    payload = json.dumps(link, separators=(",", ":"), ensure_ascii=False)
    return base64.b64encode(payload.encode("utf-8")).decode("utf-8")
