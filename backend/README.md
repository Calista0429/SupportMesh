# SupportMesh Backend

The FastAPI service: intent recognition, agent routing, retrieval, memory,
monitoring and evaluation. For the system overview see the
[root README](../README.md); for the debugging console see
[frontend/README.md](../frontend/README.md).

## Layout

```text
api/main.py                    FastAPI entry point, wires everything together
core/intent_recognizer.py      three-way intent recognition
core/skill_loader.py           hot-loadable business rules
agents/agent_orchestrator.py   routing, fallback, parallel collaboration
agents/tools.py                deterministic agent tools (not yet wired in)
memory/conversation_memory.py  Redis working memory + ChromaDB long-term memory
mcp/tool_manager.py            tool calls: rewrite, rerank, breaker, cache
mcp/knowledge_base.py          RAG over ChromaDB
monitor/performance_monitor.py metrics, anomaly detection, routing feedback
evaluation/evaluator.py        intent accuracy, LLM-as-Judge, regressions
skills/                        business rules injected into system prompts
data/demo_docs/                seed documents for the knowledge base
```

## Requirements

- Python 3.12+
- Docker, for Redis and ChromaDB
- An Anthropic API key, or a key for an Anthropic-compatible API

## Configuration

```bash
cp .env.example .env
```

`ANTHROPIC_API_KEY` is the only required variable; the service refuses to start
without it. `.env.example` documents all 18 variables the code reads, with
defaults. The ones worth knowing:

| Variable | Purpose |
|---|---|
| `ANTHROPIC_BASE_URL` | point at a compatible provider, e.g. `https://api.deepseek.com/anthropic` |
| `ANTHROPIC_MODEL` | defaults to `claude-3-5-sonnet-20241022` |
| `REDIS_URL` | working memory; `redis://:password@host:6379/0` |
| `CHROMA_HOST`, `CHROMA_PORT` | vector store; `localhost:8001` locally, `chromadb:8000` in Compose |
| `SUPPORTMESH_SKILLS_DIR` | where Skills are loaded from |
| `PROMETHEUS_PORT` | optional extra exporter port; metrics are always on `/metrics` |

**In Docker, `CHROMA_HOST` must be `chromadb`.** `localhost` inside a container
points at the container itself, and the code then degrades silently to an
embedded store — data lands inside the container instead of the ChromaDB
service.

## Running

Dependencies first, either way:

```bash
docker compose up -d redis chromadb
```

Local development:

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
```

Or the whole stack in containers:

```bash
docker compose up -d --build
```

| Service | Host port |
|---|---|
| supportmesh (API) | 8000 |
| nginx | 80 |
| chromadb | 8001 |
| redis | 6379 |
| prometheus | 9090 |

`docker-deploy.sh` wraps the Compose workflow with install, start, stop, health,
backup and restore subcommands.

## API

Interactive docs at `/docs`.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/chat` | the main dialogue endpoint |
| `POST` | `/search` | retrieval only: rewrite, parallel recall, rerank |
| `GET` | `/health` | readiness and per-agent statistics |
| `GET` | `/monitor` | agent and tool statistics, alerts, suggestions |
| `GET` | `/metrics` | Prometheus exposition format |
| `GET` | `/skills` | loaded Skills, their keywords and parse errors |
| `POST` | `/skills/reload` | rescan the Skill directory, no restart needed |
| `POST` | `/knowledge/add` | import documents as JSON |
| `POST` | `/knowledge/upload` | import a `.txt`, `.md` or `.json` file (max 10MB) |
| `GET` | `/knowledge/stats` | chunk count |
| `POST` | `/eval/run` | run the built-in evaluation |

`/chat` takes `message`, plus optional `user_id` and `conv_id`. Reusing a
`conv_id` continues a conversation; reusing a `user_id` carries the profile
across conversations.

```bash
curl -X POST http://localhost:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"message": "How long does a refund take?", "user_id": "u1001"}'
```

## How memory is stored

Three tiers, and it helps to know which is which when debugging:

| Where | Key or collection | Holds |
|---|---|---|
| Redis | `wm:{user_id}:{conv_id}` | the recent turns, newest first (LPUSH), 24h TTL |
| Redis | `summary:{user_id}:{conv_id}` | the compressed summary of this conversation |
| ChromaDB | `episodic` | summaries of past conversations, searched semantically |
| ChromaDB | `user_profile` | preferences and entities distilled per user |
| ChromaDB | `knowledge_base` | the RAG corpus |

Once a conversation reaches 15 messages the older ones are summarized into Redis
`summary` and pushed into `episodic`, and only the last 5 stay in `wm`.

To look at the data:

```bash
docker exec -it supportmesh-redis redis-cli -a "$REDIS_PASSWORD" keys 'wm:*'
curl -s http://localhost:8001/api/v2/heartbeat
```

For ChromaDB contents, query it from the application container or a venv:

```python
import chromadb
client = chromadb.HttpClient(host="localhost", port=8001)
client.get_collection("knowledge_base").get(limit=3)
```

The knowledge base seeds six default documents **only when the collection is
empty**. If you change the seeds, drop the collection and restart, otherwise the
old content stays.

## Monitoring and evaluation

`/monitor` returns per-agent success rate, average latency and routing score,
plus tool statistics, active alerts and suggestions. The monitor writes routing
penalties back to the orchestrator, so a slow or failing agent is routed around
automatically.

`POST /eval/run` scores intent accuracy (accuracy and Macro-F1) and dialogue
quality (LLM-as-Judge over relevance, accuracy, completeness and helpfulness),
then compares against `data/eval/baseline.json` and reports regressions. The
baseline is overwritten by each run.

## Skills

Business rules live in `skills/<name>/SKILL.md` with front matter for `name`,
`description`, `keywords`, `agents` and `enabled`. A Skill is injected into an
agent's system prompt when the user's message matches one of its keywords;
matching is word-boundary aware, so `bill` does not fire on `billing`. Edit a
file and `POST /skills/reload` — no restart. See [skills/README.md](skills/README.md).

## Troubleshooting

| Symptom | Usual cause |
|---|---|
| `/health` returns 503 | `ANTHROPIC_API_KEY` missing, or Redis/ChromaDB unreachable — check the startup log |
| ChromaDB connection failure | wrong `CHROMA_HOST` for the run mode; the fallback to embedded mode is silent |
| Redis authentication failure | `.env` and the Redis server disagree on the password |
| `/search` returns nothing | empty knowledge base — check `/knowledge/stats` and import documents |
| User profile missing | it updates asynchronously after a reply and needs a successful LLM call |
| Episodic memory missing | written only after a conversation passes the 15-message compression threshold |

Application logs are the fastest way in:

```bash
docker compose logs -f supportmesh
```
