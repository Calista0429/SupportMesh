"""
Highlight: end-to-end intent recognition

A three-way fusion strategy:
  1. LLM semantics (weight 70%) -- the workhorse, handles complex meaning and context
  2. Embedding similarity (weight 20%) -- fast match for common phrasings
  3. Keyword patterns (weight 10%) -- zero-latency backstop

The three results are merged by weighted vote; anything below the confidence
threshold degrades to OTHER. The LLM and embedding paths run in parallel.
"""
import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from anthropic import AsyncAnthropic

from core.llm_utils import extract_text_content
from core.text_matching import keyword_matches

logger = logging.getLogger(__name__)


class IntentCategory(Enum):
    QUERY      = "query"       # information lookup
    COMPLAINT  = "complaint"   # complaint or dissatisfaction
    REQUEST    = "request"     # asking for an action
    GREETING   = "greeting"    # greeting
    ESCALATION = "escalation"  # asking to escalate or reach a human
    TECHNICAL  = "technical"   # technical problem
    BILLING    = "billing"     # billing or refund
    ACCOUNT    = "account"     # account management
    FEEDBACK   = "feedback"    # positive feedback
    ORDER_STATUS = "order_status"        # order status
    LOGISTICS = "logistics"              # shipping and delivery
    REFUND = "refund"                    # refund or return
    INVOICE = "invoice"                  # invoice
    PAYMENT_ISSUE = "payment_issue"      # payment or charge anomaly
    ACCOUNT_SECURITY = "account_security" # account security
    TECHNICAL_LOGIN = "technical_login"  # login and auth failure
    TECHNICAL_CRASH = "technical_crash"  # crash or error code
    HUMAN_HANDOFF = "human_handoff"      # handoff to a human
    OTHER      = "other"


class UrgencyLevel(Enum):
    LOW      = 1
    MEDIUM   = 2
    HIGH     = 3
    CRITICAL = 4


@dataclass
class IntentResult:
    intent:     IntentCategory
    confidence: float
    urgency:    UrgencyLevel
    intent_group: str
    entities:   Dict[str, List[str]]   # entities extracted from the message
    reasoning:  str
    latency_ms: float
    source_scores: Dict[str, float] = field(default_factory=dict)


# ── Few-shot templates (used for both LLM examples and embedding matching) ────
_TEMPLATES: Dict[IntentCategory, List[str]] = {
    IntentCategory.QUERY:      ["What is my order status?", "How do I reset my password?", "When will the parcel arrive?"],
    IntentCategory.COMPLAINT:  ["I have been waiting for hours!", "This service is terrible!", "Nobody has dealt with this!"],
    IntentCategory.REQUEST:    ["Please cancel my order", "I need to change my address", "Help me with a refund"],
    IntentCategory.GREETING:   ["Hello", "Hi, is anyone there?", "Good morning"],
    IntentCategory.ESCALATION: ["I want to file a complaint!", "Get me a human agent", "Let me speak to your manager"],
    IntentCategory.TECHNICAL:  ["The app keeps crashing", "I cannot log in", "I am getting a 500 error"],
    IntentCategory.BILLING:    ["Why was I charged twice?", "I want to request a refund", "There is a problem with my invoice"],
    IntentCategory.ACCOUNT:    ["Change my email address", "Close my account", "Update my personal details"],
    IntentCategory.FEEDBACK:   ["Great service!", "Very satisfied", "Happy to leave a good review"],
    IntentCategory.ORDER_STATUS: ["What is the status of my order?", "Has my order shipped yet?", "Where is my order in the process?"],
    IntentCategory.LOGISTICS: ["When will the parcel arrive?", "Tracking has not updated in days", "How long does delivery take?"],
    IntentCategory.REFUND: ["I want to request a refund", "How do returns and refunds work?", "How long until the refund lands?"],
    IntentCategory.INVOICE: ["Please issue an invoice", "How do I change the invoice details?", "Where can I find my e-invoice?"],
    IntentCategory.PAYMENT_ISSUE: ["Why was I charged twice?", "My payment failed, what now?", "I was overcharged this month"],
    IntentCategory.ACCOUNT_SECURITY: ["My account was hacked", "I see a suspicious login", "I need to reset my password"],
    IntentCategory.TECHNICAL_LOGIN: ["Login keeps returning 401", "I never receive the verification code", "I cannot sign in to my account"],
    IntentCategory.TECHNICAL_CRASH: ["The app keeps crashing", "The page returns a 500 error", "The system quits unexpectedly"],
    IntentCategory.HUMAN_HANDOFF: ["Transfer me to a human agent", "I want to talk to a person", "Please escalate this"],
}

