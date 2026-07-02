#!/usr/bin/env python3
"""Best-effort backfill of results/*/usage.json for runs done before the fix.

Matches opencode sessions to runs by wall-clock window (start/end in manifest.csv).
Parallel runs can overlap in time — attribution may be imperfect, but better than $0.
Only works when sessions landed in the global opencode DB (not isolated parallel runs).

Usage:
  python3 backfill_usage.py          # write missing usage.json files
  python3 aggregate.py               # re-aggregate
"""
import csv
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from aggregate import LIST_WRAPPER_KEYS, load_sessions

RESULTS_DIR = os.environ.get("RESULTS_DIR", "./results")
OPENCODE_DB = os.environ.get(
    "OPENCODE_DB",
    os.path.expanduser("~/.local/share/opencode/opencode.db"),
)
CCUSAGE = os.environ.get("CCUSAGE", "npx ccusage")
BUFFER_MS = int(os.environ.get("BACKFILL_BUFFER_MS", "5000"))


def session_ids_in_window(db_path, start_s, end_s):
    start_ms = int(float(start_s) * 1000) - BUFFER_MS
    end_ms = int(float(end_s) * 1000) + BUFFER_MS
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT id FROM session WHERE time_updated BETWEEN ? AND ?",
        (start_ms, end_ms),
    ).fetchall()
    return {r[0] for r in rows}


def ccusage_all():
    with tempfile.TemporaryDirectory() as fake:
        dest = Path(fake) / ".local/share/opencode"
        dest.mkdir(parents=True)
        for suffix in ("", "-wal", "-shm"):
            src = Path(OPENCODE_DB).with_name(Path(OPENCODE_DB).name + suffix)
            if src.exists():
                (dest / src.name).write_bytes(src.read_bytes())
        env = {**os.environ, "HOME": fake}
        out = subprocess.run(
            CCUSAGE.split() + ["opencode", "session", "--json"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
    if out.returncode != 0:
        return {}
    raw = json.loads(out.stdout or "{}")
    if isinstance(raw, dict):
        for k in LIST_WRAPPER_KEYS:
            if isinstance(raw.get(k), list):
                return load_sessions_from_rows(raw[k])
        return load_sessions_from_rows([raw])
    return load_sessions_from_rows(raw)


def load_sessions_from_rows(rows):
    """Return full ccusage session dicts keyed by sessionId."""
    out = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        sid = r.get("sessionId") or r.get("session") or r.get("id")
        if sid:
            out[str(sid)] = r
    return out


def main():
    manifest = Path(RESULTS_DIR) / "manifest.csv"
    if not manifest.exists():
        sys.exit(f"no manifest at {manifest}")
    if not Path(OPENCODE_DB).exists():
        sys.exit(f"no opencode db at {OPENCODE_DB}")

    print("loading ccusage session index...")
    all_sessions = ccusage_all()
    n_written = 0
    with manifest.open() as f:
        for row in csv.DictReader(f):
            outdir = Path(row["outdir"])
            usage = outdir / "usage.json"
            if usage.exists():
                continue
            ids = session_ids_in_window(OPENCODE_DB, row["start"], row["end"])
            matched = [all_sessions[sid] for sid in ids if sid in all_sessions]
            outdir.mkdir(parents=True, exist_ok=True)
            usage.write_text(json.dumps({"sessions": matched}, indent=2) + "\n")
            n_written += 1
            print(f"  {row['task']} | {row['model']} | run {row['run']}: {len(matched)} session(s)")
    print(f"wrote {n_written} usage.json file(s)")


if __name__ == "__main__":
    main()
