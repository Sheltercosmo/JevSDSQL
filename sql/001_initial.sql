-- SDD v0.1 initial schema. Apply once as schema owner, then run scripts.postgres_security.

CREATE TABLE concept_revisions (
	id VARCHAR(64) NOT NULL,
	tenant VARCHAR(100) NOT NULL,
	concept_key VARCHAR(100) NOT NULL,
	revision INTEGER NOT NULL,
	definition TEXT NOT NULL,
	subject_type VARCHAR(40) NOT NULL,
	output_type VARCHAR(30) NOT NULL,
	inclusion TEXT NOT NULL,
	exclusion TEXT NOT NULL,
	context_fields JSON NOT NULL,
	status VARCHAR(30) NOT NULL,
	owner VARCHAR(100) NOT NULL,
	review JSON NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant, concept_key, revision),
	CHECK (status IN ('provisional','validated','active','deprecated'))
);

CREATE INDEX ix_concept_revisions_tenant ON concept_revisions (tenant);

CREATE TABLE decision_policy_revisions (
	id VARCHAR(64) NOT NULL,
	tenant VARCHAR(100) NOT NULL,
	accept FLOAT NOT NULL,
	reject FLOAT NOT NULL,
	calibration JSON NOT NULL,
	PRIMARY KEY (id),
	CHECK (reject >= 0 AND accept <= 1 AND reject < accept)
);

CREATE INDEX ix_decision_policy_revisions_tenant ON decision_policy_revisions (tenant);

CREATE TABLE evaluator_revisions (
	id VARCHAR(64) NOT NULL,
	tenant VARCHAR(100) NOT NULL,
	provider VARCHAR(40) NOT NULL,
	model VARCHAR(100) NOT NULL,
	instructions TEXT NOT NULL,
	state_builder VARCHAR(40) NOT NULL,
	preprocessing VARCHAR(40) NOT NULL,
	chunking VARCHAR(40) NOT NULL,
	PRIMARY KEY (id)
);

CREATE INDEX ix_evaluator_revisions_tenant ON evaluator_revisions (tenant);

CREATE TABLE governance_events (
	id VARCHAR(64) NOT NULL,
	tenant VARCHAR(100) NOT NULL,
	concept_id VARCHAR(64) NOT NULL,
	actor VARCHAR(100) NOT NULL,
	action VARCHAR(30) NOT NULL,
	details JSON NOT NULL,
	created_at VARCHAR(32) NOT NULL,
	PRIMARY KEY (id)
);

CREATE INDEX ix_governance_events_tenant ON governance_events (tenant);

CREATE TABLE query_runs (
	id VARCHAR(64) NOT NULL,
	tenant VARCHAR(100) NOT NULL,
	plan JSON NOT NULL,
	snapshot JSON NOT NULL,
	manifest JSON NOT NULL,
	result JSON NOT NULL,
	created_at VARCHAR(32) NOT NULL,
	PRIMARY KEY (id)
);

CREATE INDEX ix_query_runs_tenant ON query_runs (tenant);

CREATE TABLE source_records (
	id VARCHAR(64) NOT NULL,
	tenant VARCHAR(100) NOT NULL,
	external_id VARCHAR(200) NOT NULL,
	source_system VARCHAR(100) NOT NULL,
	current_version VARCHAR(64),
	PRIMARY KEY (id),
	UNIQUE (tenant, source_system, external_id)
);

CREATE INDEX ix_source_records_tenant ON source_records (tenant);

CREATE TABLE tenant_daily_usage (
	id VARCHAR(64) NOT NULL,
	tenant VARCHAR(100) NOT NULL,
	day VARCHAR(10) NOT NULL,
	calls INTEGER NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant, day)
);

CREATE INDEX ix_tenant_daily_usage_tenant ON tenant_daily_usage (tenant);

CREATE TABLE materialization_policies (
	id VARCHAR(64) NOT NULL,
	tenant VARCHAR(100) NOT NULL,
	concept_id VARCHAR(64) NOT NULL,
	evaluator_id VARCHAR(64) NOT NULL,
	policy_id VARCHAR(64) NOT NULL,
	population JSON NOT NULL,
	budget INTEGER NOT NULL,
	freshness_seconds INTEGER NOT NULL,
	required_coverage FLOAT NOT NULL,
	owner VARCHAR(100) NOT NULL,
	last_run VARCHAR(64),
	PRIMARY KEY (id),
	FOREIGN KEY(concept_id) REFERENCES concept_revisions (id),
	FOREIGN KEY(evaluator_id) REFERENCES evaluator_revisions (id),
	FOREIGN KEY(policy_id) REFERENCES decision_policy_revisions (id)
);

CREATE INDEX ix_materialization_policies_tenant ON materialization_policies (tenant);

