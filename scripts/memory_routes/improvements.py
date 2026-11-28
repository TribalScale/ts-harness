"""Self-improvement proposals and replay cases (migration 056).

The runtime (``ts_harness.improvement``) decides what a proposal is and when it moves; these routes
keep the rows, refuse a status change the lifecycle does not allow, and make every change a
compare-and-swap on the status the caller saw (``expected_status``), so two workers (or a worker
and a person in the Inbox) cannot both move one proposal.

``TRANSITIONS`` must match ``ts_harness.improvement.proposals.TRANSITIONS`` (a test checks it).
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from psycopg.types.json import Jsonb

TYPES = ("knowledge_note", "skill_edit", "setting_change", "fragment_change", "needs_design")
STATUSES = ("proposed", "evaluating", "canary", "awaiting_approval", "promoted", "rolled_back", "rejected")
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "proposed": ("evaluating", "promoted", "rejected"),
    "evaluating": ("proposed", "canary", "rejected"),
    "canary": ("awaiting_approval", "rolled_back"),
    "awaiting_approval": ("promoted", "rolled_back", "rejected"),
    "promoted": (),
    "rolled_back": (),
    "rejected": (),
}
TERMINAL = ("promoted", "rolled_back", "rejected")
REPLAY_KINDS = ("pre_push_review",)
MAX_HISTORY = 50
MAX_EVALUATIONS = 20
_COLUMNS = (
    "id, tenant_id, type, status, target, title, summary, pattern_key, metric, expected_effect, risk, change, "
    "evidence, evaluation, evaluations, canary, promotion, history, cost_usd, digest_id, created_by, decided_by, "
    "decided_at, created_at, updated_at"
)


class ProposalConflict(RuntimeError):
    """The proposal is not in the status the caller expected (someone else moved it)."""

    def __init__(self, message: str, current: str):
        super().__init__(message)
        self.code = "status_conflict"
        self.current = current


def _tenant(body: dict[str, Any]) -> str:
    return str(body.get("tenant_id") or "default")


def _text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _cost(value: Any) -> Decimal:
    try:
        amount = Decimal(str(value or 0))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("cost must be a number") from exc
    if not amount.is_finite() or amount < 0 or amount > 1000:
        raise ValueError("cost must be between 0 and 1000")
    return amount.quantize(Decimal("0.0001"))


def _row(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["id"] = int(out["id"])
    out["cost_usd"] = round(float(out.get("cost_usd") or 0), 4)
    for key in ("created_at", "updated_at", "decided_at"):
        if out.get(key) is not None and hasattr(out[key], "isoformat"):
            out[key] = out[key].isoformat()
    return out


def _proposal_id(value: Any) -> int:
    try:
        number = int(str(value).removeprefix("proposal-"))
    except (TypeError, ValueError) as exc:
        raise LookupError("proposal not found") from exc
    if number <= 0:
        raise LookupError("proposal not found")
    return number


def list_proposals(conn, params: dict[str, Any]) -> dict[str, Any]:
    """Proposals newest first; ``status`` may be a comma list, ``pattern_key`` and ``target`` filter."""
    tenant = _tenant(params)
    clauses, args = ["tenant_id = %s"], [tenant]
    statuses = [s for s in str(params.get("status") or "").split(",") if s.strip()]
    if statuses:
        unknown = [s for s in statuses if s.strip() not in STATUSES]
        if unknown:
            raise ValueError(f"unknown status: {', '.join(unknown)}")
        clauses.append("status = ANY(%s)")
        args.append([s.strip() for s in statuses])
    for key in ("pattern_key", "target", "type"):
        if str(params.get(key) or "").strip():
            clauses.append(f"{key} = %s")
            args.append(str(params[key]).strip())
    try:
        limit = max(1, min(200, int(params.get("limit") or 100)))
    except ValueError:
        limit = 100
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLUMNS} FROM improvement_proposals WHERE {' AND '.join(clauses)} "
            "ORDER BY created_at DESC, id DESC LIMIT %s",
            (*args, limit),
        )
        rows = [_row(row) for row in cur.fetchall()]
    return {"proposals": rows}


def get_proposal(conn, proposal_id: Any, params: dict[str, Any] | None = None) -> dict[str, Any]:
    tenant = _tenant(params or {})
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_COLUMNS} FROM improvement_proposals WHERE id = %s AND tenant_id = %s",
                    (_proposal_id(proposal_id), tenant))
        row = cur.fetchone()
    if row is None:
        raise LookupError("proposal not found")
    return _row(row)


def create_proposal(conn, body: dict[str, Any]) -> dict[str, Any]:
    kind = str(body.get("type") or "").strip()
    if kind not in TYPES:
        raise ValueError(f"type must be one of {', '.join(TYPES)}")
    title = _text(body.get("title"), 200)
    if not title:
        raise ValueError("title is required")
    status = str(body.get("status") or "proposed").strip()
    if status != "proposed":
        raise ValueError("a new proposal starts as proposed")
    actor = _text(body.get("actor"), 120) or "harness-improver"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO improvement_proposals
                (tenant_id, type, target, title, summary, pattern_key, metric, expected_effect, risk, change,
                 evidence, evaluation, history, cost_usd, digest_id, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING {_COLUMNS}
            """,
            (
                _tenant(body), kind, _text(body.get("target"), 300), title, _text(body.get("summary"), 4000),
                _text(body.get("pattern_key"), 300), _text(body.get("metric"), 300),
                _text(body.get("expected_effect"), 1000), _text(body.get("risk"), 1000),
                Jsonb(dict(body.get("change") or {})), Jsonb(dict(body.get("evidence") or {})),
                Jsonb(body["evaluation"]) if isinstance(body.get("evaluation"), dict) else None,
                Jsonb([{"status": "proposed", "actor": actor, "reason": _text(body.get("reason"), 500)}]),
                _cost(body.get("cost_usd")), _text(body.get("digest_id"), 120) or None, actor,
            ),
        )
        row = cur.fetchone()
    conn.commit()
    return _row(row)


