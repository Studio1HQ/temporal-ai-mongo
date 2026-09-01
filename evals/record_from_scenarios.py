"""Record REAL trace fixtures from live app execution.

Submits each scenario as a real HTTP request to the running API, polls the
workflow until the AI decision is produced, then merges the trace files
Monocle wrote during that window into a single JSON under `evals/traces/`.

These are NOT mock traces — every span is emitted by the actual FastAPI +
Temporal + Nebius LLM + Voyage/Nebius embedding + Mongo stack running
locally. The eval suite in `evals/test_live_evals.py` grades these traces
via Okahu's evaluator API.

Usage:
    export API_BASE_URL=http://localhost:8010/api
    python -m evals.record_from_scenarios
"""

import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, List

import httpx

ROOT = Path(__file__).resolve().parent.parent
MONOCLE_DIR = ROOT / ".monocle"
OUT = ROOT / "evals" / "traces"
API = os.environ.get("API_BASE_URL", "http://localhost:8010/api")


SCENARIOS: List[Dict] = [
    {
        "id": "low_risk_ach",
        "description": "Small domestic ACH — should auto-approve.",
        "tx": {
            "transaction_type": "ach",
            "amount": 850,
            "currency": "USD",
            "sender": {"name": "Alice Anderson", "country": "US",
                       "account_number": "ACC-LR-001", "customer_id": "CUST-LR-001"},
            "recipient": {"name": "Bob Baker", "country": "US",
                          "account_number": "ACC-LR-002"},
            "description": "Rent split for August",
            "reference_number": "LR-001",
        },
    },
    {
        "id": "high_value_wire",
        "description": "$75K international wire — should escalate.",
        "tx": {
            "transaction_type": "wire_transfer",
            "amount": 75000,
            "currency": "USD",
            "sender": {"name": "Acme Global LLC", "country": "US",
                       "account_number": "ACC-HV-001", "customer_id": "CUST-HV-001"},
            "recipient": {"name": "Berlin Partners GmbH", "country": "DE",
                          "account_number": "ACC-HV-002"},
            "description": "Quarterly settlement per contract SLA-2024-11",
            "reference_number": "HV-001",
        },
    },
    {
        "id": "structuring",
        "description": "$4,999 wire just below the $5K reporting threshold.",
        "tx": {
            "transaction_type": "wire_transfer",
            "amount": 4999,
            "currency": "USD",
            "sender": {"name": "Chris Consultant", "country": "US",
                       "account_number": "ACC-ST-001", "customer_id": "CUST-ST-001"},
            "recipient": {"name": "Dan Design LLC", "country": "US",
                          "account_number": "ACC-ST-002"},
            "description": "Consulting fee",
            "reference_number": "ST-001",
        },
    },
    {
        "id": "wire_over_limit",
        "description": "$55K wire over the $50K auto-approval limit.",
        "tx": {
            "transaction_type": "wire_transfer",
            "amount": 55000,
            "currency": "USD",
            "sender": {"name": "Acme Industrial LLC", "country": "US",
                       "account_number": "ACC-WOL-001", "customer_id": "CUST-WOL-001"},
            "recipient": {"name": "Foshan Machinery Co", "country": "CN",
                          "account_number": "ACC-WOL-002"},
            "description": "Machinery import — invoice #4471",
            "reference_number": "WOL-001",
        },
    },
    {
        "id": "ambiguous_medium",
        "description": "$9,999 cross-border wire, vague description.",
        "tx": {
            "transaction_type": "wire_transfer",
            "amount": 9999,
            "currency": "USD",
            "sender": {"name": "Elena Enterprises", "country": "US",
                       "account_number": "ACC-AM-001", "customer_id": "CUST-AM-001"},
            "recipient": {"name": "Fadi Freelance", "country": "SG",
                          "account_number": "ACC-AM-002"},
            "description": "misc services",
            "reference_number": "AM-001",
        },
    },
]


def _snapshot() -> set:
    if not MONOCLE_DIR.exists():
        return set()
    return {p.name for p in MONOCLE_DIR.glob("monocle_trace_mongodb-fraud-agent_*.json")}


def _submit_and_wait(tx: Dict, timeout: float = 90.0) -> Dict:
    with httpx.Client(timeout=30.0) as client:
        r = client.post(f"{API}/transaction", json=tx)
        r.raise_for_status()
        txn_id = r.json()["transaction_id"]

        deadline = time.time() + timeout
        while time.time() < deadline:
            r = client.get(f"{API}/transaction/{txn_id}")
            if r.status_code == 200:
                d = r.json()
                if d.get("decision") in {"approve", "reject", "escalate"}:
                    d["_transaction_id"] = txn_id
                    return d
            time.sleep(2)
        raise TimeoutError(f"transaction {txn_id} did not resolve within {timeout}s")


def _run_one(scenario: Dict) -> None:
    print(f"\n=== {scenario['id']} ===")
    print(scenario["description"])
    before = _snapshot()

    t0 = time.time()
    result = _submit_and_wait(scenario["tx"])
    elapsed = time.time() - t0

    print(f"  decision={result['decision']} confidence={result.get('confidence')} elapsed={elapsed:.1f}s")
    print(f"  reasoning: {result.get('reasoning', '')[:200]}")

    # Give the batch span processor a moment to flush.
    time.sleep(3)
    after = _snapshot()
    new_files = sorted(after - before)
    if not new_files:
        print(f"  WARNING: no new trace files produced")
        return

    merged: list = []
    for name in new_files:
        merged.extend(json.loads((MONOCLE_DIR / name).read_text()))

    OUT.mkdir(parents=True, exist_ok=True)
    dst = OUT / f"{scenario['id']}.json"
    dst.write_text(json.dumps(merged, indent=2))
    span_types = {s.get("attributes", {}).get("span.type", "?") for s in merged}
    print(f"  merged {len(new_files)} trace file(s), {len(merged)} spans -> evals/traces/{dst.name}")
    print(f"  span types: {sorted(span_types)}")


def main() -> int:
    MONOCLE_DIR.mkdir(exist_ok=True)
    # Clean slate so _snapshot diffs are unambiguous.
    for old in MONOCLE_DIR.glob("monocle_trace_mongodb-fraud-agent_*.json"):
        old.unlink()
    for s in SCENARIOS:
        _run_one(s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
