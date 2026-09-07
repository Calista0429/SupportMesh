"""
Highlight: multi-turn conversation memory

A three-tier memory architecture modelled on human memory:
  1. Working memory (Redis) -- the last N messages of the current session,
     read and written in milliseconds
  2. Episodic memory (ChromaDB) -- cross-session history, retrieved by
     semantic similarity
  3. User profile (ChromaDB) -- long-term preferences and entities distilled
     from past conversations

Key design points:
  - Context assembly fuses all three tiers, ordered by importance and recency
  - Working memory is compressed into an LLM summary past a threshold, so the
    context window cannot blow up
  - Every embedding comes from the Anthropic API; no local model
"""
import hashlib
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional

import chromadb
import redis.asyncio as redis
from anthropic import AsyncAnthropic

from core.llm_utils import extract_text_content

logger = logging.getLogger(__name__)


class MsgRole(Enum):
    USER      = "user"
    ASSISTANT = "assistant"
    SYSTEM    = "system"


@dataclass
class Message:
    role:       MsgRole
    content:    str
    timestamp:  datetime = field(default_factory=datetime.now)
    metadata:   Dict[str, Any] = field(default_factory=dict)


@dataclass
class MemoryContext:
    """The full context handed to an agent."""
    recent_messages:  List[Message]   # working memory: recent turns
    relevant_history: List[str]       # episodic memory: related excerpts
    user_profile:     Dict[str, Any]  # user profile: preferences, frequent entities
    summary:          str             # summary of this session, once compressed

    @staticmethod
    def _clean(text: str) -> str:
        """Strip Unicode surrogates to avoid encoding errors."""
        return text.encode("utf-8", errors="ignore").decode("utf-8")

    def to_prompt_text(self) -> str:
        """Render the memory context as text an LLM can consume."""
        parts = []
        if self.summary:
            parts.append(f"[Session summary]\n{self._clean(self.summary)}")
        if self.relevant_history:
            parts.append("[Related history]\n" + "\n".join(f"- {self._clean(h)}" for h in self.relevant_history[:3]))
        if self.user_profile:
            parts.append(f"[User profile]\n{json.dumps(self.user_profile, ensure_ascii=True)}")
        if self.recent_messages:
            parts.append("[Recent conversation]")
            for m in self.recent_messages:
                parts.append(f"{m.role.value}: {self._clean(m.content)}")
        return "\n\n".join(parts)


