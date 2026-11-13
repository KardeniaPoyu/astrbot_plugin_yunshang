"""Synchronous SQLite scheduling ledger, independent of AstrBot and networking.

Bootstrap once before claiming windows. The caller chooses only its most recent
closed window after a restart; this module never enumerates historical windows.
Polling a newer closed window retires older retrying preparations and expired
preparation leases as WindowSuperseded. Active preparations keep their tokens.
Existing delivery rows remain eligible for their bounded retries.

Each claim consumes one of three total attempts. Explicit failures use the
60/300/1200-second schedule; the third failure is terminal, so the final delay
does not authorize a fourth attempt. An abandoned claim is reclaimable when its
180-second lease expires. A new token fences every previous claimant. Finishes
have no clock argument: an expired, unreclaimed token can still acknowledge work,
but a replaced or released token cannot. Keep preparation <=120s and sends <=30s.

This is NOT exactly-once sending: a crash after a remote send and before its local
acknowledgement can produce a duplicate when the lease is retried.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading


# The requested schedule is fixed UTC+08:00 (modern Asia/Shanghai). A fixed
# stdlib timezone also works on Windows without an external tzdata package.
SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")
LEASE_SECONDS = 180
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (60, 300, 1200)
_WINDOW_STATES = ("preparing", "retry", "prepared", "completed", "failed")
_DELIVERY_STATES = ("pending", "sending", "retry", "sent", "failed", "blocked")


def _local(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("an aware datetime is required")
    return value.astimezone(SHANGHAI)


def completed_window(now: datetime, interval_hours: int = 4) -> tuple[datetime, datetime]:
    """Return the latest fully closed [start, end), anchored to local midnight."""
    if (type(interval_hours) is not int or interval_hours <= 0
            or 24 % interval_hours):
        raise ValueError("interval_hours must be a positive integer dividing 24")
    local = _local(now)
    end = local.replace(hour=(local.hour // interval_hours) * interval_hours,
                        minute=0, second=0, microsecond=0)
    return end - timedelta(hours=interval_hours), end


def _json_object(value: dict) -> str:
    if not isinstance(value, dict):
        raise TypeError("digest and payload must be dictionaries")
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _error_name(error_type) -> str:
    """Accept exception names (or exceptions), never store exception messages."""
    if isinstance(error_type, BaseException):
        error_type = type(error_type).__name__
    elif isinstance(error_type, type) and issubclass(error_type, BaseException):
        error_type = error_type.__name__
    if isinstance(error_type, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", error_type):
        return error_type
    return "Error"


class DeliveryStore:
    """Persistent, transactionally claimed preparation and per-target/page work.

    Window tickets contain window_key, start, end, token, attempts, lease_until.
    Delivery tickets contain id, target, page, payload, window_key, token,
    attempts, lease_until. Treat tickets as opaque acknowledgements; payload is
    the original JSON dictionary. Pages are nonnegative integers (0- or 1-based)
    and ordered within each (window, target), regardless of insertion order.

    finish_window freezes both the digest and all steps in one transaction;
    completed preparation is never repeated even if individual sends fail.
    Exhausted pages block later pages of that target, while other targets and
    windows can continue. cancel_delivery retires a removed target's claimed page
    and its later pages within that window, without sending or scheduling retries.
    close() is idempotent. Retention belongs to the caller.
    """

    def __init__(self, path):
        self._lock = threading.RLock()
        self._closed = False
        filename = os.fspath(path)
        if filename != ":memory:":
            file_path = Path(filename)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(filename, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            self._restrict_permissions(filename)
        self._db = sqlite3.connect(filename, timeout=5, isolation_level=None,
                                   check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA busy_timeout = 5000")
            self._db.execute("PRAGMA foreign_keys = ON")
            self._db.execute("PRAGMA journal_mode = WAL")
            self._db.execute("PRAGMA synchronous = FULL")
            with self._transaction() as db:
                db.execute("""CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
                db.execute("""CREATE TABLE IF NOT EXISTS windows (
                    window_key TEXT PRIMARY KEY,
                    start TEXT NOT NULL,
                    end_ts REAL NOT NULL,
                    state TEXT NOT NULL CHECK (state IN
                        ('preparing','retry','prepared','completed','failed')),
                    attempts INTEGER NOT NULL CHECK (attempts BETWEEN 1 AND 3),
                    token TEXT,
                    lease_until REAL,
                    next_attempt REAL NOT NULL DEFAULT 0,
                    digest TEXT,
                    error_type TEXT)""")
                db.execute("""CREATE TABLE IF NOT EXISTS deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    window_key TEXT NOT NULL REFERENCES windows(window_key),
                    target TEXT NOT NULL,
                    page INTEGER NOT NULL CHECK (page >= 0),
                    payload TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN
                        ('pending','sending','retry','sent','failed','blocked')),
                    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 3),
                    token TEXT,
                    lease_until REAL,
                    next_attempt REAL NOT NULL DEFAULT 0,
                    error_type TEXT,
                    UNIQUE(window_key, target, page))""")
                db.execute("""CREATE INDEX IF NOT EXISTS deliveries_ready
                    ON deliveries(state, next_attempt, lease_until)""")
                db.execute("CREATE INDEX IF NOT EXISTS windows_end ON windows(end_ts)")
            if filename != ":memory:":
                for suffix in ("-wal", "-shm"):
                    self._restrict_permissions(filename + suffix)
        except BaseException:
            self._db.close()
            self._closed = True
            raise

    @staticmethod
    def _restrict_permissions(filename):
        # Windows chmod cannot express POSIX ownership permissions; its ACLs
        # remain managed by the host. SQLite inherits DB modes for new sidecars.
        if os.name == "posix":
            try:
                os.chmod(filename, 0o600)
            except FileNotFoundError:
                pass

    @contextmanager
    def _transaction(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    def bootstrap(self, now: datetime, interval_hours: int = 4):
        """Persist the first closed-window end as an immutable no-history floor."""
        _, end = completed_window(now, interval_hours)
        with self._transaction() as db:
            db.execute("INSERT OR IGNORE INTO metadata(key, value) VALUES ('baseline', ?)",
                       (end.isoformat(),))

    def claim_window(self, start: datetime, end: datetime, now: datetime) -> dict | None:
        """Claim this window and retire superseded unfinished preparations.

        Retirement also runs when this window is already prepared or completed,
        so an older active preparation is retired on a later poll if it expires.
        Older prepared batches and their delivery retries are unaffected.
        """
        start, end, now = _local(start), _local(end), _local(now)
        if start >= end:
            raise ValueError("window start must precede end")
        key, start_key, stamp = end.isoformat(), start.isoformat(), now.timestamp()
        with self._transaction() as db:
            baseline = db.execute("SELECT value FROM metadata WHERE key = 'baseline'").fetchone()
            if baseline is None:
                raise RuntimeError("bootstrap must precede claim_window")
            if end <= datetime.fromisoformat(baseline[0]) or end > now:
                return None
            db.execute("""UPDATE windows SET state = 'failed', token = NULL,
                lease_until = NULL, next_attempt = 0, error_type = 'WindowSuperseded'
                WHERE end_ts < ? AND (state = 'retry'
                    OR (state = 'preparing' AND lease_until <= ?))""",
                       (end.timestamp(), stamp))
            row = db.execute("SELECT * FROM windows WHERE window_key = ?", (key,)).fetchone()
            if row is not None:
                if row["start"] != start_key:
                    raise ValueError("window end already exists with a different start")
                if row["state"] == "preparing":
                    if row["lease_until"] > stamp:
                        return None
                    if row["attempts"] >= MAX_ATTEMPTS:
                        db.execute("""UPDATE windows SET state = 'failed', token = NULL,
                            lease_until = NULL, error_type = 'LeaseExpired'
                            WHERE window_key = ?""", (key,))
                        return None
                elif row["state"] != "retry" or row["next_attempt"] > stamp:
                    return None
                attempts = row["attempts"] + 1
            else:
                attempts = 1
            token, lease = secrets.token_hex(16), stamp + LEASE_SECONDS
            if row is None:
                db.execute("""INSERT INTO windows
                    (window_key, start, end_ts, state, attempts, token, lease_until)
                    VALUES (?, ?, ?, 'preparing', ?, ?, ?)""",
                           (key, start_key, end.timestamp(), attempts, token, lease))
            else:
                db.execute("""UPDATE windows SET state = 'preparing', attempts = ?,
                    token = ?, lease_until = ?, next_attempt = 0,
                    error_type = CASE WHEN state = 'preparing' THEN 'LeaseExpired'
                                      ELSE error_type END WHERE window_key = ?""",
                           (attempts, token, lease, key))
            return {"window_key": key, "start": start_key, "end": key,
                    "token": token, "attempts": attempts, "lease_until": lease}

    @staticmethod
    def _owned(db, ticket, table, state):
        if not isinstance(ticket, dict) or not isinstance(ticket.get("token"), str):
            return None
        field = "window_key" if table == "windows" else "id"
        identity = ticket.get(field)
        if (table == "windows" and not isinstance(identity, str)
                or table == "deliveries" and type(identity) is not int):
            return None
        # Both identifiers come exclusively from the internal choices above.
        return db.execute(f"SELECT * FROM {table} WHERE {field} = ? AND token = ? AND state = ?",
                          (identity, ticket["token"], state)).fetchone()

    def finish_window(self, ticket: dict, digest: dict, deliveries: list[dict]) -> bool:
        with self._transaction() as db:
            row = self._owned(db, ticket, "windows", "preparing")
            if row is None:
                return False
            encoded_digest = _json_object(digest)
            if not isinstance(deliveries, list):
                raise TypeError("deliveries must be a list")
            encoded_steps, seen = [], set()
            for item in deliveries:
                if not isinstance(item, dict):
                    raise TypeError("each delivery must be a dictionary")
                target, page = item.get("target"), item.get("page")
                if not isinstance(target, str) or not target.strip():
                    raise ValueError("target must be a nonempty string")
                if type(page) is not int or not 0 <= page <= 9223372036854775807:
                    raise ValueError("page must be a nonnegative SQLite integer")
                if (target, page) in seen:
                    raise ValueError("duplicate target/page")
                seen.add((target, page))
                encoded_steps.append((row["window_key"], target, page,
                                      _json_object(item.get("payload"))))
            db.executemany("""INSERT INTO deliveries(window_key, target, page, payload)
                VALUES (?, ?, ?, ?)""", encoded_steps)
            db.execute("""UPDATE windows SET digest = ?, state = ?, token = NULL,
                lease_until = NULL, next_attempt = 0, error_type = NULL WHERE window_key = ?""",
                       (encoded_digest, "prepared" if encoded_steps else "completed", row["window_key"]))
            return True

    def fail_window(self, ticket: dict, now: datetime, error_type) -> bool:
        stamp = _local(now).timestamp()
        with self._transaction() as db:
            row = self._owned(db, ticket, "windows", "preparing")
            if row is None:
                return False
            db.execute("""UPDATE windows SET state = ?, token = NULL, lease_until = NULL,
                next_attempt = ?, error_type = ? WHERE window_key = ?""",
                       ("failed" if row["attempts"] >= MAX_ATTEMPTS else "retry",
                        stamp + BACKOFF_SECONDS[row["attempts"] - 1], _error_name(error_type),
                        row["window_key"]))
            return True

    @staticmethod
    def _settle_window(db, key):
        if db.execute("""SELECT 1 FROM deliveries WHERE window_key = ?
                         AND state IN ('failed', 'blocked') LIMIT 1""", (key,)).fetchone():
            db.execute("UPDATE windows SET state = 'failed' WHERE window_key = ?", (key,))
        elif not db.execute("""SELECT 1 FROM deliveries WHERE window_key = ?
                               AND state != 'sent' LIMIT 1""", (key,)).fetchone():
            db.execute("UPDATE windows SET state = 'completed' WHERE window_key = ?", (key,))

    @classmethod
    def _block_later_pages(cls, db, row):
        db.execute("""UPDATE deliveries SET state = 'blocked', token = NULL,
            lease_until = NULL, error_type = 'PreviousPageFailed'
            WHERE window_key = ? AND target = ? AND page > ?
              AND state IN ('pending', 'retry')""",
                   (row["window_key"], row["target"], row["page"]))
        cls._settle_window(db, row["window_key"])

    def claim_delivery(self, now: datetime) -> dict | None:
        """Claim one eligible page atomically, including bounded old retries."""
        stamp = _local(now).timestamp()
        with self._transaction() as db:
            exhausted = db.execute("""SELECT * FROM deliveries WHERE state = 'sending'
                AND lease_until <= ? AND attempts >= ?""", (stamp, MAX_ATTEMPTS)).fetchall()
            for row in exhausted:
                db.execute("""UPDATE deliveries SET state = 'failed', token = NULL,
                    lease_until = NULL, error_type = 'LeaseExpired' WHERE id = ?""", (row["id"],))
                self._block_later_pages(db, row)
            row = db.execute("""SELECT d.* FROM deliveries AS d
                JOIN windows AS w ON w.window_key = d.window_key
                WHERE d.attempts < ? AND (
                    (d.state IN ('pending', 'retry') AND d.next_attempt <= ?)
                    OR (d.state = 'sending' AND d.lease_until <= ?))
                AND NOT EXISTS (
                    SELECT 1 FROM deliveries AS prior
                    WHERE prior.window_key = d.window_key AND prior.target = d.target
                      AND prior.page < d.page AND prior.state != 'sent')
                ORDER BY w.end_ts, d.page, d.id LIMIT 1""",
                             (MAX_ATTEMPTS, stamp, stamp)).fetchone()
            if row is None:
                return None
            token, lease = secrets.token_hex(16), stamp + LEASE_SECONDS
            attempts = row["attempts"] + 1
            db.execute("""UPDATE deliveries SET state = 'sending', attempts = ?, token = ?,
                lease_until = ?, next_attempt = 0,
                error_type = CASE WHEN state = 'sending' THEN 'LeaseExpired'
                                  ELSE error_type END WHERE id = ?""",
                       (attempts, token, lease, row["id"]))
            return {"id": row["id"], "target": row["target"], "page": row["page"],
                    "payload": json.loads(row["payload"]), "window_key": row["window_key"],
                    "token": token, "attempts": attempts, "lease_until": lease}

    def finish_delivery(self, ticket: dict) -> bool:
        """Acknowledge one current token; successful pages can never be reclaimed."""
        with self._transaction() as db:
            row = self._owned(db, ticket, "deliveries", "sending")
            if row is None:
                return False
            db.execute("""UPDATE deliveries SET state = 'sent', token = NULL,
                lease_until = NULL, next_attempt = 0, error_type = NULL WHERE id = ?""", (row["id"],))
            self._settle_window(db, row["window_key"])
            return True

    def cancel_delivery(self, ticket: dict) -> bool:
        """Retire a current claim and its later pages with TargetRemoved.

        Call before sending when a saved target is no longer configured. In one
        transaction the claimed row becomes failed and its later unsent pages
        become blocked; tokens and retry eligibility are cleared. Sent pages,
        other targets, and other windows are untouched. Return False for a stale,
        released, already finished, or already cancelled ticket. This performs
        no network operation and cannot recall an already-dispatched message.
        """
        with self._transaction() as db:
            row = self._owned(db, ticket, "deliveries", "sending")
            if row is None:
                return False
            db.execute("""UPDATE deliveries
                SET state = CASE WHEN id = ? THEN 'failed' ELSE 'blocked' END,
                    token = NULL, lease_until = NULL, next_attempt = 0,
                    error_type = 'TargetRemoved'
                WHERE window_key = ? AND target = ? AND page >= ?
                  AND state IN ('pending', 'sending', 'retry')""",
                       (row["id"], row["window_key"], row["target"], row["page"]))
            self._settle_window(db, row["window_key"])
            return True

    def fail_delivery(self, ticket: dict, now: datetime, error_type) -> bool:
        stamp = _local(now).timestamp()
        with self._transaction() as db:
            row = self._owned(db, ticket, "deliveries", "sending")
            if row is None:
                return False
            exhausted = row["attempts"] >= MAX_ATTEMPTS
            db.execute("""UPDATE deliveries SET state = ?, token = NULL, lease_until = NULL,
                next_attempt = ?, error_type = ? WHERE id = ?""",
                       ("failed" if exhausted else "retry",
                        stamp + BACKOFF_SECONDS[row["attempts"] - 1], _error_name(error_type), row["id"]))
            if exhausted:
                self._block_later_pages(db, row)
            return True

    def status(self) -> dict:
        """JSON-safe snapshot: baseline/latest ISO ends, state counts, error types.

        No digest, payload, target, database path, claim token, or exception
        message is included. Errors are counts of outstanding last error types,
        not an append-only history; a successful retry clears its last error.
        """
        with self._transaction() as db:
            baseline = db.execute("SELECT value FROM metadata WHERE key = 'baseline'").fetchone()
            latest = db.execute("SELECT window_key FROM windows ORDER BY end_ts DESC LIMIT 1").fetchone()
            counts = {}
            for table, states in (("windows", _WINDOW_STATES), ("deliveries", _DELIVERY_STATES)):
                counts[table] = dict.fromkeys(states, 0)
                for row in db.execute(f"SELECT state, count(*) AS n FROM {table} GROUP BY state"):
                    counts[table][row["state"]] = row["n"]
            errors = dict(db.execute("""SELECT error_type, count(*) FROM (
                SELECT error_type FROM windows WHERE error_type IS NOT NULL
                UNION ALL SELECT error_type FROM deliveries WHERE error_type IS NOT NULL)
                GROUP BY error_type ORDER BY error_type"""))
            return {"baseline": baseline[0] if baseline else None,
                    "latest_window": latest[0] if latest else None,
                    "counts": counts, "errors": errors}

    def close(self):
        with self._lock:
            if not self._closed:
                self._db.close()
                self._closed = True
