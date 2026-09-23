"""Authenticated local service; interactive query and review API at /docs."""

import json
import os
import secrets
import asyncio
from contextlib import asynccontextmanager
from threading import Event, Thread
from fastapi import FastAPI, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import Field
from .ir import Strict, Plan
from .config import runtime
from . import schema as s
from .planner import preview
from .natural import ask
from pathlib import Path
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from .maintenance import create_policy, refresh


class SourceInput(Strict):
    external_id: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=100000)
    customer_id: str = Field(min_length=1, max_length=200)
    segment: str
    product: str
    event_time: str
    context: dict = Field(default_factory=dict)
    source_system: str = "support"


class ConceptInput(Strict):
    definition: str = Field(min_length=1, max_length=10000)
    concept_key: str | None = None
    inclusion: str = ""
    exclusion: str = ""
    context_fields: list[str] = Field(default_factory=list)


class ReviewInput(Strict):
    version_id: str
    concept_id: str
    decision: str
    reason: str = Field(min_length=1)


class PromotionInput(Strict):
    target: str
    reason: str = Field(min_length=1)
    examples: list[dict] | None = None
    quality: dict | None = None


class PreviewInput(Strict):
    request: str
    evaluator_id: str
    policy_id: str


class AskInput(Strict):
    question: str = Field(min_length=1, max_length=4000)
    execute: bool = True
    evaluator_id: str | None = None
    policy_id: str | None = None
    max_evaluations: int = Field(default=100, ge=0, le=10000)
    wait_seconds: float = Field(default=30, ge=0, le=300)


class EvaluatorInput(Strict):
    model: str


class PolicyInput(Strict):
    accept: float = 0.8
    reject: float = 0.2
    calibration: dict | None = None


class MaterializationInput(Strict):
    concept_id: str
    evaluator_id: str
    policy_id: str
    population: dict = Field(default_factory=dict)
    budget: int = Field(default=100, ge=0, le=10000)
    freshness_seconds: int = Field(default=3600, ge=1)
    required_coverage: float = Field(default=1, ge=0, le=1)


