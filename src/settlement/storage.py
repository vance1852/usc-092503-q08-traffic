"""轻微事故快速结算的 SQLite 模式与事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS settlement_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('officer','liaison','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

-- 案件生命周期由办案侧管理；结算服务登记案件并对结案进行门禁。
CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK(status IN ('open','liability_effective','in_settlement','closed')),
    created_by TEXT NOT NULL REFERENCES settlement_users(user_id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 已生效的责任认定：只追加；更正以新版本取代，旧版本保留为 superseded。
CREATE TABLE IF NOT EXISTS liability_findings (
    finding_id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    revision INTEGER NOT NULL,
    shares_json TEXT NOT NULL,
    basis_json TEXT NOT NULL,
    basis_sha256 TEXT NOT NULL,
    idempotency_key TEXT,
    state TEXT NOT NULL DEFAULT 'effective'
        CHECK(state IN ('effective','superseded')),
    supersedes_finding_id INTEGER REFERENCES liability_findings(finding_id),
    effective_at TEXT NOT NULL,
    issued_by TEXT NOT NULL REFERENCES settlement_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(case_id, revision),
    UNIQUE(case_id, idempotency_key)
);

-- 快速结算单：一个案件同一时间只有一张未终结结算单，金额由当前报价版本决定。
CREATE TABLE IF NOT EXISTS settlement_sheets (
    settlement_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    liability_finding_id INTEGER NOT NULL REFERENCES liability_findings(finding_id),
    current_quote_version INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'awaiting_confirmation'
        CHECK(state IN ('awaiting_confirmation','confirmed','partially_paid','settled','abandoned')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES settlement_users(user_id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sheets_case ON settlement_sheets(case_id);

-- 修理厂报价版本：只追加。新版本产生后旧确认失效，但每一版的报价与计算结果都保留。
CREATE TABLE IF NOT EXISTS sheet_quote_versions (
    settlement_id TEXT NOT NULL REFERENCES settlement_sheets(settlement_id),
    quote_version INTEGER NOT NULL,
    garage_id TEXT NOT NULL,
    quote_json TEXT NOT NULL,
    quote_sha256 TEXT NOT NULL,
    calc_json TEXT NOT NULL,
    calc_sha256 TEXT NOT NULL,
    gross_total_cny TEXT NOT NULL,
    deductible_total_cny TEXT NOT NULL,
    claim_total_cny TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL REFERENCES settlement_users(user_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(settlement_id, quote_version)
);

-- 当事人确认：只追加。拒绝签署是终态事实，任何流程都不得改写；
-- confirmed 在报价版本变化后置为 void，原始意思表示与时间完整保留。
CREATE TABLE IF NOT EXISTS party_confirmations (
    confirmation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    settlement_id TEXT NOT NULL REFERENCES settlement_sheets(settlement_id),
    quote_version INTEGER NOT NULL,
    party_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('confirmed','rejected','void')),
    note TEXT NOT NULL DEFAULT '',
    decided_by TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    voided_at TEXT,
    void_reason TEXT NOT NULL DEFAULT '',
    UNIQUE(settlement_id, quote_version, party_id)
);

CREATE INDEX IF NOT EXISTS idx_confirmations_sheet ON party_confirmations(settlement_id, confirmation_id);

-- 支付台账：收款与退款均只追加，不允许删除或覆盖；支付回执全局唯一。
CREATE TABLE IF NOT EXISTS payment_entries (
    payment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    settlement_id TEXT NOT NULL REFERENCES settlement_sheets(settlement_id),
    party_id TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('inbound','refund')),
    amount_cny TEXT NOT NULL,
    receipt_no TEXT NOT NULL UNIQUE,
    channel TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    received_at TEXT NOT NULL,
    recorded_by TEXT NOT NULL REFERENCES settlement_users(user_id),
    recorded_at TEXT NOT NULL,
    CHECK(CAST(amount_cny AS REAL) > 0)
);

CREATE INDEX IF NOT EXISTS idx_payments_sheet ON payment_entries(settlement_id, payment_id);

-- 哈希链审计事件：用于重建金额为何变化、谁在何时确认或拒签。
CREATE TABLE IF NOT EXISTS settlement_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_settlement_audit_entity
ON settlement_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    # ThreadingHTTPServer 会在工作线程中复用连接；WAL 与 BEGIN IMMEDIATE 保证写入串行。
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
