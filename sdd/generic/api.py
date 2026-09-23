"""Catalog-driven API. Every route uses the parent application's token identity."""

import os
from typing import Literal
from sqlglot.errors import SqlglotError
from fastapi import Depends, HTTPException, Query
from pydantic import Field, StrictBool, StrictStr
from sqlalchemy.exc import DBAPIError
from ..ir import Strict
from ..evaluators import ProviderError
from .catalog import Catalog, serial
from .jev import Decisions
from .planner import Planner, PlanReviewRequired
from .sql import SQLService
from . import schema as s
from .planning_review import PlanReviews
from .history import QueryHistory


class ColumnInput(Strict):
    name: str = Field(min_length=1, max_length=63)
    type: Literal["text", "integer", "number", "boolean", "date", "datetime", "json"]
    nullable: bool = True
    description: str = Field(default="", max_length=2000)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    unit: str = Field(default="", max_length=80)


class DatasetInput(Strict):
    name: str = Field(min_length=1, max_length=120)
    rows: list[dict] = Field(default_factory=list, max_length=10000)
    columns: list[ColumnInput] | None = None
    primary_key: list[str] | None = None
    description: str = Field(default="", max_length=4000)
    writable: bool = True


class LinkInput(Strict):
    source: str
    target: str
    source_column: str
    target_column: str


class QueryInput(Strict):
    parent_history_id: str | None = Field(default=None, max_length=64)
    sql: str = Field(min_length=1, max_length=30000)
    max_evaluations: int = Field(default=100, ge=0, le=1000)
    accept: float = Field(default=0.8, ge=0, le=1)
    reject: float = Field(default=0.2, ge=0, le=1)
    allow_all: bool = False
    max_affected: int = Field(default=1000, ge=1, le=1000)


class QuestionInput(Strict):
    planner_mode: Literal["jev", "hybrid"] = "jev"
    parent_history_id: str | None = Field(default=None, max_length=64)
    question: str = Field(min_length=1, max_length=4000)
    knowledge: list[dict] = Field(default_factory=list, max_length=256)
    dataset_ids: list[str] | None = None
    execute: bool = True
    max_evaluations: int = Field(default=100, ge=0, le=1000)


class PlanCorrectionInput(Strict):
    parent_history_id: str | None = Field(default=None, max_length=64)
    review_id: str = Field(min_length=1, max_length=64)
    corrections: dict[str, StrictStr | StrictBool] = Field(default_factory=dict, max_length=128)


class PlanConfirmationInput(Strict):
    parent_history_id: str | None = Field(default=None, max_length=64)
    review_id: str = Field(min_length=1, max_length=64)
    max_evaluations: int = Field(default=100, ge=0, le=1000)


def services(executor):
    backend = executor.workers.backends.get("jev")
    decisions = (
        Decisions(executor.db, backend, os.getenv("SDD_JEV_MODEL", "jev-1.13.0"))
        if backend
        else None
    )
    return (
        Catalog(executor.db),
        SQLService(executor.db, decisions),
        Planner(executor.db, decisions, strategy=os.getenv("SDD_PLANNER", "staged"))
        if decisions
        else None,
    )


