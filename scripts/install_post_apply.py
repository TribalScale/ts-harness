"""Post-apply orchestration: secret population guidance (no secret values in TF state)."""

from __future__ import annotations

from typing import Iterable, Sequence

SECRET_CATALOG: tuple[str, ...] = (
    "anthropic-api-key",
    "db-password",
    "gateway-token",
    "tailscale-authkey",
    "github-token",
    "coding-backend-api-key",
    "coding-backend-provider",
    "coding-backend-model",
    "jira-token",
    "jira-url",
    "jira-username",
    "telegram-token",
    "discord-token",
    "slack-bot-token",
    "slack-app-token",
    "slack-allowed-ids",
    "slack-signing-secret",
    "google-workspace-sa-json",
    "google-oauth-client-id",
    "google-oauth-client-secret",
    "google-oauth-callback-url",
    "google-workspace-default-email",
    "google-comms-email",
    "google-marketing-email",
    "google-sales-email",
    "google-pm-email",
    "google-pm-prd-folder-id",
    "google-delivery-artifacts-folder-id",
    "agent-credentials-kms-key-id",
)

REQUIRED_SECRET_KEYS: tuple[str, ...] = (
    "anthropic-api-key",
    "db-password",
    "gateway-token",
)

EMBEDDINGS_SECRET_BY_PROVIDER: dict[str, tuple[str, ...]] = {
    "openai": ("openai-api-key",),
    "voyage": ("voyage-api-key",),
    "gemini": ("gemini-api-key",),
    "mistral": ("mistral-api-key",),
    "bedrock": (),
    "local": (),
}


def list_required_secrets(embeddings_provider: str = "openai") -> list[str]:
    provider = (embeddings_provider or "openai").strip().lower()
    embedding_keys = list(EMBEDDINGS_SECRET_BY_PROVIDER.get(provider, ()))
    seen: set[str] = set()
    ordered: list[str] = []
    for key in (*REQUIRED_SECRET_KEYS, *SECRET_CATALOG, *embedding_keys):
        if key not in seen:
            seen.add(key)
            ordered.append(key)
    return ordered


def aws_secret_id(secret_prefix: str, key: str) -> str:
    return f"{secret_prefix}/{key}"


def gcp_secret_id(secret_prefix: str, key: str) -> str:
    return f"{secret_prefix}-{key}"


def format_secret_population_commands(
    *,
    cloud: str,
    secret_prefix: str,
    project_id: str | None,
    secrets: Sequence[str],
) -> str:
    lines: list[str] = [
        "# Populate secrets after terraform apply (values stay out of Terraform state).",
        "# Replace VALUE with real material. Do not commit secret values.",
        "",
    ]
    if cloud == "aws":
        for key in secrets:
            sid = aws_secret_id(secret_prefix, key)
            lines.append(
                f"aws secretsmanager put-secret-value --secret-id '{sid}' "
                f"--secret-string 'VALUE'"
            )
    elif cloud == "gcp":
        project_flag = f" --project {project_id}" if project_id else ""
        for key in secrets:
            sid = gcp_secret_id(secret_prefix, key)
            lines.append(
                f"# gcloud secrets versions access latest --secret={sid}{project_flag}"
            )
            lines.append(
                f"printf '%s' 'VALUE' | gcloud secrets versions add {sid} "
                f"--data-file=-{project_flag}"
            )
    else:
        raise ValueError(f"Unsupported cloud for secret commands: {cloud!r}")
    return "\n".join(lines).rstrip() + "\n"


def render_post_apply_next_steps(
    *,
    cloud: str,
    secret_prefix: str,
    project_id: str | None = None,
    embeddings_provider: str = "openai",
    secrets: Iterable[str] | None = None,
) -> str:
    secret_list = (
        list(secrets)
        if secrets is not None
        else list_required_secrets(embeddings_provider)
    )
    commands = format_secret_population_commands(
        cloud=cloud,
        secret_prefix=secret_prefix,
        project_id=project_id,
        secrets=secret_list,
    )
    return "\n".join(
        [
            "## Post-apply next steps",
            "",
            "Do not write secret values into Terraform state.",
            "Terraform seeds placeholder secret shells; populate real values with the CLI:",
            "",
            commands.rstrip(),
            "",
            "Then complete shared bootstrap / compose on the instance and run smoke tests:",
            "  docs/runbooks/post-deploy-smoke-test.md",
            "  ./scripts/ops/post-deploy-smoke.sh",
            "",
            "Installer entrypoints:",
            "  ./scripts/install.sh status --config <file>",
            "  python scripts/install.py status --config <file>",
            "",
        ]
    )
