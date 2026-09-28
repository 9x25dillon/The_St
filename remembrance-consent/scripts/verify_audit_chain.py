#!/usr/bin/env python3
"""Walk the audit ledger and report tampering.

    python scripts/verify_audit_chain.py                     # REMEMBRANCE_DATABASE_URL
    python scripts/verify_audit_chain.py --database-url URL --profile UUID
    python scripts/verify_audit_chain.py --jsonl export/audit_log.jsonl
    python scripts/verify_audit_chain.py --emit-anchors heads.json   # record heads
    python scripts/verify_audit_chain.py --anchors heads.json        # detect truncation

Checks every chain for a genesis link, unbroken previous_hash links,
recomputable event hashes and non-decreasing timestamps; with --anchors,
also that no chain lost events that an earlier run saw. Store anchor files
somewhere the database's operators can't write (a different account, a
WORM bucket, a printed report), or they prove nothing.

Exit status: 0 clean, 1 tampering detected, 2 usage or connection error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.consent.audit import EventRecord, iter_events, parse_anchors, verify_chain  # noqa: E402

CLEAN, TAMPERED, USAGE = 0, 1, 2


def _jsonl_events(path: Path) -> Iterator[EventRecord]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield EventRecord.from_json(json.loads(line))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--database-url", help="SQLAlchemy URL (default: REMEMBRANCE_DATABASE_URL)")
    source.add_argument("--jsonl", type=Path, help="verify an exported audit_log.jsonl instead of a database")
    parser.add_argument("--profile", type=UUID, help="verify only this profile's chain")
    parser.add_argument("--anchors", type=Path, help="JSON of previously recorded chain heads")
    parser.add_argument("--emit-anchors", type=Path, help="write current chain heads here after verifying")
    parser.add_argument("--json", action="store_true", help="machine-readable report")

    try:
        args = parser.parse_args(argv)
        anchors = parse_anchors(json.loads(args.anchors.read_text())) if args.anchors else None
        if args.jsonl:
            events = _jsonl_events(args.jsonl)
            if args.profile:
                events = (e for e in events if e.deceased_profile_id == args.profile)
            report = verify_chain(events, anchors)
        else:
            url = args.database_url or os.environ.get("REMEMBRANCE_DATABASE_URL")
            if not url:
                parser.error("pass --database-url or set REMEMBRANCE_DATABASE_URL")
            from app.db import make_engine, make_session_factory

            engine = make_engine(url)
            try:
                with make_session_factory(engine)() as session:
                    report = verify_chain(iter_events(session, args.profile), anchors)
            finally:
                engine.dispose()
    except SystemExit:
        return USAGE
    except Exception as exc:  # connection failures, unreadable files
        print(f"error: {exc}", file=sys.stderr)
        return USAGE

    if args.emit_anchors:
        args.emit_anchors.write_text(json.dumps(report.anchors(), indent=2, sort_keys=True) + "\n")
    if args.json:
        print(json.dumps(report.to_json(), indent=2))
    else:
        print(f"{report.event_count} events in {len(report.chains)} chains")
        for error in report.errors:
            print(f"TAMPERED  profile={error.deceased_profile_id} event={error.event_id} {error.kind.value}: {error.detail}")
        print("OK: audit chains intact" if report.ok else f"FAIL: {len(report.errors)} problem(s) found")
    return CLEAN if report.ok else TAMPERED


if __name__ == "__main__":
    sys.exit(main())
