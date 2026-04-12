"""Load and expose YAML config as a singleton."""
import os
import re
from pathlib import Path
import yaml

_CONFIG_PATH = Path(__file__).parent / "config.yaml"
_cache = None
_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


def _validate(cfg: dict) -> None:
    classes = cfg.get("classes") or []
    seen_ids: set[int] = set()
    for c in classes:
        cid = c.get("id")
        if not isinstance(cid, int) or not (1 <= cid <= 254):
            raise ValueError(f"class id inválido: {cid!r} (precisa 1..254)")
        if cid in seen_ids:
            raise ValueError(f"class id duplicado: {cid}")
        seen_ids.add(cid)
        if not c.get("name"):
            raise ValueError(f"classe {cid} sem 'name'")
        color = c.get("color", "")
        if not _HEX_RE.match(color):
            raise ValueError(f"classe {cid} color inválida: {color!r} (esperado #RRGGBB)")


def get_config() -> dict:
    global _cache
    if _cache is None:
        path = os.environ.get("TILECLASS_CONFIG") or _CONFIG_PATH
        with open(path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        _validate(cfg)
        _cache = cfg
    return _cache


def reload_config() -> dict:
    global _cache
    _cache = None
    return get_config()
