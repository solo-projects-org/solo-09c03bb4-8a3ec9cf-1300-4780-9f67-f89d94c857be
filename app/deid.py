"""De-identification primitives and package assembly.

Stable pseudonyms
-----------------
For each cohort, subjects receive sequential pseudonyms ``S000001``,
``S000002`` ... in order of first appearance. The mapping lives in the
``subjects`` table and is never exported, so later revisions reuse the same
pseudonym while the original subject id never leaves the system.

Stable, private date shifts
---------------------------
Each subject gets one fixed integer shift in [-180, 180] days derived from a
per-cohort random salt and the original subject id via HMAC-SHA256. Because the
salt never leaves the service:

* the same subject keeps the same shift across every revision (visit intervals
  are preserved exactly);
* different cohorts use independent salts, hence independent shifts;
* shifted dates cannot be correlated back to calendar dates without the salt.

The shift is materialised once in the ``subjects`` table and reused on every
subsequent revision.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import secrets
import zipfile
from datetime import date, timedelta
from pathlib import Path

from .csvutil import Row

SHIFT_RANGE = 361  # -180 .. 180 inclusive


def new_salt() -> str:
    return secrets.token_hex(32)


def subject_date_shift(salt: str, subject_id: str) -> int:
    digest = hmac.new(salt.encode("utf-8"), subject_id.encode("utf-8"), hashlib.sha256).digest()
    return int.from_bytes(digest[:8], "big") % SHIFT_RANGE - 180


def pseudonym(index: int) -> str:
    return f"S{index:06d}"


def build_package(
    rows: list[Row],
    identity: dict[str, tuple[str, int]],
) -> tuple[bytes, dict]:
    """Build the de-identified zip package in memory.

    ``identity`` maps original subject id -> (pseudonym, date_shift_days).
    Only pseudonyms, shifted dates and measurements are written; original
    identifiers and original dates are never serialised anywhere in the
    archive.
    """
    buffer = io.StringIO()
    buffer.write("pseudonym,visit_date_shifted,value\n")
    for row in rows:
        pseudo, shift = identity[row.subject_id]
        shifted = row.visit_date + timedelta(days=shift)
        buffer.write(f"{pseudo},{shifted.isoformat()},{_format_value(row.value)}\n")
    csv_bytes = buffer.getvalue().encode("utf-8")

    # Only structural metadata: no identifiers, no original dates, no salt.
    manifest = {
        "schema_version": 1,
        "columns": ["pseudonym", "visit_date_shifted", "value"],
        "row_count": len(rows),
        "subject_count": len(identity),
        "date_note": (
            "visit_date_shifted = original visit date + per-subject constant shift; "
            "within-subject visit intervals are unchanged"
        ),
        "privacy": "stable cohort-scoped pseudonyms; original subject ids and dates are not included",
    }
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("deidentified_visits.csv", csv_bytes)
        zf.writestr("manifest.json", manifest_bytes)
    return out.getvalue(), manifest


def _format_value(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return repr(value)


def write_package_atomic(final_path: Path, payload: bytes) -> None:
    """Write via a temp file + rename so a crashed build cannot leave a
    half-written package that later gets served."""
    final_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = final_path.with_suffix(final_path.suffix + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(payload)
        fh.flush()
    tmp.replace(final_path)
