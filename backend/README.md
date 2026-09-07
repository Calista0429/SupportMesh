# SupportMesh User Guide

This guide covers deployment, startup, API usage, the knowledge base, inspecting ChromaDB data, monitoring, evaluation and common troubleshooting.

SupportMesh is an enterprise customer-service system. The main path is:

```text
user request
  -> FastAPI /chat
  -> MemoryManager reads Redis working memory + ChromaDB episodic memory + user profile
  -> IntentRecognizer classifies the intent
  -> AgentOrchestrator routes to the General/Technical/Billing agent
  -> the LLM generates a reply
  -> the reply is written to Redis and the ChromaDB user profile updates asynchronously
```

## 1. Project layout

```text
SupportMesh/
├── api/main.py                    # FastAPI entry point: /chat /search /knowledge /monitor /eval
├── core/intent_recognizer.py      # three-way fused intent recognition
├── agents/agent_orchestrator.py   # multi-agent routing and orchestration
├── memory/conversation_memory.py  # Redis + ChromaDB memory management
├── mcp/tool_manager.py            # MCP tool calls, query rewriting, reranking, breaker, cache, fallback
├── mcp/knowledge_base.py          # ChromaDB RAG knowledge base
├── monitor/performance_monitor.py # live agent and tool monitoring
├── evaluation/evaluator.py        # end-to-end evaluation
├── data/demo_docs/                # demo knowledge-base documents
├── docker-compose.yml             # full-stack Docker orchestration
├── Dockerfile
├── requirements.txt
└── .env
```

## 2. Prerequisites

### 2.1 Required

- Docker
- Docker Compose
- An Anthropic API key, or a third-party key for an Anthropic-compatible API

### 2.2 Configure `.env`

Copy the example file:

```bash
cp .env.example .env
```

At minimum you need:

```env
ANTHROPIC_API_KEY=your_api_key
```

For an Anthropic-compatible endpoint such as DeepSeek:

```env
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
ANTHROPIC_MODEL=deepseek-v4-pro
ANTHROPIC_API_KEY=your_deepseek_key
```

Under Docker Compose, the Redis and ChromaDB connections are overridden with in-container addresses by `docker-compose.yml`. You normally do not need to touch these:

```env
REDIS_PASSWORD=supportmesh123
CHROMA_HOST=localhost
CHROMA_PORT=8001
```

### 2.3 Full-stack deployment vs. run development mode

SupportMesh is usually started one of two ways: `docker compose up` for the full stack, or `docker run` for development. The key difference: **full-stack deployment starts the application and its dependencies together, while run mode usually starts a single application container and expects the dependencies to already be up**.

| Aspect | Docker Compose full stack | Docker run development mode |
|--------|--------------------------|----------------------|
| Command | `docker compose up -d --build` | `docker run ... supportmesh ...` |
| What starts | SupportMesh, Redis, ChromaDB, Prometheus, Nginx | only the container you name |
| Redis/ChromaDB | started automatically on the same network | you must run `docker compose up -d redis chromadb` first |
| Networking | created and managed by Compose | pass `--network supportmesh_supportmesh-network` yourself |
| Name resolution | the app reaches `redis` and `chromadb` directly | only after joining the same network |
| Code changes | usually need a rebuild or service restart | mount `-v "$(pwd):/workspace"` and a container restart picks them up |
| Best for | demos, integration, full deployment, the HTTP API | local development, CLI debugging, ad-hoc env overrides |
| Common pitfall | API key or dependency health check fails | forgetting Redis/ChromaDB, giving `redis:6379 Name or service not known` |

Which to pick:

- To exercise the HTTP API, Swagger, Nginx and Prometheus: use **Docker Compose full stack**.
- To debug source or the CLI with fast local iteration: use **Docker run development mode**.
- For the CLI alone, the least painful option is `docker compose run --rm supportmesh python api/main.py --cli`, which joins the Compose network for you.

## 3. Docker Compose full-stack deployment

The recommended way to bring up the complete service.

```bash
docker compose up -d --build
```

Check service status:

```bash
docker compose ps
```

Follow application logs:

```bash
docker compose logs -f supportmesh
```

