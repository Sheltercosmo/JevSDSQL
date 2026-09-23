from sqlalchemy import (
    Index,
    Column,
    String,
    Text,
    JSON,
    Integer,
    Float,
    UniqueConstraint,
    ForeignKey,
)
from ..schema import table

datasets = table(
    "dataset_catalog",
    Column("name", String(160), nullable=False),
    Column("description", Text, nullable=False),
    Column("schema_name", String(100)),
    Column("table_name", String(160), nullable=False),
    Column("columns", JSON, nullable=False),
    Column("primary_key", JSON, nullable=False),
    Column("writable", Integer, nullable=False),
    Column("links", JSON, nullable=False),
    Column("created_at", String(32), nullable=False),
    UniqueConstraint("tenant", "name"),
)
evidence = table(
    "dataset_evidence",
    Column("dataset_id", ForeignKey("dataset_catalog.id", ondelete="CASCADE"), nullable=False),
    Column("row_key", String(64), nullable=False),
    Column("row_hash", String(64), nullable=False),
    Column("definition", Text, nullable=False),
    Column("evaluator", String(100), nullable=False),
    Column("state", String(24), nullable=False),
    Column("probability", Float),
    Column("responder", String(100)),
    Column("usage", JSON, nullable=False),
    Column("lease_token", String(64)),
    Column("lease_until", Float, nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("error", String(100)),
    Column("created_at", String(32), nullable=False),
)
attempts = table(
    "dataset_evaluation_attempts",
    Column("evidence_id", ForeignKey("dataset_evidence.id", ondelete="CASCADE"), nullable=False),
    Column("state", String(24), nullable=False),
    Column("error", String(100)),
    Column("usage", JSON, nullable=False),
    Column("created_at", String(32), nullable=False),
)
runs = table(
    "dataset_query_runs",
    Column("request", Text, nullable=False),
    Column("logical_sql", Text, nullable=False),
    Column("compiled_sql", Text, nullable=False),
    Column("parameters", JSON, nullable=False),
    Column("plan", JSON, nullable=False),
    Column("manifest", JSON, nullable=False),
    Column("result", JSON, nullable=False),
    Column("created_at", String(32), nullable=False),
)
query_history = table(
    "dataset_query_history",
    Column("actor", String(100), nullable=False),
    Column("input", JSON, nullable=False),
    Column("dataset_ids", JSON, nullable=False),
    Column("parent_id", String(64)),
    Column("status", String(24), nullable=False),
    Column("logical_sql", Text, nullable=False),
    Column("run_id", String(64)),
    Column("review_id", String(64)),
    Column("preview_id", String(64)),
    Column("error", String(100)),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
)
Index(
    "ix_query_history_recent",
    query_history.c.tenant,
    query_history.c.actor,
    query_history.c.created_at,
    query_history.c.id,
)


previews = table(
    "dataset_mutation_previews",
    Column("logical_sql", Text, nullable=False),
    Column("dataset_ids", JSON, nullable=False),
    Column("snapshot_hash", String(64), nullable=False),
    Column("affected_rows", Integer, nullable=False),
    Column("options", JSON, nullable=False),
    Column("expires_at", Float, nullable=False),
    Column("state", String(24), nullable=False),
    Column("actor", String(100), nullable=False),
    Column("created_at", String(32), nullable=False),
)

row_versions = table(
    "dataset_row_versions",
    Column("dataset_id", ForeignKey("dataset_catalog.id", ondelete="CASCADE"), nullable=False),
    Column("row_key", String(64), nullable=False),
    Column("row_hash", String(64), nullable=False),
    Column("value", JSON, nullable=False),
    Column("created_at", String(32), nullable=False),
)


features = table(
    "dataset_feature_revisions",
    Column("dataset_id", ForeignKey("dataset_catalog.id", ondelete="CASCADE"), nullable=False),
    Column("name", String(120), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("definition", JSON, nullable=False),
    Column("status", String(24), nullable=False),
    Column("owner", String(120), nullable=False),
    Column("review", JSON, nullable=False),
    Column("maintain", Integer, nullable=False),
    Column("materialization", JSON, nullable=False),
    Column("created_at", String(32), nullable=False),
    UniqueConstraint("tenant", "dataset_id", "name", "revision"),
)

payloads = table(
    "dataset_semantic_answers",
    Column(
        "evidence_id",
        ForeignKey("dataset_evidence.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    ),
    Column(
        "source_version_id",
        ForeignKey("dataset_row_versions.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("feature_id", ForeignKey("dataset_feature_revisions.id", ondelete="CASCADE")),
    Column("answer", JSON, nullable=False),
    Column("span", JSON),
    Column("created_at", String(32), nullable=False),
)

inference_calls = table(
    "dataset_inference_calls",
    Column("dataset_id", ForeignKey("dataset_catalog.id", ondelete="CASCADE"), nullable=False),
    Column("row_key", String(64), nullable=False),
    Column("model", String(100), nullable=False),
    Column("questions", Integer, nullable=False),
    Column("state", String(24), nullable=False),
    Column("usage", JSON, nullable=False),
    Column("elapsed_ms", Float, nullable=False),
    Column("error", String(100)),
    Column("created_at", String(32), nullable=False),
)

feature_reviews = table(
    "dataset_feature_assertions",
    Column(
        "feature_id", ForeignKey("dataset_feature_revisions.id", ondelete="CASCADE"), nullable=False
    ),
    Column("row_key", String(64), nullable=False),
    Column("dependency_hash", String(64), nullable=False),
    Column(
        "source_version_id",
        ForeignKey("dataset_row_versions.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("value", JSON, nullable=False),
    Column("actor", String(120), nullable=False),
    Column("reason", Text, nullable=False),
    Column("created_at", String(32), nullable=False),
)

maintenance_jobs = table(
    "dataset_feature_jobs",
    Column("dataset_id", ForeignKey("dataset_catalog.id", ondelete="CASCADE"), nullable=False),
    Column("state", String(24), nullable=False),
    Column("lease_token", String(64)),
    Column("lease_until", Float, nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("available_at", Float, nullable=False),
    Column("run_id", String(64)),
    Column("error", String(100)),
    Column("created_at", String(32), nullable=False),
)

Index("ix_feature_lookup", features.c.tenant, features.c.dataset_id, features.c.status)
Index(
    "ix_feature_review_dependencies",
    feature_reviews.c.tenant,
    feature_reviews.c.feature_id,
    feature_reviews.c.row_key,
    feature_reviews.c.dependency_hash,
)
Index(
    "ix_feature_job_ready",
    maintenance_jobs.c.tenant,
    maintenance_jobs.c.state,
    maintenance_jobs.c.available_at,
)
