"""Loads criteria.yaml and exposes paths used by the agent."""

from pathlib import Path

import yaml

PKG_DIR = Path(__file__).parent
ROOT_DIR = PKG_DIR.parent
DEFAULT_CRITERIA = PKG_DIR / "criteria.yaml"
DATA_DIR = PKG_DIR / "data"
DB_FILE = DATA_DIR / "pipeline.sqlite3"
LOGS_DIR = ROOT_DIR / "logs"
TOKEN_FILE = ROOT_DIR / "token_internship.json"   # separate from relance.py's token

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.compose",   # drafts
    "https://www.googleapis.com/auth/gmail.readonly",  # reply detection
]

VALID_MODES = {"dry_run", "draft", "send"}


def _deep_merge(base: dict, override: dict) -> dict:
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v  # scalars and lists are replaced, not appended
    return base


def load_settings(path: Path | str | None = None, use_local: bool = True) -> dict:
    """Load criteria.yaml, then criteria.local.yaml next to it if present.

    The .local file is gitignored: put personal details (name, phone, CV path)
    there so they never end up in a public repository.
    """
    path = Path(path or DEFAULT_CRITERIA)
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    local = path.with_name(path.stem + ".local" + path.suffix)
    if use_local and local.exists():
        with open(local, encoding="utf-8") as f:
            _deep_merge(cfg, yaml.safe_load(f))

    mode = cfg["engine"]["mode"]
    if mode not in VALID_MODES:
        raise ValueError(f"engine.mode must be one of {sorted(VALID_MODES)}, got {mode!r}")

    sched = cfg["schedule"]
    if sched["daily_target"] > sched["hard_daily_cap"]:
        raise ValueError("schedule.daily_target cannot exceed schedule.hard_daily_cap")

    cv = Path(cfg["candidate"]["cv_path"])
    if not cv.is_absolute():
        cv = ROOT_DIR / cv
    cfg["candidate"]["cv_path"] = cv
    return cfg
