"""Canonical JSON and stable hashing shared by every rehearsal artifact."""

from __future__ import annotations

import hashlib
import json


def canonical_json(value: object) -> str:
    """Serialize a value so the same content always hashes identically."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_hash(value: object) -> str:
    """SHA-256 over the canonical form; the identity of an offline input."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
