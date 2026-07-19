from __future__ import annotations

import io

from scripts.image_security import _stream_contains


def test_stream_secret_scan_detects_chunk_boundary() -> None:
    needle = b"owner-secret-value"
    stream = io.BytesIO(b"x" * (64 * 1024 - 5) + needle + b"tail")

    assert _stream_contains(stream, needle) is True


def test_stream_secret_scan_ignores_unrelated_content() -> None:
    assert _stream_contains(io.BytesIO(b"synthetic only"), b"owner-secret-value") is False
