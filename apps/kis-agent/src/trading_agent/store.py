"""Single-host SQLite journal; local and simulated-broker ledgers are independent."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .contracts import AgentError, Wallet, canonical, dec

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS accounts (
  account TEXT NOT NULL, ledger TEXT NOT NULL, cash TEXT NOT NULL,
  initial_cash TEXT NOT NULL, halt_reason TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(account, ledger));
CREATE TABLE IF NOT EXISTS positions (
  account TEXT NOT NULL, ledger TEXT NOT NULL, symbol TEXT NOT NULL,
  quantity INTEGER NOT NULL CHECK(quantity>=0), PRIMARY KEY(account,ledger,symbol));
CREATE TABLE IF NOT EXISTS orders (
  key TEXT PRIMARY KEY, account TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
  quantity INTEGER NOT NULL, limit_price TEXT NOT NULL, state TEXT NOT NULL,
  broker_id TEXT, filled_qty INTEGER NOT NULL DEFAULT 0, intent_json TEXT NOT NULL,
  UNIQUE(account,broker_id));
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, account TEXT NOT NULL, order_key TEXT, event TEXT NOT NULL,
  at TEXT NOT NULL, details TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS remote_orders (
  broker_id TEXT PRIMARY KEY, client_key TEXT NOT NULL, account TEXT NOT NULL,
  symbol TEXT NOT NULL, side TEXT NOT NULL, quantity INTEGER NOT NULL,
  limit_price TEXT NOT NULL, state TEXT NOT NULL, filled_qty INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS remote_fills (
  fill_id TEXT PRIMARY KEY, broker_id TEXT NOT NULL, quantity INTEGER NOT NULL,
  price TEXT NOT NULL, fee TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fills (
  fill_id TEXT PRIMARY KEY, order_key TEXT NOT NULL, quantity INTEGER NOT NULL,
  price TEXT NOT NULL, fee TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS experiments (
  run_id TEXT PRIMARY KEY, report_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evaluation_manifests (
  run_id TEXT PRIMARY KEY, manifest_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS holdouts (
  symbol TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL, run_id TEXT NOT NULL,
  PRIMARY KEY(symbol,start,end));
CREATE TABLE IF NOT EXISTS proposal_requests (
  id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, status TEXT NOT NULL,
  artifact_json TEXT, error_category TEXT);
CREATE TABLE IF NOT EXISTS broker_bindings (
  account TEXT PRIMARY KEY, mode TEXT NOT NULL, identity_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fill_amounts (
  fill_id TEXT NOT NULL, ledger TEXT NOT NULL, notional TEXT NOT NULL,
  PRIMARY KEY(fill_id,ledger));
CREATE TABLE IF NOT EXISTS kis_order_refs (
  broker_id TEXT PRIMARY KEY, session TEXT NOT NULL, orgno TEXT NOT NULL, odno TEXT NOT NULL,
  cancel_requested INTEGER NOT NULL DEFAULT 0, fee_verified INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS releases (
  id TEXT PRIMARY KEY, manifest_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shadow_slots (
  slot TEXT PRIMARY KEY, revision INTEGER NOT NULL, release_id TEXT NOT NULL,
  account TEXT NOT NULL, state TEXT NOT NULL,
  FOREIGN KEY(release_id) REFERENCES releases(id));
CREATE TABLE IF NOT EXISTS shadow_observations (
  id TEXT PRIMARY KEY, slot TEXT NOT NULL, revision INTEGER NOT NULL,
  as_of TEXT NOT NULL, input_hash TEXT NOT NULL, report_json TEXT NOT NULL,
  UNIQUE(slot,revision,as_of));
CREATE TABLE IF NOT EXISTS logic_studies (
  name TEXT PRIMARY KEY, revision INTEGER NOT NULL, settings_json TEXT NOT NULL,
  champion_json TEXT NOT NULL, implementation_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS logic_cycles (
  id TEXT PRIMARY KEY, study TEXT NOT NULL, expected_revision INTEGER NOT NULL,
  status TEXT NOT NULL, input_json TEXT NOT NULL, run_id TEXT, report_json TEXT,
  FOREIGN KEY(study) REFERENCES logic_studies(name));
CREATE UNIQUE INDEX IF NOT EXISTS logic_one_running_cycle ON logic_cycles(study) WHERE status='RUNNING';
"""


