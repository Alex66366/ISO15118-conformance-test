<template>
  <div class="llm-generate-container">
    <!-- Page Header -->
    <div class="page-header">
      <div class="header-content">
        <h1 class="page-title">AI Script Generator</h1>
        <p class="page-subtitle">Generate TTCN-3 test scripts using AI</p>
      </div>
    </div>

    <!-- Main Content -->
    <div class="main-content">
      <!-- Left Panel -->
      <div class="left-panel">
        <!-- Top Section: Config & Test Intent -->
        <div class="config-section">
          <!-- LLM Configuration -->
          <div class="config-card">
            <div class="card-header">
              <el-icon><Setting /></el-icon>
              <span>LLM Configuration</span>
            </div>
            <div class="card-body">
              <div class="config-form">
                <div class="form-row">
                  <div class="form-field flex-1">
                    <label>API Key</label>
                    <el-input
                      v-model="apiKey"
                      type="password"
                      placeholder="Enter your OpenAI API Key"
                      show-password
                      clearable
                      size="default"
                    />
                  </div>
                  <div class="form-field" style="width: 140px">
                    <label>Model</label>
                    <el-select v-model="selectedModel" placeholder="Select" size="default">
                      <el-option label="GPT-4o" value="gpt-4o" />
                      <el-option label="GPT-4o-mini" value="gpt-4o-mini" />
                      <el-option label="GPT-4 Turbo" value="gpt-4-turbo" />
                      <el-option label="GPT-4" value="gpt-4" />
                      <el-option label="GPT-3.5" value="gpt-3.5-turbo" />
                    </el-select>
                  </div>
                </div>
                <div class="config-actions">
                  <div class="action-buttons">
                    <el-button type="primary" size="small" @click="verifyConfig" :loading="isVerifying">
                      Verify
                    </el-button>
                    <el-button size="small" @click="saveConfig">
                      Save
                    </el-button>
                  </div>
                  <el-tag :type="configVerified ? 'success' : (isConfigValid ? 'warning' : 'info')" size="small">
                    {{ configVerified ? 'Verified' : (isConfigValid ? 'Saved' : 'Not Configured') }}
                  </el-tag>
                </div>
              </div>
            </div>
          </div>

          <!-- Test Intent -->
          <div class="config-card">
            <div class="card-header">
              <el-icon><Edit /></el-icon>
              <span>Test Intent</span>
            </div>
            <div class="card-body">
              <div class="test-intent-form">
                <div class="form-field">
                  <label>Test Type</label>
                  <el-select v-model="testType" :disabled="isGenerating" placeholder="Select type">
                    <el-option label="CMN (Common)" value="CMN" />
                    <el-option label="AC (AC Charging)" value="AC" />
                    <el-option label="DC (DC Charging)" value="DC" />
                  </el-select>
                </div>
                <div class="form-field">
                  <label>Describe your test scenario</label>
                  <el-input
                    v-model="testIntent"
                    type="textarea"
                    :rows="4"
                    placeholder="e.g., Test V2GTP header parsing functionality..."
                    :disabled="isGenerating"
                  />
                </div>
                <div class="form-field">
                  <label>Or select a template</label>
                  <el-select v-model="selectedTemplate" clearable placeholder="Select a template" :disabled="isGenerating" @change="applyTemplate">
                    <el-option label="V2GTP Header Test" value="v2gtp_header" />
                    <el-option label="Session Setup Test" value="session_setup" />
                    <el-option label="Service Discovery Test" value="service_discovery" />
                    <el-option label="Authorization Test" value="authorization" />
                    <el-option label="Charge Parameter Test" value="charge_parameter" />
                  </el-select>
                </div>
                <el-button
                  type="primary"
                  class="generate-btn"
                  :icon="MagicStick"
                  @click="generateScript"
                  :loading="isGenerating"
                  :disabled="!isConfigValid || !testIntent.trim()"
                >
                  {{ isGenerating ? 'Generating...' : 'Generate TTCN-3 Script' }}
                </el-button>
              </div>
            </div>
          </div>
        </div>

        <!-- Bottom Section: Titan Engine -->
        <div class="engine-section">
          <div class="config-card">
            <div class="card-header">
              <el-icon><Monitor /></el-icon>
              <span>Titan Test Engine</span>
              <div class="header-right">
                <el-tag :type="titanConnected ? 'success' : 'danger'" size="small">
                  {{ titanConnected ? 'Connected' : 'Disconnected' }}
                </el-tag>
                <el-button size="small" @click="checkTitanEnv" :loading="checkingTitan" :icon="Refresh">
                  Refresh
                </el-button>
              </div>
            </div>
            <div class="card-body">
              <div class="engine-form">
                <div class="form-row">
                  <div class="form-field flex-1">
                    <label>SECC IP Address</label>
                    <el-input
                      v-model="seccIp"
                      placeholder="ff02::1"
                      :disabled="isCompiling || isExecuting"
                    />
                  </div>
                  <div class="form-field flex-1">
                    <label>Testcase Name (for DB)</label>
                    <el-select
                      v-model="testcaseName"
                      placeholder="Select or enter name"
                      filterable
                      allow-create
                      default-first-option
                      clearable
                      :disabled="isCompiling || isExecuting"
                      :loading="loadingTestcases"
                      style="width: 100%"
                    >
                      <el-option
                        v-for="tc in generatedTestcases"
                        :key="tc.id"
                        :label="tc.name"
                        :value="tc.name"
                      >
                        <div class="tc-option">
                          <span>{{ tc.name }}</span>
                          <el-tag size="small" :type="tc.compile_status === 'success' ? 'success' : (tc.compile_status === 'failed' ? 'danger' : 'info')">
                            {{ tc.compile_status }}
                          </el-tag>
                        </div>
                      </el-option>
                    </el-select>
                  </div>
                </div>
                <div class="engine-actions">
                  <el-checkbox v-model="saveToDb" :disabled="isCompiling || isExecuting">
                    Save to database after execution
                  </el-checkbox>
                  <div class="button-group">
                    <el-button
                      type="warning"
                      :icon="Files"
                      @click="compileCode"
                      :loading="isCompiling"
                      :disabled="!generatedCode || !titanConnected"
                    >
                      {{ isCompiling ? 'Compiling...' : 'Compile' }}
                    </el-button>
                    <el-button
                      type="success"
                      :icon="VideoPlay"
                      @click="executeCode"
                      :loading="isExecuting"
                      :disabled="!generatedCode || !titanConnected || !compiledSuccessfully"
                    >
                      {{ isExecuting ? 'Executing...' : 'Execute' }}
                    </el-button>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <!-- Example Test Scenarios -->
          <div class="config-card examples-card">
            <div class="card-header">
              <el-icon><Document /></el-icon>
              <span>Example Test Scenarios</span>
            </div>
            <div class="card-body">
              <div class="examples-grid">
                <div
                  v-for="example in examples"
                  :key="example.id"
                  class="example-item"
                  @click="testIntent = example.intent; testType = example.type"
                >
                  <div class="example-name">{{ example.name }}</div>
                  <div class="example-type">
                    <el-tag size="small" type="info">{{ example.type }}</el-tag>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- Right Panel - Output -->
      <div class="right-panel">
        <div class="code-card">
          <div class="card-header">
            <el-tabs v-model="activeRightTab" class="right-tabs">
              <el-tab-pane label="Generated Code" name="code">
                <template #label>
                  <span class="tab-label">
                    <el-icon><DocumentCopy /></el-icon>
                    <span>Generated Code</span>
                  </span>
                </template>
              </el-tab-pane>
              <el-tab-pane label="Execution Log" name="log">
                <template #label>
                  <span class="tab-label">
                    <el-icon><Monitor /></el-icon>
                    <span>Execution Log</span>
                  </span>
                </template>
              </el-tab-pane>
            </el-tabs>
            <div class="header-actions">
              <el-button :icon="CopyDocument" size="small" @click="copyCode" :disabled="!generatedCode || activeRightTab !== 'code'" />
              <el-button :icon="Download" size="small" @click="handleDownload" :disabled="!hasContent" />
              <el-button v-if="activeRightTab === 'log'" :icon="Delete" size="small" @click="clearLog" :disabled="!executionOutput" />
            </div>
          </div>

          <!-- Generated Code View -->
          <div v-show="activeRightTab === 'code'" class="tab-content">
            <div class="code-content">
              <div v-if="!generatedCode" class="code-placeholder">
                <el-icon class="placeholder-icon"><Document /></el-icon>
                <p class="placeholder-text">No generated code yet</p>
                <p class="placeholder-hint">Enter a test intent and click Generate</p>
              </div>
              <pre v-else class="code-display"><code>{{ generatedCode }}</code></pre>
            </div>

            <!-- Status Section -->
            <div class="status-section">
              <div v-if="errorMessage" class="status-message error">
                <p>{{ errorMessage }}</p>
              </div>
              <div v-else-if="generatedCode" class="status-message success">
                <p>Script generated successfully using {{ modelName }}</p>
              </div>

              <!-- Compilation & Execution Status -->
              <div v-if="compileStatus || executionResult" class="result-section">
                <div v-if="compileStatus" class="result-item" :class="compileStatus">
                  <span class="result-label">Compilation:</span>
                  <span class="result-value">{{ compileStatus === 'success' ? 'Successful' : 'Failed' }}</span>
                </div>
                <div v-if="executionResult" class="result-item" :class="executionResult">
                  <span class="result-label">Execution:</span>
                  <span class="result-value">{{ executionResult.toUpperCase() }}</span>
                </div>
              </div>
            </div>
          </div>

          <!-- Execution Log View -->
          <div v-show="activeRightTab === 'log'" class="tab-content">
            <div ref="logContainer" class="log-content">
              <div v-if="!executionOutput" class="log-placeholder">
                <el-icon class="placeholder-icon"><Monitor /></el-icon>
                <p class="placeholder-text">No execution logs yet</p>
                <p class="placeholder-hint">Compile and execute a test to see logs</p>
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
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, watch, nextTick } from 'vue'
import { ElMessage } from 'element-plus'
import { Edit, MagicStick, Document, DocumentCopy, CopyDocument, Download, Setting, Key, Check, FolderChecked, Files, VideoPlay, Monitor, Refresh, Connection, Delete } from '@element-plus/icons-vue'
import api from '../api'

