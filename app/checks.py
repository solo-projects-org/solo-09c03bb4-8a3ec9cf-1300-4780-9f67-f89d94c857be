"""Dual-cohort overlap checks.

Before two cohorts that share one subject-numbering scheme are merged, the
research team needs to know whether the same subjects were enrolled twice.
A check intersects the *distinct original subject ids* of both cohorts'
current complete revisions and reports only how many subjects overlap.

Privacy contract
----------------
* The result is a single number. Overlaps smaller than
  ``MIN_REPORTABLE_OVERLAP`` are suppressed at the API boundary and reported
  only as "少于5" (small-cell suppression).
* Original subject ids and visit dates are read in memory for the count and
  are never stored in the check row, returned by the API, or shown on the
  page. Error text surfaced to the page is sanitised so it cannot carry
  cell-level content either.

Currency contract (mirrors the package lifecycle)
-------------------------------------------------
* One row per unordered cohort pair (normalised so ``cohort_a < cohort_b``):
  asking in either order yields the same single current conclusion.
* Accepting a new revision for either cohort re-arms the pair to ``pending``
  against the new current revisions inside the same transaction, so an old
  conclusion loses its currency the moment the revision is accepted.
* The background worker finalises with a conditional UPDATE: a late result
  whose revisions stopped being current mid-scan is discarded, never
  published. Scanning itself holds no lock, so revisions can still be
  accepted while a scan is running.
* Failures keep the to-be-checked state (``failed`` with backoff, retried);
  a restart returns interrupted/failed checks to ``pending``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .csvutil import CSVValidationError, parse_csv
from .database import get_conn, write_lock

# Overlaps smaller than this are never reported as an exact number.
MIN_REPORTABLE_OVERLAP = 5


class CohortNotReadyError(Exception):
    """The pair cannot be checked: unknown cohort or no accepted revision."""


class StaleCheckError(Exception):
    """The scan finished after its revisions stopped being current."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def normalize_pair(cohort_x: str, cohort_y: str) -> tuple[str, str]:
    """Unordered pair key: (A, B) and (B, A) map to the same row."""
    return (cohort_x, cohort_y) if cohort_x < cohort_y else (cohort_y, cohort_x)


