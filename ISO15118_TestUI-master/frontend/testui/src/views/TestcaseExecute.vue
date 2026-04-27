<template>
  <div class="execute-container">
    <!-- Page Header -->
    <div class="page-header">
      <div class="header-left">
        <el-button :icon="ArrowLeft" circle @click="goBack" class="back-btn" />
        <div class="header-content">
          <h1 class="page-title">Execute Test</h1>
          <p class="page-subtitle">Configure and run conformance tests</p>
        </div>
      </div>
      <div class="header-right">
        <!-- Queue Status -->
        <div v-if="queueStatus.current_running > 0 || queueStatus.pending_count > 0" class="queue-badge">
          <el-icon><List /></el-icon>
          <span>
            {{ queueStatus.current_running }}/{{ queueStatus.max_concurrent }} Running
            <span v-if="queueStatus.pending_count > 0" class="queued">+{{ queueStatus.pending_count }} Queued</span>
          </span>
        </div>
        <!-- Status Indicator -->
        <div class="status-indicator" :class="{ running: isRunning }">
          <span class="status-dot"></span>
          <span class="status-text">{{ isRunning ? 'Running' : 'Idle' }}</span>
        </div>
        <el-button v-if="isRunning" type="danger" :icon="VideoPause" @click="cancelTestcase">
          Cancel
        </el-button>
      </div>
    </div>

    <!-- Main Content -->
    <div class="main-content">
      <!-- Left Panel -->
      <div class="left-panel">
        <!-- Test Case Selection -->
        <div class="config-card">
          <div class="card-header">
            <el-icon><Search /></el-icon>
            <span>Test Case Selection</span>
          </div>
          <div class="card-body">
            <div class="form-grid">
              <div class="form-field">
                <label>Testcase ID</label>
                <el-input
                  v-model="target_testcase_id"
                  placeholder="Enter ID..."
                  :disabled="isRunning"
                  @blur="verifyId(target_testcase_id)"
                />
              </div>
              <div class="form-field">
                <label>Testcase Name</label>
                <el-input
                  v-model="target_testcase_name"
                  placeholder="Enter name..."
                  :disabled="isRunning"
                  @blur="verifyTestcaseName(target_testcase_name)"
                />
              </div>
            </div>
            <div class="form-field full-width">
              <label>SECC IP Address</label>
              <el-input
                v-model="secc_ip"
                placeholder="ff02::1"
                :disabled="isRunning"
              />
            </div>
            <div class="action-bar">
              <div class="action-buttons">
                <el-button :icon="RefreshLeft" @click="resetConfig" :disabled="isRunning">
                  Reset
                </el-button>
                <el-dropdown trigger="click" @command="handleOpenConfigCommand">
                  <el-button :icon="FolderOpened" :disabled="isRunning">
                    Open Config
                    <el-icon class="el-icon--right"><ArrowDown /></el-icon>
                  </el-button>
                  <template #dropdown>
                    <el-dropdown-menu>
                      <el-dropdown-item command="v2g">testconfig.json</el-dropdown-item>
                      <el-dropdown-item command="slac">slac_config.json</el-dropdown-item>
                    </el-dropdown-menu>
                  </template>
                </el-dropdown>
                <el-button :icon="Download" @click="storeConfig">
                  Save Config
                </el-button>
              </div>
              <el-button type="primary" :icon="VideoPlay" @click="executeTestcase" :disabled="!validExecuteFlag || isRunning" :loading="isRunning">
                Execute
              </el-button>
            </div>
          </div>
        </div>

        <!-- Configuration -->
        <div class="config-card">
          <div class="card-header">
            <el-icon><Setting /></el-icon>
            <span>Configuration</span>
            <div class="header-tabs">
              <el-tabs v-model="activeConfigTab" class="inner-tabs">
                <el-tab-pane label="PICS" name="pics" />
                <el-tab-pane label="PIXIT" name="pixit" />
                <el-tab-pane label="Timer" name="timer" />
                <el-tab-pane label="SLAC" name="slac" />
              </el-tabs>
            </div>
          </div>
          <div class="card-body">
            <div class="config-panel">
              <!-- PICS -->
              <div v-show="activeConfigTab === 'pics'" class="config-content">
                <div class="config-hint">
                  <el-icon><InfoFilled /></el-icon>
                  <span>Protocol Implementation Conformance Statement</span>
                </div>
                <el-form label-position="top">
                  <template v-for="(value, key) in configuration.pics" :key="key">
                    <el-form-item :label="formatLabel(key)">
                      <el-input v-model="configuration.pics[key]" />
                    </el-form-item>
                  </template>
                </el-form>
              </div>
              <!-- PIXIT -->
              <div v-show="activeConfigTab === 'pixit'" class="config-content">
                <div class="config-hint">
                  <el-icon><InfoFilled /></el-icon>
                  <span>Protocol Implementation extra Information for Testing</span>
                </div>
                <el-form label-position="top">
                  <template v-for="(value, key) in configuration.pixit" :key="key">
                    <el-form-item :label="formatLabel(key)">
                      <el-input v-model="configuration.pixit[key]" />
                    </el-form-item>
                  </template>
                </el-form>
              </div>
              <!-- Timer -->
              <div v-show="activeConfigTab === 'timer'" class="config-content">
                <div class="config-hint">
                  <el-icon><InfoFilled /></el-icon>
                  <span>V2G Timer Configuration</span>
                </div>
                <el-form label-position="top">
                  <template v-for="(value, key) in configuration.timer" :key="key">
                    <el-form-item :label="formatLabel(key)">
                      <el-input-number v-model="configuration.timer[key]" :min="0" :max="999999" />
                    </el-form-item>
                  </template>
                </el-form>
              </div>
              <!-- SLAC -->
              <div v-show="activeConfigTab === 'slac'" class="config-content">
                <div class="config-hint">
                  <el-icon><InfoFilled /></el-icon>
                  <span>SLAC Parameter Configuration</span>
                </div>
                <el-form label-position="top">
                  <template v-for="(value, key) in configuration.slac" :key="key">
                    <el-form-item :label="formatLabel(key)">
                      <el-input v-model="configuration.slac[key]" />
                    </el-form-item>
                  </template>
                </el-form>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- Right Panel -->
      <div class="right-panel">
        <div class="code-card">
          <div class="card-header">
            <el-tabs v-model="activeViewTab" class="right-tabs">
              <el-tab-pane label="Log" name="log">
                <template #label>
                  <span class="tab-label">
                    <el-icon><Monitor /></el-icon>
                    <span>Execution Log</span>
                  </span>
                </template>
              </el-tab-pane>
              <el-tab-pane label="Sequence" name="sequence">
                <template #label>
                  <span class="tab-label">
                    <el-icon><Connection /></el-icon>
                    <span>Sequence</span>
                  </span>
                </template>
              </el-tab-pane>
            </el-tabs>
            <div class="header-actions">
              <el-button :icon="Delete" size="small" @click="clearLog" />
              <el-button :icon="Download" size="small" @click="downloadLog" />
            </div>
          </div>

          <!-- Log View -->
          <!-- Log View -->
          <div v-show="activeViewTab === 'log'" ref="logContainer" class="log-content">
            <div v-if="!loggingData" class="log-placeholder">
              <el-icon class="placeholder-icon"><Document /></el-icon>
              <p class="placeholder-text">No logs yet</p>
              <p class="placeholder-hint">Execute a test case to see logs here</p>
            </div>
            <template v-else>
              <div
                v-for="(line, index) in logLines"
                :key="index"
                class="log-line"
                :class="getLogLineClass(line)"
              >
                <span class="log-time">[{{ formatTime(new Date()) }}]</span>
                <span class="log-text">{{ line }}</span>
              </div>
            </template>
          </div>

          <!-- Sequence View -->
          <div v-show="activeViewTab === 'sequence'" class="sequence-content">
            <SequenceDiagram :log-text="loggingData" />
          </div>
        </div>
      </div>
    </div>

    <!-- Hidden file inputs -->
    <input ref="v2gUploader" type="file" accept="json/*" class="hidden" @change="handleOpenV2GConfig" />
    <input ref="slacUploader" type="file" accept="json/*" class="hidden" @change="handleOpenSLACConfig" />
  </div>