Once the SupportMesh startup log appears and the health check passes, the service is ready.

Ports after startup:

| Service | Container | Host port | Container port | Purpose |
|------|--------|------------|------------|------|
| SupportMesh API | `supportmesh-app` | `8000` | `8000` | main API |
| Nginx | `supportmesh-nginx` | `80` | `80` | reverse proxy |
| ChromaDB | `supportmesh-chromadb` | `8001` | `8000` | vector database |
| Redis | `supportmesh-redis` | `6379` | `6379` | working memory |
| Prometheus | `supportmesh-prometheus` | `9090` | `9090` | monitoring data |

Health check:

```bash
curl http://localhost:8000/health
```

Swagger docs:

```text
http://localhost:8000/docs
```

Or through Nginx:

```bash
curl http://localhost/health
```

## 4. Docker run development mode

During development you can start only the dependencies with Compose, then `docker run` with the current source directory mounted.

Start Redis and ChromaDB first:

```bash
docker compose up -d redis chromadb
```

Build the image:

```bash
docker compose build --no-cache supportmesh
```

Start the HTTP service:

```bash
docker run -it --rm \
  --network supportmesh_supportmesh-network \
  -p 8000:8000 \
  -e ANTHROPIC_BASE_URL="https://api.deepseek.com/anthropic" \
  -e ANTHROPIC_API_KEY="your_key" \
  -e ANTHROPIC_MODEL="deepseek-v4-pro" \
  -e REDIS_URL="redis://:supportmesh123@redis:6379/0" \
  -e CHROMA_HOST="chromadb" \
  -e CHROMA_PORT="8000" \
  -e CHROMA_PERSIST_DIRECTORY="/workspace/data/chroma" \
  -v "$(pwd):/workspace" \
  -w /workspace \
  supportmesh
```

Interactive CLI mode:

```bash
docker run -it --rm \
  --network supportmesh_supportmesh-network \
  -e ANTHROPIC_BASE_URL="https://api.deepseek.com/anthropic" \
  -e ANTHROPIC_API_KEY="your_key" \
  -e ANTHROPIC_MODEL="deepseek-v4-pro" \
  -e REDIS_URL="redis://:supportmesh123@redis:6379/0" \
  -e CHROMA_HOST="chromadb" \
  -e CHROMA_PORT="8000" \
  -v "$(pwd):/workspace" \
  -w /workspace \
  supportmesh \
  python api/main.py --cli
```

## 5. Swagger and API overview

SupportMesh is built on FastAPI, so once the HTTP service is up you can call every endpoint from Swagger UI in the browser.

Local Swagger:

```text
http://localhost:8000/docs
```

Behind the Nginx reverse proxy:

```text
http://localhost/docs
```

In Swagger, click **Try it out** on any endpoint, fill in the parameters and hit **Execute** to call the local service. A useful order to work through:

```text
1. GET /health                confirm the service is ready
2. POST /chat                 exercise the main dialogue path
3. GET /knowledge/stats       check whether the knowledge base has data
4. POST /knowledge/upload     upload the demo knowledge-base file
5. POST /search               test retrieval, query rewriting and reranking
6. GET /monitor               inspect agent and tool metrics
7. GET /skills                list the loaded Skills
8. POST /skills/reload        reload the Skills
9. POST /eval/run             run the end-to-end evaluation
```

### 5.1 Endpoint overview

| Method | Path | Parameters | What it does | When to use it |
|------|------|----------|------|----------|
| `GET` | `/health` | none | health check, returns service status and agent stats | confirm the service is up |
| `POST` | `/chat` | JSON body | main dialogue endpoint: memory read, intent recognition, agent routing, reply generation, memory write | the main business path |
| `GET` | `/monitor` | none | agent and tool statistics, alerts and suggestions | watch live behaviour |
| `POST` | `/search` | query params | the retrieval chain: rewrite, parallel recall, dedupe, LLM rerank | test RAG retrieval |
| `GET` | `/skills` | none | currently loaded Skills, their keywords and any parse errors | verify dynamic capabilities |
| `POST` | `/skills/reload` | none | rescan the Skill directory at runtime | hot-reload after editing business rules |
| `POST` | `/knowledge/add` | JSON body | bulk-import documents into the ChromaDB knowledge base | programmatic import |
| `POST` | `/knowledge/upload` | form file | import a `.txt`, `.md` or `.json` file | manual upload |
| `GET` | `/knowledge/stats` | none | total number of knowledge-base chunks | confirm the base has data |
| `POST` | `/eval/run` | none | run the built-in intent and end-to-end dialogue evaluation | demo LLM-as-Judge |
| `GET` | `/docs` | browser | Swagger UI | browse and debug every endpoint |

