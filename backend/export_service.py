"""Project export for the admin UI: dispatch to the per-kind exporter, zip the
output, and hand it back. Single source of truth shared with the CLI scripts
(each exposes a `run()`); kinds covered: raster, classification.

Two delivery modes:
  - export_zip(): synchronous, returns the zip bytes (used by the CLI/agents
    and small downloads).
  - create_job()/get_job()/job_artifact(): asynchronous — the zip is built off
    the request thread and written to disk so large datasets don't hold an HTTP
    request open or buffer the whole archive in memory.
"""
import tempfile
import threading
import time
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

# Raster remap modes accepted by the API (see scripts/export_tiles.py).
REMAP_MODES = ("auto", "edgv", "raw")

# Where async-job archives live until downloaded. Archives older than
# ARCHIVE_TTL_SECONDS are pruned whenever a new job is created.
EXPORTS_DIR = Path(tempfile.gettempdir()) / "tileclass_exports"
ARCHIVE_TTL_SECONDS = 24 * 3600

# Shown on jobs that were pending/running when the server stopped.
INTERRUPTED_JOB_ERROR = "Export interrompido pela reinicialização do servidor. Gere novamente."


def _safe(name: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in (name or "proj"))


def _runner_for(kind: str):
    # Imported lazily so the (heavy) rasterio import only happens on a raster
    # export, not on every app import.
    from .scripts import export_tiles, export_classifications
    if kind == "classification":
        return export_classifications.run
    return export_tiles.run


def _validate(status_param: str, remap: str) -> None:
    if status_param not in _STATUS:
        raise ValueError("invalid_status")
    if remap not in REMAP_MODES:
        raise ValueError("invalid_remap")


def _build_zip(project_id: int, status_param: str, dest: Path,
               remap: str = "auto") -> tuple[str, int]:
    """Run the project's exporter into a temp dir and stream the result into the
    zip at `dest` (written incrementally to disk — no full-archive buffer).
    `remap` applies to raster projects only. Returns (filename, tile_count).
    Raises ValueError/LookupError on bad input."""
    _validate(status_param, remap)
    proj = project_service.get_project(project_id)
    if not proj:
        raise LookupError("project_not_found")
    kind = proj.get("kind", "raster")
    runner = _runner_for(kind)
    opts = {"remap": remap} if kind != "classification" else {}
    with tempfile.TemporaryDirectory() as td:
        out = Path(td)
        count = runner(out, status=_STATUS[status_param], project_id=project_id, **opts)
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in sorted(out.rglob("*")):
                if p.is_file():
                    zf.write(p, p.relative_to(out))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    # Raster ZIPs name the effective remap (edgv|raw, never 'auto') so two
    # same-day downloads with different remaps can't be mistaken for each
    # other. Same resolution the exporter used for the manifest column.
    remap_tag = ""
    if kind != "classification":
        from .scripts.export_tiles import resolve_remap
        remap_tag = "_" + resolve_remap(remap, [c["id"] for c in proj.get("classes") or []])
    fname = f"{_safe(proj['name'])}_{status_param}{remap_tag}_{stamp}.zip"
    return fname, count


def export_zip(project_id: int, status_param: str,
               remap: str = "auto") -> tuple[bytes, str, int]:
    """Synchronous export — returns (zip_bytes, filename, tile_count). Builds to
    a temp file (low memory) then reads it back."""
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        fname, count = _build_zip(project_id, status_param, tmp_path, remap)
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


def prune_old_archives(max_age_seconds: float = ARCHIVE_TTL_SECONDS) -> int:
    """Delete job archives in EXPORTS_DIR older than `max_age_seconds`.
    Best-effort (a locked file on Windows is just retried next time). Returns
    the number of files removed. Their job rows stay; download then answers
    `artifact_missing`."""
    if not EXPORTS_DIR.is_dir():
        return 0
    cutoff = time.time() - max_age_seconds
    removed = 0
    for p in EXPORTS_DIR.glob("job_*.zip"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def fail_interrupted_jobs() -> int:
    """Called once at app startup: jobs still 'pending'/'running' belong to a
    worker thread that died with the previous process, so they would poll
    forever. Mark them as errored (and drop any partial archive). Returns the
    number of jobs touched."""
    with transaction("IMMEDIATE") as conn:
        rows = conn.execute(
            "SELECT id FROM export_jobs WHERE state IN ('pending','running')"
        ).fetchall()
        conn.execute(
            "UPDATE export_jobs SET state='error', error=?, finished_at=? "
            "WHERE state IN ('pending','running')",
            (INTERRUPTED_JOB_ERROR, now_iso()),
        )
    for r in rows:
        try:
            (EXPORTS_DIR / f"job_{r['id']}.zip").unlink(missing_ok=True)
        except OSError:
            pass
    return len(rows)


def create_job(project_id: int, status_param: str, by_user: int, *,
               remap: str = "auto", run_async: bool = True) -> dict:
    """Validate, insert a pending job, and kick off the build (in a background
    thread by default). Returns the job row. `run_async=False` runs inline —
    used by tests for determinism. Raises ValueError/LookupError on bad input.
    `remap` (raster only) travels with the worker call; it is not persisted
    (a job never outlives its process — see fail_interrupted_jobs)."""
    _validate(status_param, remap)
    if not project_service.get_project(project_id):
        raise LookupError("project_not_found")
    prune_old_archives()
    with transaction("IMMEDIATE") as conn:
        cur = conn.execute(
            "INSERT INTO export_jobs(project_id, status_param, state, created_by, created_at) "
            "VALUES (?,?,'pending',?,?)",
            (project_id, status_param, by_user, now_iso()),
        )
        job_id = cur.lastrowid
        log_action(conn, by_user, None, "export_job_create",
                   f"{project_id}:{status_param}:{remap}:{job_id}")
    if run_async:
        threading.Thread(target=_run_job, args=(job_id, remap), daemon=True).start()
    else:
        _run_job(job_id, remap)
    return get_job(job_id)


def _run_job(job_id: int, remap: str = "auto") -> None:
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
    dest = EXPORTS_DIR / f"job_{job_id}.zip"
    try:
        EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
        fname, count = _build_zip(row["project_id"], row["status_param"], dest, remap)
        with transaction("IMMEDIATE") as conn:
            conn.execute(
                "UPDATE export_jobs SET state='done', file_path=?, filename=?, "
                "tile_count=?, finished_at=? WHERE id=?",
                (str(dest), fname, count, now_iso(), job_id),
            )
    except Exception as e:  # noqa: BLE001 — persist any failure for the poller
        # A failed build may leave a truncated archive behind; never keep it.
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
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
