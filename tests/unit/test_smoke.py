from __future__ import annotations

import hashlib
import io
import json
import zipfile

import pytest

from scripts.smoke import EXPECTED_EXPORT_FILES, SmokeError, validate_export


def _archive(*, corrupt_checksum: bool = False) -> bytes:
    files = {
        name: b"fixture\n"
        for name in EXPECTED_EXPORT_FILES
        if name not in {"manifest.json", "README.txt", "trace/model-usage.json"}
    }
    files["README.txt"] = "SYNTHETIC · NO SEND\n".encode()
    files["trace/model-usage.json"] = b'{"provider_calls":0}'
    checksums = {name: hashlib.sha256(value).hexdigest() for name, value in files.items()}
    if corrupt_checksum:
        checksums["campaign.json"] = "0" * 64
    files["manifest.json"] = json.dumps(
        {
            "synthetic": True,
            "no_send": True,
            "files": checksums,
        }
    ).encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, value in files.items():
            archive.writestr(name, value)
    return output.getvalue()


def test_smoke_export_validator_checks_full_no_send_archive() -> None:
    manifest = validate_export(_archive())

    assert manifest["synthetic"] is True
    assert manifest["no_send"] is True


def test_smoke_export_validator_rejects_checksum_mismatch() -> None:
    with pytest.raises(SmokeError, match="checksum mismatch"):
        validate_export(_archive(corrupt_checksum=True))
