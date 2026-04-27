<template>
  <div class="space-y-6" v-if="currentTestcase">
    <!-- Page Header -->
    <div class="flex items-center justify-between">
      <div class="flex items-center gap-4">
        <el-button :icon="ArrowLeft" circle @click="goBack" class="!bg-dark-card !border-dark-border" />
        <div>
          <h1 class="text-2xl font-semibold text-white">Test Case Detail</h1>
          <p class="text-gray-500 mt-1 font-mono">{{ currentTestcase.name }}</p>
        </div>
      </div>
      <div class="flex items-center gap-3">
        <StatusBadge :status="currentTestcase.status" />
        <BuildBadge :status="currentTestcase.build_status" />
      </div>
    </div>

    <!-- Main Content -->
    <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
      <!-- Left Column - Test Case Info -->
      <div class="lg:col-span-2 space-y-6">
        <div class="bg-dark-card rounded-2xl border border-dark-border p-6">
          <h2 class="text-lg font-semibold text-white mb-4 flex items-center gap-2">
            <el-icon><Document /></el-icon>
            Test Case Information
          </h2>

          <el-form label-position="top" class="space-y-4">
            <el-form-item label="Testcase Name" required>
              <el-input v-model="currentTestcase.name" readonly class="!bg-dark-elevated" />
            </el-form-item>

            <el-form-item label="Condition" required>
              <el-input
                v-model="currentTestcase.condition"
                type="textarea"
                :rows="3"
                placeholder="Test condition..."
              />
            </el-form-item>

            <el-form-item label="Expected Result" required>
              <el-input
                v-model="currentTestcase.expected"
                type="textarea"
                :rows="3"
                placeholder="Expected result..."
              />
            </el-form-item>

            <!-- PICS Field with JSON Formatting -->
            <el-form-item label="PICS">
              <div class="relative">
                <el-input
                  v-model="currentTestcase.pics"
                  type="textarea"
                  :rows="3"
                  placeholder="Protocol Implementation Conformance Statement..."
                  @focus="checkJsonFormat('pics')"
                />
                <el-button
                  v-if="isJson('pics')"
                  type="primary"
                  size="small"
                  class="absolute top-2 right-2"
                  @click="toggleJsonView('pics')"
                >
                  {{ jsonViews.pics ? 'Edit' : 'Format' }}
                </el-button>
              </div>
              <div v-if="isJson('pics') && jsonViews.pics" class="mt-2">
                <pre class="bg-dark-bg p-3 rounded-lg text-sm text-gray-300 overflow-auto max-h-40">{{ formatJson(currentTestcase.pics) }}</pre>
              </div>
            </el-form-item>

            <!-- PIXIT Field with JSON Formatting -->
            <el-form-item label="PIXIT">
              <div class="relative">
                <el-input
                  v-model="currentTestcase.pixit"
                  type="textarea"
                  :rows="3"
                  placeholder="Protocol Implementation extra Information for Testing..."
                  @focus="checkJsonFormat('pixit')"
                />
                <el-button
                  v-if="isJson('pixit')"
                  type="primary"
                  size="small"
                  class="absolute top-2 right-2"
                  @click="toggleJsonView('pixit')"
                >
                  {{ jsonViews.pixit ? 'Edit' : 'Format' }}
                </el-button>
              </div>
              <div v-if="isJson('pixit') && jsonViews.pixit" class="mt-2">
                <pre class="bg-dark-bg p-3 rounded-lg text-sm text-gray-300 overflow-auto max-h-40">{{ formatJson(currentTestcase.pixit) }}</pre>
              </div>
            </el-form-item>
          </el-form>
        </div>

        <!-- Quick Copy Section -->
        <div class="bg-dark-card rounded-2xl border border-dark-border p-6">
          <h2 class="text-lg font-semibold text-white mb-4 flex items-center gap-2">
            <el-icon><CopyDocument /></el-icon>
            Quick Copy
          </h2>
          <div class="grid grid-cols-2 gap-4">
            <div class="p-4 bg-dark-bg rounded-xl">
              <div class="flex justify-between items-center mb-2">
                <span class="text-sm text-gray-400">Testcase Name</span>
                <el-button :icon="DocumentCopy" size="small" text @click="copyField('name')" />
              </div>
              <p class="font-mono text-sm text-white truncate">{{ currentTestcase.name }}</p>
            </div>
            <div class="p-4 bg-dark-bg rounded-xl">
              <div class="flex justify-between items-center mb-2">
                <span class="text-sm text-gray-400">Testcase ID</span>
                <el-button :icon="DocumentCopy" size="small" text @click="copyField('id')" />
              </div>
              <p class="font-mono text-sm text-white">{{ currentTestcase.id }}</p>
            </div>
          </div>
        </div>
      </div>

      <!-- Right Column - Actions & Meta -->
      <div class="space-y-6">
        <!-- Type Selection -->
        <div class="bg-dark-card rounded-2xl border border-dark-border p-6">
          <h2 class="text-lg font-semibold text-white mb-4 flex items-center gap-2">
            <el-icon><Setting /></el-icon>
            Configuration
          </h2>

          <el-form label-position="top">
            <el-form-item label="Type">
              <el-select v-model="currentTestcase.type" class="w-full">
                <el-option label="CMN (Common)" value="CMN" />
                <el-option label="AC (AC Charging)" value="AC" />
                <el-option label="DC (DC Charging)" value="DC" />
              </el-select>
            </el-form-item>

            <el-form-item label="Status">
              <el-select v-model="currentTestcase.status" class="w-full">
                <el-option label="None" value="n" />
                <el-option label="Pass" value="p" />
                <el-option label="Inconclusive" value="i" />
                <el-option label="Fail" value="f" />
                <el-option label="Error" value="e" />
              </el-select>
            </el-form-item>

            <el-form-item label="Build Status">
              <el-select v-model="currentTestcase.build_status" class="w-full">
                <el-option label="N/A" value="NA" />
                <el-option label="OK" value="OK" />
              </el-select>
            </el-form-item>
          </el-form>
        </div>

        <!-- Action Buttons -->
        <div class="bg-dark-card rounded-2xl border border-dark-border p-6">
          <h2 class="text-lg font-semibold text-white mb-4 flex items-center gap-2">
            <el-icon><Operation /></el-icon>
            Actions
          </h2>

          <div class="space-y-3">
            <el-button type="primary" class="w-full" :icon="VideoPlay" @click="executeTestcase">
              Execute Test
            </el-button>
            <el-button class="w-full" :icon="View" @click="showTestcaseLogs">
              View Logs
            </el-button>
            <el-button type="success" class="w-full" :icon="Check" @click="updateTestcase">
              Save Changes
            </el-button>
            <el-button type="danger" class="w-full" :icon="Delete" @click="deleteTestcase">
              Delete
            </el-button>
          </div>
        </div>

        <!-- Test Info -->
        <div class="bg-dark-card rounded-2xl border border-dark-border p-6">
          <h2 class="text-lg font-semibold text-white mb-4 flex items-center gap-2">
            <el-icon><InfoFilled /></el-icon>
            Metadata
          </h2>

          <div class="space-y-3 text-sm">
            <div class="flex justify-between">
              <span class="text-gray-500">ID:</span>
              <span class="text-gray-300 font-mono">{{ currentTestcase.id }}</span>
            </div>
            <div class="flex justify-between">
              <span class="text-gray-500">Created:</span>
              <span class="text-gray-300">{{ currentTestcase.created_at || 'N/A' }}</span>
            </div>
            <div class="flex justify-between">
              <span class="text-gray-500">Updated:</span>
              <span class="text-gray-300">{{ currentTestcase.updated_at || 'N/A' }}</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- Not Found State -->
  <div v-else class="flex flex-col items-center justify-center py-20">
    <el-icon class="text-6xl text-gray-500 mb-4"><DocumentDelete /></el-icon>
    <h2 class="text-xl font-semibold text-white mb-2">Test Case Not Found</h2>
    <p class="text-gray-500 mb-6">The requested test case could not be found.</p>
    <el-button type="primary" :icon="ArrowLeft" @click="goBack">
      Back to List
    </el-button>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useTestStore } from '../stores/testStore'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  ArrowLeft, Document, Setting, Operation, VideoPlay, View, Check, Delete,
  InfoFilled, DocumentDelete, DocumentCopy, CopyDocument
} from '@element-plus/icons-vue'
import StatusBadge from '../components/StatusBadge.vue'
import BuildBadge from '../components/BuildBadge.vue'

