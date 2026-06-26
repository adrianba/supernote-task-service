"""Unit tests for encoding and conversion helpers."""

from __future__ import annotations

from datetime import UTC, datetime

from supernote_task_service.encoding import (
    datetime_to_ms,
    decode_document_link,
    decode_emoji,
    encode_detail,
    encode_document_link,
    encode_emoji,
    is_valid_id,
    ms_to_datetime,
    new_id,
)


def test_emoji_round_trip() -> None:
    text = "Buy groceries \U0001f6d2"
    encoded = encode_emoji(text)
    assert encoded == "Buy groceries [U+1F6D2]"
    assert decode_emoji(encoded) == text


def test_decode_emoji_handles_plain_text() -> None:
    assert decode_emoji("plain") == "plain"
    assert decode_emoji(None) == ""


def test_encode_detail_truncates_after_encoding() -> None:
    detail = "\U0001f6d2" * 50  # each emoji encodes to 9 chars
    assert len(encode_detail(detail)) == 255


def test_timestamp_round_trip() -> None:
    dt = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    ms = datetime_to_ms(dt)
    assert ms_to_datetime(ms) == dt
    assert ms_to_datetime(0) is None


def test_document_link_round_trip() -> None:
    payload = {
        "appName": "note",
        "fileId": "F123",
        "filePath": "/x/y.note",
        "page": 3,
        "pageId": "P456",
    }
    encoded = encode_document_link(payload)
    assert decode_document_link(encoded) == payload
    assert decode_document_link(None) is None
    assert decode_document_link("not-base64-json!!") is None


def test_id_helpers() -> None:
    generated = new_id()
    assert is_valid_id(generated)
    assert not is_valid_id("UPPER" + "0" * 27)
    assert not is_valid_id("short")
