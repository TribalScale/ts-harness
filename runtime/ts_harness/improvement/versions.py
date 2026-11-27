"""Run tagging: the versions every agent step and coding job ran with.

The workflow runner stamps a ``versions`` dict on each task input at dispatch, under the reserved
key ``_versions`` (:data:`VERSIONS_KEY`), and copies it into each coding job spec as ``versions``.
The agent never sees it: the runner leaves it out of the dispatch payload and the agent loop drops
it too. Shape (every section may be missing or empty)::

    {
      "schema": 1,
      "workflow": {"type": "implement_jira_ticket", "version": "3.2.0"},
      "fragments": {"verified_merge": "1.10.0"},
      "step": "deliver_work_order",
      "skills": {"governed-code-review": "1.11.0"},
      "models": {"agent": "...", "coding": "...", "review": "...",
                 "gates": {"review-agent": "...", "qa-agent": "..."}},
      "variants": {"pre_push": "gate_inputs", "<name>": "<value>"}
    }

* ``skills``: the step's ``skill_hints`` resolved to the catalog's live version (the governed
  skills the step loads), plus the code review gate's skill when the pre-push review uses its
  rubric;
* ``models``: ``agent`` is the step agent's configured model, ``coding``/``review`` the coding job's
  model and pre-push reviewer, ``gates`` the configured models of the gate agents;
* ``variants``: ``pre_push`` is the pre-push review variant of a coding job; every other entry is a
  ticket-level assignment set with :func:`set_ticket_variant` (a canary assigns a ticket's workflow
  to a variant before its steps run; the next dispatch of each step records it).

A ticket's versions (:func:`ticket_versions`) merge its tasks' versions oldest first, so later steps
add their skills and models and a re-run step's newer values win. :func:`lookup` reads one dotted
path (``variants.pre_push``, ``skills.governed-code-review``, ``models.coding``) for grouping.

Nothing here may fail a dispatch: :func:`safe_versions` returns ``{}`` on any error.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from typing import Any, Callable, Iterable, Mapping

logger = logging.getLogger(__name__)

VERSIONS_KEY = "_versions"
SCHEMA = 1
VARIANTS_NAMESPACE = "improvement-variants"
PRE_PUSH_VARIANT = "pre_push"
# Variant names and values: short slugs, so a group-by key stays readable and safe.
MAX_VARIANT_TEXT = 64
_CATALOG_TTL_SECONDS = 600
_CONFIG_TTL_SECONDS = 60
_lock = threading.Lock()
_catalog_cache: dict[str, Any] = {"at": 0.0, "versions": {}}
_config_cache: dict[str, Any] = {"at": 0.0, "config": None}


def _text(value: Any) -> str:
    return str(value or "").strip()


# Catalog and config (cached: the runner dispatches many steps a minute)


def live_skill_versions() -> dict[str, str]:
    """Live (approved, highest) version per skill id from the skill catalog; ``{}`` when unreadable."""
    now = time.monotonic()
    with _lock:
        if _catalog_cache["versions"] and now - _catalog_cache["at"] < _CATALOG_TTL_SECONDS:
            return dict(_catalog_cache["versions"])
    versions: dict[str, str] = {}
    try:
        from ts_harness.skills.catalog import SkillCatalog, _version_key

        catalog = SkillCatalog.default()
        catalog.load()
        for meta in catalog.index.values():
            if meta.approval_status != "approved":
                continue
            current = versions.get(meta.id)
            if current is None or _version_key(meta.version) > _version_key(current):
                versions[meta.id] = meta.version
    except Exception as exc:  # noqa: BLE001 - tagging is best effort
        logger.info("skill versions unavailable for run tagging: %s", exc)
    with _lock:
        _catalog_cache.update(at=now, versions=versions)
    return dict(versions)


def _runtime_config() -> Any:
    now = time.monotonic()
    with _lock:
        if _config_cache["config"] is not None and now - _config_cache["at"] < _CONFIG_TTL_SECONDS:
            return _config_cache["config"]
    # Only a runtime that already exists (inside the gateway): building one here is heavy.
    server = sys.modules.get("ts_harness.gateway.server")
    config = getattr(getattr(server, "_RUNTIME", None), "config", None)
    if config is None:
        try:
            from ts_harness.implementation.config import load_active_runtime_config

            config = load_active_runtime_config()
        except Exception as exc:  # noqa: BLE001
            logger.info("runtime config unavailable for run tagging: %s", exc)
    with _lock:
        _config_cache.update(at=now, config=config)
    return config


def agent_models(config: Any = None) -> dict[str, str]:
    """Configured model per agent id (from the runtime config)."""
    config = config if config is not None else _runtime_config()
    models: dict[str, str] = {}
    for agent in getattr(config, "agents", None) or []:
        agent_id, model = _text(getattr(agent, "id", "")), _text(getattr(agent, "model", ""))
        if agent_id and model:
            models[agent_id] = model
    return models


def gate_models(models: Mapping[str, str]) -> dict[str, str]:
    from ts_harness.config.schema import GATE_AGENT_IDS

    return {agent_id: models[agent_id] for agent_id in GATE_AGENT_IDS if models.get(agent_id)}


# Variants


def _slug(value: Any) -> str:
    text = _text(value)[:MAX_VARIANT_TEXT]
    if not text or any(ch.isspace() for ch in text):
        raise ValueError(f"variant names and values are short non-blank words: {value!r}")
    return text


def set_ticket_variant(workflow_id: str, name: str, value: str | None, *, memory: Any = None) -> dict[str, str]:
    """Assign the ticket run by ``workflow_id`` to ``value`` of variant ``name`` (``None`` clears it).

    Steps dispatched afterwards record it under ``variants.<name>``; metrics can then be grouped
    with ``group_by=variants.<name>``. Returns the workflow's variants after the change. Set it on
    the workflow that runs the coding step (for a Jira ticket, its ``implement_jira_ticket`` run).
    """
    from ts_harness.storage.kv import kv_update

    workflow_id = _text(workflow_id)
    if not workflow_id:
        raise ValueError("workflow_id is required")
    key, val = _slug(name), (None if value is None else _slug(value))

    def change(current: Any) -> Any:
        state = {str(k): str(v) for k, v in dict(current or {}).items()}
        if val is None:
            state.pop(key, None)
        else:
            state[key] = val
        return state

    return dict(kv_update(VARIANTS_NAMESPACE, workflow_id, change, default={}, client=memory) or {})


def ticket_variants(workflow_id: str, *, memory: Any = None) -> dict[str, str]:
    """Variants assigned to the ticket run by ``workflow_id`` (``{}`` when none or unreadable)."""
    workflow_id = _text(workflow_id)
    if not workflow_id:
        return {}
    try:
        from ts_harness.storage.kv import kv_read

        value = kv_read(VARIANTS_NAMESPACE, workflow_id, default={}, client=memory)
    except Exception as exc:  # noqa: BLE001 - tagging is best effort
        logger.info("ticket variants unavailable for %s: %s", workflow_id, exc)
        return {}
    return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}


# Building


def _fragments(skeleton: Any) -> dict[str, str]:
    fragments: dict[str, str] = {}
    for include in getattr(skeleton, "includes", None) or []:
        ref = _text((include or {}).get("ref")) if isinstance(include, dict) else ""
        if "@" in ref:
            fragment_id, version = ref.split("@", 1)
            fragments[fragment_id] = version
    return fragments


def versions_for_task(
    task: Mapping[str, Any],
    *,
    stage: Any = None,
    skeleton: Any = None,
    skill_versions: Mapping[str, str] | None = None,
    models: Mapping[str, str] | None = None,
    variants: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The versions an agent step (or the coding step's job) runs with. See the module docstring.

    ``skill_versions``, ``models`` (agent id -> model) and ``variants`` default to the live catalog,
    the runtime config and the ticket's assigned variants; tests pass them in.
    """
    from ts_harness.skills.aliases import canonical_skill_ids

    workflow_type = _text(task.get("workflow_type")) or _text(getattr(skeleton, "workflow_type", ""))
    version = _text(task.get("definition_version")) or _text(getattr(skeleton, "definition_version", ""))
    hints = canonical_skill_ids(list(task.get("skill_hints") or []) or list(getattr(stage, "skill_hints", None) or []))
    catalog = live_skill_versions() if skill_versions is None and hints else dict(skill_versions or {})
    agent_id = _text(task.get("agent_id")) or _text(getattr(stage, "agent_id", ""))
    configured = agent_models() if models is None else dict(models)
    model_map: dict[str, Any] = {}
    if configured.get(agent_id):
        model_map["agent"] = configured[agent_id]
    gates = gate_models(configured)
    if gates:
        model_map["gates"] = gates
    if variants is None:
        variants = ticket_variants(_text(task.get("workflow_id")))
    return {
        "schema": SCHEMA,
        "workflow": {"type": workflow_type, "version": version},
        "fragments": _fragments(skeleton),
        "step": _text(task.get("step_name")),
        "skills": {skill_id: catalog[skill_id] for skill_id in hints if catalog.get(skill_id)},
        "models": model_map,
        "variants": {str(k): str(v) for k, v in dict(variants or {}).items()},
    }