def mount(app, executor, identity, reviewer):
    catalog, sql, planner = services(executor)
    reviews = PlanReviews(executor.db, planner) if planner else None
    history = QueryHistory(executor.db)
    from .feature_api import mount as mount_features

    mount_features(app, executor, sql.decisions, identity, reviewer)
    app.state.feature_decisions = sql.decisions
    from ..operators.api import mount as mount_operators

    mount_operators(app, executor.db, sql.decisions, identity, reviewer)
    from .extraction_api import mount as mount_extraction

    mount_extraction(app, executor.db, sql.decisions, reviewer)

    @app.exception_handler(PlanReviewRequired)
    async def needs_review(_, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=422,
            content={
                "detail": str(exc),
                "review_required": True,
                "history_id": getattr(exc, "history_id", None),
                "executed": False,
                "logical_sql": exc.plan.get("logical_sql", ""),
                "review_id": exc.plan.get("review_id"),
                "review": exc.plan.get("review"),
                "plan": serial(exc.plan),
            },
        )

    @app.exception_handler(SqlglotError)
    async def sql_error(_, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=400,
            content={
                "detail": "SQL could not be parsed or resolved against the selected catalog. Check column names, aliases, and syntax."
            },
        )

    @app.exception_handler(ProviderError)
    async def provider_error(_, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=503,
            content={
                "detail": "Planning provider request failed: " + exc.code,
                "retryable": exc.retryable,
            },
        )

    @app.exception_handler(DBAPIError)
    async def database_error(_, exc):
        from fastapi.responses import JSONResponse

        # DBAPI exceptions can contain SQL parameters. Return only a stable class/code.
        code = getattr(exc.orig, "sqlstate", None)
        return JSONResponse(
            status_code=400,
            content={
                "detail": "Database rejected this query or data constraint. Check types, grouping, required values and relationships.",
                "code": code,
            },
        )

    @app.get("/datasets", tags=["Generic datasets"])
    def datasets(p=Depends(identity)):
        return {"datasets": serial(catalog.list(p["tenant"]))}

    @app.post("/datasets", tags=["Generic datasets"])
    def create(body: DatasetInput, p=Depends(reviewer)):
        return serial(catalog.create(p["tenant"], **body.model_dump()))

    @app.post("/datasets/relationships", tags=["Generic datasets"])
    def link(body: LinkInput, p=Depends(reviewer)):
        return catalog.link(p["tenant"], **body.model_dump())

    @app.get("/datasets/{dataset_id}/rows", tags=["Generic datasets"])
    def rows(dataset_id: str, p=Depends(identity)):
        dataset = catalog.get(p["tenant"], dataset_id)
        from sqlglot import exp

        query = (
            exp.select("*")
            .from_(exp.Table(this=exp.to_identifier(dataset["name"], quoted=True)))
            .limit(100)
        )
        return sql.execute(p["tenant"], query.sql(dialect="postgres"))

    def execute_plan(plan, principal, budget):
        try:
            return sql.execute(
                principal["tenant"],
                plan["logical_sql"],
                request=plan["request"],
                plan=plan,
                actor=principal["name"],
                max_evaluations=budget,
            )
        except Exception as exc:
            exc.query_plan = plan
            raise

    @app.post(
        "/ask",
        tags=["Generic query"],
        summary="Plan and query any selected datasets in English or Chinese",
    )
    def ask(body: QuestionInput, p=Depends(identity)):
        def execute():
            if planner is None:
                raise HTTPException(503, "Configure TYPESAFE_API_KEY to enable Jev planning")
            selected_reviews = (
                PlanReviews(
                    executor.db, Planner(executor.db, planner.decisions, "hybrid", planner.llm)
                )
                if body.planner_mode == "hybrid"
                else reviews
            )
            plan = selected_reviews.begin(
                p["tenant"], p["name"], body.question, body.dataset_ids, knowledge=body.knowledge
            )
            if plan["operation"] != "select" and p.get("role") != "reviewer":
                raise HTTPException(403, "Reviewer role required for mutation previews")
            if not body.execute:
                return {"plan": plan, "logical_sql": plan["logical_sql"], "executed": False}
            return execute_plan(plan, p, body.max_evaluations)

        return history.capture(
            p["tenant"],
            p["name"],
            {
                "mode": "natural",
                "text": body.question,
                "planner_mode": body.planner_mode,
                "dataset_ids": body.dataset_ids,
                "max_evaluations": body.max_evaluations,
            },
            execute,
            parent_id=body.parent_history_id,
        )

    @app.post("/ask/review", tags=["Generic query"])
    def correct_plan(body: PlanCorrectionInput, p=Depends(identity)):
        if reviews is None:
            raise HTTPException(503, "Configure TYPESAFE_API_KEY to enable Jev planning")
        original = reviews.load(p["tenant"], p["name"], body.review_id)
        parent = (
            history.get(p["tenant"], p["name"], body.parent_history_id)
            if body.parent_history_id
            else None
        )

        def execute():
            plan = reviews.resume(p["tenant"], p["name"], body.review_id, body.corrections)
            if plan["operation"] != "select" and p.get("role") != "reviewer":
                raise HTTPException(403, "Reviewer role required for mutation previews")
            return {
                "plan": plan,
                "review_id": plan["review_id"],
                "review": plan["review"],
                "logical_sql": plan["logical_sql"],
                "executed": False,
            }

        return history.capture(
            p["tenant"],
            p["name"],
            {
                "mode": "natural",
                "text": original["request"],
                "planner_mode": "hybrid"
                if original.get("_planner_strategy") == "hybrid"
                else "jev",
                "dataset_ids": original.get("_selected_dataset_ids", original["dataset_ids"]),
                "max_evaluations": parent["input"].get("max_evaluations", 100) if parent else 100,
            },
            execute,
            parent_id=body.parent_history_id,
        )

    @app.post("/ask/confirm", tags=["Generic query"])
    def confirm_plan(body: PlanConfirmationInput, p=Depends(identity)):
        if reviews is None:
            raise HTTPException(503, "Configure TYPESAFE_API_KEY to enable Jev planning")
        plan = reviews.confirm(p["tenant"], p["name"], body.review_id)

        def execute():
            _, _, _, target = sql.prepare(p["tenant"], plan["logical_sql"])
            if target and p.get("role") != "reviewer":
                raise HTTPException(403, "Reviewer role required for mutation previews")
            return execute_plan(plan, p, body.max_evaluations)

        return history.capture(
            p["tenant"],
            p["name"],
            {
                "mode": "natural",
                "text": plan["request"],
                "planner_mode": "hybrid" if plan.get("hybrid") else "jev",
                "dataset_ids": plan["dataset_ids"],
                "max_evaluations": body.max_evaluations,
            },
            execute,
            parent_id=body.parent_history_id,
            continue_review=body.review_id,
        )

    @app.post(
        "/data/sql", tags=["Generic query"], summary="Execute guarded SQL or preview a mutation"
    )
    def run(body: QueryInput, p=Depends(identity)):
        def execute():
            _, _, _, target = sql.prepare(p["tenant"], body.sql)
            if target and p.get("role") != "reviewer":
                raise HTTPException(403, "Reviewer role required for mutation previews")
            return sql.execute(
                p["tenant"], actor=p["name"], **body.model_dump(exclude={"parent_history_id"})
            )

        return history.capture(
            p["tenant"],
            p["name"],
            {
                "mode": "sql",
                "text": body.sql,
                "max_evaluations": body.max_evaluations,
            },
            execute,
            parent_id=body.parent_history_id,
        )

    @app.post("/data/mutations/{token}/commit", tags=["Generic query"])
    def commit(token: str, p=Depends(reviewer)):
        result = sql.commit(p["tenant"], token, p["name"])
        return history.committed(p["tenant"], p["name"], token, result)

    @app.get("/query-history", tags=["Generic query"])
    def recent_queries(
        limit: int = Query(default=20, ge=1, le=50),
        before: str | None = Query(default=None, max_length=64),
        p=Depends(identity),
    ):
        return history.recent(p["tenant"], p["name"], limit, before)

    @app.get("/query-history/{history_id}", tags=["Generic query"])
    def query_detail(history_id: str, p=Depends(identity)):
        return history.detail(p["tenant"], p["name"], history_id)

    @app.post("/query-history/{history_id}/review", tags=["Generic query"])
    def reopen_query(history_id: str, p=Depends(identity)):
        record = history.get(p["tenant"], p["name"], history_id)
        if record["status"] == "redacted" or not record["review_id"]:
            raise ValueError("This history entry has no available planning decisions")
        if reviews is None:
            raise HTTPException(503, "Configure TYPESAFE_API_KEY to enable Jev planning")
        plan = reviews.reopen(p["tenant"], p["name"], record["review_id"])
        return {
            "plan": plan,
            "logical_sql": plan["logical_sql"],
            "executed": False,
            "review_required": bool(plan["review"]["reason"]),
            "history_id": history_id,
        }

    @app.get("/data/runs/{run_id}", tags=["Generic query"])
    def run_detail(run_id: str, p=Depends(identity)):
        return catalog.ledger.get(p["tenant"], s.runs, run_id)

    @app.get("/data/evidence/{observation_id}", tags=["Generic query"])
    def evidence(observation_id: str, p=Depends(identity)):
        from ..ledger import digest

        observation = catalog.ledger.get(p["tenant"], s.evidence, observation_id)
        with executor.db.transaction(p["tenant"]) as connection:
            from sqlalchemy import select

            payload = (
                connection.execute(
                    select(s.payloads).where(
                        s.payloads.c.tenant == p["tenant"],
                        s.payloads.c.evidence_id == observation_id,
                    )
                )
                .mappings()
                .first()
            )
        if payload:
            return {
                "observation": observation,
                "answer": dict(payload),
                "source_version": catalog.ledger.get(
                    p["tenant"], s.row_versions, payload["source_version_id"]
                ),
            }
        version_id = digest(
            [
                p["tenant"],
                observation["dataset_id"],
                observation["row_key"],
                observation["row_hash"],
            ]
        )
        try:
            source = catalog.ledger.get(p["tenant"], s.row_versions, version_id)
        except ValueError:
            source = None
        return {"observation": observation, "source_version": source}
