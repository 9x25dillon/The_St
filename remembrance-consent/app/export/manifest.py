"""Deterministic, self-verifying export archives.

Archive layout (remembrance-export/v1):

    profile.json
    grants.json                   grants with named beneficiaries
    acknowledgments.json
    successor_designations.json
    revocation_requests.json
    derived_artifacts.json        voice models, audio, deliveries (metadata only)
    audit_log.jsonl               the profile's complete audit chain
    manifest.json                 {path, sha256, bytes} per file + chain head
    manifest.sig                  Ed25519 signature over manifest.json bytes

Because each profile has its own audit chain, audit_log.jsonl verifies from
genesis with no other data. Zip entries use fixed timestamps and sorted
order, so the same data always yields byte-identical archives. The archive
remains verifiable after the company is gone: `scripts/verify_export.py`
needs only the zip (and, for the signature, the published public key).
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.consent.audit import ChainReport, EventRecord, canonical_json, format_timestamp, verify_chain
from app.consent.tokens import KeyRing

EXPORT_FORMAT = "remembrance-export/v1"
MANIFEST = "manifest.json"
SIGNATURE = "manifest.sig"
AUDIT_LOG = "audit_log.jsonl"
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
MAX_ENTRY_BYTES = 512 * 1024 * 1024


def _json_file(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n").encode()


def audit_jsonl(events: list[EventRecord]) -> bytes:
    return "".join(canonical_json(e.to_json()) + "\n" for e in events).encode()


@dataclass(frozen=True)
class ExportBundle:
    data: bytes
    manifest: dict[str, Any]
    manifest_sha256: str
    file_count: int


def build_bundle(
    *,
    profile_id: UUID,
    generated_at: datetime,
    generated_by: str,
    documents: Mapping[str, Any],
    audit_events: list[EventRecord],
    keyring: KeyRing,
) -> ExportBundle:
    files: dict[str, bytes] = {name: _json_file(value) for name, value in documents.items()}
    files[AUDIT_LOG] = audit_jsonl(audit_events)
    report = verify_chain(audit_events)
    chain = report.chains.get(profile_id)
    manifest = {
        "format": EXPORT_FORMAT,
        "profile_id": str(profile_id),
        "generated_at": format_timestamp(generated_at),
        "generated_by": generated_by,
        "hash_algorithm": "sha256",
        "files": [
            {"path": name, "sha256": hashlib.sha256(files[name]).hexdigest(), "bytes": len(files[name])}
            for name in sorted(files)
        ],
        "audit_chain": {
            "length": chain.length if chain else 0,
            "head_hash": chain.head_hash if chain else "0" * 64,
            "verified_at_export": report.ok,
        },
        "signature": {"algorithm": "Ed25519", "key_id": keyring.signing_kid, "file": SIGNATURE},
    }
    manifest_bytes = (canonical_json(manifest) + "\n").encode()
    signature = base64.b64encode(keyring.signing_key.sign(manifest_bytes)) + b"\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in [*sorted(files.items()), (MANIFEST, manifest_bytes), (SIGNATURE, signature)]:
            info = zipfile.ZipInfo(name, date_time=ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, content)
    return ExportBundle(
        data=buffer.getvalue(),
        manifest=manifest,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        file_count=len(files),
    )


@dataclass
class ExportVerification:
    errors: list[str] = field(default_factory=list)
    signature_checked: bool = False
    chain: ChainReport | None = None
    manifest: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "signature_checked": self.signature_checked,
            "errors": self.errors,
            "audit_events": self.chain.event_count if self.chain else 0,
        }


def verify_bundle(data: bytes, public_keys: Mapping[str, Ed25519PublicKey] | None = None) -> ExportVerification:
    result = ExportVerification()
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        result.errors.append("not a zip archive")
        return result
    with archive:
        names = set(archive.namelist())
        if MANIFEST not in names:
            result.errors.append("manifest.json missing")
            return result
        manifest_bytes = archive.read(MANIFEST)
        try:
            manifest = json.loads(manifest_bytes)
        except json.JSONDecodeError:
            result.errors.append("manifest.json is not JSON")
            return result
        result.manifest = manifest
        if manifest.get("format") != EXPORT_FORMAT:
            result.errors.append(f"unsupported format {manifest.get('format')!r}")
        listed = {entry["path"]: entry for entry in manifest.get("files", [])}
        for name in sorted(names - listed.keys() - {MANIFEST, SIGNATURE}):
            result.errors.append(f"{name}: not listed in the manifest")
        for name, entry in sorted(listed.items()):
            if name not in names:
                result.errors.append(f"{name}: listed but missing")
                continue
            info = archive.getinfo(name)
            if info.file_size > MAX_ENTRY_BYTES:
                result.errors.append(f"{name}: larger than {MAX_ENTRY_BYTES} bytes")
                continue
            content = archive.read(name)
            if hashlib.sha256(content).hexdigest() != entry.get("sha256") or len(content) != entry.get("bytes"):
                result.errors.append(f"{name}: sha256 or size does not match the manifest")

        if AUDIT_LOG in names:
            try:
                events = [EventRecord.from_json(json.loads(line)) for line in archive.read(AUDIT_LOG).splitlines() if line.strip()]
            except (KeyError, TypeError, ValueError) as exc:
                result.errors.append(f"{AUDIT_LOG}: unreadable ({exc})")
                events = []
            result.chain = verify_chain(events)
            for error in result.chain.errors:
                result.errors.append(f"{AUDIT_LOG}: event {error.event_id} {error.kind.value}")
            expected = manifest.get("audit_chain", {})
            profile_id = manifest.get("profile_id")
            head = next((c for pid, c in result.chain.chains.items() if str(pid) == profile_id), None)
            if (head.length if head else 0) != expected.get("length") or (head.head_hash if head else "0" * 64) != expected.get("head_hash"):
                result.errors.append(f"{AUDIT_LOG}: chain length/head differ from the manifest")
            if any(str(pid) != profile_id for pid in result.chain.chains):
                result.errors.append(f"{AUDIT_LOG}: contains events of another profile")
        else:
            result.errors.append(f"{AUDIT_LOG} missing")

        if public_keys is not None:
            kid = manifest.get("signature", {}).get("key_id")
            key = public_keys.get(str(kid))
            if key is None:
                result.errors.append(f"no public key for key id {kid!r}")
            elif SIGNATURE not in names:
                result.errors.append("manifest.sig missing")
            else:
                try:
                    key.verify(base64.b64decode(archive.read(SIGNATURE).strip()), manifest_bytes)
                    result.signature_checked = True
                except (InvalidSignature, ValueError):
                    result.errors.append("manifest signature invalid")
    return result
