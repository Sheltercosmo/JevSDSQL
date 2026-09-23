from sqlalchemy import JSON, Column, Float, ForeignKey, Index, Integer, String, Text

from ..schema import table

observations = table(
    "jev_operator_observations",
    Column("cache_key", String(64), nullable=False),
    Column("model", String(100), nullable=False),
    Column("subject_id", String(200), nullable=False),
    Column("source_revisions", JSON, nullable=False),
    Column("question", JSON, nullable=False),
    Column("context_hash", String(64), nullable=False),
    Column("hypothesis", JSON, nullable=False),
    Column("answer", JSON, nullable=False),
    Column("created_at", String(40), nullable=False),
)
invalidations = table(
    "jev_operator_invalidations",
    Column(
        "observation_id",
        ForeignKey("jev_operator_observations.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("reason", Text, nullable=False),
    Column("created_at", String(40), nullable=False),
)
runs = table(
    "jev_operator_runs",
    Column("actor", String(100), nullable=False),
    Column("operator", String(80), nullable=False),
    Column("plan_hash", String(64), nullable=False),
    Column("request", JSON, nullable=False),
    Column("result", JSON, nullable=False),
    Column("state", String(32), nullable=False),
    Column("created_at", String(40), nullable=False),
)
attempts = table(
    "jev_operator_attempts",
    Column("run_id", ForeignKey("jev_operator_runs.id", ondelete="CASCADE"), nullable=False),
    Column("work_ids", JSON, nullable=False),
    Column("attempt", Integer, nullable=False),
    Column("state", String(32), nullable=False),
    Column("error", String(100)),
    Column("usage", JSON, nullable=False),
    Column("reserved_tokens", Integer, nullable=False),
    Column("elapsed_ms", Float, nullable=False),
    Column("created_at", String(40), nullable=False),
)
assertions = table(
    "jev_operator_assertions",
    Column(
        "observation_id",
        ForeignKey("jev_operator_observations.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("actor", String(100), nullable=False),
    Column("value", JSON, nullable=False),
    Column("reason", Text, nullable=False),
    Column("created_at", String(40), nullable=False),
)
definitions = table(
    "jev_operator_definitions",
    Column("name", String(160), nullable=False),
    Column("owner", String(100), nullable=False),
    Column("definition", JSON, nullable=False),
    Column("status", String(32), nullable=False),
    Column("validation", JSON, nullable=False),
    Column("created_at", String(40), nullable=False),
)
approvals = table(
    "jev_operator_approvals",
    Column("actor", String(100), nullable=False),
    Column("plan_hash", String(64), nullable=False),
    Column("limits", JSON, nullable=False),
    Column("expires_at", Float, nullable=False),
    Column("created_at", String(40), nullable=False),
)

generations = table(
    "jev_operator_generations",
    Column("definition_id", ForeignKey("jev_operator_definitions.id"), nullable=False),
    Column("run_id", ForeignKey("jev_operator_runs.id"), nullable=False),
    Column("target_scope", JSON, nullable=False),
    Column("refresh_policy", JSON, nullable=False),
    Column("snapshot", JSON, nullable=False),
    Column("state", String(32), nullable=False),
    Column("coverage", JSON, nullable=False),
    Column("created_at", String(40), nullable=False),
)

Index("ix_jev_operator_cache", observations.c.tenant, observations.c.cache_key)
Index("ix_jev_operator_invalid", invalidations.c.tenant, invalidations.c.observation_id)
Index("ix_jev_operator_runs", runs.c.tenant, runs.c.created_at)

subscriptions = table(
    "jev_operator_subscriptions",
    Column("owner", String(100), nullable=False),
    Column("generation_id", ForeignKey("jev_operator_generations.id"), nullable=False),
    Column("limits", JSON, nullable=False),
    Column("policy", JSON, nullable=False),
    Column("remaining", Integer, nullable=False),
    Column("interval_seconds", Integer, nullable=False),
    Column("next_check", Float, nullable=False),
    Column("lease_until", Float, nullable=False),
    Column("last_state", String(32), nullable=False),
    Column("created_at", String(40), nullable=False),
)
