"""
Highlight: the MCP tool-calling framework

Core question: when tool calls go wrong -- poor recall, poor ranking -- how do
you fix it?
This module's answer:
  1. Query rewriting -- have an LLM expand the user's question into several
     sub-queries from different angles, then merge and deduplicate. Fixes
     incomplete recall.
  2. Reranking -- score the recalled results with an LLM and reorder them by
     relevance. Fixes poor ranking.
  3. Circuit breaker -- trip after repeated failures to prevent cascades.
  4. TTL cache -- return cached results for identical parameters.
  5. Fallback -- return a meaningful degraded result when a tool is unavailable.
"""
import asyncio
import hashlib
import inspect
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

from anthropic import AsyncAnthropic

from core.llm_utils import extract_text_content

logger = logging.getLogger(__name__)


# ── Data structures ───────────────────────────────────────────────────────────

class CircuitState(Enum):
    CLOSED    = "closed"     # normal
    OPEN      = "open"       # tripped, rejecting calls
    HALF_OPEN = "half_open"  # probing for recovery


@dataclass
class ToolResult:
    success:        bool
    data:           Any
    tool_name:      str
    error:          Optional[str] = None
    cached:         bool = False
    latency_ms:     float = 0.0
    reranked:       bool = False   # whether the results went through reranking


@dataclass
class ToolStats:
    """Runtime tool statistics, read by the Monitor."""
    total:              int = 0
    success:            int = 0
    failed:             int = 0
    total_latency_ms:   float = 0.0
    consecutive_fails:  int = 0

    @property
    def success_rate(self) -> float:
        return self.success / self.total if self.total else 1.0

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / self.total if self.total else 0.0


# ── Circuit breaker ───────────────────────────────────────────────────────────

class CircuitBreaker:
    """
    A three-state circuit breaker: CLOSED -> OPEN -> HALF_OPEN -> CLOSED

    Opens after `failure_threshold` consecutive failures; after `recovery_s`
    seconds it moves to HALF_OPEN and lets one probe through; the probe either
    closes it again or reopens it.
    """

    def __init__(self, failure_threshold: int = 5, recovery_s: float = 60.0):
        self.threshold   = failure_threshold
        self.recovery_s  = recovery_s
        self.state       = CircuitState.CLOSED
        self.fail_count  = 0
        self.opened_at:  Optional[float] = None

    def allow(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            if time.monotonic() - self.opened_at >= self.recovery_s:  # type: ignore
                self.state = CircuitState.HALF_OPEN
                return True
            return False
        return True  # HALF_OPEN: let a single probe through

    def record_success(self) -> None:
        self.fail_count = 0
        self.state = CircuitState.CLOSED

    def record_failure(self) -> None:
        self.fail_count += 1
        if self.fail_count >= self.threshold:
            self.state     = CircuitState.OPEN
            self.opened_at = time.monotonic()
            logger.warning(f"Circuit breaker opened after {self.fail_count} consecutive failures")


# ── Tool definition ───────────────────────────────────────────────────────────

@dataclass
class Tool:
    name:        str
    description: str
    handler:     Callable                    # async (params, context) -> Any
    schema:      Dict[str, Any]              # JSON Schema
    cache_ttl:   float = 0.0                 # 0 = no caching
    timeout_s:   float = 30.0
    supports_rerank: bool = False            # whether reranking applies
    fallback:    Optional[Callable] = None    # sync/async (params, context, error) -> Any

    # Runtime state (not part of the constructor)
    stats:   ToolStats    = field(default_factory=ToolStats, init=False)
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker, init=False)


# ── MCP tool manager ──────────────────────────────────────────────────────────

