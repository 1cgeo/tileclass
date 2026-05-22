"""Project export for the admin UI: dispatch to the per-kind exporter, zip the
output, and hand back the bytes for a download response. Single source of truth
shared with the CLI scripts (each exposes a `run()`); kinds covered: raster,
vector, classification, detection."""
import io
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import project_service

# API status param → the scripts' STATUS_FILTERS key.
_STATUS = {
    "reviewed": "reviewed",
    "classified": "classified",
    "reviewed_classified": "reviewed+classified",
}


def _safe(name: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in (name or "proj"))


def export_zip(project_id: int, status_param: str) -> tuple[bytes, str, int]:
    """Run the exporter for the project's kind into a temp dir, zip it, and
    return (zip_bytes, filename, tile_count).
    Raises ValueError on an unknown status, LookupError when the project is gone."""
    if status_param not in _STATUS:
        raise ValueError("invalid_status")
    status = _STATUS[status_param]
    proj = project_service.get_project(project_id)
    if not proj:
        raise LookupError("project_not_found")
    kind = proj.get("kind", "raster")

    # Imported lazily so the (heavy) rasterio import only happens on a raster
    # export, not on every app import.
    from .scripts import (
        export_tiles, export_features, export_classifications, export_detections,
    )
    runner = {
        "vector": export_features.run,
        "classification": export_classifications.run,
        "detection": export_detections.run,
        "raster": export_tiles.run,
    }.get(kind, export_tiles.run)

    with tempfile.TemporaryDirectory() as td:
        out = Path(td)
        count = runner(out, status=status, project_id=project_id)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in sorted(out.rglob("*")):
                if p.is_file():
                    zf.write(p, p.relative_to(out))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    fname = f"{_safe(proj['name'])}_{status_param}_{stamp}.zip"
    return buf.getvalue(), fname, count
