"""Agent tool definitions and implementations.

Every agent tool lives here. The orchestrator only has to:
  1. Expose the tool allowlist for a given agent type
  2. Execute the tool_use the LLM returns
  3. Hand the tool result back to the LLM

The tools themselves stay deterministic and testable, and keep these apart:
  - analysis of the current request
  - technical troubleshooting advice
  - billing field verification
  - human-handoff summaries
  - the shared knowledge-base RAG tool

Actions that would need real authorization in a business system -- order lookup,
issuing a refund, editing a bill -- are deliberately not faked here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, TYPE_CHECKING, Union

if TYPE_CHECKING:
    from agents.agent_orchestrator import Request


AgentToolHandler = Callable[["Request", Dict[str, Any]], Union[Any, Awaitable[Any]]]


@dataclass(frozen=True)
class AgentToolSpec:
    """An agent-visible tool: its definition plus the function that runs it."""

    name: str
    description: str
    input_schema: Dict[str, Any]
    handler: AgentToolHandler


def make_tool(
    name: str,
    description: str,
    properties: Dict[str, Any],
    handler: AgentToolHandler,
    required: Optional[List[str]] = None,
) -> AgentToolSpec:
    """Create an agent tool with its JSON Schema."""
    return AgentToolSpec(
        name=name,
        description=description,
        input_schema={
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        },
        handler=handler,
    )


def inspect_request_context(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """General-service tool: return a redacted snapshot of the current request."""
    return {
        "intent": req.intent.value if req.intent else None,
        "intent_group": req.intent_group,
        "urgency": req.urgency.name if req.urgency else None,
        "intent_confidence": round(req.intent_confidence, 4),
        "entities": req.entities or {},
        "context_available": bool(req.context),
        "requested_focus": str(args.get("focus", "general"))[:40],
    }


def suggest_required_fields(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """General-service tool: work out which fields still need asking, by business type."""
    intent = req.intent.value if req.intent else "other"
    fields: List[str] = []
    if intent in {"order_status", "logistics"}:
        fields = ["order number or order date"]
    elif intent in {"account", "account_security"}:
        fields = ["sign-in method or account identifier", "when the problem started"]
    elif intent in {"complaint", "request"}:
        fields = ["when it happened", "the resolution you want"]
    elif intent == "other":
        fields = ["the specific problem you want solved"]
    return {
        "intent": intent,
        "required_fields": fields,
        "known_entities": req.entities or {},
    }


def lookup_error_code(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """Technical tool: explain where common error codes point, without claiming to have read server logs."""
    code = str(args.get("error_code", "")).upper().strip()
    mapping = {
        "401": ("authentication failed", ["check whether the token or API key expired", "check the request timestamp and signature", "check that the account is signed in"]),
        "403": ("insufficient permissions", ["check account or plan permissions", "check resource permissions and the IP allowlist"]),
        "404": ("resource or path not found", ["check the endpoint path and environment", "check that the resource identifier is correct"]),
        "500": ("server-side failure", ["record the request_id and the time it happened", "check dependent services, parameter format and server logs"]),
    }
    meaning, steps = mapping.get(
        code,
        ("unrecognized error code", ["provide the full error message, the time it happened and the runtime environment"]),
    )
    return {
        "error_code": code,
        "meaning": meaning,
        "next_steps": steps,
        "server_log_checked": False,
    }


def build_diagnostic_plan(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """Technical tool: produce a low-risk troubleshooting order."""
    environment = str(args.get("environment", "unknown"))[:80]
    reproduced = bool(args.get("reproduced", False))
    steps = [
        "reproduce it and record the full error message",
        "check network, DNS, proxy and certificates",
        "check version, configuration and permissions",
    ]
    if reproduced:
        steps.append("reproduce with a minimal request and record the request_id")
    return {
        "environment": environment,
        "reproduced": reproduced,
        "diagnostic_steps": steps,
    }


def check_billing_fields(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """Billing tool: check whether the required verification fields are present."""
    fields = {
        "order_id": bool(req.entities.get("order_id")),
        "amount": bool(req.entities.get("amount")),
        "date": bool(req.entities.get("date")),
        "payment_channel": bool(args.get("payment_channel")),
    }
    return {
        "fields": fields,
        "missing_fields": [name for name, present in fields.items() if not present],
        "can_confirm_refund": False,
        "reason": "this tool only checks fields; it does not reach the order or payment system",
    }


def compare_amounts(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """Billing tool: arithmetic on amounts the user stated explicitly, nothing more."""
    try:
        first = float(args["amount_a"])
        second = float(args["amount_b"])
    except (KeyError, TypeError, ValueError):
        return {"success": False, "error": "amount_a and amount_b must be numbers"}
    return {
        "success": True,
        "amount_a": first,
        "amount_b": second,
        "difference": round(first - second, 2),
        "interpretation": "this is only the difference between two amounts; it is not a verdict on a double charge or a refund",
    }


def create_handoff_summary(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """Escalation tool: produce a structured summary a human agent can pick up."""
    return {
        "request_id": req.request_id,
        "reason": str(args.get("reason", "needs a human agent to continue verification"))[:120],
        "intent": req.intent.value if req.intent else "unknown",
        "urgency": req.urgency.name if req.urgency else "UNKNOWN",
        "entities": req.entities or {},
        "sensitive_data_required": False,
    }


def build_shared_rag_tools(tool_manager: Any) -> Dict[str, AgentToolSpec]:
    """Build the RAG tool shared by every agent."""

    async def search_knowledge_base(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
        query = str(args.get("query") or req.message or "").strip()
        top_k = int(args.get("top_k", 5) or 5)
        if not query:
            return {"success": False, "error": "query must not be empty", "results": []}
        if tool_manager is None:
            return {"success": False, "error": "the RAG tool is not initialized", "results": []}

        result = await tool_manager.search_with_rewrite(
            "knowledge_search",
            query,
            top_k=top_k,
        )
        if not getattr(result, "success", False):
            return {
                "success": False,
                "query": query,
                "error": getattr(result, "error", "knowledge-base search failed"),
                "results": [],
                "reranked": False,
            }

        return {
            "success": True,
            "query": query,
            "top_k": top_k,
            "results": result.data,
            "reranked": bool(getattr(result, "reranked", False)),
        }

    return {
        "search_knowledge_base": make_tool(
            "search_knowledge_base",
            "Search the knowledge base and return the most relevant chunks; useful for general, technical, billing and escalation cases.",
            {
                "query": {"type": "string", "description": "the user's question or search keywords"},
                "top_k": {"type": "integer", "description": "how many results to return"},
            },
            search_knowledge_base,
            required=["query"],
        )
    }


def general_tools() -> Dict[str, AgentToolSpec]:
    return {
        "inspect_request_context": make_tool(
            "inspect_request_context",
            "Inspect the current request's intent, urgency, entities and available context; does not query any external business system.",
            {"focus": {"type": "string", "description": "the business area to focus on"}},
            inspect_request_context,
        ),
        "suggest_required_fields": make_tool(
            "suggest_required_fields",
            "Suggest the fields still worth asking the user for, based on the current intent.",
            {},
            suggest_required_fields,
        ),
    }


def technical_tools() -> Dict[str, AgentToolSpec]:
    return {
        "lookup_error_code": make_tool(
            "lookup_error_code",
            "Explain what common HTTP error codes may mean and give low-risk next steps; does not read server logs.",
            {"error_code": {"type": "string", "description": "for example 401, 403 or 500"}},
            lookup_error_code,
            required=["error_code"],
        ),
        "build_diagnostic_plan": make_tool(
            "build_diagnostic_plan",
            "Produce a troubleshooting order from the runtime environment and whether the issue reproduces; performs no configuration changes.",
            {
                "environment": {"type": "string", "description": "app, browser, server, Docker and so on"},
                "reproduced": {"type": "boolean", "description": "whether the problem reproduces reliably"},
            },
            build_diagnostic_plan,
            required=["environment", "reproduced"],
        ),
    }


def billing_tools() -> Dict[str, AgentToolSpec]:
    return {
        "check_billing_fields": make_tool(
            "check_billing_fields",
            "Check whether the billing verification fields are complete; does not reach the order, payment or refund systems.",
            {"payment_channel": {"type": "string", "description": "payment channel, for example card, PayPal or bank transfer"}},
            check_billing_fields,
        ),
        "compare_amounts": make_tool(
            "compare_amounts",
            "Compute the difference between two amounts the user stated explicitly; makes no double-charge judgement and issues no refund.",
            {
                "amount_a": {"type": "number", "description": "the first amount"},
                "amount_b": {"type": "number", "description": "the second amount"},
            },
            compare_amounts,
            required=["amount_a", "amount_b"],
        ),
    }


def escalation_tools() -> Dict[str, AgentToolSpec]:
    return {
        "create_handoff_summary": make_tool(
            "create_handoff_summary",
            "Produce a structured handoff summary for a human agent; does not create a real ticket.",
            {"reason": {"type": "string", "description": "why this needs escalating"}},
            create_handoff_summary,
        ),
    }
