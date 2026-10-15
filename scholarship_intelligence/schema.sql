-- Scholarship Intelligence: eight relational tables.
-- Dates: ISO calendar dates.
-- Timestamps: timezone-aware ISO strings supplied by Python.
-- Enable foreign keys on every connection, not only during initialization.

PRAGMA foreign_keys = ON;

BEGIN;

-- 1. Continuing programmes: 3 columns.
-- Matching names alone does not establish programme identity.
CREATE TABLE IF NOT EXISTS programmes (
    programme_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    provider_name TEXT NOT NULL CHECK (length(trim(provider_name)) > 0)
);

-- 2. Accepted offering details: 16 columns.
-- Unknown values remain NULL; reasons belong in verification_details_json.
CREATE TABLE IF NOT EXISTS scholarships (
    scholarship_id INTEGER PRIMARY KEY,
    programme_id INTEGER NOT NULL
        REFERENCES programmes(programme_id) ON DELETE RESTRICT,
    cycle TEXT CHECK (cycle IS NULL OR length(trim(cycle)) > 0),

    benefit_json TEXT CHECK (
        benefit_json IS NULL OR
        CASE WHEN json_valid(benefit_json)
            THEN json_type(benefit_json) = 'array'
            ELSE 0
        END
    ),
    eligibility_json TEXT CHECK (
        eligibility_json IS NULL OR
        CASE WHEN json_valid(eligibility_json)
            THEN json_type(eligibility_json) = 'object'
            ELSE 0
        END
    ),

    opening_date TEXT,
    closing_date TEXT,

    documents_json TEXT CHECK (
        documents_json IS NULL OR
        CASE WHEN json_valid(documents_json)
            THEN json_type(documents_json) = 'array'
            ELSE 0
        END
    ),
    selection_process TEXT,
    renewal_json TEXT CHECK (
        renewal_json IS NULL OR
        CASE WHEN json_valid(renewal_json)
            THEN json_type(renewal_json) = 'object'
            ELSE 0
        END
    ),

    lifecycle_status TEXT NOT NULL DEFAULT 'REVIEW_REQUIRED'
        CHECK (lifecycle_status IN (
            'ACTIVE',
            'EXPIRING_SOON',
            'EXPIRED',
            'REVIEW_REQUIRED',
            'NO_LONGER_VERIFIABLE'
        )),
    verification_status TEXT NOT NULL DEFAULT 'REVIEW_REQUIRED'
        CHECK (verification_status IN ('VERIFIED', 'REVIEW_REQUIRED')),
    confidence_score REAL CHECK (
        confidence_score IS NULL OR
        confidence_score BETWEEN 0 AND 100
    ),

    verification_details_json TEXT NOT NULL
        DEFAULT '{"critical_checks_passed":false,"missing_reasons":{},"blockers":[]}'
        CHECK (
            CASE WHEN json_valid(verification_details_json)
                THEN json_type(verification_details_json) = 'object'
                ELSE 0
            END
        ),

    last_verified_at TEXT,
    last_changed_at TEXT,

    CHECK (
        verification_status != 'VERIFIED' OR (
            confidence_score IS NOT NULL
            AND confidence_score >= 95
            AND last_verified_at IS NOT NULL
            AND CASE WHEN json_valid(verification_details_json)
                THEN coalesce(
                    json_type(
                        verification_details_json,
                        '$.critical_checks_passed'
                    ),
                    ''
                ) = 'true'
                ELSE 0
            END
        )
    ),
    CHECK (
        verification_status != 'REVIEW_REQUIRED'
        OR confidence_score IS NULL
        OR confidence_score < 95
    )
);

-- 3. Source URLs and access/authenticity tracking: 7 columns.
CREATE TABLE IF NOT EXISTS sources (
    source_id INTEGER PRIMARY KEY,
    url TEXT NOT NULL UNIQUE CHECK (length(trim(url)) > 0),
    source_type TEXT NOT NULL DEFAULT 'unknown'
        CHECK (source_type IN (
            'government',
            'university',
            'corporate_csr',
            'international',
            'ngo_trust',
            'aggregator',
            'other',
            'unknown'
        )),
    authenticity_json TEXT NOT NULL
        DEFAULT '{"status":"unresolved","basis":[]}'
        CHECK (
            CASE WHEN json_valid(authenticity_json)
                THEN json_type(authenticity_json) = 'object'
                ELSE 0
            END
        ),
    last_attempt_at TEXT,
    last_success_at TEXT,
    last_access_result TEXT
);

-- 4. Offering/source relationships: 3 columns.
CREATE TABLE IF NOT EXISTS scholarship_sources (
    scholarship_id INTEGER NOT NULL
        REFERENCES scholarships(scholarship_id) ON DELETE RESTRICT,
    source_id INTEGER NOT NULL
        REFERENCES sources(source_id) ON DELETE RESTRICT,
    role TEXT NOT NULL CHECK (role IN (
        'primary',
        'guideline',
        'announcement',
        'application',
        'discovery'
    )),
    PRIMARY KEY (scholarship_id, source_id, role)
);

