"""Google OAuth HTTP handlers."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from ts_harness.connectors.google_workspace.auth import oauth_flow, oauth_store
from ts_harness.gateway.server import get_runtime


def _success_html() -> str:
    return """<!DOCTYPE html><html><body><h1>Google Connected</h1><p>You can close this tab.</p></body></html>"""


def _error_html(msg: str) -> str:
    return f"""<!DOCTYPE html><html><body><h1>Connection Failed</h1><p>{msg}</p></body></html>"""


async def oauth_google_start(request: Request) -> Response:
    runtime = get_runtime()
    email = request.query_params.get("email", "").strip()
    tenant_id = request.query_params.get("tenant_id") or runtime.config.gateway.default_tenant_id
    if not email:
        return JSONResponse({"error": "email query parameter required"}, status_code=400)
    try:
        url = oauth_flow.build_consent_url(tenant_id=tenant_id, email=email)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    return RedirectResponse(url)


async def oauth_google_callback(request: Request) -> Response:
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    error = request.query_params.get("error")
    if error:
        return HTMLResponse(_error_html(error), status_code=400)
    if not code or not state:
        return HTMLResponse(_error_html("Missing code or state"), status_code=400)
    try:
        state_data = oauth_flow.parse_state(state)
        token_data = oauth_flow.exchange_code(code)
        access = token_data.get("access_token")
        refresh = token_data.get("refresh_token") or "__NO_REFRESH__"
        if not access:
            return HTMLResponse(_error_html("No access token returned"), status_code=500)
        google_email = oauth_flow.fetch_google_email(access)
        oauth_store.save_oauth_credentials(
            tenant_id=str(state_data.get("tenant_id") or "default"),
            email=str(state_data.get("email") or ""),
            access_token=access,
            refresh_token=refresh,
            google_email=google_email,
            expires_in=int(token_data.get("expires_in") or 3600),
        )
        return HTMLResponse(_success_html())
    except Exception as exc:  # noqa: BLE001
        return HTMLResponse(_error_html(str(exc)[:200]), status_code=500)
