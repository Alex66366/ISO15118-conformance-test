<template>
  <div class="sequence-diagram-container">
    <!-- Header -->
    <div class="diagram-header">
      <div class="header-left">
        <h3 class="diagram-title">
          <el-icon><Connection /></el-icon>
          Message Sequence
        </h3>
        <div class="diagram-stats" v-if="stats.total > 0">
          <span class="stat-item">
            <span class="stat-label">Total:</span>
            <span class="stat-value">{{ stats.total }}</span>
          </span>
          <span class="stat-item stat-evcc">
            <span class="stat-arrow">→</span>
            {{ stats.evccToSecc }}
          </span>
          <span class="stat-item stat-secc">
            <span class="stat-arrow">←</span>
            {{ stats.seccToEvcc }}
          </span>
        </div>
      </div>
      <div class="header-right">
        <el-tooltip content="Zoom In" placement="top">
          <el-button :icon="ZoomIn" size="small" circle @click="zoomIn" :disabled="scale >= 2" />
        </el-tooltip>
        <el-tooltip content="Zoom Out" placement="top">
          <el-button :icon="ZoomOut" size="small" circle @click="zoomOut" :disabled="scale <= 0.5" />
        </el-tooltip>
        <el-tooltip content="Reset Zoom" placement="top">
          <el-button :icon="Refresh" size="small" circle @click="resetZoom" />
        </el-tooltip>
        <el-divider direction="vertical" />
        <el-tooltip content="Export as SVG" placement="top">
          <el-button :icon="Download" size="small" circle @click="exportSvg" />
        </el-tooltip>
        <el-tooltip content="Export as PNG" placement="top">
          <el-button :icon="Picture" size="small" circle @click="exportPng" />
        </el-tooltip>
      </div>
    </div>

    <!-- Diagram Area -->
    <div class="diagram-content" ref="diagramContainer">
      <div
        v-if="!hasMessages"
        class="empty-state"
      >
        <el-icon class="empty-icon"><Connection /></el-icon>
        <p>No message sequence data available</p>
        <p class="empty-hint">Execute a test case to see the message sequence diagram</p>
      </div>
      <div
        v-else-if="renderError"
        class="error-state"
      >
        <el-icon class="error-icon"><Warning /></el-icon>
        <p>Failed to render sequence diagram</p>
        <p class="error-hint">Syntax error in text/mermaid version 10.8.0</p>
        <pre class="error-detail">{{ renderError }}</pre>
      </div>
      <div
        v-else
        class="diagram-wrapper"
        :style="{ transform: `scale(${scale})`, transformOrigin: 'top left' }"
      >
        <div ref="mermaidContainer" class="mermaid-container" v-html="renderedDiagram"></div>
      </div>
    </div>

    <!-- Message List Panel -->
    <div v-if="hasMessages" class="message-panel">
      <div class="panel-header">
        <span class="panel-title">Message Details</span>
        <el-button
          :icon="selectedMessage ? ArrowUp : ArrowDown"
          size="small"
          text
          @click="togglePanel"
        >
          {{ selectedMessage ? 'Hide' : 'Show' }} Details
        </el-button>
      </div>
      <el-collapse-transition>
        <div v-if="selectedMessage" class="panel-content">
          <div class="message-detail">
            <div class="detail-row">
              <span class="detail-label">Direction:</span>
              <span
                class="detail-value"
                :class="selectedMessage.direction === 'evcc-to-secc' ? 'dir-evcc' : 'dir-secc'"
              >
                {{ selectedMessage.from }} → {{ selectedMessage.to }}
              </span>
            </div>
            <div class="detail-row">
              <span class="detail-label">Message:</span>
              <span class="detail-value">{{ selectedMessage.messageType }}</span>
            </div>
            <div class="detail-row" v-if="selectedMessage.timestamp">
              <span class="detail-label">Time:</span>
              <span class="detail-value">{{ selectedMessage.timestamp }}</span>
            </div>
            <div class="detail-row">
              <span class="detail-label">Raw:</span>
              <span class="detail-value raw-text">{{ selectedMessage.raw }}</span>
            </div>
          </div>
        </div>
      </el-collapse-transition>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted, onBeforeUnmount, nextTick } from 'vue'
import mermaid from 'mermaid'
import { parseLogToSequence } from '../utils/logParser'
import {
  Connection, ZoomIn, ZoomOut, Refresh, Download, Picture,
  Warning, ArrowUp, ArrowDown
} from '@element-plus/icons-vue'

const props = defineProps({
  logText: {
    type: String,
    default: ''
  }
})

// Stable ID counter instead of Date.now() which can collide in fast async calls
let _renderCounter = 0
const nextId = () => `mermaid-${++_renderCounter}-${Date.now()}`

// State
const diagramContainer = ref(null)
const mermaidContainer = ref(null)
const renderedDiagram = ref('')
const renderError = ref(null)
const scale = ref(1)
const selectedMessage = ref(null)
const isPanelExpanded = ref(false)

// Track whether a render is in-flight so we skip/stale renders
const renderingVersion = ref(0)
let pendingDebounceTimer = null

