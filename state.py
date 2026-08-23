#!/usr/bin/env python3
"""
Per-item, per-stage pipeline state, in SQLite on DATA_DIR.

Replaces processed.json, which held one bit per video: done, or not done. To
stop a retry paying twice for audio it had already bought, every failure was
marked done, so a transient failure at a FREE stage retired the video for good.
That protected the wallet by losing the episode.

With a row per stage you can tell the two apart:

    a stage BEFORE the one-way door is free to retry
    a stage AFTER it must never re-enter it

The one-way door is narration. Everything before it costs nothing to redo;
everything after it is free to redo *given the narration master*, which is why
that master has been kept since 6e88106.

Why SQLite and not more JSON: honestly, it is close. Atomic writes landed in
961396a, so a torn file is no longer the argument it was. What SQLite buys is
an explicit schema, which is the artifact a later move to Postgres would need
and which nested dicts never produce, plus transactions so a killed process
leaves the last committed state rather than a partial one. It is a database
that is not a server: stdlib, one file, no credential, and the PVC that
Longhorn already snapshots is the whole backup story.

NOTE ON SCOPE: this module owns state only. It never fetches, pays, or writes
episode audio.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR") or HERE)
DB_PATH = DATA_DIR / "state.db"

# In pipeline order. The stage a resume restarts at is the first of these that
# is not `done`.
STAGES = ("transcript", "script", "narrate", "assemble", "publish")

# Everything from here on is free to redo. Before it, a retry costs nothing;
# at it, a retry costs money; after it, a retry is free again *if* the
# narration master survives, which is checked rather than assumed.
PAID_STAGE = "narrate"

# An item that cannot get past its first stage should not be retried for ever.
# Six matches the caption hold shipped in af922c7, which this generalises.
MAX_ATTEMPTS = 6
# ...and neither should one whose source quietly disappeared.
MAX_AGE_DAYS = 14

ACTIVE, PUBLISHED, ABANDONED = "active", "published", "abandoned"

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL DEFAULT 'youtube',
    source_url  TEXT,
    title       TEXT,
    channel     TEXT,
    status      TEXT NOT NULL DEFAULT 'active',
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    first_seen  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stages (
    item_id     TEXT NOT NULL,
    stage       TEXT NOT NULL,
    state       TEXT NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    detail      TEXT,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (item_id, stage),
    FOREIGN KEY (item_id) REFERENCES items(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS artifacts (
    item_id     TEXT NOT NULL,
    name        TEXT NOT NULL,
    path        TEXT,
    url         TEXT,
    bytes       INTEGER,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (item_id, name),
    FOREIGN KEY (item_id) REFERENCES items(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
"""


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def connect(path: Path | None = None) -> sqlite3.Connection:
    db = Path(path) if path else DB_PATH
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db, isolation_level=None)  # explicit transactions
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # A crash must leave the last committed state, which is the whole point.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    conn.executescript(SCHEMA)
    return conn


# ----------------------------- items -----------------------------

def upsert_item(conn, item_id: str, *, kind: str = "youtube", source_url: str = "",
                title: str = "", channel: str = "") -> None:
    """Record that this item exists. Never downgrades an existing row's status."""
    conn.execute(
        """INSERT INTO items (id, kind, source_url, title, channel, first_seen, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             title      = COALESCE(NULLIF(excluded.title, ''), items.title),
             channel    = COALESCE(NULLIF(excluded.channel, ''), items.channel),
             source_url = COALESCE(NULLIF(excluded.source_url, ''), items.source_url),
             updated_at = excluded.updated_at""",
        (item_id, kind, source_url, title, channel, now(), now()))


def get_item(conn, item_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()


def set_status(conn, item_id: str, status: str, error: str = "") -> None:
    conn.execute("UPDATE items SET status = ?, last_error = ?, updated_at = ? WHERE id = ?",
                 (status, error or None, now(), item_id))


def bump_attempt(conn, item_id: str, error: str = "") -> int:
    conn.execute("UPDATE items SET attempts = attempts + 1, last_error = ?, "
                 "updated_at = ? WHERE id = ?", (error or None, now(), item_id))
    row = get_item(conn, item_id)
    return row["attempts"] if row else 0


# ----------------------------- stages -----------------------------

def mark(conn, item_id: str, stage: str, state: str, detail: str = "") -> None:
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}; expected one of {STAGES}")
    conn.execute(
        """INSERT INTO stages (item_id, stage, state, attempts, detail, updated_at)
           VALUES (?, ?, ?, 1, ?, ?)
           ON CONFLICT(item_id, stage) DO UPDATE SET
             state      = excluded.state,
             attempts   = stages.attempts + 1,
             detail     = excluded.detail,
             updated_at = excluded.updated_at""",
        (item_id, stage, state, detail or None, now()))


def stage_states(conn, item_id: str) -> dict:
    rows = conn.execute("SELECT stage, state FROM stages WHERE item_id = ?", (item_id,))
    return {r["stage"]: r["state"] for r in rows}