</template>

<script setup>
import { ref, computed, onMounted, watch, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { useTestStore } from '../stores/testStore'
import { ElMessage } from 'element-plus'
import SequenceDiagram from '../components/SequenceDiagram.vue'
import {
  ArrowLeft, ArrowDown, Search, Ticket, Document, RefreshLeft, FolderOpened, Download,
  VideoPlay, VideoPause, Setting, Monitor, Delete, InfoFilled, Connection, List
} from '@element-plus/icons-vue'

const router = useRouter()
const store = useTestStore()

const logContainer = ref(null)
const v2gUploader = ref(null)
const slacUploader = ref(null)
const activeConfigTab = ref('pics')
const activeViewTab = ref('log')

const target_testcase_id = ref('')
const target_testcase_name = ref('')
const secc_ip = ref('ff02::1')
const valid_testcase = ref(false)
const err_msg_validate_id = ref('')
const err_msg_validate_name = ref('')

const picsSchema = computed(() => store.SCHEMA.PICS)
const pixitSchema = computed(() => store.SCHEMA.PIXIT)
const timerSchema = computed(() => store.SCHEMA.TIMER)
const slacSchema = computed(() => store.SCHEMA.SLAC)

const configuration = ref({
  pics: {},
  pixit: {},
  timer: {},
  slac: {}
})

const queueStatus = ref({
  max_concurrent: 3,
  current_running: 0,
  pending_count: 0,
  running: [],
  pending: []
})

const isRunning = computed(() => store.get_current_execute_tc.isrunning)
const loggingData = computed(() => store.get_logging_value)
const currentExecuteTc = computed(() => store.get_current_execute_tc)
const defaultConfig = computed(() => store.get_default_config)

const validExecuteFlag = computed(() => {
  return !isRunning.value && target_testcase_id.value !== '' && target_testcase_name.value !== '' && valid_testcase.value
})

const logLines = computed(() => {
  if (!loggingData.value) return []
  return loggingData.value.split('\n').filter(line => line.trim())
})

const formatLabel = (key) => {
  return key.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase())
}

