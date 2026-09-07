"""
Highlight: multi-agent routing and orchestration

Core question: with several agents, how do you route?

Routing strategy (three tiers):
  1. Intent routing -- map an IntentCategory straight to its dedicated agent
  2. Performance routing -- with several agents of one type, pick the one with
     the highest success rate and lowest latency
  3. Fallback routing -- degrade to GeneralAgent when the dedicated one is down

Parallel collaboration:
  - A compound question (a technical problem plus a billing problem, say) can
    be dispatched to several agents at once
  - The orchestrator merges their answers before returning

Escalation:
  - Confidence below the threshold escalates to a higher tier or to a human
"""
import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from anthropic import AsyncAnthropic

from core.intent_recognizer import IntentCategory, IntentRecognizer, UrgencyLevel
from core.llm_utils import extract_text_content
from core.text_matching import keyword_matches

logger = logging.getLogger(__name__)


# ── Data structures ───────────────────────────────────────────────────────────

class AgentType(Enum):
    GENERAL   = "general"    # general customer service
    TECHNICAL = "technical"  # technical support
    BILLING   = "billing"    # billing and refunds
    ESCALATION = "escalation" # human escalation (placeholder)


@dataclass
class AgentStats:
    """Runtime agent statistics, used by the Monitor and by routing decisions."""
    total:     int   = 0
    success:   int   = 0
    total_ms:  float = 0.0
    monitor_penalty: float = 0.0

    @property
    def success_rate(self) -> float:
        return self.success / self.total if self.total else 1.0

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.total if self.total else 0.0

    def routing_score(self) -> float:
        """Routing score: high success rate and low latency score higher."""
        latency_score = 1.0 / (1.0 + self.avg_ms / 1000)
        base_score = self.success_rate * 0.7 + latency_score * 0.3
        return base_score * max(0.0, 1.0 - self.monitor_penalty)


@dataclass
class AgentResponse:
    agent_type:  AgentType
    content:     str
    success:     bool
    confidence:  float = 1.0
    latency_ms:  float = 0.0
    escalate:    bool  = False   # whether escalation is required


@dataclass
class Request:
    message:     str
    user_id:     str
    conv_id:     str
    context:     str = ""        # formatted context from MemoryManager
    history:     Optional[List[Dict[str, str]]] = None  # dialogue history for intent recognition
    entities:    Dict[str, List[str]] = field(default_factory=dict)
    intent:      Optional[IntentCategory] = None
    intent_group: Optional[str] = None
    urgency:     Optional[UrgencyLevel]   = None
    intent_confidence: float = 1.0
    request_id:  str = field(default_factory=lambda: str(uuid.uuid4())[:8])


@dataclass
class OrchestratorResult:
    request_id:  str
    response:    str
    agent_type:  AgentType
    intent:      Optional[IntentCategory]
    escalated:   bool  = False
    latency_ms:  float = 0.0
    agent_types: List[AgentType] = field(default_factory=list)
    primary_agent: Optional[AgentType] = None
    supporting_agents: List[AgentType] = field(default_factory=list)
    routing_reason: str = ""
    routing_confidence: float = 0.0


@dataclass
class RoutingDecision:
    """The structured routing decision for one request."""
    primary_agent: AgentType
    supporting_agents: List[AgentType] = field(default_factory=list)
    reason: str = ""
    confidence: float = 0.0

    @property
    def agent_types(self) -> List[AgentType]:
        return [self.primary_agent] + self.supporting_agents

    @property
    def multi_agent(self) -> bool:
        return bool(self.supporting_agents)


# ── Base agent ────────────────────────────────────────────────────────────────

