#!/usr/bin/env python3
"""Remove offline tailnet machines that hold this host's Tailscale name, before it joins.

A rebuilt host joins with the same name (install YAML ``vm.tailscale_hostname``). If the old
machine is still in the tailnet, Tailscale names the new one ``<name>-1`` and every URL built on
the name (admin console, OAuth callbacks, GitHub webhook) breaks. Phase 06 runs this when the
auth secret is a Tailscale OAuth client secret (``tskey-client-<client id>-…``) with the
``devices:core`` scope::

    TS_OAUTH_CLIENT_SECRET=tskey-client-... python3 tailscale-stale-device.py --hostname ts-harness-dev

Only machines that match the name exactly and have been offline for ``--offline-minutes`` are
removed. Exits 0 even when nothing was removed; 1 on an API error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

API = "https://api.tailscale.com/api/v2"


def client_id_from_secret(secret: str) -> str:
    """``tskey-client-<id>-<random>`` -> ``<id>``."""
    parts = secret.split("-")
    return parts[2] if len(parts) >= 4 and secret.startswith("tskey-client-") else ""


def _parse_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def stale_devices(devices: list[dict[str, Any]], hostname: str, *, now: datetime, offline_minutes: int = 10) -> list[dict[str, Any]]:
    """Devices named ``hostname`` (machine name, not the OS hostname) that are offline."""
    wanted = hostname.lower()
    cutoff = now - timedelta(minutes=offline_minutes)
    found = []
    for device in devices:
        name = str(device.get("name") or "").split(".")[0].lower()
        if name != wanted:
            continue
        if device.get("connectedToControl") is True:
            continue
        seen = _parse_time(str(device.get("lastSeen") or ""))
        if seen is not None and seen > cutoff:
            continue
        found.append(device)
    return found


def _request(method: str, url: str, *, token: str = "", data: bytes | None = None, headers: dict[str, str] | None = None) -> Any:
    request = urllib.request.Request(url, method=method, data=data, headers={**(headers or {}), **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read()
    return json.loads(body) if body else {}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--hostname", required=True)
    parser.add_argument("--offline-minutes", type=int, default=10)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    secret = os.environ.get("TS_OAUTH_CLIENT_SECRET", "").strip()
    client_id = os.environ.get("TS_OAUTH_CLIENT_ID", "").strip() or client_id_from_secret(secret)
    if not secret or not client_id:
        print("no Tailscale OAuth client; skipping stale-device cleanup")
        return 0
    try:
        token = _request(
            "POST",
            f"{API}/oauth/token",
            data=urllib.parse.urlencode({"client_id": client_id, "client_secret": secret}).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )["access_token"]
        devices = _request("GET", f"{API}/tailnet/-/devices", token=token).get("devices") or []
        for device in stale_devices(devices, args.hostname, now=datetime.now(timezone.utc), offline_minutes=args.offline_minutes):
            label = f"{device.get('name')} (last seen {device.get('lastSeen')})"
            if args.dry_run:
                print("would remove", label)
                continue
            _request("DELETE", f"{API}/device/{urllib.parse.quote(str(device['id']))}", token=token)
            print("removed offline machine", label)
    except Exception as exc:  # noqa: BLE001
        print(f"stale-device cleanup failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
