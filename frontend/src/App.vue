<template>
  <main class="app-shell">
    <header class="topbar">
      <a class="brand" href="#" aria-label="SupportMesh home" @click.prevent="activeView = 'chat'">
        <span class="brand-mark">S</span>
        <span class="brand-name">SupportMesh</span>
      </a>

      <nav class="view-nav" aria-label="Workspaces">
        <button :class="{ active: activeView === 'chat' }" @click="activeView = 'chat'">Chat</button>
        <button :class="{ active: activeView === 'knowledge' }" @click="activeView = 'knowledge'">Knowledge</button>
        <button :class="{ active: activeView === 'evaluation' }" @click="activeView = 'evaluation'">Evaluation</button>
      </nav>

      <div class="topbar-tools">
        <span class="environment-pill">
          <i :class="healthOk ? 'online' : 'offline'"></i>
          {{ healthLabel }}
        </span>
        <a class="docs-link" :href="docsUrl" target="_blank" rel="noreferrer">API docs</a>
      </div>
    </header>

    <div v-if="toast" class="toast" role="status">{{ toast }}</div>

    <section v-if="activeView === 'chat'" class="page page-chat">
      <div class="page-heading">
        <div class="heading-copy">
          <span class="kicker">Conversation lab</span>
          <h1>Talk to the agents</h1>
          <p>Send a real request and watch how it is classified, routed and answered.</p>
        </div>
        <div class="heading-actions">
          <span class="session-label">{{ settings.conversationId || 'New session' }}</span>
          <button class="quiet-button" @click="clearConversation">Clear</button>
        </div>
      </div>

      <div class="chat-layout">
        <section class="chat-stage">
          <div class="stage-bar">
            <div class="stage-context">
              <span class="context-dot"></span>
              <span>{{ endpoint }}</span>
            </div>
            <span>{{ messages.length }} messages</span>
          </div>

          <div class="messages" ref="messageList">
            <article v-for="item in messages" :key="item.id" :class="['message', item.role]">
              <div class="message-meta">
                <span>{{ item.role === 'user' ? 'You' : 'Agent' }}</span>
                <small v-if="item.meta">{{ item.meta }}</small>
              </div>
              <p v-if="item.role === 'user'">{{ item.content }}</p>
              <!-- renderMarkdown escapes first, so only its own tags reach the DOM. -->
              <div v-else class="message-body" v-html="renderMarkdown(item.content)"></div>
            </article>

            <div v-if="messages.length === 0" class="empty-state">
              <div class="empty-symbol">✦</div>
              <h2>Start with a customer question</h2>
              <p>These are only starting points -- type your own test case any time.</p>
              <div class="starter-prompts">
                <button @click="usePrompt('I want a refund for order #12345')">Refund request</button>
                <button @click="usePrompt('Login keeps returning a 401 error, how do I fix it?')">Technical issue</button>
                <button @click="usePrompt('How long until my invoice is issued?')">Invoice question</button>
              </div>
            </div>
          </div>

          <form class="composer" @submit.prevent="sendMessage">
            <textarea
              v-model="draft"
              rows="3"
              placeholder="Type a message..."
              @keydown.meta.enter.prevent="sendMessage"
              @keydown.ctrl.enter.prevent="sendMessage"
            ></textarea>
            <div class="composer-bottom">
              <span>⌘ / Ctrl + Enter to send</span>
              <button type="submit" :disabled="busy || !draft.trim()">{{ busy ? 'Sending' : 'Send' }}</button>
            </div>
          </form>
        </section>

        <aside class="chat-sidebar">
          <div class="chat-sidebar-scroll">
            <section class="side-card session-card">
              <div class="card-heading">
                <div>
                  <span class="kicker">Session</span>
                  <h2>Current session</h2>
                </div>
                <span class="status-copy muted">{{ settings.conversationId ? 'Active' : 'New session' }}</span>
              </div>
              <div class="session-grid">
                <div>
                  <span>Conversation ID</span>
                  <strong>{{ settings.conversationId || 'Auto-generated' }}</strong>
                </div>
                <div>
                  <span>User ID</span>
                  <strong>{{ settings.userId || 'anonymous' }}</strong>
                </div>
              </div>
            </section>

            <section class="side-card connection-card">
              <div class="card-heading">
                <div>
                  <span class="kicker">Connection</span>
                  <h2>Backend connection</h2>
                </div>
                <span class="status-copy" :class="healthOk ? 'success' : 'muted'">{{ healthLabel }}</span>
              </div>

              <label>
                <span>Backend URL</span>
                <input v-model="settings.endpoint" @change="onEndpointChange" placeholder="/api/python" />
              </label>
              <label>
                <span>User ID</span>
                <input v-model="settings.userId" @change="persist" placeholder="u1001" />
              </label>
              <label>
                <span>Conversation ID</span>
                <input v-model="settings.conversationId" @change="persist" placeholder="Auto-generated" />
              </label>
              <div class="side-actions">
                <button @click="checkHealth">Check</button>
                <button class="quiet-button" @click="refreshConsole">Refresh</button>
              </div>
            </section>

            <section class="side-card trace-card">
              <div class="card-heading">
                <div>
                  <span class="kicker">Last trace</span>
                  <h2>Last request</h2>
                </div>
                <span class="trace-status" :class="lastResponse ? 'has-data' : ''"></span>
              </div>

              <div v-if="lastResponse" class="trace-body">
                <div class="latency">
                  <span>Latency</span>
                  <strong>{{ Math.round(lastResponse.latencyMs) || '-' }}<small> ms</small></strong>
                </div>
                <dl class="detail-list">
                  <div><dt>Primary agent</dt><dd>{{ lastResponse.primaryAgent || lastResponse.agentType || '-' }}</dd></div>
                  <div><dt>Intent</dt><dd>{{ lastResponse.intent || '-' }}</dd></div>
                  <div><dt>Intent confidence</dt><dd>{{ formatPercent(lastResponse.intentConfidence) }}</dd></div>
                  <div><dt>Routing confidence</dt><dd>{{ formatPercent(lastResponse.routingConfidence) }}</dd></div>
                  <div><dt>Supporting agents</dt><dd>{{ lastResponse.supportingAgents?.length ? lastResponse.supportingAgents.join(' · ') : 'None' }}</dd></div>
                  <div><dt>Knowledge base</dt><dd :class="lastResponse.knowledgeUsed ? 'success' : 'muted'">{{ lastResponse.knowledgeUsed ? 'Used' : 'Not used' }}</dd></div>
                  <div><dt>Escalated</dt><dd :class="lastResponse.escalated ? 'danger' : 'muted'">{{ lastResponse.escalated ? 'Yes' : 'No' }}</dd></div>
                </dl>
                <p v-if="lastResponse.routingReason" class="routing-reason">{{ lastResponse.routingReason }}</p>
              </div>
              <p v-else class="side-empty">Send a message and the routing, intent and latency will show up here.</p>
            </section>

            <section class="side-card monitor-card">
              <div class="card-heading">
                <div>
                  <span class="kicker">Runtime</span>
                  <h2>Live stats</h2>
                </div>
                <button class="link-button" @click="loadMonitor">Refresh</button>
              </div>
              <div class="mini-stats">
                <div><strong>{{ totalRequests }}</strong><span>requests</span></div>
                <div><strong>{{ agentCount }}</strong><span>Agent</span></div>
                <div><strong>{{ activeAlerts.length }}</strong><span>alerts</span></div>
              </div>
              <div v-if="activeAlerts.length" class="alert-note">{{ activeAlerts[0].detail || activeAlerts[0].title }}</div>
              <p v-else class="healthy-note">No active alerts.</p>
            </section>
          </div>
        </aside>
      </div>
    </section>

    <section v-else-if="activeView === 'knowledge'" class="page page-knowledge">
      <div class="page-heading">
        <div class="heading-copy">
          <span class="kicker">Knowledge operations</span>
          <h1>Knowledge base</h1>
          <p>Search, extend and maintain the chunks the agents retrieve from.</p>
        </div>
        <div class="count-display"><strong>{{ knowledgeCount }}</strong><span>chunks</span></div>
      </div>

      <div class="knowledge-layout">
        <section class="workspace-card search-workspace">
          <div class="card-heading">
            <div><span class="kicker">Retrieval</span><h2>Search</h2></div>
            <code>POST /search</code>
          </div>
          <div class="search-line">
            <input v-model="searchQuery" placeholder="e.g. how long does a refund take" @keydown.enter="searchKnowledge" />
            <button @click="searchKnowledge" :disabled="busy || !searchQuery.trim()">Search</button>
          </div>
          <div v-if="searchResults.length" class="result-list">
            <article v-for="(item, index) in searchResults" :key="item.id || item.title || index" class="result-item">
              <span class="result-number">{{ String(index + 1).padStart(2, '0') }}</span>
              <div>
                <div class="result-title"><strong>{{ item.title || 'Untitled document' }}</strong><small>score {{ item.score ?? '-' }}</small></div>
                <p>{{ item.content }}</p>
              </div>
            </article>
          </div>
          <div v-else class="workspace-empty">Enter a customer question to search.</div>
        </section>

        <section class="workspace-card import-workspace">
          <div class="card-heading">
            <div><span class="kicker">Ingestion</span><h2>Add a document</h2></div>
            <code>ChromaDB</code>
          </div>
          <label><span>Title</span><input v-model="docTitle" placeholder="Refund policy supplement" /></label>
          <label><span>Content</span><textarea v-model="docContent" rows="7" placeholder="Service standards, product notes or a troubleshooting guide"></textarea></label>
          <div class="side-actions">
            <button @click="submitKnowledge" :disabled="busy || !docTitle.trim() || !docContent.trim()">Add document</button>
            <label class="upload-button">Upload file<input type="file" accept=".txt,.md,.json" @change="handleUpload" /></label>
          </div>
        </section>
      </div>

      <section class="workspace-card skills-workspace">
        <div class="card-heading">
          <div><span class="kicker">Loaded skills</span><h2>Active skills</h2></div>
          <button class="link-button" @click="reloadSkillSet">Reload</button>
        </div>
        <div class="skill-table">
          <div v-for="skill in skillsData.skills" :key="skill.name" class="skill-item">
            <span class="skill-dot"></span><strong>{{ skill.name }}</strong><span>{{ skill.description || 'Business skill' }}</span><small>{{ skill.content_chars || 0 }} chars</small>
          </div>
          <div v-if="!skillsData.skills.length" class="workspace-empty">No skills loaded.</div>
        </div>
      </section>
    </section>

    <section v-else class="page page-evaluation">
      <div class="page-heading">
        <div class="heading-copy">
          <span class="kicker">Evaluation lab</span>
          <h1>Evaluate the agents</h1>
          <p>Run the backend's built-in evaluation: intent accuracy, dialogue quality and regressions.</p>
        </div>
        <button @click="runEvaluation" :disabled="busy">{{ busy ? 'Running...' : 'Run evaluation' }}</button>
      </div>

      <div v-if="evalData" class="evaluation-content">
        <div class="evaluation-summary">
          <div class="score-hero"><span>Pass rate</span><strong>{{ formatPercent(evalData.pass_rate) }}</strong><small>{{ evalData.passed }} / {{ evalData.total }} cases passed</small></div>
          <div><span>Passed</span><strong>{{ evalData.passed }}</strong></div>
          <div><span>Total</span><strong>{{ evalData.total }}</strong></div>
          <div><span>Regressions</span><strong :class="evalData.regressions?.length ? 'danger' : 'success'">{{ evalData.regressions?.length || 0 }}</strong></div>
        </div>
        <div class="evaluation-layout">
          <section class="workspace-card">
            <div class="card-heading"><div><span class="kicker">Scores</span><h2>Average scores</h2></div></div>
            <div class="score-list">
              <div v-for="(value, key) in evalData.avg_scores" :key="key"><span>{{ key }}</span><i><b :style="{ width: `${Math.min(Number(value) * 100, 100)}%` }"></b></i><strong>{{ Number(value).toFixed(2) }}</strong></div>
            </div>
          </section>
          <section class="workspace-card">
            <div class="card-heading"><div><span class="kicker">Recommendations</span><h2>Suggestions</h2></div></div>
            <div v-if="evalData.recommendations?.length" class="recommendations"><p v-for="(item, index) in evalData.recommendations" :key="index">{{ item }}</p></div>
            <div v-else class="workspace-empty">This run returned no extra suggestions.</div>
          </section>
        </div>
      </div>
      <div v-else class="evaluation-empty"><div class="empty-symbol">◎</div><h2>No evaluation yet</h2><p>Use the button above to run one.</p></div>
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
const healthLabel = ref('Not checked')
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
  healthLabel.value = 'Not checked'
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
    healthLabel.value = 'Unavailable'
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
    showToast('Skills reloaded')
  } catch {
    showToast('Skill reload failed')
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
      response.escalated ? 'escalated' : ''
    ].filter(Boolean).join(' · ')
    messages.value.push({ id: createMessageId(), role: 'assistant', content: response.response, meta })
    await loadMonitor()
  } catch (error) {
    messages.value.push({ id: createMessageId(), role: 'assistant', content: error.message, meta: 'Request failed' })
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
    showToast(`Search returned ${searchResults.value.length} results`)
  } catch {
    showToast('Search failed -- check the connection')
  } finally { busy.value = false }
}

async function submitKnowledge() {
  busy.value = true
  try {
    await addKnowledge(settings, [{ title: docTitle.value.trim(), content: docContent.value.trim() }])
    await loadStats()
    showToast('Document added')
  } catch {
    showToast('Failed to add the document')
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
    showToast(`${file.name} imported`)
  } catch {
    showToast('File import failed')
  } finally { busy.value = false }
}

async function runEvaluation() {
  busy.value = true
  try {
    evalData.value = await requestEvaluation(settings)
    showToast('Evaluation finished')
  } catch {
    showToast('Evaluation failed')
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
