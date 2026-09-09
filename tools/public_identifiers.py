"""Exact reviewed public identifiers, shared by tree and history checks.

A declaration records human review, not automatic proof that data is public.
It never exempts a whole file, a pattern family, a path or a secret token.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

DECLARATIONS = "tools/public-identifiers.json"
HEX = re.compile(r"[0-9a-fA-F]{64}")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def parse(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate public-identifier declaration key")
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(value, dict) or set(value) != {"schema", "entries"} or value["schema"] != "oif-public-identifiers-v1":
        raise ValueError("Invalid public-identifier declaration schema")
    entries = value["entries"]
    if not isinstance(entries, list) or len(entries) > 100:
        raise ValueError("Invalid public-identifier entry list")
    seen = set()
    for row in entries:
        if not isinstance(row, dict) or set(row) != {"path", "blob_sha256", "kind", "value", "reason", "provenance"}:
            raise ValueError("Invalid public-identifier entry fields")
        if any(not isinstance(v, str) or not v.strip() for v in row.values()):
            raise ValueError("Public-identifier fields must be nonempty text")
        path = row["path"]
        if path == DECLARATIONS or "\\" in path or ":" in path or any(p in {"", ".", ".."} for p in path.split("/")):
            raise ValueError("Public identifiers require an exact relative artifact path")
        if not HEX.fullmatch(row["blob_sha256"]):
            raise ValueError("Public identifiers require the complete artifact digest")
        pattern = {"long_hex_identifier": HEX, "email": EMAIL}.get(row["kind"])
        if pattern is None or not pattern.fullmatch(row["value"]):
            raise ValueError("Only exact reviewed digest/email values can be declared")
        key = (path, row["blob_sha256"].lower(), row["kind"], row["value"])
        if key in seen:
            raise ValueError("Duplicate public identifier")
        seen.add(key)
    return entries


class PublicIdentifiers:
    def __init__(self, root: Path):
        path = root / DECLARATIONS
        self.entries = parse(path.read_bytes()) if path.exists() else []
        self.exact = {(r["path"], r["blob_sha256"].lower(), r["kind"], r["value"]) for r in self.entries}
        self.declared = {(r["kind"], r["value"]) for r in self.entries}
        self.declared.update(("long_hex_identifier", r["blob_sha256"]) for r in self.entries)

    def permits(self, path, raw, kind, value):
        kind = {"long_hex": "long_hex_identifier"}.get(kind, kind)
        if kind not in {"long_hex_identifier", "email"}:
            return False
        if path == DECLARATIONS:
            # Older declarations do not grant permission to other old blobs.
            # Only values still present in today's reviewed declarations qualify.
            parse(raw)
            return (kind, value) in self.declared
        return (path, digest(raw), kind, value) in self.exact