class MemoryManager:
    """
    Three-tier memory manager.

    Working memory lives in Redis (24h TTL); episodic memory and user profiles
    are persisted in ChromaDB.
    """

    WORKING_MAX   = 20    # max working-memory entries before compression kicks in
    COMPRESS_AT   = 15    # compress at this count, keeping a summary plus the last 5
    HISTORY_TOP_K = 5     # how many episodic-memory hits to return

    def __init__(
        self,
        redis_url:    str = "redis://localhost:6379/0",
        chroma_host:  str = "localhost",
        chroma_port:  int = 8000,
        chroma_path:  str = "./data/chroma",
        api_key:      str = "",
        base_url:     Optional[str] = None,
        model:        str = "claude-3-5-sonnet-20241022",
    ):
        kwargs: Dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = AsyncAnthropic(**kwargs)
        self._model  = model

        self._redis = redis.from_url(redis_url, decode_responses=True)

        # ChromaDB: prefer the standalone service (docker compose mode); fall back
        # to local embedded mode when it is unreachable
        try:
            # HttpClient initializes ChromaDB telemetry by default; disable it
            # explicitly to keep posthog compatibility errors out of the logs.
            chroma = chromadb.HttpClient(
                host=chroma_host,
                port=chroma_port,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )
            chroma.heartbeat()  # probe the connection
            logger.info(f"ChromaDB connected: {chroma_host}:{chroma_port}")
        except Exception:
            logger.info(f"ChromaDB service unavailable, using local embedded mode: {chroma_path}")
            chroma = chromadb.PersistentClient(
                path=chroma_path,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )

        # Episodic memory: stores past conversation excerpts
        self._episodic = chroma.get_or_create_collection("episodic")
        # User profile: stores distilled preferences and entities
        self._profile  = chroma.get_or_create_collection("user_profile")

    # ── Writes ────────────────────────────────────────────────────────────────

    async def add_message(
        self,
        user_id: str,
        conv_id: str,
        role:    MsgRole,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Append a message to working memory, compressing once past the threshold."""
        user_id = self._safe_text(user_id)
        conv_id = self._safe_text(conv_id)
        clean_metadata = {
            self._safe_text(k): self._safe_metadata_value(v)
            for k, v in (metadata or {}).items()
        }
        msg = Message(role=role, content=self._safe_text(content), metadata=clean_metadata)
        key = self._wm_key(user_id, conv_id)

        # Append to the Redis list (lpush, so newest sits first)
        await self._redis.lpush(key, json.dumps({
            "role":      msg.role.value,
            "content":   msg.content,
            "ts":        msg.timestamp.isoformat(),
            "metadata":  msg.metadata,
        }))
        await self._redis.expire(key, 86400)  # 24h TTL

        # Past the compression threshold, compress
        if await self._redis.llen(key) >= self.COMPRESS_AT:
            await self._compress(user_id, conv_id)

    async def update_profile(self, user_id: str, conv_id: str) -> None:
        """
        Distill user preferences from current working memory and update the profile.
        An LLM extracts the preferences; ChromaDB stores them, embedding internally
        so no external API is involved.
        """
        user_id = self._safe_text(user_id)
        conv_id = self._safe_text(conv_id)
        messages = await self._get_working_memory(user_id, conv_id)
        if not messages:
            return

        text = self._safe_text("\n".join(f"{m.role.value}: {m.content}" for m in messages[-10:]))
        prompt = f"""Extract the user's preferences and key entities from the conversation below and return JSON.
Conversation:
{text}

Response format: {{"preferences": ["..."], "entities": {{"product": [], "issue_type": []}}}}"""
        prompt = self._safe_text(prompt)

        try:
            resp = await self._client.messages.create(
                model=self._model, max_tokens=512, temperature=0.0,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = extract_text_content(resp.content)
            s, e = raw.find("{"), raw.rfind("}") + 1
            profile_data = json.loads(raw[s:e])

            doc_id = f"{user_id}_profile_{conv_id}"
            doc_text = self._safe_text(json.dumps(profile_data, ensure_ascii=False))

            try:
                await asyncio.to_thread(self._profile.delete, ids=[doc_id])
            except Exception:
                pass

            # Pass documents straight through and let ChromaDB's built-in model
            # embed them (no Voyage API dependency)
            await asyncio.to_thread(
                self._profile.add,
                ids=[doc_id],
                documents=[doc_text],
                metadatas=[{"user_id": user_id, "conv_id": conv_id,
                            "ts": datetime.now().isoformat()}],
            )
            logger.info(f"User profile updated: {user_id}")
        except Exception as ex:
            logger.warning(f"Failed to update the user profile: {ex}")

    # ── Reads ─────────────────────────────────────────────────────────────────

    async def get_context(self, user_id: str, conv_id: str, query: str = "") -> MemoryContext:
        """
        Assemble the full memory context.

        `query` drives semantic retrieval of related excerpts from episodic memory.
        """
        # 1. Working memory (recent messages of the current session)
        user_id = self._safe_text(user_id)
        conv_id = self._safe_text(conv_id)
        query = self._safe_text(query)

        recent = await self._get_working_memory(user_id, conv_id)

        # 2. Episodic memory (cross-session semantic search)
        history = await self._search_episodic(user_id, query or (recent[-1].content if recent else ""))

        # 3. User profile
        profile = await self._get_profile(user_id)

        # 4. Session summary (only if it has been compressed before)
        summary = await self._redis.get(self._summary_key(user_id, conv_id)) or ""

        return MemoryContext(
            recent_messages=recent,
            relevant_history=history,
            user_profile=profile,
            summary=summary,
        )

    # ── Compression (keeps the context window from blowing up) ────────────────

    async def _compress(self, user_id: str, conv_id: str) -> None:
        """
        Working-memory compression:
          1. Summarize the older messages with an LLM
          2. Store the summary in Redis (replacing the previous one)
          3. Push the old messages into episodic memory (ChromaDB) for later recall
          4. Keep only the last 5 messages in working memory
        """
        messages = await self._get_working_memory(user_id, conv_id)
        if len(messages) < self.COMPRESS_AT:
            return

        to_compress = messages[:-5]   # keep the last 5
        keep        = messages[-5:]

        # LLM summary
        text = self._safe_text("\n".join(f"{m.role.value}: {m.content}" for m in to_compress))
        prompt = self._safe_text(f"Summarize the key information in this conversation in 2-3 sentences:\n{text}")
        try:
            resp = await self._client.messages.create(
                model=self._model, max_tokens=256, temperature=0.0,
                messages=[{"role": "user", "content": prompt}],
            )
            summary = self._safe_text(extract_text_content(resp.content)).strip()
        except Exception:
            summary = f"Conversation of {len(to_compress)} messages (summary generation failed)"

        # Store the summary in Redis
        skey = self._summary_key(user_id, conv_id)
        old_summary = await self._redis.get(skey) or ""
        new_summary = self._safe_text(f"{old_summary}\n{summary}").strip()
        await self._redis.setex(skey, 86400, new_summary)

        # Push the old messages into episodic memory
        await self._store_episodic(user_id, conv_id, text, summary)

        # Reset working memory to the last 5 messages
        key = self._wm_key(user_id, conv_id)
        await self._redis.delete(key)
        for m in reversed(keep):
            await self._redis.lpush(key, json.dumps({
                "role": m.role.value, "content": m.content,
                "ts": m.timestamp.isoformat(), "metadata": m.metadata,
            }))
        await self._redis.expire(key, 86400)
        logger.info(f"Working memory compressed: {user_id}/{conv_id}, summary is {len(summary)} characters")

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _get_working_memory(self, user_id: str, conv_id: str) -> List[Message]:
        key  = self._wm_key(user_id, conv_id)
        raws = await self._redis.lrange(key, 0, self.WORKING_MAX - 1)
        msgs = []
        for raw in reversed(raws):  # lpush puts newest first; reverse to restore order
            d = json.loads(raw)
            msgs.append(Message(
                role=MsgRole(d["role"]),
                content=d["content"],
                timestamp=datetime.fromisoformat(d["ts"]),
                metadata=d.get("metadata", {}),
            ))
        return msgs

    async def _search_episodic(self, user_id: str, query: str) -> List[str]:
        """Semantic search over episodic memory. ChromaDB embeds internally, no external API."""
        query_text = self._safe_text(query).strip()
        if not query_text:
            return []
        try:
            # Pass query_texts directly; ChromaDB's built-in model vectorizes and matches
            results = await asyncio.to_thread(
                self._episodic.query,
                query_texts=[query_text],
                n_results=self.HISTORY_TOP_K,
                where={"user_id": self._safe_text(user_id)},
            )
            docs = results["documents"][0] if results["documents"] else []
            return [self._safe_text(doc) for doc in docs if isinstance(doc, str) and doc.strip()]
        except Exception as ex:
            logger.warning(f"Episodic memory search failed: {ex}")
            return []

    async def _store_episodic(self, user_id: str, conv_id: str, text: str, summary: str) -> None:
        """Store a compressed excerpt into episodic memory. ChromaDB embeds internally."""
        try:
            user_id = self._safe_text(user_id)
            conv_id = self._safe_text(conv_id)
            text = self._safe_text(text)
            summary = self._safe_text(summary)
            doc_id = hashlib.md5(f"{user_id}{conv_id}{time.time()}".encode()).hexdigest()
            # Pass documents directly; ChromaDB's built-in model generates the embedding
            await asyncio.to_thread(
                self._episodic.add,
                ids=[doc_id],
                documents=[summary],
                metadatas=[{"user_id": user_id, "conv_id": conv_id,
                            "ts": datetime.now().isoformat(), "full_text": self._safe_text(text[:500])}],
            )
        except Exception as ex:
            logger.warning(f"Failed to store episodic memory: {ex}")

    async def _get_profile(self, user_id: str) -> Dict[str, Any]:
        """Fetch the user profile (most recent entry)."""
        try:
            results = await asyncio.to_thread(self._profile.get, where={"user_id": user_id}, limit=1)
            if results["documents"]:
                return json.loads(results["documents"][0])
        except Exception:
            pass
        return {}

    async def close(self) -> None:
        """Close the async Redis connection."""
        await self._redis.aclose()

    @staticmethod
    def _wm_key(user_id: str, conv_id: str) -> str:
        return f"wm:{user_id}:{conv_id}"

    @staticmethod
    def _summary_key(user_id: str, conv_id: str) -> str:
        return f"summary:{user_id}:{conv_id}"

    @staticmethod
    def _safe_text(value: Any) -> str:
        """Convert to a plain UTF-8 string that ChromaDB will accept."""
        if value is None:
            return ""
        if not isinstance(value, str):
            value = str(value)
        return value.encode("utf-8", errors="ignore").decode("utf-8")

    @classmethod
    def _safe_metadata_value(cls, value: Any) -> Any:
        """Recursively sanitize metadata so later Redis/ChromaDB I/O never hits bad UTF-8."""
        if isinstance(value, str):
            return cls._safe_text(value)
        if isinstance(value, dict):
            return {cls._safe_text(k): cls._safe_metadata_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [cls._safe_metadata_value(v) for v in value]
        return value
