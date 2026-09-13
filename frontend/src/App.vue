<template>
  <main class="app-shell">
    <header class="topbar">
      <a class="brand" href="#" aria-label="SupportMesh 首页" @click.prevent="activeView = 'chat'">
        <span class="brand-mark">S</span>
        <span class="brand-name">SupportMesh</span>
      </a>

      <nav class="view-nav" aria-label="工作区">
        <button :class="{ active: activeView === 'chat' }" @click="activeView = 'chat'">对话</button>
        <button :class="{ active: activeView === 'knowledge' }" @click="activeView = 'knowledge'">知识库</button>
        <button :class="{ active: activeView === 'evaluation' }" @click="activeView = 'evaluation'">评测</button>
      </nav>

      <div class="topbar-tools">
        <span class="environment-pill">
          <i :class="healthOk ? 'online' : 'offline'"></i>
          {{ healthLabel }}
        </span>
        <a class="docs-link" :href="docsUrl" target="_blank" rel="noreferrer">API 文档</a>
      </div>
    </header>

    <div v-if="toast" class="toast" role="status">{{ toast }}</div>

    <section v-if="activeView === 'chat'" class="page page-chat">
      <div class="page-heading">
        <div class="heading-copy">
          <span class="kicker">Conversation lab</span>
          <h1>和客服 Agent 对话</h1>
          <p>发送一条真实请求，查看它如何识别意图、选择 Agent 并生成回复。</p>
        </div>
        <div class="heading-actions">
          <span class="session-label">{{ settings.conversationId || '新会话' }}</span>
          <button class="quiet-button" @click="clearConversation">清空</button>
        </div>
      </div>

      <div class="chat-layout">
        <section class="chat-stage">
          <div class="stage-bar">
            <div class="stage-context">
              <span class="context-dot"></span>
              <span>{{ endpoint }}</span>
            </div>
            <span>{{ messages.length }} 条消息</span>
          </div>

          <div class="messages" ref="messageList">
            <article v-for="item in messages" :key="item.id" :class="['message', item.role]">
              <div class="message-meta">
                <span>{{ item.role === 'user' ? '你' : 'Agent' }}</span>
                <small v-if="item.meta">{{ item.meta }}</small>
              </div>
              <p v-if="item.role === 'user'">{{ item.content }}</p>
              <!-- renderMarkdown escapes first, so only its own tags reach the DOM. -->
              <div v-else class="message-body" v-html="renderMarkdown(item.content)"></div>
            </article>

            <div v-if="messages.length === 0" class="empty-state">
              <div class="empty-symbol">✦</div>
              <h2>从一个客户问题开始</h2>
              <p>下面的快捷问题只是起点，你也可以直接输入自己的测试用例。</p>
              <div class="starter-prompts">
                <button @click="usePrompt('I want a refund for order #12345')">退款申请</button>
                <button @click="usePrompt('Login keeps returning a 401 error, how do I fix it?')">技术排查</button>
                <button @click="usePrompt('How long until my invoice is issued?')">发票咨询</button>
              </div>
            </div>
          </div>

          <form class="composer" @submit.prevent="sendMessage">
            <textarea
              v-model="draft"
              rows="3"
              placeholder="输入消息..."
              @keydown.meta.enter.prevent="sendMessage"
              @keydown.ctrl.enter.prevent="sendMessage"
            ></textarea>
            <div class="composer-bottom">
              <span>⌘ / Ctrl + Enter 发送</span>
              <button type="submit" :disabled="busy || !draft.trim()">{{ busy ? '处理中' : '发送' }}</button>
            </div>
          </form>
        </section>

        <aside class="chat-sidebar">
          <div class="chat-sidebar-scroll">
            <section class="side-card session-card">
              <div class="card-heading">
                <div>
                  <span class="kicker">Session</span>
                  <h2>会话信息</h2>
                </div>
                <span class="status-copy muted">{{ settings.conversationId ? '已启用' : '新会话' }}</span>
              </div>
              <div class="session-grid">
                <div>
                  <span>会话 ID</span>
                  <strong>{{ settings.conversationId || '自动生成' }}</strong>
                </div>
                <div>
                  <span>用户 ID</span>
                  <strong>{{ settings.userId || 'anonymous' }}</strong>
                </div>
              </div>
            </section>

            <section class="side-card connection-card">
              <div class="card-heading">
                <div>
                  <span class="kicker">Connection</span>
                  <h2>连接配置</h2>
                </div>
                <span class="status-copy" :class="healthOk ? 'success' : 'muted'">{{ healthLabel }}</span>
              </div>

              <label>
                <span>后端地址</span>
                <input v-model="settings.endpoint" @change="onEndpointChange" placeholder="/api/python" />
              </label>
              <label>
                <span>用户 ID</span>
                <input v-model="settings.userId" @change="persist" placeholder="u1001" />
              </label>
              <label>
                <span>会话 ID</span>
                <input v-model="settings.conversationId" @change="persist" placeholder="自动生成" />
              </label>
              <div class="side-actions">
                <button @click="checkHealth">检查连接</button>
                <button class="quiet-button" @click="refreshConsole">刷新</button>
              </div>
            </section>

            <section class="side-card trace-card">
              <div class="card-heading">
                <div>
                  <span class="kicker">Last trace</span>
                  <h2>最近一次请求</h2>
                </div>
                <span class="trace-status" :class="lastResponse ? 'has-data' : ''"></span>
              </div>

              <div v-if="lastResponse" class="trace-body">
                <div class="latency">
                  <span>响应耗时</span>
                  <strong>{{ Math.round(lastResponse.latencyMs) || '-' }}<small> ms</small></strong>
                </div>
                <dl class="detail-list">
                  <div><dt>主 Agent</dt><dd>{{ lastResponse.primaryAgent || lastResponse.agentType || '-' }}</dd></div>
                  <div><dt>意图</dt><dd>{{ lastResponse.intent || '-' }}</dd></div>
                  <div><dt>意图置信度</dt><dd>{{ formatPercent(lastResponse.intentConfidence) }}</dd></div>
                  <div><dt>路由置信度</dt><dd>{{ formatPercent(lastResponse.routingConfidence) }}</dd></div>
                  <div><dt>协作 Agent</dt><dd>{{ lastResponse.supportingAgents?.length ? lastResponse.supportingAgents.join(' · ') : '无' }}</dd></div>
                  <div><dt>知识库</dt><dd :class="lastResponse.knowledgeUsed ? 'success' : 'muted'">{{ lastResponse.knowledgeUsed ? '已使用' : '未使用' }}</dd></div>
                  <div><dt>转人工</dt><dd :class="lastResponse.escalated ? 'danger' : 'muted'">{{ lastResponse.escalated ? '是' : '否' }}</dd></div>
                </dl>
                <p v-if="lastResponse.routingReason" class="routing-reason">{{ lastResponse.routingReason }}</p>
              </div>
              <p v-else class="side-empty">发送消息后，这里会显示 Agent 路由、意图和耗时。</p>
            </section>

            <section class="side-card monitor-card">
              <div class="card-heading">
                <div>
                  <span class="kicker">Runtime</span>
                  <h2>运行状态</h2>
                </div>
                <button class="link-button" @click="loadMonitor">刷新</button>
              </div>
              <div class="mini-stats">
                <div><strong>{{ totalRequests }}</strong><span>请求</span></div>
                <div><strong>{{ agentCount }}</strong><span>Agent</span></div>
                <div><strong>{{ activeAlerts.length }}</strong><span>告警</span></div>
              </div>
              <div v-if="activeAlerts.length" class="alert-note">{{ activeAlerts[0].detail || activeAlerts[0].title }}</div>
              <p v-else class="healthy-note">当前没有活跃告警。</p>
            </section>
          </div>
        </aside>
      </div>
    </section>

    <section v-else-if="activeView === 'knowledge'" class="page page-knowledge">
      <div class="page-heading">
        <div class="heading-copy">
          <span class="kicker">Knowledge operations</span>
          <h1>知识库</h1>
          <p>搜索、补充和维护客服 Agent 使用的知识片段。</p>
        </div>
        <div class="count-display"><strong>{{ knowledgeCount }}</strong><span>chunks</span></div>
      </div>

      <div class="knowledge-layout">
        <section class="workspace-card search-workspace">
          <div class="card-heading">
            <div><span class="kicker">Retrieval</span><h2>检索知识</h2></div>
            <code>POST /search</code>
          </div>
          <div class="search-line">
            <input v-model="searchQuery" placeholder="例如：how long does a refund take" @keydown.enter="searchKnowledge" />
            <button @click="searchKnowledge" :disabled="busy || !searchQuery.trim()">搜索</button>
          </div>
          <div v-if="searchResults.length" class="result-list">
            <article v-for="(item, index) in searchResults" :key="item.id || item.title || index" class="result-item">
              <span class="result-number">{{ String(index + 1).padStart(2, '0') }}</span>
              <div>
                <div class="result-title"><strong>{{ item.title || '未命名文档' }}</strong><small>score {{ item.score ?? '-' }}</small></div>
                <p>{{ item.content }}</p>
              </div>
            </article>
          </div>
          <div v-else class="workspace-empty">输入客户问题开始搜索。</div>
        </section>

        <section class="workspace-card import-workspace">
          <div class="card-heading">
            <div><span class="kicker">Ingestion</span><h2>添加知识</h2></div>
            <code>ChromaDB</code>
          </div>
          <label><span>标题</span><input v-model="docTitle" placeholder="Refund policy supplement" /></label>
          <label><span>内容</span><textarea v-model="docContent" rows="7" placeholder="输入客服规范、产品说明或排障流程"></textarea></label>
          <div class="side-actions">
            <button @click="submitKnowledge" :disabled="busy || !docTitle.trim() || !docContent.trim()">添加文档</button>
            <label class="upload-button">上传文件<input type="file" accept=".txt,.md,.json" @change="handleUpload" /></label>
          </div>
        </section>
      </div>

      <section class="workspace-card skills-workspace">
        <div class="card-heading">
          <div><span class="kicker">Loaded skills</span><h2>已加载能力</h2></div>
          <button class="link-button" @click="reloadSkillSet">重新加载</button>
        </div>
        <div class="skill-table">
          <div v-for="skill in skillsData.skills" :key="skill.name" class="skill-item">
            <span class="skill-dot"></span><strong>{{ skill.name }}</strong><span>{{ skill.description || '业务规范能力' }}</span><small>{{ skill.content_chars || 0 }} chars</small>
          </div>
          <div v-if="!skillsData.skills.length" class="workspace-empty">暂无已加载 Skill。</div>
        </div>
      </section>
    </section>

    <section v-else class="page page-evaluation">
      <div class="page-heading">
        <div class="heading-copy">
          <span class="kicker">Evaluation lab</span>
          <h1>评测 Agent</h1>
          <p>运行 FastAPI 内置评测，查看意图识别、对话质量和回归结果。</p>
        </div>
        <button @click="runEvaluation" :disabled="busy">{{ busy ? '运行中...' : '运行评测' }}</button>
      </div>

      <div v-if="evalData" class="evaluation-content">
        <div class="evaluation-summary">
          <div class="score-hero"><span>Pass rate</span><strong>{{ formatPercent(evalData.pass_rate) }}</strong><small>{{ evalData.passed }} / {{ evalData.total }} cases passed</small></div>
          <div><span>通过</span><strong>{{ evalData.passed }}</strong></div>
          <div><span>总数</span><strong>{{ evalData.total }}</strong></div>
          <div><span>回归</span><strong :class="evalData.regressions?.length ? 'danger' : 'success'">{{ evalData.regressions?.length || 0 }}</strong></div>
        </div>
        <div class="evaluation-layout">
          <section class="workspace-card">
            <div class="card-heading"><div><span class="kicker">Scores</span><h2>平均评分</h2></div></div>
            <div class="score-list">
              <div v-for="(value, key) in evalData.avg_scores" :key="key"><span>{{ key }}</span><i><b :style="{ width: `${Math.min(Number(value) * 100, 100)}%` }"></b></i><strong>{{ Number(value).toFixed(2) }}</strong></div>
            </div>
          </section>
          <section class="workspace-card">
            <div class="card-heading"><div><span class="kicker">Recommendations</span><h2>优化建议</h2></div></div>
            <div v-if="evalData.recommendations?.length" class="recommendations"><p v-for="(item, index) in evalData.recommendations" :key="index">{{ item }}</p></div>
            <div v-else class="workspace-empty">本次评测没有返回额外建议。</div>
          </section>
        </div>
      </div>
      <div v-else class="evaluation-empty"><div class="empty-symbol">◎</div><h2>还没有评测结果</h2><p>点击右上角运行一次评测。</p></div>
    </section>
  </main>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, reactive, ref, watch, onMounted } from 'vue'
