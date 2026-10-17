"""Google Workspace tool handlers."""

from __future__ import annotations

from typing import Any, Callable

from ts_harness.connectors.google_workspace.client import GoogleClientFactory
from ts_harness.connectors.google_workspace.errors import format_delegation_error
from ts_harness.tools.policy import ToolPolicyContext

TORONTO_TZ = "America/Toronto"


def _tool_handler(
    factory: GoogleClientFactory,
    fn: Callable[[GoogleClientFactory, ToolPolicyContext, dict[str, Any]], Any],
) -> Callable[[dict[str, Any], ToolPolicyContext], Any]:
    def handler(args: dict[str, Any], ctx: ToolPolicyContext) -> Any:
        try:
            return fn(factory, ctx, args)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "delegation" in msg.lower() or "unauthorized" in msg.lower():
                return format_delegation_error(exc)
            return {"error": msg}

    return handler


def register_gmail_tools(registry, factory: GoogleClientFactory) -> list[str]:
    from ts_harness.tools.registry import ToolDefinition

    names: list[str] = []

    def search(factory: GoogleClientFactory, ctx: ToolPolicyContext, args: dict) -> dict:
        gmail = factory.build("gmail", "v1", ctx)
        limit = min(max(int(args.get("limit") or 12), 1), 25)
        res = (
            gmail.users()
            .messages()
            .list(userId="me", q=str(args.get("query") or ""), maxResults=limit)
            .execute()
        )
        rows = res.get("messages") or []
        emails = []
        for mid in rows:
            msg = (
                gmail.users()
                .messages()
                .get(
                    userId="me",
                    id=mid["id"],
                    format="metadata",
                    metadataHeaders=["Subject", "From", "Date"],
                )
                .execute()
            )
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            emails.append(
                {
                    "id": msg["id"],
                    "threadId": msg.get("threadId"),
                    "subject": headers.get("Subject"),
                    "from": headers.get("From"),
                    "date": headers.get("Date"),
                    "snippet": msg.get("snippet"),
                }
            )
        return {"count": len(emails), "emails": emails}

    def get_msg(factory: GoogleClientFactory, ctx: ToolPolicyContext, args: dict) -> dict:
        gmail = factory.build("gmail", "v1", ctx)
        msg = (
            gmail.users()
            .messages()
            .get(userId="me", id=str(args["message_id"]), format="full")
            .execute()
        )
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        body = ""
        stack = [msg.get("payload")]
        while stack:
            part = stack.pop()
            if not part:
                continue
            if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
                import base64

                body += base64.urlsafe_b64decode(part["body"]["data"] + "==").decode("utf-8", "ignore")
            stack.extend(part.get("parts") or [])
        if len(body) > 12000:
            body = body[:12000] + "\n… [truncated]"
        return {
            "id": msg["id"],
            "subject": headers.get("Subject"),
            "from": headers.get("From"),
            "to": headers.get("To"),
            "date": headers.get("Date"),
            "body": body,
        }

    def send(factory: GoogleClientFactory, ctx: ToolPolicyContext, args: dict) -> dict:
        import base64

        to_addr = str(args.get("to") or "")
        subject = str(args.get("subject") or "")
        body = str(args.get("body") or "")
        if args.get("dry_run") is True:
            return {
                "dryRun": True,
                "to": to_addr,
                "subject": subject,
                "body_preview": body[:500],
                "nextStep": "Retry with approval_id after user approval.",
            }
        gmail = factory.build("gmail", "v1", ctx)
        raw = base64.urlsafe_b64encode(
            f"To: {to_addr}\r\nSubject: {subject}\r\n\r\n{body}".encode()
        ).decode()
        sent = gmail.users().messages().send(userId="me", body={"raw": raw}).execute()
        return {"sent": True, "id": sent.get("id"), "threadId": sent.get("threadId")}

    specs = [
        (
            "google_workspace_gmail_search",
            "Search Gmail using query syntax (from:, subject:, newer_than:, etc.).",
            {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]},
            search,
        ),
        (
            "google_workspace_gmail_get",
            "Get full email body by message id from google_workspace_gmail_search.",
            {"type": "object", "properties": {"message_id": {"type": "string"}}, "required": ["message_id"]},
            get_msg,
        ),
        (
            "google_workspace_gmail_send",
            "Send plain-text email. Requires human approval.",
            {
                "type": "object",
                "properties": {
                    "to": {"type": "string"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                    "dry_run": {"type": "boolean"},
                    "approval_id": {"type": "string"},
                },
                "required": ["to", "subject", "body"],
            },
            send,
        ),
    ]
    for name, desc, schema, fn in specs:
        registry.register(
            ToolDefinition(
                name=name,
                description=desc,
                parameters=schema,
                handler=_tool_handler(factory, fn),
                toolsets=["google-workspace"],
            )
        )
        names.append(name)
    return names


