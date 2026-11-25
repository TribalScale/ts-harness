"""Give a web repository's nightly smoke run what it needs: read access to its API and a way to report.

The ``react-web`` template's ``.github/workflows/nightly-smoke.yml`` checks out the private API
repository and reports failures to the harness. When a web repository is linked to its API
(``.ts-harness.yml`` ``contract.openapi.repo``), the bootstrap sets that up:

- ``API_REPO`` (Actions variable): the API repository, ``owner/name``.
- ``API_REPO_DEPLOY_KEY`` (Actions secret): the private half of a new ed25519 key whose public
  half is a **read-only** deploy key on the API repository (titled
  ``ts-harness nightly smoke: <web repo>``). Generated here, sent encrypted, never stored or
  logged by the harness.
- ``HARNESS_SMOKE_URL`` / ``HARNESS_SMOKE_TOKEN`` (Actions secrets): where to report
  (``SMOKE_REPORT_URL``) and the repository's report token (see nightly_smoke.report_token).

GitHub only takes secrets encrypted with the repository's public key in a libsodium sealed box.
That needs PyNaCl, which the harness does not depend on: when it is not installed, only the
variable is set and the notes point to the manual steps in docs/runbooks/nightly-smoke.md (no
deploy key is created, since its private half could not be delivered). The same goes for any
permission the GitHub App lacks (Secrets and Variables write on the web repository,
Administration write on the API repository for deploy keys). Nothing here raises: setup is
advisory and the bootstrap PR goes ahead either way.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any, Callable

logger = logging.getLogger(__name__)

WORKFLOW_PATH = ".github/workflows/nightly-smoke.yml"
API_REPO_VARIABLE = "API_REPO"
DEPLOY_KEY_SECRET = "API_REPO_DEPLOY_KEY"
URL_SECRET = "HARNESS_SMOKE_URL"
TOKEN_SECRET = "HARNESS_SMOKE_TOKEN"
URL_ENV = "SMOKE_REPORT_URL"
RUNBOOK = "docs/runbooks/nightly-smoke.md"

Sealer = Callable[[str, str], str]


def default_sealer() -> Sealer | None:
    """Encrypts a secret value for GitHub (base64 sealed box), or None without PyNaCl."""
    try:
        from nacl import encoding, public  # type: ignore[import-not-found]
    except ImportError:
        return None

    def seal(public_key_b64: str, value: str) -> str:
        box = public.SealedBox(public.PublicKey(public_key_b64.encode(), encoding.Base64Encoder()))
        return base64.b64encode(box.encrypt(value.encode())).decode()

    return seal


def deploy_key_title(web_repo: str) -> str:
    return f"ts-harness nightly smoke: {web_repo}"


def generate_deploy_key(comment: str) -> tuple[str, str]:
    """``(private OpenSSH PEM, public "ssh-ed25519 ..." line)`` for a new ed25519 key."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()
    ).decode()
    public = key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
    return private, f"{public} {comment}"


def _split(repo: str) -> tuple[str, str]:
    owner, _, name = repo.partition("/")
    return owner, name


def _set_variable(github: Any, repo: str, name: str, value: str) -> None:
    owner, name_ = _split(repo)
    path = f"/repos/{owner}/{name_}/actions/variables"
    if github.request("GET", f"{path}/{name}", allow_404=True):
        github.request("PATCH", f"{path}/{name}", body={"name": name, "value": value})
    else:
        github.request("POST", path, body={"name": name, "value": value})


def _put_secret(github: Any, repo: str, repo_key: dict[str, Any], name: str, value: str, seal: Sealer) -> None:
    owner, name_ = _split(repo)
    github.request(
        "PUT",
        f"/repos/{owner}/{name_}/actions/secrets/{name}",
        body={"encrypted_value": seal(str(repo_key["key"]), value), "key_id": str(repo_key["key_id"])},
    )