const goBack = () => {
  router.push({ name: 'testcase-list' })
}

const verifyId = async (value) => {
  if (!value) return
  const tc = store.get_testcase_by_id(value)
  if (tc) {
    loadReturnTestcase(tc)
    return
  }
  try {
    const response = await store.act_get_testcase_id(value)
    loadReturnTestcase(response.data)
  } catch (e) {
    valid_testcase.value = false
    err_msg_validate_id.value = 'Testcase ID not found'
  }
}

const verifyTestcaseName = async (value) => {
  if (!value) return
  const tc = store.get_testcase_by_name(value)
  if (tc) {
    loadReturnTestcase(tc)
    return
  }
  valid_testcase.value = false
  err_msg_validate_name.value = 'Testcase name not found'
}

const loadReturnTestcase = (testcase) => {
  target_testcase_id.value = testcase.id
  target_testcase_name.value = testcase.name
  valid_testcase.value = true
  err_msg_validate_id.value = ''
  err_msg_validate_name.value = ''
}

const resetConfig = () => {
  configuration.value = JSON.parse(JSON.stringify(defaultConfig.value))
}

const fetchQueueStatus = async () => {
  try {
    const status = await store.act_get_queue_status()
    if (status && status.queue) {
      queueStatus.value = {
        max_concurrent: status.queue.max_concurrent || 3,
        current_running: status.queue.current_running || 0,
        pending_count: status.queue.pending?.length || 0,
        running: status.queue.running || [],
        pending: status.queue.pending || []
      }
    }
  } catch (e) {
    console.log('Failed to fetch queue status:', e)
  }
}

const openV2GConfig = () => {
  v2gUploader.value?.click()
}

const openSLACConfig = () => {
  slacUploader.value?.click()
}

const handleOpenConfigCommand = (command) => {
  if (command === 'v2g') openV2GConfig()
  else if (command === 'slac') openSLACConfig()
}

