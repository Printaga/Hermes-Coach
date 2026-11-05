"""HTTP API behind the Coach settings page.

The host loads this module by file path, so the plugin root is added to
``sys.path`` before the ``coach`` package is imported. Settings live under
``plugins.entries.coach.settings`` in the host config; the two model-override
trust flags live under ``plugins.entries.coach.llm``.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from fastapi import APIRouter, Body, FastAPI, HTTPException, Query, Request

from coach.guidance import GUIDANCE_MAX_CHARS
from coach.settings import DEFAULTS, ModelChoice, _CHOICES, load_settings
from coach.state import Store

__all__ = ["app_for_tests", "router"]

router = APIRouter()

_GUIDANCE_WRITE_MAX = 8000
_GLOBAL_FILE = "guidance.md"
_PROJECT_FILE = "COACH.md"
_SECRET_KEY = "decide_typesafe_api_key"


# --------------------------------------------------------------------------
# Config access
# --------------------------------------------------------------------------


def _load_host_config() -> dict[str, Any]:
    """Read the host config; an unavailable host is an empty config."""
    try:
        from hermes_cli.config import load_config

        data = load_config()
    except Exception:
        return {}
    return copy.deepcopy(dict(data)) if isinstance(data, Mapping) else {}


def _save_host_config(config: Mapping[str, Any]) -> bool:
    """Persist the host config, reporting whether the write landed."""
    try:
        from hermes_cli.config import save_config

        save_config(dict(config))
    except Exception:
        return False
    return True


def _plugin_config(request: Request) -> Mapping[str, Any]:
    """Return the app's in-memory config, or the live host config."""
    state_config = getattr(request.app.state, "config", None)
    if isinstance(state_config, Mapping):
        return state_config
    return _load_host_config()


