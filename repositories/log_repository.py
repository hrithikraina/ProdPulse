"""Adapters that fetch application log lines around a time window for the log-context agent."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import httpx

LOGGER = logging.getLogger(__name__)


def compute_recency_shift(latest_timestamp: datetime, buffer: timedelta = timedelta(minutes=5)) -> timedelta:
    """Offset that maps `latest_timestamp` to `buffer` before the current time.

    Grafana Cloud (and Loki generally) reject log lines beyond a short acceptance window
    relative to real ingestion time — this project's fixtures use a fixed historical date, so
    pushing/testing against a live backend needs every timestamp shifted to "recent" on each
    run, while preserving the relative spacing between events. Apply the same shift to both
    the seeded data and whatever anchor timestamp is used to query it.
    """
    return datetime.now(UTC) - buffer - latest_timestamp


@dataclass(frozen=True, slots=True)
class LogEntry:
    """One parsed application log line."""

    timestamp: datetime
    service: str
    level: str
    message: str
    raw: str


class LogRepository(Protocol):
    def fetch(self, source: str, start: datetime, end: datetime) -> list[LogEntry]: ...


class JsonLogRepository:
    """Local stand-in for a real logging backend (Splunk/Loki), used until one is connected."""

    def __init__(self, data_directory: Path) -> None:
        self._path = data_directory / "simulated-logs.json"

    def fetch(self, source: str, start: datetime, end: datetime) -> list[LogEntry]:
        try:
            with self._path.open(encoding="utf-8") as file:
                records = json.load(file)
        except (FileNotFoundError, json.JSONDecodeError) as error:
            raise RuntimeError("Simulated log data could not be read.") from error

        entries = [
            LogEntry(
                timestamp=datetime.fromisoformat(record["timestamp"]),
                service=record["service"],
                level=record["level"],
                message=record["message"],
                raw=f"{record['timestamp']} {record['level']} {record['service']} {record['message']}",
            )
            for record in records
            if record["service"].casefold() == source.casefold()
        ]
        in_window = [entry for entry in entries if start <= entry.timestamp <= end]
        return sorted(in_window, key=lambda entry: entry.timestamp)


class FallbackLogRepository:
    """Tries a primary LogRepository, falling back to a secondary one if the primary raises.

    Used to keep the log-context agent working off local fixture data even when a live
    backend (Grafana Cloud/Loki) is unreachable, misconfigured, or returns a malformed
    response — any of which surface as a RuntimeError from fetch().
    """

    def __init__(self, primary: "LogRepository", fallback: "LogRepository") -> None:
        self._primary = primary
        self._fallback = fallback

    def fetch(self, source: str, start: datetime, end: datetime) -> list[LogEntry]:
        try:
            return self._primary.fetch(source, start, end)
        except RuntimeError as error:
            LOGGER.warning(
                "Primary log backend failed for source=%s (%s); falling back to local fixture data.",
                source,
                error,
            )
            return self._fallback.fetch(source, start, end)


_SEED_STATE_FILENAME = ".loki_seed_state.json"


def write_seed_shift(data_directory: Path, shift: timedelta) -> None:
    """Persist the exact shift scripts/seed_loki.py applied on its last successful push.

    Recomputing a shift independently at query time (as an earlier version of this code
    did) only stays correct for a few minutes after seeding — it silently assumes "the data
    was seeded right now," which stops being true the moment any real time passes. Reading
    back the shift actually used at push time keeps queries correct for as long as Loki
    itself retains the data (its real constraint, observed at ~1-4h on this project's
    instance), not an artificial few-minute window.
    """
    path = data_directory / _SEED_STATE_FILENAME
    path.write_text(json.dumps({"shift_seconds": shift.total_seconds()}), encoding="utf-8")


def read_seed_shift(data_directory: Path) -> timedelta | None:
    """Return the shift from the last successful seed, or None if never seeded."""
    path = data_directory / _SEED_STATE_FILENAME
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        return timedelta(seconds=state["shift_seconds"])
    except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


class ShiftedLokiLogRepository:
    """Wraps LokiLogRepository, applying a fixed shift so callers can keep querying with a
    fixture's original nominal timestamps regardless of when the data was last (re-)seeded.

    Demo fixtures carry a fixed historical date, but Loki only retains recently-pushed data
    — every seed run shifts the pushed data so its latest entry lands near "now" at push
    time (see compute_recency_shift, scripts/seed_loki.py). `shift` must be the exact value
    used for that push (read via read_seed_shift) — recomputing it independently at query
    time would only be correct in the few minutes right after seeding.
    """

    def __init__(self, inner: "LogRepository", shift: timedelta) -> None:
        self._inner = inner
        self._shift = shift

    def fetch(self, source: str, start: datetime, end: datetime) -> list[LogEntry]:
        entries = self._inner.fetch(source, start + self._shift, end + self._shift)
        # Shift returned timestamps back to nominal time — rebuilding `raw` too, not just
        # `timestamp`, since evidence text (LogContextResult.matched_lines) is built from
        # entry.raw. Leaving raw's embedded timestamp text shifted would show "now" in the
        # evidence while window_start/window_end (built from the nominal query) still show
        # the incident's actual stated time — an inconsistency that misleads the summarizer.
        result = []
        for entry in entries:
            nominal_timestamp = entry.timestamp - self._shift
            result.append(
                LogEntry(
                    timestamp=nominal_timestamp,
                    service=entry.service,
                    level=entry.level,
                    message=entry.message,
                    raw=f"{nominal_timestamp.isoformat()} {entry.level} {entry.service} {entry.message}",
                )
            )
        return result


def build_log_repository(data_directory: Path) -> "LogRepository":
    """Construct the configured LogRepository from LOG_BACKEND/LOKI_* env vars.

    Defaults to JsonLogRepository (data_directory/simulated-logs.json) when LOG_BACKEND is
    unset or "local"; set LOG_BACKEND=loki to query Grafana Cloud/Loki instead. The Loki
    repository is wrapped twice:
      1. ShiftedLokiLogRepository, using the shift persisted by the last successful
         scripts/seed_loki.py run (read_seed_shift) — not a freshly-guessed one — so queries
         built from a fixture's nominal timestamp land on wherever the data actually is, no
         matter how long ago the seed ran (within Loki's own retention). Skipped entirely if
         nothing has been seeded yet; queries then go through unshifted and correctly come
         back NO_CONTEXT_FOUND rather than silently guessing.
      2. FallbackLogRepository, so a Loki outage or misconfiguration (missing LOKI_BASE_URL, a
         failed request, an invalid response) falls back to the local fixture rather than
         losing log-context evidence entirely.
    Shared by the agents/log_context.py CLI runner and the log-context node in
    agents/rca_graph.py so both pick the same backend, shifted the same way.
    """
    local = JsonLogRepository(data_directory)
    if os.getenv("LOG_BACKEND", "local").casefold() != "loki":
        return local

    try:
        loki: LogRepository = LokiLogRepository(
            os.environ["LOKI_BASE_URL"],
            instance_id=os.getenv("LOKI_INSTANCE_ID"),
            api_token=os.getenv("LOKI_API_TOKEN"),
        )
    except KeyError as error:
        LOGGER.warning("LOG_BACKEND=loki but %s is not set; falling back to local fixture data.", error)
        return local

    shift = read_seed_shift(data_directory)
    if shift is not None:
        loki = ShiftedLokiLogRepository(loki, shift)
    else:
        LOGGER.warning(
            "LOG_BACKEND=loki but no seed record was found; run scripts/seed_loki.py first. "
            "Querying unshifted for now."
        )

    return FallbackLogRepository(loki, local)


_RAW_LINE_PATTERN = re.compile(r"^(?P<timestamp>\S+)\s+(?P<level>\S+)\s+(?P<service>\S+)\s+(?P<message>.*)$")


class LokiLogRepository:
    """Queries a Loki-compatible backend for the log-context agent.

    Works against both a self-hosted Loki container and Grafana Cloud's hosted Loki — they
    share the same /loki/api/v1/query_range API and LogQL syntax. The only difference is
    auth: Grafana Cloud requires HTTP Basic Auth (username=instance ID, password=API token);
    leave instance_id/api_token unset for an unauthenticated local container.
    """

    def __init__(
        self,
        base_url: str,
        instance_id: str | None = None,
        api_token: str | None = None,
        service_label: str = "service",
        limit: int = 500,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth = (instance_id, api_token) if instance_id and api_token else None
        self._service_label = service_label
        self._limit = limit

    def fetch(self, source: str, start: datetime, end: datetime) -> list[LogEntry]:
        params = {
            "query": f'{{{self._service_label}="{source}"}}',
            "start": str(int(start.timestamp() * 1_000_000_000)),
            "end": str(int(end.timestamp() * 1_000_000_000)),
            "direction": "forward",
            "limit": self._limit,
        }
        payload = self._request(params)
        streams = payload.get("data", {}).get("result", [])
        if not isinstance(streams, list):
            raise RuntimeError("Loki returned an invalid query_range response.")
        entries = [entry for stream in streams for entry in self._parse_stream(stream, source)]
        return sorted(entries, key=lambda entry: entry.timestamp)

    def _request(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            with httpx.Client(base_url=self._base_url, auth=self._auth, timeout=15) as client:
                response = client.get("/loki/api/v1/query_range", params=params)
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise RuntimeError(f"Loki query failed: {error}") from error
        if not isinstance(data, dict):
            raise RuntimeError("Loki returned an invalid response.")
        return data

    @staticmethod
    def _parse_stream(stream: Any, source: str) -> list[LogEntry]:
        values = stream.get("values") if isinstance(stream, dict) else None
        if not isinstance(values, list):
            return []
        entries = []
        for timestamp_ns, line in values:
            timestamp = datetime.fromtimestamp(int(timestamp_ns) / 1_000_000_000, tz=UTC)
            match = _RAW_LINE_PATTERN.match(line.strip())
            level = match.group("level") if match else "UNKNOWN"
            message = match.group("message") if match else line
            entries.append(LogEntry(timestamp=timestamp, service=source, level=level, message=message, raw=line))
        return entries
