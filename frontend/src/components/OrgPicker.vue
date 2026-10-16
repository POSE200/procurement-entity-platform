<script setup lang="ts">
/** 机构检索选择器：远程搜索采购单位或中标供应商，支持单选/多选 */
import { ref, watch } from 'vue'
import { api } from '@/api/client'
import type { Organization } from '@/types/api'

const props = defineProps<{ role: 'buyer' | 'winner' | 'bidder'; multiple?: boolean; placeholder?: string; jobId?: string }>()
const model = defineModel<string | string[]>()
const options = ref<Organization[]>([])
const loading = ref(false)
let seq = 0

async function search(q: string) {
  const my = ++seq
  loading.value = true
  try {
    const res = await api.organizations(q, props.role, props.jobId)
    if (my === seq) options.value = res
  } catch {
    if (my === seq) options.value = []
  } finally {
    if (my === seq) loading.value = false
  }
}
watch(() => [props.jobId, props.role], () => { options.value = []; search('') }, { immediate: true })
</script>

<template>
  <el-select v-model="model" :multiple="multiple" filterable remote reserve-keyword :remote-method="search" :loading="loading"
    :placeholder="placeholder" :multiple-limit="multiple ? 5 : 0" collapse-tags collapse-tags-tooltip clearable style="width: 100%"
    @focus="options.length || search('')">
    <el-option v-for="o in options" :key="o.org_id" :label="o.name" :value="o.name">
      <div class="opt"><span>{{ o.name }}</span>
        <span class="muted">{{ role === 'buyer' ? `采购 ${o.as_buyer}` : `中标 ${o.as_winner} · 参与 ${o.as_participant}` }}</span></div>
    </el-option>
  </el-select>
</template>

<style scoped>
.opt { display: flex; justify-content: space-between; gap: 16px; }
.opt .muted { font-size: 12px; }
</style>
