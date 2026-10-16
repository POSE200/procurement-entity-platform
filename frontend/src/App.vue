<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '@/api/client'

const route = useRoute()
// 左侧导航按分组组织，以后新增入口只需往对应分组里追加一行
const groups = [
  { title: '赛题总览', items: [
    { to: '/task1', label: '任务一指标', no: '一' },
    { to: '/task2', label: '任务二总览', no: '二' },
  ] },
  { title: '数据处理', items: [
    { to: '/upload', label: '数据集导入', no: '壹' },
    { to: '/jobs', label: '任务监控', no: '贰' },
  ] },
  { title: '结果查询', items: [
    { to: '/items', label: '标的物检索', no: '叁' },
    { to: '/announcements', label: '公告详情', no: '肆' },
    { to: '/relations', label: '关系分析', no: '伍' },
    { to: '/profile', label: '用户画像', no: '陆' },
  ] },
  { title: '评测与配置', items: [
    { to: '/model', label: '模型配置', no: '捌' },
  ] },
]
const active = computed(() => '/' + (route.path.split('/')[1] ?? ''))

// 库中还没有抽取结果时，在顶部给出下一步指引；切换页面时刷新
const empty = ref(false)
const backendDown = ref(false)
async function check() {
  try {
    empty.value = (await api.stats()).announcements === 0
    backendDown.value = false
  } catch {
    empty.value = false
    backendDown.value = true
  }
}
watch(() => route.path, check, { immediate: true })
</script>

<template>
  <div class="shell">
    <header class="masthead">
      <div class="brand">
        <span class="seal" aria-hidden="true">采</span>
        <div>
          <div class="brand-name">采购公告实体挖掘与关系分析</div>
          <div class="brand-sub">ICT 赛题五 · 演示平台</div>
        </div>
      </div>
    </header>
    <div class="body">
      <aside class="side">
        <nav aria-label="主导航">
          <div v-for="g in groups" :key="g.title" class="side-group">
            <div class="side-title">{{ g.title }}</div>
            <RouterLink v-for="n in g.items" :key="n.to" :to="n.to" class="side-item" :class="{ on: active === n.to }"
              :aria-current="active === n.to ? 'page' : undefined">
              <span class="side-no">{{ n.no }}</span>{{ n.label }}
            </RouterLink>
          </div>
        </nav>
      </aside>
      <div class="content">
        <div v-if="backendDown" class="strip err" role="alert">
          <b>服务未连接</b>：请启动平台后刷新。
        </div>
        <div v-else-if="empty" class="strip" role="status">
          <b>库中还没有抽取结果</b>：在 <RouterLink to="/model">模型配置</RouterLink> 填好 API，
          再到 <RouterLink to="/upload">数据集导入</RouterLink> 处理公告，结果会显示在这里。
        </div>
        <main>
          <RouterView v-slot="{ Component }">
            <KeepAlive include="ItemsView,RelationsView,UploadView"><component :is="Component" /></KeepAlive>
          </RouterView>
        </main>
      </div>
    </div>
  </div>
</template>

<style scoped>
.masthead { display: flex; align-items: center; padding: 0 24px; height: 60px; background: var(--card);
  border-bottom: 1px solid var(--rule); position: sticky; top: 0; z-index: 20; }
.brand { display: flex; align-items: center; gap: 12px; min-width: 0; }
.seal { width: 36px; height: 36px; display: grid; place-items: center; background: var(--cinnabar); color: #fff7f0;
  font-family: var(--serif); font-weight: 700; font-size: 19px; border-radius: 3px; box-shadow: inset 0 0 0 2px #c95b4b; }
.brand-name { font-family: var(--serif); font-weight: 700; font-size: 16px; }
.brand-sub { font-size: 12px; color: var(--ink-3); margin-top: 2px; }

.body { display: flex; align-items: flex-start; }
.side { width: 188px; flex-shrink: 0; position: sticky; top: 60px; height: calc(100vh - 60px); overflow-y: auto;
  background: var(--card); border-right: 1px solid var(--rule); padding: 14px 0 24px; }
.side-group + .side-group { margin-top: 14px; }
.side-title { font-size: 11.5px; color: var(--ink-3); letter-spacing: .08em; padding: 0 20px 6px; }
.side-item { display: flex; align-items: center; gap: 10px; padding: 8px 20px; color: var(--ink-2); text-decoration: none;
  font-size: 14px; border-left: 3px solid transparent; }
.side-item:hover { color: var(--indigo); background: var(--paper); }
.side-item:focus-visible { outline: 2px solid var(--indigo-2); outline-offset: -2px; }
.side-item.on { color: var(--ink); font-weight: 600; background: var(--indigo-soft); border-left-color: var(--indigo); }
.side-no { font-family: var(--serif); color: var(--ink-3); font-size: 12px; width: 14px; flex-shrink: 0; text-align: center; white-space: nowrap; }
.side-item.on .side-no { color: var(--cinnabar); }

.content { flex: 1; min-width: 0; }
.strip { background: var(--indigo-soft); border-bottom: 1px solid #b9c4d8; color: var(--ink-2); padding: 7px 28px; font-size: 13px; }
.strip.err { background: #f6e1dd; border-bottom-color: #dcaaa2; color: var(--err); }

@media (max-width: 900px) {
  .body { display: block; }
  .side { position: static; width: auto; height: auto; border-right: 0; border-bottom: 1px solid var(--rule); padding: 6px 0; }
  .side nav { display: flex; overflow-x: auto; }
  .side-group { display: flex; margin: 0 !important; }
  .side-title { display: none; }
  .side-item { white-space: nowrap; border-left: 0; border-bottom: 2px solid transparent; padding: 8px 12px; }
  .side-item.on { border-bottom-color: var(--indigo); }
}
</style>