def register_calendar_tools(registry, factory: GoogleClientFactory) -> list[str]:
    from ts_harness.tools.registry import ToolDefinition

    names: list[str] = []

    def list_events(factory: GoogleClientFactory, ctx: ToolPolicyContext, args: dict) -> dict:
        cal = factory.build("calendar", "v3", ctx)
        params: dict[str, Any] = {
            "calendarId": "primary",
            "singleEvents": True,
            "orderBy": "startTime",
            "maxResults": min(int(args.get("limit") or 20), 50),
        }
        if args.get("start_date"):
            params["timeMin"] = f"{args['start_date']}T00:00:00"
        if args.get("end_date"):
            params["timeMax"] = f"{args['end_date']}T23:59:59"
        res = cal.events().list(**params).execute()
        events = []
        for ev in res.get("items") or []:
            start = ev.get("start") or {}
            events.append(
                {
                    "id": ev.get("id"),
                    "summary": ev.get("summary"),
                    "start": start.get("dateTime") or start.get("date"),
                    "end": (ev.get("end") or {}).get("dateTime") or (ev.get("end") or {}).get("date"),
                    "htmlLink": ev.get("htmlLink"),
                }
            )
        return {"count": len(events), "events": events}

    def get_event(factory: GoogleClientFactory, ctx: ToolPolicyContext, args: dict) -> dict:
        cal = factory.build("calendar", "v3", ctx)
        ev = cal.events().get(calendarId="primary", eventId=str(args["event_id"])).execute()
        return {
            "id": ev.get("id"),
            "summary": ev.get("summary"),
            "description": (ev.get("description") or "")[:6000],
            "start": ev.get("start"),
            "end": ev.get("end"),
            "attendees": ev.get("attendees"),
            "htmlLink": ev.get("htmlLink"),
        }

    def create_event(factory: GoogleClientFactory, ctx: ToolPolicyContext, args: dict) -> dict:
        cal = factory.build("calendar", "v3", ctx)
        tz = str(args.get("timezone") or TORONTO_TZ)
        body: dict[str, Any] = {
            "summary": str(args.get("title") or ""),
            "start": {"dateTime": f"{args['date']}T{args['time']}:00", "timeZone": tz},
            "end": {
                "dateTime": f"{args['date']}T{args.get('end_time') or args['time']}:00",
                "timeZone": tz,
            },
        }
        if args.get("description"):
            body["description"] = str(args["description"])
        if args.get("attendees"):
            body["attendees"] = [{"email": e} for e in args["attendees"]]
        if args.get("add_meet"):
            body["conferenceData"] = {
                "createRequest": {"requestId": f"ec-{args['date']}-{args['time']}", "conferenceSolutionKey": {"type": "hangoutsMeet"}}
            }
            ev = (
                cal.events()
                .insert(calendarId="primary", body=body, conferenceDataVersion=1)
                .execute()
            )
        else:
            ev = cal.events().insert(calendarId="primary", body=body).execute()
        return {"created": True, "eventId": ev.get("id"), "htmlLink": ev.get("htmlLink")}

    def update_event(factory: GoogleClientFactory, ctx: ToolPolicyContext, args: dict) -> dict:
        cal = factory.build("calendar", "v3", ctx)
        event_id = str(args["event_id"])
        ev = cal.events().get(calendarId="primary", eventId=event_id).execute()
        if args.get("title"):
            ev["summary"] = str(args["title"])
        if args.get("description") is not None:
            ev["description"] = str(args["description"])
        updated = cal.events().update(calendarId="primary", eventId=event_id, body=ev).execute()
        return {"updated": True, "eventId": updated.get("id")}

    def cancel_event(factory: GoogleClientFactory, ctx: ToolPolicyContext, args: dict) -> dict:
        cal = factory.build("calendar", "v3", ctx)
        cal.events().delete(calendarId="primary", eventId=str(args["event_id"])).execute()
        return {"cancelled": True, "eventId": args["event_id"]}

    specs = [
        ("google_workspace_calendar_list", "List calendar events in optional date range.", {"type": "object", "properties": {"start_date": {"type": "string"}, "end_date": {"type": "string"}, "limit": {"type": "integer"}}}, list_events),
        ("google_workspace_calendar_get", "Get one calendar event by id.", {"type": "object", "properties": {"event_id": {"type": "string"}}, "required": ["event_id"]}, get_event),
        ("google_workspace_calendar_create", "Create calendar event. Requires approval.", {"type": "object", "properties": {"title": {"type": "string"}, "date": {"type": "string"}, "time": {"type": "string"}, "end_time": {"type": "string"}, "description": {"type": "string"}, "attendees": {"type": "array", "items": {"type": "string"}}, "add_meet": {"type": "boolean"}, "timezone": {"type": "string"}}, "required": ["title", "date", "time"]}, create_event),
        ("google_workspace_calendar_update", "Update calendar event. Requires approval.", {"type": "object", "properties": {"event_id": {"type": "string"}, "title": {"type": "string"}, "description": {"type": "string"}}, "required": ["event_id"]}, update_event),
        ("google_workspace_calendar_cancel", "Cancel/delete calendar event. Requires approval.", {"type": "object", "properties": {"event_id": {"type": "string"}}, "required": ["event_id"]}, cancel_event),
    ]
    for name, desc, schema, fn in specs:
        registry.register(
            ToolDefinition(name=name, description=desc, parameters=schema, handler=_tool_handler(factory, fn), toolsets=["google-workspace"])
        )
        names.append(name)
    return names


