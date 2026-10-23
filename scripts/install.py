#!/usr/bin/env python3
"""Unified AWS/GCP installer CLI for TS Harness.

Usage:
  python scripts/install.py validate --cloud aws --config config/install/aws.example.yaml
  ./scripts/install.sh plan --cloud gcp --config path/to/gcp.yaml
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

import env_aliases  # noqa: F401
from install_checks import (
    CheckResult,
    format_check_results,
    preflight_failed,
    read_state_metadata,
    run_preflight,
)
from install_contract import InstallContractError, load_install_config
from install_post_apply import render_post_apply_next_steps

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_BASE = REPO_ROOT / ".install"
METADATA_NAME = "install-metadata.json"
TFVARS_NAME = "install.auto.tfvars.json"
INSTALL_CONTRACT_VERSION = "0.1.0"


def select_terraform_root(cloud: str, *, repo_root: Path | None = None) -> Path:
    """Return exactly one terraform root; never both."""
    if cloud not in ("aws", "gcp"):
        raise ValueError(f"Unsupported cloud: {cloud!r}")
    root = (repo_root or REPO_ROOT) / "infra" / cloud
    return root


def resolve_state_dir(
    *,
    config: Mapping[str, Any] | None,
    state_dir: str | Path | None,
    base: Path | None = None,
    cloud: str | None = None,
) -> Path:
    """Resolve local install metadata directory.

    If --state-dir points at an env directory (contains metadata) use it.
    If --state-dir is a base (e.g. .install), append cloud/environment.
    Default: .install/<cloud_provider>/<environment>/
    """
    provider = (
        str(cloud or (config or {}).get("cloud_provider") or "unknown").strip().lower()
        or "unknown"
    )
    env = str((config or {}).get("environment") or "default").strip() or "default"

    if state_dir is not None:
        path = Path(state_dir)
        if (path / METADATA_NAME).is_file() or config is None:
            return path
        # Base dir: nest cloud then environment so aws/gcp plans do not collide.
        if path.name == env:
            return path
        if path.name == provider:
            return path / env
        return path / provider / env

    return (base or DEFAULT_STATE_BASE) / provider / env


def terraform_state_identity(config: Mapping[str, Any]) -> str:
    state = config.get("terraform_state") or {}
    backend = state.get("backend_type", "")
    bucket = state.get("bucket", "")
    key = state.get("key", "")
    if backend == "s3":
        return f"s3://{bucket}/{key}"
    if backend == "gcs":
        return f"gs://{bucket}/{key}"
    return f"{backend}:{bucket}/{key}"


def backend_config_args(config: Mapping[str, Any]) -> list[str]:
    """Partial backend settings for `terraform init`, taken from terraform_state."""
    state = config.get("terraform_state") or {}
    if state.get("backend_type") == "gcs":
        pairs = [("bucket", state["bucket"]), ("prefix", state["key"])]
    else:
        pairs = [
            ("bucket", state["bucket"]),
            ("key", state["key"]),
            ("region", state["region"]),
            ("encrypt", "true"),
        ]
        if state.get("lock_table"):
            pairs.append(("dynamodb_table", state["lock_table"]))
        else:
            pairs.append(("use_lockfile", "true"))
    return [f"-backend-config={k}={v}" for k, v in pairs]


def account_or_project(config: Mapping[str, Any]) -> str:
    provider = config.get("cloud_provider")
    if provider == "aws":
        return str(config.get("aws_account_id") or "")
    return str(config.get("gcp_project_id") or "")


def write_metadata(state_dir: Path, payload: Mapping[str, Any]) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / METADATA_NAME
    existing: dict[str, Any] = {}
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
    merged = {**existing, **dict(payload)}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def generate_tfvars(config: Mapping[str, Any], *, cloud: str) -> dict[str, Any]:
    """Map install-contract YAML to provider-root terraform variables."""
    vm = config.get("vm") or {}
    db = config.get("managed_db") or {}
    runtime = config.get("runtime") or {}
    if cloud == "aws":
        tfvars: dict[str, Any] = {
            "aws_region": config["cloud_region"],
            "aws_account_id": config.get("aws_account_id"),
            "project_name": config["project_name"],
            "secret_prefix": config.get("secret_prefix"),
            "instance_type": vm.get("instance_type"),
            "root_volume_size": vm.get("root_volume_size_gb"),
            "ebs_size": vm.get("data_disk_size_gb"),
            "admin_username": vm.get("admin_username"),
            "ssh_key_name": vm.get("ssh_key_name"),
            "ssh_allowed_cidrs": list(vm.get("ssh_allowed_cidrs") or []),
            "memory_backend": "rds",
            "rds_instance_class": db.get("instance_class"),
            "rds_backup_retention_days": db.get("backup_retention_days"),
            "rds_multi_az": bool(db.get("multi_az", True)),
            "rds_apply_immediately": bool(db.get("apply_immediately", False)),
            "rds_skip_final_snapshot": bool(db.get("skip_final_snapshot", False)),
            "monthly_budget_limit": config.get("monthly_budget_limit"),
            "budget_alert_email": config.get("notification_email"),
            "agent_runtime_version": runtime.get("agent_runtime_version"),
            "enterprise_repo_url": runtime.get("enterprise_repo_url"),
            "multi_agent_enabled": bool(runtime.get("multi_agent_enabled", False)),
            "workflow_runner_enabled": bool(runtime.get("workflow_runner_enabled", False)),
            "coding_jobs_enabled": bool(runtime.get("coding_jobs_enabled", False)),
            "coding_max_running": int(runtime.get("coding_max_running", 3)),
            "embeddings_provider": runtime.get("embeddings_provider"),
            "embeddings_model": runtime.get("embeddings_model"),
            "embeddings_dimensions": runtime.get("embeddings_dimensions"),
            "embeddings_fallback": runtime.get("embeddings_fallback", "none"),
            "agent_timezone": runtime.get("agent_timezone", "UTC"),
            "deployment_profile": config.get("deployment_profile", "single_host"),
        }
        scale_out = config.get("scale_out") or {}
        profile = tfvars["deployment_profile"]
        tfvars["network_mode"] = scale_out.get(
            "network_mode", "vpc" if profile == "scale_out" else "default"
        )
        for key in (
            "vpc_cidr",
            "coding_worker_image_tag",
            "coding_worker_cpu",
            "coding_worker_memory",
            "coding_worker_ephemeral_storage_gib",
            "memory_api_certificate_arn",
            "egress_allowed_domains",
        ):
            if key in scale_out:
                tfvars[key] = scale_out[key]
        github_app = config.get("github_app") or {}
        for key in ("app_id", "slug", "host"):
            if github_app.get(key):
                tfvars[f"github_app_{key}"] = str(github_app[key])
        image_tag = os.environ.get("TS_HARNESS_CODING_WORKER_IMAGE_TAG", "").strip()
        if image_tag:
            tfvars["coding_worker_image_tag"] = image_tag
        if db.get("backup_window"):
            tfvars["rds_backup_window"] = db["backup_window"]
        if vm.get("tailscale_authkey_secret_ref"):
            tfvars["tailscale_authkey_secret_arn"] = vm["tailscale_authkey_secret_ref"]
        for key in (
            "workflow_runner_poll_interval_seconds",
            "workflow_runner_batch_size",
            "workflow_runner_lease_seconds",
            "workflow_runner_stale_policy",
            "implement_work_orders_repo_url",
            "implement_work_orders_base_branch",
            "slack_standup_channel",
            "slack_ops_channel",
            "slack_dev_channel",
            "slack_team_channel",
            "slack_marketing_channel",
            "slack_sales_channel",
            "approved_slack_channels",
            "identity_provider",
            "oidc_issuer",
            "oidc_audience",
            "oidc_jwks_url",
            "oidc_client_id",
            "oidc_authorization_endpoint",
            "oidc_token_endpoint",
            "oidc_preset",
            "oidc_token_type",
            "oidc_groups_claim",
            "oidc_roles_claim",
            "oidc_allowed_domains",
            "oidc_groups_source",
            "identity_directory",
        ):
            if key in runtime and runtime[key] is not None:
                tfvars[key] = runtime[key]
        _storage_backend_tfvars(tfvars, runtime)
        return {k: v for k, v in tfvars.items() if v is not None}

    # GCP root uses install-contract variable names.
    ssh_public_key = os.environ.get("TS_HARNESS_SSH_PUBLIC_KEY", "")
    tfvars = {
        "cloud_provider": "gcp",
        "environment": config.get("environment"),
        "project_name": config["project_name"],
        "gcp_project_id": config["gcp_project_id"],
        "cloud_region": config["cloud_region"],
        "cloud_zone": config["cloud_zone"],
        "secret_prefix": config.get("secret_prefix"),
        "notification_email": config.get("notification_email"),
        "monthly_budget_limit": config.get("monthly_budget_limit"),
        "vm_instance_type": vm.get("instance_type"),
        "vm_root_volume_size_gb": vm.get("root_volume_size_gb"),
        "vm_data_disk_size_gb": vm.get("data_disk_size_gb"),
        "admin_username": vm.get("admin_username"),
        "ssh_allowed_cidrs": list(vm.get("ssh_allowed_cidrs") or []),
        "ssh_public_key": ssh_public_key,
        "memory_backend": "managed",
        "managed_db_instance_class": db.get("instance_class"),
        "managed_db_name": db.get("db_name"),
        "managed_db_user": db.get("db_username"),
        "managed_db_backup_retention_days": db.get("backup_retention_days"),
        "cloud_sql_availability_type": "REGIONAL" if db.get("multi_az", True) else "ZONAL",
        "agent_runtime_version": runtime.get("agent_runtime_version"),
        "enterprise_repo_url": runtime.get("enterprise_repo_url"),
        "multi_agent_enabled": bool(runtime.get("multi_agent_enabled", False)),
        "workflow_runner_enabled": bool(runtime.get("workflow_runner_enabled", False)),
        "coding_jobs_enabled": bool(runtime.get("coding_jobs_enabled", False)),
        "coding_max_running": int(runtime.get("coding_max_running", 3)),
        "embeddings_provider": runtime.get("embeddings_provider"),
        "embeddings_model": runtime.get("embeddings_model"),
        "embeddings_dimensions": runtime.get("embeddings_dimensions"),
        "embeddings_fallback": runtime.get("embeddings_fallback", "none"),
        "agent_timezone": runtime.get("agent_timezone", "UTC"),
    }
    for key in (
        "workflow_runner_poll_interval_seconds",
        "workflow_runner_batch_size",
        "workflow_runner_lease_seconds",
        "workflow_runner_stale_policy",
        "implement_work_orders_repo_url",
        "implement_work_orders_base_branch",
        "slack_standup_channel",
        "slack_ops_channel",
        "slack_dev_channel",
        "slack_team_channel",
        "slack_marketing_channel",
        "slack_sales_channel",
        "approved_slack_channels",
        "identity_provider",
        "oidc_issuer",
        "oidc_audience",
        "oidc_jwks_url",
        "oidc_client_id",
        "oidc_authorization_endpoint",
        "oidc_token_endpoint",
        "oidc_preset",
        "oidc_token_type",
        "oidc_groups_claim",
        "oidc_roles_claim",
        "oidc_allowed_domains",
        "oidc_groups_source",
        "identity_directory",
    ):
        if key in runtime and runtime[key] is not None:
            tfvars[key] = runtime[key]
    _storage_backend_tfvars(tfvars, runtime)
    return {k: v for k, v in tfvars.items() if v is not None}


def _storage_backend_tfvars(tfvars: dict[str, Any], runtime: Mapping[str, Any]) -> None:
    """Pass state_backend and object_backend only when set; blank keeps the provider root's default."""
    for key in ("state_backend", "object_backend"):
        value = str(runtime.get(key) or "").strip()
        if value:
            tfvars[key] = value