class BaseAgent:
    """Base class for every agent, wrapping the LLM call and its statistics."""

    agent_type: AgentType
    system_prompt: str

    def __init__(self, client: AsyncAnthropic, model: str, skill_manager: Optional[Any] = None):
        self._client = client
        self._model  = model
        self._skill_manager = skill_manager
        self.stats   = AgentStats()

    async def handle(self, req: Request) -> AgentResponse:
        t0 = time.monotonic()
        self.stats.total += 1
        try:
            content = await self._call_llm(req)
            ms = (time.monotonic() - t0) * 1000
            self.stats.success += 1
            self.stats.total_ms += ms
            escalate = self._needs_escalation(content)
            return AgentResponse(
                agent_type=self.agent_type,
                content=content,
                success=True,
                latency_ms=ms,
                escalate=escalate,
            )
        except Exception as ex:
            ms = (time.monotonic() - t0) * 1000
            self.stats.total_ms += ms
            logger.error(f"{self.agent_type.value} failed to handle the request: {ex}")
            return AgentResponse(
                agent_type=self.agent_type,
                content="Sorry, something went wrong handling your request. Please try again shortly.",
                success=False,
                latency_ms=ms,
            )

    async def _call_llm(self, req: Request) -> str:
        def _clean(s: str) -> str:
            return s.encode("utf-8", errors="ignore").decode("utf-8")

        messages = []
        if req.context:
            messages.append({"role": "user", "content": f"[Background]\n{_clean(req.context)}"})
            messages.append({"role": "assistant", "content": "Understood, I have the background."})
        if req.entities:
            entities_text = json.dumps(req.entities, ensure_ascii=False)
            messages.append({"role": "user", "content": f"[Structured entities]\n{_clean(entities_text)}"})
            messages.append({"role": "assistant", "content": "Understood, I will take these entities into account."})
        messages.append({"role": "user", "content": _clean(req.message)})

        resp = await self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            system=self._build_system_prompt(req),
            messages=messages,
        )
        return extract_text_content(resp.content)

    def _build_system_prompt(self, req: Request) -> str:
        """Splice hot-loaded Skills into the system prompt so business rules apply per request."""
        if self._skill_manager is None:
            return self.system_prompt
        skill_prompt = self._skill_manager.prompt_for(req.message, self.agent_type.value)
        if not skill_prompt:
            return self.system_prompt
        return f"{self.system_prompt}\n\n[Dynamic Skills]\n{skill_prompt}"

    def _needs_escalation(self, content: str) -> bool:
        """Detect whether the agent is asking to escalate (simple keyword check)."""
        keywords = ["human agent", "live agent", "escalate", "specialist", "cannot handle"]
        return any(keyword_matches(kw, content.lower()) for kw in keywords)


class GeneralAgent(BaseAgent):
    agent_type    = AgentType.GENERAL
    system_prompt = (
        "You are SupportMesh, a customer-service assistant. Answer the user in a friendly, concise way. "
        "When a question falls outside what you can do, say so plainly and suggest a specialist handoff."
    )


class TechnicalAgent(BaseAgent):
    agent_type    = AgentType.TECHNICAL
    system_prompt = (
        "You are a technical support specialist. Focus on troubleshooting, error diagnosis and system configuration. "
        "Give clear, step-by-step solutions. When something needs backend access, say it must be escalated."
    )


class BillingAgent(BaseAgent):
    agent_type    = AgentType.BILLING
    system_prompt = (
        "You are a billing specialist. Focus on billing questions, refund requests, invoices and subscriptions. "
        "Stay accurate and professional on financial matters. Any actual refund needs human review -- say so."
    )


# ── Orchestrator ──────────────────────────────────────────────────────────────