const handleOpenV2GConfig = (e) => {
  const file = e.target.files[0]
  if (!file) return
  const reader = new FileReader()
  reader.onload = (event) => {
    try {
      const result = JSON.parse(event.target.result)
      if (result.timer) configuration.value.timer = { ...configuration.value.timer, ...result.timer }
      if (result.pics) configuration.value.pics = { ...configuration.value.pics, ...result.pics }
      if (result.pixit) configuration.value.pixit = { ...configuration.value.pixit, ...result.pixit }
      ElMessage.success('testconfig.json loaded')
    } catch (err) {
      ElMessage.error('Invalid testconfig.json')
    }
  }
  reader.readAsText(file)
  e.target.value = ''
}

const handleOpenSLACConfig = (e) => {
  const file = e.target.files[0]
  if (!file) return
  const reader = new FileReader()
  reader.onload = (event) => {
    try {
      const result = JSON.parse(event.target.result)
      const slac = {}
      if (result.threshold) Object.assign(slac, result.threshold)
      if (result.timer) Object.assign(slac, result.timer)
      if (Object.keys(slac).length) {
        configuration.value.slac = { ...configuration.value.slac, ...slac }
        ElMessage.success('slac_config.json loaded')
      } else {
        ElMessage.warning('No threshold or timer in slac_config.json')
      }
    } catch (err) {
      ElMessage.error('Invalid slac_config.json')
    }
  }
  reader.readAsText(file)
  e.target.value = ''
}

const storeConfig = async () => {
  try {
    const processed = await store.act_export_config(configuration.value)
    const v2gData = JSON.stringify(processed.v2g_config, null, 2)
    const v2gBlob = new Blob([v2gData], { type: 'text/json' })
    const v2gUrl = URL.createObjectURL(v2gBlob)
    const v2gLink = document.createElement('a')
    v2gLink.href = v2gUrl
    v2gLink.download = 'testconfig.json'
    v2gLink.click()
    URL.revokeObjectURL(v2gUrl)

    const slacData = JSON.stringify(processed.slac_config, null, 2)
    const slacBlob = new Blob([slacData], { type: 'text/json' })
    const slacUrl = URL.createObjectURL(slacBlob)
    const slacLink = document.createElement('a')
    slacLink.href = slacUrl
    slacLink.download = 'slac_config.json'
    slacLink.click()
    URL.revokeObjectURL(slacUrl)

    ElMessage.success('Configuration exported')
  } catch (e) {
    console.error('Export config failed:', e)
    ElMessage.error('Failed to export configuration')
  }
}

const executeTestcase = async () => {
  if (isRunning.value) return
  // Clear previous execution log before starting a new one
  store.clear_logging_data()
  store.muta_update_execute_testcase({
    id: target_testcase_id.value,
    name: target_testcase_name.value,
    isrunning: true,
    task_id: null
  })
  try {
    const response = await store.act_execute_testcase({
      id: target_testcase_id.value,
      config: configuration.value,
      secc_ip: secc_ip.value
    })
    if (response.data?.task?.task_id) {
      store.muta_update_execute_testcase({ task_id: response.data.task.task_id })
    }
    await fetchQueueStatus()
    if (response.status === 202 || response.data.status === 'QUEUED') {
      ElMessage.success(`Test queued (Task ID: ${response.data.task?.task_id})`)
    } else {
      ElMessage.success('Test execution started')
    }
  } catch (e) {
    store.muta_update_execute_testcase({ id: '', name: '', isrunning: false, task_id: null })
    ElMessage.error('Failed to start test execution')
  }
}

const cancelTestcase = async () => {
  const taskId = currentExecuteTc.value.task_id
  try {
    let cancelled = false
    if (taskId) {
      await store.act_cancel_task(taskId)
      cancelled = true
      ElMessage.success('Test execution cancelled')
    } else {
      ElMessage.warning('No active task found to cancel')
    }
    store.clear_logging_data()
    store.muta_update_execute_testcase({ id: '', name: '', isrunning: false, task_id: null })
    await fetchQueueStatus()
  } catch (e) {
    store.clear_logging_data()
    store.muta_update_execute_testcase({ id: '', name: '', isrunning: false, task_id: null })
    ElMessage.warning('Could not cancel — test may have already finished')
    await fetchQueueStatus()
  }
}

const clearLog = () => {
  store.clear_logging_data()
}

