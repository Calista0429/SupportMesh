# SupportMesh Frontend

SupportMesh 后端的调试控制台。一个 Vue 3 + Vite 的单页应用，用来发真实请求并观察后端如何决策。

## 功能

- **对话**：发送消息，查看识别出的意图、置信度、处理它的 Agent、是否使用了知识库、端到端耗时和路由原因。Agent 回复按 Markdown 渲染。
- **知识库**：检索知识片段，手动添加文档或上传 `.txt` / `.md` / `.json` 文件，查看已加载的 Skills 并热加载。
- **评测**：运行后端内置的评测，查看通过率、平均评分和优化建议。

## 本地运行

```bash
npm install
npm run dev
```

打开 http://localhost:5173 。

开发模式下 Vite 会把 `/api/python` 代理到 `http://localhost:8000`，也就是后端的默认地址。后端端口不同时可以覆盖：

```bash
VITE_PYTHON_API_URL=http://localhost:9000 npm run dev
```

界面右侧的"连接配置"里也可以直接改后端地址，改动会存进浏览器 localStorage。

## Docker 部署

镜像里只放构建好的静态文件，所以要先构建：

```bash
npm run build
docker compose up -d --build
```

打开 http://localhost:5174 。容器里的 nginx 会把 `/api/python` 转发到 `host.docker.internal:8000`，也就是宿主机上的后端。

## 后端接口

控制台用到的接口：`/health`、`/chat`、`/monitor`、`/skills`、`/skills/reload`、`/knowledge/stats`、`/knowledge/add`、`/knowledge/upload`、`/search`、`/eval/run`。完整文档见后端的 `/docs`。