def with_coding_spec(versions: Mapping[str, Any] | None, spec: Mapping[str, Any],
                     *, skill_versions: Mapping[str, str] | None = None) -> dict[str, Any]:
    """``versions`` plus what a coding job spec decides: its model, the pre-push reviewer and variant."""
    out = merge_versions([dict(versions or {})]) if versions else {"schema": SCHEMA}
    models = dict(out.get("models") or {})
    if _text(spec.get("model")):
        models["coding"] = _text(spec["model"])
    if _text(spec.get("review_model")):
        models["review"] = _text(spec["review_model"])
    out["models"] = models
    variants = dict(out.get("variants") or {})
    if _text(spec.get("review_model")) and str(spec.get("mode") or "implement") == "implement":
        from ts_harness.implementation.pre_push_review import VARIANT_LEGACY

        variants[PRE_PUSH_VARIANT] = _text(spec.get("review_variant")) or VARIANT_LEGACY
    out["variants"] = variants
    if _text(spec.get("review_rubric")):
        from ts_harness.implementation.pre_push_review import GATE_SKILL

        gate_skill = GATE_SKILL.parent.name
        catalog = live_skill_versions() if skill_versions is None else dict(skill_versions)
        if catalog.get(gate_skill):
            out["skills"] = {**dict(out.get("skills") or {}), gate_skill: catalog[gate_skill]}
    return out