### 5.2 Dynamic Skill loading

SupportMesh loads Skills from a directory, injecting business processes, service scripts and troubleshooting SOPs into an agent at runtime.

Default configuration:

```env
SUPPORTMESH_SKILLS_DIR=./skills
SUPPORTMESH_SKILLS_MAX_PROMPT_CHARS=5000
```

Recommended layout:

```text
skills/refund/SKILL.md
skills/customer_support/SKILL.md
```

`SKILL.md` example:

```markdown
---
name: Refund handling standards
description: Customer-service rules for refund scenarios
keywords: refund,refunds,refunded,money back
agents: billing,general
enabled: true
---

# Refund handling standards

- Confirm the order number and payment method first.
- Any actual refund goes to human review.
```

Check what was loaded:

```bash
curl http://localhost:8000/skills
```

Hot-reload after editing a Skill file:

```bash
curl -X POST http://localhost:8000/skills/reload
```

### 5.3 `/health`

Purpose: confirm the service finished initializing.

```bash
curl http://localhost:8000/health
```

Example response:

```json
{
  "status": "ok",
  "agents": {
    "general_0": {
      "total": 0,
      "success_rate": 1.0,
      "avg_ms": 0.0,
      "monitor_penalty": 0.0,
      "routing_score": 1.0
    }
  }
}
```

### 5.4 `/chat`

Purpose: the main dialogue endpoint.

Request body:

```json
{
  "message": "I want a refund",
  "user_id": "user_001",
  "conv_id": "session_001"
}
```

Fields:

| Field | Required | Description |
|------|------|------|
| `message` | yes | user input |
| `user_id` | no | user ID, defaults to `anonymous` |
| `conv_id` | no | conversation ID, generated when omitted |

Response fields:

| Field | Description |
|------|------|
| `conv_id` | conversation ID |
| `response` | the agent's reply |
| `intent` | recognized intent |
| `agent_type` | the agent that actually handled the request |
| `escalated` | whether escalation was triggered |
| `latency_ms` | end-to-end latency |

### 5.5 `/search`

Purpose: exercise MCP tool calls and the RAG retrieval chain.

Query parameters:

| Parameter | Required | Default | Description |
|------|------|--------|------|
| `query` | yes | none | the search question |
| `top_k` | no | `5` | number of results |

Example:

```bash
curl -X POST "http://localhost:8000/search?query=how%20long%20does%20a%20refund%20take&top_k=3"
```

### 5.6 `/knowledge/add`

Purpose: bulk-import knowledge-base documents as JSON.

Request body:

```json
{
  "documents": [
    {
      "title": "Refund policy",
      "content": "Customers may request a no-questions-asked refund within 7 days of purchase..."
    }
  ]
}
```

### 5.7 `/knowledge/upload`

Purpose: import a knowledge-base document by file upload.

Supported formats:

| Format | Description |
|------|------|
| `.txt` | the whole file becomes one document |
| `.md` | the whole file becomes one document |
| `.json` | a JSON array shaped `[{ "title": "...", "content": "..." }]` |

Example:

```bash
curl -X POST http://localhost:8000/knowledge/upload \
  -F "file=@data/demo_docs/sample_knowledge.json"
```

### 5.8 `/knowledge/stats`

Purpose: report the number of knowledge-base chunks.

```bash
curl http://localhost:8000/knowledge/stats
```

### 5.9 `/monitor`

Purpose: inspect live agent and tool metrics.

```bash
curl http://localhost:8000/monitor
```

The response contains:

| Field | Description |
|------|------|
| `agent_stats` | agent call count, success rate, latency, routing_score |
| `tool_stats` | tool call count, success rate, latency, breaker state |
| `active_alerts` | recent alerts |
| `suggestions` | improvement suggestions |