const apiKey = ref('')
const selectedModel = ref('gpt-4o')
const testIntent = ref('')
const testType = ref('CMN')
const selectedTemplate = ref('')
const isGenerating = ref(false)
const isVerifying = ref(false)
const generatedCode = ref('')
const errorMessage = ref('')
const modelName = ref('')
const configVerified = ref(false)

// Right panel tabs
const activeRightTab = ref('code')
const logContainer = ref(null)

const titanConnected = ref(false)
const checkingTitan = ref(false)
const isCompiling = ref(false)
const isExecuting = ref(false)
const seccIp = ref('ff02::1')
const testcaseName = ref('LLM_Generated_Test')
const generatedTestcases = ref([])
const loadingTestcases = ref(false)
const saveToDb = ref(false)
const compileStatus = ref('')
const compiledSuccessfully = ref(false)
const executionResult = ref('')
const executionOutput = ref('')
const currentProjectPath = ref('')

onMounted(() => {
  const savedConfig = localStorage.getItem('llmConfig')
  if (savedConfig) {
    try {
      const config = JSON.parse(savedConfig)
      apiKey.value = config.apiKey || ''
      selectedModel.value = config.model || 'gpt-4o'
    } catch (e) {
      console.error('Failed to load config:', e)
    }
  }
  checkTitanEnv()
  fetchGeneratedTestcases()
})

