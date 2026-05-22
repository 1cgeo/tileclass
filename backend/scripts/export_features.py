"""CLI: export vector tile annotations as GeoJSON FeatureCollections.

Counterpart to `export_tiles.py` (which writes raster GeoTIFFs).

Output per tile:
    <out_dir>/gt_<tile.name>.geojson   — FeatureCollection in EPSG:4326

`--mosaic` writes a single `gt_mosaic.geojson` merging features from every
selected tile (each feature's properties get a `_tile_id` injected so the
consumer can trace back).

Manifest CSV is parallel to the raster export's: 1 row per file.

Filter modes:
    --status reviewed              (padrão) só tiles que passaram revisão
    --status reviewed+classified   inclui também classified
    --project <id|name>            opcional; sem o flag, todos os projetos
                                   vetoriais entram num único output
"""
import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.database import connect
from backend.scripts._common import resolve_project_arg


STATUS_FILTERS = {
    "reviewed": ("reviewed",),
    "classified": ("classified",),
    "reviewed+classified": ("reviewed", "classified"),
}


MANIFEST_HEADER = [
    "filename", "tile_id", "project_id", "name", "status",
    "feature_count",
    "classified_by", "reviewed_by",
    "classified_at", "reviewed_at",
    "bbox_west", "bbox_south", "bbox_east", "bbox_north",
]


def _select_rows(statuses: tuple[str, ...], project_id: int | None) -> list:
    """Pull every vector-project tile whose status matches. Joining
    `projects` filters out raster tiles in case --project wasn't given."""
    placeholders = ",".join("?" for _ in statuses)
    conn = connect()
    try:
        sql = f"""
            SELECT t.id, t.project_id, t.name, t.status,
                   t.classified_by, t.reviewed_by,
                   t.classified_at, t.reviewed_at,
                   t.bbox_west, t.bbox_south, t.bbox_east, t.bbox_north,
                   t.data_geojson, t.feature_count
            FROM tiles t JOIN projects p ON p.id=t.project_id
            WHERE p.kind='vector' AND t.status IN ({placeholders})
              AND t.data_geojson IS NOT NULL
        """
        params: list = list(statuses)
        if project_id is not None:
            sql += " AND t.project_id=?"
            params.append(project_id)
        sql += " ORDER BY t.project_id, t.name"
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _write_feature_collection(out: Path, doc: dict) -> None:
    out.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def _manifest_row(fname: str, r) -> list:
    return [
        fname, r["id"], r["project_id"], r["name"], r["status"],
        r["feature_count"] or 0,
        r["classified_by"], r["reviewed_by"],
        r["classified_at"], r["reviewed_at"],
        r["bbox_west"], r["bbox_south"], r["bbox_east"], r["bbox_north"],
    ]


def run(out_dir, *, status: str = "reviewed", project_id=None,
        mosaic: bool = False, manifest_path=None) -> int:
    """Write per-tile GeoJSON + manifest for vector `project_id` (None = all
    vector projects), filtered by `status` (a STATUS_FILTERS key). Returns the
    tile count. Shared by the CLI and the admin export endpoint."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = Path(manifest_path) if manifest_path else out_dir / "manifest.csv"
    rows = _select_rows(STATUS_FILTERS[status], project_id)

    paths: list[Path] = []
    mosaic_features: list[dict] = []
    with manifest.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(MANIFEST_HEADER)
        for r in rows:
            try:
                doc = json.loads(r["data_geojson"])
            except (TypeError, ValueError):
                # Skip — verify_db should have caught this; flagging here
                # just keeps a partial export from blowing up entirely.
                continue
            feats = doc.get("features") or []
            fname = f"gt_{r['name']}.geojson"
            _write_feature_collection(out_dir / fname, {
                "type": "FeatureCollection", "features": feats,
            })
            paths.append(out_dir / fname)
            w.writerow(_manifest_row(fname, r))
            if mosaic:
                for ff in feats:
                    props = dict(ff.get("properties") or {})
                    props["_tile_id"] = r["id"]
                    props["_tile_name"] = r["name"]
                    mosaic_features.append({**ff, "properties": props})

    if mosaic and mosaic_features:
        _write_feature_collection(out_dir / "gt_mosaic.geojson", {
            "type": "FeatureCollection", "features": mosaic_features,
        })
    return len(paths)


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("out_dir")
    p.add_argument("--status", choices=sorted(STATUS_FILTERS), default="reviewed")
    p.add_argument("--mosaic", action="store_true",
                   help="também escreve gt_mosaic.geojson com todas as features")
    p.add_argument("--manifest", default=None,
                   help="manifest CSV path (default: <out_dir>/manifest.csv)")
    p.add_argument("--project", type=str, default=None,
                   help="id ou nome do projeto vetorial; omitir = todos")
    args = p.parse_args()

    conn = connect()
    try:
        project_id = resolve_project_arg(conn, args.project, allow_all=True)
    finally:
        conn.close()
    n = run(args.out_dir, status=args.status, project_id=project_id,
            mosaic=args.mosaic, manifest_path=args.manifest)
    print(f"exported {n} tiles ({args.status}) to {args.out_dir}")


if __name__ == "__main__":
    main()