### 5.10 `/eval/run`

Purpose: run the built-in evaluation.

```bash
curl -X POST http://localhost:8000/eval/run
```

The response contains:

| Field | Description |
|------|------|
| `pass_rate` | evaluation pass rate |
| `total` | number of cases |
| `passed` | number that passed |
| `avg_scores` | average scores |
| `regressions` | regression findings |
| `recommendations` | improvement suggestions |
| `results` | per-case results |

## 6. Using the system

### 6.1 The main dialogue endpoint

Request:

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "When will my order arrive?",
    "user_id": "user_001",
    "conv_id": "session_001"
  }'
```

Example response:

```json
{
  "conv_id": "session_001",
  "response": "Please send me the order number and I can check the order status and delivery progress for you.",
  "intent": "query",
  "agent_type": "general",
  "escalated": false,
  "latency_ms": 1234.5
}
```

Fields:

| Field | Meaning |
|------|------|
| `message` | user input |
| `user_id` | unique user identifier; isolates memory and profile |
| `conv_id` | conversation ID; the same value continues one multi-turn dialogue |
| `intent` | recognized intent |
| `agent_type` | the agent that actually handled the request |
| `escalated` | whether escalation or human handoff fired |
| `latency_ms` | end-to-end latency |

### 6.2 Multi-turn dialogue

A multi-turn dialogue only needs the same `user_id` and `conv_id`.

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "The order number is A123456",
    "user_id": "user_001",
    "conv_id": "session_001"
  }'
```

The system reads the recent messages of this session from Redis, plus related history and the user profile from ChromaDB, and assembles them into the agent's context.

### 6.3 Technical question

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "The app keeps failing to log in with a 401 error",
    "user_id": "user_tech",
    "conv_id": "tech_001"
  }'
```

This should route to the `technical` agent.

### 6.4 Billing question

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Why was I charged twice this month? I want a refund",
    "user_id": "user_bill",
    "conv_id": "bill_001"
  }'
```

This should route to the `billing` agent.

### 6.5 Compound question

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Login returns a 401, and I was also charged twice this month",
    "user_id": "user_mix",
    "conv_id": "mix_001"
  }'
```

A question like this triggers parallel multi-agent collaboration: the technical and billing agents each handle their part and the replies are merged.

## 7. Using the knowledge base

The knowledge base lives in `mcp/knowledge_base.py`, backed by a ChromaDB collection:

```text
knowledge_base
```

On first startup, if the knowledge base is empty, a default set of customer-service documents is imported: refund policy, order lookup, account security, technical troubleshooting, loyalty points and delivery.

### 7.1 Knowledge-base statistics

```bash
curl http://localhost:8000/knowledge/stats
```

Example response:

```json
{
  "total_chunks": 18
}
```

### 7.2 Bulk document import

```bash
curl -X POST http://localhost:8000/knowledge/add \
  -H "Content-Type: application/json" \
  -d '{
    "documents": [
      {
        "title": "Returns and refunds policy",
        "content": "Customers may request a no-questions-asked return within 7 days of purchase; once approved, the refund is issued within 5-7 business days."
      },
      {
        "title": "Membership benefits",
        "content": "Gold members receive 10% off, and purchases during their birthday month earn double points."
      }
    ]
  }'
```

Long documents are split into roughly 500-character chunks and written to ChromaDB.

### 7.3 Import by file upload

Upload Markdown:

```bash
curl -X POST http://localhost:8000/knowledge/upload \
  -F "file=@data/demo_docs/troubleshooting.md"
```

Upload JSON:

```bash
curl -X POST http://localhost:8000/knowledge/upload \
  -F "file=@data/demo_docs/sample_knowledge.json"
