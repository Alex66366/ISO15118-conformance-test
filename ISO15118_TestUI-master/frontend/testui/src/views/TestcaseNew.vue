<template>
  <div class="new-page">
    <!-- Page Header -->
    <header class="page-header">
      <el-button :icon="ArrowLeft" circle @click="goBack" class="back-btn !bg-dark-card !border-dark-border hover:!border-gray-500" />
      <div class="header-text">
        <h1 class="page-title">Create New Test Case</h1>
        <p class="page-subtitle">Add a new ISO15118-4 conformance test case</p>
      </div>
    </header>

    <!-- Main Content: two-column layout with sticky sidebar -->
    <div class="main-grid">
      <!-- Left: Test Case Details (wider, scrollable) -->
      <div class="form-column">
        <section class="card card-details">
          <h2 class="section-title">
            <el-icon><Document /></el-icon>
            <span>Test Case Details</span>
          </h2>

          <el-form
            ref="formRef"
            :model="formData"
            :rules="formRules"
            label-position="top"
            class="details-form"
          >
            <el-form-item label="Testcase Name" prop="name" class="form-item">
              <el-input
                v-model="formData.name"
                placeholder="e.g., TC_SECC_CMN_VTB_TestName_001"
              >
                <template #prefix>
                  <el-icon><Document /></el-icon>
                </template>
              </el-input>
              <p class="helper-text">Format: TC_SECC_(CMN|AC|DC)_VTB_[a-zA-Z]+_[001-999]</p>
            </el-form-item>

            <el-form-item label="Condition" prop="condition" class="form-item">
              <el-input
                v-model="formData.condition"
                type="textarea"
                :rows="3"
                placeholder="Describe the test condition..."
              />
            </el-form-item>

            <el-form-item label="Expected Result" prop="expected" class="form-item">
              <el-input
                v-model="formData.expected"
                type="textarea"
                :rows="3"
                placeholder="Describe the expected result..."
              />
            </el-form-item>

            <el-form-item label="PICS (Protocol Implementation Conformance Statement)" class="form-item">
              <el-input
                v-model="formData.pics"
                type="textarea"
                :rows="2"
                placeholder="PICS requirements..."
              />
            </el-form-item>

            <el-form-item label="PIXIT (Protocol Implementation extra Information for Testing)" class="form-item">
              <el-input
                v-model="formData.pixit"
                type="textarea"
                :rows="2"
                placeholder="PIXIT requirements..."
              />
            </el-form-item>
          </el-form>
        </section>
      </div>

      <!-- Right: Configuration + Quick Actions + Submit (sticky, fixed width) -->
      <aside class="sidebar">
        <div class="sidebar-inner">
          <section class="card card-config">
            <h2 class="section-title">
              <el-icon><Setting /></el-icon>
              <span>Configuration</span>
            </h2>

            <el-form label-position="top" class="config-form">
              <el-form-item label="Type">
                <el-select v-model="formData.type" class="w-full">
                  <el-option label="CMN (Common)" value="CMN">
                    <div class="flex items-center gap-2">
                      <span class="w-2 h-2 rounded-full bg-blue-500"></span>
                      <span>CMN (Common)</span>
                    </div>
                  </el-option>
                  <el-option label="AC (AC Charging)" value="AC">
                    <div class="flex items-center gap-2">
                      <span class="w-2 h-2 rounded-full bg-green-500"></span>
                      <span>AC (AC Charging)</span>
                    </div>
                  </el-option>
                  <el-option label="DC (DC Charging)" value="DC">
                    <div class="flex items-center gap-2">
                      <span class="w-2 h-2 rounded-full bg-purple-500"></span>
                      <span>DC (DC Charging)</span>
                    </div>
                  </el-option>
                </el-select>
              </el-form-item>

              <el-form-item label="Status">
                <el-select v-model="formData.status" class="w-full">
                  <el-option label="None" value="n" />
                  <el-option label="Pass" value="p" />
                  <el-option label="Inconclusive" value="i" />
                  <el-option label="Fail" value="f" />
                  <el-option label="Error" value="e" />
                </el-select>
              </el-form-item>

              <el-form-item label="Build Status">
                <el-select v-model="formData.build_status" class="w-full">
                  <el-option label="N/A" value="NA" />
                  <el-option label="OK" value="OK" />
                </el-select>
              </el-form-item>
            </el-form>
          </section>

          <section class="card card-actions">
            <h2 class="section-title">
              <el-icon><Promotion /></el-icon>
              <span>Quick Actions</span>
            </h2>

            <div class="action-buttons">
              <el-button class="action-btn w-full" :icon="DocumentCopy" @click="loadFromTemplate">
                Load from Template
              </el-button>
              <el-button class="action-btn w-full" :icon="DocumentAdd" @click="importJson">
                Import from JSON
              </el-button>
              <el-button class="action-btn w-full" :icon="Refresh" @click="resetForm">
                Reset Form
              </el-button>
            </div>
          </section>

          <section class="card card-submit">
            <el-button type="primary" class="submit-btn w-full" :icon="Check" size="large" @click="submitForm">
              Create Test Case
            </el-button>
            <el-button class="cancel-btn w-full" :icon="Close" size="large" @click="goBack">
              Cancel
            </el-button>
          </section>
        </div>
      </aside>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { 
  ArrowLeft, Document, Setting, Promotion, DocumentCopy, DocumentAdd, 
  Refresh, Check, Close 
} from '@element-plus/icons-vue'

const router = useRouter()
const formRef = ref(null)

