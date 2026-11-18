"""SQLite persistence layer: schema, connection helpers and startup recovery."""
from __future__ import annotations

import sqlite3
import threading

from . import config

# Serialises the small set of multi-statement write transactions
# (revision acceptance). SQLite itself also serialises writers, but the lock
# makes the read-modify-write sequences below explicit and easy to reason about.
write_lock = threading.RLock()


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(
        config.DB_PATH,
        timeout=30.0,
        isolation_level=None,  # autocommit; transactions are managed explicitly
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS cohorts (
    cohort_id        TEXT PRIMARY KEY,
    salt             TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    current_revision INTEGER
);

CREATE TABLE IF NOT EXISTS revisions (
    cohort_id        TEXT NOT NULL REFERENCES cohorts(cohort_id),
    revision         INTEGER NOT NULL,
    content_hash     TEXT NOT NULL,
    row_count        INTEGER NOT NULL,
    raw_path         TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    PRIMARY KEY (cohort_id, revision)
);

CREATE TABLE IF NOT EXISTS subjects (
    cohort_id        TEXT NOT NULL,
    subject_id       TEXT NOT NULL,
    pseudonym        TEXT NOT NULL,
    date_shift_days  INTEGER NOT NULL,
    PRIMARY KEY (cohort_id, subject_id),
    UNIQUE (cohort_id, pseudonym)
);

CREATE TABLE IF NOT EXISTS packages (
    cohort_id        TEXT NOT NULL,
    revision         INTEGER NOT NULL,
    status           TEXT NOT NULL,
    attempts         INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT,
    next_retry_at    TEXT,
    package_path     TEXT,
    manifest_path    TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (cohort_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_packages_claim
    ON packages(status, next_retry_at);

-- One row per unordered cohort pair (cohort_a < cohort_b): querying the same
-- pair in either order can only ever yield this single current conclusion.
-- revision_a/revision_b pin the revisions the conclusion belongs to; a done
-- conclusion is current only while both still equal the cohorts'
-- current_revision.
CREATE TABLE IF NOT EXISTS overlap_checks (
    cohort_a        TEXT NOT NULL,
    cohort_b        TEXT NOT NULL,
    revision_a      INTEGER NOT NULL,
    revision_b      INTEGER NOT NULL,
    status          TEXT NOT NULL,
    overlap_count   INTEGER,
    last_error      TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_retry_at   TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (cohort_a, cohort_b),
    CHECK (cohort_a < cohort_b)
);

CREATE INDEX IF NOT EXISTS idx_overlap_checks_claim
    ON overlap_checks(status, next_retry_at);
"""


def init_db() -> None:
    with write_lock:
        conn = get_conn()
        try:
            conn.executescript(SCHEMA)
        finally:
            conn.close()


def reset_interrupted_packages() -> None:
    """Startup recovery rule.

    A package left in ``generating`` means the process died mid-build: it must
    be visible as to-be-generated again, retried immediately (backoff cleared),
    and must never serve a half-written file. Failed packages keep their error
    notes but are also retried without delay after a restart.
    """
    with write_lock:
        conn = get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """
                UPDATE packages
                   SET status = 'pending',
                       next_retry_at = NULL,
                       attempts = 0,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE status IN ('generating', 'failed')
                """
            )
            conn.execute("COMMIT")
            return cur.rowcount
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


def reset_interrupted_checks() -> None:
    """Startup recovery rule for overlap checks (mirrors packages).

    A check left in ``checking`` means the process died mid-scan; failed
    checks keep their error notes. Both go back to ``pending`` so the pair
    stays visible as to-be-checked and is retried without delay.
    """
    with write_lock:
        conn = get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """
                UPDATE overlap_checks
                   SET status = 'pending',
                       next_retry_at = NULL,
                       attempts = 0,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE status IN ('checking', 'failed')
                """
            )
            conn.execute("COMMIT")
            return cur.rowcount
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
