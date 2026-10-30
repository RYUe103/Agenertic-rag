from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path


_DASHES = re.compile(r"[\u2010-\u2015\u2212\ufe58\ufe63\uff0d]")


def normalize_identifier(value: str | None) -> str | None:
    if not value:
        return None
    normalized = unicodedata.normalize("NFKC", value)
    normalized = _DASHES.sub("-", normalized).upper()
    return re.sub(r"\s+", "", normalized).strip() or None


def normalize_filename(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = _DASHES.sub("-", normalized).casefold()
    return re.sub(r"\s+", "", normalized)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def redact_error(message: str, secrets: list[str]) -> str:
    result = message
    for secret in secrets:
        if secret:
            result = result.replace(secret, "***")
    return result[:2000]

