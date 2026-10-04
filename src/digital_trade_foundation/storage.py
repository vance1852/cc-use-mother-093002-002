"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','closed')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_parties (
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    party_role TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','replaced','exited')),
    joined_at TEXT NOT NULL,
    left_at TEXT,
    PRIMARY KEY(project_id, organization_id)
);
CREATE TABLE IF NOT EXISTS party_replacements (
    replacement_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    old_organization_id TEXT NOT NULL,
    new_organization_id TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS commitments (
    commitment_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    commitment_type TEXT NOT NULL,
    provider_organization_id TEXT NOT NULL,
    title TEXT NOT NULL,
    terms_json TEXT NOT NULL,
    original_amount_minor INTEGER CHECK(original_amount_minor IS NULL OR original_amount_minor >= 0),
    currency TEXT,
    status TEXT NOT NULL CHECK(status IN ('draft','committed','effective','breached','fulfilled','closed')),
    sealed_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS commitment_documents (
    document_id TEXT PRIMARY KEY,
    commitment_id TEXT NOT NULL REFERENCES commitments(commitment_id),
    kind TEXT NOT NULL CHECK(kind IN ('negotiation_draft','sealed_terms')),
    version INTEGER NOT NULL,
    terms_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(commitment_id, kind, version)
);
CREATE TABLE IF NOT EXISTS commitment_stages (
    stage_id TEXT PRIMARY KEY,
    commitment_id TEXT NOT NULL REFERENCES commitments(commitment_id),
    sequence INTEGER NOT NULL,
    name TEXT NOT NULL,
    due_at TEXT,
    disburse_amount_minor INTEGER NOT NULL DEFAULT 0 CHECK(disburse_amount_minor >= 0),
    status TEXT NOT NULL CHECK(status IN ('pending','effective','paid')),
    effective_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(commitment_id, sequence)
);
CREATE TABLE IF NOT EXISTS stage_conditions (
    condition_id TEXT PRIMARY KEY,
    stage_id TEXT NOT NULL REFERENCES commitment_stages(stage_id),
    sequence INTEGER NOT NULL,
    label TEXT NOT NULL,
    due_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('awaiting_evidence','in_review','accepted','rejected')),
    accepted_evidence_id TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(stage_id, sequence)
);
CREATE TABLE IF NOT EXISTS evidences (
    evidence_id TEXT PRIMARY KEY,
    condition_id TEXT NOT NULL REFERENCES stage_conditions(condition_id),
    submitter_actor_id TEXT NOT NULL,
    submitter_organization_id TEXT NOT NULL,
    reference TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    late INTEGER NOT NULL CHECK(late IN (0,1)),
    status TEXT NOT NULL CHECK(status IN ('submitted','accepted','rejected')),
    review_deadline_at TEXT NOT NULL,
    submitted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
    review_id TEXT PRIMARY KEY,
    evidence_id TEXT NOT NULL REFERENCES evidences(evidence_id),
    reviewer_actor_id TEXT,
    reviewer_organization_id TEXT,
    status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected')),
    decision_note TEXT NOT NULL DEFAULT '',
    due_at TEXT NOT NULL,
    remediation_deadline_at TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS escrow_accounts (
    project_id TEXT PRIMARY KEY REFERENCES projects(project_id),
    currency TEXT NOT NULL,
    deposited_total INTEGER NOT NULL DEFAULT 0 CHECK(deposited_total >= 0),
    disbursed_total INTEGER NOT NULL DEFAULT 0 CHECK(disbursed_total >= 0),
    balance INTEGER NOT NULL CHECK(balance = deposited_total - disbursed_total AND balance >= 0),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS escrow_deposits (
    deposit_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
    reference TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS disbursements (
    disbursement_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    commitment_id TEXT NOT NULL,
    stage_id TEXT NOT NULL UNIQUE,
    amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
    status TEXT NOT NULL CHECK(status IN ('instructed','failed','paid')),
    payment_reference TEXT,
    created_at TEXT NOT NULL,
    paid_at TEXT
);
CREATE TABLE IF NOT EXISTS responsibility_adjustments (
    adjustment_id TEXT PRIMARY KEY,
    commitment_id TEXT NOT NULL REFERENCES commitments(commitment_id),
    kind TEXT NOT NULL CHECK(kind IN ('late_evidence','partial_breach','party_replacement',
                                      'scope_reduction','dispute_relief')),
    amount_delta_minor INTEGER NOT NULL DEFAULT 0,
    old_organization_id TEXT,
    new_organization_id TEXT,
    portion_minor INTEGER NOT NULL DEFAULT 0,
    detail_json TEXT NOT NULL,
    dispute_id TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS disputes (
    dispute_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    commitment_id TEXT,
    status TEXT NOT NULL CHECK(status IN ('open','concluded')),
    title TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    opened_by TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    conclusion_json TEXT,
    concluded_by TEXT,
    concluded_at TEXT
);
CREATE TABLE IF NOT EXISTS outcomes (
    outcome_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    producer_project_id TEXT,
    measure_unit TEXT,
    measure_value INTEGER,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outcome_claims (
    claim_id TEXT PRIMARY KEY,
    outcome_id TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL,
    commitment_id TEXT,
    claimed_value INTEGER,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS state_snapshots (
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    valid_from TEXT NOT NULL,
    state_json TEXT NOT NULL,
    PRIMARY KEY(entity_type, entity_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_lookup
    ON state_snapshots(entity_type, entity_id, valid_from, sequence);
CREATE INDEX IF NOT EXISTS idx_reviews_pending ON reviews(status, due_at);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
