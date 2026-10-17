#!/usr/bin/env python3
# flake8: noqa
"""Reusable workflow stage fragments: loading, resolution and expansion.

A fragment is a versioned sequence of stages with declared ``params`` (stage
names of the including workflow) and ``inputs`` (expressions bound by the
including workflow). A workflow includes one with a ``kind: "fragment"``
stage; ``expand_fragments`` splices the fragment stages in its place.

Kept free of ``ts_harness`` imports because the workflow runner imports
``workflow_definitions`` (and therefore this module) standalone.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import copy
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Callable

FRAGMENT_KIND = "fragment"
PARAM_KINDS = frozenset({"stage_ref"})
FRAGMENT_TOP_LEVEL_KEYS = frozenset(
    {
        "fragment_id",
        "definition_version",
        "display_name",
        "description",
        "params",
        "inputs",
        "contracts",
        "stages",
    }
)
INCLUDE_KEYS = frozenset(
    {"name", "kind", "fragment", "version", "params", "inputs", "depends_on", "prefix"}
)
STAGE_NAME_LIST_KEYS = frozenset({"depends_on", "source_steps", "downstream"})
STAGE_NAME_KEYS = frozenset({"step_name", "compensate_stage", "join_stage"})

_PARAM_RE = re.compile(r"\{\{\s*param\.([A-Za-z0-9_]+)\s*\}\}")
_INPUT_RE = re.compile(r"^\$input\.([A-Za-z0-9_]+)$")


class FragmentError(ValueError):
    """Raised when a fragment or a fragment include is invalid.

    ``include_index`` is the raw stage index of the failing include, when known.
    """

    include_index: int | None = None


_RESOLVER_OVERRIDE: ContextVar[Callable[[str, str], "Fragment"] | None] = ContextVar(
    "workflow_fragment_resolver", default=None
)


@contextmanager
def override_resolver(resolver: Callable[[str, str], "Fragment"]):
    """Resolve fragments with ``resolver`` (e.g. an unsaved draft) inside the block."""
    token = _RESOLVER_OVERRIDE.set(resolver)
    try:
        yield
    finally:
        _RESOLVER_OVERRIDE.reset(token)


@dataclass(frozen=True)
class Fragment:
    fragment_id: str
    definition_version: str
    stages: list[dict[str, Any]]
    params: dict[str, dict[str, Any]] = field(default_factory=dict)
    inputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    contracts: dict[str, dict[str, Any]] = field(default_factory=dict)
    display_name: str = ""
    description: str = ""

    @property
    def ref(self) -> str:
        return f"{self.fragment_id}@{self.definition_version}"


@dataclass
class Expansion:
    payload: dict[str, Any]
    includes: list[dict[str, Any]]
    # One entry per expanded stage: (raw stage index, include name or "").
    index_map: list[tuple[int, str]]


def _require_str(data: dict[str, Any], key: str, label: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise FragmentError(f"{label} requires a non-empty string {key}")
    return value.strip()


def _declarations(raw: Any, label: str, *, params: bool) -> dict[str, dict[str, Any]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise FragmentError(f"fragment {label} must be an object")
    declared: dict[str, dict[str, Any]] = {}
    for name, spec in raw.items():
        if not re.fullmatch(r"[A-Za-z0-9_]+", str(name)):
            raise FragmentError(f"fragment {label} name is invalid: {name}")
        spec = dict(spec or {}) if isinstance(spec, dict) or spec is None else None
        if spec is None:
            raise FragmentError(f"fragment {label}.{name} must be an object")
        normalized: dict[str, Any] = {"required": bool(spec.get("required", True))}
        if params:
            kind = str(spec.get("kind") or "stage_ref")
            if kind not in PARAM_KINDS:
                raise FragmentError(f"fragment param {name} has unsupported kind: {kind}")
            normalized["kind"] = kind
        description = str(spec.get("description") or "").strip()
        if description:
            normalized["description"] = description
        declared[str(name)] = normalized
    return declared


def load_fragment_payload(payload: dict[str, Any]) -> Fragment:
    if not isinstance(payload, dict):
        raise FragmentError("fragment must be an object")
    unknown = sorted(set(payload) - FRAGMENT_TOP_LEVEL_KEYS)
    if unknown:
        raise FragmentError(f"fragment has unknown keys: {', '.join(unknown)}")
    fragment_id = _require_str(payload, "fragment_id", "fragment")
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", fragment_id):
        raise FragmentError(f"fragment_id is invalid: {fragment_id}")
    version = _require_str(payload, "definition_version", "fragment")
    stages = payload.get("stages")
    if not isinstance(stages, list) or not stages:
        raise FragmentError("fragment stages must be a non-empty array")
    names: set[str] = set()
    for stage in stages:
        if not isinstance(stage, dict):
            raise FragmentError("fragment stages must be objects")
        if stage.get("kind") == FRAGMENT_KIND:
            raise FragmentError("fragments cannot include other fragments")
        name = _require_str(stage, "name", "fragment stage")
        if name in names:
            raise FragmentError(f"fragment stage names must be unique: {name}")
        names.add(name)
    contracts = payload.get("contracts") or {}
    if not isinstance(contracts, dict):
        raise FragmentError("fragment contracts must be an object")
    fragment = Fragment(
        fragment_id=fragment_id,
        definition_version=version,
        stages=copy.deepcopy(stages),
        params=_declarations(payload.get("params"), "params", params=True),
        inputs=_declarations(payload.get("inputs"), "inputs", params=False),
        contracts=copy.deepcopy(contracts),
        display_name=str(payload.get("display_name") or ""),
        description=str(payload.get("description") or ""),
    )
    params_used, inputs_used = referenced_names(fragment)
    undeclared_params = sorted(params_used - set(fragment.params))
    if undeclared_params:
        raise FragmentError(f"fragment references undeclared params: {', '.join(undeclared_params)}")
    undeclared_inputs = sorted(inputs_used - set(fragment.inputs))
    if undeclared_inputs:
        raise FragmentError(f"fragment references undeclared inputs: {', '.join(undeclared_inputs)}")
    return fragment


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    return []


def referenced_names(fragment: Fragment) -> tuple[set[str], set[str]]:
    """Param and input names the fragment's stages and contracts reference."""
    texts = _strings(fragment.stages) + _strings(fragment.contracts)
    params = {match.group(1) for text in texts for match in _PARAM_RE.finditer(text)}
    inputs = {
        match.group(1)
        for text in texts
        for match in [_INPUT_RE.match(text.strip())]
        if match
    }
    return params, inputs


