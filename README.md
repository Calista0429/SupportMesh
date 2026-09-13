# SupportMesh

An enterprise customer-service system built on large language models. A request
is classified, routed to a specialist agent, answered with retrieval from a
knowledge base, and remembered across turns — with monitoring and evaluation
built in rather than bolted on.

```text
user request
  -> MemoryManager     reads Redis working memory + ChromaDB episodic memory and user profile
  -> IntentRecognizer  classifies the intent (LLM + embedding + keyword patterns)
  -> KnowledgeBase     rewrites the query, recalls in parallel, reranks with an LLM
  -> AgentOrchestrator routes to the General, Technical or Billing agent
  -> LLM               generates the reply, with matching Skills injected
  -> MemoryManager     writes the turn back and updates the user profile asynchronously
```

## Layout

```text
backend/     FastAPI service — the agents, memory, retrieval, monitoring and evaluation
frontend/    Vue 3 console for driving the backend and watching how it decides
```

Each directory has its own README: [backend/README.md](backend/README.md) is the
full operations guide, [frontend/README.md](frontend/README.md) covers the console.

## What is inside the backend

| Module | Responsibility |
|---|---|
| `api/main.py` | FastAPI entry point; wires every component together |
| `core/intent_recognizer.py` | Three-way intent recognition: LLM semantics (70%), embedding similarity (20%), keyword patterns (10%), merged by weighted vote |
| `agents/agent_orchestrator.py` | Routing by intent, then by live performance, with fallback; parallel collaboration for compound questions |
| `agents/tools.py` | Deterministic agent tools (not yet wired into the orchestrator) |
| `memory/conversation_memory.py` | Three tiers: Redis working memory, ChromaDB episodic memory, ChromaDB user profile, with automatic compression |
| `mcp/tool_manager.py` | Tool calling with query rewriting, LLM reranking, a circuit breaker, a TTL cache and fallbacks |
| `mcp/knowledge_base.py` | RAG over ChromaDB |
| `monitor/performance_monitor.py` | Live metrics, Z-score anomaly detection, alerts, and routing penalties fed back to the orchestrator |
| `evaluation/evaluator.py` | Intent accuracy, LLM-as-Judge dialogue scoring, regression detection against a baseline |
| `skills/` | Hot-loadable business rules injected into an agent's system prompt |

## Requirements

- Python 3.12+
- Node 20.19+ or 22.12+ (required by Vite 7)
- Docker, for Redis and ChromaDB
- An Anthropic API key, or a key for an Anthropic-compatible API such as DeepSeek

## Quick start

Start the dependencies:

```bash
cd backend
docker compose up -d redis chromadb
```

Configure and run the backend:

```bash
cp .env.example .env        # then set ANTHROPIC_API_KEY
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
```

Run the console:

```bash
cd ../frontend
npm install
npm run dev
```

Open http://localhost:5173 . The backend answers on http://localhost:8000, with
interactive API docs at `/docs`.

## Full stack in Docker

```bash
cd backend
docker compose up -d --build
```

| Service | Host port | Purpose |
|---|---|---|
| supportmesh | 8000 | the API |
| nginx | 80 | reverse proxy |
| chromadb | 8001 | vector store |
| redis | 6379 | working memory |
| prometheus | 9090 | metrics storage |

In this mode set `CHROMA_HOST=chromadb` and `CHROMA_PORT=8000` in `.env`, since
`localhost` inside a container points at the container itself.

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/chat` | the main dialogue endpoint |
| `POST` | `/search` | retrieval only: rewrite, recall, rerank |
| `GET` | `/health` | readiness and per-agent statistics |
| `GET` | `/monitor` | agent and tool statistics, alerts, suggestions |
| `GET` | `/metrics` | Prometheus exposition format |
| `GET` | `/skills`, `POST` `/skills/reload` | inspect and hot-reload business rules |
| `POST` | `/knowledge/add`, `/knowledge/upload` | import documents |
| `GET` | `/knowledge/stats` | chunk count |
| `POST` | `/eval/run` | run the built-in evaluation |

## Configuration

`backend/.env.example` documents every variable the code reads, with defaults.
`ANTHROPIC_API_KEY` is the only one that is required — the service refuses to
start without it. Point `ANTHROPIC_BASE_URL` at a compatible provider to use one
instead of Anthropic.

Skills live in `backend/skills/` as Markdown with front matter. Edit a file and
call `POST /skills/reload`; no restart needed.
