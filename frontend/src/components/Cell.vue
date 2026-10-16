<script setup lang="ts">
/** 表格单元：空值统一显示“未披露”，金额与数量右对齐 */
import { computed } from 'vue'
import { MISSING, isBlank, money, num } from '@/utils/format'

const props = defineProps<{ value: unknown; kind?: 'text' | 'count' | 'money' | 'list' | 'date' }>()
const blank = computed(() => isBlank(props.value))
const shown = computed(() => {
  const v = props.value
  if (blank.value) return MISSING
  if (props.kind === 'money') return money(v as number)
  if (props.kind === 'count') return num(v as number)
  if (props.kind === 'date') return String(v).slice(0, 10)
  return String(v)
})
</script>

<template>
  <span v-if="blank" class="missing">{{ MISSING }}</span>
  <span v-else :class="{ num: kind === 'money' || kind === 'count' }">{{ shown }}</span>
</template>
