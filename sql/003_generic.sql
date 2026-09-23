-- Generic v0.3 catalog/evidence DDL. Apply tenant RLS/grants with scripts.migrate_generic.


CREATE TABLE dataset_catalog (
	id VARCHAR(64) NOT NULL,
	tenant VARCHAR(100) NOT NULL,
	name VARCHAR(160) NOT NULL,
	description TEXT NOT NULL,
	schema_name VARCHAR(100),
	table_name VARCHAR(160) NOT NULL,
	columns JSON NOT NULL,
	primary_key JSON NOT NULL,
	writable INTEGER NOT NULL,
	links JSON NOT NULL,
	created_at VARCHAR(32) NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant, name)
)

;

CREATE INDEX ix_dataset_catalog_tenant ON dataset_catalog (tenant);


CREATE TABLE dataset_mutation_previews (
	id VARCHAR(64) NOT NULL,
	tenant VARCHAR(100) NOT NULL,
	logical_sql TEXT NOT NULL,
	dataset_ids JSON NOT NULL,
	snapshot_hash VARCHAR(64) NOT NULL,
	affected_rows INTEGER NOT NULL,
	options JSON NOT NULL,
	expires_at FLOAT NOT NULL,
	state VARCHAR(24) NOT NULL,
	actor VARCHAR(100) NOT NULL,
	created_at VARCHAR(32) NOT NULL,
	PRIMARY KEY (id)
)

;

CREATE INDEX ix_dataset_mutation_previews_tenant ON dataset_mutation_previews (tenant);


CREATE TABLE dataset_query_runs (
	id VARCHAR(64) NOT NULL,
	tenant VARCHAR(100) NOT NULL,
	request TEXT NOT NULL,
	logical_sql TEXT NOT NULL,
	compiled_sql TEXT NOT NULL,
	parameters JSON NOT NULL,
	plan JSON NOT NULL,
	manifest JSON NOT NULL,
	result JSON NOT NULL,
	created_at VARCHAR(32) NOT NULL,
	PRIMARY KEY (id)
)

;

CREATE INDEX ix_dataset_query_runs_tenant ON dataset_query_runs (tenant);


CREATE TABLE dataset_evidence (
	id VARCHAR(64) NOT NULL,
	tenant VARCHAR(100) NOT NULL,
	dataset_id VARCHAR(64) NOT NULL,
	row_key VARCHAR(64) NOT NULL,
	row_hash VARCHAR(64) NOT NULL,
	definition TEXT NOT NULL,
	evaluator VARCHAR(100) NOT NULL,
	state VARCHAR(24) NOT NULL,
	probability FLOAT,
	responder VARCHAR(100),
	usage JSON NOT NULL,
	lease_token VARCHAR(64),
	lease_until FLOAT NOT NULL,
	attempts INTEGER NOT NULL,
	error VARCHAR(100),
	created_at VARCHAR(32) NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(dataset_id) REFERENCES dataset_catalog (id) ON DELETE CASCADE
)

;

CREATE INDEX ix_dataset_evidence_tenant ON dataset_evidence (tenant);


CREATE TABLE dataset_row_versions (
	id VARCHAR(64) NOT NULL,
	tenant VARCHAR(100) NOT NULL,
	dataset_id VARCHAR(64) NOT NULL,
	row_key VARCHAR(64) NOT NULL,
	row_hash VARCHAR(64) NOT NULL,
	value JSON NOT NULL,
	created_at VARCHAR(32) NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(dataset_id) REFERENCES dataset_catalog (id) ON DELETE CASCADE
)

;

CREATE INDEX ix_dataset_row_versions_tenant ON dataset_row_versions (tenant);


CREATE TABLE dataset_evaluation_attempts (
	id VARCHAR(64) NOT NULL,
	tenant VARCHAR(100) NOT NULL,
	evidence_id VARCHAR(64) NOT NULL,
	state VARCHAR(24) NOT NULL,
	error VARCHAR(100),
	usage JSON NOT NULL,
	created_at VARCHAR(32) NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(evidence_id) REFERENCES dataset_evidence (id) ON DELETE CASCADE
)

;

CREATE INDEX ix_dataset_evaluation_attempts_tenant ON dataset_evaluation_attempts (tenant);
