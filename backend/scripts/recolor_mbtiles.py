"""Recoloriza um MBTiles PNG por substituição exata de RGB.

Caso de uso: o overlay foi renderizado com paleta A; queremos exibir com paleta B
sem regenerar a partir do raster fonte. Cada par `--map AABBCC:XXYYZZ` substitui
todo pixel cuja tripla (R,G,B) bate exatamente. O canal alpha é preservado.

Uso:
  python -m backend.scripts.recolor_mbtiles <in.mbtiles> <out.mbtiles> \\
      --map AABBCC:XXYYZZ ... [--workers 8]

Cópia de metadata e schema MBTiles 1.3 padrão do destino.
"""
from __future__ import annotations
import argparse
import io
import os
import sqlite3
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
from PIL import Image

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


MBTILES_SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS tiles (
    zoom_level INTEGER NOT NULL,
    tile_column INTEGER NOT NULL,
    tile_row INTEGER NOT NULL,
    tile_data BLOB NOT NULL,
    PRIMARY KEY (zoom_level, tile_column, tile_row)
);
CREATE INDEX IF NOT EXISTS tiles_index ON tiles (zoom_level, tile_column, tile_row);
"""


def parse_pair(s: str) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    a, b = s.split(":")
    aa = bytes.fromhex(a.lstrip("#"))
    bb = bytes.fromhex(b.lstrip("#"))
    if len(aa) != 3 or len(bb) != 3:
        raise ValueError(f"par inválido (espera RRGGBB:RRGGBB): {s}")
    return (aa[0], aa[1], aa[2]), (bb[0], bb[1], bb[2])


def recolor(args):
    """Decodifica PNG, substitui pares de RGB, re-codifica."""
    z, x, tms_y, data, mapping = args
    img = Image.open(io.BytesIO(data))
    arr = np.array(img.convert("RGBA"))
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    changed = False
    for (or_, og, ob), (nr, ng, nb) in mapping:
        mask = (r == or_) & (g == og) & (b == ob)
        if mask.any():
            arr[..., 0][mask] = nr
            arr[..., 1][mask] = ng
            arr[..., 2][mask] = nb
            changed = True
    if not changed:
        return z, x, tms_y, data  # passa adiante sem re-encode
    buf = io.BytesIO()
    Image.fromarray(arr, "RGBA").save(buf, format="PNG", optimize=True)
    return z, x, tms_y, buf.getvalue()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("in_mbtiles")
    ap.add_argument("out_mbtiles")
    ap.add_argument("--map", action="append", required=True,
                    help="par RRGGBB:RRGGBB (pode repetir)")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) - 1))
    ap.add_argument("--batch-size", type=int, default=2000)
    args = ap.parse_args()

    in_path = Path(args.in_mbtiles).resolve()
    out_path = Path(args.out_mbtiles).resolve()
    if not in_path.exists():
        sys.exit(f"[err] não achou {in_path}")
    if out_path.exists():
        sys.exit(f"[err] saída já existe (apague antes): {out_path}")
    mapping = [parse_pair(s) for s in args.map]
    print(f"[info] {len(mapping)} pares de cor:")
    for o, n in mapping:
        print(f"  {o} -> {n}")

    # Abre source (read-only) e destino
    src = sqlite3.connect(f"file:{in_path.as_posix()}?mode=ro&immutable=1", uri=True)
    dst = sqlite3.connect(str(out_path), isolation_level=None)
    dst.executescript(MBTILES_SCHEMA)
    dst.execute("PRAGMA journal_mode=WAL")
    dst.execute("PRAGMA synchronous=NORMAL")

    # Copia metadata
    for k, v in src.execute("SELECT name, value FROM metadata").fetchall():
        dst.execute(
            "INSERT INTO metadata(name,value) VALUES(?,?) "
            "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
            (k, v),
        )

    total = src.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
    print(f"[info] {total:,} tiles para processar")
    t_start = time.time()

    # Stream tiles em batches via cursor — evita carregar tudo na RAM
    cur = src.execute("SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles")
    n_done = 0
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        while True:
            batch = cur.fetchmany(args.batch_size)
            if not batch:
                break
            jobs = [(z, x, ty, data, mapping) for z, x, ty, data in batch]
            futs = [ex.submit(recolor, j) for j in jobs]
            dst.execute("BEGIN")
            for f in as_completed(futs):
                z, x, ty, data = f.result()
                dst.execute(
                    "INSERT INTO tiles(zoom_level,tile_column,tile_row,tile_data) VALUES(?,?,?,?)",
                    (z, x, ty, data),
                )
            dst.execute("COMMIT")
            n_done += len(batch)
            dt = time.time() - t_start
            rate = n_done / dt if dt > 0 else 0
            eta = (total - n_done) / rate if rate > 0 else 0
            print(f"  {n_done:,}/{total:,}  {rate:.0f} t/s  ETA {eta/60:.1f}m", flush=True)

    src.close()
    dst.execute("ANALYZE")
    dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    dst.execute("PRAGMA journal_mode=DELETE")
    dst.close()

    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(f"\n[done] {out_path}  {size_mb:.1f} MB  {total:,} tiles em {(time.time()-t_start)/60:.1f} min")


if __name__ == "__main__":
    main()
