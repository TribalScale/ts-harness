"""Slack Socket Mode inbound listener for TS Harness."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
import fcntl
import os
import random
import re
import threading
from pathlib import Path

from ts_harness.gateway.auth import AuthContext
from ts_harness.observability.logging import log_event

if TYPE_CHECKING:
    from ts_harness.gateway.server import RuntimeState

IGNORED_MESSAGE_SUBTYPES = frozenset(
    {
        "bot_message",
        "message_changed",
        "message_deleted",
        "channel_join",
        "channel_leave",
        "channel_topic",
        "channel_purpose",
        "channel_name",
        "channel_archive",
        "group_join",
        "group_leave",
    }
)

NON_RECOVERABLE_AUTH_ERRORS = re.compile(
    r"account_inactive|invalid_auth|token_revoked|missing_scope|not_authed|"
    r"token_expired|invalid_token|app_disabled",
    re.IGNORECASE,
)

RECONNECT_POLICY = {
    "initial_s": 2.0,
    "max_s": 30.0,
    "factor": 1.8,
    "max_attempts": 12,
}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def socket_mode_enabled() -> bool:
    bot = (os.environ.get("SLACK_BOT_TOKEN") or "").strip()
    app = (os.environ.get("SLACK_APP_TOKEN") or "").strip()
    if not bot or not app:
        return False
    slack_mode = (os.environ.get("SLACK_MODE") or "").strip().lower()
    if slack_mode == "http":
        return False
    explicit = os.environ.get("SLACK_SOCKET_MODE")
    if explicit is not None and str(explicit).strip():
        return _env_bool("SLACK_SOCKET_MODE", True)
    return True


def build_envelope(body: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    dedup_key = body.get("dedup_key") or event.get("client_msg_id") or event.get("ts")
    return {
        "type": "event_callback",
        "event_id": body.get("event_id") or event.get("client_msg_id") or event.get("ts"),
        "team_id": body.get("team_id"),
        "dedup_key": dedup_key,
        "event": dict(event),
    }


def is_non_recoverable_auth_error(error: BaseException | str | None) -> bool:
    if error is None:
        return False
    return bool(NON_RECOVERABLE_AUTH_ERRORS.search(str(error)))


def compute_backoff(attempt: int) -> float:
    initial = RECONNECT_POLICY["initial_s"]
    maximum = RECONNECT_POLICY["max_s"]
    factor = RECONNECT_POLICY["factor"]
    delay = min(maximum, initial * (factor ** max(attempt - 1, 0)))
    jitter = delay * 0.25 * random.random()
    return delay + jitter


def should_process_message(
    event: dict[str, Any],
    *,
    bot_user_id: str | None,
    require_mention: bool,
    from_app_mention: bool = False,
) -> bool:
    if event.get("bot_id"):
        return False
    subtype = str(event.get("subtype") or "")
    if subtype in IGNORED_MESSAGE_SUBTYPES:
        return False
    channel_type = str(event.get("channel_type") or "")
    if channel_type == "im":
        return bool(str(event.get("text") or "").strip() or event.get("blocks") or event.get("files"))
    if from_app_mention:
        return True
    if channel_type in {"channel", "group", "mpim"}:
        text = str(event.get("text") or "")
        if not text.strip() and not event.get("blocks") and not event.get("files"):
            return False
        if not require_mention:
            return True
        if bot_user_id and f"<@{bot_user_id}>" in text:
            return False  # defer to app_mention
        return False
    return bool(str(event.get("text") or "").strip())


def should_process_reaction(
    event: dict[str, Any],
    *,
    reaction_mode: str,
    bot_user_id: str | None,
) -> bool:
    if reaction_mode == "off":
        return False
    item = event.get("item") or {}
    if item.get("type") != "message":
        return False
    if event.get("bot_id"):
        return False
    if reaction_mode == "own":
        return bool(bot_user_id and event.get("item_user") == bot_user_id)
    if reaction_mode == "all":
        return bool(event.get("user"))
    return False


def build_reaction_event(
    event: dict[str, Any],
    *,
    action: str,
) -> dict[str, Any]:
    item = event.get("item") or {}
    channel_id = str(item.get("channel") or "")
    user_id = str(event.get("user") or "unknown")
    emoji = str(event.get("reaction") or "")
    item_ts = str(item.get("ts") or "")
    text = (
        f"[slack:{action}] user {user_id} {action.replace('_', ' ')} "
        f":{emoji}: on message ts={item_ts} in channel {channel_id}"
    )
    synthetic = {
        "type": "message",
        "user": user_id,
        "text": text,
        "channel": channel_id,
        "ts": item_ts,
        "thread_ts": item_ts,
        "channel_type": "channel",
    }
    event_id = f"{action}:{user_id}:{emoji}:{item_ts}"
    return build_envelope(
        {"event_id": event_id, "dedup_key": event_id, "team_id": event.get("team_id")},
        synthetic,
    )


@dataclass
class SlackSocketHealth:
    status: str  # disabled | connecting | connected | degraded | stopped
    detail: str | None = None


class SlackSocketListener:
    """Background Slack Socket Mode client delegating to handle_channel_webhook."""

    def __init__(self, runtime: RuntimeState) -> None:
        self._runtime = runtime
        self._shutdown = threading.Event()
        self._thread: threading.Thread | None = None
        self._handler: Any = None
        self._app: Any = None
        self._bot_user_id: str | None = None
        self._lock_file: Any = None
        self._lock_path: Path | None = None
        self._state = "disabled"
        self._state_detail: str | None = None
        self._state_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="slack-socket")
        self._require_mention = _env_bool("SLACK_REQUIRE_MENTION", True)
        self._reaction_mode = (os.environ.get("SLACK_REACTION_MODE") or "own").strip().lower()

    def start(self) -> None:
        if not socket_mode_enabled():
            self._set_state("disabled", "SLACK_SOCKET_MODE disabled or tokens missing")
            log_event("slack_socket_disabled", reason=self._state_detail)
            return
        if not self._acquire_lock():
            self._set_state("disabled", "another process holds slack socket lock")
            log_event("slack_socket_disabled", reason=self._state_detail)
            return
        self._thread = threading.Thread(
            target=self._run_loop,
            name="ec-slack-socket",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._shutdown.set()
        self._close_handler()
        self._executor.shutdown(wait=False, cancel_futures=True)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._release_lock()
        self._set_state("stopped")

    def health(self) -> SlackSocketHealth:
        with self._state_lock:
            return SlackSocketHealth(status=self._state, detail=self._state_detail)

    def _set_state(self, status: str, detail: str | None = None) -> None:
        with self._state_lock:
            self._state = status
            self._state_detail = detail

    def _lock_file_path(self) -> Path:
        dedup = os.environ.get("TS_HARNESS_CHANNEL_DEDUP", "")
        if dedup:
            return Path(dedup).parent / "slack-socket.lock"
        return Path("/tmp/ts-harness-slack-socket.lock")

    def _acquire_lock(self) -> bool:
        path = self._lock_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return False
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._lock_file = handle
        self._lock_path = path
        return True

    def _release_lock(self) -> None:
        if self._lock_file is not None:
            try:
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
                self._lock_file.close()
            except OSError:
                pass
            self._lock_file = None

    def _auth_context(self, event: dict[str, Any]) -> AuthContext:
        tenant_id = self._runtime.config.gateway.default_tenant_id
        user_id = str(event.get("user") or "unknown")
        return AuthContext(
            actor_id=f"slack:{user_id}",
            tenant_id=tenant_id,
            auth_provider="slack",
        )

    def _safe_handle(self, envelope: dict[str, Any], auth: AuthContext) -> None:
        try:
            self._runtime.handle_channel_webhook("slack", envelope, auth)
        except ValueError as exc:
            log_event("slack_socket_event_skipped", error=str(exc))
        except Exception as exc:  # noqa: BLE001
            log_event("slack_socket_handle_error", error=str(exc))
            self._runtime.audit.append(
                tenant_id=auth.tenant_id,
                action_type="channel_webhook_error",
                actor_id=auth.actor_id,
                payload={"channel": "slack", "error": str(exc), "transport": "socket"},
            )
            self._send_error_reply(envelope, auth, str(exc))

    def _send_error_reply(self, envelope: dict[str, Any], auth: AuthContext, error: str) -> None:
        try:
            from ts_harness.gateway.channels.router import get_channel_router

            event = envelope.get("event") or envelope
            channel_id = str(event.get("channel") or "")
            thread_id = event.get("thread_ts") or event.get("ts")
            if not channel_id:
                return
            preview = error[:300] if error else "unknown error"
            get_channel_router().send(
                "slack",
                {
                    "channel": channel_id,
                    "text": (
                        "TS Harness hit an error handling your message. "
                        f"Details: {preview}"
                    ),
                    "thread_id": thread_id,
                },
                tenant_id=auth.tenant_id,
            )
        except Exception as send_exc:  # noqa: BLE001
            log_event("slack_socket_error_reply_failed", error=str(send_exc))

    def _dispatch(self, envelope: dict[str, Any], auth: AuthContext) -> None:
        self._executor.submit(self._safe_handle, envelope, auth)

    def _register_handlers(self, app: Any) -> None:
        listener = self

        @app.event("message")
        def on_message(body: dict[str, Any], event: dict[str, Any], logger: Any) -> None:
            if not should_process_message(
                event,
                bot_user_id=listener._bot_user_id,
                require_mention=listener._require_mention,
            ):
                return
            listener._dispatch(listener._envelope(body, event), listener._auth_context(event))

        @app.event("app_mention")
        def on_app_mention(body: dict[str, Any], event: dict[str, Any], logger: Any) -> None:
            if not should_process_message(
                event,
                bot_user_id=listener._bot_user_id,
                require_mention=listener._require_mention,
                from_app_mention=True,
            ):
                return
            listener._dispatch(listener._envelope(body, event), listener._auth_context(event))

        @app.event("reaction_added")
        def on_reaction_added(body: dict[str, Any], event: dict[str, Any], logger: Any) -> None:
            if not should_process_reaction(
                event,
                reaction_mode=listener._reaction_mode,
                bot_user_id=listener._bot_user_id,
            ):
                return
            envelope = build_reaction_event(event, action="reaction_added")
            listener._dispatch(envelope, listener._auth_context(event))

        @app.event("reaction_removed")
        def on_reaction_removed(body: dict[str, Any], event: dict[str, Any], logger: Any) -> None:
            if not should_process_reaction(
                event,
                reaction_mode=listener._reaction_mode,
                bot_user_id=listener._bot_user_id,
            ):
                return
            envelope = build_reaction_event(event, action="reaction_removed")
            listener._dispatch(envelope, listener._auth_context(event))

    @staticmethod
    def _envelope(body: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
        return build_envelope(body, event)

    def _close_handler(self) -> None:
        handler = self._handler
        self._handler = None
        self._app = None
        if handler is not None:
            try:
                handler.close()
            except Exception as exc:  # noqa: BLE001
                log_event("slack_socket_close_error", error=str(exc))

    def _start_handler(self, bot_token: str, app_token: str) -> None:
        from slack_bolt import App
        from slack_bolt.adapter.socket_mode import SocketModeHandler

        app = App(token=bot_token)
        auth = app.client.auth_test()
        if auth.get("ok"):
            self._bot_user_id = str(auth.get("user_id") or "") or None
        self._register_handlers(app)
        handler = SocketModeHandler(app, app_token)
        self._app = app
        self._handler = handler
        handler.connect()
        client = getattr(handler, "client", None)
        if client is not None and hasattr(client, "is_connected") and not client.is_connected():
            raise RuntimeError("slack socket mode failed to connect")
        self._set_state("connected")
        log_event("slack_socket_connected", bot_user_id=self._bot_user_id)

    def _run_loop(self) -> None:
        bot_token = (os.environ.get("SLACK_BOT_TOKEN") or "").strip()
        app_token = (os.environ.get("SLACK_APP_TOKEN") or "").strip()
        reconnect_attempts = 0

        while not self._shutdown.is_set():
            self._set_state("connecting")
            try:
                self._close_handler()
                self._start_handler(bot_token, app_token)
                reconnect_attempts = 0
                while not self._shutdown.is_set() and self._handler is not None:
                    client = getattr(self._handler, "client", None)
                    if (
                        client is not None
                        and hasattr(client, "is_connected")
                        and not client.is_connected()
                    ):
                        break
                    if self._shutdown.wait(1.0):
                        break
                if self._shutdown.is_set():
                    break
                self._set_state("degraded", "socket disconnected")
                log_event("slack_socket_reconnect", reason="disconnected")
            except Exception as exc:  # noqa: BLE001
                if is_non_recoverable_auth_error(exc):
                    self._set_state("degraded", str(exc))
                    log_event("slack_socket_auth_error", error=str(exc))
                    break
                reconnect_attempts += 1
                if reconnect_attempts >= RECONNECT_POLICY["max_attempts"]:
                    self._set_state("degraded", f"max reconnect attempts reached: {exc}")
                    log_event("slack_socket_reconnect_exhausted", error=str(exc))
                    break
                delay = compute_backoff(reconnect_attempts)
                self._set_state("degraded", f"reconnect attempt {reconnect_attempts}: {exc}")
                log_event(
                    "slack_socket_reconnect",
                    attempt=reconnect_attempts,
                    delay_s=delay,
                    error=str(exc),
                )
                self._close_handler()
                if self._shutdown.wait(delay):
                    break

        self._close_handler()