// Initialize mermaid ONCE at setup time — not deferred inside onMounted
// This avoids the race where the immediate watcher fires before init completes
mermaid.initialize({
  startOnLoad: false,
  theme: 'dark',
  sequence: {
    showSequenceNumbers: true,
    actorMargin: 50,
    boxMargin: 10,
    boxTextMargin: 5,
    noteMargin: 10,
    messageMargin: 35,
    mirrorActors: false,
    bottomMarginAdj: 1,
    wrapperPadding: 10
  },
  themeVariables: {
    darkMode: true,
    background: '#1a1a24',
    primaryColor: '#00d4aa',
    primaryTextColor: '#fff',
    primaryBorderColor: '#00d4aa',
    lineColor: '#00d4aa',
    secondaryColor: '#3b82f6',
    tertiaryColor: '#1a1a24',
    actorBorder: '#00d4aa',
    actorBkg: '#1a1a24',
    actorTextColor: '#fff',
    actorLineColor: '#00d4aa',
    signalColor: '#00d4aa',
    signalTextColor: '#fff'
  }
})

// Computed
const parsedData = computed(() => parseLogToSequence(props.logText))
const messages = computed(() => parsedData.value.messages)
const stats = computed(() => parsedData.value.stats)
const hasMessages = computed(() => messages.value && messages.value.length > 0)

async function renderDiagram() {
  // Bump version: any in-flight render with an older version is considered stale
  const myVersion = ++renderingVersion.value

  // Debounce: cancel any pending render so only the latest log text triggers a render
  if (pendingDebounceTimer !== null) {
    clearTimeout(pendingDebounceTimer)
    pendingDebounceTimer = null
  }
  pendingDebounceTimer = setTimeout(async () => {
    pendingDebounceTimer = null

    // Guard: stale render (a newer version was started while we were waiting)
    if (myVersion !== renderingVersion.value) return

    if (!hasMessages.value) {
      renderedDiagram.value = ''
      renderError.value = null
      return
    }

    try {
      const { mermaidCode } = parsedData.value

      // Escape any characters in message text that could break Mermaid parsing
      const safeCode = sanitizeMermaidCode(mermaidCode)

      const { svg } = await mermaid.render(nextId(), safeCode)

      // Guard: stale render — drop the result if a newer version won the race
      if (myVersion !== renderingVersion.value) return

      renderedDiagram.value = svg
      renderError.value = null

      await nextTick()
      attachClickHandlers()
    } catch (error) {
      // Only surface the error if this render is still the latest one
      if (myVersion !== renderingVersion.value) return
      console.error('Mermaid render error:', error)
      renderError.value = error.message || 'Failed to render diagram'
      renderedDiagram.value = ''
    }
  }, 300)
}

/**
 * Escape or remove characters that Mermaid's parser would reject.
 * Crucial when message labels contain colons, arrows, quotes, etc.
 */
function sanitizeMermaidCode(code) {
  if (!code) return code

  // Remove lines with colons in the label position that would break Mermaid parsing.
  // The label is after the first ': ' on a message/note line. Only strip characters
  // that are genuinely problematic for the Mermaid parser.
  return code
    // Strip empty participant declarations that Mermaid cannot render
    .replace(/^\s*participant\s+(\w+)\s*$/gm, (match, name) => {
      const safeName = name.replace(/[^a-zA-Z0-9_]/g, '_')
      const hasUsage =
        code.includes(`${safeName}->>`) ||
        code.includes(`${safeName}-->>`) ||
        code.includes(`->>${safeName}`) ||
        code.includes(`-->>${safeName}`)
      return hasUsage ? match : ''
    })
    // Clean up Mermaid-sensitive characters inside message/note labels only.
    // The label portion is: after the first ':' (or ' : ') on a line that starts
    // with valid Mermaid keywords or participant names.
    .replace(/^(\s*(?:participant|note|loop|alt|else|opt|par|break|critical|section|divider|EVCC|SECC|\w+)[^\n]*?):\s*([^\n]*)$/gim, (match, prefix, label) => {
      const cleanLabel = label
        .replace(/[{}\[\]]/g, ' ')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/\s+/g, ' ')
        .trim()
      return `${prefix}: ${cleanLabel}`
    })
    // Collapse any remaining problematic whitespace
    .replace(/\n{3,}/g, '\n\n')
}

function attachClickHandlers() {
  if (!mermaidContainer.value) return
  const svg = mermaidContainer.value.querySelector('svg')
  if (!svg) return
  svg.style.cursor = 'pointer'
  svg.querySelectorAll('text').forEach(el => { el.style.cursor = 'pointer' })
}

// Watch log changes with debounce — no immediate: true to avoid the
// onMounted / initialize() race. renderDiagram() is called explicitly
// in onMounted after a small tick to guarantee mermaid is ready.
watch(() => props.logText, () => {
  // Clear any stale render error — new log data invalidates old failures.
  // The diagram will either re-render successfully or show a fresh error.
  renderError.value = null
  renderDiagram()
})