def register_drive_tools(registry, factory: GoogleClientFactory) -> list[str]:
    from ts_harness.tools.registry import ToolDefinition

    names: list[str] = []

    def search(factory: GoogleClientFactory, ctx: ToolPolicyContext, args: dict) -> dict:
        drive = factory.build("drive", "v3", ctx)
        q = f"fullText contains '{str(args.get('query') or '').replace(chr(39), chr(92)+chr(39))}' and trashed = false"
        res = (
            drive.files()
            .list(
                q=q,
                pageSize=min(int(args.get("limit") or 10), 25),
                fields="files(id,name,mimeType,modifiedTime,webViewLink)",
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
                corpora="allDrives",
            )
            .execute()
        )
        return {"count": len(res.get("files") or []), "files": res.get("files") or []}

    def get_file(factory: GoogleClientFactory, ctx: ToolPolicyContext, args: dict) -> dict:
        drive = factory.build("drive", "v3", ctx)
        f = (
            drive.files()
            .get(
                fileId=str(args["file_id"]),
                fields="id,name,mimeType,modifiedTime,webViewLink,owners",
                supportsAllDrives=True,
            )
            .execute()
        )
        return f

    def create_file(factory: GoogleClientFactory, ctx: ToolPolicyContext, args: dict) -> dict:
        drive = factory.build("drive", "v3", ctx)
        mime = {
            "document": "application/vnd.google-apps.document",
            "spreadsheet": "application/vnd.google-apps.spreadsheet",
        }.get(str(args.get("file_type") or "document"))
        body: dict[str, Any] = {"name": str(args.get("name") or "Untitled"), "mimeType": mime}
        if args.get("folder_id"):
            body["parents"] = [str(args["folder_id"])]
        created = drive.files().create(body=body, fields="id,name,webViewLink", supportsAllDrives=True).execute()
        return {"created": True, **created}

    specs = [
        ("google_workspace_drive_search", "Search Drive including shared drives.", {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]}, search),
        ("google_workspace_drive_get", "Get Drive file metadata.", {"type": "object", "properties": {"file_id": {"type": "string"}}, "required": ["file_id"]}, get_file),
        ("google_workspace_drive_create", "Create Google Doc or Sheet. Requires approval.", {"type": "object", "properties": {"name": {"type": "string"}, "file_type": {"type": "string", "enum": ["document", "spreadsheet"]}, "folder_id": {"type": "string"}}, "required": ["name", "file_type"]}, create_file),
    ]
    for name, desc, schema, fn in specs:
        registry.register(
            ToolDefinition(name=name, description=desc, parameters=schema, handler=_tool_handler(factory, fn), toolsets=["google-workspace"])
        )
        names.append(name)
    return names


