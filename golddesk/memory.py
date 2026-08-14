"""Queryable memory over the ledger. DERIVED — never the source of truth.

WHY THE JSONL STAYS AUTHORITATIVE

The append-only files are the record. They are greppable during an incident,
they are tamper-evident by construction (the capture log is hash-chained), and a
partial write costs you one line rather than a corrupt page. Those properties
are worth more than query speed for a system whose entire purpose is producing
evidence somebody will later doubt.

So this is an INDEX. It is rebuilt from the JSONL, it is never written to by the
live desk, and if it ever disagrees with the ledger the ledger wins and this gets
deleted. `rebuild()` from scratch is the fix for every problem it can have, which
is only true because it holds no information the ledger does not.

WHAT IT IS FOR

The live path does not need it: nothing in the desk calls read_all(), the
service only appends. It earns its place the moment you start ASKING things of
months of forward evidence — "every LONDON swing-reversal that reached +1R and
gave it back", "capture rate by mechanism since the desk went live", "which
refusal reason precedes the largest forgone moves" — where a full-file JSON
parse per question stops being reasonable.

INCREMENTAL BY BYTE OFFSET

Re-parsing the whole file on every sync would defeat the point. Each source file
is tracked by size and by the offset already consumed, so a sync reads only what
was appended since. If a file SHRINKS it was rewritten — which an append-only
log must never do — and that file is reindexed from zero and flagged.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

log = logging.getLogger(__name__)

MEMORY_VERSION = "memory-2026-08-14-a"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT);

-- one row per ledger line. `raw` is the whole original object, so nothing is
-- lost to the extraction and a future question needs no re-ingest.
CREATE TABLE IF NOT EXISTS decisions (
    id            INTEGER PRIMARY KEY,
    source        TEXT NOT NULL,
    line_no       INTEGER NOT NULL,
    kind          TEXT,
    ts            TEXT,           -- t0 for decisions, ts for events
    symbol        TEXT,
    decided_by    TEXT,
    reason        TEXT,
    direction     TEXT,
    setup         TEXT,
    mechanism     TEXT,
    session       TEXT,
    trend         TEXT,
    volatility    TEXT,
    realised_r    REAL,
    gross_r       REAL,
    cost_r        REAL,
    mfe_r         REAL,
    mae_r         REAL,
    forgone_r     REAL,
    resolution    TEXT,
    best_achievable_r REAL,
    raw           TEXT NOT NULL,
    UNIQUE(source, line_no));

CREATE INDEX IF NOT EXISTS ix_kind    ON decisions(kind);
CREATE INDEX IF NOT EXISTS ix_ts      ON decisions(ts);
CREATE INDEX IF NOT EXISTS ix_mech    ON decisions(mechanism);
CREATE INDEX IF NOT EXISTS ix_session ON decisions(session);
CREATE INDEX IF NOT EXISTS ix_setup   ON decisions(setup);

-- what has already been consumed from each file, so a sync is incremental
CREATE TABLE IF NOT EXISTS sources (
    path       TEXT PRIMARY KEY,
    bytes_read INTEGER NOT NULL,
    lines_read INTEGER NOT NULL,
    last_sync  TEXT NOT NULL);
"""


def _extract(row: dict) -> dict:
    """Pull the columns worth indexing. Everything else stays in `raw`."""
    dec = row.get("decision") or {}
    ctx = row.get("context") or {}
    out = row.get("outcome") or {}
    return {
        "kind": row.get("kind"),
        "ts": row.get("t0") or row.get("ts") or row.get("entry_t0"),
        "symbol": row.get("symbol"),
        "decided_by": row.get("decided_by"),
        "reason": row.get("reason"),
        "direction": dec.get("direction") or dec.get("declined") or row.get("direction"),
        "setup": dec.get("setup") or row.get("setup"),
        "mechanism": (row.get("mechanism_name")
                      or (dec.get("analyst_read") or {}).get("mechanism_name")),
        "session": ctx.get("session") or (row.get("context") or {}).get("session"),
        "trend": ctx.get("trend_direction"),
        "volatility": ctx.get("volatility_state"),
        "realised_r": row.get("realised_r", dec.get("realised_r")),
        "gross_r": row.get("gross_r"),
        "cost_r": row.get("cost_r", dec.get("cost_r")),
        "mfe_r": row.get("mfe_r", out.get("mfe_r")),
        "mae_r": row.get("mae_r", out.get("mae_r")),
        "forgone_r": row.get("forgone_r"),
        "resolution": row.get("resolution"),
        "best_achievable_r": out.get("best_achievable_r"),
    }


