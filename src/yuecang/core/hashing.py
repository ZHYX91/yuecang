"""Deterministic content identities, independent of storage and application state."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator


def iter_canonical_json(value: object) -> Iterator[str]:
    yield from json.JSONEncoder(ensure_ascii=False, sort_keys=True,
                                separators=(",", ":"), allow_nan=False).iterencode(value)


def canonical_json(value: object) -> str:
    return "".join(iter_canonical_json(value))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def sha256_json(value: object) -> str:
    digest = hashlib.sha256()
    for part in iter_canonical_json(value):
        digest.update(part.encode("utf-8"))
    return digest.hexdigest()