def register_docs_tools(registry, factory: GoogleClientFactory) -> list[str]:
    from ts_harness.tools.registry import ToolDefinition

    names: list[str] = []

    def read_doc(factory: GoogleClientFactory, ctx: ToolPolicyContext, args: dict) -> dict:
        docs = factory.build("docs", "v1", ctx)
        doc_id = str(args["document_id"]).strip()
        if "/document/d/" in doc_id:
            doc_id = doc_id.split("/document/d/")[1].split("/")[0]
        doc = docs.documents().get(documentId=doc_id).execute()
        sections: list[dict] = []
        current = {"heading": "(start)", "text": ""}
        for el in doc.get("body", {}).get("content") or []:
            para = el.get("paragraph")
            if not para:
                continue
            style = (para.get("paragraphStyle") or {}).get("namedStyleType") or ""
            text = "".join(
                (run.get("textRun") or {}).get("content", "")
                for run in para.get("elements") or []
            ).strip()
            if not text:
                continue
            if "HEADING" in style:
                if current["text"]:
                    sections.append(current)
                current = {"heading": text, "text": ""}
            else:
                current["text"] += (text + "\n")
        if current["text"]:
            sections.append(current)
        return {"title": doc.get("title"), "documentId": doc_id, "sections": sections}

    def append_doc(factory: GoogleClientFactory, ctx: ToolPolicyContext, args: dict) -> dict:
        docs = factory.build("docs", "v1", ctx)
        doc_id = str(args["document_id"]).strip()
        if "/document/d/" in doc_id:
            doc_id = doc_id.split("/document/d/")[1].split("/")[0]
        content = str(args.get("content") or "")
        confirmed = args.get("confirmed") is True
        approval_id = str(args.get("approval_id") or "").strip()
        if not confirmed and approval_id:
            from ts_harness.tools.approvals import get_approval_queue

            clean_args = {
                k: v
                for k, v in args.items()
                if k not in {"approval_id", "confirmed"}
            }
            if get_approval_queue().is_approved_and_matches(
                approval_id,
                tool_name="google_workspace_docs_append",
                args=clean_args,
                tenant_id=ctx.tenant_id,
                agent_id=ctx.agent_id,
            ):
                confirmed = True
        doc = docs.documents().get(documentId=doc_id).execute()
        end_index = doc["body"]["content"][-1]["endIndex"] - 1
        insert_at = max(1, end_index)
        if not confirmed:
            return {
                "dryRun": True,
                "documentId": doc_id,
                "documentTitle": doc.get("title"),
                "insertAt": insert_at,
                "charsToInsert": len(content),
                "nextStep": "Call again with confirmed=true after user approval.",
            }
        docs.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": [{"insertText": {"location": {"index": insert_at}, "text": "\n" + content}}]},
        ).execute()
        return {"written": True, "documentId": doc_id, "charsInserted": len(content)}

    specs = [
        ("google_workspace_docs_read", "Read Google Doc sections by document id or URL.", {"type": "object", "properties": {"document_id": {"type": "string"}}, "required": ["document_id"]}, read_doc),
        ("google_workspace_docs_append", "Append to Google Doc with dry-run unless confirmed=true or approval_id.", {"type": "object", "properties": {"document_id": {"type": "string"}, "content": {"type": "string"}, "confirmed": {"type": "boolean"}, "approval_id": {"type": "string"}}, "required": ["document_id", "content"]}, append_doc),
    ]
    for name, desc, schema, fn in specs:
        registry.register(
            ToolDefinition(name=name, description=desc, parameters=schema, handler=_tool_handler(factory, fn), toolsets=["google-workspace"])
        )
        names.append(name)
    return names
