"""Monocle telemetry bootstrap for the mongodb-temporal-ai-agent-qs sample.

All Monocle wiring lives here. The vendored sample is otherwise untouched —
the only addition is one `import monocle_bootstrap` line at the top of each
entrypoint (api/main.py, temporal/run_worker.py, app.py).

What we register (each becomes a span on every real request):

1. `openai_client.OpenAIClient.analyze_transaction` → agent span
   `fraud_agent` (span.type = agentic.invocation). The OpenAI SDK call
   underneath is auto-instrumented by Monocle, so the LLM inference span
   with token counts comes for free.
2. `embedding_client.EmbeddingClient.get_embedding` → tool span
   `embed_transaction`.
3. `services.rule_engine.RuleEngine.evaluate_transaction` → tool span
   `rule_engine` (deterministic fraud signals; the grader's ground truth).
4. `services.risk_engine.RiskEngine.calculate_base_risk` +
   `.apply_risk_factors` → tool spans `risk_engine_base` / `risk_engine_apply`.
5. `TransactionActivities.*` for each Temporal activity → tool spans.

Only registers Monocle if `OKAHU_API_KEY` (or `MONOCLE_EXPORTER`) is set,
so import-time is cheap for tests / dev without observability.
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent
load_dotenv(_HERE / ".env")
os.environ.setdefault("MONOCLE_EXPORTER", "file")

from monocle_apptrace import setup_monocle_telemetry  # noqa: E402
from monocle_apptrace.instrumentation.common.constants import (  # noqa: E402
    SPAN_TYPES,
    SPAN_SUBTYPES,
)
from monocle_apptrace.instrumentation.common.wrapper import (  # noqa: E402
    task_wrapper,
    atask_wrapper,
)

AGENT_NAME = "fraud_agent"
WORKFLOW_NAME = "mongodb-fraud-agent"


def _first_arg_text(arguments):
    args = arguments.get("args") or ()
    kwargs = arguments.get("kwargs") or {}
    payload = None
    if args:
        payload = args[0]
    else:
        for k in ("transaction", "transaction_data", "text", "enriched_data", "prompt"):
            if k in kwargs:
                payload = kwargs[k]
                break
    if payload is None:
        return ""
    if isinstance(payload, (dict, list)):
        try:
            return json.dumps(payload, default=str)[:2000]
        except Exception:
            return str(payload)[:2000]
    return str(payload)[:2000]


def _result_text(arguments):
    result = arguments.get("result")
    if result is None:
        return ""
    if isinstance(result, (dict, list)):
        try:
            return json.dumps(result, default=str)[:2000]
        except Exception:
            return str(result)[:2000]
    return str(result)[:2000]


def _agent_processor(agent_name: str) -> dict:
    return {
        "type": SPAN_TYPES.AGENTIC_INVOCATION,
        "subtype": SPAN_SUBTYPES.CONTENT_PROCESSING,
        "attributes": [
            [
                {"attribute": "type", "accessor": lambda a: "agent.mongodb-fraud"},
                {"attribute": "name", "accessor": lambda a, n=agent_name: n},
                {"attribute": "description",
                 "accessor": lambda a: "Fraud-detection LLM agent (MongoDB Temporal AI sample)."},
            ],
        ],
        "events": [
            {"name": "data.input", "attributes": [
                {"attribute": "input", "accessor": _first_arg_text},
            ]},
            {"name": "data.output", "attributes": [
                {"attribute": "response", "accessor": _result_text},
            ]},
        ],
    }


def _tool_processor(tool_name: str, source_agent: str = AGENT_NAME) -> dict:
    return {
        "type": SPAN_TYPES.AGENTIC_TOOL_INVOCATION,
        "subtype": SPAN_SUBTYPES.CONTENT_GENERATION,
        "attributes": [
            [
                {"attribute": "type", "accessor": lambda a: "tool.mongodb-fraud"},
                {"attribute": "name", "accessor": lambda a, n=tool_name: n},
                {"attribute": "description",
                 "accessor": lambda a, n=tool_name: f"Tool: {n}"},
            ],
            [
                {"attribute": "type", "accessor": lambda a: "agent.mongodb-fraud"},
                {"attribute": "name", "accessor": lambda a, n=source_agent: n},
            ],
        ],
        "events": [
            {"name": "data.input", "attributes": [
                {"attribute": "input", "accessor": _first_arg_text},
            ]},
            {"name": "data.output", "attributes": [
                {"attribute": "response", "accessor": _result_text},
            ]},
        ],
    }


WRAPPER_METHODS = [
    # LLM agent — async method, atask_wrapper.
    {
        "package": "ai.openai_client",
        "object": "OpenAIClient",
        "method": "analyze_transaction",
        "span_name": AGENT_NAME,
        "wrapper_method": atask_wrapper,
        "output_processor": _agent_processor(AGENT_NAME),
    },
    # Embedding tool — async method.
    {
        "package": "ai.embedding_client",
        "object": "EmbeddingClient",
        "method": "get_embedding",
        "span_name": "embed_transaction",
        "wrapper_method": atask_wrapper,
        "output_processor": _tool_processor("embed_transaction"),
    },
    # Rule engine — deterministic signals. Its output is the ground truth
    # the LLM's reasoning must cite; the Okahu hallucination grader compares
    # claims against this span.
    {
        "package": "services.rule_engine",
        "object": "RuleEngine",
        "method": "apply_rules",
        "span_name": "rule_engine",
        "wrapper_method": task_wrapper,
        "output_processor": _tool_processor("rule_engine"),
    },
    # Risk engine — deterministic 0-100 score with named risk factors.
    {
        "package": "services.risk_engine",
        "object": "RiskEngine",
        "method": "calculate_base_risk",
        "span_name": "risk_engine_base",
        "wrapper_method": task_wrapper,
        "output_processor": _tool_processor("risk_engine_base"),
    },
    {
        "package": "services.risk_engine",
        "object": "RiskEngine",
        "method": "apply_risk_factors",
        "span_name": "risk_engine_apply",
        "wrapper_method": task_wrapper,
        "output_processor": _tool_processor("risk_engine_apply"),
    },
    # Temporal activities — instance methods on TransactionActivities.
    {
        "package": "temporal.activities",
        "object": "TransactionActivities",
        "method": "enrich_transaction_data",
        "span_name": "enrich_transaction",
        "wrapper_method": atask_wrapper,
        "output_processor": _tool_processor("enrich_transaction"),
    },
    {
        "package": "temporal.activities",
        "object": "TransactionActivities",
        "method": "perform_risk_assessment",
        "span_name": "risk_assessment",
        "wrapper_method": atask_wrapper,
        "output_processor": _tool_processor("risk_assessment"),
    },
    {
        "package": "temporal.activities",
        "object": "TransactionActivities",
        "method": "find_similar_transactions",
        "span_name": "vector_search",
        "wrapper_method": atask_wrapper,
        "output_processor": _tool_processor("vector_search"),
    },
    {
        "package": "temporal.activities",
        "object": "TransactionActivities",
        "method": "ai_decision_analysis",
        "span_name": "ai_decision",
        "wrapper_method": atask_wrapper,
        "output_processor": _tool_processor("ai_decision"),
    },
    {
        "package": "temporal.activities",
        "object": "TransactionActivities",
        "method": "store_decision",
        "span_name": "store_decision",
        "wrapper_method": atask_wrapper,
        "output_processor": _tool_processor("store_decision"),
    },
]


_INITIALIZED = False


def init() -> None:
    global _INITIALIZED
    if _INITIALIZED:
        return
    setup_monocle_telemetry(
        workflow_name=WORKFLOW_NAME,
        wrapper_methods=WRAPPER_METHODS,
        union_with_default_methods=True,  # keep OpenAI / FastAPI auto-instrumentation
    )
    _INITIALIZED = True


init()