-- 5. Preserved source/preparation versions: 6 columns.
-- Identical raw bytes may be shared by different preparation versions.
-- Keep those versions separate so historical block references stay inspectable.
CREATE TABLE IF NOT EXISTS source_snapshots (
    snapshot_id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL
        REFERENCES sources(source_id) ON DELETE RESTRICT,
    retrieved_at TEXT NOT NULL,
    content_hash TEXT NOT NULL CHECK (
        length(content_hash) = 64
        AND content_hash NOT GLOB '*[^0-9a-f]*'
    ),
    raw_file_path TEXT NOT NULL CHECK (length(trim(raw_file_path)) > 0),
    prepared_content_path TEXT NOT NULL
        CHECK (length(trim(prepared_content_path)) > 0),
    UNIQUE (source_id, content_hash, prepared_content_path)
);

-- 6. Field evidence associations: 8 columns.
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id INTEGER PRIMARY KEY,
    scholarship_id INTEGER NOT NULL
        REFERENCES scholarships(scholarship_id) ON DELETE RESTRICT,
    field_name TEXT NOT NULL CHECK (length(trim(field_name)) > 0),
    snapshot_id INTEGER NOT NULL
        REFERENCES source_snapshots(snapshot_id) ON DELETE RESTRICT,
    quote TEXT NOT NULL CHECK (length(trim(quote)) > 0),
    location_json TEXT NOT NULL CHECK (
        CASE WHEN json_valid(location_json)
            THEN json_type(location_json) = 'object'
            ELSE 0
        END
    ),
    check_result_json TEXT NOT NULL DEFAULT '{}' CHECK (
        CASE WHEN json_valid(check_result_json)
            THEN json_type(check_result_json) = 'object'
            ELSE 0
        END
    ),
    is_current INTEGER NOT NULL DEFAULT 1
        CHECK (is_current IN (0, 1))
);

-- 7. Meaningful accepted changes: 9 columns.
-- Evidence-ID arrays require application-level reference validation.
CREATE TABLE IF NOT EXISTS changes (
    change_id INTEGER PRIMARY KEY,
    scholarship_id INTEGER NOT NULL
        REFERENCES scholarships(scholarship_id) ON DELETE RESTRICT,
    field_name TEXT NOT NULL CHECK (length(trim(field_name)) > 0),
    old_value_json TEXT NOT NULL CHECK (json_valid(old_value_json)),
    new_value_json TEXT NOT NULL CHECK (json_valid(new_value_json)),
    detected_at TEXT NOT NULL,
    old_evidence_ids_json TEXT NOT NULL CHECK (
        CASE WHEN json_valid(old_evidence_ids_json)
            THEN json_type(old_evidence_ids_json) = 'array'
            ELSE 0
        END
    ),
    new_evidence_ids_json TEXT NOT NULL CHECK (
        CASE WHEN json_valid(new_evidence_ids_json)
            THEN json_type(new_evidence_ids_json) = 'array'
            ELSE 0
        END
    ),
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0)
);

-- 8. Pipeline executions: 6 columns.
CREATE TABLE IF NOT EXISTS crawl_runs (
    run_id INTEGER PRIMARY KEY,
    trigger TEXT NOT NULL CHECK (trigger IN (
        'command', 'manual', 'scheduled'
    )),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'RUNNING' CHECK (status IN (
        'RUNNING', 'COMPLETED', 'PARTIAL', 'FAILED'
    )),
    summary_json TEXT NOT NULL DEFAULT '{}' CHECK (
        CASE WHEN json_valid(summary_json)
            THEN json_type(summary_json) = 'object'
            ELSE 0
        END
    ),
    CHECK (
        (status = 'RUNNING' AND finished_at IS NULL)
        OR (status != 'RUNNING' AND finished_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_programmes_name_provider
    ON programmes(name, provider_name);

CREATE INDEX IF NOT EXISTS idx_scholarships_programme_cycle
    ON scholarships(programme_id, cycle);

CREATE INDEX IF NOT EXISTS idx_scholarships_verification
    ON scholarships(verification_status, confidence_score);

CREATE INDEX IF NOT EXISTS idx_scholarships_lifecycle_deadline
    ON scholarships(lifecycle_status, closing_date);

CREATE INDEX IF NOT EXISTS idx_scholarship_sources_source
    ON scholarship_sources(source_id, scholarship_id);

CREATE INDEX IF NOT EXISTS idx_evidence_current_field
    ON evidence(scholarship_id, field_name, is_current);

CREATE INDEX IF NOT EXISTS idx_evidence_snapshot
    ON evidence(snapshot_id);

CREATE INDEX IF NOT EXISTS idx_changes_history
    ON changes(scholarship_id, detected_at, change_id);

CREATE INDEX IF NOT EXISTS idx_crawl_runs_status
    ON crawl_runs(status, started_at);

COMMIT;