```

The JSON must be an array:

```json
[
  {
    "title": "document title",
    "content": "document body"
  }
]
```

### 7.4 Searching the knowledge base

```bash
curl -X POST "http://localhost:8000/search?query=how%20long%20does%20a%20refund%20take&top_k=3"
```

Example response:

```json
{
  "query": "how long does a refund take",
  "results": [
    {
      "title": "Refund policy",
      "content": "After approval, the money is returned to the original payment method within 5-7 business days.",
      "score": 0.82,
      "chunk": 0
    }
  ],
  "reranked": true
}
```

`/search` runs the full retrieval chain:

```text
original query
  -> LLM rewrites it from several angles
  -> sub-queries hit ChromaDB in parallel
  -> merge and deduplicate
  -> LLM rerank
  -> return Top-K
```

## 8. How ChromaDB is used

SupportMesh uses three ChromaDB collections:

| Collection | Module | Purpose |
|------------|------|------|
| `knowledge_base` | `mcp/knowledge_base.py` | RAG knowledge-base chunks |
| `episodic` | `memory/conversation_memory.py` | summaries of compressed past dialogue |
| `user_profile` | `memory/conversation_memory.py` | user profile: preferences and key entities |

When each is written:

| Data | Written when |
|------|----------|
| `knowledge_base` | default documents on startup, or via `/knowledge/add` and `/knowledge/upload` |
| `episodic` | automatically, once the session's working memory passes the compression threshold |
| `user_profile` | asynchronously after every `/chat` reply |

## 9. Inspecting ChromaDB inside Docker

The ChromaDB container in Compose is named:

```text
supportmesh-chromadb
```

Host port:

```text
http://localhost:8001
```

Container port:

```text
http://localhost:8000
```

### 9.1 Check that ChromaDB is alive

From the host:

```bash
curl http://localhost:8001/api/v1/heartbeat
```

From inside the container:

```bash
docker exec -it supportmesh-chromadb curl http://localhost:8000/api/v1/heartbeat
```

### 9.2 List all collections

```bash
curl http://localhost:8001/api/v1/collections
```

If your ChromaDB version returns a tenant/database error, use the Python client instead -- see the next section.

### 9.3 List collections with the Python client

Enter the application container:

```bash
docker exec -it supportmesh-app bash
```

Then run:

```bash
python - <<'PY'
import chromadb

client = chromadb.HttpClient(host="chromadb", port=8000)
print("heartbeat:", client.heartbeat())

collections = client.list_collections()
print("collections:")
for c in collections:
    print("-", c.name, "count=", c.count())
PY
```

You should see:

```text
collections:
- knowledge_base count= ...
- episodic count= ...
- user_profile count= ...
```

### 9.4 Inspect `knowledge_base` documents

```bash
docker exec -it supportmesh-app bash
```

Run:

```bash
python - <<'PY'
import chromadb

client = chromadb.HttpClient(host="chromadb", port=8000)
col = client.get_collection("knowledge_base")

data = col.get(limit=10, include=["documents", "metadatas"])
for i, doc_id in enumerate(data["ids"]):
    print("=" * 80)
    print("id:", doc_id)
    print("metadata:", data["metadatas"][i])
    print("document:", data["documents"][i][:500])
PY
```

### 9.5 Query `knowledge_base`

```bash
docker exec -it supportmesh-app bash
```

Run:

```bash
python - <<'PY'
import chromadb

client = chromadb.HttpClient(host="chromadb", port=8000)
col = client.get_collection("knowledge_base")

result = col.query(
    query_texts=["how long does a refund take"],
    n_results=3,
    include=["documents", "metadatas", "distances"],
)

for doc, meta, dist in zip(
    result["documents"][0],
    result["metadatas"][0],
    result["distances"][0],
):
    print("=" * 80)
    print("title:", meta.get("title"))
    print("distance:", dist)
    print("content:", doc[:300])
PY
```

### 9.6 Inspect the `user_profile` collection

Call `/chat` a few times first, so the system generates a profile asynchronously:

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "I often ask about membership points and refunds; please keep answers concise", "user_id": "profile_user", "conv_id": "profile_session"}'
```

Wait a few seconds, then look:

```bash
docker exec -it supportmesh-app bash
```

```bash
python - <<'PY'
import json
import chromadb

client = chromadb.HttpClient(host="chromadb", port=8000)
col = client.get_collection("user_profile")

data = col.get(
    where={"user_id": "profile_user"},
    include=["documents", "metadatas"],
)

for i, doc in enumerate(data["documents"]):
    print("=" * 80)
    print("metadata:", data["metadatas"][i])
    print(json.dumps(json.loads(doc), ensure_ascii=False, indent=2))
PY
```