class AgentOrchestrator:
    """
    The multi-agent orchestrator.

    Routing logic (three tiers):
      1. Map intent to an agent type
      2. With several instances of that type, pick the best routing_score()
      3. Degrade to GeneralAgent when the dedicated agent fails
    """

    # Static intent-to-agent-type mapping (the routing table)
    _INTENT_ROUTING: Dict[IntentCategory, AgentType] = {
        IntentCategory.TECHNICAL:  AgentType.TECHNICAL,
        IntentCategory.TECHNICAL_LOGIN: AgentType.TECHNICAL,
        IntentCategory.TECHNICAL_CRASH: AgentType.TECHNICAL,
        IntentCategory.BILLING:    AgentType.BILLING,
        IntentCategory.REFUND:     AgentType.BILLING,
        IntentCategory.INVOICE:    AgentType.BILLING,
        IntentCategory.PAYMENT_ISSUE: AgentType.BILLING,
        IntentCategory.ACCOUNT:    AgentType.BILLING,
        IntentCategory.ACCOUNT_SECURITY: AgentType.BILLING,
        IntentCategory.ESCALATION: AgentType.ESCALATION,
        IntentCategory.HUMAN_HANDOFF: AgentType.ESCALATION,
        # Everything else falls through to GENERAL
    }

    def __init__(
        self,
        api_key:  str,
        base_url: Optional[str] = None,
        model:    str = "claude-3-5-sonnet-20241022",
        skill_manager: Optional[Any] = None,
    ):
        kwargs: Dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        client = AsyncAnthropic(**kwargs)

        self._intent_recognizer = IntentRecognizer(api_key=api_key, base_url=base_url, model=model)
        self._skill_manager = skill_manager

        # Agent pool: each type may hold several instances (horizontal scaling)
        self._pool: Dict[AgentType, List[BaseAgent]] = {
            AgentType.GENERAL:   [GeneralAgent(client, model, skill_manager)],
            AgentType.TECHNICAL: [TechnicalAgent(client, model, skill_manager)],
            AgentType.BILLING:   [BillingAgent(client, model, skill_manager)],
        }

    def set_skill_manager(self, skill_manager: Optional[Any]) -> None:
        """Swap the SkillManager reference, for runtime reloads or test doubles."""
        self._skill_manager = skill_manager
        for agents in self._pool.values():
            for agent in agents:
                agent._skill_manager = skill_manager

    async def recognize_intent(
        self,
        message: str,
        history: Optional[List[Dict[str, str]]] = None,
    ):
        """Expose intent recognition so the API layer can decide upfront whether RAG is needed."""
        return await self._intent_recognizer.recognize(message, history=history)

    # ── Main entry point ──────────────────────────────────────────────────────

    async def run(self, req: Request) -> OrchestratorResult:
        """
        The full request-handling flow:
          recognize intent -> route to an agent -> execute -> check escalation -> return
        """
        t0 = time.monotonic()

        # 1. Intent recognition (skipped when the caller already resolved it)
        if req.intent is None:
            intent_result = await self._intent_recognizer.recognize(req.message, history=req.history)
            req.intent  = intent_result.intent
            req.intent_group = intent_result.intent_group
            req.urgency = intent_result.urgency
            req.intent_confidence = intent_result.confidence

        if self._needs_clarification(req):
            return OrchestratorResult(
                request_id=req.request_id,
                response="I am not yet sure which area this falls under. Is it about an order or delivery, a refund or billing, your account details, or a technical fault?",
                agent_type=AgentType.GENERAL,
                intent=req.intent,
                escalated=False,
                latency_ms=(time.monotonic() - t0) * 1000,
                agent_types=[AgentType.GENERAL],
                primary_agent=AgentType.GENERAL,
                routing_reason="low-confidence OTHER intent; clarify the request first",
                routing_confidence=req.intent_confidence,
            )

        # Compound questions collaborate in parallel -- one sentence covering both a
        # login failure and a double charge, for instance.
        decision = self._route_decision(req)
        if decision.multi_agent:
            return await self.run_parallel(req, decision)

        # 2. Run the primary agent (with fallback)
        response = await self._execute(req, decision.primary_agent)

        # 4. Escalation check
        escalated = False
        if response.escalate or req.urgency == UrgencyLevel.CRITICAL or req.intent in (
            IntentCategory.ESCALATION,
            IntentCategory.HUMAN_HANDOFF,
        ):
            escalated = True
            logger.warning(f"Request {req.request_id} triggered escalation: urgency={req.urgency}")
            # In production this is where you would open a ticket and page a human

        return OrchestratorResult(
            request_id=req.request_id,
            response=response.content,
            agent_type=response.agent_type,
            intent=req.intent,
            escalated=escalated,
            latency_ms=(time.monotonic() - t0) * 1000,
            agent_types=[response.agent_type],
            primary_agent=decision.primary_agent,
            supporting_agents=[],
            routing_reason=decision.reason,
            routing_confidence=decision.confidence,
        )

    async def run_parallel(self, req: Request, decision: RoutingDecision) -> OrchestratorResult:
        """
        Dispatch to several agents in parallel and merge their answers.
        Used for compound questions that span, say, technical and billing topics.
        """
        t0 = time.monotonic()
        agent_types = decision.agent_types
        tasks = [self._execute(req, at) for at in agent_types]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

        # Merge: primary agent first, supporting agents after.
        parts = []
        for r in responses:
            if isinstance(r, AgentResponse) and r.success:
                role = "primary" if r.agent_type == decision.primary_agent else "supporting"
                parts.append(f"[{r.agent_type.value} - {role}]\n{r.content}")

        combined = "\n\n".join(parts) if parts else "Sorry, every agent failed to handle this request."
        escalated = any(isinstance(r, AgentResponse) and r.escalate for r in responses)

        return OrchestratorResult(
            request_id=req.request_id,
            response=combined,
            agent_type=decision.primary_agent,
            intent=req.intent,
            escalated=escalated,
            latency_ms=(time.monotonic() - t0) * 1000,
            agent_types=[
                r.agent_type for r in responses
                if isinstance(r, AgentResponse) and r.success
            ] or agent_types,
            primary_agent=decision.primary_agent,
            supporting_agents=decision.supporting_agents,
            routing_reason=decision.reason,
            routing_confidence=decision.confidence,
        )

    # ── Routing logic ─────────────────────────────────────────────────────────

    def _route(self, intent: Optional[IntentCategory], urgency: Optional[UrgencyLevel]) -> AgentType:
        """
        Three-tier routing decision:
          1. Intent mapping
          2. Urgency override (CRITICAL escalates immediately)
          3. Default to GENERAL
        """
        if urgency == UrgencyLevel.CRITICAL:
            return AgentType.ESCALATION

        if intent and intent in self._INTENT_ROUTING:
            target = self._INTENT_ROUTING[intent]
            # Use the target type when an instance is available, otherwise degrade
            if target in self._pool and self._pool[target]:
                return target

        return AgentType.GENERAL

    def _route_decision(self, req: Request) -> RoutingDecision:
        """
        The structured routing decision.

        Urgency and human handoff come first, then domain scores pick the primary
        agent and its supporting agents. That lets the result express "primary plus
        supporting diagnosis" instead of concatenating whatever keywords matched.
        """
        if req.urgency == UrgencyLevel.CRITICAL:
            return RoutingDecision(
                primary_agent=AgentType.ESCALATION,
                reason="urgency is CRITICAL, routing to escalation",
                confidence=1.0,
            )

        if req.intent in (IntentCategory.ESCALATION, IntentCategory.HUMAN_HANDOFF):
            return RoutingDecision(
                primary_agent=AgentType.ESCALATION,
                reason=f"intent is {req.intent.value if req.intent else 'unknown'}, routing to escalation",
                confidence=max(req.intent_confidence, 0.8),
            )

        scores = self._domain_scores(req)
        available_scores = {
            agent_type: score
            for agent_type, score in scores.items()
            if agent_type == AgentType.GENERAL or self._pool.get(agent_type)
        }
        if not available_scores:
            return RoutingDecision(
                primary_agent=AgentType.GENERAL,
                reason="no dedicated agent available, degrading to GeneralAgent",
                confidence=0.1,
            )

        ordered = sorted(available_scores.items(), key=lambda item: item[1], reverse=True)
        primary_agent, primary_score = ordered[0]
        supporting_agents = [
            agent_type
            for agent_type, score in ordered[1:]
            if agent_type != AgentType.GENERAL and score >= 0.45 and score >= primary_score * 0.55
        ]

        reason = self._routing_reason(req, available_scores, primary_agent, supporting_agents)
        return RoutingDecision(
            primary_agent=primary_agent,
            supporting_agents=supporting_agents,
            reason=reason,
            confidence=round(min(primary_score, 1.0), 3),
        )

    def _domain_scores(self, req: Request) -> Dict[AgentType, float]:
        """Score each domain agent by intent, keywords and entities."""
        msg = req.message.lower()
        scores = {
            AgentType.GENERAL: 0.1,
            AgentType.TECHNICAL: 0.0,
            AgentType.BILLING: 0.0,
        }

        if req.intent in (
            IntentCategory.QUERY,
            IntentCategory.ORDER_STATUS,
            IntentCategory.LOGISTICS,
            IntentCategory.REQUEST,
            IntentCategory.COMPLAINT,
            IntentCategory.GREETING,
            IntentCategory.FEEDBACK,
            IntentCategory.OTHER,
        ):
            scores[AgentType.GENERAL] += 0.55

        if req.intent in (
            IntentCategory.TECHNICAL,
            IntentCategory.TECHNICAL_LOGIN,
            IntentCategory.TECHNICAL_CRASH,
        ):
            scores[AgentType.TECHNICAL] += 0.75

        if req.intent in (
            IntentCategory.BILLING,
            IntentCategory.ACCOUNT,
            IntentCategory.ACCOUNT_SECURITY,
            IntentCategory.REFUND,
            IntentCategory.INVOICE,
            IntentCategory.PAYMENT_ISSUE,
        ):
            scores[AgentType.BILLING] += 0.75

        technical_kws = ["crash", "error", "bug", "cannot log in", "can't log in", "login failed", "500", "401", "verification code"]
        billing_kws = ["refund", "return", "charge", "charged", "invoice", "bill", "billing", "payment", "subscription", "overcharged"]
        general_kws = ["order", "tracking", "parcel", "delivery", "shipping", "membership", "points", "help"]

        technical_hits = sum(1 for kw in technical_kws if keyword_matches(kw, msg))
        billing_hits = sum(1 for kw in billing_kws if keyword_matches(kw, msg))
        general_hits = sum(1 for kw in general_kws if keyword_matches(kw, msg))

        scores[AgentType.TECHNICAL] += min(0.45, technical_hits * 0.18)
        scores[AgentType.BILLING] += min(0.45, billing_hits * 0.18)
        scores[AgentType.GENERAL] += min(0.35, general_hits * 0.12)

        entities = req.entities or {}
        if entities.get("error_code"):
            scores[AgentType.TECHNICAL] += 0.2
        if entities.get("amount"):
            scores[AgentType.BILLING] += 0.15
        if entities.get("order_id"):
            scores[AgentType.GENERAL] += 0.1

        return {agent_type: round(score, 3) for agent_type, score in scores.items()}

    @staticmethod
    def _routing_reason(
        req: Request,
        scores: Dict[AgentType, float],
        primary_agent: AgentType,
        supporting_agents: List[AgentType],
    ) -> str:
        score_text = ", ".join(
            f"{agent_type.value}={score:.2f}"
            for agent_type, score in sorted(scores.items(), key=lambda item: item[1], reverse=True)
        )
        support_text = ", ".join(agent.value for agent in supporting_agents) or "none"
        intent = req.intent.value if req.intent else "unknown"
        return (
            f"intent={intent}, group={req.intent_group or 'unknown'}, "
            f"primary={primary_agent.value}, supporting={support_text}, scores=[{score_text}]"
        )

    def _collaboration_targets(self, req: Request) -> List[AgentType]:
        """
        Decide whether several agents should collaborate in parallel.

        Intent recognition usually returns a single primary intent, so domain keywords
        fill the gap for compound questions -- "login error and charged twice" needs
        the technical and billing agents at the same time.
        """
        msg = req.message.lower()
        targets: List[AgentType] = []

        technical_kws = ["crash", "error", "bug", "cannot log in", "can't log in", "login failed", "500", "401"]
        billing_kws = ["refund", "charge", "charged", "invoice", "bill", "billing", "payment", "subscription"]

        if req.intent in (
            IntentCategory.TECHNICAL,
            IntentCategory.TECHNICAL_LOGIN,
            IntentCategory.TECHNICAL_CRASH,
        ) or any(keyword_matches(kw, msg) for kw in technical_kws):
            targets.append(AgentType.TECHNICAL)
        if req.intent in (
            IntentCategory.BILLING,
            IntentCategory.ACCOUNT,
            IntentCategory.ACCOUNT_SECURITY,
            IntentCategory.REFUND,
            IntentCategory.INVOICE,
            IntentCategory.PAYMENT_ISSUE,
        ) or any(keyword_matches(kw, msg) for kw in billing_kws):
            targets.append(AgentType.BILLING)

        # Deduplicate while preserving order, keeping only types that have instances.
        deduped = list(dict.fromkeys(targets))
        return [agent_type for agent_type in deduped if self._pool.get(agent_type)]

    @staticmethod
    def _needs_clarification(req: Request) -> bool:
        """On low confidence with no clear intent, ask a follow-up rather than misroute."""
        if req.intent != IntentCategory.OTHER:
            return False
        text = (req.message or "").strip()
        if len(text) <= 2:
            return False
        return req.intent_confidence < 0.5

    def _best_agent(self, agent_type: AgentType) -> Optional[BaseAgent]:
        """
        Performance routing: pick the highest routing_score() among agents of a type.
        This is the heart of adjusting routing from live performance.
        """
        agents = self._pool.get(agent_type, [])
        if not agents:
            return None
        return max(agents, key=lambda a: a.stats.routing_score())

    async def _execute(self, req: Request, agent_type: AgentType) -> AgentResponse:
        """Run an agent, degrading to GeneralAgent on failure."""
        agent = self._best_agent(agent_type)
        if agent is None:
            agent = self._best_agent(AgentType.GENERAL)
        if agent is None:
            return AgentResponse(
                agent_type=AgentType.GENERAL,
                content="The service is temporarily unavailable. Please try again shortly.",
                success=False,
            )

        response = await agent.handle(req)

        # Degrade to GeneralAgent when the dedicated agent fails
        if not response.success and agent_type != AgentType.GENERAL:
            logger.warning(f"{agent_type.value} failed, degrading to GeneralAgent")
            fallback = self._best_agent(AgentType.GENERAL)
            if fallback:
                response = await fallback.handle(req)

        return response

    # ── Statistics (read by the Monitor) ──────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        result = {}
        for agent_type, agents in self._pool.items():
            for i, agent in enumerate(agents):
                key = f"{agent_type.value}_{i}"
                result[key] = {
                    "total":        agent.stats.total,
                    "success_rate": round(agent.stats.success_rate, 3),
                    "avg_ms":       round(agent.stats.avg_ms, 1),
                    "monitor_penalty": round(agent.stats.monitor_penalty, 3),
                    "routing_score": round(agent.stats.routing_score(), 3),
                }
        return result

    def update_routing_penalties(self, penalties: Dict[str, float]) -> None:
        """
        Take live performance feedback from the Monitor and adjust routing penalties.

        Keys in `penalties` are the agent keys from get_stats(), e.g. technical_0.
        """
        for agent_type, agents in self._pool.items():
            for i, agent in enumerate(agents):
                key = f"{agent_type.value}_{i}"
                penalty = penalties.get(key, 0.0)
                agent.stats.monitor_penalty = min(max(penalty, 0.0), 0.9)
