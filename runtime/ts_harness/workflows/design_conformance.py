"""Nightly design conformance: every delivered screen captured against the real API, compared.

Each UI work order is checked against its own frames before merge (frame captures in QA). What
that can't see: a later ticket changing a screen an earlier one built, shared pieces (the shell,
page backgrounds, a grid) drifting, a screen that only looks right with the fake API's data.

So when QA passes a work order that names design frames, the frames are recorded for its web
repository (route, size, the design image). The repository's nightly smoke run (react-web
template) asks for that list, opens each route at the frame's size against the real API and a
real database, pushes the screenshots to ``ts-harness-evidence/design-conformance`` and reports
the commit here. Each capture is compared with its frame: where the design is a flat colour,
the colour at the same place in the capture is read, and pairs that differ are listed (a beige
page behind a white design, white rows where the design is grey). Routes that could not be
captured are listed too. New findings become one proposed bug per night, like smoke failures:
the bug program's gates wait for a person before anything reaches Jira.

Layout and missing panels need judgement, not sampling: the bug lists each frame with its
Figma link and its capture, so the bug's investigation (and the person approving it) can look.

Auth, de-duplication and the program started are the nightly smoke report's
(``ts_harness.workflows.nightly_smoke``). State lives in ``runtime_kv``: the frame list in
namespace ``design-frames``, findings already proposed in ``design-conformance``.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

FRAMES_NAMESPACE = "design-frames"
FINDINGS_NAMESPACE = "design-conformance"
EVIDENCE_BRANCH = "ts-harness-evidence/design-conformance"
MAX_FRAMES_PER_REPO = 60
# A colour pair seen at fewer sample points than this is noise (a moved edge, a tooltip).
MIN_MISMATCH_POINTS = 4
RECENT_SECONDS = 7 * 24 * 3600
_REPO = re.compile(r"github\.com[/:]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$")


def repo_name(repo_url: str) -> str:
    """``owner/name`` of a GitHub URL (or of ``owner/name`` itself); empty when it isn't one."""
    text = str(repo_url or "").strip()
    match = _REPO.search(text)
    if match:
        return match.group(1)
    return text if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", text) else ""


def frame_key(object_key: str) -> str:
    """A stable short key per design image: the same frame delivered twice is one entry."""
    return "f" + hashlib.sha256(str(object_key).encode()).hexdigest()[:12]


def delivered_frames(
    work_order_body: str, refs: list[dict[str, Any]], *, work_order_ref: str = ""
) -> list[dict[str, Any]]:
    """The frames a passed work order names, with route, size and design image, ready to record."""
    from ts_harness.implementation.frame_checklist import frames_to_capture, work_order_frames

    by_id = {str(ref.get("id")): ref for ref in refs if isinstance(ref, dict)}
    sizes = {frame["id"]: frame for frame in frames_to_capture(work_order_body)}
    out: list[dict[str, Any]] = []
    for frame in work_order_frames(work_order_body):
        ref = by_id.get(frame["id"]) or {}
        route = str(frame.get("route") or "").strip()
        if not route.startswith("/") or not ref.get("object_key"):
            continue
        size = sizes.get(frame["id"]) or {}
        out.append(
            {
                "key": frame_key(str(ref["object_key"])),
                "id": frame["id"],
                "title": frame.get("title") or ref.get("title") or "",
                "route": route.split()[0],
                "state": frame.get("state") or "",
                "width": int(size.get("width") or 1440),
                "height": int(size.get("height") or 900),
                "object_key": ref["object_key"],
                "url": ref.get("url") or "",
                "work_order_ref": work_order_ref,
            }
        )
    return out


def record_frames(
    repo: str, frames: list[dict[str, Any]], *, memory: Any, now: float | None = None
) -> int:
    """Add or refresh ``frames`` in the repository's list; the oldest go past ``MAX_FRAMES_PER_REPO``."""
    from ts_harness.storage.kv import kv_update

    if not repo or not frames:
        return 0
    now = time.time() if now is None else now

    def merge(value: Any) -> Any:
        state = dict(value or {})
        known = dict(state.get("frames") or {})
        for frame in frames:
            known[frame["key"]] = {**frame, "recorded_at": now}
        newest = sorted(known.values(), key=lambda f: -float(f.get("recorded_at") or 0))[
            :MAX_FRAMES_PER_REPO
        ]
        state["frames"] = {f["key"]: f for f in newest}
        return state

    kv_update(FRAMES_NAMESPACE, f"repo:{repo}", merge, default={}, client=memory)
    return len(frames)