def request_check(cohort_x: str, cohort_y: str) -> dict:
    """Arm an overlap check for the pair's current revisions.

    Idempotent: a check for the same pair at the same revisions that is
    already pending/checking/done is returned unchanged. A failed check at
    the same revisions is re-armed. Any revision change starts a fresh
    pending check, replacing whatever the row held.
    """
    cohort_a, cohort_b = normalize_pair(cohort_x, cohort_y)
    now = _utcnow()
    with write_lock:
        conn = get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rev_a = _current_revision(conn, cohort_a)
            rev_b = _current_revision(conn, cohort_b)
            row = conn.execute(
                "SELECT * FROM overlap_checks WHERE cohort_a = ? AND cohort_b = ?",
                (cohort_a, cohort_b),
            ).fetchone()
            if (
                row is not None
                and row["revision_a"] == rev_a
                and row["revision_b"] == rev_b
                and row["status"] in ("pending", "checking", "done")
            ):
                view = _view(conn, row)
                conn.execute("COMMIT")
                return {"outcome": "existing", "check": view}

            conn.execute(
                """
                INSERT INTO overlap_checks
                    (cohort_a, cohort_b, revision_a, revision_b, status,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?)
                ON CONFLICT(cohort_a, cohort_b) DO UPDATE SET
                    revision_a = excluded.revision_a,
                    revision_b = excluded.revision_b,
                    status = 'pending',
                    overlap_count = NULL,
                    last_error = NULL,
                    next_retry_at = NULL,
                    attempts = 0,
                    updated_at = excluded.updated_at
                """,
                (cohort_a, cohort_b, rev_a, rev_b, now, now),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
    return {"outcome": "armed", "check": get_check(cohort_a, cohort_b)}


def rearm_checks_for_cohort(conn, cohort_id: str, now: str) -> int:
    """Withdraw every conclusion involving ``cohort_id`` and re-arm the pair.

    Called inside :func:`services.accept_revision`'s transaction *after* the
    cohort's ``current_revision`` moved, so the re-armed check targets the
    new current revisions of both sides. Flipping the status out of
    ``checking`` also invalidates any in-flight scan for the old revisions:
    its late finalisation can no longer match the conditional UPDATE.
    """
    cur = conn.execute(
        """
        UPDATE overlap_checks
           SET status = 'pending',
               revision_a = (SELECT current_revision FROM cohorts
                              WHERE cohort_id = overlap_checks.cohort_a),
               revision_b = (SELECT current_revision FROM cohorts
                              WHERE cohort_id = overlap_checks.cohort_b),
               overlap_count = NULL,
               last_error = NULL,
               next_retry_at = NULL,
               attempts = 0,
               updated_at = ?
         WHERE cohort_a = ? OR cohort_b = ?
        """,
        (now, cohort_id, cohort_id),
    )
    return cur.rowcount


def claim_pending_check() -> tuple[str, str, object, object] | None:
    """Atomically claim one due check (``pending`` or a ``failed`` one whose
    backoff has elapsed)."""
    now = _utcnow()
    with write_lock:
        conn = get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT cohort_a, cohort_b, revision_a, revision_b
                  FROM overlap_checks
                 WHERE status = 'pending'
                    OR (status = 'failed' AND (next_retry_at IS NULL OR next_retry_at <= ?))
                 ORDER BY updated_at
                 LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            conn.execute(
                "UPDATE overlap_checks SET status = 'checking', "
                "attempts = attempts + 1, updated_at = ? "
                "WHERE cohort_a = ? AND cohort_b = ?",
                (now, row["cohort_a"], row["cohort_b"]),
            )
            conn.execute("COMMIT")
            return (row["cohort_a"], row["cohort_b"], row["revision_a"], row["revision_b"])
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


def finalize_check(cohort_a: str, cohort_b: str, revision_a, revision_b) -> None:
    """Count the overlap and publish the conclusion.

    The scan (reading and intersecting both revisions' subject ids) runs
    without holding the write lock, so revisions remain acceptable
    mid-scan. The final UPDATE only applies when the row is still
    ``checking`` for the exact revisions that were scanned and both are
    still the cohorts' current revisions; a late result is discarded and
    never becomes the current conclusion.
    """
    conn = get_conn()
    try:
        subjects_a = _revision_subjects(conn, cohort_a, revision_a)
        subjects_b = _revision_subjects(conn, cohort_b, revision_b)
        overlap = len(subjects_a & subjects_b)

        now = _utcnow()
        with write_lock:
            cur = conn.execute(
                """
                UPDATE overlap_checks
                   SET status = 'done',
                       overlap_count = ?,
                       last_error = NULL,
                       next_retry_at = NULL,
                       updated_at = ?
                 WHERE cohort_a = ? AND cohort_b = ?
                   AND status = 'checking'
                   AND revision_a = ? AND revision_b = ?
                   AND revision_a = (SELECT current_revision FROM cohorts
                                      WHERE cohort_id = ?)
                   AND revision_b = (SELECT current_revision FROM cohorts
                                      WHERE cohort_id = ?)
                """,
                (
                    overlap,
                    now,
                    cohort_a,
                    cohort_b,
                    revision_a,
                    revision_b,
                    cohort_a,
                    cohort_b,
                ),
            )
            if cur.rowcount == 0:
                raise StaleCheckError(
                    f"队列对 ({cohort_a}, {cohort_b}) 的迟到核查结果已丢弃 "
                    f"(扫描修订 rev{revision_a}/rev{revision_b} 已不现行)"
                )
    finally:
        conn.close()


def mark_check_failed(cohort_a: str, cohort_b: str, error: str) -> None:
    """Record a scan failure with capped exponential backoff.

    A failure stays visible as to-be-checked: the worker retries it, a
    restart retries it immediately, and it never produces a conclusion.
    The guard on ``checking`` makes a failure report for an already
    re-armed row a no-op.
    """
    delay = min(60, 2 ** min(_attempts(cohort_a, cohort_b), 6))
    next_retry = datetime.now(timezone.utc).timestamp() + delay
    next_retry_text = datetime.fromtimestamp(next_retry, timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    with write_lock:
        conn = get_conn()
        try:
            conn.execute(
                """
                UPDATE overlap_checks
                   SET status = 'failed',
                       last_error = ?,
                       next_retry_at = ?,
                       updated_at = ?
                 WHERE cohort_a = ? AND cohort_b = ? AND status = 'checking'
                """,
                (error[:1000], next_retry_text, _utcnow(), cohort_a, cohort_b),
            )
        finally:
            conn.close()


def get_check(cohort_x: str, cohort_y: str) -> dict | None:
    """Current view of one pair's check, or None if never requested."""
    cohort_a, cohort_b = normalize_pair(cohort_x, cohort_y)
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM overlap_checks WHERE cohort_a = ? AND cohort_b = ?",
            (cohort_a, cohort_b),
        ).fetchone()
        return _view(conn, row) if row is not None else None
    finally:
        conn.close()


def list_checks() -> list[dict]:
    """All known pairs, for the operations page (survives restarts)."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM overlap_checks ORDER BY created_at, cohort_a, cohort_b"
        ).fetchall()
        return [_view(conn, row) for row in rows]
    finally:
        conn.close()


def _current_revision(conn, cohort_id: str):
    row = conn.execute(
        "SELECT current_revision FROM cohorts WHERE cohort_id = ?", (cohort_id,)
    ).fetchone()
    if row is None:
        raise CohortNotReadyError(f"队列不存在: {cohort_id}")
    if row["current_revision"] is None:
        raise CohortNotReadyError(f"队列 {cohort_id} 尚无已受理修订，无法核查")
    return row["current_revision"]


def _revision_subjects(conn, cohort_id: str, revision) -> set[str]:
    """Distinct original subject ids of one accepted revision.

    The subjects table is append-only across revisions, so the current
    revision's subject set is re-derived from its stored raw CSV. Error
    text is sanitised: cell-level content (ids, dates) must never reach
    the API or the page via ``last_error``.
    """
    row = conn.execute(
        "SELECT raw_path FROM revisions WHERE cohort_id = ? AND revision = ?",
        (cohort_id, revision),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"修订记录不存在: {cohort_id} rev{revision}")
    try:
        raw = Path(row["raw_path"]).read_bytes()
        rows = parse_csv(raw)
    except CSVValidationError as exc:
        raise RuntimeError("已受理修订文件重新校验失败") from exc
    return {r.subject_id for r in rows}


def _view(conn, row) -> dict:
    """API/page view of a check row.

    A ``done`` conclusion is reported only while both pinned revisions are
    still the cohorts' current revisions; anything else carries no count.
    Exact counts below ``MIN_REPORTABLE_OVERLAP`` are suppressed to "少于5".
    """
    currents = {}
    for cohort_id in (row["cohort_a"], row["cohort_b"]):
        cohort = conn.execute(
            "SELECT current_revision FROM cohorts WHERE cohort_id = ?", (cohort_id,)
        ).fetchone()
        currents[cohort_id] = cohort["current_revision"] if cohort else None
    revisions_current = (
        row["revision_a"] == currents[row["cohort_a"]]
        and row["revision_b"] == currents[row["cohort_b"]]
    )
    is_current = row["status"] == "done" and revisions_current

    count = row["overlap_count"]
    suppressed = bool(is_current and count is not None and count < MIN_REPORTABLE_OVERLAP)
    if is_current and count is not None:
        overlap_display = "少于5" if suppressed else str(count)
        overlap_count = None if suppressed else count
    else:
        overlap_display = None
        overlap_count = None

    return {
        "cohort_a": row["cohort_a"],
        "cohort_b": row["cohort_b"],
        "revision_a": str(row["revision_a"]),
        "revision_b": str(row["revision_b"]),
        "status": row["status"],
        "current": is_current,
        "overlap_count": overlap_count,
        "overlap_display": overlap_display,
        "suppressed": suppressed,
        "attempts": row["attempts"],
        "last_error": row["last_error"],
        "next_retry_at": row["next_retry_at"],
        "updated_at": row["updated_at"],
    }


def _attempts(cohort_a: str, cohort_b: str) -> int:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT attempts FROM overlap_checks WHERE cohort_a = ? AND cohort_b = ?",
            (cohort_a, cohort_b),
        ).fetchone()
        return row["attempts"] if row else 0
    finally:
        conn.close()