def fragment_to_payload(fragment: Fragment) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "fragment_id": fragment.fragment_id,
        "definition_version": fragment.definition_version,
        "display_name": fragment.display_name,
        "params": copy.deepcopy(fragment.params),
        "inputs": copy.deepcopy(fragment.inputs),
        "contracts": copy.deepcopy(fragment.contracts),
        "stages": copy.deepcopy(fragment.stages),
    }
    if fragment.description:
        payload["description"] = fragment.description
    return payload


def canonical_fragment_json(payload: dict[str, Any]) -> str:
    canonical = fragment_to_payload(load_fragment_payload(payload))
    return json.dumps(canonical, indent=2, sort_keys=True) + "\n"


def fragment_content_revision(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_fragment_json(payload).encode("utf-8")).hexdigest()


def builtin_fragments_dir() -> Path:
    override = os.environ.get("TS_HARNESS_FRAGMENTS_DIR", "").strip()
    if override:
        return Path(override)
    root = os.environ.get("TS_HARNESS_ROOT", "").strip()
    if root:
        return Path(root) / "config" / "fragments"
    return Path(__file__).resolve().parents[1] / "config" / "fragments"


def fragment_store_root() -> Path | None:
    configured = os.environ.get("TS_HARNESS_FRAGMENT_DEFINITION_STORE", "").strip()
    return Path(configured) if configured else None


def _version_key(version: str) -> str:
    # Must match definition_paths.definition_version_key (history filenames).
    return hashlib.sha256(str(version).encode("utf-8")).hexdigest()[:32]


def _read_fragment(path: Path) -> Fragment | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return load_fragment_payload(data)


def list_builtin_fragment_ids() -> list[str]:
    root = builtin_fragments_dir()
    if not root.exists():
        return []
    return sorted(path.stem for path in root.glob("*.json"))


def resolve_fragment(fragment_id: str, version: str, *, store_root: Path | None = None) -> Fragment:
    """Load ``fragment_id`` at exactly ``version`` (built-in, then managed history)."""
    fragment_id = str(fragment_id or "").strip()
    version = str(version or "").strip()
    if not fragment_id or not version:
        raise FragmentError("fragment include requires fragment and version")
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", fragment_id):
        raise FragmentError(f"fragment id is invalid: {fragment_id}")
    builtin = builtin_fragments_dir() / f"{fragment_id}.json"
    if builtin.exists():
        fragment = _read_fragment(builtin)
        if fragment is not None and fragment.definition_version == version:
            return fragment
    # Superseded built-in versions stay resolvable for workflows that pinned them.
    if re.fullmatch(r"[A-Za-z0-9_.\-]+", version):
        superseded = builtin_fragments_dir() / "history" / fragment_id / f"{version}.json"
        if superseded.exists():
            fragment = _read_fragment(superseded)
            if fragment is not None and fragment.definition_version == version:
                return fragment
    store_root = store_root or fragment_store_root()
    if store_root is not None:
        history = store_root / "history" / fragment_id
        prefix = f"{_version_key(version)}-"
        for path in sorted(history.glob(f"{prefix}*.json")) if history.exists() else []:
            fragment = _read_fragment(path)
            if fragment is not None and fragment.definition_version == version:
                return fragment
    raise FragmentError(f"fragment not found: {fragment_id}@{version}")


