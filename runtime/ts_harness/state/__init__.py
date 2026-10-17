"""Shared state backends for multi-instance runtime."""

from ts_harness.state.redis_store import get_state_store

__all__ = ["get_state_store"]
