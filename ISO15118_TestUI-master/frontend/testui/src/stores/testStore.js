import { defineStore } from 'pinia'
import api from '../api'
import picsSchemaData from '../assets/pics_schema.json'
import pixitSchemaData from '../assets/pixit_schema.json'
import timerSchemaData from '../assets/timer_schema.json'
import slacSchemaData from '../assets/slac_schema.json'

// Simple defaults function
const getDefaults = (schema) => {
  const defaults = {}
  if (schema?.properties) {
    for (const key in schema.properties) {
      const prop = schema.properties[key]
      if (prop.default !== undefined) {
        defaults[key] = prop.default
      } else if (prop.type === 'object' && prop.properties) {
        defaults[key] = getDefaults(prop)
      } else if (prop.type === 'array') {
        defaults[key] = []
      } else if (prop.type === 'string') {
        defaults[key] = ''
      } else if (prop.type === 'boolean') {
        defaults[key] = false
      } else if (prop.type === 'number') {
        defaults[key] = 0
      }
    }
  }
  return defaults
}

export const useTestStore = defineStore('test', {
  state: () => ({
    SCHEMA: {
      PICS: picsSchemaData,
      PIXIT: pixitSchemaData,
      TIMER: timerSchemaData,
      SLAC: slacSchemaData
    },
    summaryInfo: [],
    testcaseList: [],
    defaultConfig: {
      pics: getDefaults(picsSchemaData),
      pixit: getDefaults(pixitSchemaData),
      timer: getDefaults(timerSchemaData),
      slac: getDefaults(slacSchemaData)
    },
    currentConfig: {
      pics: getDefaults(picsSchemaData),
      pixit: getDefaults(pixitSchemaData),
      timer: getDefaults(timerSchemaData),
      slac: getDefaults(slacSchemaData)
    },
    executeTestcase: {
      id: '',
      name: '',
      isrunning: false,
      status: 'n',
      build_status: 'NA',
      pics: {},
      pixit: {}
    },
    taskQueue: {
      running: [],
      pending: [],
      maxConcurrent: 3,
      currentRunning: 0
    },
    loggingData: '',
    loggingSocket: null,
    isLoading: false
  }),

  getters: {
    get_testcase_list: (state) => state.testcaseList,
    get_summary: (state) => state.summaryInfo,
    get_current_execute_tc: (state) => state.executeTestcase,
    get_default_config: (state) => state.defaultConfig,
    get_current_config: (state) => state.currentConfig,
    get_logging_value: (state) => state.loggingData,
    get_task_queue: (state) => state.taskQueue,
    get_testcase_by_id: (state) => (id) => state.testcaseList.find(tc => tc.id === id),
    get_testcase_by_name: (state) => (name) => state.testcaseList.find(tc => tc.name === name)
  },

  actions: {
    async act_get_testcases(queryParams = {}) {
      this.isLoading = true
      try {
        const response = await api.getTestcases(queryParams)
        this.testcaseList = response.data
        return response
      } catch (error) {
        console.error('Failed to fetch testcases:', error)
        throw error
      } finally {
        this.isLoading = false
      }
    },

    async act_get_testcase_id(id) {
      try {
        const response = await api.getTestcase(id)
        return response
      } catch (error) {
        console.error('Failed to fetch testcase:', error)
        throw error
      }
    },

    async act_get_summary() {
      this.isLoading = true
      try {
        const response = await api.getSummary()
        this.summaryInfo = response.data
        return response
      } catch (error) {
        console.error('Failed to fetch summary:', error)
        throw error
      } finally {
        this.isLoading = false
      }
    },

    async act_execute_testcase({ id, config, secc_ip = 'ff02::1' }) {
      try {
        const response = await api.executeTestcase(id, config, secc_ip)
        this.executeTestcase.isrunning = true
        return response
      } catch (error) {
        console.error('Failed to execute testcase:', error)
        throw error
      }
    },

    async act_check_exec_testcase({ id, query_params }) {
      try {
        const response = await api.checkExecution(id, query_params)
        this.muta_update_execute_testcase(response.data)
        return response
      } catch (error) {
        console.error('Failed to check execution:', error)
        throw error
      }
    },

    async act_export_config(config) {
      try {
        const response = await api.exportConfig(config)
        return response.data
      } catch (error) {
        console.error('Failed to export config:', error)
        throw error
      }
    },

    muta_update_execute_testcase(data) {
      this.executeTestcase = { ...this.executeTestcase, ...data }
    },

    muta_update_task_queue(data) {
      this.taskQueue = { ...this.taskQueue, ...data }
    },

    muta_update_partial_current_cfg(partialConfig) {
      this.currentConfig = { ...this.currentConfig, ...partialConfig }
    },

    muta_update_default_cfg({ default_config }) {
      this.defaultConfig = { ...default_config }
    },

    mutate_update_logging_data(message) {
      this.loggingData += message + '\n'
    },

    clear_logging_data() {
      this.loggingData = ''
    },

    async act_update_testcase({ id, data }) {
      try {
        const response = await api.updateTestcase(id, data)
        return response
      } catch (error) {
        console.error('Failed to update testcase:', error)
        throw error
      }
    },

    async act_remove_testcase_id(id) {
      try {
        const response = await api.deleteTestcase(id)
        this.testcaseList = this.testcaseList.filter(tc => tc.id !== id)
        return response
      } catch (error) {
        console.error('Failed to remove testcase:', error)
        throw error
      }
    },

    // Task Queue Management
    async act_get_queue_status() {
      try {
        const response = await api.getQueueStatus()
        return response.data
      } catch (error) {
        console.error('Failed to get queue status:', error)
        throw error
      }
    },

    async act_get_task_status(taskId) {
      try {
        const response = await api.getTaskStatus(taskId)
        return response.data
      } catch (error) {
        console.error('Failed to get task status:', error)
        throw error
      }
    },

    async act_cancel_task(taskId) {
      try {
        const response = await api.cancelTask(taskId)
        return response.data
      } catch (error) {
        console.error('Failed to cancel task:', error)
        throw error
      }
    },

    initWebSocket() {
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      const wsUrl = `${protocol}//${window.location.host}/ws/logging`
      this.loggingSocket = new WebSocket(wsUrl)

      this.loggingSocket.onopen = () => {
        console.log('WebSocket connected')
        this.act_check_exec_testcase({ id: '1', query_params: { action: 'get_info' } })
      }

      this.loggingSocket.onmessage = (response) => {
        const recv = JSON.parse(response.data)
        if (recv.status === 'update') {
          this.act_check_exec_testcase({ id: '1', query_params: { action: 'get_info' } })
        }
        this.mutate_update_logging_data(recv.message)
      }

      this.loggingSocket.onerror = (error) => {
        console.error('WebSocket error:', error)
      }

      this.loggingSocket.onclose = () => {
        console.log('WebSocket disconnected')
      }
    }
  }
})