def _coach_section(config: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """Return ``plugins.entries.coach.<key>`` when it is a mapping."""
    plugins = config.get("plugins")
    if not isinstance(plugins, Mapping):
        return {}
    entries = plugins.get("entries")
    if not isinstance(entries, Mapping):
        return {}
    coach = entries.get("coach")
    if not isinstance(coach, Mapping):
        return {}
    section = coach.get(key)
    return section if isinstance(section, Mapping) else {}


def _coach_settings(config: Mapping[str, Any]) -> Mapping[str, Any]:
    return _coach_section(config, "settings")


def _permissions(config: Mapping[str, Any]) -> dict[str, Any]:
    """Report the two model-override trust flags the host enforces."""
    llm = _coach_section(config, "llm")
    provider = llm.get("allow_provider_override") is True
    model = llm.get("allow_model_override") is True
    return {
        "provider_overrides": provider,
        "model_overrides": model,
        "granted": provider and model,
    }


def _mapping_section(parent: dict[str, Any], key: str) -> dict[str, Any]:
    """Return a mutable child mapping, replacing a malformed value."""
    value = parent.get(key)
    if not isinstance(value, dict):
        value = {}
        parent[key] = value
    return value


def _apply_grant(config: dict[str, Any]) -> None:
    """Set both trust flags; the host still owns the actual write."""
    plugins = _mapping_section(config, "plugins")
    entries = _mapping_section(plugins, "entries")
    coach = _mapping_section(entries, "coach")
    llm = _mapping_section(coach, "llm")
    llm["allow_provider_override"] = True
    llm["allow_model_override"] = True


def _json_value(value: Any) -> Any:
    """Make one config value JSON-safe without dropping structure."""
    if isinstance(value, ModelChoice):
        return {"provider": value.provider, "model": value.model}
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


# --------------------------------------------------------------------------
# Paths and state
# --------------------------------------------------------------------------


def _home(request: Request) -> Path:
    override = getattr(request.app.state, "home", None)
    if override is not None:
        return Path(override).expanduser()
    return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _seen_cwds(home: Path) -> list[Path]:
    """Read the profile's recorded session directories, tolerantly."""
    try:
        data = json.loads(
            (home / "coach" / "state.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    values = data.get("seen_cwds")
    if values is None:
        values = data.get("seen_cwd")
    if not isinstance(values, list):
        return []
    return [
        Path(value).expanduser()
        for value in values
        if isinstance(value, str) and value.strip()
    ]


def _project_dir(home: Path, cwd: str) -> Path:
    """Resolve an existing directory the profile is allowed to write into."""
    text = str(cwd or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="cwd_required")
    try:
        resolved = Path(text).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(status_code=400, detail="cwd_not_found") from None
    if not resolved.is_dir():
        raise HTTPException(status_code=400, detail="cwd_not_directory")
    roots = [home.expanduser().resolve()]
    roots.extend(path.resolve() for path in _seen_cwds(home))
    if any(_within(resolved, root) for root in roots):
        return resolved
    raise HTTPException(status_code=400, detail="cwd_not_registered")


def _clean_scope(value: Any) -> str:
    name = str(value or "global").strip().lower()
    if name in {"local", "cwd"}:
        name = "project"
    if name not in {"global", "project"}:
        raise HTTPException(
            status_code=400, detail="scope must be global or project"
        )
    return name


def _guidance_target(home: Path, scope: str, cwd: str) -> tuple[Path, Path | None]:
    if scope == "project":
        project = _project_dir(home, cwd)
        return project / _PROJECT_FILE, project
    return home / "coach" / _GLOBAL_FILE, None


def _read_guidance(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[
            :_GUIDANCE_WRITE_MAX
        ]
    except OSError:
        return ""


def _write_guidance(path: Path, text: str, *, create_parent: bool) -> str:
    if path.is_symlink():
        raise HTTPException(status_code=400, detail="guidance_file_is_symlink")
    if not path.parent.is_dir():
        if not create_parent:
            raise HTTPException(status_code=400, detail="cwd_missing")
        path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise HTTPException(
            status_code=400, detail=f"guidance_write_failed: {type(exc).__name__}"
        ) from None
    return f"saved {len(text)} chars to {path}"


def _guidance_payload(scope: str, cwd: str, path: Path, text: str) -> dict[str, Any]:
    return {
        "scope": scope,
        "cwd": cwd,
        "path": str(path),
        "text": text,
        "chars": len(text),
        "max_chars": _GUIDANCE_WRITE_MAX,
        "prompt_max_chars": GUIDANCE_MAX_CHARS,
    }


# --------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------


def _model_dict(choice: ModelChoice | None) -> dict[str, str] | None:
    if choice is None:
        return None
    return {"provider": choice.provider, "model": choice.model}


def _status_payload(request: Request) -> dict[str, Any]:
    home = _home(request)
    config = _plugin_config(request)
    settings = load_settings(_coach_settings(config))
    store = Store(home / "coach" / "sessions")

    spend = 0.0
    pending = 0
    last_error = ""
    sessions = store.sessions_on_disk()
    for name in sessions:
        state = store.load(name)
        spend += state.spend_usd
        pending += len(state.pending)
        if state.last_error:
            last_error = state.last_error

    return {
        "configured": settings.is_configured(),
        "coach_model": _model_dict(settings.coach_model),
        "active_model": _model_dict(settings.model_for("active")),
        "review_model": _model_dict(settings.model_for("review")),
        "consult_model": _model_dict(settings.model_for("consult")),
        "active_enabled": settings.active_enabled,
        "review_enabled": settings.review_enabled,
        "decide_enabled": settings.decide_enabled,
        "decide_backend": settings.decide_backend,
        "spend_usd": round(spend, 6),
        "budget_usd": settings.budget_session_usd,
        "budget_on_exceed": settings.budget_on_exceed,
        "pending_notes": pending,
        "sessions": len(sessions),
        "last_error": last_error,
        "permissions": _permissions(config),
    }


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@router.get("/status")
def get_status(request: Request) -> dict[str, Any]:
    """Configuration, spend, pending advice, and permissions."""
    return _status_payload(request)


@router.get("/config")
def get_config(request: Request) -> dict[str, Any]:
    """Current values, defaults, and enum choices for the settings form."""
    config = _plugin_config(request)
    raw = _coach_settings(config)
    values: dict[str, Any] = {}
    for key, default in DEFAULTS.items():
        if key == _SECRET_KEY:
            values[key] = ""
        else:
            values[key] = _json_value(raw.get(key, default))
    return {
        "values": values,
        "defaults": {
            key: _json_value(default)
            for key, default in DEFAULTS.items()
            if key != _SECRET_KEY
        },
        "choices": {key: list(choices) for key, choices in _CHOICES.items()},
        "configured": load_settings(raw).is_configured(),
        "permissions": _permissions(config),
        "secret_set": bool(str(raw.get(_SECRET_KEY) or "").strip()),
    }


@router.get("/guidance")
def get_guidance(
    request: Request,
    scope: str = Query("global"),
    cwd: str = Query(""),
) -> dict[str, Any]:
    """Read one guidance file."""
    name = _clean_scope(scope)
    home = _home(request)
    path, project = _guidance_target(home, name, cwd)
    return _guidance_payload(
        name, str(project) if project is not None else "", path, _read_guidance(path)
    )


@router.put("/guidance")
def put_guidance(
    request: Request, payload: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    """Replace one guidance file after validating its target."""
    body = payload if isinstance(payload, Mapping) else {}
    name = _clean_scope(body.get("scope"))
    text = body.get("text")
    if not isinstance(text, str):
        raise HTTPException(status_code=400, detail="text_required")
    if len(text) > _GUIDANCE_WRITE_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"text_too_long: max {_GUIDANCE_WRITE_MAX} chars",
        )
    home = _home(request)
    path, project = _guidance_target(home, name, str(body.get("cwd") or ""))
    message = _write_guidance(path, text, create_parent=name == "global")
    return {
        **_guidance_payload(
            name, str(project) if project is not None else "", path, text
        ),
        "message": message,
    }


@router.post("/permissions/grant")
def grant_permissions(request: Request) -> dict[str, Any]:
    """Write both model-override trust flags, then return the new status."""
    state_config = getattr(request.app.state, "config", None)
    if isinstance(state_config, dict):
        _apply_grant(state_config)
    else:
        config = _load_host_config()
        _apply_grant(config)
        _save_host_config(config)
    return _status_payload(request)


# --------------------------------------------------------------------------
# Test app
# --------------------------------------------------------------------------


def app_for_tests(
    home: Path, config: Mapping[str, Any] | None = None
) -> FastAPI:
    """Build an isolated app with the API mounted and config in memory."""
    app = FastAPI(title="Coach plugin API")
    app.state.home = Path(home)
    app.state.config = copy.deepcopy(dict(config or {}))
    app.include_router(router)
    return app