_SPECIFIC_INTENTS = {
    IntentCategory.ORDER_STATUS,
    IntentCategory.LOGISTICS,
    IntentCategory.REFUND,
    IntentCategory.INVOICE,
    IntentCategory.PAYMENT_ISSUE,
    IntentCategory.ACCOUNT_SECURITY,
    IntentCategory.TECHNICAL_LOGIN,
    IntentCategory.TECHNICAL_CRASH,
    IntentCategory.HUMAN_HANDOFF,
}

_GENERIC_INTENTS = {
    IntentCategory.QUERY,
    IntentCategory.BILLING,
    IntentCategory.TECHNICAL,
    IntentCategory.ACCOUNT,
    IntentCategory.ESCALATION,
}

_INTENT_GROUPS: Dict[IntentCategory, IntentCategory] = {
    IntentCategory.ORDER_STATUS: IntentCategory.QUERY,
    IntentCategory.LOGISTICS: IntentCategory.QUERY,
    IntentCategory.REFUND: IntentCategory.BILLING,
    IntentCategory.INVOICE: IntentCategory.BILLING,
    IntentCategory.PAYMENT_ISSUE: IntentCategory.BILLING,
    IntentCategory.ACCOUNT_SECURITY: IntentCategory.ACCOUNT,
    IntentCategory.TECHNICAL_LOGIN: IntentCategory.TECHNICAL,
    IntentCategory.TECHNICAL_CRASH: IntentCategory.TECHNICAL,
    IntentCategory.HUMAN_HANDOFF: IntentCategory.ESCALATION,
}

# Urgency keywords
_URGENCY_KEYWORDS = {
    UrgencyLevel.CRITICAL: ["emergency", "urgent", "asap", "immediately", "right now"],
    UrgencyLevel.HIGH:     ["today", "hurry", "as soon as possible", "quickly", "now"],
    UrgencyLevel.MEDIUM:   ["this week", "soon", "before long"],
}


def _cosine(a: List[float], b: List[float]) -> float:
    """Cosine similarity in pure Python, no numpy dependency."""
    dot = sum(x * y for x, y in zip(a, b))
    na  = sum(x * x for x in a) ** 0.5
    nb  = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0



