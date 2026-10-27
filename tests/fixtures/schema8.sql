CREATE TABLE activities (
	id VARCHAR(36) NOT NULL, 
	job_id VARCHAR(36) NOT NULL, 
	kind VARCHAR(50) NOT NULL, 
	message TEXT NOT NULL, 
	details JSON NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(job_id) REFERENCES jobs (id)
);
CREATE TABLE agent_tokens (
	id VARCHAR(36) NOT NULL, 
	name VARCHAR(80) NOT NULL, 
	prefix VARCHAR(18) NOT NULL, 
	token_hash VARCHAR(64) NOT NULL, 
	permissions JSON NOT NULL, 
	created_at DATETIME NOT NULL, 
	expires_at DATETIME NOT NULL, 
	last_used_at DATETIME, 
	revoked_at DATETIME, 
	PRIMARY KEY (id), 
	UNIQUE (token_hash)
);
CREATE TABLE contacts (
	id VARCHAR(36) NOT NULL, 
	job_id VARCHAR(36) NOT NULL, 
	name VARCHAR(500) NOT NULL, 
	email VARCHAR(500) NOT NULL, 
	notes TEXT NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(job_id) REFERENCES jobs (id)
);
CREATE TABLE extraction_tasks (
	id VARCHAR(36) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	result JSON NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);
CREATE TABLE ingestion_requests (
	id VARCHAR(36) NOT NULL, 
	idempotency_key VARCHAR(128) NOT NULL, 
	token_id VARCHAR(36), 
	url TEXT NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	result JSON NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (idempotency_key), 
	FOREIGN KEY(token_id) REFERENCES agent_tokens (id)
);
CREATE TABLE jobs (
	id VARCHAR(36) NOT NULL, 
	title VARCHAR(500) NOT NULL, 
	company VARCHAR(500) NOT NULL, 
	url TEXT NOT NULL, 
	found_date DATE NOT NULL, 
	description TEXT NOT NULL, 
	location VARCHAR(500) NOT NULL, 
	salary VARCHAR(500) NOT NULL, 
	lane VARCHAR(500) NOT NULL, 
	priority VARCHAR(20) NOT NULL, 
	stage VARCHAR(40) NOT NULL, 
	outreach_completed BOOLEAN DEFAULT '0' NOT NULL, 
	outcome VARCHAR(20) NOT NULL, 
	notes TEXT NOT NULL, 
	next_action TEXT NOT NULL, 
	next_action_date DATE, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	version INTEGER NOT NULL, 
	PRIMARY KEY (id), 
	CHECK (stage IN ('needs_review','interesting','resume_updated','outreach','applied','recruiter_interview','hiring_manager_interview','panel_interview','offer')), 
	CHECK (outcome IN ('active','rejected','withdrawn','closed','accepted','not_interested')), 
	CHECK (priority IN ('low','medium','high')), 
	CHECK (version > 0)
);
CREATE TABLE resumes (
	id VARCHAR(36) NOT NULL, 
	job_id VARCHAR(36) NOT NULL, 
	original_filename VARCHAR(500) NOT NULL, 
	stored_filename VARCHAR(100) NOT NULL, 
	media_type VARCHAR(200) NOT NULL, 
	size INTEGER NOT NULL, 
	sha256 VARCHAR(64) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CHECK (size >= 0), 
	FOREIGN KEY(job_id) REFERENCES jobs (id), 
	UNIQUE (stored_filename)
);
CREATE TABLE schema_version (version INTEGER NOT NULL);
CREATE TABLE workspace_credentials (
	id INTEGER NOT NULL, 
	password_hash VARCHAR(512) NOT NULL, 
	session_version INTEGER NOT NULL, 
	PRIMARY KEY (id), 
	CHECK (id = 1), 
	CHECK (session_version > 0)
);
CREATE INDEX ix_activities_job_id ON activities (job_id);
CREATE INDEX ix_contacts_job_id ON contacts (job_id);
CREATE INDEX ix_extraction_tasks_created_at ON extraction_tasks (created_at);
CREATE INDEX ix_ingestion_requests_created_at ON ingestion_requests (created_at);
CREATE INDEX ix_ingestion_requests_token_id ON ingestion_requests (token_id);
CREATE INDEX ix_resumes_job_id ON resumes (job_id);
