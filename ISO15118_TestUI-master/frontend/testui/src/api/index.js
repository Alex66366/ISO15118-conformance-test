import axios from 'axios'

const api = axios.create({
  baseURL: '/api/v1',
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json'
  }
})

// Request interceptor
api.interceptors.request.use(
  config => {
    return config
  },
  error => {
    return Promise.reject(error)
  }
)

// Response interceptor
api.interceptors.response.use(
  response => {
    return response
  },
  error => {
    console.error('API Error:', error)
    return Promise.reject(error)
  }
)

export default {
  // Testcases
  getTestcases(params = {}) {
    return api.get('/testcases/', { params })
  },

  getTestcase(id) {
    return api.get(`/testcases/${id}/`)
  },

  createTestcase(data) {
    return api.post('/testcases/', data)
  },

  updateTestcase(id, data) {
    return api.put(`/testcases/${id}/`, data)
  },

  deleteTestcase(id) {
    return api.delete(`/testcases/${id}/`)
  },

  // Execution
  executeTestcase(id, config, secc_ip = 'ff02::1') {
    return api.put(`/execute/${id}/`, { ...config, secc_ip })
  },

  checkExecution(id, params = {}) {
    return api.get(`/execute/${id}/`, { params })
  },

  // Summary
  getSummary() {
    return api.get('/summary/')
  },

  // Export config (processed by backend)
  exportConfig(config) {
    return api.post('/execute/export_config/', config)
  },

  // Clear test results
  clearData() {
    return api.post('/clear/clear/')
  },

  // LLM Script Generation
  generateTestScript(testIntent, testType = 'CMN', apiKey = '', model = 'gpt-4o') {
    const config = {}
    if (apiKey) {
      config.headers = { 'X-LLM-Api-Key': apiKey }
    }
    return api.post('/llm/generate/', {
      test_intent: testIntent,
      test_type: testType,
      api_key: apiKey,
      model: model
    }, config)
  },

  getLLMConfig() {
    return api.get('/llm/generate/config/')
  },

  verifyLLMConfig(apiKey, model = 'gpt-4o') {
    return api.post('/llm/generate/verify/', {
      api_key: apiKey,
      model: model
    })
  },

  // 获取AI生成的测试用例列表
  getGeneratedTestcases() {
    return api.get('/llm/generate/list_generated/')
  },

  // TTCN-3 Titan Engine
  checkTitanEnv() {
    return api.get('/ttcn/compile/check_env/')
  },

  compileTTCNCode(code, projectName = null, mainModule = 'TestSuite') {
    return api.post('/ttcn/compile/compile/', {
      code: code,
      project_name: projectName,
      main_module: mainModule
    })
  },

  executeTTCNTest(projectPath, mainModule = 'TestSuite', seccIp = 'ff02::1', configFile = null) {
    return api.post('/ttcn/execute/execute/', {
      project_path: projectPath,
      main_module: mainModule,
      secc_ip: seccIp,
      config_file: configFile
    })
  },

  compileAndExecuteTTCN(code, projectName = null, mainModule = 'TestSuite', seccIp = 'ff02::1', saveToDb = false, testcaseName = 'LLM_Generated_Test') {
    return api.post('/ttcn/execute/compile_and_execute/', {
      code: code,
      project_name: projectName,
      main_module: mainModule,
      secc_ip: seccIp,
      save_to_db: saveToDb,
      testcase_name: testcaseName
    }, {
      timeout: 600000  // 10 minutes timeout for compile and execute
    })
  },

  // Task Queue Management
  getQueueStatus() {
    return api.get('/execute/', { params: { action: 'queue_status' } })
  },

  getTaskStatus(taskId) {
    return api.get('/execute/', { params: { action: 'task_status', task_id: taskId } })
  },

  cancelTask(taskId) {
    return api.post('/tasks/cancel/', { task_id: taskId })
  },

  getTasksList() {
    return api.get('/tasks/')
  },

  // Resource Pool Management
  getResourcesList() {
    return api.get('/resources/')
  },

  getAvailableResources() {
    return api.get('/resources/available/')
  }
}
