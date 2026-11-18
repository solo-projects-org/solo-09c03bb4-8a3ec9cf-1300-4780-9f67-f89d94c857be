"""FastAPI application: revision intake, status polling and package download."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .csvutil import CSVValidationError, parse_csv
from .database import get_conn, init_db, reset_interrupted_packages
from .services import ConflictError, accept_revision
from .worker import PackageWorker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("deid.api")

worker = PackageWorker()

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    recovered = reset_interrupted_packages()
    if recovered:
        log.info("startup recovery: %d interrupted package(s) back to pending", recovered)
    worker.start()
    try:
        yield
    finally:
        worker.stop()


app = FastAPI(
    title="随访数据去标识包服务",
    description="按队列受理 CSV 修订并发布只含稳定假名、平移日期与测值的去标识包。",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/health", include_in_schema=False)
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/cohorts/{cohort_id}/revisions/{revision_label}")
async def upload_revision(
    cohort_id: str,
    revision_label: str,
    file: UploadFile = File(..., description="完整修订 CSV (subject_id,visit_date,value)"),
) -> JSONResponse:
    if not config.is_valid_identifier(cohort_id):
        raise HTTPException(status_code=400, detail="队列 ID 只能包含字母、数字、_ - . 且长度 ≤128")
    if not config.is_valid_identifier(revision_label):
        raise HTTPException(status_code=400, detail="修订号只能包含字母、数字、_ - . 且长度 ≤128")

    content = await file.read(config.MAX_UPLOAD_BYTES + 1)
    if len(content) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="文件超过大小上限")
    if not content:
        raise HTTPException(status_code=400, detail="上传内容为空")

    try:
        rows = parse_csv(content)
    except CSVValidationError as exc:
        raise HTTPException(status_code=400, detail=f"CSV 校验失败: {exc}") from exc

    try:
        result = accept_revision(cohort_id, revision_label, rows, content)
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    status_code = 200 if result["status"] == "duplicate" else 202
    return JSONResponse(status_code=status_code, content=result)


@app.get("/api/cohorts")
def list_cohorts() -> dict:
    conn = get_conn()
    try:
        cohort_rows = conn.execute(
            "SELECT c.cohort_id, c.created_at, c.current_revision, "
            "(SELECT COUNT(*) FROM revisions r WHERE r.cohort_id = c.cohort_id) AS revision_count, "
            "(SELECT COUNT(*) FROM subjects s WHERE s.cohort_id = c.cohort_id) AS subject_count "
            "FROM cohorts c ORDER BY c.created_at"
        ).fetchall()
        result = []
        for c in cohort_rows:
            result.append(
                {
                    "cohort_id": c["cohort_id"],
                    "created_at": c["created_at"],
                    "current_revision": str(c["current_revision"])
                    if c["current_revision"] is not None
                    else None,
                    "revision_count": c["revision_count"],
                    "subject_count": c["subject_count"],
                    "current_package": _package_view(
                        conn, c["cohort_id"], c["current_revision"]
                    ),
                }
            )
        return {"cohorts": result}
    finally:
        conn.close()


@app.get("/api/cohorts/{cohort_id}")
def cohort_status(cohort_id: str) -> dict:
    conn = get_conn()
    try:
        cohort = conn.execute(
            "SELECT * FROM cohorts WHERE cohort_id = ?", (cohort_id,)
        ).fetchone()
        if cohort is None:
            raise HTTPException(status_code=404, detail="队列不存在")

        revisions = conn.execute(
            "SELECT revision, content_hash, row_count, created_at FROM revisions "
            "WHERE cohort_id = ? ORDER BY revision",
            (cohort_id,),
        ).fetchall()
        packages = conn.execute(
            "SELECT revision, status, attempts, last_error, next_retry_at, updated_at "
            "FROM packages WHERE cohort_id = ? ORDER BY revision",
            (cohort_id,),
        ).fetchall()
        return {
            "cohort_id": cohort_id,
            "current_revision": str(cohort["current_revision"])
            if cohort["current_revision"] is not None
            else None,
            "revisions": [
                {**dict(r), "revision": str(r["revision"])} for r in revisions
            ],
            "packages": [_package_status_dict(r) for r in packages],
            "current_package": _package_view(conn, cohort_id, cohort["current_revision"]),
        }
    finally:
        conn.close()


@app.post("/api/cohorts/{cohort_id}/retry")
def retry_current(cohort_id: str) -> dict:
    """Re-arm the current package for an immediate attempt (failure recovery)."""
    conn = get_conn()
    try:
        cohort = conn.execute(
            "SELECT current_revision FROM cohorts WHERE cohort_id = ?", (cohort_id,)
        ).fetchone()
        if cohort is None:
            raise HTTPException(status_code=404, detail="队列不存在")
        revision_label = cohort["current_revision"]
        pkg = conn.execute(
            "SELECT status FROM packages WHERE cohort_id = ? AND revision = ?",
            (cohort_id, revision_label),
        ).fetchone()
        if pkg is None:
            raise HTTPException(status_code=404, detail="当前修订没有待生成的包")
        if pkg["status"] == "published":
            return {"status": "published", "revision": str(revision_label)}
        if pkg["status"] != "failed":
            raise HTTPException(
                status_code=409,
                detail=f"当前包状态为 {pkg['status']}，无需重试",
            )
        conn.execute(
            "UPDATE packages SET status = 'pending', next_retry_at = NULL, "
            "updated_at = CURRENT_TIMESTAMP "
            "WHERE cohort_id = ? AND revision = ?",
            (cohort_id, revision_label),
        )
        return {"status": "pending", "revision": str(revision_label)}
    finally:
        conn.close()


@app.get("/api/cohorts/{cohort_id}/package")
def download_current_package(cohort_id: str):
    """Only the package of the current, published revision is downloadable.

    Every non-published state of the current revision (pending / generating /
    failed) returns 409 with an explanatory message. Superseded packages are
    never reachable here because ``cohorts.current_revision`` moves to the new
    revision atomically when one is accepted.
    """
    conn = get_conn()
    try:
        cohort = conn.execute(
            "SELECT current_revision FROM cohorts WHERE cohort_id = ?", (cohort_id,)
        ).fetchone()
        if cohort is None:
            raise HTTPException(status_code=404, detail="队列不存在")
        revision_label = cohort["current_revision"]
        pkg = conn.execute(
            "SELECT * FROM packages WHERE cohort_id = ? AND revision = ?",
            (cohort_id, revision_label),
        ).fetchone()
        if pkg is None:
            raise HTTPException(status_code=404, detail="当前修订尚无包记录")

        if pkg["status"] != "published":
            raise HTTPException(
                status_code=409,
                detail={
                    "pending": "包仍在待生成队列中，请稍后重试",
                    "generating": "包正在生成中，请稍后重试",
                    "failed": f"上次生成失败，等待自动重试: {pkg['last_error']}",
                    "superseded": "该包已被更新的修订取代",
                }.get(pkg["status"], f"当前状态 {pkg['status']} 不可下载"),
            )

        path = Path(pkg["package_path"])
        if not path.is_file():
            # File vanished underneath us: surface as to-be-generated, never
            # as a partial download, and re-arm generation immediately.
            conn.execute(
                "UPDATE packages SET status = 'pending', next_retry_at = NULL, "
                "updated_at = CURRENT_TIMESTAMP "
                "WHERE cohort_id = ? AND revision = ?",
                (cohort_id, revision_label),
            )
            raise HTTPException(status_code=409, detail="包文件缺失，已重新排队生成")
        return FileResponse(
            path,
            media_type="application/zip",
            filename=f"{cohort_id}_rev{revision_label}_deid.zip",
        )
    finally:
        conn.close()


def _package_view(conn, cohort_id: str, revision_label) -> dict | None:
    if revision_label is None:
        return None
    row = conn.execute(
        "SELECT revision, status, attempts, last_error, next_retry_at, updated_at "
        "FROM packages WHERE cohort_id = ? AND revision = ?",
        (cohort_id, revision_label),
    ).fetchone()
    return _package_status_dict(row) if row else None


def _package_status_dict(row) -> dict:
    return {
        "revision": str(row["revision"]),
        "status": row["status"],
        "attempts": row["attempts"],
        "last_error": row["last_error"],
        "next_retry_at": row["next_retry_at"],
        "updated_at": row["updated_at"],
        "download_available": row["status"] == "published",
    }


# Static assets (kept last; the explicit HTML route above serves "/").
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