const downloadLog = () => {
  if (!loggingData.value) return
  const blob = new Blob([loggingData.value], { type: 'text/plain' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `testlog_${Date.now()}.txt`
  a.click()
  URL.revokeObjectURL(url)
}

const formatTime = (date) => {
  return date.toLocaleTimeString('en-US', { hour12: false })
}

const getLogLineClass = (line) => {
  if (line.includes('ERROR') || line.includes('FAIL')) return 'error'
  if (line.includes('PASS') || line.includes('SUCCESS')) return 'success'
  if (line.includes('WARN')) return 'warning'
  return 'normal'
}

watch(loggingData, () => {
  nextTick(() => {
    if (logContainer.value) {
      logContainer.value.scrollTop = logContainer.value.scrollHeight
    }
  })
})

onMounted(async () => {
  configuration.value = JSON.parse(JSON.stringify(defaultConfig.value))
  await fetchQueueStatus()
  const queueRefreshInterval = setInterval(fetchQueueStatus, 5000)

  const storedTc = currentExecuteTc.value
  if (storedTc.id && storedTc.name) {
    target_testcase_id.value = storedTc.id
    target_testcase_name.value = storedTc.name
    valid_testcase.value = true
    if (storedTc.isrunning) {
      try {
        await store.act_check_exec_testcase({
          id: storedTc.id,
          query_params: { action: 'get_info' }
        })
      } catch (e) {
        console.log('No running test case')
      }
    }
    return
  }

  try {
    const response = await store.act_check_exec_testcase({
      id: '1',
      query_params: { action: 'get_info' }
    })
    if (response.data) {
      const tc = response.data
      if (tc.id && tc.name) {
        target_testcase_id.value = tc.id
        target_testcase_name.value = tc.name
        valid_testcase.value = true
      }
    }
  } catch (e) {
    console.log('No running test case')
  }
})
</script>

<style scoped>
.execute-container {
  padding: 20px 24px;
  height: 100vh;
  display: flex;
  flex-direction: column;
  box-sizing: border-box;
}

.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-shrink: 0;
}

.header-left {
  display: flex;
  align-items: center;
  gap: 16px;
}

.back-btn {
  background: #1e1e2e;
  border-color: #313244;
  color: #cdd6f4;
}

.back-btn:hover {
  background: #313244;
  border-color: #45475a;
}

.header-content {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.page-title {
  font-size: 24px;
  font-weight: 600;
  color: #fff;
  margin: 0;
}

.page-subtitle {
  font-size: 14px;
  color: #9ca3af;
  margin: 0;
}

.header-right {
  display: flex;
  align-items: center;
  gap: 12px;
}

.queue-badge {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 14px;
  background: #1e1e2e;
  border: 1px solid #313244;
  border-radius: 10px;
  font-size: 13px;
  color: #9ca3af;
}

.queue-badge .queued {
  color: #f9e2af;
}

.status-indicator {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 16px;
  border-radius: 12px;
  background: #1e1e2e;
  border: 1px solid #313244;
}

.status-indicator.running {
  background: rgba(0, 212, 170, 0.1);
  border-color: #00d4aa;
}

.status-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: #6c7086;
}

.status-indicator.running .status-dot {
  background: #00d4aa;
  animation: pulse 1.5s infinite;
}

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}

.status-text {
  font-size: 14px;
  font-weight: 500;
  color: #6c7086;
}

.status-indicator.running .status-text {
  color: #00d4aa;
}

.main-content {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 20px;
  flex: 1;
  min-height: 0;
}

.left-panel {
  display: flex;
  flex-direction: column;
  gap: 16px;
  overflow-y: auto;
  min-height: 0;
  height: 100%;
}

.right-panel {
  display: flex;
  flex-direction: column;
  min-height: 0;
  height: 100%;
}

/* Card Styles */
.config-card, .code-card {
  background: #1e1e2e;
  border: 1px solid #313244;
  border-radius: 12px;
  overflow: hidden;
}