def safe_versions(build: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """``build()``, or ``{}`` when it fails: tagging never fails a dispatch."""
    try:
        result = build()
        return result if isinstance(result, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("run tagging failed: %s", exc)
        return {}


# Reading


def _merge(into: dict[str, Any], item: Mapping[str, Any]) -> None:
    for key, value in item.items():
        if isinstance(value, Mapping) and isinstance(into.get(key), dict):
            _merge(into[key], value)
        elif isinstance(value, Mapping):
            into[key] = {}
            _merge(into[key], value)
        elif value not in (None, ""):
            into[key] = value


def merge_versions(items: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Deep-merge versions dicts in order; later non-empty values win."""
    out: dict[str, Any] = {}
    for item in items:
        if isinstance(item, Mapping):
            _merge(out, item)
    return out


def task_versions(task: Mapping[str, Any]) -> dict[str, Any]:
    data = task.get("input") if isinstance(task.get("input"), dict) else {}
    value = data.get(VERSIONS_KEY)
    return dict(value) if isinstance(value, dict) else {}


def ticket_versions(tasks: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """A ticket's versions: its tasks' ``_versions`` merged oldest first (``step`` left out)."""
    ordered = sorted((t for t in tasks if isinstance(t, Mapping)), key=lambda t: str(t.get("created_at") or ""))
    merged = merge_versions(task_versions(t) for t in ordered)
    merged.pop("step", None)
    return merged


def lookup(versions: Mapping[str, Any] | None, path: str) -> str | None:
    """The value at dotted ``path`` (``variants.pre_push``), or ``None``. Dicts are not values.

    A skill id or agent id may itself contain dots: the longest matching key wins at each level.
    """
    node: Any = versions or {}
    rest = _text(path)
    while rest:
        if not isinstance(node, Mapping):
            return None
        if rest in node:
            node, rest = node[rest], ""
            break
        parts = rest.split(".")
        for cut in range(len(parts) - 1, 0, -1):
            head = ".".join(parts[:cut])
            if head in node:
                node, rest = node[head], ".".join(parts[cut:])
                break
        else:
            return None
    if node is None or isinstance(node, (Mapping, list)):
        return None
    return str(node)


def without_versions(task_input: Mapping[str, Any]) -> dict[str, Any]:
    """The input an agent sees: everything but ``_versions``."""
    return {k: v for k, v in task_input.items() if k != VERSIONS_KEY}


def enabled() -> bool:
    """``TS_HARNESS_RUN_TAGGING`` (default on)."""
    return (os.environ.get("TS_HARNESS_RUN_TAGGING") or "on").strip().lower() not in {"0", "off", "false", "no"}
