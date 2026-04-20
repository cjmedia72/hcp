"""anthropic_plan -- Hermes plugin that routes Anthropic requests through
the Claude Code subscription billing channel.

Starts a loopback HTTP proxy on plugin load and registers a
``custom_providers`` entry in ``~/.hermes/config.yaml`` so hermes sends
Anthropic Messages API requests through the proxy.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("anthropic_plan")

_PLUGIN_DIR = Path(__file__).resolve().parent

import importlib.util as _ilu  # noqa: E402

_proxy_spec = _ilu.spec_from_file_location(
    "anthropic_plan_proxy",
    _PLUGIN_DIR / "proxy.py",
)
_proxy = _ilu.module_from_spec(_proxy_spec)
sys.modules["anthropic_plan_proxy"] = _proxy
_proxy_spec.loader.exec_module(_proxy)


PROVIDER_KEY = "anthropic_plan"
PROVIDER_DEFAULT_MODEL = "claude-opus-4-6"


def _build_provider_entry(port: int) -> dict:
    return {
        "name": "anthropic_plan",
        "base_url": f"http://127.0.0.1:{port}",
        "api_mode": "anthropic_messages",
        "model": PROVIDER_DEFAULT_MODEL,
        "models_endpoint": "/v1/models",
        "key_env": "ANTHROPIC_PLAN_DUMMY",
        "_managed_by": "anthropic_plan_plugin",
    }


# -- config.yaml management ------------------------------------------------- #

def _hermes_home() -> Path:
    return Path.home() / ".hermes"


def _config_path() -> Path:
    return _hermes_home() / "config.yaml"


def _load_yaml(path: Path):
    try:
        from ruamel.yaml import YAML
        ya = YAML()
        ya.preserve_quotes = True
        with path.open("r", encoding="utf-8") as f:
            return ya.load(f), ya
    except ImportError:
        import yaml
        with path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}, None


def _dump_yaml(data, path: Path, ya):
    if ya is not None:
        with path.open("w", encoding="utf-8") as f:
            ya.dump(data, f)
    else:
        import yaml
        with path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


def _backup(path: Path) -> Path:
    bak = path.with_suffix(path.suffix + f".bak-{int(time.time())}")
    shutil.copy2(path, bak)
    return bak


def ensure_provider_in_config(port: int) -> Optional[Path]:
    """Insert or refresh the ``custom_providers`` entry for this plugin."""
    cfg_path = _config_path()
    if not cfg_path.exists():
        logger.warning("anthropic_plan: %s not found -- skipping config injection", cfg_path)
        return None
    try:
        data, ya = _load_yaml(cfg_path)
    except Exception as exc:
        logger.warning("anthropic_plan: failed to parse %s: %s", cfg_path, exc)
        return None
    if not isinstance(data, dict):
        return None

    desired = _build_provider_entry(port)
    changed = False

    custom = data.get("custom_providers")
    if not isinstance(custom, list):
        custom = []
        data["custom_providers"] = custom

    existing_idx = None
    for i, entry in enumerate(custom):
        if isinstance(entry, dict) and (
            entry.get("name") == desired["name"]
            or entry.get("_managed_by") == "anthropic_plan_plugin"
        ):
            existing_idx = i
            break

    if existing_idx is None:
        custom.append(desired)
        changed = True
    else:
        existing = custom[existing_idx]
        if (
            existing.get("base_url") != desired["base_url"]
            or existing.get("api_mode") != desired["api_mode"]
            or existing.get("_managed_by") != desired["_managed_by"]
        ):
            custom[existing_idx] = desired
            changed = True

    # Clean up any stale providers.anthropic_plan from earlier versions
    providers = data.get("providers")
    if isinstance(providers, dict) and PROVIDER_KEY in providers:
        old = providers[PROVIDER_KEY]
        if isinstance(old, dict) and old.get("_managed_by") == "anthropic_plan_plugin":
            del providers[PROVIDER_KEY]
            if not providers:
                del data["providers"]
            changed = True

    if not changed:
        return None

    bak = _backup(cfg_path)
    try:
        _dump_yaml(data, cfg_path, ya)
        logger.info("anthropic_plan: wrote custom_providers entry (backup: %s)", bak.name)
        return bak
    except Exception as exc:
        try:
            shutil.copy2(bak, cfg_path)
        except Exception:
            pass
        logger.error("anthropic_plan: write failed: %s -- restored backup", exc)
        return None


def remove_provider_from_config() -> bool:
    """Remove our managed entries from config.yaml."""
    cfg_path = _config_path()
    if not cfg_path.exists():
        return False
    try:
        data, ya = _load_yaml(cfg_path)
    except Exception:
        return False
    if not isinstance(data, dict):
        return False

    changed = False

    custom = data.get("custom_providers")
    if isinstance(custom, list):
        keep = []
        for entry in custom:
            if isinstance(entry, dict) and (
                entry.get("_managed_by") == "anthropic_plan_plugin"
                or entry.get("name") == PROVIDER_KEY
            ):
                changed = True
                continue
            keep.append(entry)
        if changed:
            if keep:
                data["custom_providers"] = keep
            else:
                del data["custom_providers"]

    providers = data.get("providers")
    if isinstance(providers, dict) and PROVIDER_KEY in providers:
        entry = providers.get(PROVIDER_KEY)
        if isinstance(entry, dict) and entry.get("_managed_by") == "anthropic_plan_plugin":
            del providers[PROVIDER_KEY]
            if not providers:
                del data["providers"]
            changed = True

    if not changed:
        return False

    _backup(cfg_path)
    _dump_yaml(data, cfg_path, ya)
    logger.info("anthropic_plan: removed managed entries from config.yaml")
    return True


# -- Hermes plugin entry point ---------------------------------------------- #

def _on_session_start(**kwargs) -> None:
    try:
        port = _proxy.start_proxy()
    except Exception as exc:
        logger.warning("anthropic_plan: failed to start proxy: %s", exc)
        return
    try:
        ensure_provider_in_config(port)
    except Exception as exc:
        logger.warning("anthropic_plan: failed to write config.yaml: %s", exc)


def register(ctx) -> None:
    """Plugin entry point -- called by hermes during startup."""
    try:
        port = _proxy.start_proxy()
    except Exception as exc:
        logger.warning("anthropic_plan: failed to start proxy at register-time: %s", exc)
        port = None

    if port is not None:
        try:
            ensure_provider_in_config(port)
        except Exception as exc:
            logger.warning("anthropic_plan: config injection failed: %s", exc)

    try:
        ctx.register_hook("on_session_start", _on_session_start)
    except Exception as exc:
        logger.debug("anthropic_plan: register_hook failed (non-fatal): %s", exc)
