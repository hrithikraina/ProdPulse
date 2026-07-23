import json
from datetime import datetime, timedelta
from pathlib import Path

from agents.log_context import (
    CorrelatedSource,
    LogContextAgent,
    extract_anchor_timestamp,
    extract_correlation_value,
    fetch_log_context,
    find_correlated_neighbors,
    resolve_log_source,
    sibling_sources,
)
from domain.models import Incident
from repositories.log_repository import (
    FallbackLogRepository,
    JsonLogRepository,
    LogEntry,
    LokiLogRepository,
    ShiftedLokiLogRepository,
    build_log_repository,
    read_seed_shift,
    write_seed_shift,
)

ARCHITECTURE = {
    "components": {
        "processing": {
            "repositories": [
                {"name": "ledger-posting", "logging": {"source": "ledger-posting-service"}},
                {"name": "transaction-validation-service", "logging": {"source": "transaction-validation-service"}},
            ]
        },
        "data": {
            "repositories": [
                {"name": "account-query", "logging": {"source": "account-query-service"}},
            ]
        },
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


def _entry(time_str: str, service: str = "ledger-posting-service", message: str = "note") -> LogEntry:
    timestamp = datetime.fromisoformat(f"2026-07-17T{time_str}+00:00")
    return LogEntry(timestamp=timestamp, service=service, level="INFO", message=message, raw=f"{timestamp.isoformat()} INFO {service} {message}")


def test_resolve_log_source_matches_configured_repository() -> None:
    assert resolve_log_source(ANCHOR_LOGS, None, ARCHITECTURE) == "ledger-posting-service"


def test_resolve_log_source_falls_back_to_parsed_service_without_config() -> None:
    logs = "2026-07-17T09:01:15+00:00 ERROR unknown-service trace-1 Something broke"
    assert resolve_log_source(logs, None, ARCHITECTURE) == "unknown-service"


def test_resolve_log_source_returns_none_without_any_service_signal() -> None:
    assert resolve_log_source("not a structured log line", None, ARCHITECTURE) is None


def test_resolve_log_source_matches_repo_name_embedded_in_a_wrapped_context() -> None:
    # agents/code_investigation.py and services/analysis_sessions.py wrap the raw log line
    # inside "Service: X\nSymptoms: Y\nLogs: <line>" before it reaches run_rca() — the repo
    # name is still present in the text even though no single line matches the strict
    # <timestamp> <LEVEL> <service> <trace-id> <message> format.
    wrapped = (
        "Service: ledger-posting-service\n"
        "Symptoms: Payments fail to post\n"
        "Logs: 2026-07-17T09:01:15Z ERROR ledger-posting-service ledger-441 pool exhausted"
    )
    assert resolve_log_source(wrapped, None, ARCHITECTURE) == "ledger-posting-service"


def test_extract_anchor_timestamp_parses_last_valid_timestamp() -> None:
    assert extract_anchor_timestamp(ANCHOR_LOGS, None) == datetime.fromisoformat("2026-07-17T09:01:15+00:00")


def test_extract_anchor_timestamp_finds_timestamp_embedded_in_a_wrapped_context() -> None:
    wrapped = (
        "Service: ledger-posting-service\n"
        "Symptoms: Payments fail to post\n"
        "Logs: 2026-07-17T09:01:15Z ERROR ledger-posting-service ledger-441 pool exhausted"
    )
    assert extract_anchor_timestamp(wrapped, None) == datetime.fromisoformat("2026-07-17T09:01:15+00:00")


def test_fetch_log_context_resolves_a_wrapped_service_context_end_to_end() -> None:
    entries = [_entry("09:01:00", message="request accepted")]
    wrapped = (
        "Service: ledger-posting-service\n"
        "Symptoms: Payments fail to post\n"
        "Logs: 2026-07-17T09:01:15Z ERROR ledger-posting-service ledger-441 pool exhausted"
    )
    result = fetch_log_context(wrapped, FakeLogRepository(entries), ARCHITECTURE)
    assert result.status == "LOG_CONTEXT_FOUND"
    assert result.source == "ledger-posting-service"


def test_extract_anchor_timestamp_prefers_explicit_override() -> None:
    override = datetime.fromisoformat("2026-01-01T00:00:00+00:00")
    assert extract_anchor_timestamp(ANCHOR_LOGS, override) == override


def test_extract_anchor_timestamp_returns_none_when_missing() -> None:
    assert extract_anchor_timestamp("no timestamp here", None) is None


def test_fetch_log_context_returns_no_logs_supplied_when_empty() -> None:
    result = fetch_log_context("", FakeLogRepository([]), ARCHITECTURE)
    assert result.status == "NO_LOGS_SUPPLIED"


def test_fetch_log_context_returns_no_log_source_matched() -> None:
    result = fetch_log_context("not a structured log line", FakeLogRepository([]), ARCHITECTURE)
    assert result.status == "NO_LOG_SOURCE_MATCHED"


def test_fetch_log_context_returns_no_timestamp_found() -> None:
    # Structured enough to resolve a log source, but the timestamp field itself is malformed.
    logs = "not-a-timestamp INFO ledger-posting-service ledger-441 something broke"
    result = fetch_log_context(logs, FakeLogRepository([]), ARCHITECTURE)
    assert result.status == "NO_TIMESTAMP_FOUND"


def test_fetch_log_context_returns_only_in_window_matches_for_the_resolved_service() -> None:
    entries = [
        _entry("08:50:00", message="too early"),
        _entry("08:58:00", message="pool warming up"),
        _entry("08:59:00", message="pool at capacity"),
        _entry("09:01:00", message="request accepted"),
        _entry("09:03:00", message="retry attempted"),
        _entry("09:10:00", message="too late"),
        _entry("09:01:00", service="transaction-validation-service", message="unrelated service"),
    ]
    result = fetch_log_context(ANCHOR_LOGS, FakeLogRepository(entries), ARCHITECTURE, window=timedelta(minutes=5))
    assert result.status == "LOG_CONTEXT_FOUND"
    assert len(result.matched_lines) == 4
    assert not any("too early" in line or "too late" in line or "unrelated service" in line for line in result.matched_lines)


def test_extract_correlation_value_prefers_id_suffixed_field() -> None:
    logs = "connection pool exhausted: active=2, max=2 payment_id=PAY-90021"
    assert extract_correlation_value(logs) == "PAY-90021"


def test_extract_correlation_value_falls_back_to_any_key_value_pair() -> None:
    assert extract_correlation_value("region=EU") == "EU"


def test_extract_correlation_value_returns_none_without_any_key_value_pairs() -> None:
    assert extract_correlation_value("no identifiers here") is None


def test_sibling_sources_returns_other_repos_in_same_component() -> None:
    assert sibling_sources("ledger-posting-service", ARCHITECTURE) == ["transaction-validation-service"]


def test_sibling_sources_excludes_a_different_component() -> None:
    # account-query-service is configured under "data", a different component entirely.
    assert "account-query-service" not in sibling_sources("ledger-posting-service", ARCHITECTURE)


def test_sibling_sources_returns_empty_for_unknown_source() -> None:
    assert sibling_sources("unknown-service", ARCHITECTURE) == []


def test_find_correlated_neighbors_skipped_without_correlation_key() -> None:
    key, results = find_correlated_neighbors(
        "no identifiers here", "ledger-posting-service", FakeLogRepository([]), ARCHITECTURE,
        datetime.fromisoformat("2026-07-17T09:00:00+00:00"), datetime.fromisoformat("2026-07-17T09:10:00+00:00"),
    )
    assert key is None
    assert results == []


def test_find_correlated_neighbors_marks_match_when_value_present_in_sibling_window() -> None:
    entries = [_entry("09:01:13", service="transaction-validation-service", message="Evaluating risk rules payment_id=PAY-90021")]
    key, results = find_correlated_neighbors(
        ANCHOR_LOGS, "ledger-posting-service", FakeLogRepository(entries), ARCHITECTURE,
        datetime.fromisoformat("2026-07-17T08:56:15+00:00"), datetime.fromisoformat("2026-07-17T09:06:15+00:00"),
    )
    assert key == "PAY-90021"
    assert len(results) == 1
    assert results[0].source == "transaction-validation-service"
    assert results[0].status == "CORRELATED_MATCH"
    assert "PAY-90021" in results[0].matched_lines[0]


def test_find_correlated_neighbors_marks_no_matching_entries_when_absent() -> None:
    entries = [_entry("09:01:13", service="transaction-validation-service", message="Validating payment payment_id=PAY-90099")]
    key, results = find_correlated_neighbors(
        ANCHOR_LOGS, "ledger-posting-service", FakeLogRepository(entries), ARCHITECTURE,
        datetime.fromisoformat("2026-07-17T08:56:15+00:00"), datetime.fromisoformat("2026-07-17T09:06:15+00:00"),
    )
    assert key == "PAY-90021"
    assert results == [CorrelatedSource("transaction-validation-service", "NO_MATCHING_ENTRIES", [])]


def test_fetch_log_context_includes_cross_service_correlation() -> None:
    entries = [
        _entry("09:01:00", message="request accepted"),
        _entry("09:01:13", service="transaction-validation-service", message="Evaluating risk rules payment_id=PAY-90021"),
    ]
    result = fetch_log_context(ANCHOR_LOGS, FakeLogRepository(entries), ARCHITECTURE)
    assert result.correlation_key == "PAY-90021"
    assert [c.source for c in result.correlated_sources] == ["transaction-validation-service"]
    assert result.correlated_sources[0].status == "CORRELATED_MATCH"


def test_log_context_agent_investigate_wraps_finding() -> None:
    entries = [_entry("09:01:00", message="request accepted")]
    agent = LogContextAgent(FakeLogRepository(entries), ARCHITECTURE)
    incident = Incident(id="INC-NEW-BNK-02", title="t", service="ledger-posting-service", severity="SEV-1", symptoms="s", logs=ANCHOR_LOGS)
    finding = agent.investigate(incident)
    assert finding.agent_name == "LogContextAgent"
    assert finding.status == "LOG_CONTEXT_FOUND"
    assert "request accepted" in finding.evidence
    assert "[ledger-posting-service]" in finding.evidence


def test_log_context_agent_tags_and_summarizes_correlated_evidence() -> None:
    entries = [
        _entry("09:01:00", message="request accepted"),
        _entry("09:01:13", service="transaction-validation-service", message="Evaluating risk rules payment_id=PAY-90021"),
    ]
    agent = LogContextAgent(FakeLogRepository(entries), ARCHITECTURE)
    incident = Incident(id="INC-NEW-BNK-02", title="t", service="ledger-posting-service", severity="SEV-1", symptoms="s", logs=ANCHOR_LOGS)
    finding = agent.investigate(incident)
    assert "[transaction-validation-service]" in finding.evidence
    assert "PAY-90021" in finding.summary
    assert "transaction-validation-service" in finding.summary


def test_log_context_agent_reports_missing_timestamp_without_crashing() -> None:
    agent = LogContextAgent(FakeLogRepository([]), ARCHITECTURE)
    incident = Incident(id="INC-X", title="t", service="ledger-posting-service", severity="SEV-1", symptoms="s", logs="ledger-posting-service saw an error")
    finding = agent.investigate(incident)
    assert finding.status == "NO_TIMESTAMP_FOUND"


def test_json_log_repository_filters_by_service_and_window(tmp_path) -> None:
    records = [
        {"service": "ledger-posting-service", "timestamp": "2026-07-17T08:58:00+00:00", "level": "INFO", "message": "in window"},
        {"service": "ledger-posting-service", "timestamp": "2026-07-17T09:20:00+00:00", "level": "INFO", "message": "outside window"},
        {"service": "transaction-validation-service", "timestamp": "2026-07-17T09:00:00+00:00", "level": "INFO", "message": "different service"},
    ]
    (tmp_path / "simulated-logs.json").write_text(json.dumps(records), encoding="utf-8")
    repository = JsonLogRepository(tmp_path)
    entries = repository.fetch(
        "ledger-posting-service",
        datetime.fromisoformat("2026-07-17T08:56:15+00:00"),
        datetime.fromisoformat("2026-07-17T09:06:15+00:00"),
    )
    assert [entry.message for entry in entries] == ["in window"]


def test_json_log_repository_includes_window_boundaries(tmp_path) -> None:
    records = [{"service": "svc", "timestamp": "2026-07-17T09:00:00+00:00", "level": "INFO", "message": "on boundary"}]
    (tmp_path / "simulated-logs.json").write_text(json.dumps(records), encoding="utf-8")
    repository = JsonLogRepository(tmp_path)
    boundary = datetime.fromisoformat("2026-07-17T09:00:00+00:00")
    entries = repository.fetch("svc", boundary, boundary)
    assert len(entries) == 1


def test_json_log_repository_raises_on_missing_fixture(tmp_path) -> None:
    repository = JsonLogRepository(tmp_path)
    try:
        repository.fetch("svc", datetime.now().astimezone(), datetime.now().astimezone())
    except RuntimeError as error:
        assert "could not be read" in str(error)
    else:
        raise AssertionError("Expected RuntimeError for a missing fixture file.")


def test_loki_log_repository_has_no_auth_without_credentials() -> None:
    repository = LokiLogRepository("https://localhost:3100")
    assert repository._auth is None


def test_loki_log_repository_uses_basic_auth_with_instance_id_and_token() -> None:
    repository = LokiLogRepository("https://logs-prod-1.grafana.net", instance_id="123", api_token="secret-token")
    assert repository._auth == ("123", "secret-token")


def test_loki_log_repository_builds_logql_query_and_nanosecond_time_range() -> None:
    repository = LokiLogRepository("https://localhost:3100")
    captured = {}

    def fake_request(params):
        captured.update(params)
        return {"data": {"result": []}}

    repository._request = fake_request  # type: ignore[method-assign]
    start = datetime.fromisoformat("2026-07-17T08:56:15+00:00")
    end = datetime.fromisoformat("2026-07-17T09:06:15+00:00")
    repository.fetch("ledger-posting-service", start, end)

    assert captured["query"] == '{service="ledger-posting-service"}'
    assert captured["start"] == str(int(start.timestamp() * 1_000_000_000))
    assert captured["end"] == str(int(end.timestamp() * 1_000_000_000))
    assert captured["direction"] == "forward"


def test_loki_log_repository_parses_streams_into_sorted_log_entries() -> None:
    repository = LokiLogRepository("https://localhost:3100")

    def fake_request(_params):
        return {
            "data": {
                "result": [
                    {
                        "stream": {"service": "ledger-posting-service"},
                        "values": [
                            ["1784368875000000000", "2026-07-17T09:01:15+00:00 ERROR ledger-posting-service pool exhausted"],
                            ["1784368858000000000", "2026-07-17T08:59:30+00:00 INFO ledger-posting-service pool initialized max=2"],
                        ],
                    }
                ]
            }
        }

    repository._request = fake_request  # type: ignore[method-assign]
    entries = repository.fetch(
        "ledger-posting-service",
        datetime.fromisoformat("2026-07-17T08:56:15+00:00"),
        datetime.fromisoformat("2026-07-17T09:06:15+00:00"),
    )
    assert [entry.level for entry in entries] == ["INFO", "ERROR"]
    assert [entry.message for entry in entries] == ["pool initialized max=2", "pool exhausted"]
    assert entries[0].timestamp < entries[1].timestamp


def test_loki_log_repository_raises_on_invalid_response_shape() -> None:
    repository = LokiLogRepository("https://localhost:3100")
    repository._request = lambda _params: {"data": {"result": "not-a-list"}}  # type: ignore[method-assign]
    try:
        repository.fetch("ledger-posting-service", datetime.now().astimezone(), datetime.now().astimezone())
    except RuntimeError as error:
        assert "invalid" in str(error)
    else:
        raise AssertionError("Expected RuntimeError for a malformed Loki response.")


class RaisingLogRepository:
    def fetch(self, source: str, start: datetime, end: datetime) -> list[LogEntry]:
        raise RuntimeError("primary backend unavailable")


def test_fallback_log_repository_uses_primary_when_it_succeeds() -> None:
    primary_entries = [_entry("09:01:00", message="from primary")]
    repository = FallbackLogRepository(FakeLogRepository(primary_entries), FakeLogRepository([]))
    entries = repository.fetch(
        "ledger-posting-service",
        datetime.fromisoformat("2026-07-17T08:56:15+00:00"),
        datetime.fromisoformat("2026-07-17T09:06:15+00:00"),
    )
    assert [entry.message for entry in entries] == ["from primary"]


def test_fallback_log_repository_falls_back_when_primary_raises() -> None:
    fallback_entries = [_entry("09:01:00", message="from fallback")]
    repository = FallbackLogRepository(RaisingLogRepository(), FakeLogRepository(fallback_entries))
    entries = repository.fetch(
        "ledger-posting-service",
        datetime.fromisoformat("2026-07-17T08:56:15+00:00"),
        datetime.fromisoformat("2026-07-17T09:06:15+00:00"),
    )
    assert [entry.message for entry in entries] == ["from fallback"]


def test_build_log_repository_defaults_to_json(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("LOG_BACKEND", raising=False)
    repository = build_log_repository(tmp_path)
    assert isinstance(repository, JsonLogRepository)


def test_build_log_repository_wraps_loki_with_persisted_shift_and_local_fallback(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LOG_BACKEND", "loki")
    monkeypatch.setenv("LOKI_BASE_URL", "https://logs-prod-1.grafana.net")
    monkeypatch.setenv("LOKI_INSTANCE_ID", "123")
    monkeypatch.setenv("LOKI_API_TOKEN", "secret-token")
    write_seed_shift(tmp_path, timedelta(hours=1))
    repository = build_log_repository(tmp_path)
    assert isinstance(repository, FallbackLogRepository)
    assert isinstance(repository._primary, ShiftedLokiLogRepository)
    assert isinstance(repository._primary._inner, LokiLogRepository)
    assert repository._primary._shift == timedelta(hours=1)
    assert isinstance(repository._fallback, JsonLogRepository)


def test_build_log_repository_skips_shift_wrapper_when_never_seeded(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LOG_BACKEND", "loki")
    monkeypatch.setenv("LOKI_BASE_URL", "https://logs-prod-1.grafana.net")
    monkeypatch.setenv("LOKI_INSTANCE_ID", "123")
    monkeypatch.setenv("LOKI_API_TOKEN", "secret-token")
    # No .loki_seed_state.json in tmp_path — scripts/seed_loki.py was never run here.
    repository = build_log_repository(tmp_path)
    assert isinstance(repository, FallbackLogRepository)
    assert isinstance(repository._primary, LokiLogRepository)


def test_write_and_read_seed_shift_round_trips(tmp_path) -> None:
    write_seed_shift(tmp_path, timedelta(minutes=42, seconds=15))
    assert read_seed_shift(tmp_path) == timedelta(minutes=42, seconds=15)


def test_read_seed_shift_returns_none_when_state_file_missing(tmp_path) -> None:
    assert read_seed_shift(tmp_path) is None


def test_build_log_repository_falls_back_to_local_when_loki_base_url_missing(monkeypatch) -> None:
    monkeypatch.setenv("LOG_BACKEND", "loki")
    monkeypatch.delenv("LOKI_BASE_URL", raising=False)
    repository = build_log_repository(Path("data"))
    assert isinstance(repository, JsonLogRepository)


def test_shifted_loki_log_repository_shifts_query_window_and_unshifts_results() -> None:
    class RecordingLokiRepository:
        def __init__(self) -> None:
            self.received_window: tuple[datetime, datetime] | None = None

        def fetch(self, source: str, start: datetime, end: datetime) -> list[LogEntry]:
            self.received_window = (start, end)
            # Simulate Loki returning an entry at the shifted ("recent") time.
            return [LogEntry(timestamp=start, service=source, level="INFO", message="shifted entry", raw="raw")]

    inner = RecordingLokiRepository()
    shift = timedelta(hours=6, minutes=12)  # an arbitrary, fixed, deterministic shift
    repository = ShiftedLokiLogRepository(inner, shift)

    nominal_start = datetime.fromisoformat("2026-07-23T08:56:15+00:00")
    nominal_end = datetime.fromisoformat("2026-07-23T09:06:15+00:00")
    entries = repository.fetch("ledger-posting-service", nominal_start, nominal_end)

    assert inner.received_window == (nominal_start + shift, nominal_end + shift)
    # The returned entry's timestamp is shifted back to nominal time (round-trips exactly,
    # since fetch() un-shifts by the same amount it shifted the query window by).
    assert entries[0].timestamp == nominal_start
    # raw is rebuilt from the un-shifted timestamp too — evidence text must show the
    # incident's nominal time, not the shifted "now" the backend actually stored it at.
    assert entries[0].raw.startswith(nominal_start.isoformat())
    assert "shifted entry" in entries[0].raw
