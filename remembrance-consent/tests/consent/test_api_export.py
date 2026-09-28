"""Export: gated by the kernel, verifiable offline, tamper-evident."""
from __future__ import annotations

import io
import json
import zipfile
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select

from app.consent.audit import iter_events
from app.consent.constants import AuditEventType
from app.consent.models import AuditEvent
from app.consent.tokens import KeyRing
from app.export.manifest import build_bundle, verify_bundle
from tests.support import ADMIN, BEN_A, OWNER, STRANGER, SUCCESSOR, Person


def export(client, profile_id, who=OWNER, expect=201):
    response = client.post(f"/export/{profile_id}", headers=who.headers())
    assert response.status_code == expect, response.text
    return response.json()


def download(client, url):
    return client.get(url.replace("http://testserver", ""))


def rezip(data: bytes, change) -> bytes:
    source = zipfile.ZipFile(io.BytesIO(data))
    files = {name: source.read(name) for name in source.namelist()}
    change(files)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as target:
        for name, content in files.items():
            target.writestr(name, content)
    return out.getvalue()


@pytest.fixture
def exported(api, client, services):
    grant = api.ready_grant()
    client.post("/successors", json={"deceased_profile_id": grant["deceased_profile_id"], "successor_user_id": SUCCESSOR.user_id,
                                     "priority_order": 1}, headers=OWNER.headers())
    api.authorize(OWNER, "INITIATE_VOICE_SYNTHESIS", grant["deceased_profile_id"], "VOICE_SYNTHESIS")
    result = export(client, grant["deceased_profile_id"])
    response = download(client, result["url"])
    assert response.status_code == 200
    return grant, result, response


def test_export_archive_is_complete_and_verifies(exported, services):
    grant, result, response = exported
    assert response.headers["content-type"] == "application/zip" and response.headers["cache-control"] == "no-store"
    report = verify_bundle(response.content, services.keyring.verification_keys)
    assert report.ok and report.signature_checked, report.errors
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert archive.namelist() == [
        "acknowledgments.json", "audit_log.jsonl", "derived_artifacts.json", "grants.json", "profile.json",
        "revocation_requests.json", "successor_designations.json", "manifest.json", "manifest.sig",
    ]
    manifest = json.loads(archive.read("manifest.json"))
    assert manifest["profile_id"] == grant["deceased_profile_id"] and result["file_count"] == len(manifest["files"]) == 7
    grants = json.loads(archive.read("grants.json"))
    assert grants[0]["named_beneficiaries"][0]["email"] == "ben.a@example.com"
    assert len(json.loads(archive.read("acknowledgments.json"))) == 2
    assert json.loads(archive.read("successor_designations.json"))[0]["successor_user_id"] == SUCCESSOR.user_id
    audit_lines = archive.read("audit_log.jsonl").decode().splitlines()
    assert manifest["audit_chain"]["length"] == len(audit_lines) and manifest["audit_chain"]["verified_at_export"]


def test_export_is_audited_with_the_manifest_hash(exported, services):
    grant, result, _ = exported
    with services.session_factory() as s:
        [event] = s.scalars(select(AuditEvent).where(AuditEvent.event_type == AuditEventType.DATA_EXPORTED)).all()
        chain = list(iter_events(s, UUID(grant["deceased_profile_id"])))
    assert event.payload["manifest_sha256"] == result["manifest_sha256"] and event.payload["export_id"] == result["export_id"]
    assert event.consent_grant_id == UUID(grant["id"])
    kinds = [e.event_type for e in chain]
    assert kinds.index("AUTHORIZATION_GRANTED") < kinds.index("DATA_EXPORTED")  # the export's own kernel decision


def test_download_link_is_a_capability(exported, client, clock):
    _, result, _ = exported
    path = result["url"].replace("http://testserver", "")
    tampered = path[:-1] + ("0" if path[-1] != "0" else "1")
    assert client.get(tampered).json()["error"]["code"] == "LINK_INVALID"
    other_key = path.replace("exports%2F", "models%2F")
    assert client.get(other_key).status_code == 403
    clock.advance(seconds=901)
    assert client.get(path).status_code == 403


def test_missing_object_is_404(exported, client, storage):
    _, result, _ = exported
    for file in (storage.root / "exports").rglob("*.zip"):
        file.unlink()
    assert download(client, result["url"]).status_code == 404


@pytest.mark.parametrize("who", [ADMIN, SUCCESSOR])
def test_admins_and_successors_can_export(exported, client, who):
    grant, _, _ = exported
    assert export(client, grant["deceased_profile_id"], who)["file_count"] == 7


def test_export_survives_revocation(exported, api, client):
    grant, _, _ = exported
    api.revoke(grant["id"], BEN_A)
    export(client, grant["deceased_profile_id"])