def create_app(executor=None, tokens=None):
    if executor is None:
        _, executor = runtime()
    ledger = executor.ledger
    tokens = tokens if tokens is not None else json.loads(os.getenv("SDD_API_TOKENS", "{}"))

    @asynccontextmanager
    async def lifespan(app):
        stop = Event()
        worker = None
        decisions = getattr(app.state, "feature_decisions", None)
        if decisions is not None and os.getenv("SDD_FEATURE_MAINTENANCE", "1") == "1":
            from .generic.features import FeatureRegistry
            from .operators.maintenance import work_one as maintain_operators

            registry = FeatureRegistry(executor.db)
            tenants = sorted({principal["tenant"] for principal in tokens.values()})

            def maintain():
                while not stop.is_set():
                    for tenant in tenants:
                        if stop.is_set():
                            break
                        try:
                            registry.work_one(tenant, decisions, stop_event=stop)
                            if not stop.is_set():
                                maintain_operators(executor.db, decisions, tenant)
                        except Exception:
                            # Durable leases recover interrupted work on the next pass.
                            pass
                    stop.wait(1)

            worker = Thread(target=maintain, daemon=True, name="sdd-feature-maintenance")
            worker.start()
        try:
            yield
        finally:
            stop.set()
            if worker:
                await asyncio.to_thread(worker.join, 3)

    app = FastAPI(
        title="SDD · Semantic evidence database",
        version="0.4.0",
        lifespan=lifespan,
        description="Query, inspect evidence, review decisions, and govern reusable concepts. Tenant is bound to the bearer token.",
    )
    web = Path(__file__).parent / "web"
    app.mount("/ask-assets", StaticFiles(directory=web), name="ask-assets")

    @app.get("/ask", include_in_schema=False)
    @app.get("/ask/en", include_in_schema=False)
    @app.get("/ask/zh", include_in_schema=False)
    def ask_page():
        return FileResponse(
            web / "explore.html",
            headers={
                "Content-Security-Policy": "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'",
                "Referrer-Policy": "no-referrer",
            },
        )

    auth = HTTPBearer()

    def identity(credentials: HTTPAuthorizationCredentials = Depends(auth)):
        for token, principal in tokens.items():
            if secrets.compare_digest(token, credentials.credentials):
                return principal
        raise HTTPException(401, "Invalid bearer token")

    def reviewer(p=Depends(identity)):
        if p.get("role") != "reviewer":
            raise HTTPException(403, "Reviewer role required")
        return p

    @app.exception_handler(ValueError)
    async def invalid(_, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=400, content={"detail": str(exc)})

    from .generic.api import mount

    mount(app, executor, identity, reviewer)

    @app.get("/health")
    def health():
        return {"status": "ok", "version": "0.4.0"}

    @app.get("/catalog")
    def catalog(p=Depends(identity)):
        return {
            "subject": "customer_message",
            "entity_grain": ["message", "customer"],
            "relationship": "customer has many messages; customer counts use DISTINCT",
            "fields": ["segment", "product", "customer_id"],
            "time_semantics": "segment/product at message ingestion",
            "concepts": ledger.list(p["tenant"], s.concepts),
            "evaluators": ledger.list(p["tenant"], s.evaluators),
            "policies": ledger.list(p["tenant"], s.policies),
        }

    @app.post("/sources")
    def ingest(body: SourceInput, p=Depends(reviewer)):
        return ledger.ingest(p["tenant"], **body.model_dump())

    @app.delete("/sources/{record_id}")
    def erase(record_id: str, p=Depends(reviewer)):
        ledger.delete_record(p["tenant"], record_id)
        return {"deleted": record_id}

    @app.post("/concepts")
    def concept(body: ConceptInput, p=Depends(identity)):
        return ledger.concept(p["tenant"], owner=p["name"], **body.model_dump())

    @app.post("/concepts/{concept_id}/promote")
    def promote(concept_id: str, body: PromotionInput, p=Depends(reviewer)):
        return ledger.promote(p["tenant"], concept_id, actor=p["name"], **body.model_dump())

    @app.post("/evaluators")
    def evaluator(body: EvaluatorInput, p=Depends(reviewer)):
        return ledger.evaluator(p["tenant"], "jev", body.model)

    @app.post("/policies")
    def policy(body: PolicyInput, p=Depends(reviewer)):
        return ledger.policy(p["tenant"], **body.model_dump())

    @app.post("/reviews")
    def review(body: ReviewInput, p=Depends(reviewer)):
        return ledger.review(p["tenant"], reviewer=p["name"], **body.model_dump())

    @app.post("/legacy/ask", summary="Legacy support-message demo query")
    def natural_query(body: AskInput, p=Depends(identity)):
        return ask(executor, p["tenant"], p["name"], **body.model_dump())

    @app.post("/query/preview")
    def plan_preview(body: PreviewInput, p=Depends(identity)):
        return preview(
            body.request, ledger, p["tenant"], p["name"], body.evaluator_id, body.policy_id
        )

    @app.post("/query/explain")
    def explain(body: Plan, p=Depends(identity)):
        for cid in body.predicate.concepts():
            ledger.get(p["tenant"], s.concepts, cid)
        ledger.get(p["tenant"], s.evaluators, body.evaluator_id)
        ledger.get(p["tenant"], s.policies, body.policy_id)
        return {
            "plan": body.model_dump(),
            "operators": [
                "PinSourceVersions",
                "EnsureObservations",
                "ResolvePolicy",
                "SemanticFilter",
                "SQLAggregate",
            ],
            "grain": body.grain,
            "semantics": "Customer membership is EXISTS a matching message; NOT applies at message scope.",
        }

    @app.post("/query")
    def query(body: Plan, inline: bool = True, p=Depends(identity)):
        return executor.execute(p["tenant"], body, inline=inline)

    @app.get("/runs/{run_id}")
    def run(run_id: str, p=Depends(identity)):
        return ledger.get(p["tenant"], s.runs, run_id)

    @app.get("/evidence")
    def evidence(p=Depends(identity)):
        return {
            "observations": ledger.list(p["tenant"], s.observations),
            "reviews": ledger.list(p["tenant"], s.assertions),
            "jobs": ledger.list(p["tenant"], s.jobs),
            "attempts": ledger.list(p["tenant"], s.attempts),
        }

    @app.post("/jobs/{job_id}/cancel")
    def cancel(job_id: str, p=Depends(reviewer)):
        executor.workers.cancel(p["tenant"], job_id)
        return {"cancelled": job_id}

    @app.get("/materializations")
    def maintained_features(p=Depends(identity)):
        return ledger.list(p["tenant"], s.materializations)

    @app.get("/usage")
    def usage(p=Depends(identity)):
        return ledger.list(p["tenant"], s.usage_budgets)

    @app.post("/materializations")
    def materialize(body: MaterializationInput, p=Depends(reviewer)):
        return create_policy(ledger, p["tenant"], owner=p["name"], **body.model_dump())

    @app.post("/materializations/{policy_id}/refresh")
    def materialize_refresh(policy_id: str, p=Depends(reviewer)):
        return refresh(executor, p["tenant"], policy_id)

    return app
