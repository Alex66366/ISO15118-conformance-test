<template>
  <div class="space-y-6">
    <!-- Page Header -->
    <div class="flex items-center justify-between">
      <div>
        <h1 class="text-2xl font-semibold text-white">Dashboard</h1>
        <p class="text-gray-500 mt-1">ISO15118-2 Message Test Summary</p>
      </div>
      <div class="flex items-center gap-3">
        <el-button type="danger" :icon="Delete" @click="handleClearData" :loading="clearLoading">
          Clear Data
        </el-button>
        <el-dropdown trigger="click" @command="handleExport">
          <el-button type="primary" :icon="Download" :loading="isLoading">
            Export
            <el-icon class="el-icon--right"><arrow-down /></el-icon>
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="csv">Export as CSV</el-dropdown-item>
              <el-dropdown-item command="json">Export as JSON</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
        <el-button type="primary" :icon="Refresh" @click="retrieveSummary" :loading="isLoading">
          Refresh
        </el-button>
      </div>
    </div>

    <!-- Stats Cards -->
    <div class="grid grid-cols-1 md:grid-cols-4 gap-4">
      <div
        v-for="(stat, index) in stats"
        :key="stat.label"
        class="stat-card animate-slide-up"
        :style="{ animationDelay: `${index * 100}ms` }"
      >
        <div class="flex items-start justify-between">
          <div>
            <p class="text-gray-500 text-sm font-medium">{{ stat.label }}</p>
            <p class="text-3xl font-bold text-white mt-2">{{ stat.value }}</p>
            <div class="flex items-center gap-1 mt-2">
              <span :class="stat.change >= 0 ? 'text-accent' : 'text-red-400'" class="text-sm font-medium">
                {{ stat.change >= 0 ? '+' : '' }}{{ stat.change }}%
              </span>
              <span class="text-gray-500 text-xs">vs last run</span>
            </div>
          </div>
          <div :class="`stat-icon ${stat.iconBg}`">
            <el-icon class="text-2xl" :class="stat.iconColor">
              <component :is="stat.icon" />
            </el-icon>
          </div>
        </div>
      </div>
    </div>

    <!-- Charts Row -->
    <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
      <!-- Pass Rate Chart -->
      <div class="bg-dark-card rounded-2xl border border-dark-border p-6">
        <h3 class="text-lg font-semibold text-white mb-4">Pass Rate by Message Type</h3>
        <div class="h-64 flex items-center justify-center">
          <div v-if="passRateData.length > 0" class="w-full">
            <div v-for="item in passRateData" :key="item.name" class="mb-4">
              <div class="flex justify-between text-sm mb-1">
                <span class="text-gray-400">{{ item.name }}</span>
                <span class="text-white font-medium">{{ item.rate }}%</span>
              </div>
              <div class="h-2 bg-dark-bg rounded-full overflow-hidden">
                <div
                  class="h-full rounded-full transition-all duration-500"
                  :class="item.color"
                  :style="{ width: `${item.rate}%` }"
                ></div>
              </div>
            </div>
          </div>
          <div v-else class="text-gray-500">No data available</div>
        </div>
      </div>

      <!-- Test Distribution -->
      <div class="bg-dark-card rounded-2xl border border-dark-border p-6">
        <h3 class="text-lg font-semibold text-white mb-4">Test Distribution</h3>
        <div class="h-64 flex items-center justify-center">
          <div class="grid grid-cols-3 gap-4 w-full">
            <div class="text-center p-4 rounded-xl bg-dark-bg">
              <p class="text-3xl font-bold text-blue-400">{{ testDistribution.CMN }}</p>
              <p class="text-sm text-gray-500 mt-1">CMN</p>
            </div>
            <div class="text-center p-4 rounded-xl bg-dark-bg">
              <p class="text-3xl font-bold text-green-400">{{ testDistribution.AC }}</p>
              <p class="text-sm text-gray-500 mt-1">AC</p>
            </div>
            <div class="text-center p-4 rounded-xl bg-dark-bg">
              <p class="text-3xl font-bold text-purple-400">{{ testDistribution.DC }}</p>
              <p class="text-sm text-gray-500 mt-1">DC</p>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Message Summary Table -->
    <div class="bg-dark-card rounded-2xl border border-dark-border overflow-hidden animate-slide-up" style="animation-delay: 400ms">
      <div class="px-6 py-4 border-b border-dark-border flex items-center justify-between">
        <div class="flex items-center gap-3">
          <div class="w-10 h-10 rounded-xl bg-primary-500/10 flex items-center justify-center">
            <el-icon class="text-xl text-primary-500"><DataAnalysis /></el-icon>
          </div>
          <div>
            <h2 class="text-lg font-semibold text-white">Message Test Summary</h2>
            <p class="text-sm text-gray-500">Overview of all V2G message test cases</p>
          </div>
        </div>
        <el-input
          v-model="searchQuery"
          placeholder="Search messages..."
          class="w-64"
          :prefix-icon="Search"
          clearable
        />
      </div>

      <!-- Skeleton Loading -->
      <div v-if="isLoading" class="p-6">
        <div class="space-y-4">
          <div v-for="i in 5" :key="i" class="flex items-center gap-4">
            <div class="w-8 h-8 rounded-lg bg-dark-bg animate-pulse"></div>
            <div class="flex-1 h-4 bg-dark-bg rounded animate-pulse"></div>
            <div class="w-24 h-4 bg-dark-bg rounded animate-pulse"></div>
            <div class="w-24 h-4 bg-dark-bg rounded animate-pulse"></div>
            <div class="w-24 h-4 bg-dark-bg rounded animate-pulse"></div>
          </div>
        </div>
      </div>

      <!-- Empty State -->
      <div v-else-if="filteredSummary.length === 0" class="flex flex-col items-center justify-center py-16">
        <el-icon class="text-6xl text-gray-600 mb-4"><Document /></el-icon>
        <h3 class="text-lg font-medium text-white mb-2">No Results Found</h3>
        <p class="text-gray-500 mb-4">Try adjusting your search query</p>
        <el-button type="primary" @click="searchQuery = ''">Clear Search</el-button>
      </div>

      <el-table
        v-else
        :data="filteredSummary"
        style="width: 100%"
        :header-cell-style="{ background: '#12121a', color: '#9ca3af', fontWeight: '500', fontSize: '12px', textTransform: 'uppercase', letterSpacing: '0.05em' }"
        :row-style="{ background: 'transparent' }"
        @row-click="selectMsg"
        class="message-table"
      >
        <el-table-column prop="name" label="Message" min-width="280">
          <template #default="{ row }">
            <div class="flex items-center gap-3">
              <div class="w-8 h-8 rounded-lg bg-dark-elevated flex items-center justify-center">
                <el-icon class="text-gray-400"><Document /></el-icon>
              </div>
              <span class="font-mono text-sm text-white">{{ formatMessageName(row.name) }}</span>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="CMN (Common)" min-width="140" align="center">
          <template #default="{ row }">
            <TestProgressBar :pass="row.CMN?.pass || 0" :total="row.CMN?.total || 0" />
          </template>
        </el-table-column>

        <el-table-column label="AC (AC Charging)" min-width="140" align="center">
          <template #default="{ row }">
            <TestProgressBar :pass="row.AC?.pass || 0" :total="row.AC?.total || 0" type="ac" />
          </template>
        </el-table-column>

        <el-table-column label="DC (DC Charging)" min-width="140" align="center">
          <template #default="{ row }">
            <TestProgressBar :pass="row.DC?.pass || 0" :total="row.DC?.total || 0" type="dc" />
          </template>
        </el-table-column>

        <el-table-column label="Actions" width="100" align="center">
          <template #default="{ row }">
            <el-button type="primary" size="small" text>
              <el-icon><ArrowRight /></el-icon>
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useTestStore } from '../stores/testStore'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh, Search, DataAnalysis, Document, ArrowRight, Check, Close, Download, ArrowDown, Delete } from '@element-plus/icons-vue'
import api from '../api'