.code-card {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.config-card {
  display: flex;
  flex-direction: column;
  width: 100%;
  box-sizing: border-box;
}

/* First card (Test Case Selection): natural height */
.left-panel > .config-card:first-child {
  align-self: flex-start;
}

/* Second card (Configuration): stretch to fill remaining height */
.left-panel > .config-card:last-child {
  align-self: stretch;
  flex: 1;
  min-height: 80px;
}

.card-header {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 14px 16px;
  background: #252536;
  border-bottom: 1px solid #313244;
  font-size: 15px;
  font-weight: 600;
  color: #fff;
}

.card-header .header-tabs {
  margin-left: auto;
}

.card-body {
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 14px;
  flex: 1;
  min-height: 0;
}

.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}

.form-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.form-field.full-width {
  grid-column: span 2;
}

.form-field label {
  font-size: 13px;
  color: #9ca3af;
  font-weight: 500;
}

.form-field :deep(.el-input__wrapper) {
  background: #181825;
  border-color: #45475a;
}

.form-field :deep(.el-input__wrapper:hover) {
  border-color: #6c7086;
}

.action-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-top: 8px;
  border-top: 1px solid #313244;
}

.action-buttons {
  display: flex;
  gap: 8px;
}

/* Inner Tabs */
.inner-tabs :deep(.el-tabs__header) {
  margin: 0;
}

.inner-tabs :deep(.el-tabs__item) {
  color: #9ca3af;
  font-size: 13px;
  padding: 0 12px;
  height: 36px;
  line-height: 36px;
}

.inner-tabs :deep(.el-tabs__item.is-active) {
  color: #00d4aa;
}

.inner-tabs :deep(.el-tabs__active-bar) {
  background-color: #00d4aa;
}

/* Config Panel */
.config-panel {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}

.config-content {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.config-hint {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: #6c7086;
  padding-bottom: 8px;
}

/* Right Panel Tabs */
.code-card .card-header {
  padding: 0;
  border-bottom: 1px solid #313244;
}

.right-tabs {
  flex: 1;
}

.right-tabs :deep(.el-tabs__header) {
  margin: 0;
  background: #252536;
}

.right-tabs :deep(.el-tabs__nav-wrap) {
  padding-left: 16px;
}

.right-tabs :deep(.el-tabs__item) {
  color: #9ca3af;
  font-size: 14px;
  padding: 0 16px;
  height: 48px;
  line-height: 48px;
}

.right-tabs :deep(.el-tabs__item.is-active) {
  color: #00d4aa;
}

.right-tabs :deep(.el-tabs__active-bar) {
  background-color: #00d4aa;
}

.tab-label {
  display: flex;
  align-items: center;
  gap: 6px;
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  padding-right: 12px;
}

/* Log Content */
.log-content {
  flex: 1;
  overflow: auto;
  padding: 12px;
  background: #181825;
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: 12px;
}

.log-placeholder {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: #6c7086;
}

.placeholder-icon {
  font-size: 48px;
  margin-bottom: 12px;
  opacity: 0.5;
}

.placeholder-text {
  font-size: 15px;
  margin: 0;
}

.placeholder-hint {
  font-size: 12px;
  margin: 6px 0 0;
  opacity: 0.7;
}

.log-line {
  padding: 3px 0;
  border-bottom: 1px solid #1a1a24;
  display: flex;
  gap: 8px;
}

.log-line:last-child {
  border-bottom: none;
}

.log-time {
  color: #6c7086;
  flex-shrink: 0;
}

.log-line.error .log-text {
  color: #f38ba8;
}

.log-line.success .log-text {
  color: #a6e3a1;
}

.log-line.warning .log-text {
  color: #f9e2af;
}

.log-line.normal .log-text {
  color: #a6adc8;
}

.sequence-content {
  flex: 1;
  overflow: hidden;
}

/* Scrollbar */
::-webkit-scrollbar {
  width: 8px;
  height: 8px;
}

::-webkit-scrollbar-track {
  background: #181825;
}

::-webkit-scrollbar-thumb {
  background: #45475a;
  border-radius: 4px;
}

::-webkit-scrollbar-thumb:hover {
  background: #585b70;
}

/* Responsive */
@media (max-width: 1200px) {
  .main-content {
    grid-template-columns: 1fr;
  }

  .right-panel {
    min-height: 400px;
  }
}
</style>