const router = useRouter()
const route = useRoute()
const store = useTestStore()

const currentTestcase = ref(null)
const jsonViews = ref({
  pics: false,
  pixit: false
})
const jsonFields = ref({
  pics: false,
  pixit: false
})

const loadTestcase = async (id) => {
  try {
    const response = await store.act_get_testcase_id(id)
    const data = response.data
    currentTestcase.value = {
      ...data,
      condition: data.condition?.replace(/\n/g, ' '),
      expected: data.expected?.replace(/\n/g, ' '),
      pics: data.pics?.replace(/\n/g, ' '),
      pixit: data.pixit?.replace(/\n/g, ' ')
    }
    // Check if PICS/PIXIT are JSON
    checkJsonFormat('pics')
    checkJsonFormat('pixit')
  } catch (error) {
    currentTestcase.value = null
    console.error('Failed to load testcase:', error)
  }
}

const checkJsonFormat = (field) => {
  if (!currentTestcase.value?.[field]) {
    jsonFields.value[field] = false
    return
  }
  try {
    JSON.parse(currentTestcase.value[field])
    jsonFields.value[field] = true
  } catch (e) {
    jsonFields.value[field] = false
  }
}

const isJson = (field) => {
  return jsonFields.value[field]
}

const formatJson = (str) => {
  try {
    return JSON.stringify(JSON.parse(str), null, 2)
  } catch (e) {
    return str
  }
}

