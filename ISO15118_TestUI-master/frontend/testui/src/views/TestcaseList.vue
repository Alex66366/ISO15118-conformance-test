<template>
  <div class="space-y-6">
    <!-- Page Header -->
    <div class="flex items-center justify-between">
      <div>
        <h1 class="text-2xl font-semibold text-white">Test Cases</h1>
        <p class="text-gray-500 mt-1">
          <span v-if="route.query.msg_type">Browsing: {{ route.query.msg_type }}</span>
          <span v-else>Browse and manage all ISO15118-4 conformance test cases</span>
        </p>
      </div>
      <div class="flex items-center gap-3">
        <el-button type="primary" :icon="Refresh" @click="refreshList" :loading="isLoading">
          Refresh
        </el-button>
      </div>
    </div>

    <!-- Filters & Search -->
    <div class="bg-dark-card rounded-2xl border border-dark-border p-4">
      <div class="flex flex-wrap items-center gap-4">
        <el-input
          v-model="search"
          placeholder="Search test cases (supports fuzzy match)..."
          class="w-80"
          :prefix-icon="Search"
          clearable
          @input="handleSearch"
        />

        <div class="flex items-center gap-2">
          <span class="text-sm text-gray-400">Status:</span>
          <el-select v-model="statusFilter" placeholder="All" clearable class="w-36">
            <el-option label="All" value="" />
            <el-option label="Pass" value="p" />
            <el-option label="Fail" value="f" />
            <el-option label="Inconclusive" value="i" />
            <el-option label="Error" value="e" />
            <el-option label="None" value="n" />
          </el-select>
        </div>

        <div class="flex items-center gap-2">
          <span class="text-sm text-gray-400">Type:</span>
          <el-select v-model="typeFilter" placeholder="All" clearable class="w-32">
            <el-option label="All" value="" />
            <el-option label="CMN" value="CMN" />
            <el-option label="AC" value="AC" />
            <el-option label="DC" value="DC" />
          </el-select>
        </div>

        <div class="flex-1"></div>

        <!-- Batch Actions -->
        <div v-if="selectedRows.length > 0" class="flex items-center gap-2">
          <span class="text-sm text-gray-400">{{ selectedRows.length }} selected</span>
          <el-button size="small" type="danger" :icon="Delete" @click="batchDelete">
            Delete Selected
          </el-button>
          <el-button size="small" :icon="Close" @click="clearSelection">
            Clear
          </el-button>
        </div>

        <div class="text-sm text-gray-500">
          Showing {{ filteredTestcases.length }} of {{ testcaseList.length }} test cases
        </div>
      </div>
    </div>

    <!-- Test Cases Table -->
    <div class="bg-dark-card rounded-2xl border border-dark-border overflow-hidden">
      <!-- Skeleton Loading -->
      <div v-if="isLoading" class="p-6">
        <div class="space-y-4">
          <div v-for="i in 10" :key="i" class="flex items-center gap-4">
            <div class="w-4 h-4 rounded bg-dark-bg animate-pulse"></div>
            <div class="w-8 h-8 rounded-lg bg-dark-bg animate-pulse"></div>
            <div class="flex-1 h-4 bg-dark-bg rounded animate-pulse"></div>
            <div class="w-16 h-4 bg-dark-bg rounded animate-pulse"></div>
            <div class="w-16 h-4 bg-dark-bg rounded animate-pulse"></div>
            <div class="w-24 h-4 bg-dark-bg rounded animate-pulse"></div>
          </div>
        </div>
      </div>

      <!-- Empty State -->
      <div v-else-if="filteredTestcases.length === 0" class="flex flex-col items-center justify-center py-20">
        <el-icon class="text-6xl text-gray-600 mb-4"><DocumentDelete /></el-icon>
        <h3 class="text-xl font-semibold text-white mb-2">No Test Cases Found</h3>
        <p class="text-gray-500 mb-6 max-w-md text-center">
          <span v-if="search || statusFilter || typeFilter">
            No results match your current filters. Try adjusting your search criteria.
          </span>
          <span v-else>
            No test cases available. Import test cases from your backend or create new ones.
          </span>
        </p>
        <div class="flex gap-3">
          <el-button v-if="search || statusFilter || typeFilter" @click="clearFilters">
            Clear Filters
          </el-button>
          <el-button type="primary" :icon="Plus" @click="$router.push('/new')">
            Create Test Case
          </el-button>
        </div>
      </div>

      <el-table
        v-else
        ref="tableRef"
        :data="paginatedTestcases"
        style="width: 100%"
        :header-cell-style="{ background: '#12121a', color: '#9ca3af', fontWeight: '500', fontSize: '12px', textTransform: 'uppercase', letterSpacing: '0.05em' }"
        :row-style="{ background: 'transparent' }"
        @row-click="handleRowClick"
        @selection-change="handleSelectionChange"
        class="testcase-table"
      >
        <el-table-column type="selection" width="50" />

        <el-table-column prop="id" label="ID" width="80" align="center">
          <template #default="{ row }">
            <span class="font-mono text-xs text-gray-400">{{ row.id }}</span>
          </template>
        </el-table-column>

        <el-table-column prop="name" label="Testcase" min-width="280" sortable>
          <template #default="{ row }">
            <div class="flex items-center gap-3">
              <div
                class="w-8 h-8 rounded-lg flex items-center justify-center"
                :class="getTypeBg(row.type)"
              >
                <el-icon class="text-gray-300" :class="getTypeIconColor(row.type)">
                  <Document />
                </el-icon>
              </div>
              <div>
                <div class="flex items-center gap-2">
                  <span class="font-mono text-sm text-white">{{ row.name }}</span>
                  <el-button
                    :icon="DocumentCopy"
                    size="small"
                    text
                    @click.stop="copyToClipboard(row.name)"
                  />
                </div>
                <p class="text-xs text-gray-500 mt-0.5">{{ row.condition?.substring(0, 60) }}...</p>
              </div>
            </div>
          </template>
        </el-table-column>

        <el-table-column prop="type" label="Type" width="100" align="center">
          <template #default="{ row }">
            <el-tag :type="getTypeTag(row.type)" size="small" effect="dark" round>
              {{ row.type }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column prop="pics" label="PICS" width="100" align="center">
          <template #default="{ row }">
            <span class="font-mono text-xs text-gray-400">{{ row.pics || 'N/A' }}</span>
          </template>
        </el-table-column>

        <el-table-column prop="pixit" label="PIXIT" width="100" align="center">
          <template #default="{ row }">
            <span class="font-mono text-xs text-gray-400">{{ row.pixit || 'N/A' }}</span>
          </template>
        </el-table-column>

        <el-table-column prop="status" label="Status" width="120" align="center" sortable>
          <template #default="{ row }">
            <StatusBadge :status="row.status" />
          </template>
        </el-table-column>

        <el-table-column prop="build_status" label="Build" width="100" align="center" sortable>
          <template #default="{ row }">
            <BuildBadge :status="row.build_status" />
          </template>
        </el-table-column>

        <el-table-column label="Actions" width="150" align="center" fixed="right">
          <template #default="{ row }">
            <div class="flex items-center justify-center gap-1">
              <el-tooltip content="Execute" placement="top">
                <el-button
                  type="primary"
                  size="small"
                  circle
                  :icon="VideoPlay"
                  :class="{ 'animate-pulse': isRunning(row.id) }"
                  @click.stop="executeTestcase(row)"
                />
              </el-tooltip>
              <el-tooltip content="Edit" placement="top">
                <el-button
                  size="small"
                  circle
                  :icon="Edit"
                  @click.stop="editTestcase(row.id)"
                />
              </el-tooltip>
              <el-tooltip content="Delete" placement="top">
                <el-button
                  type="danger"
                  size="small"
                  circle
                  :icon="Delete"
                  @click.stop="deleteTestcase(row.id)"
                />
              </el-tooltip>
            </div>
          </template>
        </el-table-column>
      </el-table>

      <!-- Pagination -->
      <div class="px-6 py-4 border-t border-dark-border flex items-center justify-between">
        <el-pagination
          v-model:current-page="currentPage"
          v-model:page-size="pageSize"
          :page-sizes="[10, 20, 50, 100]"
          :total="filteredTestcases.length"
          layout="total, sizes, prev, pager, next, jumper"
          background
        />
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useTestStore } from '../stores/testStore'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Search, Refresh, VideoPlay, Edit, Delete, Document, Plus,
  DocumentCopy, DocumentDelete, Close
} from '@element-plus/icons-vue'
import Fuse from 'fuse.js'
import StatusBadge from '../components/StatusBadge.vue'
import BuildBadge from '../components/BuildBadge.vue'

