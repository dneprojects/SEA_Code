"""Constants and configuration helpers for Smart Energy Agent."""

from __future__ import annotations

import os
from pathlib import Path

# --- Home Assistant websocket connection -------------------------------------
# When running as a HA add-on, the Supervisor exposes the core websocket here
# and provides an auth token via the SUPERVISOR_TOKEN environment variable.
SUPERVISOR_WS_URI = "ws://supervisor/core/websocket"

# For standalone/local development the app falls back to these env vars.
ENV_HA_URL = "HA_URL"          # e.g. ws://192.168.178.144:8123/api/websocket
ENV_HA_TOKEN = "HA_TOKEN"      # long-lived access token

ENV_SUPERVISOR_TOKEN = "SUPERVISOR_TOKEN"

# --- Add-on options (exported by run.sh) -------------------------------------
ENV_LOG_LEVEL = "SEA_LOG_LEVEL"
ENV_HISTORY_DB = "SEA_HISTORY_DB"
ENV_HISTORY_DAYS = "SEA_HISTORY_DAYS"

DEFAULT_LOG_LEVEL = "info"
# /addon_config is a host-mapped folder (see config.yaml `map:`) that survives
# an uninstall/reinstall; /data is the add-on's private volume and is wiped on
# uninstall. All JSON settings derive their directory from this path, so keeping
# it under /addon_config makes the whole configuration persistent.
DEFAULT_HISTORY_DB = "/addon_config/smart_energy_agent.db"
DEFAULT_HISTORY_DAYS = 90

# Web/Ingress server
WEB_HOST = "0.0.0.0"
WEB_PORT = 7790
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# Heartbeat / reconnect tuning (seconds)
WS_PING_INTERVAL = 30
WS_RECONNECT_MIN = 2
WS_RECONNECT_MAX = 60
WS_OPEN_TIMEOUT = 5

# History recorder
RECORD_INTERVAL = 30      # seconds between energy-state snapshots
PURGE_INTERVAL = 86400    # seconds between retention purges (daily)

# Solar forecast (Energy dashboard / Forecast.Solar) refresh
SOLAR_FORECAST_INTERVAL = 900  # seconds between solar-forecast refreshes (15 min)

# Thermostat absence-setback engine
SETBACK_INTERVAL = 120     # seconds between setback decisions

# PV-surplus control engine
CONTROL_INTERVAL = 60     # seconds between control decisions
CONTROL_ON_MARGIN_W = 50  # surplus must exceed this to consider switching on
CONTROL_OFF_MARGIN_W = 50 # import (negative surplus) beyond this triggers switch off
# Domains we can switch on/off in this phase (simple on/off loads).
CONTROLLABLE_DOMAINS = ("switch", "input_boolean", "light")


def is_addon() -> bool:
    """True when running inside a HA add-on (Supervisor token present)."""
    return os.getenv(ENV_SUPERVISOR_TOKEN) is not None


def get_log_level() -> str:
    return os.getenv(ENV_LOG_LEVEL, DEFAULT_LOG_LEVEL)


def get_history_db() -> str:
    return os.getenv(ENV_HISTORY_DB, DEFAULT_HISTORY_DB)


def get_history_days() -> int:
    try:
        return int(os.getenv(ENV_HISTORY_DAYS, str(DEFAULT_HISTORY_DAYS)))
    except ValueError:
        return DEFAULT_HISTORY_DAYS


def get_overrides_path() -> str:
    """JSON file storing user curation (include/role) next to the history DB."""
    db = get_history_db()
    directory = os.path.dirname(db) or "."
    return os.path.join(directory, "overrides.json")


def get_settings_path() -> str:
    """JSON file storing runtime settings (sign conventions, retention)."""
    db = get_history_db()
    directory = os.path.dirname(db) or "."
    return os.path.join(directory, "settings.json")


def get_consumers_path() -> str:
    """JSON file storing per-consumer control configuration."""
    db = get_history_db()
    directory = os.path.dirname(db) or "."
    return os.path.join(directory, "consumers.json")


def get_device_overrides_path() -> str:
    """JSON file storing device curation (include/type)."""
    db = get_history_db()
    directory = os.path.dirname(db) or "."
    return os.path.join(directory, "device_overrides.json")


def get_energy_config_path() -> str:
    """JSON file storing the wizard's explicit category->entity configuration."""
    db = get_history_db()
    directory = os.path.dirname(db) or "."
    return os.path.join(directory, "energy_config.json")


# --- Legacy data migration ---------------------------------------------------
# Older versions stored the DB and all JSON settings under /data, the add-on's
# private volume, which Home Assistant wipes on uninstall. The default is now
# /addon_config (host-mapped, survives uninstall). On a normal update the old
# /data files are still present, so copy them over once.
LEGACY_DATA_DIR = "/data"
_CONFIG_FILENAMES = (
    "overrides.json",
    "settings.json",
    "consumers.json",
    "device_overrides.json",
    "energy_config.json",
)


def migrate_legacy_data_if_needed() -> list[str]:
    """Copy config/history from the old /data volume to the configured directory.

    Non-destructive (the /data originals are kept as a fallback) and only runs
    when the DB path is still the default and points somewhere other than the
    legacy dir. Returns the list of filenames that were copied.
    """
    import shutil

    db = get_history_db()
    if db != DEFAULT_HISTORY_DB:
        return []  # user pointed storage elsewhere -> don't touch
    new_dir = os.path.dirname(db) or "."
    if os.path.abspath(new_dir) == os.path.abspath(LEGACY_DATA_DIR):
        return []
    if not os.path.isdir(LEGACY_DATA_DIR):
        return []

    moved: list[str] = []
    names = (os.path.basename(db), *_CONFIG_FILENAMES)
    try:
        os.makedirs(new_dir, exist_ok=True)
    except OSError:
        return []
    for name in names:
        src = os.path.join(LEGACY_DATA_DIR, name)
        dst = os.path.join(new_dir, name)
        if os.path.exists(src) and not os.path.exists(dst):
            try:
                shutil.copy2(src, dst)
                moved.append(name)
            except OSError:
                pass
    return moved