def write_tfvars(path: Path, tfvars: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(tfvars), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def terraform_bin() -> str:
    return os.environ.get("TS_HARNESS_TF_BIN") or os.environ.get("TF_CLI") or "terraform"


def run_terraform(
    cmd: Sequence[str],
    *,
    cwd: Path,
    dry_run: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a terraform command (mockable via TS_HARNESS_TF_BIN / patch).

    Output streams to the caller's terminal; long applies (RDS) must not look hung in CI.
    """
    full = list(cmd)
    if dry_run:
        print(f"[dry-run] would run in {cwd}: {' '.join(full)}")
        return subprocess.CompletedProcess(full, 0, stdout="", stderr="")
    sys.stdout.flush()
    return subprocess.run(full, cwd=str(cwd), text=True, check=check)


def _print_tf_output(result: subprocess.CompletedProcess[str]) -> None:
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.stderr:
        print(result.stderr, end="" if result.stderr.endswith("\n") else "\n", file=sys.stderr)


def _base_metadata(config: Mapping[str, Any], cloud: str) -> dict[str, Any]:
    root = select_terraform_root(cloud)
    return {
        "cloud_provider": cloud,
        "account_or_project": account_or_project(config),
        "cloud_region": config.get("cloud_region"),
        "environment": config.get("environment"),
        "terraform_state_identity": terraform_state_identity(config),
        "terraform_root": f"infra/{cloud}",
        "terraform_root_path": str(root),
        "install_contract_version": INSTALL_CONTRACT_VERSION,
        "config_cloud_provider": config.get("cloud_provider"),
    }


def _emit_checks(results: list[CheckResult]) -> None:
    print(format_check_results(results))


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        config = load_install_config(args.config)
    except InstallContractError as exc:
        print(f"FAIL CONFIG_INVALID: {exc}", file=sys.stderr)
        return 2

    state_dir = resolve_state_dir(
        config=config, state_dir=args.state_dir, cloud=args.cloud
    )
    results = run_preflight(cloud=args.cloud, config=config, state_dir=state_dir)
    _emit_checks(results)
    if preflight_failed(results):
        return 1
    print(f"validate ok: cloud={args.cloud} root=infra/{args.cloud}")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    return _cmd_plan_or_apply(args, command="plan")


def cmd_apply(args: argparse.Namespace) -> int:
    return _cmd_plan_or_apply(args, command="apply")


def _cmd_plan_or_apply(args: argparse.Namespace, *, command: str) -> int:
    try:
        config = load_install_config(args.config)
    except InstallContractError as exc:
        print(f"FAIL CONFIG_INVALID: {exc}", file=sys.stderr)
        return 2

    state_dir = resolve_state_dir(
        config=config, state_dir=args.state_dir, cloud=args.cloud
    )
    results = run_preflight(cloud=args.cloud, config=config, state_dir=state_dir)
    _emit_checks(results)
    if preflight_failed(results):
        return 1

    tf_root = select_terraform_root(args.cloud)
    tfvars = generate_tfvars(config, cloud=args.cloud)
    # Terraform resolves relative paths after -chdir, so pass absolute ones.
    tfvars_path = (state_dir / TFVARS_NAME).resolve()
    write_tfvars(tfvars_path, tfvars)
    plan_file = Path(args.plan_file).resolve() if args.plan_file else None
    detailed = command == "plan" and args.detailed_exitcode

    meta = _base_metadata(config, args.cloud)
    meta.update(
        {
            "last_command": command,
            "status": "planning" if command == "plan" else "applying",
            "tfvars_path": str(tfvars_path),
        }
    )
    if plan_file:
        meta["plan_file"] = str(plan_file)
    write_metadata(state_dir, meta)

    tf = terraform_bin()
    var_file_args = [f"-var-file={tfvars_path}"]
    try:
        init = run_terraform(
            [tf, f"-chdir={tf_root}", "init", "-input=false", *backend_config_args(config)],
            cwd=REPO_ROOT,
            dry_run=args.dry_run,
            check=False,
        )
        if not args.dry_run:
            _print_tf_output(init)
        if init.returncode != 0:
            write_metadata(state_dir, {**meta, "status": f"{command}_failed_init"})
            return init.returncode

        if command == "plan":
            cmd = [tf, f"-chdir={tf_root}", "plan", "-input=false", *var_file_args]
            if plan_file:
                plan_file.parent.mkdir(parents=True, exist_ok=True)
                cmd.append(f"-out={plan_file}")
            if detailed:
                cmd.append("-detailed-exitcode")
        elif plan_file:
            # Variables and approval are baked into the saved plan.
            cmd = [tf, f"-chdir={tf_root}", "apply", "-input=false", str(plan_file)]
        else:
            cmd = [
                tf,
                f"-chdir={tf_root}",
                "apply",
                "-input=false",
                "-auto-approve",
                *var_file_args,
            ]
        action = run_terraform(cmd, cwd=REPO_ROOT, dry_run=args.dry_run, check=False)
        if not args.dry_run:
            _print_tf_output(action)
        has_changes = detailed and action.returncode == 2
        if action.returncode != 0 and not has_changes:
            write_metadata(state_dir, {**meta, "status": f"{command}_failed"})
            return action.returncode
    except FileNotFoundError:
        print("FAIL TERRAFORM_MISSING: terraform binary not found", file=sys.stderr)
        return 1

    if command == "plan":
        status = "planned" if has_changes or not detailed else "planned_no_changes"
    else:
        status = "applied"
    write_metadata(state_dir, {**meta, "status": status, "last_command": command})
    print(f"{command} ok: cloud={args.cloud} root=infra/{args.cloud} state={state_dir}")

    if command == "apply":
        runtime = config.get("runtime") or {}
        print(
            render_post_apply_next_steps(
                cloud=args.cloud,
                secret_prefix=str(config.get("secret_prefix") or config.get("project_name")),
                project_id=config.get("gcp_project_id"),
                embeddings_provider=str(runtime.get("embeddings_provider") or "openai"),
            )
        )
    else:
        print("Next: ./scripts/install.sh apply --cloud", args.cloud, "--config", args.config)
    return 2 if has_changes else 0


def cmd_destroy(args: argparse.Namespace) -> int:
    try:
        config = load_install_config(args.config)
    except InstallContractError as exc:
        print(f"FAIL CONFIG_INVALID: {exc}", file=sys.stderr)
        return 2

    environment = str(config.get("environment") or "")
    if args.confirm != environment:
        print(
            f"FAIL DESTROY_CONFIRM: --confirm must match environment {environment!r} "
            f"(got {args.confirm!r})",
            file=sys.stderr,
        )
        return 2

    state_dir = resolve_state_dir(
        config=config, state_dir=args.state_dir, cloud=args.cloud
    )
    results = run_preflight(cloud=args.cloud, config=config, state_dir=state_dir)
    _emit_checks(results)
    if preflight_failed(results):
        return 1

    tf_root = select_terraform_root(args.cloud)
    tfvars = generate_tfvars(config, cloud=args.cloud)
    tfvars_path = (state_dir / TFVARS_NAME).resolve()
    write_tfvars(tfvars_path, tfvars)
    meta = _base_metadata(config, args.cloud)
    meta.update({"last_command": "destroy", "status": "destroying"})
    write_metadata(state_dir, meta)

    tf = terraform_bin()
    try:
        init = run_terraform(
            [tf, f"-chdir={tf_root}", "init", "-input=false", *backend_config_args(config)],
            cwd=REPO_ROOT,
            dry_run=args.dry_run,
            check=False,
        )
        if not args.dry_run:
            _print_tf_output(init)
        if init.returncode != 0:
            return init.returncode
        destroy = run_terraform(
            [
                tf,
                f"-chdir={tf_root}",
                "destroy",
                "-input=false",
                "-auto-approve",
                f"-var-file={tfvars_path}",
            ],
            cwd=REPO_ROOT,
            dry_run=args.dry_run,
            check=False,
        )
        if not args.dry_run:
            _print_tf_output(destroy)
        if destroy.returncode != 0:
            write_metadata(state_dir, {**meta, "status": "destroy_failed"})
            return destroy.returncode
    except FileNotFoundError:
        print("FAIL TERRAFORM_MISSING: terraform binary not found", file=sys.stderr)
        return 1

    write_metadata(state_dir, {**meta, "status": "destroyed"})
    print(f"destroy ok: cloud={args.cloud} environment={environment}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = None
    if args.config:
        try:
            config = load_install_config(args.config)
        except InstallContractError as exc:
            print(f"FAIL CONFIG_INVALID: {exc}", file=sys.stderr)
            return 2

    if not args.state_dir and not args.config:
        print("status requires --state-dir and/or --config", file=sys.stderr)
        return 2

    cloud = getattr(args, "cloud", None) or (
        str((config or {}).get("cloud_provider") or "") or None
    )
    state_dir = resolve_state_dir(
        config=config, state_dir=args.state_dir, cloud=cloud
    )
    meta = read_state_metadata(state_dir)
    if not meta:
        # Also try state_dir as the env dir directly when passed with trailing env.
        meta = read_state_metadata(Path(args.state_dir)) if args.state_dir else None
    if not meta:
        print(f"No install metadata at {state_dir / METADATA_NAME}", file=sys.stderr)
        return 1

    print(f"state_dir: {state_dir}")
    for key in (
        "cloud_provider",
        "environment",
        "account_or_project",
        "cloud_region",
        "terraform_root",
        "terraform_state_identity",
        "status",
        "last_command",
        "install_contract_version",
    ):
        if key in meta:
            print(f"{key}: {meta[key]}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="install",
        description="TS Harness install-time AWS/GCP lifecycle CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_cloud_config(p: argparse.ArgumentParser, *, require_cloud: bool = True) -> None:
        if require_cloud:
            p.add_argument("--cloud", choices=("aws", "gcp"), required=True)
        p.add_argument("--config", required=True, help="Install contract YAML path")
        p.add_argument(
            "--state-dir",
            default=None,
            help=(
                "Local install state base or env dir "
                "(default: .install/<cloud_provider>/<environment>)"
            ),
        )
        p.add_argument(
            "--dry-run",
            action="store_true",
            help="Print terraform commands without executing",
        )

    p_validate = sub.add_parser("validate", help="Validate config and prerequisites")
    add_cloud_config(p_validate)
    p_validate.set_defaults(func=cmd_validate)

    p_plan = sub.add_parser("plan", help="Terraform plan in the selected provider root")
    add_cloud_config(p_plan)
    p_plan.add_argument(
        "--plan-file",
        default=None,
        help="Save the plan to this file (terraform plan -out) for a later apply",
    )
    p_plan.add_argument(
        "--detailed-exitcode",
        action="store_true",
        help="Exit 0 when there are no changes, 2 when changes are pending, 1 on error",
    )
    p_plan.set_defaults(func=cmd_plan)

    p_apply = sub.add_parser("apply", help="Terraform apply in the selected provider root")
    add_cloud_config(p_apply)
    p_apply.add_argument(
        "--plan-file",
        default=None,
        help="Apply this saved plan exactly (no re-plan, no -auto-approve)",
    )
    p_apply.set_defaults(func=cmd_apply, detailed_exitcode=False)

    p_destroy = sub.add_parser("destroy", help="Terraform destroy (requires --confirm)")
    add_cloud_config(p_destroy)
    p_destroy.add_argument(
        "--confirm",
        required=True,
        help="Must equal the config environment name",
    )
    p_destroy.set_defaults(func=cmd_destroy)

    p_status = sub.add_parser("status", help="Show local install metadata")
    p_status.add_argument("--state-dir", default=None)
    p_status.add_argument("--config", default=None)
    p_status.set_defaults(func=cmd_status)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
