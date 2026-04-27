import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  {
    path: '/',
    name: 'Home',
    component: () => import('../views/Home.vue'),
    meta: { title: 'Dashboard', icon: 'DataAnalysis' }
  },
  {
    path: '/testcases',
    name: 'testcase-list',
    component: () => import('../views/TestcaseList.vue'),
    meta: { title: 'Test Cases', icon: 'Document' }
  },
  {
    path: '/testcase/:id',
    name: 'testcase-detail',
    component: () => import('../views/Testcase.vue'),
    meta: { title: 'Test Case Detail', icon: 'Document' }
  },
  {
    path: '/new',
    name: 'testcase-new',
    component: () => import('../views/TestcaseNew.vue'),
    meta: { title: 'New Test Case', icon: 'Plus' }
  },
  {
    path: '/execute',
    name: 'testcase-execute',
    component: () => import('../views/TestcaseExecute.vue'),
    meta: { title: 'Execute Test', icon: 'VideoPlay' }
  },
  {
    path: '/llm-generate',
    name: 'llm-generate',
    component: () => import('../views/LLMGenerate.vue'),
    meta: { title: 'AI Script Generator', icon: 'MagicStick' }
  }
]

const router = createRouter({
  history: createWebHistory(),
  routes
})

router.beforeEach((to, from, next) => {
  document.title = `${to.meta.title || 'Test Platform'} | ISO15118`
  next()
})

export default router