// Auto-scroll log
watch(executionOutput, () => {
  nextTick(() => {
    if (logContainer.value && activeRightTab.value === 'log') {
      logContainer.value.scrollTop = logContainer.value.scrollHeight
    }
  })
})

const isConfigValid = computed(() => {
  return apiKey.value.trim().length > 0
})

const hasContent = computed(() => {
  return activeRightTab.value === 'code' ? !!generatedCode.value : !!executionOutput.value
})

const logLines = computed(() => {
  if (!executionOutput.value) return []
  return executionOutput.value.split('\n').filter(line => line.trim())
})

const formatTime = (date) => {
  return date.toLocaleTimeString('en-US', { hour12: false })
}

const getLogLineClass = (line) => {
  if (line.includes('ERROR') || line.includes('FAIL')) return 'error'
  if (line.includes('PASS') || line.includes('SUCCESS')) return 'success'
  if (line.includes('WARN')) return 'warning'
  return 'normal'
}

const verifyConfig = async () => {
  if (!apiKey.value.trim()) {
    ElMessage.warning('Please enter an API key')
    return
  }
  isVerifying.value = true
  configVerified.value = false
  try {
    const response = await api.verifyLLMConfig(apiKey.value, selectedModel.value)
    const data = response.data
    if (data.status === 'success') {
      configVerified.value = true
      ElMessage.success('Configuration verified successfully')
    } else {
      ElMessage.error(data.message || 'Verification failed')
    }
  } catch (error) {
    ElMessage.error(error.response?.data?.message || 'Failed to verify configuration')
  } finally {
    isVerifying.value = false
  }
}