class Memory:
    """SQLite index over one or more append-only ledgers."""

    def __init__(self, path: Path = Path("state/memory.db")):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT OR REPLACE INTO meta VALUES ('version', ?)",
                        (MEMORY_VERSION,))
        self.db.commit()

    # -- ingest -----------------------------------------------------------
    def sync(self, *paths: Path) -> dict:
        """Consume whatever is new in each file. Safe to run on a timer."""
        stats = {"files": 0, "new_rows": 0, "rewound": []}
        for p in paths:
            p = Path(p)
            if not p.exists():
                continue
            stats["files"] += 1
            cur = self.db.execute("SELECT bytes_read, lines_read FROM sources "
                                  "WHERE path = ?", (str(p),)).fetchone()
            offset, line_no = (cur["bytes_read"], cur["lines_read"]) if cur else (0, 0)
            size = p.stat().st_size
            if size < offset:
                # An append-only log that shrank was rewritten. That is a fact
                # worth surfacing rather than papering over, and the index for
                # that file is rebuilt because its earlier contents are suspect.
                log.error("%s SHRANK (%d -> %d bytes) — it was rewritten, which an "
                          "append-only ledger must never do. Reindexing from zero.",
                          p, offset, size)
                self.db.execute("DELETE FROM decisions WHERE source = ?", (str(p),))
                stats["rewound"].append(str(p))
                offset, line_no = 0, 0

            added = 0
            with p.open("rb") as fh:
                fh.seek(offset)
                for raw_line in fh:
                    text = raw_line.decode("utf-8", "replace").strip()
                    offset += len(raw_line)
                    if not text:
                        continue
                    line_no += 1
                    try:
                        row = json.loads(text)
                    except json.JSONDecodeError:
                        # A torn final line is normal if the writer is mid-append.
                        # Rewind so the complete line is picked up next sync.
                        offset -= len(raw_line)
                        line_no -= 1
                        break
                    f = _extract(row)
                    self.db.execute(
                        "INSERT OR IGNORE INTO decisions "
                        "(source,line_no,kind,ts,symbol,decided_by,reason,direction,"
                        " setup,mechanism,session,trend,volatility,realised_r,gross_r,"
                        " cost_r,mfe_r,mae_r,forgone_r,resolution,best_achievable_r,raw)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (str(p), line_no, f["kind"], f["ts"], f["symbol"],
                         f["decided_by"], f["reason"], f["direction"], f["setup"],
                         f["mechanism"], f["session"], f["trend"], f["volatility"],
                         f["realised_r"], f["gross_r"], f["cost_r"], f["mfe_r"],
                         f["mae_r"], f["forgone_r"], f["resolution"],
                         f["best_achievable_r"], text))
                    added += 1
            self.db.execute(
                "INSERT INTO sources VALUES (?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
                "bytes_read=excluded.bytes_read, lines_read=excluded.lines_read, "
                "last_sync=excluded.last_sync",
                (str(p), offset, line_no, datetime.now(timezone.utc).isoformat()))
            stats["new_rows"] += added
        self.db.commit()
        return stats

    def rebuild(self, *paths: Path) -> dict:
        """Throw the index away and re-derive it. The fix for everything.

        This is safe precisely because the index holds nothing the ledger does
        not. If that ever stops being true, this method stops being a fix and
        the design has gone wrong.
        """
        self.db.execute("DELETE FROM decisions")
        self.db.execute("DELETE FROM sources")
        self.db.commit()
        return self.sync(*paths)

    # -- questions --------------------------------------------------------
    def q(self, sql: str, *params) -> list[sqlite3.Row]:
        return self.db.execute(sql, params).fetchall()

    def summary(self) -> str:
        rows = self.q("SELECT kind, COUNT(*) n FROM decisions GROUP BY kind ORDER BY n DESC")
        src = self.q("SELECT path, lines_read, last_sync FROM sources")
        out = [f"MEMORY {self.path}  ({MEMORY_VERSION})",
               f"  indexed: {sum(r['n'] for r in rows):,} rows from {len(src)} file(s)"]
        out += [f"    {r['kind'] or '(none)':<22} {r['n']:>7,}" for r in rows]
        span = self.q("SELECT MIN(ts) a, MAX(ts) b FROM decisions WHERE ts IS NOT NULL")
        if span and span[0]["a"]:
            out.append(f"  span: {span[0]['a'][:19]} .. {span[0]['b'][:19]}")
        return "\n".join(out)

    def capture_by(self, column: str = "mechanism", min_n: int = 1) -> list[sqlite3.Row]:
        """Realised vs available R, grouped. The question this exists to answer.

        Column is whitelisted rather than interpolated freely — this is a local
        analysis tool, but a query builder that accepts arbitrary column names
        from a caller is a habit worth not forming.
        """
        allowed = {"mechanism", "session", "setup", "direction", "trend",
                   "volatility", "resolution"}
        if column not in allowed:
            raise ValueError(f"column must be one of {sorted(allowed)}")
        return self.q(
            f"SELECT {column} AS grp, COUNT(*) n, "
            f"       ROUND(SUM(realised_r),3) net_r, "
            f"       ROUND(AVG(realised_r),4) mean_r, "
            f"       ROUND(SUM(mfe_r),3) total_mfe, "
            f"       ROUND(SUM(forgone_r),3) forgone, "
            f"       SUM(CASE WHEN realised_r > 0 THEN 1 ELSE 0 END) wins "
            f"FROM decisions WHERE kind = 'TRADE_CLOSED' "
            f"GROUP BY {column} HAVING n >= ? ORDER BY n DESC", min_n)

    def refusal_costs(self, limit: int = 15) -> list[sqlite3.Row]:
        return self.q(
            "SELECT reason, COUNT(*) n, ROUND(SUM(best_achievable_r),1) total_mfe, "
            "       ROUND(AVG(best_achievable_r),3) mean_mfe "
            "FROM decisions WHERE kind LIKE 'REFUSAL%' AND best_achievable_r IS NOT NULL "
            "GROUP BY reason ORDER BY total_mfe DESC LIMIT ?", limit)

    def close(self) -> None:
        self.db.close()


if __name__ == "__main__":
    import glob
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    files = sys.argv[1:] or (glob.glob("state/*.jsonl") or glob.glob("backtest_out/*.jsonl"))
    m = Memory()
    print(m.sync(*[Path(f) for f in files]))
    print()
    print(m.summary())
    rows = m.capture_by("session")
    if rows:
        print("\ncapture by session")
        for r in rows:
            print(f"  {str(r['grp']):<10} n={r['n']:<4} net={r['net_r']:>+8.2f} "
                  f"mfe={r['total_mfe']:>+8.2f} forgone={r['forgone']:>+8.2f}")
