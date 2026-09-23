"""Portable Core schema; PostgreSQL is the deployment database."""

from sqlalchemy import (
    MetaData,
    Table,
    Column,
    String,
    Text,
    Integer,
    Float,
    JSON,
    ForeignKey,
    UniqueConstraint,
    CheckConstraint,
    Index,
)

metadata = MetaData()


def table(name, *columns, **kw):
    return Table(
        name,
        metadata,
        Column("id", String(64), primary_key=True),
        Column("tenant", String(100), nullable=False, index=True),
        *columns,
        **kw,
    )


records = table(
    "source_records",
    Column("external_id", String(200), nullable=False),
    Column("source_system", String(100), nullable=False),
    Column("current_version", String(64)),
    UniqueConstraint("tenant", "source_system", "external_id"),
)
versions = table(
    "source_versions",
    Column("record_id", ForeignKey("source_records.id", ondelete="CASCADE"), nullable=False),
    Column("text", Text, nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("context", JSON, nullable=False),
    Column("context_hash", String(64), nullable=False),
    Column("customer_id", String(200), nullable=False),
    Column("segment", String(100), nullable=False),
    Column("product", String(200), nullable=False),
    Column("event_time", String(32), nullable=False),
    Column("ingested_at", String(32), nullable=False),
)
concepts = table(
    "concept_revisions",
    Column("concept_key", String(100), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("definition", Text, nullable=False),
    Column("subject_type", String(40), nullable=False),
    Column("output_type", String(30), nullable=False),
    Column("inclusion", Text, nullable=False),
    Column("exclusion", Text, nullable=False),
    Column("context_fields", JSON, nullable=False),
    Column("status", String(30), nullable=False),
    Column("owner", String(100), nullable=False),
    Column("review", JSON, nullable=False),
    UniqueConstraint("tenant", "concept_key", "revision"),
    CheckConstraint("status IN ('provisional','validated','active','deprecated')"),
)
evaluators = table(
    "evaluator_revisions",
    Column("provider", String(40), nullable=False),
    Column("model", String(100), nullable=False),
    Column("instructions", Text, nullable=False),
    Column("state_builder", String(40), nullable=False),
    Column("preprocessing", String(40), nullable=False),
    Column("chunking", String(40), nullable=False),
)
policies = table(
    "decision_policy_revisions",
    Column("accept", Float, nullable=False),
    Column("reject", Float, nullable=False),
    Column("calibration", JSON, nullable=False),
    CheckConstraint("reject >= 0 AND accept <= 1 AND reject < accept"),
)
jobs = table(
    "evaluation_jobs",
    Column("version_id", ForeignKey("source_versions.id", ondelete="CASCADE"), nullable=False),
    Column("concept_id", ForeignKey("concept_revisions.id"), nullable=False),
    Column("evaluator_id", ForeignKey("evaluator_revisions.id"), nullable=False),
    Column("context_hash", String(64), nullable=False),
    Column("state", String(20), nullable=False),
    Column("lease_token", String(64)),
    Column("lease_until", Float, nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("available_at", Float, nullable=False),
    Column("max_attempts", Integer, nullable=False),
    Column("error", Text),
    UniqueConstraint("tenant", "version_id", "concept_id", "evaluator_id", "context_hash"),
)
observations = table(
    "observations",
    Column(
        "job_id", ForeignKey("evaluation_jobs.id", ondelete="CASCADE"), nullable=False, unique=True
    ),
    Column("version_id", ForeignKey("source_versions.id", ondelete="CASCADE"), nullable=False),
    Column("concept_id", ForeignKey("concept_revisions.id"), nullable=False),
    Column("evaluator_id", ForeignKey("evaluator_revisions.id"), nullable=False),
    Column("context_hash", String(64), nullable=False),
    Column("probability", Float, nullable=False),
    Column("distribution", JSON, nullable=False),
    Column("responder", String(100), nullable=False),
    Column("evidence", JSON, nullable=False),
    Column("usage", JSON, nullable=False),
    Column("created_at", String(32), nullable=False),
    CheckConstraint("probability >= 0 AND probability <= 1"),
)
attempts = table(
    "evaluation_attempts",
    Column("job_id", ForeignKey("evaluation_jobs.id", ondelete="CASCADE"), nullable=False),
    Column("lease_token", String(64), nullable=False),
    Column("status", String(30), nullable=False),
    Column("error", Text),
    Column("started_at", Float, nullable=False),
    Column("finished_at", Float),
    Column("usage", JSON, nullable=False),
)
assertions = table(
    "human_assertions",
    Column("version_id", ForeignKey("source_versions.id", ondelete="CASCADE"), nullable=False),
    Column("concept_id", ForeignKey("concept_revisions.id"), nullable=False),
    Column("decision", String(10), nullable=False),
    Column("reviewer", String(100), nullable=False),
    Column("reason", Text, nullable=False),
    Column("created_at", String(32), nullable=False),
    Column("supersedes", String(64)),
    CheckConstraint("decision IN ('true','false','unknown')"),
)
runs = table(
    "query_runs",
    Column("plan", JSON, nullable=False),
    Column("snapshot", JSON, nullable=False),
    Column("manifest", JSON, nullable=False),
    Column("result", JSON, nullable=False),
    Column("created_at", String(32), nullable=False),
)
materializations = table(
    "materialization_policies",
    Column("concept_id", ForeignKey("concept_revisions.id"), nullable=False),
    Column("evaluator_id", ForeignKey("evaluator_revisions.id"), nullable=False),
    Column("policy_id", ForeignKey("decision_policy_revisions.id"), nullable=False),
    Column("population", JSON, nullable=False),
    Column("budget", Integer, nullable=False),
    Column("freshness_seconds", Integer, nullable=False),
    Column("required_coverage", Float, nullable=False),
    Column("owner", String(100), nullable=False),
    Column("last_run", String(64)),
)
audit = table(
    "governance_events",
    Column("concept_id", String(64), nullable=False),
    Column("actor", String(100), nullable=False),
    Column("action", String(30), nullable=False),
    Column("details", JSON, nullable=False),
    Column("created_at", String(32), nullable=False),
)
Index("ix_jobs_ready", jobs.c.tenant, jobs.c.state, jobs.c.available_at)
Index(
    "ix_observation_reuse",
    observations.c.tenant,
    observations.c.version_id,
    observations.c.concept_id,
    observations.c.evaluator_id,
    observations.c.context_hash,
)

usage_budgets = table(
    "tenant_daily_usage",
    Column("day", String(10), nullable=False),
    Column("calls", Integer, nullable=False),
    UniqueConstraint("tenant", "day"),
)
