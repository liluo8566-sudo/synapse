"""One-shot backfill of marrow `sessions` from historical cc jsonl files.

Walks ~/.claude/projects/<slug>/*.jsonl, derives sid from filename, mtime as
last_active, extracts model from the first system/init event when present.
Channel defaults to 'cli' but never clobbers an existing 'wx' row (upsert
COALESCE keeps the channel + model already written by the bridge).

Run once after the B1 cli-half ships:
    /Users/Gabrielle/CC-Lab/synapse-wx/.venv/bin/python /Users/Gabrielle/CC-Lab/synapse-wx/scripts/backfill_sessions.py
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "marrow"))

from marrow import repo  # noqa: E402

PROJECTS = Path.home() / ".claude" / "projects"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _model_from_jsonl(p: Path) -> str | None:
    try:
        with p.open("r", encoding="utf-8") as f:
            for _ in range(20):
                line = f.readline()
                if not line:
                    break
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                m = ev.get("model") if isinstance(ev, dict) else None
                if isinstance(m, str) and m:
                    return m
                msg = ev.get("message") if isinstance(ev, dict) else None
                if isinstance(msg, dict):
                    m2 = msg.get("model")
                    if isinstance(m2, str) and m2:
                        return m2
    except OSError:
        return None
    return None


def main() -> int:
    if not PROJECTS.exists():
        print(f"no {PROJECTS} — nothing to backfill")
        return 0
    written = 0
    skipped = 0
    t0 = time.time()
    for slug_dir in PROJECTS.iterdir():
        if not slug_dir.is_dir():
            continue
        for jsonl in slug_dir.glob("*.jsonl"):
            sid = jsonl.stem
            if len(sid) < 8:
                skipped += 1
                continue
            mtime = jsonl.stat().st_mtime
            model = _model_from_jsonl(jsonl)
            try:
                repo.upsert_session(
                    sid,
                    model,
                    "cli",
                    last_active=_iso(mtime),
                )
                written += 1
            except Exception as e:  # noqa: BLE001
                print(f"  fail {sid}: {e}")
                skipped += 1
    dt = time.time() - t0
    print(f"backfilled {written} sessions, skipped {skipped}, {dt:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
