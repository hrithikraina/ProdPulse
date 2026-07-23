"""Tests for the log-context expansion node wired into agents/rca_graph.py.

agents/rca_graph.py constructs an AzureChatOpenAI client (no network call) at import time,
which requires AZURE_OPENAI_ENDPOINT/AZURE_OPENAI_API_KEY to be set. setdefault() only fills
them in when missing, so this doesn't override real credentials when present (e.g. via .env
in local dev) — it just keeps this test importable in CI without live Azure config.
"""

import os

os.environ.setdefault("AZURE_OPENAI_ENDPOINT", "https://placeholder.openai.azure.com")
os.environ.setdefault("AZURE_OPENAI_API_KEY", "placeholder")

from datetime import datetime

from agents.rca_graph import make_log_context_node
from repositories.log_repository import LogEntry

ARCHITECTURE = {
    "components": {
        "processing": {
            "repositories": [
                {"name": "ledger-posting", "logging": {"source": "ledger-posting-service"}},
                {"name": "transaction-validation-service", "logging": {"source": "transaction-validation-service"}},
            ]
        }
    }
}

ANCHOR_LOGS = (
    "2026-07-17T09:01:15+00:00 INFO ledger-posting-service ledger-441 Posting double-entry transaction payment_id=PAY-90021\n"
    "2026-07-17T09:01:15+00:00 ERROR ledger-posting-service ledger-441 DatabaseConnectionPoolExhaustedException: "
    "connection pool exhausted: active=2, max=2 payment_id=PAY-90021"
)


class FakeLogRepository:
    def __init__(self, entries: list[LogEntry]) -> None:
        self._entries = entries

    def fetch(self, source: str, start: datetime, end: datetime) -> list[LogEntry]:
        return [entry for entry in self._entries if entry.service == source and start <= entry.timestamp <= end]


class RaisingLogRepository:
    def fetch(self, source: str, start: datetime, end: datetime) -> list[LogEntry]:
        raise RuntimeError("backend unavailable")


def _entry(time_str: str, service: str = "ledger-posting-service", message: str = "note") -> LogEntry:
    timestamp = datetime.fromisoformat(f"2026-07-17T{time_str}+00:00")
    return LogEntry(timestamp=timestamp, service=service, level="INFO", message=message, raw=f"{timestamp.isoformat()} INFO {service} {message}")


def test_expand_log_context_replaces_error_logs_with_tagged_correlated_text() -> None:
    entries = [
        _entry("09:01:00", message="request accepted"),
        _entry("09:01:13", service="transaction-validation-service", message="Evaluating risk rules payment_id=PAY-90021"),
    ]
    node = make_log_context_node(FakeLogRepository(entries))
    state = {"error_logs": ANCHOR_LOGS, "architecture": ARCHITECTURE}

    result = node(state)

    assert "error_logs" in result
    assert "[ledger-posting-service]" in result["error_logs"]
    assert "[transaction-validation-service]" in result["error_logs"]
    assert "PAY-90021" in result["error_logs"]


def test_expand_log_context_leaves_state_unchanged_when_nothing_found() -> None:
    node = make_log_context_node(FakeLogRepository([]))
    state = {"error_logs": ANCHOR_LOGS, "architecture": ARCHITECTURE}

    assert node(state) == {}


def test_expand_log_context_leaves_state_unchanged_for_empty_logs() -> None:
    node = make_log_context_node(FakeLogRepository([]))
    state = {"error_logs": "", "architecture": ARCHITECTURE}

    assert node(state) == {}


def test_expand_log_context_degrades_gracefully_when_backend_raises() -> None:
    node = make_log_context_node(RaisingLogRepository())
    state = {"error_logs": ANCHOR_LOGS, "architecture": ARCHITECTURE}

    assert node(state) == {}
