"""CLI: consistent backup of `tileclass.db` while WAL is active.

Uses sqlite3's backup API (`source.backup(target)`) which is safe under
concurrent readers/writers — unlike a raw file copy that would miss the
WAL contents. Output is a self-contained .db file with the WAL flushed in.

Uso:
    python -m backend.scripts.backup_db backups/tileclass-2026-05-07.db
    python -m backend.scripts.backup_db --auto-name backups/        # adds timestamp
"""
import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.database import _db_path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("output", type=Path,
                    help="path do arquivo .db de saída (ou diretório com --auto-name)")
    ap.add_argument("--auto-name", action="store_true",
                    help="append `tileclass-YYYYMMDD-HHMMSS.db` ao output (que vira diretório)")
    args = ap.parse_args()

    src_path = _db_path()
    if not src_path.exists():
        print(f"erro: DB de origem não existe: {src_path}", file=sys.stderr)
        sys.exit(1)

    out = args.output
    if args.auto_name:
        out.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out = out / f"tileclass-{stamp}.db"
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        print(f"erro: destino já existe: {out}", file=sys.stderr)
        sys.exit(1)

    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(out)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    size = out.stat().st_size
    print(f"backup ok: {out}  ({size:,} bytes)")


if __name__ == "__main__":
    main()