def _substitute_params(value: Any, params: dict[str, str], ref: str) -> Any:
    if isinstance(value, str):
        def repl(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in params:
                raise FragmentError(f"fragment {ref} references undeclared param: {name}")
            return params[name]

        return _PARAM_RE.sub(repl, value)
    if isinstance(value, list):
        return [_substitute_params(item, params, ref) for item in value]
    if isinstance(value, dict):
        return {key: _substitute_params(item, params, ref) for key, item in value.items()}
    return value


_UNBOUND = object()


def _substitute_inputs(value: Any, inputs: dict[str, str], ref: str) -> Any:
    if isinstance(value, str):
        match = _INPUT_RE.match(value.strip())
        if match:
            return inputs.get(match.group(1), _UNBOUND)
        if "$input." in value:
            raise FragmentError(f"fragment {ref} uses $input inside a larger string: {value}")
        return value
    if isinstance(value, list):
        items = [_substitute_inputs(item, inputs, ref) for item in value]
        if any(item is _UNBOUND for item in items):
            raise FragmentError(f"fragment {ref} list references an unbound input")
        return items
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            resolved = _substitute_inputs(item, inputs, ref)
            if resolved is _UNBOUND:
                continue
            result[key] = resolved
        return result
    return value


def _rename(value: Any, renames: dict[str, str], key: str | None = None) -> Any:
    if isinstance(value, str):
        if key in STAGE_NAME_KEYS and value in renames:
            return renames[value]
        for old, new in renames.items():
            value = value.replace(f"$prior.{old}.", f"$prior.{new}.")
            if value.endswith(f"revision_count:{old}"):
                value = value[: -len(old)] + new
        return value
    if isinstance(value, list):
        if key in STAGE_NAME_LIST_KEYS:
            return [renames.get(item, item) if isinstance(item, str) else item for item in value]
        return [_rename(item, renames) for item in value]
    if isinstance(value, dict):
        return {k: _rename(v, renames, k) for k, v in value.items()}
    return value


def _string_map(raw: Any, label: str) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise FragmentError(f"{label} must be an object")
    result: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(value, str) or not value.strip():
            raise FragmentError(f"{label}.{key} must be a non-empty string")
        result[str(key)] = value.strip()
    return result


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def _expand_include(
    include: dict[str, Any],
    fragment: Fragment,
    *,
    workflow_stage_names: set[str],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], list[str]]:
    ref = fragment.ref
    name = include["name"]
    params = _string_map(include.get("params"), f"fragment include {name} params")
    inputs = _string_map(include.get("inputs"), f"fragment include {name} inputs")
    for label, supplied, declared in (
        ("param", params, fragment.params),
        ("input", inputs, fragment.inputs),
    ):
        unknown = sorted(set(supplied) - set(declared))
        if unknown:
            raise FragmentError(f"fragment include {name} sets unknown {label}s for {ref}: {', '.join(unknown)}")
        missing = sorted(key for key, spec in declared.items() if spec.get("required") and key not in supplied)
        if missing:
            raise FragmentError(f"fragment include {name} is missing required {label}s for {ref}: {', '.join(missing)}")
    for param, value in params.items():
        if fragment.params[param].get("kind") == "stage_ref" and value not in workflow_stage_names:
            raise FragmentError(f"fragment include {name} param {param} names unknown stage: {value}")

    stages = _substitute_params(copy.deepcopy(fragment.stages), params, ref)
    contracts = _substitute_params(copy.deepcopy(fragment.contracts), params, ref)
    stages = _substitute_inputs(stages, inputs, ref)

    internal = [str(stage["name"]) for stage in stages]
    prefix = str(include.get("prefix") or "").strip()
    if prefix:
        renames = {stage_name: f"{prefix}{stage_name}" for stage_name in internal}
        stages = _rename(stages, renames)
        for stage in stages:
            stage["name"] = renames[str(stage["name"])]
        internal = [renames[stage_name] for stage_name in internal]
    internal_set = set(internal)

    include_deps = [str(dep) for dep in include.get("depends_on") or []]
    for stage in stages:
        deps = [str(dep) for dep in stage.get("depends_on") or []]
        if not any(dep in internal_set for dep in deps) and stage.get("kind", "task") != "compensate":
            stage["depends_on"] = _dedupe(include_deps + deps)

    depended = {
        str(dep)
        for stage in stages
        for dep in stage.get("depends_on") or []
        if str(dep) in internal_set
    }
    exits = [
        str(stage["name"])
        for stage in stages
        if str(stage["name"]) not in depended and stage.get("kind", "task") != "compensate"
    ]
    return stages, contracts, exits


