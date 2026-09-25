"""事故快处服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS traffic_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('planner','dispatcher','risk','auditor','officer')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_index_risk_records (
    risk_record_id INTEGER PRIMARY KEY AUTOINCREMENT,
    risk_index TEXT NOT NULL,
    duty_date TEXT NOT NULL,
    index_value TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    supersedes_risk_record_id INTEGER REFERENCES risk_index_risk_records(risk_record_id),
    recorded_by TEXT NOT NULL REFERENCES traffic_users(user_id),
    recorded_at TEXT NOT NULL,
    UNIQUE(risk_index, duty_date, source_revision)
);

CREATE INDEX IF NOT EXISTS idx_risk_records_series
ON risk_index_risk_records(risk_index, duty_date, risk_record_id);

CREATE TABLE IF NOT EXISTS response_centers (
    center_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    timezone TEXT NOT NULL,
    capacity_units TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS road_corridors (
    corridor_id TEXT PRIMARY KEY,
    origin_center_id TEXT NOT NULL REFERENCES response_centers(center_id),
    destination_center_id TEXT NOT NULL REFERENCES response_centers(center_id),
    response_resource_kind TEXT NOT NULL,
    hourly_capacity TEXT NOT NULL,
    delay_basis_points INTEGER NOT NULL,
    response_minutes INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','suspended','retired')),
    created_at TEXT NOT NULL,
    CHECK(origin_center_id <> destination_center_id)
);

CREATE TABLE IF NOT EXISTS corridor_restrictions (
    restriction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    corridor_id TEXT NOT NULL REFERENCES road_corridors(corridor_id),
    starts_at TEXT NOT NULL,
    ends_at TEXT,
    capacity_percent TEXT NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'announced' CHECK(state IN ('announced','active','closed','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES traffic_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outages_route_time
ON corridor_restrictions(corridor_id, starts_at, ends_at);

CREATE TABLE IF NOT EXISTS response_resource_lots (
    response_resource_lot_id TEXT PRIMARY KEY,
    center_id TEXT NOT NULL REFERENCES response_centers(center_id),
    response_resource_kind TEXT NOT NULL,
    grade TEXT NOT NULL,
    quantity_units TEXT NOT NULL,
    available_units TEXT NOT NULL,
    unit_cost_cny TEXT NOT NULL,
    received_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES traffic_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_inventory_available
ON response_resource_lots(center_id, response_resource_kind, received_at);

CREATE TABLE IF NOT EXISTS response_resource_adjustments (
    adjustment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    response_resource_lot_id TEXT NOT NULL REFERENCES response_resource_lots(response_resource_lot_id),
    delta_units TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    note TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL REFERENCES traffic_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dispatch_requests (
    dispatch_id TEXT PRIMARY KEY,
    corridor_id TEXT NOT NULL REFERENCES road_corridors(corridor_id),
    incident_id TEXT NOT NULL,
    duty_date TEXT NOT NULL,
    requested_units TEXT NOT NULL,
    allocated_units TEXT NOT NULL DEFAULT '0',
    arrived_units TEXT NOT NULL DEFAULT '0',
    priority INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'submitted'
        CHECK(state IN ('submitted','allocated','in_transit','delivered','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1,
    idempotency_key TEXT NOT NULL UNIQUE,
    submitted_by TEXT NOT NULL REFERENCES traffic_users(user_id),
    submitted_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dispatch_requests_schedule
ON dispatch_requests(corridor_id, duty_date, priority, submitted_at);

CREATE TABLE IF NOT EXISTS dispatch_plans (
    plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
    corridor_id TEXT NOT NULL REFERENCES road_corridors(corridor_id),
    duty_date TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    available_units TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES traffic_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(corridor_id, duty_date, input_sha256)
);

CREATE TABLE IF NOT EXISTS deployments (
    deployment_id TEXT PRIMARY KEY,
    dispatch_id TEXT NOT NULL UNIQUE REFERENCES dispatch_requests(dispatch_id),
    inventory_response_resource_lot_id TEXT NOT NULL REFERENCES response_resource_lots(response_resource_lot_id),
    deployed_units TEXT NOT NULL,
    expected_arrived_units TEXT NOT NULL,
    departed_at TEXT NOT NULL,
    arrived_at TEXT,
    state TEXT NOT NULL DEFAULT 'in_transit' CHECK(state IN ('in_transit','delivered','disputed')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES traffic_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS response_scenarios (
    scenario_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','approved','retired')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES traffic_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS response_scenario_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id TEXT NOT NULL REFERENCES response_scenarios(scenario_id),
    as_of_date TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES traffic_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(scenario_id, as_of_date, input_sha256)
);

-- 快速结算：已生效的责任认定是结算单的唯一前置
CREATE TABLE IF NOT EXISTS liability_determinations (
    determination_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    shares_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'effective' CHECK(state IN ('effective','revoked')),
    revision INTEGER NOT NULL DEFAULT 1,
    decided_by TEXT NOT NULL REFERENCES traffic_users(user_id),
    decided_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_liability_incident
ON liability_determinations(incident_id, state);

CREATE TABLE IF NOT EXISTS quick_settlements (
    settlement_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    determination_id TEXT NOT NULL REFERENCES liability_determinations(determination_id),
    fixed_items_json TEXT NOT NULL DEFAULT '[]',
    deductibles_json TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL DEFAULT 'open'
        CHECK(state IN ('open','partially_paid','paid','settled','closed','refused','void')),
    quote_version INTEGER NOT NULL DEFAULT 0,
    total_amount TEXT NOT NULL DEFAULT '0.00',
    total_deductible TEXT NOT NULL DEFAULT '0.00',
    total_payable TEXT NOT NULL DEFAULT '0.00',
    amount_paid TEXT NOT NULL DEFAULT '0.00',
    amount_refunded TEXT NOT NULL DEFAULT '0.00',
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES traffic_users(user_id),
    created_at TEXT NOT NULL,
    settled_at TEXT,
    closed_at TEXT,
    UNIQUE(determination_id)
);

CREATE INDEX IF NOT EXISTS idx_settlements_incident
ON quick_settlements(incident_id, state);

CREATE TABLE IF NOT EXISTS repair_quote_versions (
    quote_version INTEGER NOT NULL,
    settlement_id TEXT NOT NULL REFERENCES quick_settlements(settlement_id),
    shop_id TEXT NOT NULL,
    shop_name TEXT NOT NULL,
    quote_ref TEXT NOT NULL,
    items_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','superseded')),
    submitted_by TEXT NOT NULL REFERENCES traffic_users(user_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(settlement_id, quote_version)
);

CREATE TABLE IF NOT EXISTS party_confirmations (
    confirmation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    settlement_id TEXT NOT NULL REFERENCES quick_settlements(settlement_id),
    quote_version INTEGER NOT NULL,
    party_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('confirmed','refused')),
    note TEXT NOT NULL DEFAULT '',
    content_sha256 TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','voided')),
    void_reason TEXT,
    actor_id TEXT NOT NULL REFERENCES traffic_users(user_id),
    created_at TEXT NOT NULL,
    voided_at TEXT,
    FOREIGN KEY(settlement_id, quote_version) REFERENCES repair_quote_versions(settlement_id, quote_version)
);

CREATE INDEX IF NOT EXISTS idx_confirmations_lookup
ON party_confirmations(settlement_id, party_id, quote_version);

CREATE TABLE IF NOT EXISTS settlement_payments (
    payment_id TEXT PRIMARY KEY,
    settlement_id TEXT NOT NULL REFERENCES quick_settlements(settlement_id),
    direction TEXT NOT NULL CHECK(direction IN ('inbound','refund')),
    amount TEXT NOT NULL,
    receipt_no TEXT NOT NULL,
    payer_party_id TEXT,
    method TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    balance_after TEXT NOT NULL,
    registered_by TEXT NOT NULL REFERENCES traffic_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(receipt_no)
);

CREATE INDEX IF NOT EXISTS idx_payments_settlement
ON settlement_payments(settlement_id, created_at, payment_id);

CREATE TABLE IF NOT EXISTS settlement_breakdowns (
    settlement_id TEXT NOT NULL REFERENCES quick_settlements(settlement_id),
    quote_version INTEGER NOT NULL,
    breakdown_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(settlement_id, quote_version)
);

CREATE TABLE IF NOT EXISTS traffic_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS traffic_audit_events (
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

CREATE INDEX IF NOT EXISTS idx_traffic_audit_entity
ON traffic_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10)
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


def row_dict(row: sqlite3.Row | None) -> dict[str, object] | None:
    return None if row is None else dict(row)