const toggleJsonView = (field) => {
  jsonViews.value[field] = !jsonViews.value[field]
}

const copyField = async (field) => {
  if (!currentTestcase.value) return
  try {
    await navigator.clipboard.writeText(currentTestcase.value[field])
    ElMessage.success(`${field} copied to clipboard`)
  } catch (err) {
    ElMessage.error('Failed to copy')
  }
}

const goBack = () => {
  router.push({ name: 'testcase-list' })
}

const executeTestcase = () => {
  if (currentTestcase.value) {
    store.muta_update_execute_testcase({
      id: currentTestcase.value.id,
      name: currentTestcase.value.name,
      isrunning: true
    })
    router.push({ name: 'testcase-execute' })
  }
}

const showTestcaseLogs = () => {
  ElMessage.info('Log viewer coming soon')
}

const updateTestcase = () => {
  store.act_update_testcase({
    id: currentTestcase.value.id,
    data: currentTestcase.value
  })
    .then(() => {
      ElMessage.success('Test case updated successfully')
    })
    .catch(() => {
      ElMessage.error('Failed to update test case')
    })
}

const deleteTestcase = () => {
  ElMessageBox.confirm(
    'Are you sure you want to delete this test case? This action cannot be undone.',
    'Warning',
    {
      confirmButtonText: 'Delete',
      cancelButtonText: 'Cancel',
      type: 'warning',
    }
  ).then(() => {
    store.act_remove_testcase_id(currentTestcase.value.id)
      .then(() => {
        ElMessage.success('Test case deleted successfully')
        router.push({ name: 'testcase-list' })
      })
      .catch(() => {
        ElMessage.error('Failed to delete test case')
      })
  }).catch(() => {})
}

watch(() => route.params.id, (newId) => {
  if (newId) loadTestcase(newId)
}, { immediate: true })

onMounted(() => {
  if (route.params.id) {
    loadTestcase(route.params.id)
  }
})
</script>
