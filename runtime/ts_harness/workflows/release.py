"""Release on the program graph: plan, promote, trigger and record deployments.

TS Harness never touches infrastructure. A promote gate approves one environment,
``trigger_deploy`` asks the target repo's GitHub automation to deploy (a
``workflow_dispatch`` run or a Deployments API deployment), and
``record_release`` stores the terminal deployment status as release evidence.

``deploy_release`` promotes through two environment slots, ``staging`` then
``production``. ``release.environments`` maps a repo to the two environments
that fill those slots; without an entry the slots keep their names and use
Deployments API triggers.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from ts_harness.config.bindings import check_repo_allowed, tenant_bindings
from ts_harness.integrations.github_client import (
    GitHubClient,
    GitHubError,
    parse_github_pr_url,
    parse_repo_slug,
)
from ts_harness.workflows.expressions import resolve_value
from ts_harness.workflows.hooks._helpers import load_workflow_payload

logger = logging.getLogger(__name__)

DEPLOYMENT_EVENT = "github.deployment_status"
CHECKS_EVENT = "github.pr_checks"
SLOTS = ("staging", "production")
DEFAULT_ENVIRONMENTS: tuple[dict[str, Any], ...] = (
    {"name": "staging", "trigger": {"kind": "deployment"}, "required_roles": [], "requires": None},
    {"name": "production", "trigger": {"kind": "deployment"}, "required_roles": [], "requires": "staging"},
)
TERMINAL_DEPLOYMENT_STATES = frozenset({"success", "failure", "error", "inactive"})


class ReleaseBlocked(Exception):
    """A release step cannot proceed; the task blocks with this reason."""


def _live_program_defaults() -> dict[str, Any]:
    try:
        from ts_harness.gateway.server import get_runtime

        return dict(getattr(get_runtime().config, "program_defaults", None) or {})
    except Exception:
        return {}


def environments_for(repo: str, program_defaults: dict[str, Any] | None) -> list[dict[str, Any]]:
    by_repo = dict(((program_defaults or {}).get("release") or {}).get("environments") or {})
    configured = by_repo.get(str(repo or "").lower())
    if configured:
        return [dict(env) for env in configured]
    return [dict(env, trigger=dict(env["trigger"])) for env in DEFAULT_ENVIRONMENTS]


def resolve_repo(task_input: dict[str, Any]) -> str:
    """``owner/repo`` from ``repo_url``, else from the first PR URL."""
    slug = parse_repo_slug(str(task_input.get("repo_url") or task_input.get("repo") or ""))
    if slug is None:
        urls = list(task_input.get("pr_urls") or [])
        if task_input.get("pr_url"):
            urls.append(task_input["pr_url"])
        for url in urls:
            parsed = parse_github_pr_url(str(url))
            if parsed:
                slug = (parsed[0], parsed[1])
                break
    if slug is None:
        raise ReleaseBlocked("release needs repo_url or a GitHub PR URL to resolve the repo")
    return f"{slug[0]}/{slug[1]}".lower()


def _split(repo: str) -> tuple[str, str]:
    owner, _, name = repo.partition("/")
    return owner, name


def _task_input(task: dict[str, Any]) -> dict[str, Any]:
    raw = task.get("input")
    return dict(raw) if isinstance(raw, dict) else {}


def _latest_step(tasks: list[dict[str, Any]], step_name: str, *, succeeded: bool = True) -> dict[str, Any] | None:
    matches = [
        task
        for task in tasks
        if str(task.get("step_name") or "") == step_name
        and (not succeeded or task.get("status") == "succeeded")
    ]
    return matches[-1] if matches else None


def _step_output(tasks: list[dict[str, Any]], step_name: str) -> dict[str, Any]:
    task = _latest_step(tasks, step_name)
    if task is None:
        raise ReleaseBlocked(f"{step_name} has not succeeded")
    output = task.get("output")
    return dict(output) if isinstance(output, dict) else {}


def _last_success(memory: Any, *, repo: str, environment: str, tenant_id: str, exclude_sha: str | None = None) -> dict[str, Any] | None:
    result = memory.last_successful_release(
        repo=repo, environment=environment, tenant_id=tenant_id, exclude_sha=exclude_sha
    )
    release = (result or {}).get("release")
    return dict(release) if isinstance(release, dict) else None


def _slot_plan(
    env: dict[str, Any],
    *,
    memory: Any,
    repo: str,
    sha: str,
    tenant_id: str,
) -> dict[str, Any]:
    previous = _last_success(memory, repo=repo, environment=env["name"], tenant_id=tenant_id, exclude_sha=sha)
    return {
        "environment": env["name"],
        "trigger": dict(env.get("trigger") or {"kind": "deployment"}),
        "required_roles": list(env.get("required_roles") or []),
        "required_permission": str(env.get("required_permission") or ""),
        "requires": env.get("requires"),
        "rollback_sha": (previous or {}).get("sha"),
    }


def _patch_gate_policy(
    memory: Any,
    gate: dict[str, Any],
    *,
    slot: dict[str, Any],
    repo: str,
    sha: str,
    actor: str,
) -> None:
    task_input = _task_input(gate)
    policy = dict(task_input.get("_approval_policy") or {})
    roles = sorted({*policy.get("required_roles", []), *slot["required_roles"]})
    if roles:
        policy["required_roles"] = roles
    extra = str(slot.get("required_permission") or "").strip()
    if extra:
        policy["required_permission"] = extra
    policy["decision_kind"] = "promote"
    policy["require_dependencies_succeeded"] = True
    policy["details"] = {
        **dict(policy.get("details") or {}),
        "environment": slot["environment"],
        "sha": sha,
        "repo": repo,
        "rollback_sha": slot.get("rollback_sha"),
    }
    memory.patch_task_input(str(gate["id"]), {**task_input, "_approval_policy": policy}, actor=actor)


def plan_release(
    task: dict[str, Any],
    config: dict[str, Any],
    *,
    memory: Any,
    program_defaults: dict[str, Any] | None = None,
    actor: str = "workflow-runner",
) -> dict[str, Any]:
    """Resolve repo, SHA and environments, and pin them onto the promote gates."""
    workflow, tasks = load_workflow_payload(memory, str(task.get("workflow_id") or ""))
    tenant_id = str(task.get("tenant_id") or workflow.get("tenant_id") or "default")
    stored = next((item for item in tasks if str(item.get("id")) == str(task.get("id"))), {})
    task_input = {**_task_input(stored), **_task_input(task)}
    repo = resolve_repo(task_input)
    defaults = program_defaults if program_defaults is not None else _live_program_defaults()
    binding_error = check_repo_allowed(repo, tenant_bindings(defaults))
    if binding_error:
        raise ReleaseBlocked(binding_error)
    environments = environments_for(repo, defaults)
    gates = dict(config.get("gates") or {})
    mode = str(config.get("mode") or "promote")

    if mode == "rollback":
        name = str(task_input.get("rollback_environment") or "").strip().lower()
        env = next((item for item in environments if item["name"] == name), None)
        if env is None:
            known = ", ".join(item["name"] for item in environments)
            raise ReleaseBlocked(f"rollback_environment must be one of: {known}")
        sha = str(task_input.get("rollback_sha") or "").strip()
        current = _last_success(memory, repo=repo, environment=name, tenant_id=tenant_id)
        if not sha:
            if current is None:
                raise ReleaseBlocked(f"no successful release of {repo} to {name} to roll back from")
            target = _last_success(memory, repo=repo, environment=name, tenant_id=tenant_id, exclude_sha=current["sha"])
            if target is None:
                raise ReleaseBlocked(f"no earlier successful release of {repo} to {name} to roll back to")
            sha = str(target["sha"])
        slots = {
            "target": {
                "environment": name,
                "trigger": dict(env.get("trigger") or {"kind": "deployment"}),
                "required_roles": list(env.get("required_roles") or []),
                "requires": None,
                "rollback_sha": (current or {}).get("sha"),
            }
        }
    else:
        sha = str(task_input.get("merge_sha") or task_input.get("sha") or "").strip()
        if not sha:
            raise ReleaseBlocked("release needs merge_sha")
        if len(environments) != len(SLOTS):
            raise ReleaseBlocked(
                f"deploy_release promotes through {len(SLOTS)} environments; {repo} has {len(environments)} configured"
            )
        slots = {
            slot: _slot_plan(env, memory=memory, repo=repo, sha=sha, tenant_id=tenant_id)
            for slot, env in zip(SLOTS, environments)
        }

    for slot_name, gate_step in gates.items():
        slot = slots.get(slot_name)
        gate = _latest_step(tasks, str(gate_step), succeeded=False)
        if slot is None or gate is None:
            raise ReleaseBlocked(f"promote gate {gate_step} for slot {slot_name} is missing")
        _patch_gate_policy(memory, gate, slot=slot, repo=repo, sha=sha, actor=actor)

    return {
        "mode": mode,
        "repo": repo,
        "sha": sha,
        "base_branch": str(task_input.get("base_branch") or "").strip() or None,
        "pr_urls": list(task_input.get("pr_urls") or []),
        "slots": slots,
        "environments": [slot["environment"] for slot in slots.values()],
    }


def _promote_decision(memory: Any, *, workflow_id: str, gate: dict[str, Any]) -> dict[str, Any]:
    decisions = list((memory.list_decisions(workflow_id=workflow_id, kind="promote") or {}).get("decisions") or [])
    approved = [
        item
        for item in decisions
        if str(item.get("task_id") or "") == str(gate.get("id")) and item.get("decision") == "approved"
    ]
    if not approved:
        raise ReleaseBlocked(f"no approved promote decision recorded for {gate.get('step_name')}")
    return dict(approved[-1])


def _trigger(
    github: GitHubClient,
    *,
    repo: str,
    sha: str,
    slot: dict[str, Any],
    base_branch: str | None,
    payload: dict[str, Any],
) -> dict[str, Any]:
    owner, name = _split(repo)
    trigger = dict(slot.get("trigger") or {"kind": "deployment"})
    environment = slot["environment"]
    if trigger.get("kind") == "workflow_dispatch":
        ref = base_branch or str((github.request("GET", f"/repos/{owner}/{name}") or {}).get("default_branch") or "main")
        inputs = {str(trigger.get("ref_input") or "sha"): sha, "environment": environment}
        github.dispatch_workflow(owner, name, str(trigger["workflow"]), ref=ref, inputs=inputs)
        return {"kind": "workflow_dispatch", "workflow": trigger["workflow"], "ref": ref, "inputs": inputs, "deployment_id": None}
    created = github.create_deployment(
        owner,
        name,
        ref=sha,
        environment=environment,
        description=f"Claw release {sha[:12]} to {environment}",
        payload=payload,
    )
    return {"kind": "deployment", "deployment_id": created.get("id")}


def trigger_deploy(
    task: dict[str, Any],
    config: dict[str, Any],
    *,
    memory: Any,
    github: GitHubClient | None = None,
    actor: str = "workflow-runner",
) -> dict[str, Any]:
    """Record the release as in progress and ask GitHub to deploy the pinned SHA."""
    workflow_id = str(task.get("workflow_id") or "")
    workflow, tasks = load_workflow_payload(memory, workflow_id)
    program_id = str(workflow.get("program_id") or "")
    if not program_id:
        raise ReleaseBlocked("release workflows must belong to a program")
    plan = _step_output(tasks, str(config.get("plan_step") or "plan_release"))
    slot_name = str(config.get("slot") or "")
    slot = dict((plan.get("slots") or {}).get(slot_name) or {})
    if not slot:
        raise ReleaseBlocked(f"plan has no environment for slot {slot_name}")
    gate = _latest_step(tasks, str(config.get("gate") or ""))
    if gate is None:
        raise ReleaseBlocked(f"promote gate {config.get('gate')} has not been approved")
    decision = _promote_decision(memory, workflow_id=workflow_id, gate=gate)
    promoted_by = str(decision.get("actor_email") or decision.get("actor_id") or "")
    repo, sha = str(plan["repo"]), str(plan["sha"])
    is_rollback = plan.get("mode") == "rollback"

    recorded = memory.record_release(
        program_id,
        repo=repo,
        sha=sha,
        environment=slot["environment"],
        status="in_progress",
        workflow_id=workflow_id,
        promoted_by=promoted_by,
        decision_id=decision.get("id"),
        rollback_sha=slot.get("rollback_sha"),
        is_rollback=is_rollback,
        actor=actor,
    )
    release_id = str(recorded.get("id") or "")
    github = github or GitHubClient.from_env()
    try:
        triggered = _trigger(
            github,
            repo=repo,
            sha=sha,
            slot=slot,
            base_branch=plan.get("base_branch"),
            payload={"program_id": program_id, "workflow_id": workflow_id, "release_id": release_id},
        )
    except GitHubError as exc:
        memory.record_release(program_id, id=release_id, status="failed", actor=actor)
        raise ReleaseBlocked(f"deploy trigger for {slot['environment']} failed: {exc}") from exc

    deployment_ref = str(triggered.get("deployment_id") or "") or f"workflow_dispatch:{triggered.get('workflow')}@{triggered.get('ref')}"
    memory.record_release(program_id, id=release_id, status="in_progress", deployment_ref=deployment_ref, actor=actor)
    try:
        memory.record_program_external_ref(
            program_id,
            kind="deployment",
            ref=f"{repo}@{sha}:{slot['environment']}",
            state="open",
            workflow_id=workflow_id,
            metadata={"release_id": release_id, "deployment_ref": deployment_ref, "trigger": triggered["kind"]},
            actor=actor,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("deployment ref not recorded for release %s: %s", release_id, exc)
    return {
        "release_id": release_id,
        "repo": repo,
        "sha": sha,
        "environment": slot["environment"],
        "trigger": triggered["kind"],
        "deployment_id": triggered.get("deployment_id"),
        "deployment_ref": deployment_ref,
        "promoted_by": promoted_by,
        "decision_id": decision.get("id"),
        "rollback_sha": slot.get("rollback_sha"),
    }


def record_release(
    task: dict[str, Any],
    config: dict[str, Any],
    *,
    memory: Any,
    actor: str = "workflow-runner",
) -> dict[str, Any]:
    """Store the terminal deployment status; a failed deployment blocks with rollback guidance."""
    workflow_id = str(task.get("workflow_id") or "")
    workflow, tasks = load_workflow_payload(memory, workflow_id)
    program_id = str(workflow.get("program_id") or "")
    triggered = _step_output(tasks, str(config.get("trigger_step") or ""))
    outcome = _step_output(tasks, str(config.get("wait_step") or ""))
    state = str(outcome.get("state") or "").lower()
    status = "succeeded" if state == "success" else "failed"
    evidence_url = str(outcome.get("target_url") or "") or None
    memory.record_release(
        program_id,
        id=triggered["release_id"],
        status=status,
        evidence_url=evidence_url,
        deployment_ref=str(outcome.get("deployment_id") or "") or None,
        actor=actor,
    )
    try:
        memory.record_program_external_ref(
            program_id,
            kind="deployment",
            ref=f"{triggered['repo']}@{triggered['sha']}:{triggered['environment']}",
            state=status,
            workflow_id=workflow_id,
            metadata={"release_id": triggered["release_id"], "evidence_url": evidence_url or ""},
            actor=actor,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("deployment ref state not recorded for release %s: %s", triggered["release_id"], exc)
    if status == "failed":
        rollback = triggered.get("rollback_sha")
        guidance = (
            f"; roll back to {rollback} by starting the program's rollback node for {triggered['environment']}"
            if rollback
            else "; no earlier successful release to roll back to"
        )
        raise ReleaseBlocked(f"deployment to {triggered['environment']} ended {state or 'unknown'}{guidance}")
    return {
        "release_id": triggered["release_id"],
        "environment": triggered["environment"],
        "sha": triggered["sha"],
        "status": status,
        "evidence_url": evidence_url,
    }


SYSTEM_STEPS: dict[str, Callable[..., dict[str, Any]]] = {
    "plan_release": plan_release,
    "trigger_deploy": trigger_deploy,
    "record_release": record_release,
}


def run_system_step(hook: str, task: dict[str, Any], config: dict[str, Any], *, memory: Any) -> dict[str, Any]:
    from ts_harness.workflows.system_steps import run_system_step as run

    return run(hook, task, config, memory=memory)


def deployment_state(
    github: GitHubClient,
    *,
    repo: str,
    sha: str,
    environment: str,
    deployment_id: Any = None,
) -> dict[str, Any] | None:
    """Latest status of the deployment for ``sha`` in ``environment``, or ``None``."""
    owner, name = _split(repo)
    if not deployment_id:
        deployments = github.list_deployments(owner, name, sha=sha, environment=environment)
        if not deployments:
            return None
        deployment_id = max(deployments, key=lambda item: int(item.get("id") or 0)).get("id")
    statuses = github.deployment_statuses(owner, name, deployment_id)
    if not statuses:
        return {"state": "pending", "deployment_id": deployment_id}
    latest = max(statuses, key=lambda item: int(item.get("id") or 0))
    return {
        "state": str(latest.get("state") or "").lower(),
        "deployment_id": deployment_id,
        "target_url": str(latest.get("target_url") or latest.get("environment_url") or ""),
    }


def deployment_payload(wait_match: dict[str, Any], repo: str, state: dict[str, Any]) -> dict[str, Any]:
    return {
        "sha": wait_match.get("sha"),
        "environment": wait_match.get("environment"),
        "state": state.get("state"),
        "deployment_id": state.get("deployment_id"),
        "target_url": state.get("target_url") or "",
        "repo": repo,
        "creator": "",
    }


def prepare_deployment_wait(
    task: dict[str, Any],
    config: dict[str, Any],
    *,
    memory: Any,
    github: GitHubClient | None = None,
    actor: str = "workflow-runner",
) -> dict[str, Any]:
    """Pin a deployment wait to the triggered SHA and environment."""
    _, tasks = load_workflow_payload(memory, str(task.get("workflow_id") or ""))
    spec = dict(config.get("deployment") or {})
    triggered = _step_output(tasks, str(spec.get("trigger_step") or ""))
    match = {"sha": triggered["sha"], "environment": triggered["environment"]}
    options = {"repo": triggered["repo"], "deployment_id": triggered.get("deployment_id"), "release_id": triggered["release_id"]}
    memory.patch_task_input(
        str(task["id"]),
        {**_task_input(task), "_wait_match": match, "_wait_options": options},
        actor=actor,
    )
    result: dict[str, Any] = {"match": match, "options": options}
    github = github or GitHubClient.from_env()
    state = deployment_state(
        github, repo=triggered["repo"], sha=match["sha"], environment=match["environment"], deployment_id=options["deployment_id"]
    )
    if state and state["state"] in TERMINAL_DEPLOYMENT_STATES:
        result["ready"] = deployment_payload(match, triggered["repo"], state)
    return result


def prepare_sha_checks_wait(
    task: dict[str, Any],
    config: dict[str, Any],
    *,
    memory: Any,
    github: GitHubClient | None = None,
    program_defaults: dict[str, Any] | None = None,
    actor: str = "workflow-runner",
) -> dict[str, Any]:
    """Pin a checks wait to a commit SHA (CI on the merged commit)."""
    from ts_harness.workflows.merge_readiness import (
        TERMINAL_STATES,
        aggregate_checks,
        checks_payload,
        required_checks_for,
    )

    workflow, tasks = load_workflow_payload(memory, str(task.get("workflow_id") or ""))
    spec = dict(config.get("sha_checks") or {})
    sha = str(resolve_value(spec.get("sha"), tasks=tasks, workflow=workflow) or "").strip()
    repo = str(resolve_value(spec.get("repo"), tasks=tasks, workflow=workflow) or "").strip().lower()
    if not sha or parse_repo_slug(repo) is None:
        raise ValueError("await_ci_on_sha needs a resolved sha and owner/repo")
    defaults = program_defaults if program_defaults is not None else _live_program_defaults()
    head = {"pr_url": "", "head_sha": sha, "repo": repo, "required_checks": required_checks_for(repo, defaults)}
    match = {"head_sha": sha}
    options = {"heads": [head], "warnings": []}
    memory.patch_task_input(
        str(task["id"]),
        {**_task_input(task), "_wait_match": match, "_wait_options": options},
        actor=actor,
    )
    aggregate = aggregate_checks(github or GitHubClient.from_env(), [head])
    result: dict[str, Any] = {"match": match, "options": options, "summary": aggregate}
    if aggregate["state"] in TERMINAL_STATES:
        result["ready"] = checks_payload(aggregate)
    return result