### 9.7 Inspect the `episodic` collection

Episodic memory is only written once the session reaches the compression threshold -- `MemoryManager.COMPRESS_AT`, currently 15 messages.

Send several messages in a row to trigger compression:

```bash
for i in $(seq 1 16); do
  curl -s -X POST http://localhost:8000/chat \
    -H "Content-Type: application/json" \
    -d "{\"message\": \"This is test message $i, I want to ask about refunds and orders\", \"user_id\": \"episodic_user\", \"conv_id\": \"episodic_session\"}" > /dev/null
done
```

Inspect episodic memory:

```bash
docker exec -it supportmesh-app bash
```

```bash
python - <<'PY'
import chromadb

client = chromadb.HttpClient(host="chromadb", port=8000)
col = client.get_collection("episodic")

data = col.get(
    where={"user_id": "episodic_user"},
    include=["documents", "metadatas"],
)

for i, doc in enumerate(data["documents"]):
    print("=" * 80)
    print("metadata:", data["metadatas"][i])
    print("summary:", doc)
PY
```

### 9.8 Inspect the ChromaDB persistence files

The ChromaDB volume is declared in Compose as:

```yaml
volumes:
  chromadb-data:
```

List the Docker volume:

```bash
docker volume ls | grep chromadb
docker volume inspect supportmesh_chromadb-data
```

Look at the data directory inside the container:

```bash
docker exec -it supportmesh-chromadb sh
ls -lah /chroma/chroma
find /chroma/chroma -maxdepth 2 -type f | head
```

Do not edit these files directly. Use the ChromaDB API or the Python client to inspect and manage data.

### 9.9 Wipe ChromaDB data

Destructive. Stop the services and delete the volume:

```bash
docker compose down
docker volume rm supportmesh_chromadb-data
docker compose up -d --build
```

To drop a single collection, use the Python client:

```bash
docker exec -it supportmesh-app bash
```

```bash
python - <<'PY'
import chromadb

client = chromadb.HttpClient(host="chromadb", port=8000)
client.delete_collection("knowledge_base")
print("deleted knowledge_base")
PY
```

After dropping a collection and restarting, `KnowledgeBase` re-imports the default documents when it finds the collection empty.

## 10. Inspecting Redis working memory

Redis container:

```text
supportmesh-redis
```

Enter Redis:

```bash
docker exec -it supportmesh-redis redis-cli -a supportmesh123
```

List keys:

```redis
KEYS *
```

Working-memory key format:

```text
wm:{user_id}:{conv_id}
```

Session-summary key format:

```text
summary:{user_id}:{conv_id}
```

Read the recent messages of a session:

```redis
LRANGE wm:user_001:session_001 0 -1
```

Check the TTL:

```redis
TTL wm:user_001:session_001
```

The default TTL is 24 hours.

## 11. Inspecting compressed working memory

Compression happens in `memory/conversation_memory.py`. Defaults:

```text
WORKING_MAX = 20
COMPRESS_AT = 15
```

Once working memory for one `user_id + conv_id` reaches 15 messages, the system does this:

```text
old messages     -> LLM summary          -> Redis summary
summary of those -> ChromaDB episodic
last 5 messages  -> stay in the Redis wm list
```

Sample log line:

```text
Working memory compressed: cli_user/5a076f2b-b607-4339-9e9f-f0399862d366, summary is 19 characters
```

Where:

```text
user_id = cli_user
conv_id = 5a076f2b-b607-4339-9e9f-f0399862d366
```

### 11.1 Read the session summary from Redis

Enter Redis:

```bash
docker exec -it supportmesh-redis redis-cli -a supportmesh123
```

Query the summary:

```redis
GET summary:cli_user:5a076f2b-b607-4339-9e9f-f0399862d366
```

Or as a one-liner:

```bash
docker exec -it supportmesh-redis redis-cli -a supportmesh123 \
  GET summary:cli_user:5a076f2b-b607-4339-9e9f-f0399862d366
```

### 11.2 Read the last 5 messages kept after compression

