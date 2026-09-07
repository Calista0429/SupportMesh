"""
RAG knowledge base -- a real retrieval implementation backed by ChromaDB.

What it does:
  1. Ingestion: chunk text and store it in ChromaDB (embeddings generated for you)
  2. Semantic search: retrieve the most relevant chunks for a query
  3. MCP integration: acts as the real handler behind the knowledge_search tool

ChromaDB plays two separate roles in this project:
  - memory/ uses it for conversation memory (episodic memory + user profiles)
  - here it stores knowledge-base documents for RAG retrieval
  They are different collections and do not interfere with each other.
"""
import asyncio
import hashlib
import logging
from typing import Any, Dict, List, Optional

import chromadb

logger = logging.getLogger(__name__)


class KnowledgeBase:
    """
    A RAG knowledge base backed by ChromaDB.

    ChromaDB ships with an embedding model (all-MiniLM-L6-v2): add() vectorizes
    automatically and query() does the semantic match for you, so there is no
    separate call to the Anthropic Embeddings API.
    """

    COLLECTION_NAME = "knowledge_base"

    def __init__(
        self,
        chroma_host: str = "localhost",
        chroma_port: int = 8000,
        chroma_path: str = "./data/chroma",
    ):
        # Prefer the standalone ChromaDB service: the embedding model lives on the
        # server, so the client never has to download it
        self._use_server = False
        try:
            # HttpClient initializes ChromaDB telemetry by default; disable it
            # explicitly to keep posthog compatibility errors out of the logs.
            self._client = chromadb.HttpClient(
                host=chroma_host,
                port=chroma_port,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )
            self._client.heartbeat()
            self._use_server = True
            logger.info(f"Knowledge base connected to ChromaDB: {chroma_host}:{chroma_port}")
        except Exception:
            logger.info(f"ChromaDB service unavailable, using local mode: {chroma_path}")
            self._client = chromadb.PersistentClient(
                path=chroma_path,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )

        # Do not pass embedding_function when talking to the service -- the server
        # handles it. Local mode omits it too and falls back to ChromaDB's default
        # (which triggers a model download).
        self._collection = self._client.get_or_create_collection(
            name=self.COLLECTION_NAME,
            metadata={"description": "SupportMesh RAG knowledge base"},
        )

        # Seed the default documents when the knowledge base is empty
        if self._collection.count() == 0:
            self._load_default_docs()

    # ── Document management ───────────────────────────────────────────────────

    def add_documents(self, documents: List[Dict[str, str]]) -> int:
        """
        Bulk-import documents into the knowledge base.

        documents format: [{"title": "...", "content": "..."}, ...]
        Long documents are chunked automatically (500 characters per chunk).
        """
        ids, docs, metas = [], [], []

        for doc in documents:
            title   = doc.get("title", "")
            content = doc.get("content", "")
            chunks  = self._chunk_text(content, chunk_size=500)

            for i, chunk in enumerate(chunks):
                doc_id = hashlib.md5(f"{title}_{i}_{chunk[:50]}".encode()).hexdigest()
                ids.append(doc_id)
                docs.append(chunk)
                metas.append({"title": title, "chunk_index": i, "total_chunks": len(chunks)})

        if ids:
            # ChromaDB generates the embeddings
            self._collection.add(ids=ids, documents=docs, metadatas=metas)
            logger.info(f"Imported {len(ids)} chunks into the knowledge base")

        return len(ids)

    async def add_documents_async(self, documents: List[Dict[str, str]]) -> int:
        """Async ingest. The ChromaDB client is synchronous, so it runs in a thread pool."""
        return await asyncio.to_thread(self.add_documents, documents)

    def search(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """
        Semantic search: return the chunks most relevant to `query`.

        ChromaDB vectorizes the query internally and cosine-matches it against the
        stored document vectors.
        """
        results = self._collection.query(
            query_texts=[query],
            n_results=top_k,
        )

        items = []
        if results["documents"] and results["documents"][0]:
            for doc, meta, dist in zip(
                results["documents"][0],
                results["metadatas"][0],
                results["distances"][0],
            ):
                items.append({
                    "title":    meta.get("title", ""),
                    "content":  doc,
                    "score":    round(1.0 - dist, 4),  # ChromaDB returns distance; invert to similarity
                    "chunk":    meta.get("chunk_index", 0),
                })

        return items

    async def search_async(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """Async search. The ChromaDB client is synchronous, so it runs in a thread pool."""
        return await asyncio.to_thread(self.search, query, top_k)

    @property
    def doc_count(self) -> int:
        return self._collection.count()

    async def doc_count_async(self) -> int:
        """Async chunk count."""
        return await asyncio.to_thread(self._collection.count)

    # ── MCP tool handler ──────────────────────────────────────────────────────

    async def search_handler(self, params: Dict[str, Any], context: Any) -> List[Dict]:
        """
        Registered as the handler behind an MCP tool.

        MCPToolManager.register(Tool(
            name="knowledge_search",
            handler=kb.search_handler,
            ...
        ))
        """
        query = params.get("query", "")
        top_k = params.get("top_k", 5)
        return await self.search_async(query, top_k=top_k)

    # ── Internals ─────────────────────────────────────────────────────────────

    def _chunk_text(self, text: str, chunk_size: int = 500) -> List[str]:
        """Chunk long text by chunk_size, splitting on sentence ends and newlines."""
        if len(text) <= chunk_size:
            return [text] if text.strip() else []

        chunks = []
        current = ""
        # Split on sentence boundaries
        sentences = text.replace("\n", "。").split("。")
        for sent in sentences:
            sent = sent.strip()
            if not sent:
                continue
            if len(current) + len(sent) + 1 > chunk_size:
                if current:
                    chunks.append(current)
                current = sent
            else:
                current = f"{current}。{sent}" if current else sent

        if current:
            chunks.append(current)

        return chunks

    def _load_default_docs(self) -> None:
        """Seed the default knowledge base (common customer-service questions)."""
        default_docs = [
            {
                "title": "Refund policy",
                "content": (
                    "Refund policy. "
                    "Customers may request a no-questions-asked refund within 7 days of purchase. "
                    "Once a refund request is submitted, it is reviewed within 1-3 business days. "
                    "After approval, the money is returned to the original payment method within 5-7 business days. "
                    "If the item has already shipped, the return must be completed before the refund is issued. "
                    "Return shipping is paid by the customer unless the item is faulty. "
                    "Orders older than 7 days but within 30 days need evidence of a quality problem to qualify for a refund."
                ),
            },
            {
                "title": "Order lookup",
                "content": (
                    "Order lookup guide. "
                    "Customers can check an order's status with the order number. "
                    "Order statuses are: awaiting payment, paid, shipped, in transit, delivered and completed. "
                    "If an order shows as shipped but has not arrived after 7 days, contact support to open a trace request. "
                    "Tracking information usually updates within 24 hours of dispatch. "
                    "If an order shows an error state, contact support with the order number."
                ),
            },
            {
                "title": "Account security",
                "content": (
                    "Account security. "
                    "Change your password regularly; use at least 8 characters with both letters and digits. "
                    "A forgotten password can be reset through the registered phone number or email address. "
                    "When a suspicious sign-in is detected, the account is locked automatically and a notification is sent. "
                    "Two-factor authentication can be enabled in the security settings for stronger protection. "
                    "Never share your password. Support staff will never ask you for it."
                ),
            },
            {
                "title": "Technical troubleshooting",
                "content": (
                    "Common technical problems. "
                    "App crashes: clear the cache and restart the app; if it persists, update to the latest version. "
                    "Login fails with 401: authentication failed. Check the username and password, or reset the password. "
                    "Slow page loads: check the network connection and try switching between Wi-Fi and mobile data. "
                    "Payment fails: confirm the card has sufficient funds and that online payments are enabled. "
                    "500 server error: this is a server-side problem. Retry shortly, and contact technical support if it continues."
                ),
            },
            {
                "title": "Membership and points",
                "content": (
                    "Membership points rules. "
                    "Every $1 spent earns 1 point. "
                    "Points can be redeemed against a future purchase at 100 points = $1. "
                    "Membership tiers are: Standard, Silver (over $1,000 lifetime spend) and Gold (over $5,000 lifetime spend). "
                    "Silver members receive 5% off and Gold members receive 10% off. "
                    "Points expire 1 year after they are earned. "
                    "Purchases made during your birthday month earn double points."
                ),
            },
            {
                "title": "Delivery information",
                "content": (
                    "Delivery options. "
                    "Standard delivery: 3-5 business days, free on orders over $99. "
                    "Express delivery: 1-2 business days, $15 shipping. "
                    "Local delivery: same day or next day, $10 shipping. "
                    "Remote areas may need an extra 2-3 days. "
                    "Deliveries run 09:00-18:00 daily and may be delayed on public holidays. "
                    "To change the delivery address, contact support before the order ships."
                ),
            },
        ]
        self.add_documents(default_docs)
        logger.info(f"Seeded the default knowledge base: {len(default_docs)} documents")
