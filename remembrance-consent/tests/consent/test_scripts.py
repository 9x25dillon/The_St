"""scripts/verify_audit_chain.py, verify_export.py, generate_signing_key.py."""
from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.consent.audit import append_event
from app.consent.constants import AuditEventType
from app.db import create_sqlite_schema, make_engine, make_session_factory
from scripts import generate_signing_key, verify_audit_chain, verify_export
from tests.support import OWNER


@pytest.fixture
def ledger_db(tmp_path, clock):
    path = tmp_path / "ledger.db"
    url = f"sqlite+pysqlite:///{path}"
    engine = make_engine(url)
    create_sqlite_schema(engine)
    sf = make_session_factory(engine)
    profiles = [uuid4(), uuid4()]
    for n in range(6):
        with sf.begin() as s:
            append_event(s, clock=clock, event_type=AuditEventType.CONSENT_CREATED, actor_id="a",
                         deceased_profile_id=profiles[n % 2], payload={"n": n})
        clock.advance(seconds=1)
    yield url, engine, profiles
    engine.dispose()


def tamper(engine, statement):
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TRIGGER audit_event_no_update")
        connection.exec_driver_sql("DROP TRIGGER audit_event_no_delete")
        connection.execute(text(statement))


def test_clean_ledger_passes(ledger_db, capsys):
    url, _, profiles = ledger_db
    assert verify_audit_chain.main(["--database-url", url]) == 0
    assert "OK: audit chains intact" in capsys.readouterr().out
    assert verify_audit_chain.main(["--database-url", url, "--profile", str(profiles[0]), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report == {"ok": True, "chains": 1, "events": 3, "errors": [], "heads": report["heads"]}


def test_environment_url_is_used(ledger_db, monkeypatch):
    url, _, _ = ledger_db
    monkeypatch.setenv("REMEMBRANCE_DATABASE_URL", url)
    assert verify_audit_chain.main([]) == 0


def test_tampered_ledger_fails(ledger_db, capsys):
    url, engine, _ = ledger_db
    tamper(engine, "UPDATE audit_event SET payload = '{\"n\": 42}' WHERE id = 3")
    assert verify_audit_chain.main(["--database-url", url]) == 1
    out = capsys.readouterr().out
    assert "TAMPERED" in out and "event=3 HASH_MISMATCH" in out and "FAIL: 1 problem(s)" in out


def test_anchors_catch_truncation(ledger_db, tmp_path, capsys):
    url, engine, _ = ledger_db
    anchors = tmp_path / "heads.json"
    assert verify_audit_chain.main(["--database-url", url, "--emit-anchors", str(anchors)]) == 0
    tamper(engine, "DELETE FROM audit_event WHERE id = (SELECT max(id) FROM audit_event)")
    assert verify_audit_chain.main(["--database-url", url]) == 0  # a hash chain alone can't see truncation
    assert verify_audit_chain.main(["--database-url", url, "--anchors", str(anchors), "--json"]) == 1
    assert "ANCHOR_MISSING" in capsys.readouterr().out


def test_jsonl_mode(ledger_db, tmp_path):
    url, engine, profiles = ledger_db
    from app.consent.audit import iter_events

    with make_session_factory(engine)() as s:
        lines = [json.dumps(e.to_json()) for e in iter_events(s)]
    log = tmp_path / "audit_log.jsonl"
    log.write_text("\n".join(lines) + "\n\n")
    assert verify_audit_chain.main(["--jsonl", str(log)]) == 0
    assert verify_audit_chain.main(["--jsonl", str(log), "--profile", str(profiles[1])]) == 0
    broken = json.loads(lines[2])
    broken["actor_id"] = "mallory"
    log.write_text("\n".join(lines[:2] + [json.dumps(broken)] + lines[3:]))
    assert verify_audit_chain.main(["--jsonl", str(log)]) == 1


def test_usage_errors(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("REMEMBRANCE_DATABASE_URL", raising=False)
    assert verify_audit_chain.main([]) == 2
    assert verify_audit_chain.main(["--jsonl", str(tmp_path / "missing.jsonl")]) == 2
    assert verify_audit_chain.main(["--database-url", "postgresql+psycopg://nobody@127.0.0.1:1/none"]) == 2
    assert verify_audit_chain.main(["--bogus"]) == 2
    capsys.readouterr()


def test_verify_export_cli(api, client, tmp_path, capsys):
    grant = api.ready_grant()
    url = client.post(f"/export/{grant['deceased_profile_id']}", headers=OWNER.headers()).json()["url"]
    archive = tmp_path / "export.zip"
    archive.write_bytes(client.get(url.replace("http://testserver", "")).content)
    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps(client.get("/.well-known/consent-keys").json()))

    assert verify_export.main([str(archive), "--keys", str(keys)]) == 0
    assert "signature verified" in capsys.readouterr().out
    assert verify_export.main([str(archive), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["signature_checked"] is False

    corrupted = tmp_path / "corrupted.zip"
    data = bytearray(archive.read_bytes())
    corrupted.write_bytes(bytes(data[: len(data) // 2]))
    assert verify_export.main([str(corrupted)]) == 1
    assert "FAIL" in capsys.readouterr().out
    assert verify_export.main([str(tmp_path / "missing.zip")]) == 2
    assert verify_export.main([]) == 2
    capsys.readouterr()


def test_generate_signing_key(capsys):
    assert generate_signing_key.main(["--kid", "kernel-9"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "REMEMBRANCE_TOKEN_KEY_ID=kernel-9"
    from app.config import Settings
    from app.consent.tokens import KeyRing

    pem = json.loads(lines[1].split("=", 1)[1])
    ring = KeyRing.from_settings(Settings(env="test", token_signing_key_pem=pem, token_key_id="kernel-9"))
    assert list(ring.verification_keys) == ["kernel-9"]
    assert "kernel-9" in json.loads(lines[3][2:])