import {
  addKnowledge,
  baseUrl,
  createInitialSettings,
  reloadSkills,
  requestChat,
  requestHealth,
  requestKnowledgeStats,
  requestMonitor,
  requestSearch,
  requestSkills,
  runEvaluation as requestEvaluation,
  saveSettings,
  uploadKnowledge
} from './lib/backends'
import { renderMarkdown } from './lib/markdown'

const settings = reactive(createInitialSettings())
const activeView = ref('chat')
const messages = ref([])
const draft = ref('')
const busy = ref(false)
const healthOk = ref(false)
const healthLabel = ref('未检查')
const knowledgeCount = ref('-')
const searchQuery = ref('how long does a refund take')
const searchResults = ref([])
const docTitle = ref('Refund policy supplement')
const docContent = ref('During sale periods refund review may take 3-5 business days.')
const messageList = ref(null)
const monitorData = ref({ agent_stats: {}, tool_stats: {}, active_alerts: [], suggestions: [] })
const skillsData = ref({ count: 0, skills: [], errors: [] })
const lastResponse = ref(null)
const evalData = ref(null)
const toast = ref('')
let toastTimer
let messageSequence = 0

const endpoint = computed(() => baseUrl(settings))
const docsUrl = computed(() => `${endpoint.value}/docs`)
const activeAlerts = computed(() => monitorData.value.active_alerts || [])
const agentCount = computed(() => Object.keys(monitorData.value.agent_stats || {}).length)
const totalRequests = computed(() => Object.values(monitorData.value.agent_stats || {}).reduce((sum, item) => sum + Number(item.total || 0), 0))

