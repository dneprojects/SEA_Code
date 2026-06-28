"""Smart Energy Agent - energy-aware monitoring and (later) control for Home Assistant."""

import os
from pathlib import Path


def _resolve_version() -> str:
    """Version reported by the app. In the container the HA builder bakes the
    full channel version (e.g. ``0.6.0-beta.90``) into ``SEA_VERSION``; in a dev
    checkout we fall back to the base version in ``config.yaml`` next to the
    package, so the reported version never drifts from the manifest."""
    env = os.environ.get("SEA_VERSION")
    if env and env.strip():
        return env.strip()
    try:
        cfg = Path(__file__).resolve().parent.parent / "config.yaml"
        for line in cfg.read_text(encoding="utf-8").splitlines():
            if line.startswith("version:"):
                return line.split(":", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return "0.6.0"


__version__ = _resolve_version()
