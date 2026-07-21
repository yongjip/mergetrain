"""The machine-readable contract version (issue #44).

Every JSON/JSONL surface mergetrain emits — one-shot ``--json`` payloads, the
HTTP dashboard snapshot, and the resumable event stream — carries a single
top-level ``contract_version`` so a consumer can tell "is this the shape I
understand?" from one integer comparison.

This is deliberately **separate** from the product ``__version__`` (which bumps
for unrelated reasons) and from the SQLite ``SCHEMA_VERSION``. It mirrors the
one-number-per-artifact discipline of ``store.SCHEMA_VERSION`` and
``registry.REGISTRY_VERSION``.

Compatibility rule (enforced by ``tests/test_contract_fingerprints.py`` once it
lands): additive changes — a new key on a payload, a new optional field, a new
command — do NOT bump this number; consumers must ignore unknown keys and
dispatch JSONL on ``type``. Removing or renaming a key, or changing a value's
type or meaning, is breaking and requires a deliberate bump.
"""

from __future__ import annotations

CONTRACT_VERSION = 1