def record_artifact(conn, item_id: str, name: str, path: str = "",
                    url: str = "", size: int | None = None) -> None:
    conn.execute(
        """INSERT INTO artifacts (item_id, name, path, url, bytes, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(item_id, name) DO UPDATE SET
             path       = COALESCE(NULLIF(excluded.path, ''), artifacts.path),
             url        = COALESCE(NULLIF(excluded.url, ''),  artifacts.url),
             bytes      = COALESCE(excluded.bytes, artifacts.bytes),
             updated_at = excluded.updated_at""",
        (item_id, name, path or None, url or None, size, now()))


def artifact(conn, item_id: str, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM artifacts WHERE item_id = ? AND name = ?",
                        (item_id, name)).fetchone()


def artifact_file(conn, item_id: str, name: str) -> Path | None:
    """The artifact's file, only if it is really on disk.

    A `done` stage whose artifact has gone missing is not done in any useful
    sense. 24 published episodes have no narration master, so trusting the
    record over the filesystem would produce rows that fail at assemble on
    every single run, for ever.
    """
    row = artifact(conn, item_id, name)
    if not row or not row["path"]:
        return None
    p = DATA_DIR / row["path"]
    return p if p.exists() and p.stat().st_size > 0 else None


# ----------------------------- resume -----------------------------

def resume_at(conn, item_id: str) -> str | None:
    """First stage that still needs doing, or None if the item is complete.

    A stage counts as done only when its state says so AND, for the stages that
    leave a file behind, that file is still there.

    A published or abandoned item is never resumable, whatever its artifacts
    look like. That distinction matters for the 24 episodes migrated in without
    a narration master, and for every episode published before transcripts were
    kept at all: they are finished, not stuck. Asking "can this be re-rendered?"
    is a different question, answered by artifact_file(id, "narration").
    """
    row = get_item(conn, item_id)
    if row and row["status"] in (PUBLISHED, ABANDONED):
        return None
    states = stage_states(conn, item_id)
    needs_file = {"transcript": "transcript", "script": "script",
                  "narrate": "narration", "assemble": "mp3"}
    for stage in STAGES:
        if states.get(stage) != "done":
            return stage
        want = needs_file.get(stage)
        if want and artifact_file(conn, item_id, want) is None:
            # Recorded as done, but the file is gone. Before the paid stage
            # that just means redo it. At or after the paid stage it means the
            # work cannot be resumed without paying again, so say so loudly
            # rather than silently re-buying it.
            return stage
    return None


def is_stale(conn, item_id: str, max_age_days: int = MAX_AGE_DAYS) -> bool:
    row = get_item(conn, item_id)
    if not row:
        return False
    try:
        first = dt.datetime.fromisoformat(row["first_seen"])
    except (TypeError, ValueError):
        return False
    return (dt.datetime.now() - first).days > max_age_days


def pending(conn) -> list:
    """Items still wanting work, oldest first."""
    rows = conn.execute("SELECT * FROM items WHERE status = ? ORDER BY first_seen",
                        (ACTIVE,)).fetchall()
    return list(rows)


def summary(conn) -> dict:
    out = {}
    for r in conn.execute("SELECT status, COUNT(*) n FROM items GROUP BY status"):
        out[r["status"]] = r["n"]
    return out


# ----------------------------- migration -----------------------------

class MigrationRefused(RuntimeError):
    """The old state could not be read. Running anyway would re-pay for everything."""


def _rel(path: Path) -> str:
    try:
        return str(Path(path).relative_to(DATA_DIR))
    except ValueError:
        return str(path)


