"""Private storage and a bounded, transactional job ledger (no Django database)."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import threading
import time

HEX = re.compile(r"[0-9a-f]{64}\Z")
MAX_PDF = 64 * 1024 * 1024
MAX_RESULT = 8 * 1024 * 1024
MAX_RESPONSE = MAX_RESULT + 65536
MAX_CLIENTS = 32


def private_directory(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Worker storage must be an absolute private path")
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("Worker storage cannot contain symbolic links")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
        raise ValueError("Worker directory must be owned by this user with mode 0700")
    return path


def read_private(path, limit):
    path = Path(path)
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Invalid private file")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise ValueError("Private file must be owned by this user with mode 0600")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Private file exceeds limit")
    return data


def clients_from_file(path):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate client namespace")
            value[key] = item
        return value
    value = json.loads(read_private(path, 65536), object_pairs_hook=unique)
    if not isinstance(value, dict) or not 1 <= len(value) <= MAX_CLIENTS:
        raise ValueError("Invalid worker clients")
    for name, token in value.items():
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
            raise ValueError("Invalid client namespace")
        if not isinstance(token, str) or not re.fullmatch(r"[\x21-\x7e]{32,512}", token):
            raise ValueError("Invalid client token")
    if len(set(value.values())) != len(value):
        raise ValueError("Client tokens must be distinct")
    return value


def signing_secret(root, explicit=None):
    path = Path(explicit) if explicit else root / "signing.secret"
    if not explicit and not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(secrets.token_hex(32).encode("ascii"))
            stream.flush()
            os.fsync(stream.fileno())
    secret = read_private(path, 4096).strip()
    if len(secret) < 32:
        raise ValueError("Worker signing secret is too short")
    return path


class QueueFull(Exception):
    pass


class RetryLimit(Exception):
    pass


class Ledger:
    def __init__(self, root, *, max_jobs=128, max_pending=32, per_client=4,
                 max_bytes=512 * 1024 * 1024, ttl=7200):
        self.root = private_directory(root)
        self.path = self.root / "jobs.sqlite3"
        self.max_jobs, self.max_pending, self.per_client = max_jobs, max_pending, per_client
        self.max_bytes, self.ttl = max_bytes, ttl
        self.lock = threading.RLock()
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        with self.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    namespace TEXT NOT NULL, key TEXT NOT NULL, contract TEXT NOT NULL,
                    before_hash TEXT NOT NULL, after_hash TEXT NOT NULL,
                    state TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
                    created REAL NOT NULL, updated REAL NOT NULL,
                    input BLOB, output BLOB, reserved INTEGER NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(namespace, key));
                CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state, created);
                CREATE TABLE IF NOT EXISTS clients (
                    namespace TEXT PRIMARY KEY, served INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS events (
                    namespace TEXT NOT NULL, key TEXT NOT NULL, at REAL NOT NULL,
                    state TEXT NOT NULL,
                    FOREIGN KEY(namespace, key) REFERENCES jobs(namespace, key) ON DELETE CASCADE);
            """)

    @contextmanager
    def transaction(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=5)
            db.row_factory = sqlite3.Row
            try:
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("PRAGMA journal_mode=DELETE")
                db.execute("PRAGMA synchronous=FULL")
                db.execute("BEGIN IMMEDIATE")
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    def _event(self, db, namespace, key, state):
        db.execute("INSERT INTO events VALUES (?, ?, ?, ?)", (namespace, key, time.time(), state))

    def recover(self):
        with self.transaction() as db:
            for row in db.execute("SELECT namespace,key FROM jobs WHERE state='running'").fetchall():
                self._event(db, row["namespace"], row["key"], "interrupted")
            db.execute("UPDATE jobs SET state='failed', error='interrupted', input=NULL, output=NULL, "
                       "reserved=0, updated=? WHERE state='running'", (time.time(),))

    def invalidate_runtime(self, contract, runtime):
        """Completed caches from another verified runtime are explicit retries."""
        with self.transaction() as db:
            rows = db.execute("SELECT namespace,key,output FROM jobs WHERE state='completed' AND contract=?",
                              (contract,)).fetchall()
            for row in rows:
                try:
                    previous = json.loads(row["output"])["runtime"]
                except (ValueError, TypeError, KeyError):
                    previous = None
                if previous == runtime:
                    continue
                db.execute("UPDATE jobs SET state='failed',error='configuration_changed',output=NULL,reserved=0,"
                           "attempts=0,updated=? WHERE namespace=? AND key=? AND state='completed'",
                           (time.time(), row["namespace"], row["key"]))
                self._event(db, row["namespace"], row["key"], "runtime_changed")

    def prune(self):
        with self.transaction() as db:
            db.execute("DELETE FROM jobs WHERE state != 'running' AND created < ?", (time.time() - self.ttl,))
            db.execute("DELETE FROM clients WHERE namespace NOT IN (SELECT namespace FROM jobs)")

    def expired(self, row, *, now=None):
        # In-flight work remains reconcilable until its bounded process terminates.
        return row["state"] != "running" and row["created"] < (time.time() if now is None else now) - self.ttl

    def get(self, namespace, key):
        with self.transaction() as db:
            row = db.execute("SELECT namespace,key,contract,before_hash,after_hash,state,error,output,created,updated,attempts "
                             "FROM jobs WHERE namespace=? AND key=?", (namespace, key)).fetchone()
            return dict(row) if row else None

    def submit(self, namespace, key, contract, hashes, body, *, retry_failed=False):
        with self.transaction() as db:
            old = db.execute("SELECT contract,before_hash,after_hash,state,attempts FROM jobs WHERE namespace=? AND key=?",
                             (namespace, key)).fetchone()
            if old:
                if list(old)[:3] != [contract, *hashes]:
                    raise ValueError("Job identity conflict")
                if not retry_failed or old["state"] != "failed":
                    return False
                if old["attempts"] >= 3:
                    raise RetryLimit()
            count, size = db.execute("SELECT COUNT(*),COALESCE(SUM(reserved),0) FROM jobs").fetchone()
            pending = db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0]
            own = db.execute("SELECT COUNT(*) FROM jobs WHERE namespace=? AND state IN ('queued','running')",
                             (namespace,)).fetchone()[0]
            reserved = len(body) + MAX_RESPONSE
            if count - bool(old) >= self.max_jobs or pending >= self.max_pending or own >= self.per_client or size + reserved > self.max_bytes:
                raise QueueFull()
            now = time.time()
            db.execute("INSERT OR IGNORE INTO clients(namespace) VALUES (?)", (namespace,))
            if old:
                db.execute("UPDATE jobs SET state='queued',error='',updated=?,input=?,output=NULL,reserved=?,attempts=attempts+1 "
                           "WHERE namespace=? AND key=?", (now, body, reserved, namespace, key))
            else:
                db.execute("INSERT INTO jobs(namespace,key,contract,before_hash,after_hash,state,created,updated,input,reserved) "
                           "VALUES (?,?,?,?,?,'queued',?,?,?,?)", (namespace, key, contract, *hashes, now, now, body, reserved))
            self._event(db, namespace, key, "retry_queued" if old else "queued")
            return True

    def claim(self):
        with self.transaction() as db:
            row = db.execute("SELECT j.* FROM jobs j JOIN clients c ON j.namespace=c.namespace "
                             "WHERE j.state='queued' AND j.created>=? "
                             "ORDER BY c.served,j.created,j.key LIMIT 1", (time.time() - self.ttl,)).fetchone()
            if row is None:
                return None
            sequence = db.execute("SELECT COALESCE(MAX(served),0)+1 FROM clients").fetchone()[0]
            db.execute("UPDATE clients SET served=? WHERE namespace=?", (sequence, row["namespace"]))
            db.execute("UPDATE jobs SET state='running',updated=? WHERE namespace=? AND key=?",
                       (time.time(), row["namespace"], row["key"]))
            self._event(db, row["namespace"], row["key"], "running")
            return dict(row)

    def finish(self, row, output=None, error="comparison_failed"):
        state = "completed" if output is not None else "failed"
        with self.transaction() as db:
            db.execute("UPDATE jobs SET state=?,error=?,input=NULL,output=?,reserved=?,updated=? "
                       "WHERE namespace=? AND key=? AND state='running'",
                       (state, "" if output is not None else error, output, len(output) if output else 0,
                        time.time(), row["namespace"], row["key"]))
            self._event(db, row["namespace"], row["key"], state)

    def busy(self, *, running_only=False):
        with self.transaction() as db:
            states = "state='running'" if running_only else "state IN ('queued','running')"
            return bool(db.execute(f"SELECT 1 FROM jobs WHERE {states} LIMIT 1").fetchone())


def namespace_path(root, namespace):
    return root / "namespaces" / hashlib.sha256(namespace.encode("utf-8")).hexdigest()
