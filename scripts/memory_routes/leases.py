"""Named singleton leases: one holder per name until it stops renewing."""

from __future__ import annotations

from typing import Any


def acquire_service_lease(conn, body: dict[str, Any]) -> dict[str, Any]:
    name = str(body.get("name") or "").strip()
    holder = str(body.get("holder") or "").strip()
    if not name or not holder:
        raise ValueError("name and holder are required")
    ttl = max(1, int(body.get("ttl_seconds") or 30))
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO service_leases (name, holder, expires_at)
            VALUES (%s, %s, now() + make_interval(secs => %s))
            ON CONFLICT (name) DO UPDATE
              SET holder = EXCLUDED.holder, expires_at = EXCLUDED.expires_at
              WHERE service_leases.holder = EXCLUDED.holder OR service_leases.expires_at < now()
            RETURNING holder
            """,
            (name, holder, ttl),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute("SELECT holder FROM service_leases WHERE name = %s", (name,))
            current = cur.fetchone()
    conn.commit()
    if row is not None:
        return {"leader": True, "holder": holder}
    return {"leader": False, "holder": str(current["holder"]) if current else ""}