watch(() => settings.conversationId, persist)
onMounted(refreshConsole)
onBeforeUnmount(() => clearTimeout(toastTimer))

function persist() { saveSettings(settings) }

function onEndpointChange() {
  persist()
  healthOk.value = false
  healthLabel.value = '未检查'
  refreshConsole()
}

async function refreshConsole() {
  await Promise.allSettled([checkHealth(), loadStats(), loadMonitor(), loadSkills()])
}

async function checkHealth() {
  try {
    const data = await requestHealth(settings)
    healthOk.value = data.status === 'ok'
    healthLabel.value = data.status || 'ok'
  } catch {
    healthOk.value = false
    healthLabel.value = '不可用'
  }
}

async function loadStats() {
  try {
    const data = await requestKnowledgeStats(settings)
    knowledgeCount.value = data.total_chunks ?? '-'
  } catch {
    knowledgeCount.value = '-'
  }
}

async function loadMonitor() {
  try {
    monitorData.value = await requestMonitor(settings)
  } catch {
    monitorData.value = { agent_stats: {}, tool_stats: {}, active_alerts: [], suggestions: [] }
  }
}

async function loadSkills() {
  try {
    skillsData.value = await requestSkills(settings)
  } catch {
    skillsData.value = { count: 0, skills: [], errors: [] }
  }
}