def record_passed_work_order(
    task: dict[str, Any], tasks: list[dict[str, Any]], task_input: dict[str, Any], *, memory: Any
) -> int:
    """After a QA pass: record the work order's frames for its repository. Best effort."""
    from ts_harness.intake.design import refs_named_by_work_order
    from ts_harness.workflows.design_input import design_refs_for_task

    body = str(task_input.get("work_order_body") or "")
    repo = repo_name(str(task_input.get("repo_url") or ""))
    if not body or not repo:
        return 0
    refs = refs_named_by_work_order(design_refs_for_task(task, tasks, task_input), body)
    frames = delivered_frames(
        body,
        refs,
        work_order_ref=str(
            task_input.get("work_order_ref") or task_input.get("work_order_id") or ""
        ),
    )
    return record_frames(repo, frames, memory=memory)


def frames_manifest(repo: str, *, memory: Any) -> dict[str, Any]:
    """What the nightly run captures: ``{frames: [{key, title, route, width, height}]}``."""
    value = (memory.kv_get(FRAMES_NAMESPACE, f"repo:{repo}").get("value") or {}) if repo else {}
    frames = sorted(
        (value.get("frames") or {}).values(),
        key=lambda f: (f.get("route") or "", f.get("id") or ""),
    )
    return {
        "frames": [
            {k: frame.get(k) for k in ("key", "id", "title", "route", "width", "height")}
            for frame in frames
        ]
    }


def compare_capture(design_png: bytes, capture_png: bytes) -> dict[str, Any]:
    """Colour pairs that differ between a frame and its capture (see ``frame_review.sample_colours``)."""
    from ts_harness.workflows.frame_review import _image, sample_colours

    sampled = sample_colours(_image(design_png), _image(capture_png))
    sampled["mismatches"] = [m for m in sampled["mismatches"] if m["points"] >= MIN_MISMATCH_POINTS]
    return sampled


def findings_for_run(
    repo: str,
    commit: str,
    *,
    memory: Any,
    store: Any,
    github: Any,
) -> list[dict[str, Any]]:
    """One finding per frame whose capture failed or whose colours differ from the design."""
    import base64
    import json

    known = (memory.kv_get(FRAMES_NAMESPACE, f"repo:{repo}").get("value") or {}).get("frames") or {}
    owner, name = repo.split("/", 1)

    def read(path: str) -> bytes:
        payload = github.get_file(owner, name, path, ref=commit) or {}
        return base64.b64decode(str(payload.get("content") or "").replace("\n", ""))

    try:
        results = json.loads(read("manifest.json").decode("utf-8")).get("frames") or []
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"the capture commit has no readable manifest.json: {exc}") from exc
    findings: list[dict[str, Any]] = []
    for result in results:
        frame = known.get(str(result.get("key") or ""))
        if frame is None:
            continue
        base = {
            k: frame.get(k)
            for k in ("key", "id", "title", "route", "width", "height", "url", "work_order_ref")
        }
        base["capture"] = (
            f"https://github.com/{repo}/blob/{commit}/{result.get('file')}?raw=true"
            if result.get("file")
            else ""
        )
        if result.get("error") or not result.get("file"):
            findings.append(
                {
                    **base,
                    "kind": "not_captured",
                    "detail": str(result.get("error") or "no screenshot")[:300],
                }
            )
            continue
        try:
            compared = compare_capture(
                store.get(str(frame["object_key"])), read(str(result["file"]))
            )
        except Exception as exc:  # noqa: BLE001 - one frame that can't be read doesn't stop the rest
            logger.warning("design conformance: %s %s not compared: %s", repo, frame.get("id"), exc)
            continue
        if compared["mismatches"]:
            findings.append(
                {
                    **base,
                    "kind": "colour",
                    "mismatches": compared["mismatches"],
                    "compared_points": compared["compared_points"],
                }
            )
    return findings


def finding_print(repo: str, finding: dict[str, Any]) -> str:
    """De-duplication key: the frame and what is wrong with it (the colour pairs, or not captured)."""
    pairs = ",".join(
        sorted(f"{m['design']}>{m['capture']}" for m in finding.get("mismatches") or [])
    )
    raw = f"{repo}|{finding.get('key')}|{finding.get('kind')}|{pairs}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def bug_summary(repo: str, run_url: str, findings: list[dict[str, Any]]) -> str:
    """The proposed bug's summary: each frame, what differs, and where to see both."""
    lines = [
        f"Nightly design check: {len(findings)} screen{'s' if len(findings) != 1 else ''} of {repo} differ from "
        "their design frames when run against the real API.",
        f"Run: {run_url}" if run_url else "",
        "",
        "Each capture was taken at its frame's size. Colours were sampled at the same places in both images, "
        "only where both are a flat colour (text and icons skipped). A layout change can explain a pair; "
        "compare the images before fixing.",
        "",
    ]
    for finding in findings:
        head = f"- {finding.get('id')} · {finding.get('title')} (route {finding.get('route')}, {finding.get('width')}×{finding.get('height')})"
        if finding.get("work_order_ref"):
            head += f", built by {finding['work_order_ref']}"
        lines.append(head)
        if finding.get("url"):
            lines.append(f"  - Design: {finding['url']}")
        if finding.get("capture"):
            lines.append(f"  - Capture: {finding['capture']}")
        if finding["kind"] == "not_captured":
            lines.append(f"  - Could not be captured: {finding.get('detail')}")
        for pair in finding.get("mismatches") or []:
            lines.append(
                f"  - Design {pair['design']}, app {pair['capture']} at {pair['points']} of "
                f"{finding.get('compared_points')} sampled points"
            )
    return "\n".join(line for line in lines if line is not None).strip()