Inside Redis:

```redis
LRANGE wm:cli_user:5a076f2b-b607-4339-9e9f-f0399862d366 0 -1
```

Or as a one-liner:

```bash
docker exec -it supportmesh-redis redis-cli -a supportmesh123 \
  LRANGE wm:cli_user:5a076f2b-b607-4339-9e9f-f0399862d366 0 -1
```

Notes:

- Redis writes with `LPUSH`, so the newest message sits at the head of the list.
- The code calls `reversed(raws)` on read to restore chronological order.
- After compression the Redis working-memory list keeps only the last 5 messages; everything older survives as a summary in Redis `summary` and ChromaDB `episodic`.

### 11.3 Read episodic summaries from ChromaDB

In a full-stack deployment the application container is usually:

```text
supportmesh-app
```

Enter the application container:

```bash
docker exec -it supportmesh-app bash
```

If you ran the CLI with `docker run --rm`, the container name is random. Find it first:

```bash
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Networks}}\t{{.Status}}'
```

Then enter it:

```bash
docker exec -it <container-name> bash
```

Run a Python snippet to query `episodic`:

```bash
python - <<'PY'
import chromadb

user_id = "cli_user"
conv_id = "5a076f2b-b607-4339-9e9f-f0399862d366"

client = chromadb.HttpClient(host="chromadb", port=8000)
col = client.get_collection("episodic")

data = col.get(
    where={"user_id": user_id},
    include=["documents", "metadatas"],
)

for i, doc in enumerate(data["documents"]):
    meta = data["metadatas"][i]
    if meta.get("conv_id") == conv_id:
        print("=" * 80)
        print("metadata:", meta)
        print("summary:", doc)
        print("full_text_preview:", meta.get("full_text"))
PY
```

Fields:

| Field | Meaning |
|------|------|
| `documents[i]` | the LLM-generated summary of past dialogue |
| `metadata.user_id` | user ID |
| `metadata.conv_id` | conversation ID |
| `metadata.ts` | write timestamp |
| `metadata.full_text` | first 500 characters of the original messages that were compressed |

### 11.4 Listing all episodic memory for one user

```bash
docker exec -it supportmesh-app bash
```

```bash
python - <<'PY'
import chromadb

user_id = "cli_user"

client = chromadb.HttpClient(host="chromadb", port=8000)
col = client.get_collection("episodic")

data = col.get(
    where={"user_id": user_id},
    include=["documents", "metadatas"],
)

for i, doc in enumerate(data["documents"]):
    print("=" * 80)
    print("metadata:", data["metadatas"][i])
    print("summary:", doc)
PY
```

### 11.5 Redis summary vs. ChromaDB episodic

| Location | Holds | Purpose |
|------|----------|------|
| Redis `summary:{user_id}:{conv_id}` | the compressed summary of this session | spliced straight into the prompt on the next request in the same session |
| ChromaDB `episodic` | summary plus metadata | semantic retrieval of related history across sessions |
| Redis `wm:{user_id}:{conv_id}` | the last 5 messages | keeps the current dialogue coherent |

## 12. Live monitoring

Fetch the monitoring summary:

```bash
curl http://localhost:8000/monitor
```

The response contains:

```json
{
  "agent_stats": {
    "general_0": {
      "total": 10,
      "success_rate": 1.0,
      "avg_ms": 1200.3,
      "monitor_penalty": 0.0,
      "routing_score": 0.836
    }
  },
  "tool_stats": {
    "knowledge_search": {
      "total": 5,
      "success_rate": 1.0,
      "avg_latency_ms": 80.2,
      "consecutive_fails": 0,
      "circuit_state": "closed"
    }
  },
  "active_alerts": [],
  "suggestions": []
}
```

What the metrics mean:

| Metric | Meaning |
|------|------|
| `total` | number of calls |
| `success_rate` | success rate |
| `avg_ms` / `avg_latency_ms` | average latency |
| `routing_score` | the agent's routing score |
| `monitor_penalty` | penalty factor written back by the Monitor from live performance |
| `consecutive_fails` | consecutive tool failures |
| `circuit_state` | tool breaker state: `closed`, `open` or `half_open` |

Prometheus UI:

