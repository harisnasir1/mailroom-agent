CREATE TABLE IF NOT EXISTS matters (
    id INTEGER PRIMARY KEY,
    our_ref TEXT UNIQUE NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'closed')),
    claim_type TEXT NOT NULL CHECK (claim_type IN ('car_accident', 'housing')),
    client_name TEXT,
    other_party TEXT,
    fingerprint TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS matter_identifiers (
    id INTEGER PRIMARY KEY,
    matter_id INTEGER NOT NULL REFERENCES matters(id),
    type TEXT NOT NULL CHECK (type IN ('our_ref', 'insurer_ref', 'vehicle_reg', 'property_address', 'contact_email', 'contact_domain')),
    value_normalised TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('seed', 'human', 'auto')),
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE (matter_id, type, value_normalised)
);

CREATE INDEX IF NOT EXISTS idx_matter_identifiers_lookup ON matter_identifiers (type, value_normalised);

CREATE TABLE IF NOT EXISTS emails (
    id INTEGER PRIMARY KEY,
    message_id TEXT UNIQUE NOT NULL,
    sender TEXT,
    subject TEXT,
    body_raw TEXT,
    body_new TEXT,
    body_quoted TEXT,
    injection_flag INTEGER DEFAULT 0,
    status TEXT NOT NULL CHECK (status IN ('received', 'processing', 'matched', 'needs_review', 'refused')),
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY,
    email_id INTEGER NOT NULL REFERENCES emails(id),
    config TEXT NOT NULL,
    route TEXT NOT NULL CHECK (route IN ('matched', 'needs_review', 'refused')),
    matter_id INTEGER REFERENCES matters(id),
    reason_code TEXT,
    evidence_json TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE (email_id, config)
);

CREATE TABLE IF NOT EXISTS review_tasks (
    id INTEGER PRIMARY KEY,
    decision_id INTEGER NOT NULL REFERENCES decisions(id),
    packet_json TEXT,
    status TEXT NOT NULL CHECK (status IN ('open', 'resolved')),
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS traces (
    id INTEGER PRIMARY KEY,
    run_id TEXT,
    email_id INTEGER REFERENCES emails(id),
    stage TEXT,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd REAL,
    latency_ms INTEGER,
    retries INTEGER DEFAULT 0,
    error TEXT,
    output_json TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
