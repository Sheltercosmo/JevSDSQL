"""Reviewable semantic features shared by SQL, natural language and maintenance."""

from typing import Any, Literal
from fastapi import Depends, HTTPException
from pydantic import Field
from sqlalchemy import update

from ..ir import Strict
from . import schema
from .catalog import serial
from .features import FeatureRegistry


class FeatureInput(Strict):
    dataset_id: str
    name: str = Field(min_length=1, max_length=120)
    column: str
    definition: str = Field(min_length=1, max_length=4000)
    kind: Literal["noul", "choice", "score", "extract"] = "noul"
    criteria: dict[str, str] | list[str] | None = None
    context_columns: list[str] | None = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    confidence: float = Field(default=0.55, ge=0, le=1)
    maintain: bool = False


class FeatureReviewInput(Strict):
    status: Literal["active", "deprecated"]
    reason: str = Field(min_length=1, max_length=2000)
    examples: list[dict] = Field(default_factory=list, max_length=100)


class FeatureAssertionInput(Strict):
    primary_key: dict
    value: Any
    reason: str = Field(min_length=1, max_length=2000)


class FeatureBudget(Strict):
    max_evaluations: int = Field(default=100, ge=0, le=1000)


class RefreshInput(FeatureBudget):
    dataset_id: str


def mount(app, executor, decisions, identity, reviewer):
    registry = FeatureRegistry(executor.db)

    def configured():
        if decisions is None:
            raise HTTPException(503, "Configure TYPESAFE_API_KEY to evaluate features")

    @app.get("/features", tags=["Semantic features"])
    def features(dataset_id: str | None = None, p=Depends(identity)):
        return {"features": serial(registry.list(p["tenant"], dataset_id))}

    @app.post("/features", tags=["Semantic features"])
    def create(body: FeatureInput, p=Depends(reviewer)):
        return serial(registry.create(p["tenant"], p["name"], **body.model_dump()))

    @app.post("/features/refresh", tags=["Semantic features"])
    def refresh(body: RefreshInput, p=Depends(reviewer)):
        configured()
        return serial(
            registry.refresh(p["tenant"], body.dataset_id, decisions, body.max_evaluations)
        )

    @app.post("/features/{feature_id}/preview", tags=["Semantic features"])
    def preview(feature_id: str, body: FeatureBudget, p=Depends(identity)):
        configured()
        return serial(registry.preview(p["tenant"], feature_id, decisions, body.max_evaluations))

    @app.post("/features/{feature_id}/review", tags=["Semantic features"])
    def review(feature_id: str, body: FeatureReviewInput, p=Depends(reviewer)):
        return serial(registry.review(p["tenant"], feature_id, p["name"], **body.model_dump()))

    @app.post("/features/{feature_id}/assertions", tags=["Semantic features"])
    def assertion(feature_id: str, body: FeatureAssertionInput, p=Depends(reviewer)):
        return serial(
            registry.assert_value(p["tenant"], feature_id, actor=p["name"], **body.model_dump())
        )

    @app.get("/feature-jobs", tags=["Semantic features"])
    def jobs(p=Depends(identity)):
        return {"jobs": registry.catalog.ledger.list(p["tenant"], schema.maintenance_jobs)}

    @app.post("/feature-jobs/{job_id}/cancel", tags=["Semantic features"])
    def cancel(job_id: str, p=Depends(reviewer)):
        registry.catalog.ledger.get(p["tenant"], schema.maintenance_jobs, job_id)
        with executor.db.transaction(p["tenant"]) as connection:
            changed = connection.execute(
                update(schema.maintenance_jobs)
                .where(
                    schema.maintenance_jobs.c.tenant == p["tenant"],
                    schema.maintenance_jobs.c.id == job_id,
                    schema.maintenance_jobs.c.state.in_(["pending", "running"]),
                )
                .values(state="cancelled", lease_token=None)
            )
        return {"cancelled": bool(changed.rowcount)}