const router = useRouter()
const route = useRoute()
const store = useTestStore()

const search = ref('')
const statusFilter = ref('')
const typeFilter = ref('')
const currentPage = ref(1)
const pageSize = ref(20)
const selectedRows = ref([])
const tableRef = ref(null)

// Fuse.js instance for fuzzy search
const fuse = computed(() => new Fuse(testcaseList.value, {
  keys: ['name', 'condition', 'expected', 'id'],
  threshold: 0.4,
  includeScore: true,
  ignoreLocation: true,
}))

const isLoading = computed(() => store.isLoading)
const testcaseList = computed(() => store.get_testcase_list)
const currentExecuteTc = computed(() => store.get_current_execute_tc)

const filteredTestcases = computed(() => {
  let result = testcaseList.value

  if (search.value) {
    const query = search.value.trim()
    const fuseResults = fuse.value.search(query)
    const fuzzyMatches = new Set(fuseResults.map(r => r.item.id))
    result = result.filter(tc =>
      fuzzyMatches.has(tc.id)
    )
  }

  if (statusFilter.value) {
    result = result.filter(tc => tc.status === statusFilter.value)
  }

  if (typeFilter.value) {
    result = result.filter(tc => tc.type === typeFilter.value)
  }

  return result
})

const paginatedTestcases = computed(() => {
  const start = (currentPage.value - 1) * pageSize.value
  const end = start + pageSize.value
  return filteredTestcases.value.slice(start, end)
})