class MCPToolManager:
    """
    The MCP tool-calling framework.

    The optimization chain for retrieval tools:
      user query -> rewrite into sub-queries -> parallel recall -> rerank -> Top-K
    """

    def __init__(self, api_key: str, base_url: Optional[str] = None, model: str = "claude-3-5-sonnet-20241022"):
        kwargs: Dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = AsyncAnthropic(**kwargs)
        self._model  = model
        self._tools: Dict[str, Tool] = {}
        self._cache: Dict[str, tuple] = {}   # key → (result, expire_at, reranked)

    # ── Registration ──────────────────────────────────────────────────────────

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool
        logger.info(f"Registered tool: {tool.name}")

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    # ── Core call path ────────────────────────────────────────────────────────

    async def call(
        self,
        name: str,
        params: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
        *,
        use_cache: bool = True,
        rerank_top_k: int = 0,          # >0 reranks the results and keeps Top-K
    ) -> ToolResult:
        """
        Call a tool. The full chain:
          cache check -> breaker check -> param validation -> execute (with
          timeout) -> optional rerank -> cache write
        """
        tool = self._tools.get(name)
        if not tool:
            return ToolResult(success=False, data=None, tool_name=name, error=f"No such tool: {name}")

        cache_rerank_top_k = rerank_top_k if rerank_top_k > 0 and tool.supports_rerank else 0

        # Cache hit
        if use_cache and tool.cache_ttl > 0:
            cached = self._get_cache(name, params, cache_rerank_top_k)
            if cached is not None:
                cached_data, cached_reranked = cached
                tool.stats.total += 1
                tool.stats.success += 1
                return ToolResult(
                    success=True,
                    data=cached_data,
                    tool_name=name,
                    cached=True,
                    reranked=cached_reranked,
                )

        # Circuit-breaker check
        if not tool.breaker.allow():
            error = f"Tool {name} is circuit-broken; please retry shortly"
            return await self._fallback_result(tool, params, context, error)

        t0 = time.monotonic()
        tool.stats.total += 1
        try:
            # Validate params against the JSON Schema (required and properties.type)
            self._validate_params(tool, params)

            data = await asyncio.wait_for(self._run_handler(tool, params, context), timeout=tool.timeout_s)
            latency = (time.monotonic() - t0) * 1000

            tool.stats.success += 1
            tool.stats.consecutive_fails = 0
            tool.stats.total_latency_ms += latency
            tool.breaker.record_success()

            # Rerank (only for retrieval tools that return a list)
            reranked = False
            if rerank_top_k > 0 and tool.supports_rerank and isinstance(data, list):
                query = params.get("query", "")
                data, reranked = await self._rerank(query, data, rerank_top_k), True

            # Cache the final result, so a later hit does not return the un-reranked one.
            if tool.cache_ttl > 0:
                self._set_cache(name, params, data, tool.cache_ttl, cache_rerank_top_k, reranked)

            return ToolResult(success=True, data=data, tool_name=name,
                              latency_ms=latency, reranked=reranked)

        except asyncio.TimeoutError:
            tool.stats.failed += 1
            tool.stats.consecutive_fails += 1
            tool.breaker.record_failure()
            logger.error(f"Tool timed out: {name} ({tool.timeout_s}s)")
            return await self._fallback_result(tool, params, context, "execution timed out")

        except Exception as ex:
            tool.stats.failed += 1
            tool.stats.consecutive_fails += 1
            tool.breaker.record_failure()
            logger.error(f"Tool error: {name} -- {ex}")
            return await self._fallback_result(tool, params, context, str(ex))

    async def _fallback_result(
        self,
        tool: Tool,
        params: Dict[str, Any],
        context: Optional[Dict[str, Any]],
        error: str,
    ) -> ToolResult:
        """Return a degraded result when a tool is down, rather than a bare error."""
        if tool.fallback is None:
            return ToolResult(success=False, data=None, tool_name=tool.name, error=error)
        try:
            data = tool.fallback(params, context, error)
            if asyncio.iscoroutine(data):
                data = await data
            return ToolResult(
                success=True,
                data=data,
                tool_name=tool.name,
                error=error,
            )
        except Exception as ex:
            logger.error(f"Tool fallback failed: {tool.name} -- {ex}")
            return ToolResult(success=False, data=None, tool_name=tool.name, error=f"{error}; fallback failed: {ex}")

    async def _run_handler(
        self,
        tool: Tool,
        params: Dict[str, Any],
        context: Optional[Dict[str, Any]],
    ) -> Any:
        """
        Execute a tool handler.

        Async handlers are preferred; a legacy synchronous handler runs in a thread
        pool so it cannot block the event loop.
        """
        if inspect.iscoroutinefunction(tool.handler):
            return await tool.handler(params, context)
        result = await asyncio.to_thread(tool.handler, params, context)
        if inspect.isawaitable(result):
            return await result
        return result

    # ── Query rewriting (fixes incomplete recall) ─────────────────────────────

    async def rewrite_query(self, query: str, n: int = 3) -> List[str]:
        """
        Use an LLM to rewrite the original query into n sub-queries.

        Why: a single query tends to recall documents from only one angle.
        Retrieving several angles in parallel and merging lifts recall sharply.

        Example:
          original:  "refund process"
          rewritten: ["how do I request a refund", "how long does a refund take", "what is the refund policy"]
        """
        prompt = f"""Rewrite the user query below into {n} search sub-queries, each from a different angle, for retrieving from a knowledge base.
Requirement: each sub-query must take a different angle and cover a different facet of the original question.
Original query: "{query}"
Return a JSON array, for example: ["sub-query 1", "sub-query 2", "sub-query 3"]"""
        prompt = self._clean_text(prompt)
        try:
            resp = await self._client.messages.create(
                model=self._model, max_tokens=256, temperature=0.3,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = extract_text_content(resp.content)
            s, e = raw.find("["), raw.rfind("]") + 1
            queries = json.loads(raw[s:e])
            # Keep the original query too, then deduplicate
            return list(dict.fromkeys([query] + queries))
        except Exception as ex:
            logger.warning(f"Query rewriting failed, falling back to the original query: {ex}")
            return [query]

    async def search_with_rewrite(
        self,
        tool_name: str,
        query: str,
        top_k: int = 5,
        context: Optional[Dict[str, Any]] = None,
    ) -> ToolResult:
        """
        The full retrieval chain: rewrite -> parallel recall -> dedupe -> rerank -> Top-K

        This is the complete answer to incomplete recall and poor ranking.
        """
        # 1. Rewrite: generate sub-queries from several angles
        sub_queries = await self.rewrite_query(query, n=3)
        logger.info(f"Query rewritten: {query!r} -> {sub_queries}")

        # 2. Parallel recall: run every sub-query at once
        recall_k = max(top_k, 5)
        tasks = [
            self.call(tool_name, {"query": q, "top_k": recall_k}, context, use_cache=True)
            for q in sub_queries
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # 3. Merge and deduplicate (by content hash)
        seen, merged = set(), []
        for r in results:
            if isinstance(r, ToolResult) and r.success and isinstance(r.data, list):
                for item in r.data:
                    key = hashlib.md5(str(item).encode()).hexdigest()
                    if key not in seen:
                        seen.add(key)
                        merged.append(item)

        if not merged:
            return ToolResult(success=False, data=[], tool_name=tool_name, error="no sub-query returned any result")

        # 4. Rerank: score the merged results with an LLM, keep Top-K
        reranked = await self._rerank(query, merged, top_k)
        return ToolResult(success=True, data=reranked, tool_name=tool_name, reranked=True)

    # ── Reranking (fixes poor ranking) ────────────────────────────────────────

    async def _rerank(self, query: str, items: List[Any], top_k: int) -> List[Any]:
        """
        Rescore and reorder recalled results with an LLM.

        The problem it solves: a vector-similarity score is not the same as being
        useful to the user. An LLM understands semantic relevance, so Top-K quality
        improves markedly after reranking.
        """
        if len(items) <= top_k:
            return items

        # Serialize the results as text for the LLM to score
        items_text = "\n".join(f"{i}. {json.dumps(item, ensure_ascii=False)[:200]}"
                               for i, item in enumerate(items))
        prompt = f"""Score the following search results for relevance to the user's query (0-10) and return a JSON array.
User query: "{query}"
Search results:
{items_text}

Response format (indices ordered by descending relevance): [most relevant index, ..., least relevant index]
Return the JSON array only, with no other text."""
        prompt = self._clean_text(prompt)

        try:
            resp = await self._client.messages.create(
                model=self._model, max_tokens=256, temperature=0.0,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = extract_text_content(resp.content)
            s, e = raw.find("["), raw.rfind("]") + 1
            order: List[int] = json.loads(raw[s:e])
            reranked = [items[i] for i in order if 0 <= i < len(items)]
            return reranked[:top_k]
        except Exception as ex:
            logger.warning(f"Reranking failed, returning the original order: {ex}")
            return items[:top_k]

    # ── Cache ─────────────────────────────────────────────────────────────────

    def _cache_key(self, name: str, params: Dict, rerank_top_k: int = 0) -> str:
        payload = {"params": params, "rerank_top_k": rerank_top_k}
        return f"{name}:{hashlib.md5(json.dumps(payload, sort_keys=True).encode()).hexdigest()}"

    def _get_cache(self, name: str, params: Dict, rerank_top_k: int = 0) -> Optional[Tuple[Any, bool]]:
        key = self._cache_key(name, params, rerank_top_k)
        if key in self._cache:
            data, expire_at, reranked = self._cache[key]
            if time.monotonic() < expire_at:
                return data, reranked
            del self._cache[key]
        return None

    def _set_cache(
        self,
        name: str,
        params: Dict,
        data: Any,
        ttl: float,
        rerank_top_k: int = 0,
        reranked: bool = False,
    ) -> None:
        if len(self._cache) >= 5000:
            # Drop the oldest quarter
            for k in list(self._cache)[:1250]:
                del self._cache[k]
        self._cache[self._cache_key(name, params, rerank_top_k)] = (data, time.monotonic() + ttl, reranked)

    # ── Parameter validation ──────────────────────────────────────────────────

    _TYPE_MAP = {"string": str, "number": (int, float), "integer": int, "boolean": bool, "array": list, "object": dict}

    def _validate_params(self, tool: Tool, params: Dict[str, Any]) -> None:
        """Validate params against the tool's JSON Schema; raise ValueError if invalid."""
        schema = tool.schema
        required = schema.get("required", [])
        properties = schema.get("properties", {})

        for field in required:
            if field not in params:
                raise ValueError(f"Tool {tool.name} is missing the required parameter: {field}")

        for key, value in params.items():
            if key in properties:
                expected_type = properties[key].get("type")
                if expected_type and expected_type in self._TYPE_MAP:
                    if not isinstance(value, self._TYPE_MAP[expected_type]):
                        raise ValueError(
                            f"Tool {tool.name} parameter {key} has the wrong type: expected {expected_type}, got {type(value).__name__}"
                        )

    @staticmethod
    def _clean_text(value: Any) -> str:
        """Strip Unicode surrogates so the LLM request cannot fail on encoding."""
        if value is None:
            return ""
        if not isinstance(value, str):
            value = str(value)
        return value.encode("utf-8", errors="ignore").decode("utf-8")

    # ── Statistics ────────────────────────────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        return {
            name: {
                "total": t.stats.total,
                "success_rate": round(t.stats.success_rate, 3),
                "avg_latency_ms": round(t.stats.avg_latency_ms, 1),
                "consecutive_fails": t.stats.consecutive_fails,
                "circuit_state": t.breaker.state.value,
            }
            for name, t in self._tools.items()
        }