const formData = reactive({
  name: '',
  condition: '',
  expected: '',
  pics: '',
  pixit: '',
  type: 'CMN',
  status: 'n',
  build_status: 'NA'
})

const formRules = {
  name: [
    { required: true, message: 'Testcase name is required', trigger: 'blur' },
    { 
      pattern: /^TC_SECC_(CMN|AC|DC)_VTB_[a-zA-Z]+_\d{3}$/,
      message: 'Invalid format: TC_SECC_(CMN|AC|DC)_VTB_[a-zA-Z]+_[001-999]',
      trigger: 'blur'
    }
  ],
  condition: [
    { required: true, message: 'Condition is required', trigger: 'blur' }
  ],
  expected: [
    { required: true, message: 'Expected result is required', trigger: 'blur' }
  ]
}

const goBack = () => {
  router.push({ name: 'Home' })
}

const loadFromTemplate = () => {
  ElMessage.info('Template loading coming soon')
}

const importJson = () => {
  ElMessage.info('JSON import coming soon')
}

const resetForm = () => {
  formRef.value?.resetFields()
  Object.assign(formData, {
    name: '',
    condition: '',
    expected: '',
    pics: '',
    pixit: '',
    type: 'CMN',
    status: 'n',
    build_status: 'NA'
  })
}

const submitForm = async () => {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return

  // In a real app, this would call an API to create the test case
  ElMessage.success('Test case created successfully')
  router.push({ name: 'Home' })
}
</script>

<style scoped>
.new-page {
  --new-gap: 1.5rem;
  --card-radius: 1rem;
  --sidebar-width: 20rem;
}

/* Page header: typography & spacing */
.page-header {
  display: flex;
  align-items: center;
  gap: 1rem;
  margin-bottom: 1.75rem;
}

.header-text {
  flex: 1;
  min-width: 0;
}

.page-title {
  font-size: 1.625rem;
  font-weight: 600;
  letter-spacing: -0.02em;
  line-height: 1.3;
  color: #fff;
  margin: 0;
}

.page-subtitle {
  font-size: 0.9375rem;
  color: rgb(107 114 128);
  margin: 0.25rem 0 0;
  line-height: 1.4;
}

/* Two-column layout: form takes remaining space, sidebar fixed width + sticky */
.main-grid {
  display: grid;
  grid-template-columns: 1fr minmax(280px, var(--sidebar-width));
  gap: var(--new-gap);
  align-items: start;
}

@media (max-width: 1023px) {
  .main-grid {
    grid-template-columns: 1fr;
  }

  .sidebar-inner {
    position: static !important;
  }
}

.form-column {
  min-width: 0;
}

.sidebar {
  min-width: 0;
}

.sidebar-inner {
  position: sticky;
  top: 6rem;
  display: flex;
  flex-direction: column;
  gap: var(--new-gap);
}

/* Cards */
.card {
  background: #1a1a24;
  border: 1px solid #2a2a3a;
  border-radius: var(--card-radius);
  padding: 1.5rem;
}

.card-details {
  padding: 1.75rem 1.5rem;
}

.section-title {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 1rem;
  font-weight: 600;
  color: #fff;
  margin: 0 0 1.25rem;
  letter-spacing: 0.01em;
}

.section-title .el-icon {
  font-size: 1.125rem;
  color: #00d4aa;
}

/* Form: consistent vertical rhythm */
.details-form :deep(.el-form-item) {
  margin-bottom: 1.25rem;
}

.details-form :deep(.el-form-item:last-child) {
  margin-bottom: 0;
}

.details-form :deep(.el-form-item__label) {
  font-weight: 500;
  color: rgb(229 231 235);
  font-size: 0.875rem;
  padding-bottom: 0.375rem;
}

.form-item {
  display: block;
}

.helper-text {
  font-size: 0.75rem;
  color: rgb(107 114 128);
  margin: 0.375rem 0 0;
  line-height: 1.4;
  letter-spacing: 0.01em;
}

.config-form :deep(.el-form-item) {
  margin-bottom: 1rem;
}

.config-form :deep(.el-form-item:last-child) {
  margin-bottom: 0;
}

.config-form :deep(.el-form-item__label) {
  font-weight: 500;
  color: rgb(229 231 235);
  font-size: 0.875rem;
}

/* Quick Actions: full-width buttons, left-aligned */
.action-buttons {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  width: 100%;
}

.action-buttons .action-btn,
.action-buttons :deep(.el-button) {
  width: 100% !important;
  min-width: 0 !important;
  display: flex !important;
  justify-content: flex-start !important;
  margin: 0 !important;
}

.action-btn {
  justify-content: flex-start !important;
  background: #0a0a0f !important;
  border-color: #2a2a3a !important;
  color: rgb(229 231 235) !important;
  width: 100% !important;
}

.action-btn:hover {
  border-color: #00d4aa !important;
  color: #00d4aa !important;
}

/* Submit block: both buttons full-width, vertically aligned */
.card-submit {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  width: 100%;
}

.card-submit .submit-btn,
.card-submit .cancel-btn,
.card-submit :deep(.el-button) {
  width: 100% !important;
  min-width: 0 !important;
  margin: 0 !important;
}

.submit-btn {
  font-weight: 600;
}

.cancel-btn {
  background: #1a1a24 !important;
  border-color: #2a2a3a !important;
  color: rgb(209 213 219) !important;
}

.cancel-btn:hover {
  border-color: rgb(107 114 128) !important;
  color: #fff !important;
}
</style>
