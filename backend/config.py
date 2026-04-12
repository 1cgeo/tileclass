"""Load and expose YAML config as a singleton."""
import os
from pathlib import Path
import yaml

_CONFIG_PATH = Path(__file__).parent / "config.yaml"
_cache = None


def get_config() -> dict:
    global _cache
    if _cache is None:
        path = os.environ.get("TILECLASS_CONFIG") or _CONFIG_PATH
        with open(path, "r", encoding="utf-8") as f:
            _cache = yaml.safe_load(f)
    return _cache


def reload_config() -> dict:
    global _cache
    _cache = None
    return get_config()
