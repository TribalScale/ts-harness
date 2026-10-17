"""Automated release-gate smoke runner for TS Harness cutover."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml

from ts_harness.repo_paths import config_dir


Check = Callable[[], bool]


@dataclass
class GateSpec:
    id: str
    tier: str = "required"
    requires_env: tuple[str, ...] = ()
    metadata: dict = field(default_factory=dict)


@dataclass
class GateResult:
    name: str
    status: str
    detail: str = ""


@dataclass
class ReleaseGateReport:
    results: dict[str, GateResult]

    @property
    def passed(self) -> bool:
        for result in self.results.values():
            if result.status in {"failed", "missing"}:
                return False
        return True

    @property
    def required_passed(self) -> bool:
        for result in self.results.values():
            if result.status in {"failed", "missing"}:
                return False
        return True


def _default_manifest_path() -> Path:
    return config_dir() / "release-gates.yaml"


def _parse_gate_entries(entries: list, *, tier: str) -> list[GateSpec]:
    specs: list[GateSpec] = []
    seen: set[str] = set()
    for entry in entries or []:
        if isinstance(entry, str):
            gate_id = entry
            requires_env: tuple[str, ...] = ()
            metadata: dict = {}
        elif isinstance(entry, dict):
            gate_id = str(entry.get("id") or "")
            requires_env = tuple(str(v) for v in (entry.get("requires_env") or []) if str(v).strip())
            metadata = {k: v for k, v in entry.items() if k not in {"id", "requires_env"}}
        else:
            continue
        if not gate_id or gate_id in seen:
            continue
        seen.add(gate_id)
        specs.append(GateSpec(id=gate_id, tier=tier, requires_env=requires_env, metadata=metadata))
    return specs


def load_gate_manifest(manifest_path: Path | None = None) -> list[GateSpec]:
    path = manifest_path or _default_manifest_path()
    if not path.exists():
        return [
            GateSpec(gate_id, tier="required")
            for gate_id in ReleaseGateRunner.DEFAULT_REQUIRED_GATES
        ]
    data = yaml.safe_load(path.read_text()) or {}
    specs: list[GateSpec] = []
    specs.extend(_parse_gate_entries(data.get("required") or [], tier="required"))
    specs.extend(_parse_gate_entries(data.get("optional") or [], tier="optional"))
    specs.extend(_parse_gate_entries(data.get("post_deploy") or [], tier="post_deploy"))
    return specs


def load_required_gates(manifest_path: Path | None = None) -> tuple[str, ...]:
    return tuple(spec.id for spec in load_gate_manifest(manifest_path) if spec.tier == "required")


def gates_for_profile(profile: str, manifest: list[GateSpec] | None = None) -> list[GateSpec]:
    manifest = manifest or load_gate_manifest()
    profile = (profile or "full").strip().lower()
    if profile == "ci":
        return [spec for spec in manifest if spec.tier == "required"]
    if profile == "live":
        return [spec for spec in manifest if spec.id in {"agent_redteam", "agent_output_judge"}]
    if profile == "post_deploy":
        return list(manifest)
    # full: required + optional (post_deploy excluded unless explicitly requested)
    return [spec for spec in manifest if spec.tier in {"required", "optional"}]


def env_requirements_met(spec: GateSpec) -> bool:
    if not spec.requires_env:
        return True
    return all(os.environ.get(name, "").strip() for name in spec.requires_env)


class ReleaseGateRunner:
    DEFAULT_REQUIRED_GATES = (
        "runtime_health",
        "memory_smoke",
        "workflow_dispatch",
        "slack_round_trip",
        "mcp_discovery",
        "sandbox_denial",
        "subagent_delegation",
        "cron_heartbeat",
        "tenant_isolation",
        "rbac_identity",
        "audit_export",
        "secrets_redaction",
        "agent_harness_security",
        "agent_harness_quality",
        "budget_hard_stop",
        "channel_chat",
        "workflow_record_create",
        "workflow_promotion_smoke",
        "workflow_hook_smoke",
        "prd_request_smoke",
        "continue_bridge_smoke",
        "intent_routing_smoke",
        "workflow_runner_import_smoke",
        "delivery_program",
        "skeleton_hooks_registered",
    )

    def __init__(
        self,
        checks: dict[str, Check],
        *,
        required_gates: tuple[str, ...] | None = None,
        profile: str | None = None,
        manifest: list[GateSpec] | None = None,
    ):
        self.checks = checks
        self.profile = profile or os.environ.get("RELEASE_GATES_PROFILE", "full")
        self.manifest = manifest or load_gate_manifest()
        if required_gates is not None:
            self.gate_specs = [GateSpec(gate_id, tier="required") for gate_id in required_gates]
        else:
            self.gate_specs = gates_for_profile(self.profile, self.manifest)

    def run(self) -> ReleaseGateReport:
        results: dict[str, GateResult] = {}
        for spec in self.gate_specs:
            gate = spec.id
            if not env_requirements_met(spec):
                missing = f"missing env: {', '.join(spec.requires_env)}"
                status = "failed" if self.profile == "live" else "skipped"
                results[gate] = GateResult(gate, status, missing)
                continue
            check = self.checks.get(gate)
            if check is None:
                results[gate] = GateResult(gate, "missing", "no check registered")
                continue
            try:
                ok = bool(check())
            except Exception as exc:  # noqa: BLE001
                results[gate] = GateResult(gate, "failed", str(exc))
                continue
            results[gate] = GateResult(gate, "passed" if ok else "failed")
        return ReleaseGateReport(results)
