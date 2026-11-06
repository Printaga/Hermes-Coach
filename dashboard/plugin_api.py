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
from collections import deque
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from fastapi import APIRouter, Body, FastAPI, HTTPException, Query, Request

from coach.guidance import GUIDANCE_MAX_CHARS
from coach.settings import DEFAULTS, ModelChoice, _CHOICES, load_settings
from coach.state import Store

__all__ = ["app_for_tests", "router"]

router = APIRouter()

# One cap for reading and writing: an 8 000-char save reported success and
# the prompt only ever saw the first 4 000.
_GUIDANCE_WRITE_MAX = GUIDANCE_MAX_CHARS
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


def _mutate_config(request: Request, mutate: Any) -> None:
    """Apply *mutate* to the host config and persist it once.

    The branch between the in-memory test config and the on-disk one was
    re-implemented in every mutating route, so a validation fix could land in
    one and be hidden by the other.
    """
    state_config = getattr(request.app.state, "config", None)
    if isinstance(state_config, dict):
        mutate(state_config)
        return
    config = _load_host_config()
    mutate(config)
    if not _save_host_config(config):
        raise HTTPException(status_code=500, detail="config_write_failed")


def _coach_root(config: dict[str, Any]) -> dict[str, Any]:
    return _mapping_section(
        _mapping_section(_mapping_section(config, "plugins"), "entries"), "coach"
    )


def _apply_grant(config: dict[str, Any]) -> None:
    """Set both trust flags; the host still owns the actual write."""
    llm = _mapping_section(_coach_root(config), "llm")
    llm["allow_provider_override"] = True
    llm["allow_model_override"] = True


def _apply_revoke(config: dict[str, Any]) -> None:
    """Clear both trust flags; the host still owns the actual write."""
    llm = _mapping_section(_coach_root(config), "llm")
    llm["allow_provider_override"] = False
    llm["allow_model_override"] = False