```text
http://localhost:9090
```

## 13. Running the end-to-end evaluation

```bash
curl -X POST http://localhost:8000/eval/run
```

What it evaluates:

1. Intent accuracy and Macro-F1
2. Real replies generated through the Orchestrator
3. LLM-as-Judge scores for relevance, accuracy, completeness and helpfulness
4. Regression detection against the previous run
5. Improvement suggestions

Example response:

```json
{
  "pass_rate": 0.83,
  "total": 5,
  "passed": 4,
  "avg_scores": {
    "intent_accuracy": 0.875,
    "relevance": 0.88,
    "accuracy": 0.82,
    "completeness": 0.79,
    "helpfulness": 0.85
  },
  "regressions": [],
  "recommendations": [
    "Intent accuracy below 90%: add more few-shot examples, or more training data for the low-F1 intent categories"
  ],
  "results": []
}
```

## 14. Stopping, restarting and cleaning up

Stop the services:

```bash
docker compose stop
```

Restart:

```bash
docker compose restart supportmesh
```

Stop and remove containers, keeping the volumes:

```bash
docker compose down
```

Stop and remove containers and volumes:

```bash
docker compose down -v
```

Rebuild and start:

```bash
docker compose up -d --build
```

## 15. Troubleshooting

### 15.1 `/health` returns 503

Check the application logs:

```bash
docker compose logs -f supportmesh
```

Look for:

- Is `ANTHROPIC_API_KEY` set in `.env`?
- Is Redis healthy?
- Is ChromaDB healthy?
- Is the application container restarting in a loop?

### 15.2 ChromaDB connection failure

Check ChromaDB status:

```bash
docker compose ps chromadb
docker compose logs -f chromadb
curl http://localhost:8001/api/v1/heartbeat
```

Test from inside the application container:

```bash
docker exec -it supportmesh-app bash
python - <<'PY'
import chromadb
client = chromadb.HttpClient(host="chromadb", port=8000)
print(client.heartbeat())
PY
```

### 15.3 Redis authentication failure

Make sure `.env` and `docker-compose.yml` agree on the password. The default is:

```text
supportmesh123
```

Test the connection:

```bash
docker exec -it supportmesh-redis redis-cli -a supportmesh123 ping
```

### 15.4 `/search` returns nothing

First confirm the knowledge base has data:

```bash
curl http://localhost:8000/knowledge/stats
```

If it reports 0, re-import the demo documents:

```bash
curl -X POST http://localhost:8000/knowledge/upload \
  -F "file=@data/demo_docs/sample_knowledge.json"
```

Then try again:

```bash
curl -X POST "http://localhost:8000/search?query=how%20do%20I%20integrate%20the%20API&top_k=3"
```

### 15.5 The user profile is missing

User profiles update asynchronously and depend on a successful LLM call. To debug:

1. Call `/chat` with a fixed `user_id`
2. Wait a few seconds
3. Check `docker compose logs -f supportmesh` for the `User profile updated` line
4. Query `user_profile` with the Python snippet from section 9.6

### 15.6 Episodic memory is missing

Episodic memory is not written on every turn -- only once the session reaches the compression threshold. The default:

```text
MemoryManager.COMPRESS_AT = 15
```

Send more than 16 messages, then check `episodic` again.

## 16. Recommended verification run

A full check, in order:

```bash
# 1. Start
docker compose up -d --build

# 2. Health check
curl http://localhost:8000/health

# 3. Main dialogue
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Hello, I would like to know about the refund policy", "user_id": "demo_user", "conv_id": "demo_conv"}'

# 4. Knowledge-base statistics
curl http://localhost:8000/knowledge/stats

# 5. Import the demo knowledge base
curl -X POST http://localhost:8000/knowledge/upload \
  -F "file=@data/demo_docs/sample_knowledge.json"

# 6. Search
curl -X POST "http://localhost:8000/search?query=how%20do%20I%20integrate%20the%20SupportMesh%20API&top_k=3"

# 7. Monitoring
curl http://localhost:8000/monitor

# 8. Skills
curl http://localhost:8000/skills

# 9. Evaluation
curl -X POST http://localhost:8000/eval/run
```
