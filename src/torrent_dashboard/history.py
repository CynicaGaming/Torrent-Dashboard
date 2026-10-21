"""SQLite history with serialized transactions and deterministic connection cleanup."""

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import threading
import time

from .audit import AuditStore

_AUDIT_PREFIXES = (
    "login_", "recovery_login_", "setup_", "account_", "user_", "settings_",
    "client_settings_", "integration_", "jellyfin_", "backup_", "update_",
    "notification_sound_",
)


class HistoryStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.last_sample = {}
        self.last_seen = {}
        self._last_cleanup = 0.0
        self.audit = AuditStore(self.path.with_name("security-audit.sqlite3"))
        self.initialize()

    def initialize(self):
        """Create missing schema after startup or a validated state restore."""
        with self._db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS snapshots(
                ts INTEGER NOT NULL, server_id TEXT NOT NULL, dl INTEGER, up INTEGER,
                active INTEGER, total INTEGER, remaining INTEGER, disk_free INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_snapshots ON snapshots(server_id, ts);
            CREATE TABLE IF NOT EXISTS torrent_history(
                server_id TEXT NOT NULL, hash TEXT NOT NULL, name TEXT, category TEXT,
                added_on INTEGER, completion_on INTEGER, downloaded INTEGER, uploaded INTEGER,
                ratio REAL, last_seen INTEGER, PRIMARY KEY(server_id, hash)
            );
            CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, server_id TEXT,
                hash TEXT, name TEXT, event TEXT, data TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_events ON events(ts);
            """)

    @contextmanager
    def _db(self):
        # The SQLite connection context commits/rolls back but does not close.
        # Hold the history lock until the connection is closed, including reads.
        with self.lock:
            db = sqlite3.connect(self.path, timeout=10)
            try:
                db.row_factory = sqlite3.Row
                with db:
                    yield db
            finally:
                db.close()

    def reset_tracking(self):
        with self.lock:
            self.last_sample.clear()
            self.last_seen.clear()
            self._last_cleanup = 0.0

    def sample(self, server_id, torrents, transfer, disk_free, every):
        """Record a polling pass.

        Completion detection runs on every pass so "completed" events stay
        immediate, but the per-torrent history upsert and the snapshot row are
        only written once per sample interval (or when a torrent completes).
        The collector polls every second, so writing every torrent on every
        pass previously meant hundreds of SQLite writes per second.
        """
        now = int(time.time())
        with self.lock:
            due = now - self.last_sample.get(server_id, 0) >= every
            completed_events = []
            changed = []
            for t in torrents:
                h = t.get("hash")
                if not h:
                    continue
                prev = self.last_seen.get((server_id, h))
                completed = float(t.get("progress", 0) or 0) >= 0.999999
                if prev is not None and not prev and completed:
                    completed_events.append((now, server_id, h, t.get("name", ""), "completed", "{}"))
                if due or prev is None or prev != completed:
                    changed.append(t)
                self.last_seen[(server_id, h)] = completed
            if not due and not changed and not completed_events:
                return
            with self._db() as db:
                if due:
                    active = sum(1 for t in torrents if float(t.get("progress", 0) or 0) < 1 and "paused" not in str(t.get("state", "")).lower() and "stopped" not in str(t.get("state", "")).lower())
                    remaining = sum(int(t.get("amount_left", 0) or 0) for t in torrents)
                    db.execute("INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?)", (
                        now, server_id, int(transfer.get("dl_info_speed", 0) or 0), int(transfer.get("up_info_speed", 0) or 0),
                        active, len(torrents), remaining, disk_free
                    ))
                if completed_events:
                    db.executemany("INSERT INTO events(ts,server_id,hash,name,event,data) VALUES(?,?,?,?,?,?)", completed_events)
                if changed:
                    db.executemany("""INSERT INTO torrent_history(server_id,hash,name,category,added_on,completion_on,downloaded,uploaded,ratio,last_seen)
                        VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(server_id,hash) DO UPDATE SET name=excluded.name,category=excluded.category,
                        completion_on=excluded.completion_on,downloaded=excluded.downloaded,uploaded=excluded.uploaded,ratio=excluded.ratio,last_seen=excluded.last_seen""",
                        [(server_id,t.get("hash"),t.get("name",""),t.get("category",""),int(t.get("added_on",0) or 0),int(t.get("completion_on",0) or 0),int(t.get("downloaded",0) or 0),int(t.get("uploaded",0) or 0),float(t.get("ratio",0) or 0),now) for t in changed])
            if due:
                self.last_sample[server_id] = now

    def maybe_cleanup(self, days, interval=600):
        """Run retention cleanup at most once per ``interval`` seconds."""
        now = time.time()
        with self.lock:
            if now - getattr(self, "_last_cleanup", 0.0) < interval:
                return None
            self._last_cleanup = now
        return self.cleanup(days)

    def event(self, server_id, event, name="", hash_="", data=None):
        payload = data or {}
        with self._db() as db:
            db.execute("INSERT INTO events(ts,server_id,hash,name,event,data) VALUES(?,?,?,?,?,?)",
                       (int(time.time()), server_id, hash_, name, event, json.dumps(payload)))
        if str(event or "").startswith(_AUDIT_PREFIXES):
            outcome = "failure" if str(event).endswith("_failed") else "denied" if "denied" in str(event) else "success"
            details = dict(payload) if isinstance(payload, dict) else {"value": payload}
            client_ip = str(details.pop("client_ip", "") or "")
            try:
                self.audit.record(
                    str(event), actor=str(name or ""), outcome=outcome, client_ip=client_ip,
                    target=str(hash_ or server_id or ""), details=details,
                )
            except Exception:
                # Audit failures must not break the user action already recorded in history.
                pass

    def cleanup(self, days):
        cutoff = int(time.time()) - max(1, int(days)) * 86400
        with self._db() as db:
            snapshots = db.execute("DELETE FROM snapshots WHERE ts < ?", (cutoff,)).rowcount
            events = db.execute("DELETE FROM events WHERE ts < ?", (cutoff,)).rowcount
            torrents = db.execute("DELETE FROM torrent_history WHERE last_seen < ?", (cutoff,)).rowcount
        return {
            "snapshots": max(0, int(snapshots or 0)),
            "events": max(0, int(events or 0)),
            "torrents": max(0, int(torrents or 0)),
        }

    def history(self, server_id, minutes):
        cutoff = int(time.time()) - max(1, min(int(minutes), 43200)) * 60
        with self._db() as db:
            rows = db.execute("SELECT * FROM snapshots WHERE (?='all' OR server_id=?) AND ts>=? ORDER BY ts", (server_id, server_id, cutoff)).fetchall()
            return [dict(r) for r in rows]

    def events(self, limit=100):
        with self._db() as db:
            rows = db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (max(1,min(int(limit),500)),)).fetchall()
            return [dict(r) for r in rows]

    def analytics(self, server_id):
        now = int(time.time())
        with self._db() as db:
            rows = db.execute("SELECT * FROM snapshots WHERE (?='all' OR server_id=?) AND ts>=? ORDER BY ts", (server_id,server_id,now-7*86400)).fetchall()
            hist = db.execute("SELECT * FROM torrent_history WHERE (?='all' OR server_id=?)", (server_id,server_id)).fetchall()
        avg_dl = int(sum(r["dl"] or 0 for r in rows)/len(rows)) if rows else 0
        peak_dl = max((r["dl"] or 0 for r in rows), default=0)
        completed = sum(1 for r in hist if r["completion_on"] and r["completion_on"] > 0)
        avg_ratio = sum(float(r["ratio"] or 0) for r in hist)/len(hist) if hist else 0
        return {"avg_dl_7d":avg_dl,"peak_dl_7d":peak_dl,"known_torrents":len(hist),"completed":completed,"avg_ratio":avg_ratio}



__all__ = ["HistoryStore"]