def _canonical_values(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Serialize validated settings so stored values are the effective ones."""
    settings = load_settings(raw)
    out: dict[str, Any] = {}
    for key in DEFAULTS:
        if key == _SECRET_KEY:
            continue
        out[key] = _json_value(getattr(settings, key, DEFAULTS[key]))
    return out


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


def _read_guidance(path: Path, *, root: Path | None = None) -> str:
    # Reads mirror the write-time guards: a planted symlink (or a path that
    # resolves outside the project root) must not expose arbitrary files.
    try:
        if path.is_symlink():
            return ""
        if root is not None and path.parent.resolve() != Path(root).resolve():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")[
            :_GUIDANCE_WRITE_MAX
        ]
    except OSError:
        return ""


def _write_guidance(
    path: Path, text: str, *, create_parent: bool, root: Path | None = None
) -> str:
    if path.is_symlink():
        raise HTTPException(status_code=400, detail="guidance_file_is_symlink")
    if root is not None:
        try:
            if path.parent.resolve() != Path(root).resolve():
                raise HTTPException(status_code=400, detail="project_mismatch")
        except OSError:
            raise HTTPException(status_code=400, detail="cwd_missing") from None
    if not path.parent.is_dir():
        if not create_parent:
            raise HTTPException(status_code=400, detail="cwd_missing")
        path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # O_NOFOLLOW closes the check-then-write race on the final component.
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
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


def _guard_origin(request: Request) -> None:
    """Refuse a mutating request that a browser page on another origin sent.

    The dashboard is a local, unauthenticated API: a page open in the user's
    browser can reach it. A cross-origin POST from a normal page carries
    ``Origin``; a same-origin call from the desktop app does not carry a
    foreign one, so rejecting a foreign Origin closes the drive-by write
    without touching the local caller.
    """
    origin = str(request.headers.get("origin") or "").strip()
    if not origin:
        return
    parsed = urlparse(origin)
    host = str(request.headers.get("host") or "")
    if parsed.netloc and parsed.netloc == host:
        return
    if parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        return
    raise HTTPException(status_code=403, detail="cross_origin_request")


# --------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------


def _model_dict(choice: ModelChoice | None) -> dict[str, str] | None:
    return choice.as_dict() if choice is not None else None


def _status_payload(request: Request) -> dict[str, Any]:
    home = _home(request)
    config = _plugin_config(request)
    settings = load_settings(_coach_settings(config))
    store = Store(home / "coach" / "sessions")

    spend = 0.0
    pending = 0
    last_error = ""
    states = store.states_on_disk()
    spend_sessions: dict[str, float] = {}
    for state in states:
        spend += state.spend_usd
        spend_sessions[state.session_id] = round(state.spend_usd, 6)
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
        "spend_total_usd": round(spend, 6),
        "spend_sessions": spend_sessions,
        "budget_usd": settings.budget_session_usd,
        "budget_per_session_usd": settings.budget_session_usd,
        "budget_on_exceed": settings.budget_on_exceed,
        "pending_notes": pending,
        "sessions": len(states),
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


@router.put("/config")
def put_config(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Save settings values for the keys declared in the manifest."""
    _guard_origin(request)
    body = payload if isinstance(payload, Mapping) else {}
    incoming = body.get("values")
    if not isinstance(incoming, Mapping):
        raise HTTPException(status_code=400, detail="values_required")
    clean: dict[str, Any] = {}
    for key, value in incoming.items():
        name = str(key)
        if name not in DEFAULTS or name == _SECRET_KEY:
            raise HTTPException(status_code=400, detail=f"unknown_setting: {name}")
        clean[name] = value
    if not clean:
        raise HTTPException(status_code=400, detail="values_required")

    def mutate(config: dict[str, Any]) -> None:
        merged = dict(_coach_settings(config))
        merged.update(clean)
        _mapping_section(_coach_root(config), "settings").update(
            _canonical_values(merged)
        )

    _mutate_config(request, mutate)
    return get_config(request)


@router.get("/recent")
def get_recent(
    request: Request,
    limit: int = Query(10, ge=1, le=50),
    session: str = Query("", description="Comma-separated session ids to keep"),
) -> dict[str, Any]:
    """Return the newest review findings across sessions, newest first."""
    wanted = {part.strip() for part in str(session or "").split(",") if part.strip()}
    root = _home(request) / "coach" / "logs"
    rows: list[dict[str, Any]] = []
    try:
        paths = sorted(
            root.glob("*.jsonl"), key=lambda item: item.stat().st_mtime, reverse=True
        )[:20]
    except OSError:
        paths = []
    for path in paths:
        try:
            # The log is trimmed to 200 lines / 128 KB, so a full read is
            # bounded; filtering before the tail is what keeps a busy other
            # session from displacing this one's rows.
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        kept: list[dict[str, Any]] = []
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            # A decision row has no severity or title; the page rendered it as
            # "none — no finding". Only reviews belong in this list.
            if str(record.get("kind") or "review") != "review":
                continue
            if wanted and str(record.get("session_id") or "") not in wanted:
                continue
            record["session"] = str(record.get("session_id") or "")[:12]
            kept.append(record)
        rows.extend(kept[-max(1, limit):])
    rows.sort(key=lambda row: str(row.get("at") or ""), reverse=True)
    return {"reviews": rows[:limit]}


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
        name,
        str(project) if project is not None else "",
        path,
        _read_guidance(path, root=project),
    )


@router.put("/guidance")
def put_guidance(
    request: Request, payload: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    """Replace one guidance file after validating its target."""
    _guard_origin(request)
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
    message = _write_guidance(
        path, text, create_parent=name == "global", root=project
    )
    return {
        **_guidance_payload(
            name, str(project) if project is not None else "", path, text
        ),
        "message": message,
    }


@router.post("/permissions/grant")
def grant_permissions(request: Request) -> dict[str, Any]:
    """Write both model-override trust flags, then return the new status."""
    _guard_origin(request)
    _mutate_config(request, _apply_grant)
    return _status_payload(request)


@router.post("/permissions/revoke")
def revoke_permissions(request: Request) -> dict[str, Any]:
    """Clear both model-override trust flags, then return the new status."""
    _guard_origin(request)
    _mutate_config(request, _apply_revoke)
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
