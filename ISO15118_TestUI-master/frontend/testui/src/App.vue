<template>
  <div class="min-h-screen bg-dark-bg">
    <!-- Top Navigation Bar -->
    <header class="fixed top-0 left-0 right-0 z-50 h-16 bg-dark-surface/80 backdrop-blur-xl border-b border-dark-border">
      <div class="flex items-center justify-between h-full px-6">
        <!-- Logo & Brand -->
        <div class="flex items-center gap-4">
          <div class="flex items-center gap-3">
            <div class="w-10 h-10 rounded-xl bg-gradient-to-br from-accent to-primary-500 flex items-center justify-center shadow-glow">
              <el-icon class="text-xl text-white"><Monitor /></el-icon>
            </div>
            <div>
              <h1 class="text-lg font-semibold text-white tracking-tight">ISO15118</h1>
              <p class="text-xs text-gray-500 -mt-0.5">Test Platform</p>
            </div>
          </div>
        </div>

        <!-- Navigation Menu -->
        <nav class="flex items-center gap-1">
          <router-link
            v-for="item in navItems"
            :key="item.path"
            :to="item.path"
            class="nav-item group"
            :class="{ 'active': isActive(item.path) }"
          >
            <el-icon class="text-lg"><component :is="item.icon" /></el-icon>
            <span class="font-medium">{{ item.label }}</span>
          </router-link>
        </nav>

        <!-- Right Actions -->
        <div class="flex items-center gap-3">
          <!-- Status Indicator -->
          <div class="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-dark-card border border-dark-border">
            <span class="w-2 h-2 rounded-full bg-accent animate-pulse-slow"></span>
            <span class="text-sm text-gray-400">System Ready</span>
          </div>

          <!-- Settings -->
          <el-button circle size="default" class="!bg-dark-card !border-dark-border !text-gray-400 hover:!text-white">
            <el-icon><Setting /></el-icon>
          </el-button>
        </div>
      </div>
    </header>

    <!-- Breadcrumb -->
    <div class="fixed top-16 left-0 right-0 z-40 h-10 bg-dark-surface/50 backdrop-blur-sm border-b border-dark-border/50 flex items-center px-6">
      <el-breadcrumb separator="/">
        <el-breadcrumb-item :to="{ path: '/' }">
          <el-icon><HomeFilled /></el-icon>
        </el-breadcrumb-item>
        <el-breadcrumb-item v-for="(item, index) in breadcrumbs" :key="index">
          <router-link v-if="item.path" :to="item.path">{{ item.label }}</router-link>
          <span v-else>{{ item.label }}</span>
        </el-breadcrumb-item>
      </el-breadcrumb>
    </div>

    <!-- Main Content Area -->
    <main class="pt-24 pb-8 px-6">
      <div class="max-w-7xl mx-auto">
        <router-view v-slot="{ Component }">
          <transition name="fade-slide" mode="out-in">
            <component :is="Component" />
          </transition>
        </router-view>
      </div>
    </main>
  </div>
</template>

<script setup>
import { computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { useTestStore } from './stores/testStore'
import { Monitor, HomeFilled, Document, Plus, VideoPlay, Setting, MagicStick } from '@element-plus/icons-vue'

const route = useRoute()
const store = useTestStore()

const navItems = [
  { path: '/', label: 'Dashboard', icon: 'HomeFilled' },
  { path: '/testcases', label: 'Test Cases', icon: 'Document' },
  { path: '/new', label: 'New', icon: 'Plus' },
  { path: '/execute', label: 'Execute', icon: 'VideoPlay' },
  { path: '/llm-generate', label: 'AI Generate', icon: 'MagicStick' }
]

const isActive = (path) => {
  if (path === '/') return route.path === '/'
  return route.path.startsWith(path)
}

const breadcrumbs = computed(() => {
  const items = []
  const path = route.path

  if (path === '/') {
    return []
  }

  if (path.startsWith('/testcases')) {
    items.push({ label: 'Test Cases', path: '/testcases' })

    if (path.includes('/detail')) {
      const id = route.params.id
      if (id) {
        items.push({ label: `Testcase #${id}`, path: '' })
      }
    }
  }

  if (path === '/new') {
    items.push({ label: 'New Test Case', path: '' })
  }

  if (path === '/execute') {
    items.push({ label: 'Execute', path: '' })
  }

  return items
})

onMounted(() => {
  store.initWebSocket()
})
</script>

<style scoped>
.nav-item {
  @apply flex items-center gap-2 px-4 py-2 rounded-lg text-gray-400 font-medium text-sm
         transition-all duration-200 hover:bg-dark-card hover:text-white;
}

.nav-item.active {
  @apply bg-dark-card text-accent;
}

.fade-slide-enter-active,
.fade-slide-leave-active {
  transition: all 0.25s ease-out;
}

.fade-slide-enter-from {
  opacity: 0;
  transform: translateY(10px);
}

.fade-slide-leave-to {
  opacity: 0;
  transform: translateY(-10px);
}

:deep(.el-breadcrumb__inner) {
  color: #6b7280 !important;
  font-size: 12px;
}

:deep(.el-breadcrumb__inner.is-link:hover) {
  color: #00d4aa !important;
}

:deep(.el-breadcrumb__item:last-child .el-breadcrumb__inner) {
  color: #e5e7eb !important;
}
</style>
