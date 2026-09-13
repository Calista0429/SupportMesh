const DEFAULT_BASE_URL = import.meta.env.VITE_PYTHON_API_URL || '/api/python'
const STORAGE_KEY = 'supportmesh.console.settings'

export function createInitialSettings() {
  const saved = readSettings()
  return {
    userId: saved.userId || 'u1001',
    conversationId: saved.conversationId || '',
    endpoint: saved.endpoint || DEFAULT_BASE_URL
  }
}

export function saveSettings(settings) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify({
    userId: settings.userId,
    conversationId: settings.conversationId,
    endpoint: settings.endpoint
  }))
}

export function baseUrl(settings) {
  return String(settings.endpoint || DEFAULT_BASE_URL).replace(/\/+$/, '')
}

export const requestHealth = (settings) => requestJson(settings, '/health')
export const requestMonitor = (settings) => requestJson(settings, '/monitor')
export const requestSkills = (settings) => requestJson(settings, '/skills')
export const requestKnowledgeStats = (settings) => requestJson(settings, '/knowledge/stats')
export const reloadSkills = (settings) => requestJson(settings, '/skills/reload', { method: 'POST' })

export const runEvaluation = (settings) => requestJson(settings, '/eval/run', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' }
})

export function requestSearch(settings, query, topK = 5) {
  const params = new URLSearchParams({ query, top_k: String(topK) })
  return requestJson(settings, `/search?${params}`, { method: 'POST' })
}

export async function requestChat(settings, message) {
  const raw = await requestJson(settings, '/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      message,
      user_id: settings.userId || 'anonymous',
      conv_id: settings.conversationId || undefined
    })
  })
  // Field names mirror the backend's ChatResponse model one to one.
  return {
    conversationId: raw.conv_id || '',
    response: raw.response || '',
    intent: raw.intent || 'other',
    intentGroup: raw.intent_group || 'other',
    agentType: raw.agent_type || '',
    primaryAgent: raw.primary_agent || '',
    supportingAgents: raw.supporting_agents || [],
    routingReason: raw.routing_reason || '',
    routingConfidence: Number(raw.routing_confidence ?? 0),
    intentConfidence: Number(raw.intent_confidence ?? 0),
    entities: raw.entities || {},
    escalated: Boolean(raw.escalated),
    latencyMs: Number(raw.latency_ms ?? 0),
    knowledgeUsed: Boolean(raw.knowledge_used)
  }
}

export function addKnowledge(settings, documents) {
  return requestJson(settings, '/knowledge/add', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ documents })
  })
}

export function uploadKnowledge(settings, file) {
  const form = new FormData()
  form.append('file', file)
  return requestJson(settings, '/knowledge/upload', { method: 'POST', body: form })
}

async function requestJson(settings, path, options = {}) {
  const response = await fetch(`${baseUrl(settings)}${path}`, options)
  const text = await response.text()
  let data = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    data = text
  }
  if (!response.ok) {
    const detail = typeof data === 'string' ? data : JSON.stringify(data)
    throw new Error(`${response.status} ${response.statusText}: ${detail}`)
  }
  return data
}

function readSettings() {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}')
  } catch {
    return {}
  }
}
