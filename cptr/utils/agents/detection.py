"""Detection helpers for coding agent profiles."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import shutil
from dataclasses import dataclass, asdict
from typing import Any

from cptr.utils.agents.models import (
    default_agent_profiles,
    get_raw_agent_profiles,
    model_id_for_profile,
    normalize_agent_profiles,
)

DETECTION_TTL_SECONDS = 30
CLAUDE_MODELS = [
    "claude-fable-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-opus-4-5",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
]
MIN_CLAUDE_FABLE_5 = (2, 1, 169)
MIN_CLAUDE_OPUS_4_8 = (2, 1, 154)
MIN_CLAUDE_OPUS_4_7 = (2, 1, 111)


@dataclass
class AgentDetection:
    status: str
    command: str | None = None
    version: str | None = None
    message: str | None = None
    models: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _resolve_command(command: str) -> str | None:
    command = command.strip()
    if not command:
        return None
    expanded = os.path.expanduser(command)
    if os.path.isabs(expanded):
        return expanded if os.access(expanded, os.X_OK) else None
    return shutil.which(command)


async def _run_probe(argv: list[str], timeout: float = 3.0) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        return 124, "probe timed out"
    except Exception as exc:  # noqa: BLE001 - surfaced as detection status.
        return 1, str(exc)

    text = (stdout or stderr).decode(errors="replace").strip()
    return proc.returncode or 0, text


async def detect_profile(profile: dict[str, Any]) -> AgentDetection:
    command = _resolve_command(str(profile.get("command") or ""))
    if command is None:
        return AgentDetection("not_found", None, None, "Command not found")

    code, version_text = await _run_probe([command, "--version"])
    version = version_text.splitlines()[0] if code == 0 and version_text else None

    if profile.get("agent") == "claude_code":
        models = _claude_models_for_version(version)
        if importlib.util.find_spec("claude_agent_sdk") is None:
            return AgentDetection(
                "missing_dependency",
                command,
                version,
                "Python package claude-agent-sdk is not installed",
                models,
            )
        return AgentDetection("ready", command, version, None, models)

    if profile.get("agent") == "codex":
        help_code, help_text = await _run_probe([command, "app-server", "--help"])
        if help_code != 0:
            return AgentDetection(
                "error",
                command,
                version,
                help_text or "codex app-server is unavailable",
            )
        models = await _probe_codex_models(command, profile)
        return AgentDetection("ready", command, version, None, models)

    return AgentDetection("error", command, version, "Unknown agent")


def _parse_version_tuple(version: str | None) -> tuple[int, int, int] | None:
    if not version:
        return None
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", version)
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def _version_at_least(version: tuple[int, int, int] | None, minimum: tuple[int, int, int]) -> bool:
    return version is not None and version >= minimum


def _claude_models_for_version(version: str | None) -> list[str]:
    parsed = _parse_version_tuple(version)
    models = []
    for model in CLAUDE_MODELS:
        if model == "claude-fable-5" and not _version_at_least(parsed, MIN_CLAUDE_FABLE_5):
            continue
        if model == "claude-opus-4-8" and not _version_at_least(parsed, MIN_CLAUDE_OPUS_4_8):
            continue
        if model == "claude-opus-4-7" and not _version_at_least(parsed, MIN_CLAUDE_OPUS_4_7):
            continue
        models.append(model)
    return models


async def _probe_codex_models(command: str, profile: dict[str, Any]) -> list[str] | None:
    from cptr.utils.agents.codex import CodexAppServer

    env = os.environ.copy()
    if profile.get("home"):
        env["CODEX_HOME"] = os.path.expanduser(str(profile["home"]))

    client = CodexAppServer(command, os.getcwd(), env)
    try:
        await asyncio.wait_for(client.start(), timeout=5)
        models: list[str] = []
        cursor: str | None = None
        while True:
            response = await asyncio.wait_for(
                client.request("model/list", {"cursor": cursor} if cursor else {}),
                timeout=5,
            )
            result = response.get("result") if isinstance(response.get("result"), dict) else {}
            data = result.get("data") if isinstance(result, dict) else None
            if not isinstance(data, list):
                break
            for item in data:
                if isinstance(item, dict) and isinstance(item.get("model"), str):
                    models.append(item["model"])
            next_cursor = result.get("nextCursor") if isinstance(result, dict) else None
            cursor = next_cursor if isinstance(next_cursor, str) and next_cursor else None
            if not cursor:
                break
        return models or None
    except Exception:
        return None
    finally:
        await client.close()


async def get_agent_status(app_state=None, refresh: bool = False) -> dict[str, Any]:
    now = asyncio.get_running_loop().time()
    cache = getattr(app_state, "AGENTS", None) if app_state is not None else None
    if not refresh and isinstance(cache, dict):
        cached_at = cache.get("cached_at")
        if isinstance(cached_at, (int, float)) and now - cached_at < DETECTION_TTL_SECONDS:
            return cache["payload"]

    raw_profiles = await get_raw_agent_profiles()
    implicit_defaults = raw_profiles is None
    profiles = [] if implicit_defaults else normalize_agent_profiles(raw_profiles)
    entries = []
    candidates = profiles if not implicit_defaults else default_agent_profiles()
    for profile in candidates:
        detected = await detect_profile(profile)
        if implicit_defaults and detected.status == "not_found":
            continue
        mode = profile.get("mode", "auto")
        available = mode != "disabled" and (mode != "auto" or detected.status == "ready")
        models = detected.models or profile.get("models") or ["default"]
        effective_profile = dict(profile)
        effective_profile["models"] = models
        if effective_profile.get("default_model") not in models:
            effective_profile["default_model"] = models[0]
        entries.append(
            {
                "id": effective_profile["id"],
                "agent": effective_profile["agent"],
                "name": effective_profile["name"],
                "config": effective_profile,
                "detected": detected.to_dict(),
                "available": available,
                "implicit": implicit_defaults,
                "model_ids": [model_id_for_profile(effective_profile, model) for model in models],
            }
        )

    payload = {"profiles": entries}
    if app_state is not None:
        app_state.AGENTS = {"cached_at": now, "payload": payload}
    return payload


def invalidate_agent_detection_cache(app_state) -> None:
    if app_state is not None and hasattr(app_state, "AGENTS"):
        delattr(app_state, "AGENTS")


async def get_available_agent_model_entries(app_state=None) -> list[dict[str, str]]:
    status = await get_agent_status(app_state)
    entries: list[dict[str, str]] = []
    for profile in status["profiles"]:
        if not profile["available"]:
            continue
        config = profile["config"]
        for model in config.get("models") or ["default"]:
            model_id = model_id_for_profile(config, model)
            entries.append(
                {
                    "id": model_id,
                    "name": model_id,
                    "provider": "agent",
                    "connection_id": f"agent:{config['id']}",
                    "agent_id": config["agent"],
                    "profile_id": config["id"],
                }
            )
    return entries
