import json
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"


def load_config(*required: str) -> dict:
    if not CONFIG_PATH.exists():
        raise SystemExit(f"config file not found: {CONFIG_PATH}")
    with CONFIG_PATH.open(encoding="utf-8") as f:
        config = json.load(f)
    for key in required:
        if not config.get(key):
            raise SystemExit(f"missing '{key}' in {CONFIG_PATH}")
    return config