const getTypeBg = (type) => {
  const map = {
    'CMN': 'bg-blue-500/20',
    'AC': 'bg-green-500/20',
    'DC': 'bg-purple-500/20'
  }
  return map[type] || 'bg-gray-500/20'
}

const getTypeIconColor = (type) => {
  const map = {
    'CMN': 'text-blue-400',
    'AC': 'text-green-400',
    'DC': 'text-purple-400'
  }
  return map[type] || 'text-gray-400'
}

const getTypeTag = (type) => {
  const map = {
    'CMN': '',
    'AC': 'success',
    'DC': 'warning'
  }
  return map[type] || 'info'
}

const isRunning = (id) => {
  return currentExecuteTc.value.id === id && currentExecuteTc.value.isrunning
}

const handleSearch = () => {
  currentPage.value = 1
}

const handleRowClick = (row) => {
  editTestcase(row.id)
}

const handleSelectionChange = (selection) => {
  selectedRows.value = selection
}

const clearSelection = () => {
  tableRef.value?.clearSelection()
}

const copyToClipboard = async (text) => {
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('Copied to clipboard')
  } catch (err) {
    ElMessage.error('Failed to copy')
  }
}

const editTestcase = (id) => {
  router.push({ name: 'testcase-detail', params: { id } })
}

const executeTestcase = (testcase) => {
  // Set the testcase ID and name, then navigate to Execute page
  // The actual execution will be triggered manually on the Execute page
  store.muta_update_execute_testcase({
    id: testcase.id,
    name: testcase.name,
    isrunning: false
  })
  router.push({ name: 'testcase-execute' })
}

const deleteTestcase = (id) => {
  ElMessageBox.confirm(
    'Are you sure you want to delete this test case?',
    'Warning',
    {
      confirmButtonText: 'Delete',
      cancelButtonText: 'Cancel',
      type: 'warning',
    }
  ).then(() => {
    store.act_remove_testcase_id(id)
      .then(() => {
        ElMessage.success('Test case deleted successfully')
        refreshList()
      })
      .catch(() => {
        ElMessage.error('Failed to delete test case')
      })
  }).catch(() => {})
}

const batchDelete = () => {
  ElMessageBox.confirm(
    `Are you sure you want to delete ${selectedRows.value.length} test cases?`,
    'Warning',
    {
      confirmButtonText: 'Delete All',
      cancelButtonText: 'Cancel',
      type: 'warning',
    }
  ).then(async () => {
    const deletePromises = selectedRows.value.map(row =>
      store.act_remove_testcase_id(row.id)
    )
    try {
      await Promise.all(deletePromises)
      ElMessage.success(`Successfully deleted ${selectedRows.value.length} test cases`)
      clearSelection()
      refreshList()
    } catch (err) {
      ElMessage.error('Some deletions failed')
    }
  }).catch(() => {})
}

const clearFilters = () => {
  search.value = ''
  statusFilter.value = ''
  typeFilter.value = ''
  currentPage.value = 1
}

const refreshList = () => {
  const queryParams = {}
  if (route.query.msg_type) {
    queryParams.msg_type = route.query.msg_type
  }
  store.act_get_testcases(queryParams)
}

watch(() => route.query.msg_type, () => {
  refreshList()
}, { immediate: true })

onMounted(() => {
  refreshList()
})
</script>

<style scoped>
.testcase-table :deep(.el-table__row) {
  cursor: pointer;
  transition: background-color 0.2s;
}

.testcase-table :deep(.el-table__row:hover > td) {
  background-color: #1a1a24 !important;
}

.testcase-table :deep(.el-table__row > td) {
  border-bottom-color: #2a2a3a;
}

.testcase-table :deep(.el-table__header-wrapper .el-checkbox) {
  display: none;
}
</style>
