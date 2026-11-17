"""Leases in ``service_leases`` (migration 039) for jobs only one runtime replica may run."""

from __future__ import annotations

import logging
import os
import socket
from typing import Any, Callable

logger = logging.getLogger(__name__)


def default_holder() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


class ServiceLease:
    """``held()`` takes or renews the lease; True while this process holds it.

    Without a database there is one replica, so the lease is always held. When
    the database cannot be reached the lease is not held, so two replicas never
    both run the job.
    """

    def __init__(self, name: str, connect: Callable[[], Any] | None, *, ttl_seconds: int = 30, holder: str = ""):
        self.name = name
        self.connect = connect
        self.ttl_seconds = ttl_seconds
        self.holder = holder or default_holder()

    def held(self) -> bool:
        if self.connect is None:
            return True
        try:
            with self.connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO service_leases (name, holder, expires_at)
                    VALUES (%(name)s, %(holder)s, now() + make_interval(secs => %(ttl)s))
                    ON CONFLICT (name) DO UPDATE
                       SET holder = EXCLUDED.holder, expires_at = EXCLUDED.expires_at
                     WHERE service_leases.holder = EXCLUDED.holder
                        OR service_leases.expires_at < now()
                    RETURNING holder
                    """,
                    {"name": self.name, "holder": self.holder, "ttl": self.ttl_seconds},
                )
                return cur.fetchone() is not None
        except Exception as exc:  # noqa: BLE001
            logger.warning("service lease %s unavailable: %s", self.name, exc)
            return False

    def release(self) -> None:
        if self.connect is None:
            return
        try:
            with self.connect() as conn, conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM service_leases WHERE name = %s AND holder = %s", (self.name, self.holder)
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("service lease %s release failed: %s", self.name, exc)


def scheduler_lease() -> ServiceLease:
    from ts_harness.agent.sessions import shared_state_connection

    return ServiceLease("runtime-scheduler", shared_state_connection())