def update_proposal(conn, proposal_id: Any, body: dict[str, Any]) -> dict[str, Any]:
    """Change a proposal: a status move (checked against ``TRANSITIONS``), and/or its evaluation,
    canary, promotion or change; ``add_cost_usd`` adds to its cost. ``expected_status`` makes the
    write conditional (409 ``status_conflict`` when it moved)."""
    tenant = _tenant(body)
    number = _proposal_id(proposal_id)
    actor = _text(body.get("actor"), 120) or "harness-improver"
    reason = _text(body.get("reason"), 1000)
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_COLUMNS} FROM improvement_proposals WHERE id = %s AND tenant_id = %s FOR UPDATE",
                    (number, tenant))
        current = cur.fetchone()
        if current is None:
            conn.rollback()
            raise LookupError("proposal not found")
        expected = str(body.get("expected_status") or "").strip()
        if expected and expected != current["status"]:
            conn.rollback()
            raise ProposalConflict(f"proposal is {current['status']}, not {expected}", current["status"])
        sets: list[str] = ["updated_at = now()"]
        args: list[Any] = []
        history = list(current.get("history") or [])
        status = str(body.get("status") or "").strip()
        if status and status != current["status"]:
            if status not in STATUSES:
                conn.rollback()
                raise ValueError(f"unknown status: {status}")
            if status not in TRANSITIONS[current["status"]]:
                conn.rollback()
                raise ProposalConflict(f"a {current['status']} proposal cannot become {status}", current["status"])
            sets.append("status = %s")
            args.append(status)
            if status in TERMINAL:
                sets += ["decided_by = %s", "decided_at = now()"]
                args.append(actor)
            history.append({"status": status, "from": current["status"], "actor": actor, "reason": reason,
                            "at": _now_text(cur)})
        elif reason and body.get("note"):
            history.append({"note": reason, "actor": actor, "at": _now_text(cur)})
        sets.append("history = %s")
        args.append(Jsonb(history[-MAX_HISTORY:]))
        if isinstance(body.get("evaluation"), dict):
            evaluations = [*list(current.get("evaluations") or []), body["evaluation"]][-MAX_EVALUATIONS:]
            sets += ["evaluation = %s", "evaluations = %s"]
            args += [Jsonb(body["evaluation"]), Jsonb(evaluations)]
        for key in ("canary", "promotion", "change", "evidence"):
            if isinstance(body.get(key), dict):
                sets.append(f"{key} = %s")
                args.append(Jsonb(body[key]))
        if body.get("add_cost_usd") not in (None, "", 0):
            sets.append("cost_usd = cost_usd + %s")
            args.append(_cost(body["add_cost_usd"]))
        try:
            cur.execute(
                f"UPDATE improvement_proposals SET {', '.join(sets)} WHERE id = %s RETURNING {_COLUMNS}",
                (*args, number),
            )
        except Exception as exc:  # the one-live-experiment-per-target index
            conn.rollback()
            if "uq_improvement_proposals_live_target" in str(exc):
                raise ProposalConflict("another proposal is already live on this target", current["status"]) from exc
            raise
        row = cur.fetchone()
    conn.commit()
    return _row(row)


def _now_text(cur) -> str:
    cur.execute("SELECT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS now")
    return str(cur.fetchone()["now"])


# Replay cases


def _kind(value: Any) -> str:
    kind = str(value or "pre_push_review").strip()
    if kind not in REPLAY_KINDS:
        raise ValueError(f"kind must be one of {', '.join(REPLAY_KINDS)}")
    return kind


