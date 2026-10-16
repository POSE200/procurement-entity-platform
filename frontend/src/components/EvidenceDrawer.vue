<script setup lang="ts">
/**
 * 证据抽屉：展示字段在公告原文中的出处。
 * - 传 focusItem：逐字段定位该标的物的原文片段；
 * - 传 announcementIds：列出关系统计所依据的公告（关系分析页）。
 */
import { computed, ref, watch } from 'vue'
import { api } from '@/api/client'
import type { AnnouncementDetail, AnnouncementSummary, Item } from '@/types/api'
import { findSnippets, numberForms, type Snippet } from '@/utils/evidence'
import { date, money, moneyWan, text } from '@/utils/format'

const props = defineProps<{
  modelValue: boolean
  announcementId?: string
  focusItem?: Item | null
  announcementIds?: string[]
  title?: string
}>()
const emit = defineEmits<{ 'update:modelValue': [boolean] }>()
const open = computed({ get: () => props.modelValue, set: (v) => emit('update:modelValue', v) })

const detail = ref<AnnouncementDetail | null>(null)
const list = ref<AnnouncementSummary[]>([])
const loading = ref(false)
const error = ref('')

async function load() {
  error.value = ''
  detail.value = null
  list.value = []
  loading.value = true
  try {
    if (props.announcementIds?.length) {
      const all = await Promise.all(props.announcementIds.slice(0, 60).map((id) => api.announcement(id).catch(() => null)))
      list.value = all.filter((x): x is AnnouncementDetail => !!x)
    } else if (props.announcementId) {
      detail.value = await api.announcement(props.announcementId)
    }
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    loading.value = false
  }
}
watch(() => [props.modelValue, props.announcementId, props.announcementIds] as const, ([v]) => { if (v) load() })

const FIELDS: [keyof Item, string][] = [
  ['item_name', '标的名称'], ['category', '品目'], ['brand', '品牌'], ['model', '规格型号'],
  ['unit_price', '单价'], ['quantity', '数量'], ['total_price', '总价'],
]
const fieldEvidence = computed(() => {
  const d = detail.value
  const it = props.focusItem
  if (!d || !it) return []
  const full = d.summary_text + '\n' + d.body_text
  return FIELDS.map(([k, label]) => {
    const v = it[k]
    const terms = typeof v === 'number' ? numberForms(v) : [v as string | null]
    const snips: Snippet[] = v === null || v === undefined ? [] : findSnippets(full, terms, 50, 1)
    const shown = k === 'unit_price' || k === 'total_price' ? money(v as number | null) : text(v)
    return { k, label, shown, missing: v === null || v === undefined, snips }
  })
})
</script>

<template>
  <el-drawer v-model="open" :title="title ?? (announcementIds?.length ? '统计依据' : '原文证据')" size="min(760px, 92vw)" append-to-body>
    <div v-if="error" class="banner err">{{ error }}</div>
    <div v-if="loading" class="hint">加载中…</div>

    <!-- 标的物字段证据 -->
    <template v-if="detail && focusItem">
      <div class="doc-head">
        <div class="serif doc-title">{{ detail.title }}</div>
        <div class="hint">{{ detail.buyer }} · {{ date(detail.publish_time) }} · 包 {{ text(focusItem.package_no) }} ·
          <RouterLink :to="`/announcements/${detail.announcement_id}`" @click="open = false">查看公告详情</RouterLink></div>
      </div>
      <table class="ev">
        <tbody>
          <tr v-for="f in fieldEvidence" :key="f.k">
            <th>{{ f.label }}</th>
            <td>
              <div :class="{ missing: f.missing }">{{ f.shown }}</div>
              <div v-for="(s, i) in f.snips" :key="i" class="quote">
                <span class="muted">…{{ s.before }}</span><mark>{{ s.hit }}</mark><span class="muted">{{ s.after }}…</span>
                <span class="ln">第 {{ s.line }} 行</span>
              </div>
              <div v-if="!f.missing && !f.snips.length" class="hint">该值出自附件：{{ detail.gold_source ?? '未记录来源' }}</div>
            </td>
          </tr>
        </tbody>
      </table>
    </template>

    <!-- 关系统计依据 -->
    <template v-if="announcementIds?.length">
      <p class="hint">共 {{ announcementIds.length }} 篇公告<span v-if="announcementIds.length > 60">，此处显示前 60 篇</span>。</p>
      <div v-for="a in list" :key="a.announcement_id" class="ann">
        <RouterLink :to="`/announcements/${a.announcement_id}`" class="serif" @click="open = false">{{ a.title }}</RouterLink>
        <div class="hint">{{ a.buyer }} · {{ date(a.publish_time) }} · 总中标金额 {{ moneyWan(a.total_amount) }}</div>
        <div class="bidders">
          <span v-for="b in a.bidders" :key="b.name + b.package_no" class="tag" :class="b.role === 'winner' ? 'win' : 'lose'">
            {{ b.package_no ? `包${b.package_no} · ` : '' }}{{ b.name }}{{ b.role === 'winner' ? ' · 中标' : '' }}
          </span>
        </div>
      </div>
    </template>
  </el-drawer>
</template>

<style scoped>
.doc-head { border-bottom: 1px solid var(--rule); padding-bottom: 10px; margin-bottom: 12px; }
.doc-title { font-size: 16px; line-height: 1.5; }
.ev { width: 100%; border-collapse: collapse; }
.ev th { width: 84px; text-align: left; vertical-align: top; padding: 10px 8px; font-family: var(--serif); font-weight: 600; color: var(--ink-2);
  border-bottom: 1px solid var(--rule-2); }
.ev td { padding: 10px 8px; border-bottom: 1px solid var(--rule-2); }
.quote { margin-top: 6px; padding: 6px 10px; background: var(--paper); border-left: 2px solid var(--indigo); font-size: 12px; line-height: 1.7; word-break: break-all; }
mark { background: #f1dfae; color: var(--ink); padding: 0 1px; }
.ln { color: var(--ink-3); margin-left: 8px; font-size: 11px; }
.ann { padding: 10px 0; border-bottom: 1px solid var(--rule-2); }
.ann a { text-decoration: none; }
.bidders { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; }
</style>