class IntentRecognizer:
    """
    End-to-end intent recognizer.

    No local model is loaded at init; every AI capability goes through the
    Anthropic API. Template embeddings are lazily computed on the first request
    and cached for reuse.
    """

    def __init__(
        self,
        api_key: str,
        base_url: Optional[str] = None,
        model: str = "claude-3-5-sonnet-20241022",
        confidence_threshold: float = 0.5,
    ):
        kwargs: Dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self.client    = AsyncAnthropic(**kwargs)
        self.model     = model
        self.threshold = confidence_threshold
        # Third-party compatible APIs (DeepSeek and friends) usually lack embeddings,
        # so that strategy is disabled. The official Anthropic SDK has no embeddings
        # resource either, so a stable local character n-gram vector serves as the
        # lightweight backstop, keeping the three-way fusion genuinely runnable.
        self._embedding_enabled = not bool(base_url)

        self._tpl_embeddings: Dict[IntentCategory, List[List[float]]] = {}
        self._cache: Dict[str, IntentResult] = {}
        self.cache_hits   = 0
        self.cache_misses = 0

    # ── Public interface ──────────────────────────────────────────────────────

    async def recognize(
        self,
        message: str,
        history: Optional[List[Dict[str, str]]] = None,
    ) -> IntentResult:
        """
        Recognize the user's intent.

        history format: [{"role": "user"/"assistant", "content": "..."}]
        """
        key = self._cache_key(message, history)
        if key in self._cache:
            self.cache_hits += 1
            return self._cache[key]
        self.cache_misses += 1

        t0 = time.monotonic()

        # LLM and embedding run in parallel (embedding skipped when unavailable)
        llm_task = asyncio.create_task(self._llm_recognize(message, history))
        emb_task = asyncio.create_task(self._embedding_recognize(message)) if self._embedding_enabled else None
        pat      = self._pattern_recognize(message)

        if emb_task:
            llm, emb = await asyncio.gather(llm_task, emb_task)
        else:
            llm = await llm_task
            emb = {"intent": IntentCategory.OTHER, "confidence": 0.0}

        intent, confidence, source_scores = self._vote(llm, emb, pat)
        entities = self._extract_entities(message)
        urgency  = self._urgency(message, intent)

        result = IntentResult(
            intent=intent,
            confidence=confidence,
            urgency=urgency,
            intent_group=self._intent_group(intent),
            entities=entities,
            reasoning=llm.get("reasoning", ""),
            latency_ms=(time.monotonic() - t0) * 1000,
            source_scores=source_scores,
        )

        # LRU cache
        if len(self._cache) >= 1000:
            for k in list(self._cache)[:500]:
                del self._cache[k]
        self._cache[key] = result
        return result

    def learn(self, message: str, correct: IntentCategory) -> None:
        """Online learning: add a corrected sample to the templates and drop its cached embedding."""
        tpls = _TEMPLATES.setdefault(correct, [])
        if message not in tpls:
            tpls.append(message)
            self._tpl_embeddings.pop(correct, None)  # recomputed next time
            logger.info(f"Learned a new sample -> {correct.value}: {message[:40]}")

    # ── The three recognition strategies ──────────────────────────────────────

    async def _llm_recognize(
        self,
        message: str,
        history: Optional[List[Dict[str, str]]],
    ) -> Dict[str, Any]:
        """Strategy 1: LLM semantic understanding (few-shot plus context)."""
        message = self._clean_text(message)
        # Build the few-shot examples
        examples = "\n".join(
            f'  message: "{t}" -> intent: {cat.value}'
            for cat, tpls in _TEMPLATES.items()
            for t in tpls[:1]  # one per category, to keep the prompt short
        )
        # Context from the last 3 turns
        ctx = ""
        if history:
            ctx = "\nRecent conversation:\n" + "\n".join(
                f"  {self._clean_text(m.get('role', 'user'))}: {self._clean_text(m.get('content', ''))}"
                for m in history[-3:]
            )

        prompt = f"""You are an expert at customer-service intent analysis. Use the examples to classify the user's intent and return JSON.
Prefer a fine-grained business intent over a broad category whenever the message supports one.
For example, prefer refund over billing, invoice over billing, and technical_login over technical.

Examples:
{examples}

{ctx}
User message: "{message}"

Response format (JSON only, no other text):
{{"intent": "<intent value>", "confidence": <0-1>, "reasoning": "<one sentence>"}}

Allowed intents: {", ".join(c.value for c in IntentCategory)}"""
        prompt = self._clean_text(prompt)

        try:
            resp = await self.client.messages.create(
                model=self.model,
                max_tokens=256,
                temperature=0.1,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = extract_text_content(resp.content)
            s, e = raw.find("{"), raw.rfind("}") + 1
            data = json.loads(raw[s:e])
            try:
                data["intent"] = IntentCategory(data["intent"])
            except ValueError:
                data["intent"] = IntentCategory.OTHER
            return data
        except Exception as ex:
            logger.warning(f"LLM recognition failed: {ex}")
            return {"intent": IntentCategory.OTHER, "confidence": 0.0, "reasoning": "LLM failed", "failed": True}

    async def _embedding_recognize(self, message: str) -> Dict[str, Any]:
        """Strategy 2: embedding similarity matching."""
        try:
            await self._load_template_embeddings()
            msg_vec = await self._embed_text(message)

            best_cat, best_score = IntentCategory.OTHER, 0.0
            for cat, vecs in self._tpl_embeddings.items():
                score = max(_cosine(msg_vec, v) for v in vecs)
                if score > best_score:
                    best_score, best_cat = score, cat

            return {"intent": best_cat, "confidence": best_score}
        except Exception as ex:
            logger.warning(f"Embedding recognition failed: {ex}")
            return {"intent": IntentCategory.OTHER, "confidence": 0.0}

    def _pattern_recognize(self, message: str) -> Dict[str, Any]:
        """Strategy 3: keyword patterns (synchronous, zero-latency backstop)."""
        msg = message.lower()
        specific_patterns = {
            IntentCategory.HUMAN_HANDOFF: ["human agent", "real person", "speak to someone", "live agent"],
            IntentCategory.ORDER_STATUS: ["order status", "has it shipped", "where is my order", "order progress", "not arrived", "hasn't arrived", "overdue"],
            IntentCategory.LOGISTICS: ["tracking", "tracked", "parcel", "package", "courier", "delivery", "deliveries", "delivered", "shipping", "shipment", "waybill"],
            IntentCategory.REFUND: ["refund", "refunds", "refunded", "return", "returns", "money back", "send it back"],
            IntentCategory.INVOICE: ["invoice", "invoices", "receipt", "receipts", "billing address", "vat", "tax id"],
            IntentCategory.PAYMENT_ISSUE: ["charged twice", "double charge", "double charged", "overcharged", "charged an extra", "extra charge", "wrong amount", "payment failed", "declined"],
            IntentCategory.ACCOUNT_SECURITY: ["hacked", "suspicious login", "reset password", "reset my password", "two-factor", "2fa", "security"],
            IntentCategory.TECHNICAL_LOGIN: ["cannot log in", "can't log in", "cannot sign in", "login failed", "401", "verification code"],
            IntentCategory.TECHNICAL_CRASH: ["crash", "crashes", "crashing", "crashed", "freezes", "freezing", "500", "broken"],
        }
        generic_patterns = {
            IntentCategory.ESCALATION: ["complaint", "complaints", "complain", "manager", "supervisor", "escalate"],
            IntentCategory.COMPLAINT:  ["terrible", "awful", "horrible", "waited too long", "unacceptable", "disappointed"],
            IntentCategory.QUERY:      ["?", "how do", "how long", "what is", "status", "where"],
            IntentCategory.REQUEST:    ["help me", "i need", "please", "can you", "could you"],
            IntentCategory.GREETING:   ["hello", "hi", "hey", "good morning", "good afternoon"],
            IntentCategory.BILLING:    ["refund", "charge", "charges", "charged", "invoice", "bill", "bills", "billing"],
            IntentCategory.TECHNICAL:  ["crash", "crashes", "crashing", "error", "errors", "bug", "bugs", "not working"],
            IntentCategory.ACCOUNT:    ["password", "email", "account", "accounts", "profile"],
        }

        best_cat, best_score = self._best_pattern_match(msg, specific_patterns)
        if best_cat != IntentCategory.OTHER:
            return {"intent": best_cat, "confidence": best_score}

        best_cat, best_score = self._best_pattern_match(msg, generic_patterns)
        return {"intent": best_cat, "confidence": best_score}

    def _vote(self, llm: Dict, emb: Dict, pat: Dict) -> tuple[IntentCategory, float, Dict[str, float]]:
        """Weighted vote. Returns the final intent, fused confidence and per-strategy scores."""
        source_scores = {
            "llm": float(llm.get("confidence", 0.0) or 0.0),
            "embedding": float(emb.get("confidence", 0.0) or 0.0),
            "pattern": float(pat.get("confidence", 0.0) or 0.0),
        }
        if llm.get("failed"):
            if emb.get("intent") != IntentCategory.OTHER and emb.get("confidence", 0.0) > 0:
                return emb["intent"], source_scores["embedding"], source_scores
            if pat.get("intent") != IntentCategory.OTHER and pat.get("confidence", 0.0) > 0:
                return pat["intent"], source_scores["pattern"], source_scores
            return IntentCategory.OTHER, 0.0, source_scores

        if self._embedding_enabled:
            weights = [(llm, 0.7), (emb, 0.2), (pat, 0.1)]
        else:
            weights = [(llm, 0.85), (pat, 0.15)]
        scores: Dict[IntentCategory, float] = {}
        for result, w in weights:
            cat  = result.get("intent", IntentCategory.OTHER)
            conf = result.get("confidence", 0.0)
            scores[cat] = scores.get(cat, 0.0) + w * conf

        best = max(scores, key=scores.get)  # type: ignore
        best_score = scores[best]
        pat_intent = pat.get("intent", IntentCategory.OTHER)
        pat_conf = float(pat.get("confidence", 0.0) or 0.0)
        if best in _GENERIC_INTENTS and pat_intent in _SPECIFIC_INTENTS and pat_conf >= 0.5 and best_score < 0.8:
            source_scores["refined_by_pattern"] = pat_conf
            return pat_intent, max(best_score, pat_conf), source_scores
        if best_score < self.threshold:
            return IntentCategory.OTHER, best_score, source_scores
        return best, best_score, source_scores

    # ── Entity extraction ─────────────────────────────────────────────────────

    def _extract_entities(self, message: str) -> Dict[str, List[str]]:
        """Extract high-value entities by rule, avoiding an extra LLM call per request."""
        message = self._clean_text(message)
        return {
            "order_id": self._unique(re.findall(r"(?:order(?:\s*(?:id|number|no\.?))?|#)\s*[:#]?\s*([A-Za-z0-9_-]{4,32})", message, re.I)),
            "product": [],
            "date": self._unique(re.findall(r"\b(today|tomorrow|yesterday|this week|next week|last week|\d{4}[-/.]\d{1,2}[-/.]\d{1,2})\b", message, re.I)),
            "amount": self._unique(re.findall(r"((?:[$£€]|USD|GBP|EUR)\s*\d+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?\s*(?:dollars?|pounds?|euros?|usd|gbp|eur))", message, re.I)),
            "error_code": self._unique(re.findall(r"\b([45]\d{2}|[A-Z][A-Z0-9_-]{2,16})\b", message)),
        }

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _load_template_embeddings(self) -> None:
        """Lazily embed every template (runs only on the first call)."""
        missing = [cat for cat in _TEMPLATES if cat not in self._tpl_embeddings]
        if not missing:
            return

        all_texts = [t for cat in missing for t in _TEMPLATES[cat]]
        vecs = [await self._embed_text(text) for text in all_texts]
        idx = 0
        for cat in missing:
            n = len(_TEMPLATES[cat])
            self._tpl_embeddings[cat] = vecs[idx: idx + n]
            idx += n

    async def _embed_text(self, text: str) -> List[float]:
        """
        Produce a text vector.

        If the configured client ever exposes embeddings.create, remote vectors win.
        Today's Anthropic SDK has no such resource, so this degrades to a character
        n-gram hash vector -- a missing embedding service never breaks the fusion.
        """
        embeddings = getattr(self.client, "embeddings", None)
        if embeddings is not None:
            try:
                resp = await embeddings.create(model="voyage-3-lite", input=[text])
                return list(resp.data[0].embedding)
            except Exception as ex:
                logger.warning(f"Remote embedding failed, falling back to the local vector: {ex}")

        return self._local_embedding(text)

    @staticmethod
    def _local_embedding(text: str, dims: int = 256) -> List[float]:
        """Stable character n-gram hash vector, for semantic approximation without a remote embedder."""
        normalized = text.lower().strip()
        vec = [0.0] * dims
        tokens = set()
        for n in (1, 2, 3):
            if len(normalized) >= n:
                tokens.update(normalized[i:i + n] for i in range(len(normalized) - n + 1))
        if not tokens:
            tokens.add(normalized)

        for token in tokens:
            digest = hashlib.md5(token.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:4], "big") % dims
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vec[idx] += sign
        return vec

    def _urgency(self, message: str, intent: IntentCategory) -> UrgencyLevel:
        msg = message.lower()
        for level, kws in _URGENCY_KEYWORDS.items():
            if any(kw in msg for kw in kws):
                return level
        if intent in (IntentCategory.ESCALATION, IntentCategory.HUMAN_HANDOFF):
            return UrgencyLevel.HIGH
        if intent == IntentCategory.COMPLAINT:
            return UrgencyLevel.MEDIUM
        return UrgencyLevel.LOW

    def _cache_key(self, message: str, history: Optional[List[Dict[str, str]]] = None) -> str:
        payload = {"message": self._clean_text(message)[:200]}
        if history:
            payload["history"] = [
                {
                    "role": self._clean_text(item.get("role", ""))[:20],
                    "content": self._clean_text(item.get("content", ""))[:160],
                }
                for item in history[-3:]
            ]
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _unique(values: List[str]) -> List[str]:
        return list(dict.fromkeys(value.strip() for value in values if value and value.strip()))

    @staticmethod
    def _best_pattern_match(
        message: str,
        patterns: Dict[IntentCategory, List[str]],
    ) -> tuple[IntentCategory, float]:
        best_cat, best_score = IntentCategory.OTHER, 0.0
        for cat, kws in patterns.items():
            hits = sum(1 for kw in kws if keyword_matches(kw, message))
            if not hits:
                continue
            # One clear business keyword earns usable confidence; several raise it.
            score = min(1.0, 0.5 + 0.25 * (hits - 1))
            if score > best_score:
                best_score, best_cat = score, cat
        return best_cat, best_score

    @staticmethod
    def _intent_group(intent: IntentCategory) -> str:
        return _INTENT_GROUPS.get(intent, intent).value

    @staticmethod
    def _clean_text(value: Any) -> str:
        """Strip Unicode surrogates so the HTTP client cannot crash encoding the prompt."""
        if value is None:
            return ""
        if not isinstance(value, str):
            value = str(value)
        return value.encode("utf-8", errors="ignore").decode("utf-8")

    @property
    def cache_stats(self) -> Dict[str, Any]:
        total = self.cache_hits + self.cache_misses
        return {
            "size": len(self._cache),
            "hits": self.cache_hits,
            "misses": self.cache_misses,
            "hit_rate": self.cache_hits / total if total else 0.0,
        }