const router = useRouter()
const store = useTestStore()

const searchQuery = ref('')
const clearLoading = ref(false)
const isLoading = computed(() => store.isLoading)
const summary = computed(() => store.get_summary)

const stats = computed(() => {
  const data = summary.value

  // Show 0 when data is loading or empty
  if (!data || data.length === 0) {
    return [
      { label: 'Total Tests', value: 0, change: 0, icon: 'Document', iconBg: 'bg-primary-500/10', iconColor: 'text-primary-500' },
      { label: 'Passed', value: 0, change: 0, icon: 'Check', iconBg: 'bg-accent/10', iconColor: 'text-accent' },
      { label: 'Failed', value: 0, change: 0, icon: 'Close', iconBg: 'bg-red-500/10', iconColor: 'text-red-400' },
      { label: 'Pass Rate', value: '0%', change: 0, icon: 'TrendCharts', iconBg: 'bg-purple-500/10', iconColor: 'text-purple-400' }
    ]
  }

  let totalPassed = 0
  let totalFailed = 0
  let totalExecuted = 0

  data.forEach(item => {
    const cmnPass = item.CMN?.pass || 0
    const acPass = item.AC?.pass || 0
    const dcPass = item.DC?.pass || 0

    const cmnFail = item.CMN?.fail ?? (item.CMN ? (item.CMN.total || 0) - (item.CMN.pass || 0) : 0)
    const acFail = item.AC?.fail ?? (item.AC ? (item.AC.total || 0) - (item.AC.pass || 0) : 0)
    const dcFail = item.DC?.fail ?? (item.DC ? (item.DC.total || 0) - (item.DC.pass || 0) : 0)

    const cmnExecuted = item.CMN?.executed ?? (item.CMN?.total || 0)
    const acExecuted = item.AC?.executed ?? (item.AC?.total || 0)
    const dcExecuted = item.DC?.executed ?? (item.DC?.total || 0)

    totalPassed += cmnPass + acPass + dcPass
    totalFailed += cmnFail + acFail + dcFail
    totalExecuted += cmnExecuted + acExecuted + dcExecuted
  })

  if (totalExecuted === 0) {
    return [
      { label: 'Total Tests', value: 0, change: 0, icon: 'Document', iconBg: 'bg-primary-500/10', iconColor: 'text-primary-500' },
      { label: 'Passed', value: 0, change: 0, icon: 'Check', iconBg: 'bg-accent/10', iconColor: 'text-accent' },
      { label: 'Failed', value: 0, change: 0, icon: 'Close', iconBg: 'bg-red-500/10', iconColor: 'text-red-400' },
      { label: 'Pass Rate', value: '0%', change: 0, icon: 'TrendCharts', iconBg: 'bg-purple-500/10', iconColor: 'text-purple-400' }
    ]
  }

  const passRate = Math.round((totalPassed / totalExecuted) * 100)

  return [
    { label: 'Total Tests', value: totalExecuted, change: 0, icon: 'Document', iconBg: 'bg-primary-500/10', iconColor: 'text-primary-500' },
    { label: 'Passed', value: totalPassed, change: 0, icon: 'Check', iconBg: 'bg-accent/10', iconColor: 'text-accent' },
    { label: 'Failed', value: totalFailed, change: 0, icon: 'Close', iconBg: 'bg-red-500/10', iconColor: 'text-red-400' },
    { label: 'Pass Rate', value: `${passRate}%`, change: 0, icon: 'TrendCharts', iconBg: 'bg-purple-500/10', iconColor: 'text-purple-400' }
  ]
})

