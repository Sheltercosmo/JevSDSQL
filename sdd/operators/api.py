"""Authenticated operator execution; promotion and approvals require reviewer identity."""

import inspect
import time
from dataclasses import asdict

from fastapi import Body, Depends, HTTPException
from pydantic import Field
from sqlalchemy import update

from ..ir import Strict
from ..ledger import digest
from . import schema
from .service import OperatorService, manifest, operator_options
from .store import Store
from .usage import by_operator, examples


class OperatorInput(Strict):
    operator: str = Field(min_length=1, max_length=80)
    arguments: dict = Field(default_factory=dict)
    limits: dict = Field(default_factory=dict)
    policy: dict = Field(default_factory=dict)
    approval_id: str | None = None


class ApprovalInput(OperatorInput):
    valid_seconds: int = Field(default=300, ge=1, le=3600)


def mount(app, db, decisions, identity, reviewer):
    request_examples = {
        entry["operator"]: {"summary": entry["purpose"], "value": entry["request"]}
        for entry in examples()
        if entry["operator"] in {"JEV.NOUL", "JEV.TAG", "JEV.WORKFLOW", "JEV.EXPLAIN_PLAN"}
    }

    @app.get("/jev/operators", tags=["JEV operators"])
    def operators(p=Depends(identity)):
        data = manifest()
        usage = by_operator()
        return {
            "spec_version": data["spec_version"],
            "functions": [
                {
                    **f,
                    "usage": usage[f["name"]],
                    "implemented_signature": str(
                        inspect.signature(
                            getattr(OperatorService, f["name"].removeprefix("JEV.").lower())
                        )
                    ),
                }
                for f in data["functions"]
            ],
            "workflow": usage["JEV.WORKFLOW"],
            "output_states": ["VALUE", "UNKNOWN", "NOT_EVALUATED"],
            "runtime": "Bounded SDD compositions of Noul, Choice and Score; WORKFLOW adds conditional stage execution",
            "notes": "Native names in this catalog are SDD interfaces, not additional provider primitives. See /jev/operators/contracts.",
        }

    @app.get("/jev/operators/examples", tags=["JEV operators"])
    def operator_examples(p=Depends(identity)):
        return {"examples": examples()}

    @app.get("/jev/operators/contracts", tags=["JEV operators"])
    def contracts(p=Depends(identity)):
        return {
            "value_states": {
                "VALUE": "Resolved value, including false or zero",
                "UNKNOWN": "Evaluated or derived answer remains ambiguous",
                "NOT_EVALUATED": "No semantic result: unexecuted, skipped, blocked or failed attempt",
            },
            "operational_states": [
                "SUCCEEDED",
                "SKIPPED",
                "FAILED",
                "BLOCKED_BY_BUDGET",
                "BLOCKED_BY_DEPENDENCY",
                "BLOCKED_BY_POLICY",
                "TRUNCATED",
                "CANCELLED",
                "STALE",
            ],
            "scope": "Registered datasets or explicitly supplied subjects; tenant token controls catalog/cache access.",
            "materialization": "Versioned generations support explicit refresh or bounded on_change subscriptions. Only fully resolved, fresh generations are published.",
            "token_accounting": "Conservative UTF-8 byte upper bound, not the native tokenizer.",
            "policy": "Threshold defaults are unvalidated application policy, not universal correctness guarantees.",
        }

    @app.post("/jev/call", tags=["JEV operators"])
    def call(body: OperatorInput = Body(openapi_examples=request_examples), p=Depends(identity)):
        try:
            return OperatorService(
                db, decisions, p["tenant"], p["name"], p.get("role", "reader")
            ).call(**body.model_dump())
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except (TypeError, KeyError, AttributeError) as exc:
            raise HTTPException(
                422,
                "Invalid operator argument structure; see the implemented signature in /jev/operators",
            ) from exc

    @app.post("/jev/approve", tags=["JEV operators"])
    def approve(body: ApprovalInput, p=Depends(reviewer)):
        if body.approval_id:
            raise ValueError("An approval request cannot reuse another approval")
        limits, policy = operator_options(body.operator, body.limits, body.policy)
        request = {
            "source_signature": OperatorService(
                db, decisions, p["tenant"], p["name"]
            ).source_signature(body.arguments, body.operator),
            "operator": body.operator.upper().removeprefix("JEV."),
            "arguments": body.arguments,
            "limits": asdict(limits),
            "policy": asdict(policy),
        }
        store = Store(db, p["tenant"], p["name"])
        approval = store.add(
            schema.approvals,
            actor=p["name"],
            plan_hash=digest(request),
            limits=request["limits"],
            expires_at=time.time() + body.valid_seconds,
        )
        return {
            "approval_id": approval["id"],
            "plan_hash": approval["plan_hash"],
            "expires_at": approval["expires_at"],
            "provider_and_hard_limits_unchanged": True,
        }

    @app.get("/jev/runs/{run_id}", tags=["JEV operators"])
    def run(run_id: str, p=Depends(identity)):
        return Store(db, p["tenant"], p["name"]).get(schema.runs, run_id)

    @app.post("/jev/runs/{run_id}/resume", tags=["JEV operators"])
    def resume(run_id: str, p=Depends(identity)):
        store = Store(db, p["tenant"], p["name"])
        previous = store.get(schema.runs, run_id)
        if previous["actor"] != p["name"] and p.get("role") != "reviewer":
            raise HTTPException(403, "Only the owner or reviewer can resume this run")
        if previous["state"] in {"RUNNING", "RESUMED"}:
            raise ValueError("Run is active or has already been resumed")
        request = previous["request"]
        limits = dict(request["limits"])
        usage = previous["result"].get("manifest", {})
        for limit, used in (
            ("max_judgments", "reserved_judgments"),
            ("max_requests", "reserved_requests"),
            ("max_input_tokens", "reserved_input_tokens"),
        ):
            limits[limit] = max(0, limits[limit] - usage.get(used, 0))
        from .budget import Budget

        limits["max_input_usd"] = max(
            0,
            limits["max_input_usd"] - usage.get("reserved_input_tokens", 0) * Budget().price / 1e6,
        )
        service = OperatorService(db, decisions, p["tenant"], p["name"], p.get("role", "reader"))
        if request.get("source_signature", {}) != service.source_signature(
            request["arguments"], request["operator"]
        ):
            raise ValueError("Source changed; submit a new request for the new population")
        with db.transaction(p["tenant"]) as connection:
            changed = connection.execute(
                update(schema.runs)
                .where(
                    schema.runs.c.tenant == p["tenant"],
                    schema.runs.c.id == run_id,
                    schema.runs.c.state == previous["state"],
                )
                .values(state="RESUMED")
            )
            if not changed.rowcount:
                raise ValueError("Another request already resumed or changed this run")
        result = service.call(request["operator"], request["arguments"], limits, request["policy"])
        result["resumed_from"] = run_id
        return result

    @app.post("/jev/runs/{run_id}/cancel", tags=["JEV operators"])
    def cancel(run_id: str, p=Depends(identity)):
        store = Store(db, p["tenant"], p["name"])
        previous = store.get(schema.runs, run_id)
        if previous["actor"] != p["name"] and p.get("role") != "reviewer":
            raise HTTPException(403, "Only the owner or reviewer can cancel this run")
        with db.transaction(p["tenant"]) as connection:
            changed = connection.execute(
                update(schema.runs)
                .where(
                    schema.runs.c.id == run_id,
                    schema.runs.c.tenant == p["tenant"],
                    schema.runs.c.state == "RUNNING",
                )
                .values(state="CANCELLED")
            )
        return {"cancelled": bool(changed.rowcount), "in_flight_calls_may_complete": True}
