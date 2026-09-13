# SupportMesh Frontend

A debugging console for the SupportMesh backend: a Vue 3 + Vite single-page app
for sending real requests and watching how the backend decides.

## What it covers

- **Chat**: send a message and read the recognized intent and its confidence,
  which agent handled it, any supporting agents, whether the knowledge base was
  used, the end-to-end latency and the routing reason. Agent replies render as
  Markdown.
- **Knowledge**: search the knowledge base, add a document by hand or upload a
  `.txt`, `.md` or `.json` file, inspect the loaded Skills and hot-reload them.
- **Evaluation**: run the backend's built-in evaluation and read the pass rate,
  average scores and suggestions.

## Running locally

```bash
npm install
npm run dev
```

Open http://localhost:5173 .

In development Vite proxies `/api/python` to `http://localhost:8000`, the
backend's default address. Point it elsewhere with:

```bash
VITE_PYTHON_API_URL=http://localhost:9000 npm run dev
```

The backend URL can also be edited in the Connection card in the sidebar; it is
stored in the browser's localStorage along with the user and conversation IDs.

## Docker

The image serves the built files, so build before it:

```bash
npm run build
docker compose up -d --build
```

Open http://localhost:5174 . The container's nginx forwards `/api/python` to
`host.docker.internal:8000`, the backend running on the host.

## Backend endpoints used

`/health`, `/chat`, `/monitor`, `/skills`, `/skills/reload`, `/knowledge/stats`,
`/knowledge/add`, `/knowledge/upload`, `/search`, `/eval/run`. Full reference at
the backend's `/docs`.
