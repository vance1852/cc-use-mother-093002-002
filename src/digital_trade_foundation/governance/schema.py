"""承诺治理模块在基础库表之外扩展的 SQLite 表结构。"""

from __future__ import annotations


SCHEMA = """
CREATE TABLE IF NOT EXISTS governance_agreements (
    agreement_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    title TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('negotiating','committed','active','exiting','exited','terminated')),
    currency TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS governance_parties (
    party_id TEXT PRIMARY KEY,
    agreement_id TEXT NOT NULL REFERENCES governance_agreements(agreement_id),
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    party_role TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','replaced','withdrawn')),
    replaced_by TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS governance_documents (
    document_id TEXT PRIMARY KEY,
    agreement_id TEXT NOT NULL REFERENCES governance_agreements(agreement_id),
    kind TEXT NOT NULL CHECK(kind IN ('negotiation_draft','formal_commitment','performance_evidence','dispute_ruling')),
    title TEXT NOT NULL,
    content_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    supersedes TEXT,
    status TEXT NOT NULL CHECK(status IN ('proposed','superseded','registered','submitted','concluded')),
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS governance_commitments (
    commitment_id TEXT PRIMARY KEY,
    agreement_id TEXT NOT NULL REFERENCES governance_agreements(agreement_id),
    category TEXT NOT NULL CHECK(category IN
        ('contribution','beneficiary','localization','ip_data','escrow','risk_guarantee','exit_duty')),
    title TEXT NOT NULL,
    stage INTEGER NOT NULL CHECK(stage >= 1),
    responsible_party_id TEXT NOT NULL REFERENCES governance_parties(party_id),
    target_amount INTEGER NOT NULL CHECK(target_amount >= 0),
    fulfilled_amount INTEGER NOT NULL CHECK(fulfilled_amount >= 0),
    terms_json TEXT NOT NULL,
    original_terms_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN
        ('draft','committed','active','fulfilled','breached','rectifying','superseded','withdrawn')),
    source_document_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS governance_amendments (
    amendment_id TEXT PRIMARY KEY,
    commitment_id TEXT NOT NULL REFERENCES governance_commitments(commitment_id),
    kind TEXT NOT NULL CHECK(kind IN ('scope_reduction','party_replacement')),
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS governance_condition_groups (
    group_id TEXT PRIMARY KEY,
    agreement_id TEXT NOT NULL REFERENCES governance_agreements(agreement_id),
    title TEXT NOT NULL,
    effect_type TEXT NOT NULL CHECK(effect_type IN ('activate_stage','release_tranche')),
    effect_target TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','verified')),
    verified_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS governance_conditions (
    condition_id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL REFERENCES governance_condition_groups(group_id),
    commitment_id TEXT NOT NULL REFERENCES governance_commitments(commitment_id),
    description TEXT NOT NULL,
    evidence_due_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('pending','verified')),
    verified_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS governance_evidence (
    evidence_id TEXT PRIMARY KEY,
    condition_id TEXT NOT NULL REFERENCES governance_conditions(condition_id),
    document_id TEXT NOT NULL REFERENCES governance_documents(document_id),
    submitted_by TEXT NOT NULL REFERENCES actors(actor_id),
    submitter_org TEXT NOT NULL,
    late INTEGER NOT NULL CHECK(late IN (0, 1)),
    status TEXT NOT NULL CHECK(status IN ('submitted','accepted','rejected')),
    reviewed_by TEXT,
    reviewed_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS governance_fulfillments (
    fulfillment_id TEXT PRIMARY KEY,
    commitment_id TEXT NOT NULL REFERENCES governance_commitments(commitment_id),
    amount INTEGER NOT NULL CHECK(amount > 0),
    note TEXT NOT NULL,
    submitted_by TEXT NOT NULL REFERENCES actors(actor_id),
    submitter_org TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending_review','approved','rejected')),
    reviewed_by TEXT,
    reviewed_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS governance_escrow_accounts (
    account_id TEXT PRIMARY KEY,
    agreement_id TEXT NOT NULL UNIQUE REFERENCES governance_agreements(agreement_id),
    currency TEXT NOT NULL,
    opened_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS governance_escrow_ledger (
    entry_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES governance_escrow_accounts(account_id),
    entry_type TEXT NOT NULL CHECK(entry_type IN ('deposit','disburse')),
    amount INTEGER NOT NULL CHECK(amount > 0),
    tranche_id TEXT,
    reference TEXT NOT NULL,
    balance_after INTEGER NOT NULL CHECK(balance_after >= 0),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS governance_tranches (
    tranche_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES governance_escrow_accounts(account_id),
    sequence_no INTEGER NOT NULL,
    amount INTEGER NOT NULL CHECK(amount > 0),
    condition_group_id TEXT NOT NULL REFERENCES governance_condition_groups(group_id),
    status TEXT NOT NULL CHECK(status IN ('scheduled','held','released','disbursed','cancelled')),
    release_pending INTEGER NOT NULL CHECK(release_pending IN (0, 1)),
    created_at TEXT NOT NULL,
    UNIQUE(account_id, sequence_no)
);
CREATE TABLE IF NOT EXISTS governance_outcomes (
    outcome_id TEXT PRIMARY KEY,
    agreement_id TEXT NOT NULL REFERENCES governance_agreements(agreement_id),
    outcome_key TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    beneficiary_group TEXT NOT NULL,
    shared INTEGER NOT NULL CHECK(shared IN (0, 1)),
    claimed_by_project TEXT,
    claimed_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS governance_breaches (
    breach_id TEXT PRIMARY KEY,
    commitment_id TEXT NOT NULL REFERENCES governance_commitments(commitment_id),
    severity TEXT NOT NULL CHECK(severity IN ('partial','full')),
    description TEXT NOT NULL,
    unfulfilled_amount INTEGER NOT NULL CHECK(unfulfilled_amount >= 0),
    previous_status TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','rectifying','resolved','escalated')),
    reported_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS governance_rectifications (
    rectification_id TEXT PRIMARY KEY,
    breach_id TEXT NOT NULL REFERENCES governance_breaches(breach_id),
    requirement TEXT NOT NULL,
    due_at TEXT NOT NULL,
    order_seq INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','submitted','approved','rejected','overdue')),
    submitted_at TEXT,
    reviewed_by TEXT,
    reviewed_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS governance_disputes (
    dispute_id TEXT PRIMARY KEY,
    agreement_id TEXT NOT NULL REFERENCES governance_agreements(agreement_id),
    commitment_id TEXT,
    raised_by TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('filed','concluded')),
    ruling_document_id TEXT,
    concluded_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS governance_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    agreement_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_governance_events_agreement
    ON governance_events(agreement_id, occurred_at, sequence);
CREATE INDEX IF NOT EXISTS idx_governance_commitments_agreement
    ON governance_commitments(agreement_id, status);
CREATE INDEX IF NOT EXISTS idx_governance_conditions_group
    ON governance_conditions(group_id);
CREATE INDEX IF NOT EXISTS idx_governance_evidence_condition
    ON governance_evidence(condition_id, status);
CREATE INDEX IF NOT EXISTS idx_governance_ledger_account
    ON governance_escrow_ledger(account_id);
"""


def ensure_schema(connection) -> None:
    """在基础服务同一连接上建治理模块的表。"""

    connection.executescript(SCHEMA)
