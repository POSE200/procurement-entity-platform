import { createRouter, createWebHashHistory } from 'vue-router'
import { ElMessage } from 'element-plus'
import { rememberResultScope } from '@/utils/resultScope'

// 使用 hash 路由：构建产物放在任意静态路径下都能直接刷新，不依赖服务端回退规则
export const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/task1' },
    { path: '/task1', name: 'task1', component: () => import('@/views/Task1View.vue'), meta: { title: '任务一指标', no: '一' } },
    { path: '/task2', name: 'task2', component: () => import('@/views/Task2View.vue'), meta: { title: '任务二总览', no: '二' } },
    { path: '/upload', name: 'upload', component: () => import('@/views/UploadView.vue'), meta: { title: '数据集导入', no: '壹' } },
    { path: '/jobs/:id?', name: 'jobs', component: () => import('@/views/JobsView.vue'), meta: { title: '任务监控', no: '贰' } },
    { path: '/items', name: 'items', component: () => import('@/views/ItemsView.vue'), meta: { title: '标的物检索', no: '叁' } },
    { path: '/announcements/:id?', name: 'announcement', component: () => import('@/views/AnnouncementView.vue'), meta: { title: '公告详情', no: '肆' } },
    { path: '/relations', name: 'relations', component: () => import('@/views/RelationsView.vue'), meta: { title: '关系分析', no: '伍' } },
    { path: '/profile', name: 'profile', component: () => import('@/views/ProfileView.vue'), meta: { title: '用户画像', no: '陆' } },
    { path: '/evaluation/:job?', redirect: (to) => ({ name: 'task1', query: to.params.job ? { job: String(to.params.job) } : {} }) },
    { path: '/model', name: 'model', component: () => import('@/views/ModelView.vue'), meta: { title: '模型配置', no: '捌' } },
    { path: '/:pathMatch(.*)*', redirect: '/items' },
  ],
})

router.onError((error, to) => {
  const message = String(error?.message ?? error)
  const assetError = /Failed to fetch dynamically imported module|Importing a module script failed|error loading dynamically imported module|Loading (?:CSS )?chunk|Unable to preload CSS/i.test(message)
  if (assetError) {
    const target = router.resolve(to).href
    const key = 'ict.navigationAssetReload'
    try {
      const previous = JSON.parse(sessionStorage.getItem(key) ?? 'null')
      if (!previous || previous.target !== target || Date.now() - previous.at > 60000) {
        sessionStorage.setItem(key, JSON.stringify({ target, at: Date.now() }))
        window.location.replace(target)
        window.location.reload()
        return
      }
    } catch { /* storage unavailable */ }
  }
  ElMessage.error(`“${String(to.meta.title ?? '页面')}”加载失败，请刷新后重试。`)
})

const RESULT_PAGES = ['task1', 'task2', 'items', 'announcement', 'relations', 'profile']
router.beforeEach((to) => {
  if (RESULT_PAGES.includes(String(to.name)) && to.query.job == null) {
    try {
      const selected = localStorage.getItem('ict.selectedResultJob')
      if (selected) return { path: to.path, query: { ...to.query, job: selected }, hash: to.hash, replace: true }
    } catch { /* storage unavailable */ }
  }
})
router.afterEach((to) => {
  if (RESULT_PAGES.includes(String(to.name)) && to.query.job) rememberResultScope(String(to.query.job))
  if (to.name === 'jobs' && to.params.id) rememberResultScope(String(to.params.id))
  const t = to.meta.title as string | undefined
  document.title = (t ? `${t} · ` : '') + '采购公告实体挖掘与关系分析'
})