def migrate_from_json(conn, manifest: Path, processed: Path, verbose: bool = True) -> dict:
    """Seed the database from episodes.json and processed.json. Idempotent.

    THIS IS THE DANGEROUS PART OF THE WHOLE CHANGE, and the danger is one
    specific shape: new code plus an empty database means every video in the
    playlist looks unprocessed, and the next run buys all of them again. That
    is the August 7 failure with a bigger bill. So:

      - a manifest that exists but will not parse raises rather than being
        treated as empty (this is the load_json default that made a torn file
        catastrophic, and it must not come back here)
      - anything already retired in processed.json is seeded as `abandoned`, so
        it is never picked up again
      - stages are marked done only where the artifact is actually on disk

    It writes nothing outside the database and never touches episode audio.
    """
    if conn.execute("SELECT COUNT(*) c FROM items").fetchone()["c"]:
        return {"skipped": "already migrated"}

    episodes = []
    if manifest.exists():
        try:
            episodes = json.loads(manifest.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise MigrationRefused(
                f"{manifest.name} exists but will not parse ({e}). Refusing to "
                f"migrate: an empty database here means paying for every episode "
                f"again. Fix or move the file, then re-run.")
        if not isinstance(episodes, list):
            raise MigrationRefused(f"{manifest.name} is not a list of episodes.")

    proc = {"ids": [], "waiting": {}}
    if processed.exists():
        try:
            proc = json.loads(processed.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise MigrationRefused(
                f"{processed.name} exists but will not parse ({e}). Refusing to "
                f"migrate: every id in it would look unprocessed and be re-paid.")

    counts = {"published": 0, "abandoned": 0, "waiting": 0, "artifacts_missing": 0}
    conn.execute("BEGIN")
    try:
        for ep in episodes:
            vid = ep.get("video_id")
            if not vid:
                continue
            upsert_item(conn, vid, kind="youtube", source_url=ep.get("source_url", ""),
                        title=ep.get("title", ""), channel=ep.get("channel", ""))
            files = {"script": ep.get("script_file"), "narration": ep.get("narration_file"),
                     "mp3": ep.get("mp3_file")}
            present = {}
            for name, rel in files.items():
                if not rel:
                    continue
                p = DATA_DIR / rel
                if p.exists() and p.stat().st_size > 0:
                    record_artifact(conn, vid, name, path=rel, size=p.stat().st_size)
                    present[name] = True
            for i, rel in enumerate(ep.get("clip_files") or [], start=1):
                p = DATA_DIR / rel
                if p.exists():
                    record_artifact(conn, vid, f"clip{i}", path=rel, size=p.stat().st_size)

            # It is in the manifest, so it was made and published. That is true
            # whether or not its intermediate files survived.
            for stage in STAGES:
                mark(conn, vid, stage, "done", detail="migrated")
            set_status(conn, vid, PUBLISHED)
            counts["published"] += 1
            if "narration" not in present:
                # Published, complete, and NOT resumable: no master to re-render
                # from. Recorded so nothing ever tries and fails on every run.
                counts["artifacts_missing"] += 1
                conn.execute("UPDATE items SET last_error = ? WHERE id = ?",
                             ("published without a narration master; not re-renderable", vid))

        known = {e.get("video_id") for e in episodes if e.get("video_id")}
        for vid in proc.get("ids", []):
            if vid in known:
                continue
            # Retired deliberately by the old code: either it failed after
            # spending credits, or it had no captions. Either way it must not
            # come back round.
            upsert_item(conn, vid)
            set_status(conn, vid, ABANDONED, "retired before migration")
            counts["abandoned"] += 1

        for vid, attempts in (proc.get("waiting") or {}).items():
            if vid in known:
                continue
            upsert_item(conn, vid)
            mark(conn, vid, "transcript", "failed", detail="waiting on captions")
            conn.execute("UPDATE items SET attempts = ? WHERE id = ?", (int(attempts), vid))
            counts["waiting"] += 1

        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    if verbose:
        print(f"  migrated: {counts['published']} published, "
              f"{counts['abandoned']} previously retired, "
              f"{counts['waiting']} waiting on captions"
              + (f", {counts['artifacts_missing']} without a narration master"
                 if counts["artifacts_missing"] else ""))
    return counts


def ensure_migrated(conn, manifest: Path, processed: Path, verbose: bool = True) -> None:
    """Migrate on first use. Refuses to leave an empty database beside old state.

    Called from the runner before anything else, so there is no window in which
    new code sees no state and concludes nothing has been done.
    """
    empty = conn.execute("SELECT COUNT(*) c FROM items").fetchone()["c"] == 0
    if not empty:
        return
    if not manifest.exists() and not processed.exists():
        return  # genuinely a fresh install
    migrate_from_json(conn, manifest, processed, verbose=verbose)


# ----------------------------- inspection -----------------------------

def main() -> None:
    import argparse
    p = argparse.ArgumentParser(description="Inspect The Gist's pipeline state")
    p.add_argument("--summary", action="store_true", help="Counts by status")
    p.add_argument("--pending", action="store_true", help="Items still wanting work")
    p.add_argument("--item", help="Everything known about one item")
    p.add_argument("--db", help="Database path (default: DATA_DIR/state.db)")
    p.add_argument("--migrate", action="store_true",
                   help="Seed from episodes.json/processed.json. With --db pointing "
                        "somewhere scratch and DATA_DIR mounted read-only, this is a "
                        "safe rehearsal against real data.")
    args = p.parse_args()

    conn = connect(Path(args.db) if args.db else None)
    if args.migrate:
        migrate_from_json(conn, DATA_DIR / "episodes.json", DATA_DIR / "processed.json")
        print(json.dumps(summary(conn), indent=2))
        return
    if args.item:
        row = get_item(conn, args.item)
        if not row:
            raise SystemExit(f"No such item: {args.item}")
        print(json.dumps(dict(row), indent=2))
        print("stages:   ", json.dumps(stage_states(conn, args.item), indent=2))
        arts = conn.execute("SELECT name, path, url, bytes FROM artifacts WHERE item_id = ?",
                            (args.item,)).fetchall()
        print("artifacts:", json.dumps([dict(a) for a in arts], indent=2))
        print("resume at:", resume_at(conn, args.item) or "complete")
        return
    if args.pending:
        for r in pending(conn):
            print(f"{r['id']}  attempts={r['attempts']}  "
                  f"resume={resume_at(conn, r['id'])}  {r['title'] or ''}"[:110])
        return
    print(json.dumps(summary(conn), indent=2))


if __name__ == "__main__":
    main()
