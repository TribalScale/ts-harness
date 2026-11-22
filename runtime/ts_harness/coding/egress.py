"""Egress allowlist for local coding workers (``CODING_EGRESS_ALLOWLIST=on``).

With the flag on, the local launcher starts workers on an internal docker network
(``CODING_EGRESS_NETWORK``, default ``ts-harness-coding-egress``) whose only ways out
are the ``egress-proxy`` squid service and agent-runtime's network namespace
(memory-api, the runtime and the LLM proxy). Workers get ``HTTP(S)_PROXY`` pointing
at the proxy and ``NO_PROXY`` for loopback and the internal services, so the
Postgres, Redis and app servers a job starts on 127.0.0.1 keep working.

The proxy reads the allowed domains from a file the launcher writes into a shared
volume: the defaults below, ``CODING_EGRESS_ALLOW`` (comma list), the hosts of every
project package registry (memory-api ``/registry-hosts``) and, when the LLM proxy is
off, the model provider domains. The launcher rewrites the file when it changes and
sends the proxy a SIGHUP so squid reloads it without dropping running workers.

``python3 -m ts_harness.coding.egress check`` run inside a worker proves the policy:
an allowed host answers, a blocked host and a direct connection fail.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

DEFAULT_NETWORK = "ts-harness-coding-egress"
DEFAULT_PROXY_URL = "http://egress-proxy:3128"
DEFAULT_INTERNAL_HOST = "agent-runtime"
DEFAULT_ALLOWLIST_PATH = "/egress/domains.txt"
PROXY_LABEL = "ts-harness.egress-proxy=true"
SYNC_SECONDS = 60

# A leading dot allows the domain and every subdomain (squid dstdomain syntax).
DEFAULT_DOMAINS = (
    # GitHub: git over HTTPS, the API, archives, release assets and raw files.
    "github.com",
    "api.github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
    ".githubusercontent.com",
    # JavaScript packages and Node toolchains.
    "registry.npmjs.org",
    "registry.yarnpkg.com",
    "nodejs.org",
    # Python packages.
    "pypi.org",
    "files.pythonhosted.org",
    # Go modules.
    "proxy.golang.org",
    "sum.golang.org",
    # OpenCode's model catalogue and updates.
    "models.dev",
    ".opencode.ai",
    # Playwright browser builds for a repo on another Playwright version.
    "cdn.playwright.dev",
    "playwright.download.prss.microsoft.com",
)
# Only needed when workers call providers directly (LLM proxy off).
PROVIDER_DOMAINS = ("api.openai.com", "api.anthropic.com", "openrouter.ai")
PROVIDER_URL_NAMES = ("MODEL_BASE_URL", "OPENAI_BASE_URL", "ANTHROPIC_BASE_URL")
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1", "0.0.0.0")
_DOMAIN_RE = re.compile(r"^\.?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")
_TRUE = {"on", "true", "1", "yes"}


def enabled(environ: Mapping[str, str]) -> bool:
    return str(environ.get("CODING_EGRESS_ALLOWLIST") or "").strip().lower() in _TRUE


def _split(value: Any) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def normalize_domain(value: str) -> str:
    """``*.example.com`` -> ``.example.com``; a URL or ``host:port`` -> its host; "" if invalid."""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = urlsplit(text if "://" in text else f"//{text}").hostname or ""
    if text.startswith("*."):
        text = text[1:]
    text = text.rstrip(".")
    return text if _DOMAIN_RE.match(text) else ""


def collapse(domains: Iterable[str]) -> list[str]:
    """Sorted, without duplicates or entries a wildcard already covers.

    Squid refuses an ACL that lists ``.example.com`` and ``example.com`` (or a subdomain
    of it) together, so the narrower entries are dropped.
    """
    unique = {d for d in (normalize_domain(x) for x in domains) if d}
    wildcards = {d for d in unique if d.startswith(".")}

    def covered(domain: str) -> bool:
        bare = domain.lstrip(".")
        for wildcard in wildcards:
            if wildcard == domain:
                continue
            if bare == wildcard[1:] or bare.endswith(wildcard):
                return True
        return False

    return sorted(d for d in unique if not covered(d))


def allowlist(
    environ: Mapping[str, str], registry_hosts: Iterable[str] = (), *, llm_proxy_on: bool | None = None
) -> list[str]:
    """Domains workers may reach through the proxy."""
    if llm_proxy_on is None:
        from ts_harness.coding.launcher import worker_proxy_url

        llm_proxy_on = bool(worker_proxy_url(environ))
    domains: list[str] = list(DEFAULT_DOMAINS)
    if not llm_proxy_on:
        domains += PROVIDER_DOMAINS
        domains += [str(environ.get(name) or "") for name in PROVIDER_URL_NAMES]
    domains += _split(environ.get("CODING_EGRESS_ALLOW"))
    domains += list(registry_hosts)
    internal = internal_host(environ)
    return [d for d in collapse(domains) if d.lstrip(".") != internal]


def render_domains_file(domains: Iterable[str]) -> str:
    header = (
        "# Coding worker egress allowlist, written by coding-launcher. Do not edit:\n"
        "# add domains with CODING_EGRESS_ALLOW or a project package registry.\n"
    )
    return header + "".join(f"{d}\n" for d in domains)


def internal_host(environ: Mapping[str, str]) -> str:
    return str(environ.get("CODING_EGRESS_INTERNAL_HOST") or DEFAULT_INTERNAL_HOST).strip()


def internal_url(url: str, host: str) -> str:
    """A loopback URL (agent-runtime's namespace) as seen from the worker network."""
    if not url:
        return url
    parts = urlsplit(url)
    if (parts.hostname or "") not in LOOPBACK_HOSTS:
        return url
    netloc = host if parts.port is None else f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def no_proxy(environ: Mapping[str, str]) -> str:
    hosts = [*LOOPBACK_HOSTS, internal_host(environ), *_split(environ.get("CODING_EGRESS_NO_PROXY"))]
    return ",".join(dict.fromkeys(h for h in hosts if h))


def proxy_env(environ: Mapping[str, str]) -> dict[str, str]:
    """Proxy settings for a worker. git, curl, pip, npm, yarn, go and Node honour these."""
    proxy = str(environ.get("CODING_EGRESS_PROXY_WORKER_URL") or DEFAULT_PROXY_URL).strip()
    bypass = no_proxy(environ)
    env = {}
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env[name] = proxy
    env.update({"NO_PROXY": bypass, "no_proxy": bypass})
    # npm/yarn classic read npm_config_*; Node's built-in fetch needs NODE_USE_ENV_PROXY.
    env.update(
        {
            "npm_config_proxy": proxy,
            "npm_config_https_proxy": proxy,
            "npm_config_noproxy": bypass,
            "NODE_USE_ENV_PROXY": "1",
        }
    )
    return env


def write_if_changed(path: Path, text: str) -> bool:
    """Atomically replace ``path`` with ``text`` (mode 644, squid reads it as its own user)."""
    try:
        if path.read_text() == text:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return True


# ---------------------------------------------------------------------------
# check: run inside a worker to prove the policy
# ---------------------------------------------------------------------------


def _fetch(url: str, timeout: float) -> tuple[bool, str]:
    """(reached, detail). An HTTP error status still means the host answered."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=timeout) as response:
            return True, f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        return True, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:200]}"


def _direct(host: str, port: int, timeout: float) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "connected"
    except OSError as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:200]}"


def run_check(
    *,
    allowed: list[str],
    blocked: list[str],
    internal: list[str],
    direct: list[str],
    timeout: float = 10,
    environ: Mapping[str, str] | None = None,
    fetch=_fetch,
    connect=_direct,
) -> tuple[bool, list[dict[str, Any]]]:
    env = os.environ if environ is None else environ
    results: list[dict[str, Any]] = []
    proxy = env.get("HTTPS_PROXY") or env.get("https_proxy") or ""
    results.append({"check": "proxy_configured", "ok": bool(proxy), "detail": proxy or "HTTPS_PROXY is not set"})
    for url in allowed:
        reached, detail = fetch(url, timeout)
        results.append({"check": "allowed", "target": url, "ok": reached, "detail": detail})
    for url in internal:
        reached, detail = fetch(url, timeout)
        results.append({"check": "internal", "target": url, "ok": reached, "detail": detail})
    for url in blocked:
        reached, detail = fetch(url, timeout)
        results.append({"check": "blocked", "target": url, "ok": not reached, "detail": detail})
    for target in direct:
        host, _, port = target.rpartition(":")
        reached, detail = connect(host, int(port or 443), timeout)
        results.append({"check": "direct_blocked", "target": target, "ok": not reached, "detail": detail})
    return all(r["ok"] for r in results), results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m ts_harness.coding.egress")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="verify the egress policy from inside a worker")
    check.add_argument("--allowed", action="append", default=None, help="URL that must answer")
    check.add_argument("--blocked", action="append", default=None, help="URL that must fail")
    check.add_argument("--internal", action="append", default=[], help="internal URL that must answer")
    check.add_argument("--direct", action="append", default=None, help="host:port a direct connection must fail")
    check.add_argument("--timeout", type=float, default=10)
    render = sub.add_parser("render", help="print the allowlist for this environment")
    render.add_argument("--registry-host", action="append", default=[])
    args = parser.parse_args(argv)
    if args.command == "render":
        sys.stdout.write(render_domains_file(allowlist(os.environ, args.registry_host)))
        return 0
    ok, results = run_check(
        allowed=args.allowed or ["https://api.github.com/", "https://registry.npmjs.org/"],
        blocked=args.blocked or ["https://example.com/"],
        internal=args.internal,
        direct=args.direct or ["1.1.1.1:443"],
        timeout=args.timeout,
    )
    for result in results:
        print(json.dumps(result), flush=True)
    print(json.dumps({"check": "summary", "ok": ok}), flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
