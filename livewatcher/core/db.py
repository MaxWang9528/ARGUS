"""SQLite storage. FROZEN after Phase 0.

Calls are small and synchronous; they are safe from any thread. After changing a
clip, save it and announce it::

    db.save_clip(clip)
    bus.publish(Topic.CLIP, clip)
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from .models import Clip, ClipStatus, PeakEvent

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clips (
    id TEXT PRIMARY KEY,
    streamer TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS clips_status ON clips(status, created_at);
CREATE TABLE IF NOT EXISTS peaks (
    id TEXT PRIMARY KEY,
    streamer TEXT NOT NULL,
    ts_start REAL NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS peaks_streamer ON peaks(streamer, ts_start);
CREATE TABLE IF NOT EXISTS uploads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id TEXT NOT NULL,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    account TEXT NOT NULL DEFAULT 'default'
);
CREATE INDEX IF NOT EXISTS uploads_ts ON uploads(ts);
"""

DEFAULT_ACCOUNT = "default"  # what upload rows from before accounts existed count as


class Database:
    def __init__(self, path: str | Path = ":memory:") -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            if str(path) != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Bring an older database up to date. Called with the lock held."""
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(uploads)")}
        if "account" not in cols:  # before multiple TikTok accounts: every row was "default"
            self._conn.execute(
                f"ALTER TABLE uploads ADD COLUMN account TEXT NOT NULL DEFAULT '{DEFAULT_ACCOUNT}'")
        self._conn.execute("CREATE INDEX IF NOT EXISTS uploads_account_ts ON uploads(account, ts)")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- clips ---------------------------------------------------------------
    def save_clip(self, clip: Clip) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO clips(id, streamer, status, created_at, data) VALUES(?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET status=excluded.status, data=excluded.data",
                (clip.id, clip.streamer, clip.status.value, clip.created_at, clip.model_dump_json()),
            )
            self._conn.commit()

    def get_clip(self, clip_id: str) -> Clip | None:
        with self._lock:
            row = self._conn.execute("SELECT data FROM clips WHERE id=?", (clip_id,)).fetchone()
        return Clip.model_validate_json(row[0]) if row else None

    def list_clips(self, status: ClipStatus | str | list[str] | None = None,
                   streamer: str | None = None, limit: int = 200,
                   oldest_first: bool = False) -> list[Clip]:
        where, args = [], []
        if status is not None:
            statuses = [status] if isinstance(status, str) else list(status)
            where.append(f"status IN ({','.join('?' * len(statuses))})")
            args += [str(ClipStatus(s).value) for s in statuses]
        if streamer:
            where.append("streamer=?")
            args.append(streamer)
        sql = "SELECT data FROM clips"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY created_at {'ASC' if oldest_first else 'DESC'} LIMIT ?"
        with self._lock:
            rows = self._conn.execute(sql, (*args, limit)).fetchall()
        return [Clip.model_validate_json(r[0]) for r in rows]

    # ---- peaks ---------------------------------------------------------------
    def save_peak(self, peak: PeakEvent) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO peaks(id, streamer, ts_start, data) VALUES(?,?,?,?)",
                (peak.id, peak.streamer, peak.ts_start, peak.model_dump_json()),
            )
            self._conn.commit()

    def list_peaks(self, streamer: str | None = None, since: float = 0.0,
                   limit: int = 200) -> list[PeakEvent]:
        sql, args = "SELECT data FROM peaks WHERE ts_start>=?", [since]
        if streamer:
            sql += " AND streamer=?"
            args.append(streamer)
        sql += " ORDER BY ts_start DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(sql, (*args, limit)).fetchall()
        return [PeakEvent.model_validate_json(r[0]) for r in rows]

    # ---- upload history (used for rate limits) -------------------------------
    def add_upload(self, clip_id: str, kind: str, error: str = "", ts: float | None = None,
                   account: str = DEFAULT_ACCOUNT) -> None:
        """Record one upload attempt. ``kind`` is an UploadResult.kind; ``account`` the
        TikTok account it went to."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO uploads(clip_id, ts, kind, error, account) VALUES(?,?,?,?,?)",
                (clip_id, ts if ts is not None else time.time(), kind, error, account),
            )
            self._conn.commit()

    @staticmethod
    def _account_filter(account: str | None) -> tuple[str, tuple]:
        return ("", ()) if account is None else (" AND account=?", (account,))

    def count_uploads_since(self, since: float, kind: str = "ok", account: str | None = None) -> int:
        """Attempts of ``kind`` since ``since``; all accounts, or just ``account``."""
        extra, args = self._account_filter(account)
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM uploads WHERE ts>=? AND kind=?" + extra, (since, kind, *args)
            ).fetchone()
        return int(row[0])

    def upload_times_since(self, since: float, kind: str = "ok", account: str | None = None) -> list[float]:
        """Timestamps of attempts of ``kind`` since ``since``, oldest first."""
        extra, args = self._account_filter(account)
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts FROM uploads WHERE ts>=? AND kind=?" + extra + " ORDER BY ts",
                (since, kind, *args)).fetchall()
        return [r[0] for r in rows]

    def last_upload_ts(self, kind: str = "ok", account: str | None = None) -> float | None:
        extra, args = self._account_filter(account)
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(ts) FROM uploads WHERE kind=?" + extra, (kind, *args)).fetchone()
        return row[0]

    def last_upload_account(self, accounts: list[str] | None = None) -> str | None:
        """The account of the most recent attempt of any kind (dry runs too), optionally
        only among ``accounts``. None if there is none."""
        sql, args = "SELECT account FROM uploads", []
        if accounts is not None:
            if not accounts:
                return None
            sql += f" WHERE account IN ({','.join('?' * len(accounts))})"
            args = list(accounts)
        with self._lock:
            row = self._conn.execute(sql + " ORDER BY ts DESC, id DESC LIMIT 1", args).fetchone()
        return row[0] if row else None
