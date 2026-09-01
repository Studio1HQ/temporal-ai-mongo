"""Submit evals against the App's recent trace window and wait for them to complete.

This isn't a pytest-style assertion suite — Okahu's evaluator is async, and
the portal / VS Code extension are the canonical places to view verdicts.
This helper exists to *seed* the portal so a human reviewer opens VS Code
and immediately sees fresh verdicts on their traces.

The jobs are submitted via POST /v1/eval/jobs, then polled via
GET /v1/eval/jobs?job_id=<id> until each reports SUCCEEDED. Once done, the
verdicts are visible in:
  - Okahu portal → App `mongodb-fraud-agent` → Traces
  - VS Code Okahu extension → sidebar → Traces
  - Kahu Chat (VS Code) → "what does the {hallucination|pii|bias|fraud_reasoning_grounded_v2} say?"

Usage:
    python -m evals.submit_evals            # all built-in graders + custom template
    python -m evals.submit_evals --hallucination-only
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, List

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# Ensure Monocle bootstrap wires Okahu exporter (used by monocle_test_tools when
# it re-ingests our recorded fixture spans into the App).
sys.path.insert(0, str(ROOT))
import monocle_bootstrap  # noqa: F401

from monocle_test_tools import TraceAssertion  # noqa: E402
from monocle_test_tools.file_span_loader import JSONSpanLoader  # noqa: E402

TRACES = ROOT / "evals" / "traces"
TEMPLATES = ROOT / "evals" / "templates"

EVAL_ENDPOINT = os.getenv("OKAHU_EVALUATION_ENDPOINT", "https://eval.okahu.co/api").rstrip("/")
API_KEY = (os.getenv("OKAHU_API_KEY") or "").strip()

SUITE: List[Dict] = [
    {"scenario": "low_risk_ach", "eval": "hallucination"},
    {"scenario": "low_risk_ach", "eval": "pii_leakage"},
    {"scenario": "high_value_wire", "eval": "hallucination"},
    {"scenario": "high_value_wire", "eval": "bias"},
    {"scenario": "structuring", "eval": "hallucination"},
    {"scenario": "structuring", "eval": "custom", "template": "fraud_reasoning_grounded_v2.json"},
    {"scenario": "wire_over_limit", "eval": "hallucination"},
    {"scenario": "wire_over_limit", "eval": "custom", "template": "fraud_reasoning_grounded_v2.json"},
    {"scenario": "ambiguous_medium", "eval": "hallucination"},
    {"scenario": "ambiguous_medium", "eval": "pii_leakage"},
]


def _submit(scenario: str, eval_spec: Dict) -> str | None:
    trace_file = TRACES / f"{scenario}.json"
    if not trace_file.exists():
        print(f"  SKIP {scenario}/{eval_spec['eval']}: {trace_file.name} missing")
        return None

    spans = JSONSpanLoader.from_json(str(trace_file))
    asserter = TraceAssertion(filtered_spans=spans)
    asserter.with_evaluation("okahu")

    if eval_spec["eval"] == "custom":
        asserter.check_eval(template_path=str(TEMPLATES / eval_spec["template"]), expected="grounded")
    else:
        expected = {"hallucination": "no_hallucination", "pii_leakage": "no_pii", "bias": "unbiased"}[eval_spec["eval"]]
        asserter.check_eval(eval_spec["eval"], expected)

    # The sync response is `result: []` so check_eval records an internal
    # assertion. The job_id is embedded in that recorded message. Messages
    # accumulate across TraceAssertion instances, so read the last occurrence.
    msg = asserter.get_assertion_messages() if asserter.has_assertions() else ""
    marker = "'job_id': '"
    i = msg.rfind(marker)
    if i == -1:
        return None
    j = msg.find("'", i + len(marker))
    return msg[i + len(marker):j]


def _poll(job_id: str, timeout: float = 180.0) -> str:
    url = f"{EVAL_ENDPOINT}/v1/eval/jobs"
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = requests.get(url, headers={"x-api-key": API_KEY}, params={"job_id": job_id}, timeout=15)
        r.raise_for_status()
        for _slot, info in r.json().get("jobs", {}).items():
            if info.get("job_id", "").endswith(job_id.split("_")[-1]):
                status = info.get("status")
                if status in {"SUCCEEDED", "FAILED"}:
                    return status
        time.sleep(5)
    return "TIMEOUT"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--hallucination-only", action="store_true")
    args = p.parse_args()

    if not API_KEY:
        print("OKAHU_API_KEY not set — cannot submit evals.")
        return 1

    suite = [s for s in SUITE if not args.hallucination_only or s["eval"] == "hallucination"]

    submitted: List[Dict] = []
    print(f"Submitting {len(suite)} eval jobs...")
    for spec in suite:
        job_id = _submit(spec["scenario"], spec)
        label = spec["eval"] if spec["eval"] != "custom" else spec["template"].replace(".json", "")
        if job_id:
            submitted.append({**spec, "job_id": job_id, "label": label})
            print(f"  submitted {spec['scenario']:20s} {label:35s} job={job_id[-16:]}")
        else:
            print(f"  FAILED   {spec['scenario']:20s} {label}")

    print(f"\nPolling {len(submitted)} jobs until SUCCEEDED (up to 3 min each)...")
    for job in submitted:
        status = _poll(job["job_id"])
        job["status"] = status
        print(f"  {status:10s} {job['scenario']:20s} {job['label']}")

    ok = sum(1 for j in submitted if j["status"] == "SUCCEEDED")
    print(f"\n{ok}/{len(submitted)} jobs succeeded. "
          f"Open the Okahu portal or VS Code Okahu extension to view verdicts.")

    out = ROOT / "evals" / "last_submitted.json"
    out.write_text(json.dumps(submitted, indent=2))
    print(f"job log: {out}")
    return 0 if ok == len(submitted) else 2


if __name__ == "__main__":
    sys.exit(main())