class Store:
    def guard_unified_interval(self, start: str, end: str, owner: str) -> None:
        """Respect market-wide reservations created by the unified runtime.

        Kept optional for standalone databases created before that runtime.
        Call again inside the claiming transaction to close the worker race.
        """
        exists = self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_intervals'").fetchone()
        if exists and self.db.execute(
                "SELECT 1 FROM agent_intervals WHERE market='KRX' AND owner<>? AND NOT(end<? OR start>?)",
                (owner, start, end)).fetchone():
            raise AgentError("Holdout overlaps consumed unified research or shadow evidence")

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version','1')")
        version = self.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        if version not in {"1", "2", "3", "4", "5"}:
            raise AgentError("Unsupported database schema version")
        self.db.execute("UPDATE meta SET value='5' WHERE key='schema_version'")

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def create_account(self, account: str, cash) -> None:
        cash = dec(cash)
        if not account or cash <= 0:
            raise AgentError("Account and positive initial cash are required")
        with self.transaction():
            for ledger in ("LOCAL", "REMOTE"):
                row = self.db.execute("SELECT initial_cash FROM accounts WHERE account=? AND ledger=?",
                                      (account, ledger)).fetchone()
                if row and dec(row[0]) != cash:
                    raise AgentError("Existing account initial cash differs; use a new account/database")
                self.db.execute("INSERT OR IGNORE INTO accounts(account,ledger,cash,initial_cash) VALUES(?,?,?,?)",
                                (account, ledger, str(cash), str(cash)))

    def wallet(self, account: str, ledger: str = "LOCAL") -> Wallet:
        row = self.db.execute("SELECT cash FROM accounts WHERE account=? AND ledger=?", (account, ledger)).fetchone()
        if row is None:
            raise AgentError("Unknown account")
        positions = {r[0]: r[1] for r in self.db.execute(
            "SELECT symbol,quantity FROM positions WHERE account=? AND ledger=?", (account, ledger)) if r[1]}
        return Wallet(dec(row[0]), positions)

    def save_wallet(self, account: str, ledger: str, wallet: Wallet) -> None:
        self.db.execute("UPDATE accounts SET cash=? WHERE account=? AND ledger=?", (str(wallet.cash), account, ledger))
        self.db.execute("DELETE FROM positions WHERE account=? AND ledger=?", (account, ledger))
        self.db.executemany("INSERT INTO positions VALUES(?,?,?,?)",
                            [(account, ledger, s, q) for s, q in wallet.positions.items() if q])

    def halt_reason(self, account: str) -> str:
        row = self.db.execute("SELECT halt_reason FROM accounts WHERE account=? AND ledger='LOCAL'", (account,)).fetchone()
        if row is None:
            raise AgentError("Unknown account")
        return row[0]

    def halt(self, account: str, reason: str) -> None:
        self.db.execute("UPDATE accounts SET halt_reason=? WHERE account=? AND ledger='LOCAL'", (reason, account))

    def event(self, account: str, key: str | None, name: str, details=None) -> None:
        self.db.execute("INSERT INTO events(account,order_key,event,at,details) VALUES(?,?,?,?,?)",
                        (account, key, name, datetime.now(timezone.utc).isoformat(), canonical(details or {})))

    def order(self, key: str) -> dict:
        row = self.db.execute("SELECT * FROM orders WHERE key=?", (key,)).fetchone()
        if row is None:
            raise AgentError("Unknown local order")
        return dict(row)

    def status(self, account: str) -> dict:
        binding = self.db.execute("SELECT mode FROM broker_bindings WHERE account=?", (account,)).fetchone()
        result = {"account": account, "mode": binding[0] if binding else "LOCAL_SIMULATED_PAPER", "wallet": self.wallet(account),
                "halt_reason": self.halt_reason(account),
                "orders": {r[0]: r[1] for r in self.db.execute(
                    "SELECT state,count(*) FROM orders WHERE account=? GROUP BY state", (account,))},
                "fills": self.db.execute("SELECT count(*) FROM fills f JOIN orders o ON f.order_key=o.key WHERE o.account=?", (account,)).fetchone()[0],
                "experiments": self.db.execute("SELECT count(*) FROM experiments").fetchone()[0]}
        if result["mode"] == "KIS_PAPER":
            result["cash_basis"] = "prvs_rcdl_excc_amt: expected settlement cash, not buying power"
        return result

    def bind_broker(self, account: str, mode: str, identity_hash: str) -> None:
        previous = self.db.execute("SELECT mode,identity_hash FROM broker_bindings WHERE account=?", (account,)).fetchone()
        if previous and tuple(previous) != (mode, identity_hash):
            raise AgentError("Account is bound to a different broker or credential identity")
        if mode == "KIS_PAPER" and self.db.execute("SELECT 1 FROM broker_bindings WHERE mode=? AND identity_hash=? AND account<>?", (mode, identity_hash, account)).fetchone():
            raise AgentError("This paper identity already has an account alias in this database")
        self.db.execute("INSERT OR IGNORE INTO broker_bindings VALUES(?,?,?)", (account, mode, identity_hash))