// Initial render after the component is mounted and mermaid is definitely ready
onMounted(() => {
  nextTick(() => renderDiagram())
})

// Clean up pending debounce timer and any mermaid state when the component is destroyed
onBeforeUnmount(() => {
  if (pendingDebounceTimer !== null) {
    clearTimeout(pendingDebounceTimer)
    pendingDebounceTimer = null
  }
  renderingVersion.value = -1 // mark all renders as stale
})

// Zoom controls
function zoomIn() {
  if (scale.value < 2) {
    scale.value = Math.min(2, scale.value + 0.1)
  }
}

function zoomOut() {
  if (scale.value > 0.5) {
    scale.value = Math.max(0.5, scale.value - 0.1)
  }
}

function resetZoom() {
  scale.value = 1
}

// Export functions
async function exportSvg() {
  if (!renderedDiagram.value) return

  const svg = mermaidContainer.value.querySelector('svg')
  if (!svg) return

  const svgData = new XMLSerializer().serializeToString(svg)
  const blob = new Blob([svgData], { type: 'image/svg+xml' })
  const url = URL.createObjectURL(blob)

  const a = document.createElement('a')
  a.href = url
  a.download = `sequence_diagram_${Date.now()}.svg`
  a.click()

  URL.revokeObjectURL(url)
}

async function exportPng() {
  if (!renderedDiagram.value) return

  const svg = mermaidContainer.value.querySelector('svg')
  if (!svg) return

  const svgData = new XMLSerializer().serializeToString(svg)
  const canvas = document.createElement('canvas')
  const ctx = canvas.getContext('2d')
  const img = new Image()

  img.onload = () => {
    canvas.width = img.width * 2
    canvas.height = img.height * 2
    ctx.scale(2, 2)
    ctx.fillStyle = '#1a1a24'
    ctx.fillRect(0, 0, canvas.width, canvas.height)
    ctx.drawImage(img, 0, 0)

    canvas.toBlob((blob) => {
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `sequence_diagram_${Date.now()}.png`
      a.click()
      URL.revokeObjectURL(url)
    })
  }

  img.src = 'data:image/svg+xml;base64,' + btoa(unescape(encodeURIComponent(svgData)))
}

// Panel toggle
function togglePanel() {
  isPanelExpanded.value = !isPanelExpanded.value
}
</script>

<style scoped>
.sequence-diagram-container {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: #1a1a24;
  border-radius: 12px;
  overflow: hidden;
}

.diagram-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 16px;
  background: #252532;
  border-bottom: 1px solid #333;
}

.header-left {
  display: flex;
  align-items: center;
  gap: 16px;
}

.diagram-title {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  color: #fff;
}

.diagram-stats {
  display: flex;
  align-items: center;
  gap: 12px;
}

.stat-item {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  color: #9ca3af;
}

.stat-value {
  font-weight: 600;
  color: #fff;
}

.stat-evcc {
  color: #3b82f6;
}

.stat-secc {
  color: #f59e0b;
}

.stat-arrow {
  font-weight: bold;
}

.header-right {
  display: flex;
  align-items: center;
  gap: 4px;
}

.diagram-content {
  flex: 1;
  overflow: auto;
  padding: 16px;
  background: #1a1a24;
}

.empty-state,
.error-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 200px;
  color: #6b7280;
}

.empty-icon,
.error-icon {
  font-size: 48px;
  margin-bottom: 16px;
}

.empty-icon {
  color: #4b5563;
}

.error-icon {
  color: #ef4444;
}

.empty-hint,
.error-hint {
  font-size: 12px;
  margin-top: 8px;
  color: #f59e0b;
}

.error-detail {
  margin-top: 12px;
  padding: 10px 14px;
  background: rgba(0,0,0,0.3);
  border: 1px solid #ef4444;
  border-radius: 6px;
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: 11px;
  color: #f38ba8;
  white-space: pre-wrap;
  word-break: break-word;
  max-width: 600px;
  text-align: left;
}

.diagram-wrapper {
  transition: transform 0.2s ease;
}

.mermaid-container {
  display: inline-block;
  min-width: 100%;
}

.mermaid-container :deep(svg) {
  max-width: none;
}

.message-panel {
  background: #252532;
  border-top: 1px solid #333;
}

.panel-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 16px;
}

.panel-title {
  font-size: 12px;
  font-weight: 600;
  color: #9ca3af;
}

.panel-content {
  padding: 0 16px 16px;
}

.message-detail {
  background: #1a1a24;
  border-radius: 8px;
  padding: 12px;
}

.detail-row {
  display: flex;
  gap: 12px;
  margin-bottom: 8px;
  font-size: 12px;
}

.detail-row:last-child {
  margin-bottom: 0;
}

.detail-label {
  color: #6b7280;
  min-width: 60px;
}

.detail-value {
  color: #fff;
  word-break: break-word;
}

.detail-value.dir-evcc {
  color: #3b82f6;
}

.detail-value.dir-secc {
  color: #f59e0b;
}

.raw-text {
  font-family: 'Monaco', 'Menlo', monospace;
  font-size: 11px;
  color: #9ca3af;
}
</style>
