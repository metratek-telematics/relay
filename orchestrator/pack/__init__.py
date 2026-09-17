"""Adapters for the agent pack. Each module exposes ADAPTERS = {name: adapter instance}."""
from __future__ import annotations

import importlib

PACK_ADAPTERS: dict = {}

for _mod in ("opencode", "stream_json", "json_events", "text_based"):
    try:
        PACK_ADAPTERS.update(importlib.import_module(f"{__name__}.{_mod}").ADAPTERS)
    except ModuleNotFoundError as e:  # a group not written yet must not take Relay down
        if e.name != f"{__name__}.{_mod}":
            raise
