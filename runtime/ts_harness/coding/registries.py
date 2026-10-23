"""Write package manager credentials for a coding job's project registries.

Files go to the standard per-user locations under ``home`` with mode 600:
``.npmrc``, ``.config/pip/pip.conf``, ``.m2/settings.xml`` and
``.docker/config.json``. Contents are never logged. For Maven, ``scope`` is
the repository id the project's pom uses; without one the registry becomes a
mirror of everything.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse
from xml.sax.saxutils import escape


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(path, 0o600)


def _npmrc(entries: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for entry in entries:
        url = entry["url"].rstrip("/") + "/"
        scope = str(entry.get("scope") or "").strip()
        if scope:
            lines.append(f"{scope if scope.startswith('@') else '@' + scope}:registry={url}")
        else:
            lines.append(f"registry={url}")
        token = entry.get("token")
        if token:
            key = "//" + url.split("://", 1)[1]
            if entry.get("username"):
                auth = _b64(f"{entry['username']}:{token}")
                lines.append(f"{key}:_auth={auth}")
            else:
                lines.append(f"{key}:_authToken={token}")
            lines.append(f"{key}:always-auth=true")
    return "\n".join(lines) + "\n"


def _pip_url(entry: dict[str, Any]) -> str:
    parsed = urlparse(entry["url"])
    if not entry.get("token"):
        return entry["url"]
    user = quote(str(entry.get("username") or "__token__"), safe="")
    secret = quote(str(entry["token"]), safe="")
    netloc = f"{user}:{secret}@{parsed.netloc}"
    return parsed._replace(netloc=netloc).geturl()


def _pip_conf(entries: list[dict[str, Any]]) -> str:
    lines = ["[global]", f"index-url = {_pip_url(entries[0])}"]
    extras = [_pip_url(e) for e in entries[1:]]
    if extras:
        lines.append("extra-index-url =")
        lines += [f"    {url}" for url in extras]
    return "\n".join(lines) + "\n"


def _maven_settings(entries: list[dict[str, Any]]) -> str:
    servers: list[str] = []
    mirrors: list[str] = []
    for index, entry in enumerate(entries, start=1):
        server_id = str(entry.get("scope") or "").strip() or f"ts-harness-registry-{index}"
        if entry.get("token"):
            servers.append(
                "    <server>\n"
                f"      <id>{escape(server_id)}</id>\n"
                f"      <username>{escape(str(entry.get('username') or 'token'))}</username>\n"
                f"      <password>{escape(str(entry['token']))}</password>\n"
                "    </server>"
            )
        if not entry.get("scope") and not mirrors:
            mirrors.append(
                "    <mirror>\n"
                f"      <id>{escape(server_id)}</id>\n"
                "      <mirrorOf>*</mirrorOf>\n"
                f"      <url>{escape(entry['url'])}</url>\n"
                "    </mirror>"
            )
    body = ['<?xml version="1.0" encoding="UTF-8"?>', "<settings>"]
    body += ["  <servers>", *servers, "  </servers>"] if servers else []
    body += ["  <mirrors>", *mirrors, "  </mirrors>"] if mirrors else []
    body.append("</settings>")
    return "\n".join(body) + "\n"


def _docker_config(entries: list[dict[str, Any]]) -> str:
    auths = {}
    for entry in entries:
        if not entry.get("token"):
            continue
        host = urlparse(entry["url"]).netloc
        auths[host] = {"auth": _b64(f"{entry.get('username') or 'token'}:{entry['token']}")}
    return json.dumps({"auths": auths}, indent=2) + "\n"


def write_registry_config(registries: list[dict[str, Any]], home: Path) -> list[str]:
    """Write config files for the given registries; returns the registry types configured."""
    by_type: dict[str, list[dict[str, Any]]] = {}
    for entry in registries:
        if entry.get("url") and entry.get("type"):
            by_type.setdefault(str(entry["type"]), []).append(entry)
    if "npm" in by_type:
        _write_private(home / ".npmrc", _npmrc(by_type["npm"]))
    if "pypi" in by_type:
        _write_private(home / ".config" / "pip" / "pip.conf", _pip_conf(by_type["pypi"]))
    if "maven" in by_type:
        _write_private(home / ".m2" / "settings.xml", _maven_settings(by_type["maven"]))
    if "docker" in by_type:
        _write_private(home / ".docker" / "config.json", _docker_config(by_type["docker"]))
    return sorted(by_type)