def handle_frames_report(
    body: Any,
    token: str,
    *,
    tenant_id: str | None = None,
    memory: Any = None,
    service: Any = None,
    store: Any = None,
    github: Any = None,
    secret: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """A nightly run captured the repository's frames: compare them and propose one bug for what's new."""
    from ts_harness.storage.kv import kv_update
    from ts_harness.workflows.nightly_smoke import (
        ACTOR,
        ENTRY_NODE,
        PROGRAM_TYPE,
        SECRET_ENV,
        SmokeReportError,
        _text,
        report_secret,
        token_matches,
    )

    if not (report_secret() if secret is None else secret):
        raise SmokeReportError(503, f"smoke reports are off: {SECRET_ENV} is not set")
    repo = _text(body.get("repo"), 200) if isinstance(body, dict) else ""
    if not repo or not token_matches(repo, token, secret):
        raise SmokeReportError(401, "invalid smoke report token for this repository")
    commit = _text(body.get("commit"), 64)
    if not re.fullmatch(r"[0-9a-f]{7,64}", commit):
        raise SmokeReportError(400, "commit must be the capture commit's sha")
    run_url = _text(body.get("run_url"), 500)
    if memory is None:
        from ts_harness.memory.client import get_memory_client

        memory = get_memory_client()
    if store is None:
        from ts_harness.intake.blobs import intake_store

        store = intake_store()
    if github is None:
        from ts_harness.integrations.github_client import GitHubClient

        github = GitHubClient.from_env()
    try:
        findings = findings_for_run(repo, commit, memory=memory, store=store, github=github)
    except ValueError as exc:
        raise SmokeReportError(400, str(exc)) from exc
    if not findings:
        return {"status": "conforms", "repo": repo, "findings": 0}
    now = time.time() if now is None else now
    prints = {finding_print(repo, f): f for f in findings}
    before = (memory.kv_get(FINDINGS_NAMESPACE, f"repo:{repo}").get("value") or {}).get(
        "findings"
    ) or {}
    new = [
        fp
        for fp in prints
        if fp not in before or now - float(before[fp].get("at") or 0) > RECENT_SECONDS
    ]
    if not new:
        return {"status": "duplicate", "repo": repo, "findings": len(findings), "new_findings": 0}

    def claim(value: Any) -> Any:
        state = dict(value or {})
        seen = {
            fp: v
            for fp, v in dict(state.get("findings") or {}).items()
            if now - float(v.get("at") or 0) <= RECENT_SECONDS
        }
        for fp in new:
            seen[fp] = {"at": now, "frame": prints[fp].get("id"), "kind": prints[fp].get("kind")}
        state["findings"] = seen
        return state

    kv_update(FINDINGS_NAMESPACE, f"repo:{repo}", claim, default={}, client=memory)
    if service is None:
        from ts_harness.workflows.delivery_program import get_delivery_program_service

        service = get_delivery_program_service()
    from ts_harness.workflows.delivery_program import resolve_program_tenant

    result = service.create_program(
        entry_node=ENTRY_NODE,
        program_type=PROGRAM_TYPE,
        summary=bug_summary(repo, run_url, [prints[fp] for fp in new]),
        tenant_id=resolve_program_tenant(tenant_id),
        binding_key=f"web:web:design-conformance-{repo.replace('/', '-')}-{commit[:12]}",
        requester_actor_id=ACTOR,
        artifacts={"repo_url": f"https://github.com/{repo}"},
        actor=ACTOR,
    )
    program_id = str(getattr(result, "program_id", "") or "")
    logger.info(
        "design conformance for %s proposed program %s (%d new findings)",
        repo,
        program_id,
        len(new),
    )
    return {
        "status": "proposed",
        "repo": repo,
        "program_id": program_id,
        "findings": len(findings),
        "new_findings": len(new),
    }