def _ensure_deploy_key(github: Any, web_repo: str, api_repo: str, repo_key: dict[str, Any], seal: Sealer) -> str:
    """Create the deploy key and its secret unless both are already there; what was done."""
    owner, name = _split(web_repo)
    api_owner, api_name = _split(api_repo)
    title = deploy_key_title(web_repo)
    have_secret = bool(github.request("GET", f"/repos/{owner}/{name}/actions/secrets/{DEPLOY_KEY_SECRET}", allow_404=True))
    ours = [k for k in github.request("GET", f"/repos/{api_owner}/{api_name}/keys") or [] if k.get("title") == title]
    if have_secret and ours:
        return "deploy key already in place"
    # One half without the other is useless: start over with a fresh pair.
    for key in ours:
        github.request("DELETE", f"/repos/{api_owner}/{api_name}/keys/{key['id']}")
    private, public = generate_deploy_key(title.replace(" ", "-").replace(":", ""))
    github.request("POST", f"/repos/{api_owner}/{api_name}/keys", body={"title": title, "key": public, "read_only": True})
    _put_secret(github, web_repo, repo_key, DEPLOY_KEY_SECRET, private, seal)
    return f"added a read-only deploy key to {api_repo} and stored it as {DEPLOY_KEY_SECRET}"


def _failed(what: str, exc: Exception) -> str:
    status = getattr(exc, "status", None)
    why = " (the GitHub App lacks the permission)" if status in (403, 404) else ""
    return f"could not {what}{why}: HTTP {status or '?'}; see {RUNBOOK} for the manual steps"


def ensure_smoke_access(
    github: Any,
    web_repo: str,
    api_repo: str,
    *,
    seal: Sealer | None = None,
    report_url: str | None = None,
    report_secret: str | None = None,
) -> list[str]:
    """Set up the nightly smoke run's access for ``web_repo``; notes for the people who start it.

    Idempotent: a deploy key and secret already in place are kept. Never raises, never puts a
    secret value in a note or a log line.
    """
    from ts_harness.workflows.nightly_smoke import report_token

    notes: list[str] = []
    prefix = f"{web_repo} nightly smoke:"
    try:
        _set_variable(github, web_repo, API_REPO_VARIABLE, api_repo)
        notes.append(f"{prefix} set {API_REPO_VARIABLE}={api_repo}")
    except Exception as exc:  # noqa: BLE001
        notes.append(f"{prefix} {_failed(f'set the {API_REPO_VARIABLE} variable', exc)}")

    seal = seal or default_sealer()
    if seal is None:
        notes.append(
            f"{prefix} secrets not set: the harness cannot encrypt GitHub secrets without PyNaCl. "
            f"Add the deploy key and {DEPLOY_KEY_SECRET}, {URL_SECRET}, {TOKEN_SECRET} by hand ({RUNBOOK})"
        )
        return notes

    owner, name = _split(web_repo)
    try:
        repo_key = github.request("GET", f"/repos/{owner}/{name}/actions/secrets/public-key")
    except Exception as exc:  # noqa: BLE001
        notes.append(f"{prefix} {_failed('read the repository key for Actions secrets', exc)}")
        return notes

    try:
        notes.append(f"{prefix} {_ensure_deploy_key(github, web_repo, api_repo, repo_key, seal)}")
    except Exception as exc:  # noqa: BLE001
        notes.append(f"{prefix} {_failed(f'set up read access to {api_repo}', exc)}")

    url = (report_url if report_url is not None else os.environ.get(URL_ENV, "")).strip()
    secret = report_secret if report_secret is not None else os.environ.get("SMOKE_REPORT_SECRET", "").strip()
    if not url or not secret:
        notes.append(f"{prefix} failures are not reported to the harness: set {URL_ENV} and SMOKE_REPORT_SECRET ({RUNBOOK})")
        return notes
    try:
        _put_secret(github, web_repo, repo_key, URL_SECRET, url, seal)
        _put_secret(github, web_repo, repo_key, TOKEN_SECRET, report_token(web_repo, secret), seal)
        notes.append(f"{prefix} stored {URL_SECRET} and {TOKEN_SECRET}")
    except Exception as exc:  # noqa: BLE001
        notes.append(f"{prefix} {_failed('store the failure report secrets', exc)}")
    return notes