const passRateData = computed(() => {
  const data = summary.value
  if (!data || data.length === 0) return []

  const types = ['CMN', 'AC', 'DC']
  return types.map(type => {
    let passed = 0
    let executed = 0
    data.forEach(item => {
      passed += item[type]?.pass || 0
      executed += item[type]?.executed ?? item[type]?.total ?? 0
    })
    const rate = executed > 0 ? Math.round((passed / executed) * 100) : 0
    const colors = {
      CMN: 'bg-blue-500',
      AC: 'bg-green-500',
      DC: 'bg-purple-500'
    }
    return { name: type, rate, color: colors[type] }
  })
})

const testDistribution = computed(() => {
  const data = summary.value
  if (!data || data.length === 0) return { CMN: 0, AC: 0, DC: 0 }

  const dist = { CMN: 0, AC: 0, DC: 0 }
  data.forEach(item => {
    dist.CMN += item.CMN?.total || 0
    dist.AC += item.AC?.total || 0
    dist.DC += item.DC?.total || 0
  })
  return dist
})

const filteredSummary = computed(() => {
  if (!summary.value || summary.value.length === 0) return []
  if (!searchQuery.value) return summary.value
  const query = searchQuery.value.toLowerCase()
  return summary.value.filter(item =>
    item.name.toLowerCase().includes(query)
  )
})