const saveConfig = () => {
  if (!apiKey.value.trim()) {
    ElMessage.warning('Please enter an API key')
    return
  }
  localStorage.setItem('llmConfig', JSON.stringify({
    apiKey: apiKey.value,
    model: selectedModel.value
  }))
  ElMessage.success('Configuration saved')
}

const examples = [
  { id: 1, name: 'V2GTP Header Test', type: 'CMN', intent: 'Test V2GTP header version and message type parsing for correct identification' },
  { id: 2, name: 'Session Setup', type: 'CMN', intent: 'Test session setup request and response message exchange with proper EVCCID' },
  { id: 3, name: 'Service Discovery', type: 'AC', intent: 'Test service discovery request to find available charging services' },
  { id: 4, name: 'Authorization', type: 'AC', intent: 'Test authorization process with EIM payment method' },
  { id: 5, name: 'DC Charge Parameter', type: 'DC', intent: 'Test DC charging parameter discovery with EVMaximumCurrentLimit' }
]

const templates = {
  v2gtp_header: 'Test V2GTP header parsing functionality, verify protocol version is 0x0001, message type correctly identifies the payload, and payload length matches',
  session_setup: 'Test ISO 15118 session setup process, create a new charging session, verify EVCCID is transmitted correctly, and receive valid session ID',
  service_discovery: 'Test service discovery, request available services from SECC, verify service list contains expected charging services',
  authorization: 'Test authorization process, send authorization request with EIM, verify authorization response indicates success',
  charge_parameter: 'Test charge parameter discovery, send EVChargingParameter with maximum current and voltage limits, verify SECC accepts parameters'
}

const applyTemplate = () => {
  if (selectedTemplate.value && templates[selectedTemplate.value]) {
    testIntent.value = templates[selectedTemplate.value]
  }
}

const generateScript = async () => {
  if (!testIntent.value.trim()) {
    ElMessage.warning('Please enter a test intent')
    return
  }
  if (!isConfigValid.value) {
    ElMessage.warning('Please configure API key first')
    return
  }
  isGenerating.value = true
  errorMessage.value = ''
  generatedCode.value = ''
  try {
    const response = await api.generateTestScript(testIntent.value, testType.value, apiKey.value, selectedModel.value)
    const data = response.data
    if (data.status === 'success') {
      generatedCode.value = data.code
      modelName.value = data.model || selectedModel.value
      ElMessage.success('Script generated successfully')
    } else {
      errorMessage.value = data.message || 'Failed to generate script'
      ElMessage.error(errorMessage.value)
    }
  } catch (error) {
    errorMessage.value = error.response?.data?.message || error.message || 'Network error'
    ElMessage.error('Failed to generate script')
  } finally {
    isGenerating.value = false
  }
}

const copyCode = async () => {
  if (!generatedCode.value) return
  try {
    await navigator.clipboard.writeText(generatedCode.value)
    ElMessage.success('Code copied to clipboard')
  } catch (err) {
    ElMessage.error('Failed to copy code')
  }
}

