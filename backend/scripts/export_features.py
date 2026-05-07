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

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = Path(args.manifest) if args.manifest else out_dir / "manifest.csv"
    statuses = STATUS_FILTERS[args.status]

    conn = connect()
    try:
        project_id = resolve_project_arg(conn, args.project, allow_all=True)
    finally:
        conn.close()
    rows = _select_rows(statuses, project_id)

    paths: list[Path] = []
    mosaic_features: list[dict] = []
    with manifest_path.open("w", newline="", encoding="utf-8") as f:
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
            out = out_dir / fname
            _write_feature_collection(out, {
                "type": "FeatureCollection",
                "features": feats,
            })
            paths.append(out)
            w.writerow(_manifest_row(fname, r))
            if args.mosaic:
                # Tag each feature with its source tile so consumers can
                # join back to the manifest.
                for ff in feats:
                    props = dict(ff.get("properties") or {})
                    props["_tile_id"] = r["id"]
                    props["_tile_name"] = r["name"]
                    mosaic_features.append({**ff, "properties": props})

    print(f"exported {len(paths)} tiles ({args.status}) to {out_dir}")
    print(f"manifest: {manifest_path}")
    if args.mosaic and mosaic_features:
        mpath = out_dir / "gt_mosaic.geojson"
        _write_feature_collection(mpath, {
            "type": "FeatureCollection",
            "features": mosaic_features,
        })
        print(f"mosaic: {mpath}  ({len(mosaic_features)} features)")


if __name__ == "__main__":
    main()