const formatMessageName = (name) => {
  return name.replace('TestCases_SECC_', '')
}

const selectMsg = (row) => {
  const msgName = formatMessageName(row.name)
  router.push({ name: 'testcase-list', query: { msg_type: msgName } })
}

const retrieveSummary = () => {
  store.act_get_summary()
}

const handleExport = (type) => {
  const data = filteredSummary.value

  if (type === 'json') {
    const json = JSON.stringify(data, null, 2)
    downloadFile(json, 'test-summary.json', 'application/json')
    ElMessage.success('Exported as JSON')
  } else if (type === 'csv') {
    const headers = ['Message', 'CMN Pass', 'CMN Total', 'AC Pass', 'AC Total', 'DC Pass', 'DC Total']
    const rows = data.map(item => [
      item.name,
      item.CMN?.pass || 0,
      item.CMN?.total || 0,
      item.AC?.pass || 0,
      item.AC?.total || 0,
      item.DC?.pass || 0,
      item.DC?.total || 0
    ])
    const csv = [headers.join(','), ...rows.map(r => r.join(','))].join('\n')
    downloadFile(csv, 'test-summary.csv', 'text/csv')
    ElMessage.success('Exported as CSV')
  }
}

const handleClearData = async () => {
  try {
    await ElMessageBox.confirm(
      'This will reset all testcase statuses to none. Continue?',
      'Warning',
      {
        confirmButtonText: 'OK',
        cancelButtonText: 'Cancel',
        type: 'warning',
      }
    )
    clearLoading.value = true
    await api.clearData()
    ElMessage.success('All test data has been cleared')
    store.act_get_summary()
  } catch (error) {
    if (error !== 'cancel') {
      ElMessage.error('Failed to clear data')
    }
  } finally {
    clearLoading.value = false
  }
}

const downloadFile = (content, filename, type) => {
  const blob = new Blob([content], { type })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

onMounted(() => {
  retrieveSummary()
})
</script>

<style scoped>
.stat-card {
  @apply bg-dark-card rounded-2xl border border-dark-border p-5 transition-all duration-300;
}

.stat-card:hover {
  @apply border-dark-border shadow-elevated transform -translate-y-1;
}

.stat-icon {
  @apply w-12 h-12 rounded-xl flex items-center justify-center;
}

.message-table :deep(.el-table__row) {
  cursor: pointer;
  transition: background-color 0.2s;
}

.message-table :deep(.el-table__row:hover > td) {
  background-color: #1a1a24 !important;
}

.message-table :deep(.el-table__row > td) {
  border-bottom-color: #2a2a3a;
}
</style>