const downloadCode = () => {
  if (!generatedCode.value) return
  const blob = new Blob([generatedCode.value], { type: 'text/plain' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `test_script_${Date.now()}.ttcn`
  a.click()
  URL.revokeObjectURL(url)
  ElMessage.success('Code downloaded')
}

const clearLog = () => {
  executionOutput.value = ''
}

const handleDownload = () => {
  if (activeRightTab.value === 'code') {
    downloadCode()
  } else {
    if (!executionOutput.value) return
    const blob = new Blob([executionOutput.value], { type: 'text/plain' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `execution_log_${Date.now()}.txt`
    a.click()
    URL.revokeObjectURL(url)
    ElMessage.success('Log downloaded')
  }
}

const checkTitanEnv = async () => {
  checkingTitan.value = true
  try {
    const response = await api.checkTitanEnv()
    const data = response.data
    titanConnected.value = data.connected && data.titan_available
    if (!titanConnected.value) {
      ElMessage.warning(data.message || 'Titan environment not available')
    }
  } catch (error) {
    titanConnected.value = false
    ElMessage.error('Failed to check Titan environment')
  } finally {
    checkingTitan.value = false
  }
}

const fetchGeneratedTestcases = async () => {
  loadingTestcases.value = true
  try {
    const response = await api.getGeneratedTestcases()
    const data = response.data
    if (data.status === 'success') {
      generatedTestcases.value = data.testcases || []
    }
  } catch (error) {
    console.error('Failed to fetch generated testcases:', error)
  } finally {
    loadingTestcases.value = false
  }
}

const compileCode = async () => {
  if (!generatedCode.value) return
  isCompiling.value = true
  compileStatus.value = ''
  compiledSuccessfully.value = false
  errorMessage.value = ''
  try {
    const response = await api.compileTTCNCode(generatedCode.value, null, 'TestSuite')
    const data = response.data
    if (data.status === 'success') {
      compileStatus.value = 'success'
      compiledSuccessfully.value = true
      currentProjectPath.value = data.project_path
      ElMessage.success('Compilation successful')
    } else {
      compileStatus.value = 'failed'
      errorMessage.value = data.errors || 'Compilation failed'
      ElMessage.error('Compilation failed')
    }
  } catch (error) {
    compileStatus.value = 'failed'
    errorMessage.value = error.response?.data?.message || 'Compilation error'
    ElMessage.error('Compilation error')
  } finally {
    isCompiling.value = false
  }
}

const executeCode = async () => {
  if (!generatedCode.value || !currentProjectPath.value) {
    ElMessage.warning('Please compile the code first')
    return
  }
  isExecuting.value = true
  executionResult.value = ''
  executionOutput.value = ''
  errorMessage.value = ''
  try {
    const response = await api.executeTTCNTest(currentProjectPath.value, 'TestSuite', seccIp.value)
    const data = response.data
    executionResult.value = data.result
    executionOutput.value = data.output || data.errors || ''
    if (data.status === 'success') {
      ElMessage.success(`Test execution completed: ${data.result.toUpperCase()}`)
    } else {
      ElMessage.warning(`Test execution completed: ${data.result.toUpperCase()}`)
    }
  } catch (error) {
    errorMessage.value = error.response?.data?.message || 'Execution error'
    ElMessage.error('Test execution error')
  } finally {
    isExecuting.value = false
  }
}
</script>

<style scoped>
.llm-generate-container {
  padding: 20px 24px;
  height: 100vh;
  display: flex;
  flex-direction: column;
  box-sizing: border-box;
}

.page-header {
  flex-shrink: 0;
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
}

.config-section {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.engine-section {
  display: flex;
  flex-direction: column;
  gap: 16px;
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

.card-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 14px 16px;
  background: #252536;
  border-bottom: 1px solid #313244;
  font-size: 15px;
  font-weight: 600;
  color: #fff;
}

.card-header .header-right {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 10px;
}

.card-body {
  padding: 16px;
}

/* Form Styles */
.config-form, .test-intent-form, .engine-form {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.form-row {
  display: flex;
  gap: 12px;
}

.form-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.form-field label {
  font-size: 13px;
  color: #9ca3af;
  font-weight: 500;
}

.form-field :deep(.el-input__wrapper),
.form-field :deep(.el-select) {
  background: #181825;
  border-color: #45475a;
}

.form-field :deep(.el-input__wrapper:hover),
.form-field :deep(.el-select:hover .el-input__wrapper) {
  border-color: #6c7086;
}

.config-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-top: 4px;
}

.action-buttons {
  display: flex;
  gap: 8px;
}

.generate-btn {
  width: 100%;
  height: 42px;
  font-size: 15px;
  margin-top: 6px;
}

/* Engine Section */
.engine-form .form-row {
  margin-bottom: 4px;
}

.engine-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-top: 8px;
  border-top: 1px solid #313244;
  margin-top: 4px;
}

.button-group {
  display: flex;
  gap: 10px;
}

.tc-option {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
}

/* Examples */
.examples-card .card-body {
  padding: 12px;
}

.examples-grid {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.example-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 12px;
  background: #181825;
  border-radius: 8px;
  cursor: pointer;
  transition: all 0.2s;
}

.example-item:hover {
  background: #313244;
}

.example-name {
  font-size: 13px;
  color: #cdd6f4;
}

/* Code Panel */
.code-card {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 500px;
}

.code-card .card-body {
  flex: 1;
  display: flex;
  flex-direction: column;
  padding: 0;
  min-height: 0;
}

.code-content {
  flex: 1;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

.code-placeholder {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  color: #6c7086;
}

.placeholder-icon {
  font-size: 56px;
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

.code-display {
  flex: 1;
  margin: 0;
  padding: 16px;
  overflow: auto;
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: 13px;
  line-height: 1.6;
  color: #a6adc8;
  background: #181825;
  white-space: pre-wrap;
  word-wrap: break-word;
}

/* Status Section */
.status-section {
  padding: 14px 16px;
  border-top: 1px solid #313244;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.status-message {
  padding: 10px 14px;
  border-radius: 8px;
  font-size: 13px;
}

.status-message.error {
  background: rgba(239, 68, 68, 0.15);
  border: 1px solid rgba(239, 68, 68, 0.3);
  color: #f38ba8;
}

.status-message.success {
  background: rgba(166, 227, 161, 0.15);
  border: 1px solid rgba(166, 227, 161, 0.3);
  color: #a6e3a1;
}

.result-section {
  display: flex;
  gap: 12px;
}

.result-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-radius: 6px;
  font-size: 13px;
}

.result-item.success {
  background: rgba(166, 227, 161, 0.15);
  color: #a6e3a1;
}

.result-item.failed {
  background: rgba(239, 68, 68, 0.15);
  color: #f38ba8;
}

.result-item.pass {
  background: rgba(166, 227, 161, 0.15);
  color: #a6e3a1;
}

.result-item.fail {
  background: rgba(239, 68, 68, 0.15);
  color: #f38ba8;
}

.result-label {
  opacity: 0.8;
}

.result-value {
  font-weight: 600;
}

.output-collapse {
  border: none;
  background: transparent;
}

.output-collapse :deep(.el-collapse-item__header) {
  background: transparent;
  border: none;
  color: #9ca3af;
  font-size: 13px;
}

.output-collapse :deep(.el-collapse-item__wrap) {
  background: transparent;
  border: none;
}

.output-collapse :deep(.el-collapse-item__content) {
  padding: 0;
}

.output-log {
  margin: 0;
  padding: 12px;
  background: #181825;
  border-radius: 6px;
  font-size: 11px;
  color: #6c7086;
  max-height: 120px;
  overflow: auto;
  white-space: pre-wrap;
  word-wrap: break-word;
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

.tab-content {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

/* Log View */
.log-content {
  flex: 1;
  overflow: auto;
  padding: 12px;
  background: #181825;
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: 12px;
  line-height: 1.6;
}

.log-placeholder {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: #6c7086;
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
</style>
