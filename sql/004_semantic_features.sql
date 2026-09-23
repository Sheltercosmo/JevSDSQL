-- v0.4 additive feature tables. Use scripts.migrate_generic for RLS, guards and grants.

CREATE TABLE dataset_feature_revisions (
	id VARCHAR(64) NOT NULL,
	tenant VARCHAR(100) NOT NULL,
	dataset_id VARCHAR(64) NOT NULL,
	name VARCHAR(120) NOT NULL,
	revision INTEGER NOT NULL,
	definition JSON NOT NULL,
	status VARCHAR(24) NOT NULL,
	owner VARCHAR(120) NOT NULL,
	review JSON NOT NULL,
	maintain INTEGER NOT NULL,
	materialization JSON NOT NULL,
	created_at VARCHAR(32) NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant, dataset_id, name, revision),
	FOREIGN KEY(dataset_id) REFERENCES dataset_catalog (id) ON DELETE CASCADE
);

CREATE INDEX ix_dataset_feature_revisions_tenant ON dataset_feature_revisions (tenant);

CREATE INDEX ix_feature_lookup ON dataset_feature_revisions (tenant, dataset_id, status);

CREATE TABLE dataset_semantic_answers (
	id VARCHAR(64) NOT NULL,
	tenant VARCHAR(100) NOT NULL,
	evidence_id VARCHAR(64) NOT NULL,
	source_version_id VARCHAR(64) NOT NULL,
	feature_id VARCHAR(64),
	answer JSON NOT NULL,
	span JSON,
	created_at VARCHAR(32) NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (evidence_id),
	FOREIGN KEY(evidence_id) REFERENCES dataset_evidence (id) ON DELETE CASCADE,
	FOREIGN KEY(source_version_id) REFERENCES dataset_row_versions (id) ON DELETE CASCADE,
	FOREIGN KEY(feature_id) REFERENCES dataset_feature_revisions (id) ON DELETE CASCADE
);

CREATE INDEX ix_dataset_semantic_answers_tenant ON dataset_semantic_answers (tenant);

CREATE TABLE dataset_inference_calls (
	id VARCHAR(64) NOT NULL,
	tenant VARCHAR(100) NOT NULL,
	dataset_id VARCHAR(64) NOT NULL,
	row_key VARCHAR(64) NOT NULL,
	model VARCHAR(100) NOT NULL,
	questions INTEGER NOT NULL,
	state VARCHAR(24) NOT NULL,
	usage JSON NOT NULL,
	elapsed_ms FLOAT NOT NULL,
	error VARCHAR(100),
	created_at VARCHAR(32) NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(dataset_id) REFERENCES dataset_catalog (id) ON DELETE CASCADE
);

CREATE INDEX ix_dataset_inference_calls_tenant ON dataset_inference_calls (tenant);

CREATE TABLE dataset_feature_assertions (
	id VARCHAR(64) NOT NULL,
	tenant VARCHAR(100) NOT NULL,
	feature_id VARCHAR(64) NOT NULL,
	row_key VARCHAR(64) NOT NULL,
	dependency_hash VARCHAR(64) NOT NULL,
	source_version_id VARCHAR(64) NOT NULL,
	value JSON NOT NULL,
	actor VARCHAR(120) NOT NULL,
	reason TEXT NOT NULL,
	created_at VARCHAR(32) NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(feature_id) REFERENCES dataset_feature_revisions (id) ON DELETE CASCADE,
	FOREIGN KEY(source_version_id) REFERENCES dataset_row_versions (id) ON DELETE CASCADE
);

CREATE INDEX ix_dataset_feature_assertions_tenant ON dataset_feature_assertions (tenant);

CREATE INDEX ix_feature_review_dependencies ON dataset_feature_assertions (tenant, feature_id, row_key, dependency_hash);

CREATE TABLE dataset_feature_jobs (
	id VARCHAR(64) NOT NULL,
	tenant VARCHAR(100) NOT NULL,
	dataset_id VARCHAR(64) NOT NULL,
	state VARCHAR(24) NOT NULL,
	lease_token VARCHAR(64),
	lease_until FLOAT NOT NULL,
	attempts INTEGER NOT NULL,
	available_at FLOAT NOT NULL,
	run_id VARCHAR(64),
	error VARCHAR(100),
	created_at VARCHAR(32) NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(dataset_id) REFERENCES dataset_catalog (id) ON DELETE CASCADE
);

CREATE INDEX ix_dataset_feature_jobs_tenant ON dataset_feature_jobs (tenant);

CREATE INDEX ix_feature_job_ready ON dataset_feature_jobs (tenant, state, available_at);