def _case(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["id"] = int(out["id"])
    for key in ("gate_at", "created_at"):
        if out.get(key) is not None and hasattr(out[key], "isoformat"):
            out[key] = out[key].isoformat()
    return out


_CASE_COLUMNS = ("id, tenant_id, kind, workflow_id, pr_url, head_sha, truth_verdict, truth_findings, inputs, verdicts, "
                 "gate_at, created_at")


def list_replay_cases(conn, params: dict[str, Any]) -> dict[str, Any]:
    """Newest first; ``inputs=0`` leaves the (large) inputs out; ``workflow_ids`` (comma) filters."""
    tenant, kind = _tenant(params), _kind(params.get("kind"))
    try:
        limit = max(1, min(500, int(params.get("limit") or 100)))
    except ValueError:
        limit = 100
    columns = _CASE_COLUMNS if str(params.get("inputs") or "1") not in {"0", "false"} else \
        _CASE_COLUMNS.replace("inputs, ", "")
    clauses, args = ["tenant_id = %s", "kind = %s"], [tenant, kind]
    ids = [item.strip() for item in str(params.get("workflow_ids") or "").split(",") if item.strip()]
    if ids:
        clauses.append("workflow_id = ANY(%s)")
        args.append(ids)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {columns} FROM improvement_replay_cases WHERE {' AND '.join(clauses)} "
            "ORDER BY gate_at DESC NULLS LAST, id DESC LIMIT %s",
            (*args, limit),
        )
        return {"cases": [_case(row) for row in cur.fetchall()]}


def put_replay_case(conn, body: dict[str, Any]) -> dict[str, Any]:
    """Insert or refresh a case (keyed by tenant, kind and workflow); its cached verdicts are kept
    only when the head SHA is unchanged."""
    workflow_id = _text(body.get("workflow_id"), 200)
    if not workflow_id:
        raise ValueError("workflow_id is required")
    truth = str(body.get("truth_verdict") or "").strip()
    if truth not in {"block", "pass"}:
        raise ValueError("truth_verdict must be block or pass")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO improvement_replay_cases
                (tenant_id, kind, workflow_id, pr_url, head_sha, truth_verdict, truth_findings, inputs, gate_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, kind, workflow_id) DO UPDATE SET
                pr_url = EXCLUDED.pr_url, truth_verdict = EXCLUDED.truth_verdict,
                truth_findings = EXCLUDED.truth_findings, inputs = EXCLUDED.inputs, gate_at = EXCLUDED.gate_at,
                verdicts = CASE WHEN improvement_replay_cases.head_sha = EXCLUDED.head_sha
                                THEN improvement_replay_cases.verdicts ELSE '{{}}'::jsonb END,
                head_sha = EXCLUDED.head_sha
            RETURNING {_CASE_COLUMNS}
            """,
            (
                _tenant(body), _kind(body.get("kind")), workflow_id, _text(body.get("pr_url"), 500),
                _text(body.get("head_sha"), 80), truth, Jsonb(list(body.get("truth_findings") or [])[:50]),
                Jsonb(dict(body.get("inputs") or {})), body.get("gate_at") or None,
            ),
        )
        row = cur.fetchone()
    conn.commit()
    return _case(row)


def put_replay_verdict(conn, case_id: Any, body: dict[str, Any]) -> dict[str, Any]:
    """Cache one reviewer version's verdict on a case (``key`` is the version's hash)."""
    key = _text(body.get("key"), 100)
    if not key or not isinstance(body.get("verdict"), dict):
        raise ValueError("key and verdict are required")
    try:
        number = int(case_id)
    except (TypeError, ValueError) as exc:
        raise LookupError("case not found") from exc
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE improvement_replay_cases SET verdicts = verdicts || jsonb_build_object(%s::text, %s::jsonb) "
            "WHERE id = %s AND tenant_id = %s RETURNING id",
            (key, Jsonb(body["verdict"]), number, _tenant(body)),
        )
        row = cur.fetchone()
    conn.commit()
    if row is None:
        raise LookupError("case not found")
    return {"id": int(row["id"]), "key": key}


def prune_replay_cases(conn, body: dict[str, Any]) -> dict[str, Any]:
    """Delete cases whose gate verdict is older than ``days`` (default 90)."""
    try:
        days = max(7, min(365, int(body.get("days") or 90)))
    except (TypeError, ValueError):
        days = 90
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM improvement_replay_cases WHERE tenant_id = %s "
            "AND COALESCE(gate_at, created_at) < now() - make_interval(days => %s)",
            (_tenant(body), days),
        )
        deleted = cur.rowcount
    conn.commit()
    return {"deleted": int(deleted or 0), "days": days}