def _prepare_include(
    stage: dict[str, Any],
    resolve: Callable[[str, str], Fragment],
    *,
    workflow_stage_names: set[str],
    taken: set[str],
) -> tuple[str, Fragment, list[dict[str, Any]], dict[str, dict[str, Any]], list[str]]:
    name = str(stage.get("name") or "").strip()
    if not name:
        raise FragmentError("fragment include requires a name")
    unknown = sorted(set(stage) - INCLUDE_KEYS)
    if unknown:
        raise FragmentError(f"fragment include {name} has unknown keys: {', '.join(unknown)}")
    if name in workflow_stage_names or name in taken:
        raise FragmentError(f"fragment include name collides with a stage: {name}")
    fragment = resolve(str(stage.get("fragment") or ""), str(stage.get("version") or ""))
    stages, contracts, exits = _expand_include(
        stage,
        fragment,
        workflow_stage_names=workflow_stage_names,
    )
    return name, fragment, stages, contracts, exits


def expand_fragments(
    payload: dict[str, Any],
    *,
    resolver: Callable[[str, str], Fragment] | None = None,
) -> Expansion:
    """Return ``payload`` with every fragment include replaced by its stages."""
    raw_stages = payload.get("stages")
    if not isinstance(raw_stages, list):
        return Expansion(payload=payload, includes=[], index_map=[])
    if not any(isinstance(stage, dict) and stage.get("kind") == FRAGMENT_KIND for stage in raw_stages):
        return Expansion(
            payload=payload,
            includes=[],
            index_map=[(index, "") for index in range(len(raw_stages))],
        )
    resolve = resolver or _RESOLVER_OVERRIDE.get() or resolve_fragment
    workflow_stage_names = {
        str(stage.get("name"))
        for stage in raw_stages
        if isinstance(stage, dict) and stage.get("kind") != FRAGMENT_KIND
    }
    contracts = copy.deepcopy(dict(payload.get("contracts") or {}))
    expanded: list[dict[str, Any]] = []
    index_map: list[tuple[int, str]] = []
    includes: list[dict[str, Any]] = []
    exits_by_include: dict[str, list[str]] = {}
    for index, stage in enumerate(raw_stages):
        if not isinstance(stage, dict) or stage.get("kind") != FRAGMENT_KIND:
            expanded.append(stage)
            index_map.append((index, ""))
            continue
        try:
            name, fragment, stages, fragment_contracts, exits = _prepare_include(
                stage,
                resolve,
                workflow_stage_names=workflow_stage_names,
                taken=set(exits_by_include),
            )
            contributed: list[str] = []
            for key, contract in fragment_contracts.items():
                if key in contracts:
                    if contracts[key] != contract:
                        raise FragmentError(
                            f"fragment include {name} contract {key} conflicts with the workflow's"
                        )
                    continue
                contracts[key] = contract
                contributed.append(key)
        except FragmentError as exc:
            exc.include_index = index
            raise
        includes.append(
            {
                "index": index,
                "name": name,
                "ref": fragment.ref,
                "raw": copy.deepcopy(stage),
                "stage_names": [str(item["name"]) for item in stages],
                "contract_keys": sorted(contributed),
                "rewired": {},
            }
        )
        exits_by_include[name] = exits
        for item in stages:
            expanded.append(item)
            index_map.append((index, name))

    seen: set[str] = set()
    for item in expanded:
        stage_name = str(item.get("name") or "")
        if stage_name in seen:
            raise FragmentError(f"fragment expansion produced a duplicate stage name: {stage_name}")
        seen.add(stage_name)

    rewired: dict[str, dict[str, list[str]]] = {name: {} for name in exits_by_include}
    for position, item in enumerate(expanded):
        deps = [str(dep) for dep in item.get("depends_on") or []]
        hits = [dep for dep in deps if dep in exits_by_include]
        if not hits:
            continue
        new_deps: list[str] = []
        for dep in deps:
            new_deps.extend(exits_by_include.get(dep, [dep]))
        replacement = dict(item)
        replacement["depends_on"] = _dedupe(new_deps)
        expanded[position] = replacement
        if not index_map[position][1]:
            for hit in hits:
                rewired[hit][str(item.get("name"))] = deps
    for include in includes:
        include["rewired"] = rewired.get(include["name"], {})

    result = dict(payload)
    result["stages"] = expanded
    result["contracts"] = contracts
    return Expansion(payload=result, includes=includes, index_map=index_map)
