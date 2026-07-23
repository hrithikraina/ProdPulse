"""One-off script: push data/simulated-logs.json into Loki/Grafana Cloud for local testing.

Usage:
    python3 scripts/seed_loki.py

Requires LOKI_BASE_URL, LOKI_INSTANCE_ID, and LOKI_API_TOKEN in .env (or the environment).

data/simulated-logs.json uses a fixed historical date. Loki/Grafana Cloud rejects log lines
past a short acceptance window relative to real ingestion time (observed on this project's
Grafana Cloud stack: accepted up to ~2h old, rejected by ~4h old — pushes still return 204
even for rejected lines, so this is easy to miss). Every timestamp is therefore shifted so the
fixture's latest entry lands 5 minutes before "now" at the moment you run this script,
preserving the relative spacing between events. The exact shift used is persisted to
data/.loki_seed_state.json (write_seed_shift) so queries — from the CLI, the API, or the RCA
graph — read the same value back rather than each independently guessing "now" at query
time, which only stays correct for a few minutes. Re-run this before each demo/test
session — the underlying data is only valid for a few hours after seeding (Loki's own
retention), not a few minutes.
"""

import json
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

from repositories.log_repository import compute_recency_shift, write_seed_shift

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value or "XXX" in value or value.startswith("REPLACE_"):
        raise SystemExit(f"Set a real {name} in .env before seeding Loki.")
    return value


def main() -> None:
    base_url = _required("LOKI_BASE_URL").rstrip("/")
    instance_id = _required("LOKI_INSTANCE_ID")
    api_token = _required("LOKI_API_TOKEN")

    records = json.loads((PROJECT_ROOT / "data" / "simulated-logs.json").read_text(encoding="utf-8"))

    original_timestamps = [datetime.fromisoformat(record["timestamp"]) for record in records]
    shift = compute_recency_shift(max(original_timestamps))
    print(f"Shifting fixture timestamps forward by {shift} so the latest entry lands ~5 minutes ago.")

    streams_by_service: dict[str, list[list[str]]] = defaultdict(list)
    for record, original_timestamp in zip(records, original_timestamps):
        shifted_timestamp = original_timestamp + shift
        # Same 4-field "<timestamp> <LEVEL> <service> <message>" shape LokiLogRepository/
        # JsonLogRepository already parse back out (repositories/log_repository.py).
        raw_line = f"{shifted_timestamp.isoformat()} {record['level']} {record['service']} {record['message']}"
        streams_by_service[record["service"]].append([str(int(shifted_timestamp.timestamp() * 1_000_000_000)), raw_line])

    payload = {
        "streams": [
            {"stream": {"service": service}, "values": sorted(values, key=lambda pair: pair[0])}
            for service, values in streams_by_service.items()
        ]
    }

    with httpx.Client(base_url=base_url, auth=(instance_id, api_token), timeout=15) as client:
        response = client.post("/loki/api/v1/push", json=payload)
        response.raise_for_status()

    write_seed_shift(PROJECT_ROOT / "data", shift)

    total_lines = sum(len(values) for values in streams_by_service.values())
    print(f"Pushed {total_lines} log line(s) across {len(streams_by_service)} service(s): {', '.join(sorted(streams_by_service))}")


if __name__ == "__main__":
    main()
