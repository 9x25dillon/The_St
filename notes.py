"""Personal Markdown and plain-text notes, imported only from chosen files."""
from __future__ import annotations

import argparse
from pathlib import Path
import re

from mirror import Record

MAX_FILE_BYTES = 1_000_000
MAX_TOTAL_BYTES = 8_000_000
MAX_RECORDS = 5000
EXTENSIONS = {".md", ".markdown", ".txt"}


def parse_note(name: str, text: str) -> list[Record]:
    if Path(name).suffix.lower() not in EXTENSIONS:
        raise ValueError("Choose Markdown (.md) or plain-text (.txt) notes.")
    if len(text.encode("utf-8")) > MAX_FILE_BYTES:
        raise ValueError(f"{name}: exceeds the 1 MB per-file limit.")
    if "\x00" in text:
        raise ValueError(f"{name}: appears to be a binary file.")
    text = text.lstrip("\ufeff").replace("\r\n", "\n")
    # Exclude YAML metadata and fenced code from expressed prose.
    text = re.sub(r"\A---\n.*?\n---(?:\n|$)", "", text, flags=re.S)
    text = re.sub(r"^(`{3,}|~{3,})[^\n]*\n.*?^\1[^\n]*(?:\n|$)", "", text,
                  flags=re.S | re.M)
    records = []
    heading = Path(name).stem
    for paragraph in re.split(r"\n\s*\n", text):
        lines = []
        for line in paragraph.splitlines():
            if re.match(r"^#{1,6}\s", line):
                heading = re.sub(r"^#+\s*", "", line).strip()
            else:
                lines.append(line.strip())
        prose = " ".join(lines).strip()
        if not prose:
            continue
        # Keep chunks within the embedding model's useful input length.
        words = prose.split()
        for start in range(0, len(words), 120):
            records.append(Record(" ".join(words[start:start + 120]), "note",
                                  f"{name} · {heading}"))
    return records


def load(path: str) -> list[Record]:
    root = Path(path).expanduser()
    if not root.exists():
        raise ValueError(f"Notes path does not exist: {root}")
    files = [root] if root.is_file() else sorted(
        p for p in root.rglob("*")
        if p.suffix.lower() in EXTENSIONS and p.is_file()
        and not any(part.startswith(".") for part in p.relative_to(root).parts))
    records = []
    total = 0
    for path in files:
        if path.is_symlink() or (root.is_dir() and not path.resolve().is_relative_to(root.resolve())):
            continue
        size = path.stat().st_size
        total += size
        if size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            raise ValueError("Notes exceed the 1 MB per-file or 8 MB total limit.")
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeError as exc:
            raise ValueError(f"{path.name}: save this note as UTF-8 text.") from exc
        records.extend(parse_note(path.name if root.is_file() else str(path.relative_to(root)), text))
        if len(records) > MAX_RECORDS:
            raise ValueError("Choose fewer notes (maximum 5,000 passages).")
    if not records:
        raise ValueError("No usable note passages found. Choose .md or .txt files with prose.")
    return records


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="The Saint — personal notes")
    parser.add_argument("path", help="a note or folder of Markdown/plain-text notes")
    parser.add_argument("--out", help="explicitly save a semantic map as HTML")
    args = parser.parse_args()
    try:
        records = load(args.path)
        print(f"{len(records)} passages from {len({r.detail.split(' · ')[0] for r in records})} files")
        if args.out:
            if len(records) < 30:
                raise ValueError("At least 30 passages are needed for semantic clustering.")
            from mirror import embed, cluster, render
            render(records, *cluster(embed([r.text for r in records])), out=args.out)
    except (ValueError, OSError, ImportError) as exc:
        parser.exit(1, f"{exc}\n")
