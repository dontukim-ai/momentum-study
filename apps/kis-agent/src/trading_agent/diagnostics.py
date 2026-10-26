"""Secret-free readiness and opt-in, read-only real/paper contract probes."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from .contracts import KST, UTC, AgentError, implementation_id, kis_account
from .kis import http_json
from .kis_paper import PAPER_HOST, ROUTES, KisPaperBroker, PaperSession
from .kis_real import (
    READ_ROUTES,
    REAL_CAPITAL_LIMIT,
    REAL_HOST,
    REAL_NAMES,
    KisRealReadOnly,
)
from .store import Store

PAPER_NAMES = ("KIS_PAPER_APP_KEY", "KIS_PAPER_APP_SECRET", "KIS_PAPER_ACCOUNT")


def host_access(host: str, policy: dict | None) -> str:
    """Evaluate known managed policy only; lack of a policy is not proof of access."""
    if not isinstance(policy, dict):
        return "UNKNOWN"
    network = policy.get("http_network_policy", {})
    if network.get("type") == "unrestricted":
        return "POLICY_ALLOWS"
    if network.get("type") != "restricted":
        return "UNKNOWN"
    for rule in network.get("egress_rules", []):
        pattern = rule.get("host", "") if isinstance(rule, dict) else ""
        if pattern == host or (pattern.startswith("*.") and host.endswith(pattern[1:])):
            return "POLICY_ALLOWS"
    return "POLICY_BLOCKS"


def load_network_policy(path: Path = Path("/etc/codex/network-policy.json")) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def doctor(*, probe_paper: bool = False, probe_real: bool = False, symbol: str | None = None,
           policy: dict | None = None, transport=http_json,
           token_cache_dir: Path | None = None) -> dict:
    """No keys, tokens, account numbers, balances or raw errors enter the report.

    An explicit probe permits OAuth token issuance and quote/balance/order GETs.
    It never initializes an account or invokes an order-writing operation.
    Policy allowance and credential presence alone never mark a service verified.
    """
    if probe_paper and probe_real:
        raise AgentError("Select either the real or paper read probe")
    if (probe_paper or probe_real) and (not isinstance(symbol, str) or not re.fullmatch(r"[0-9]{6}", symbol)):
        raise AgentError("A six-digit --symbol is required for a read probe")
    policy = load_network_policy() if policy is None else policy
    present = {name: bool(os.environ.get(name)) for name in (*PAPER_NAMES, *REAL_NAMES, "OPENAI_API_KEY", "OPENAI_MODEL")}
    account_valid = bool(kis_account(os.environ.get("KIS_PAPER_ACCOUNT", "")))
    paper_access = host_access("openapivts.koreainvestment.com", policy)
    llm_access = host_access("api.openai.com", policy)
    paper_configured = all(present[name] for name in PAPER_NAMES) and account_valid
    report = {"mode": "READINESS_DIAGNOSTIC", "observed_at": datetime.now(UTC).isoformat(),
              "implementation_id": implementation_id(), "credentials_present": present,
              "paper_account_format_valid": account_valid,
              "paper": {"configured": paper_configured, "network_policy": paper_access,
                        "probe": "NOT_REQUESTED", "read_probe_verified": False,
                        "full_order_contract_verified": False},
              "real": {"configured": all(present[name] for name in REAL_NAMES) and bool(kis_account(os.environ.get("KIS_ACCOUNT", ""))),
                       "network_policy": host_access("openapi.koreainvestment.com", policy),
                       "mode": "REAL_READ_ONLY", "capital_limit_krw": str(REAL_CAPITAL_LIMIT),
                       "orders_enabled": False, "probe": "NOT_REQUESTED", "read_probe_verified": False,
                       "full_order_contract_verified": False},
              "llm": {"configured": present["OPENAI_API_KEY"] and present["OPENAI_MODEL"],
                      "network_policy": llm_access, "service_verified": False},
              "http_requests": 0, "order_write_requests": 0,
              "limitations": ["Presence and policy allowance do not verify credentials or service access",
                              "Real/paper fees, cancellation, exchange calendar and order acceptance remain unverified",
                              "Quote timestamps reflect receipt, not a verified exchange event time"]}
    if probe_real:
        return _probe_real(report, symbol, transport, token_cache_dir)
    if not probe_paper:
        return report
    if not paper_configured or paper_access == "POLICY_BLOCKS":
        report["paper"]["probe"] = "BLOCKED_CONFIGURATION_OR_POLICY"
        return report

    def read_only_transport(method, url, headers, body):
        parsed = urlparse(url)
        allowed = ((method == "POST" and parsed.path == "/oauth2/tokenP") or
                   (method == "GET" and parsed.path in {ROUTES[op][1] for op in ("quote", "balance", "orders")}))
        if parsed.scheme != "https" or parsed.netloc != urlparse(PAPER_HOST).netloc or not allowed:
            raise AgentError("Diagnostic transport rejected a non-read-only paper request")
        report["http_requests"] += 1
        return transport(method, url, headers, body)

    store = Store(":memory:")
    checks = {}
    try:
        session = PaperSession(transport=read_only_transport, token_cache_dir=token_cache_dir)
        broker = KisPaperBroker(store, session)
        checks_to_run = {"quote": lambda: broker.quote(symbol), "balance": broker._balance,
                         "recent_orders": lambda: broker.orders(datetime.now(KST).date())}
        for name, action in checks_to_run.items():
            try:
                action()
            except Exception:
                # Arbitrary provider errors may contain secrets or private account data.
                checks[name] = "FAILED_CONTRACT_OR_CONNECTION"
                break  # No token retry loop after failed authentication/connection.
            else:
                checks[name] = "PASSED"
        passed = len(checks) == 3 and all(v == "PASSED" for v in checks.values())
        actual = transport is http_json
        report["paper"].update(probe="PASSED" if passed else "FAILED", checks=checks,
                               evidence_source="SERVICE_RESPONSE" if actual else "INJECTED_TRANSPORT",
                               read_probe_verified=passed and actual)
    finally:
        store.close()
    return report


def _probe_real(report, symbol, transport, token_cache_dir):
    if not report["real"]["configured"] or report["real"]["network_policy"] == "POLICY_BLOCKS":
        report["real"]["probe"] = "BLOCKED_CONFIGURATION_OR_POLICY"
        return report

    def read_only_transport(method, url, headers, body):
        parsed = urlparse(url)
        allowed = ((method == "POST" and parsed.path == "/oauth2/tokenP") or
                   (method == "GET" and parsed.path in {route[0] for route in READ_ROUTES.values()}))
        if parsed.scheme != "https" or parsed.netloc != urlparse(REAL_HOST).netloc or not allowed:
            raise AgentError("Diagnostic transport rejected a non-read-only real request")
        report["http_requests"] += 1
        return transport(method, url, headers, body)

    client = KisRealReadOnly(transport=read_only_transport, token_cache_dir=token_cache_dir)
    checks = {}
    for name, action in {"quote": lambda: client.quote(symbol), "balance": client.balance,
                         "recent_orders": lambda: client.orders(datetime.now(KST).date())}.items():
        try:
            action()
        except Exception:
            checks[name] = "FAILED_CONTRACT_OR_CONNECTION"
            break
        else:
            checks[name] = "PASSED"
    passed = len(checks) == 3 and all(value == "PASSED" for value in checks.values())
    report["real"].update(probe="PASSED" if passed else "FAILED", checks=checks,
                           read_probe_verified=passed and transport is http_json,
                           evidence_source="SERVICE_RESPONSE" if transport is http_json else "INJECTED_TRANSPORT")
    return report