CREATE TABLE source_versions (
	id VARCHAR(64) NOT NULL,
	tenant VARCHAR(100) NOT NULL,
	record_id VARCHAR(64) NOT NULL,
	text TEXT NOT NULL,
	content_hash VARCHAR(64) NOT NULL,
	context JSON NOT NULL,
	context_hash VARCHAR(64) NOT NULL,
	customer_id VARCHAR(200) NOT NULL,
	segment VARCHAR(100) NOT NULL,
	product VARCHAR(200) NOT NULL,
	event_time VARCHAR(32) NOT NULL,
	ingested_at VARCHAR(32) NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(record_id) REFERENCES source_records (id) ON DELETE CASCADE
);

CREATE INDEX ix_source_versions_tenant ON source_versions (tenant);

CREATE TABLE evaluation_jobs (
	id VARCHAR(64) NOT NULL,
	tenant VARCHAR(100) NOT NULL,
	version_id VARCHAR(64) NOT NULL,
	concept_id VARCHAR(64) NOT NULL,
	evaluator_id VARCHAR(64) NOT NULL,
	context_hash VARCHAR(64) NOT NULL,
	state VARCHAR(20) NOT NULL,
	lease_token VARCHAR(64),
	lease_until FLOAT NOT NULL,
	attempts INTEGER NOT NULL,
	available_at FLOAT NOT NULL,
	max_attempts INTEGER NOT NULL,
	error TEXT,
	PRIMARY KEY (id),
	UNIQUE (tenant, version_id, concept_id, evaluator_id, context_hash),
	FOREIGN KEY(version_id) REFERENCES source_versions (id) ON DELETE CASCADE,
	FOREIGN KEY(concept_id) REFERENCES concept_revisions (id),
	FOREIGN KEY(evaluator_id) REFERENCES evaluator_revisions (id)
);

CREATE INDEX ix_evaluation_jobs_tenant ON evaluation_jobs (tenant);

CREATE INDEX ix_jobs_ready ON evaluation_jobs (tenant, state, available_at);

CREATE TABLE human_assertions (
	id VARCHAR(64) NOT NULL,
	tenant VARCHAR(100) NOT NULL,
	version_id VARCHAR(64) NOT NULL,
	concept_id VARCHAR(64) NOT NULL,
	decision VARCHAR(10) NOT NULL,
	reviewer VARCHAR(100) NOT NULL,
	reason TEXT NOT NULL,
	created_at VARCHAR(32) NOT NULL,
	supersedes VARCHAR(64),
	PRIMARY KEY (id),
	CHECK (decision IN ('true','false','unknown')),
	FOREIGN KEY(version_id) REFERENCES source_versions (id) ON DELETE CASCADE,
	FOREIGN KEY(concept_id) REFERENCES concept_revisions (id)
);

CREATE INDEX ix_human_assertions_tenant ON human_assertions (tenant);

CREATE TABLE evaluation_attempts (
	id VARCHAR(64) NOT NULL,
	tenant VARCHAR(100) NOT NULL,
	job_id VARCHAR(64) NOT NULL,
	lease_token VARCHAR(64) NOT NULL,
	status VARCHAR(30) NOT NULL,
	error TEXT,
	started_at FLOAT NOT NULL,
	finished_at FLOAT,
	usage JSON NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(job_id) REFERENCES evaluation_jobs (id) ON DELETE CASCADE
);

CREATE INDEX ix_evaluation_attempts_tenant ON evaluation_attempts (tenant);

CREATE TABLE observations (
	id VARCHAR(64) NOT NULL,
	tenant VARCHAR(100) NOT NULL,
	job_id VARCHAR(64) NOT NULL,
	version_id VARCHAR(64) NOT NULL,
	concept_id VARCHAR(64) NOT NULL,
	evaluator_id VARCHAR(64) NOT NULL,
	context_hash VARCHAR(64) NOT NULL,
	probability FLOAT NOT NULL,
	distribution JSON NOT NULL,
	responder VARCHAR(100) NOT NULL,
	evidence JSON NOT NULL,
	usage JSON NOT NULL,
	created_at VARCHAR(32) NOT NULL,
	PRIMARY KEY (id),
	CHECK (probability >= 0 AND probability <= 1),
	UNIQUE (job_id),
	FOREIGN KEY(job_id) REFERENCES evaluation_jobs (id) ON DELETE CASCADE,
	FOREIGN KEY(version_id) REFERENCES source_versions (id) ON DELETE CASCADE,
	FOREIGN KEY(concept_id) REFERENCES concept_revisions (id),
	FOREIGN KEY(evaluator_id) REFERENCES evaluator_revisions (id)
);

CREATE INDEX ix_observation_reuse ON observations (tenant, version_id, concept_id, evaluator_id, context_hash);

CREATE INDEX ix_observations_tenant ON observations (tenant);