async function reloadSkillSet() {
  busy.value = true
  try {
    skillsData.value = await reloadSkills(settings)
    showToast('Skills 已重新加载')
  } catch {
    showToast('Skills 加载失败')
  } finally { busy.value = false }
}

async function sendMessage() {
  const content = draft.value.trim()
  if (!content || busy.value) return
  messages.value.push({ id: createMessageId(), role: 'user', content })
  draft.value = ''
  busy.value = true
  try {
    const response = await requestChat(settings, content)
    if (response.conversationId && !settings.conversationId) {
      settings.conversationId = response.conversationId
      persist()
    }
    lastResponse.value = response
    const meta = [
      response.intent,
      response.primaryAgent || response.agentType,
      response.knowledgeUsed ? 'RAG' : '',
      response.escalated ? '转人工' : ''
    ].filter(Boolean).join(' · ')
    messages.value.push({ id: createMessageId(), role: 'assistant', content: response.response, meta })
    await loadMonitor()
  } catch (error) {
    messages.value.push({ id: createMessageId(), role: 'assistant', content: error.message, meta: '请求失败' })
  } finally {
    busy.value = false
    await nextTick()
    messageList.value?.scrollTo({ top: messageList.value.scrollHeight, behavior: 'smooth' })
  }
}

function usePrompt(prompt) { draft.value = prompt }

function clearConversation() {
  messages.value = []
  lastResponse.value = null
  settings.conversationId = ''
  persist()
}

async function searchKnowledge() {
  busy.value = true
  try {
    const data = await requestSearch(settings, searchQuery.value, 5)
    searchResults.value = data.results || []
    showToast(`检索完成，返回 ${searchResults.value.length} 条结果`)
  } catch {
    showToast('检索失败，请检查连接')
  } finally { busy.value = false }
}

async function submitKnowledge() {
  busy.value = true
  try {
    await addKnowledge(settings, [{ title: docTitle.value.trim(), content: docContent.value.trim() }])
    await loadStats()
    showToast('文档已添加')
  } catch {
    showToast('文档导入失败')
  } finally { busy.value = false }
}

async function handleUpload(event) {
  const file = event.target.files?.[0]
  event.target.value = ''
  if (!file) return
  busy.value = true
  try {
    await uploadKnowledge(settings, file)
    await loadStats()
    showToast(`${file.name} 导入成功`)
  } catch {
    showToast('文件导入失败')
  } finally { busy.value = false }
}

async function runEvaluation() {
  busy.value = true
  try {
    evalData.value = await requestEvaluation(settings)
    showToast('评测完成')
  } catch {
    showToast('评测运行失败')
  } finally { busy.value = false }
}

function formatPercent(value) {
  const number = Number(value || 0)
  return `${(number <= 1 ? number * 100 : number).toFixed(1)}%`
}

function createMessageId() {
  messageSequence += 1
  return `message-${Date.now()}-${messageSequence}`
}

function showToast(message) {
  toast.value = message
  clearTimeout(toastTimer)
  toastTimer = setTimeout(() => { toast.value = '' }, 2600)
}
</script>