def test_export_access_rules(api, client):
    grant = api.create_grant()  # unverified
    profile_id = grant["deceased_profile_id"]
    assert export(client, profile_id, OWNER, expect=403)["error"]["reason"] == "ROLE_NOT_PERMITTED"
    assert export(client, profile_id, BEN_A, expect=403)["error"]["code"] == "EXPORT_DENIED"
    export(client, profile_id, STRANGER, expect=404)
    export(client, str(uuid4()), ADMIN, expect=404)


# --- offline verification -----------------------------------------------------

@pytest.mark.parametrize(
    "change,message",
    [
        (lambda f: f.__setitem__("grants.json", b"[]\n"), "grants.json: sha256"),
        (lambda f: f.__setitem__("extra.txt", b"hi"), "extra.txt: not listed"),
        (lambda f: f.pop("profile.json"), "profile.json: listed but missing"),
        (lambda f: f.__setitem__("manifest.sig", b"AAAA\n"), "signature invalid"),
        (lambda f: f.pop("manifest.sig"), "manifest.sig missing"),
    ],
)
def test_tampering_is_detected(exported, services, change, message):
    _, _, response = exported
    report = verify_bundle(rezip(response.content, change), services.keyring.verification_keys)
    assert not report.ok and any(message in e for e in report.errors), report.errors


def test_rewritten_audit_log_is_detected_even_with_a_consistent_manifest(exported, services):
    _, _, response = exported

    def rewrite(files):
        lines = files["audit_log.jsonl"].decode().splitlines()
        event = json.loads(lines[1])
        event["actor_id"] = "mallory"
        lines[1] = json.dumps(event)
        files["audit_log.jsonl"] = ("\n".join(lines) + "\n").encode()
        manifest = json.loads(files["manifest.json"])
        import hashlib

        for entry in manifest["files"]:
            if entry["path"] == "audit_log.jsonl":
                entry["sha256"] = hashlib.sha256(files["audit_log.jsonl"]).hexdigest()
                entry["bytes"] = len(files["audit_log.jsonl"])
        files["manifest.json"] = json.dumps(manifest).encode()

    report = verify_bundle(rezip(response.content, rewrite), services.keyring.verification_keys)
    assert "audit_log.jsonl: event" in " ".join(report.errors) and "manifest signature invalid" in report.errors


def test_structural_failures(exported, services):
    _, _, response = exported
    assert verify_bundle(b"not a zip").errors == ["not a zip archive"]
    assert verify_bundle(rezip(response.content, lambda f: f.pop("manifest.json"))).errors == ["manifest.json missing"]
    assert verify_bundle(rezip(response.content, lambda f: f.__setitem__("manifest.json", b"{"))).errors == ["manifest.json is not JSON"]
    no_log = verify_bundle(rezip(response.content, lambda f: f.pop("audit_log.jsonl")))
    assert "audit_log.jsonl missing" in no_log.errors
    garbage = verify_bundle(rezip(response.content, lambda f: f.__setitem__("audit_log.jsonl", b"{}\n")))
    assert any("unreadable" in e for e in garbage.errors)
    other = verify_bundle(response.content, {"someone-else": Ed25519PrivateKey.generate().public_key()})
    assert any("no public key" in e for e in other.errors)
    unsigned_check = verify_bundle(response.content)
    assert unsigned_check.ok and not unsigned_check.signature_checked
    assert unsigned_check.to_json()["audit_events"] > 0


def test_bundles_are_deterministic_and_reject_foreign_events(services, clock):
    profile_id = uuid4()
    ring = KeyRing.generate("k")
    kwargs = dict(profile_id=profile_id, generated_at=clock.now(), generated_by="u", documents={"profile.json": {"a": 1}},
                  audit_events=[], keyring=ring)
    first, second = build_bundle(**kwargs), build_bundle(**kwargs)
    assert first.data == second.data and first.manifest["audit_chain"]["length"] == 0
    assert verify_bundle(first.data, ring.verification_keys).ok

    def smuggle(files):
        files["manifest.json"] = json.dumps({**json.loads(files["manifest.json"]), "format": "v0"}).encode()

    assert any("unsupported format" in e for e in verify_bundle(rezip(first.data, smuggle)).errors)


def test_export_of_profile_events_only(api, client, services):
    first = api.ready_grant()
    api.ready_grant(OWNER)  # a second profile with its own chain
    response = download(client, export(client, first["deceased_profile_id"])["url"])
    report = verify_bundle(response.content, services.keyring.verification_keys)
    assert report.ok and list(report.chain.chains) == [UUID(first["deceased_profile_id"])]


def test_unverified_email_owner_path(api, client):
    grant = api.ready_grant()
    owner_without_email = Person(OWNER.user_id)
    export(client, grant["deceased_profile_id"], owner_without_email)
