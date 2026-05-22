"""Project export for the admin UI: dispatch to the per-kind exporter, zip the
output, and hand it back. Single source of truth shared with the CLI scripts
(each exposes a `run()`); kinds covered: raster, vector, classification,
detection.

Two delivery modes:
  - export_zip(): synchronous, returns the zip bytes (used by the CLI/agents
    and small downloads).
  - create_job()/get_job()/job_artifact(): asynchronous — the zip is built off
    the request thread and written to disk so large datasets don't hold an HTTP
    request open or buffer the whole archive in memory.
"""
import tempfile
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import project_service
from .database import connect, transaction, now_iso, log_action

# API status param → the scripts' STATUS_FILTERS key.
_STATUS = {
    "reviewed": "reviewed",
    "classified": "classified",
    "reviewed_classified": "reviewed+classified",
}

# Where async-job archives live until downloaded.
EXPORTS_DIR = Path(tempfile.gettempdir()) / "tileclass_exports"


def _safe(name: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in (name or "proj"))


def _runner_for(kind: str):
    # Imported lazily so the (heavy) rasterio import only happens on a raster
    # export, not on every app import.
    from .scripts import (
        export_tiles, export_features, export_classifications, export_detections,
    )
    return {
        "vector": export_features.run,
        "classification": export_classifications.run,
        "detection": export_detections.run,
        "raster": export_tiles.run,
    }.get(kind, export_tiles.run)


def _build_zip(project_id: int, status_param: str, dest: Path) -> tuple[str, int]:
    """Run the project's exporter into a temp dir and stream the result into the
    zip at `dest` (written incrementally to disk — no full-archive buffer).
    Returns (filename, tile_count). Raises ValueError/LookupError on bad input."""
    if status_param not in _STATUS:
        raise ValueError("invalid_status")
    proj = project_service.get_project(project_id)
    if not proj:
        raise LookupError("project_not_found")
    runner = _runner_for(proj.get("kind", "raster"))
    with tempfile.TemporaryDirectory() as td:
        out = Path(td)
        count = runner(out, status=_STATUS[status_param], project_id=project_id)
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in sorted(out.rglob("*")):
                if p.is_file():
                    zf.write(p, p.relative_to(out))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    fname = f"{_safe(proj['name'])}_{status_param}_{stamp}.zip"
    return fname, count


def export_zip(project_id: int, status_param: str) -> tuple[bytes, str, int]:
    """Synchronous export — returns (zip_bytes, filename, tile_count). Builds to
    a temp file (low memory) then reads it back."""
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        fname, count = _build_zip(project_id, status_param, tmp_path)
        return tmp_path.read_bytes(), fname, count
    finally:
        tmp_path.unlink(missing_ok=True)


# ---- Async jobs ------------------------------------------------------------

def _job_row(conn, job_id):
    return conn.execute("SELECT * FROM export_jobs WHERE id=?", (job_id,)).fetchone()


def get_job(job_id: int) -> dict | None:
    conn = connect()
    try:
        row = _job_row(conn, job_id)
    finally:
        conn.close()
    return dict(row) if row else None


def create_job(project_id: int, status_param: str, by_user: int, *, run_async: bool = True) -> dict:
    """Validate, insert a pending job, and kick off the build (in a background
    thread by default). Returns the job row. `run_async=False` runs inline —
    used by tests for determinism. Raises ValueError/LookupError on bad input."""
    if status_param not in _STATUS:
        raise ValueError("invalid_status")
    if not project_service.get_project(project_id):
        raise LookupError("project_not_found")
    with transaction("IMMEDIATE") as conn:
        cur = conn.execute(
            "INSERT INTO export_jobs(project_id, status_param, state, created_by, created_at) "
            "VALUES (?,?,'pending',?,?)",
            (project_id, status_param, by_user, now_iso()),
        )
        job_id = cur.lastrowid
        log_action(conn, by_user, None, "export_job_create",
                   f"{project_id}:{status_param}:{job_id}")
    if run_async:
        threading.Thread(target=_run_job, args=(job_id,), daemon=True).start()
    else:
        _run_job(job_id)
    return get_job(job_id)


def _run_job(job_id: int) -> None:
    """Worker body: build the zip to disk and record the outcome. Never raises —
    failures are persisted on the job row so the poller can surface them."""
    conn = connect()
    try:
        row = _job_row(conn, job_id)
    finally:
        conn.close()
    if not row:
        return
    with transaction("IMMEDIATE") as conn:
        conn.execute("UPDATE export_jobs SET state='running' WHERE id=?", (job_id,))
    try:
        EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
        dest = EXPORTS_DIR / f"job_{job_id}.zip"
        fname, count = _build_zip(row["project_id"], row["status_param"], dest)
        with transaction("IMMEDIATE") as conn:
            conn.execute(
                "UPDATE export_jobs SET state='done', file_path=?, filename=?, "
                "tile_count=?, finished_at=? WHERE id=?",
                (str(dest), fname, count, now_iso(), job_id),
            )
    except Exception as e:  # noqa: BLE001 — persist any failure for the poller
        with transaction("IMMEDIATE") as conn:
            conn.execute(
                "UPDATE export_jobs SET state='error', error=?, finished_at=? WHERE id=?",
                (str(e)[:500], now_iso(), job_id),
            )


def job_artifact(job_id: int) -> tuple[str, str]:
    """Return (file_path, filename) for a finished job. Raises LookupError if
    the job/file is missing, ValueError('not_ready') if it isn't done yet."""
    job = get_job(job_id)
    if not job:
        raise LookupError("job_not_found")
    if job["state"] != "done" or not job["file_path"]:
        raise ValueError("not_ready")
    if not Path(job["file_path"]).exists():
        raise LookupError("artifact_missing")
    return job["file_path"], job["filename"] or f"export_{job_id}.zip"
