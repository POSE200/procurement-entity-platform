<script setup lang="ts">
/**
 * 模型配置。API Key 只在提交时发送给本机后端（写入项目 .env），后端只回传掩码。
 * 可用模型通过服务商的 GET /models 接口获取。
 */
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '@/api/client'
import type { LlmConfig, ProviderHealth, ReadyzInfo } from '@/types/api'
import { ms } from '@/utils/format'

type P = 'qwen' | 'deepseek'
const cfg = ref<LlmConfig | null>(null)
const ready = ref<ReadyzInfo | null>(null)
const health = ref<(ProviderHealth & { elapsed: number; provider: string }) | null>(null)
const backendErr = ref('')
const testing = ref(false)
const saving = ref(false)
const showKey = ref(false)
const fetching = ref(false)
const fetched = reactive<Record<P, string[]>>({ qwen: [], deepseek: [] })
const fetchErr = ref('')

const PRESETS: Record<P, { label: string; base_url: string; models: string[] }> = {
  deepseek: { label: 'DeepSeek', base_url: 'https://api.deepseek.com', models: ['deepseek-chat', 'deepseek-reasoner'] },
  qwen: { label: 'Qwen / OpenAI 兼容服务', base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    models: ['qwen-plus', 'qwen-max', 'qwen-turbo'] },
}
const PROVIDERS: P[] = ['deepseek', 'qwen']
const form = reactive({ provider: 'deepseek' as P, base_url: '', model: '', api_key: '', max_concurrency: 16, timeout_seconds: 60 })

const REASON: Record<string, string> = {
  api_key_missing: '未配置 API Key',
  model_missing: '未配置模型名称',
  http_401: '认证失败：API Key 不正确',
  http_402: '账户余额不足',
  http_403: '无权访问该模型',
  http_404: '接口地址或模型名称不存在（检查 Base URL 与模型名）',
  http_429: '请求过于频繁或额度用尽',
  ConnectError: '无法连接模型服务（网络或 Base URL）',
  ReadTimeout: '模型服务响应超时',
  ConnectTimeout: '连接模型服务超时',
}

/** 推理类模型输出很长，抽取会明显变慢 */
const isSlow = (m: string) => /reason|r1|think|qwq/i.test(m)
const modelOptions = computed(() => {
  const list = fetched[form.provider].length ? fetched[form.provider] : PRESETS[form.provider].models
  return form.model && !list.includes(form.model) ? [form.model, ...list] : list
})

function fill(p: P) {
  const slot = cfg.value?.providers[p]
  form.base_url = slot?.base_url || PRESETS[p].base_url
  form.model = slot?.model || ''
  form.api_key = ''
  fetchErr.value = ''
  if (slot?.key_configured && !fetched[p].length) fetchModels(true)
}
watch(() => form.provider, fill)

async function refresh() {
  backendErr.value = ''
  try {
    ;[cfg.value, ready.value] = await Promise.all([api.llmConfig(), api.readyz()])
    form.provider = cfg.value.provider
    form.max_concurrency = cfg.value.max_concurrency
    form.timeout_seconds = cfg.value.timeout_seconds
    fill(form.provider)
  } catch (e) {
    backendErr.value = (e as Error).message
  }
}

/** 查询服务商可用模型；quiet=true 时失败不弹提示（页面加载时自动调用） */
async function fetchModels(quiet = false) {
  fetching.value = true
  fetchErr.value = ''
  const p = form.provider
  try {
    const r = await api.listModels(p, form.base_url.trim(), form.api_key.trim())
    fetched[p] = r.models
    if (!r.models.length) fetchErr.value = '服务商返回的模型列表为空，可直接输入模型 ID'
    else if (!form.model) form.model = r.models.find((m) => !isSlow(m)) ?? r.models[0]
    if (!quiet) ElMessage.success(`获取到 ${r.models.length} 个可用模型`)
  } catch (e) {
    fetchErr.value = (e as Error).message
  } finally {
    fetching.value = false
  }
}

const slot = computed(() => cfg.value?.providers[form.provider])
const canSave = computed(() => !!form.model.trim() && !!form.base_url.trim() && (!!form.api_key.trim() || !!slot.value?.key_configured))
const canFetch = computed(() => !!form.base_url.trim() && (!!form.api_key.trim() || !!slot.value?.key_configured))

async function save(andTest = false) {
  saving.value = true
  try {
    cfg.value = await api.saveLlmConfig({
      provider: form.provider, base_url: form.base_url.trim(), model: form.model.trim(),
      api_key: form.api_key.trim() || undefined, make_default: true,
      max_concurrency: form.max_concurrency, timeout_seconds: form.timeout_seconds,
    })
    form.api_key = ''
    ready.value = await api.readyz()
    ElMessage.success('已保存，立即生效')
    if (andTest) await test()
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    saving.value = false
  }
}

async function setDefault(p: P) {
  try {
    cfg.value = await api.saveLlmConfig({ provider: p, make_default: true })
    ready.value = await api.readyz()
    form.provider = p
    ElMessage.success(`已切换为 ${PRESETS[p].label}`)
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}

function edit(p: P) {
  if (form.provider === p) fill(p)
  else form.provider = p
}

async function test() {
  testing.value = true
  const t0 = performance.now()
  try {
    const h = await api.providerHealth(form.provider)
    health.value = { ...h, elapsed: performance.now() - t0, provider: form.provider }
  } catch (e) {
    health.value = { ok: false, configured: false, reason: 'request_failed', detail: (e as Error).message, elapsed: performance.now() - t0, provider: form.provider }
  } finally {
    testing.value = false
  }
}
onMounted(refresh)
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div>
        <h1><span class="no">捌</span>模型配置</h1>
        <div class="sub">配置自己的 API Key、接口地址与模型；配置保存在当前浏览器标签页。</div>
      </div>
      <el-button @click="refresh">刷新</el-button>
    </div>

    <div v-if="backendErr" class="banner err">
      <div><b>{{ backendErr }}</b><br />请通过“启动平台.bat”打开本程序，后端会随之启动。</div>
    </div>

    <section v-if="cfg" class="sheet">
      <h2 class="sheet-title">已保存的配置</h2>
      <el-table :data="PROVIDERS.map((p) => ({ p, ...cfg!.providers[p] }))" border>
        <el-table-column label="服务商" width="200">
          <template #default="{ row }">
            {{ PRESETS[row.p as P].label }}
            <span v-if="cfg!.provider === row.p" class="tag win" style="margin-left: 6px">当前使用</span>
          </template>
        </el-table-column>
        <el-table-column label="Base URL" min-width="240"><template #default="{ row }"><span class="mono">{{ row.base_url }}</span></template></el-table-column>
        <el-table-column label="模型" width="200"><template #default="{ row }"><span class="mono">{{ row.model || '未设置' }}</span></template></el-table-column>
        <el-table-column label="API Key" width="160">
          <template #default="{ row }"><span class="tag" :class="row.key_configured ? 'win' : 'lose'">{{ row.key_configured ? row.key_hint : '未设置' }}</span></template>
        </el-table-column>
        <el-table-column label="操作" width="190">
          <template #default="{ row }">
            <el-button size="small" @click="edit(row.p)">编辑</el-button>
            <el-button size="small" type="primary" :disabled="cfg!.provider === row.p || !row.key_configured || !row.model"
              @click="setDefault(row.p)">设为当前</el-button>
          </template>
        </el-table-column>
      </el-table>
      <p class="hint">并发数 {{ cfg.max_concurrency }} · 超时 {{ cfg.timeout_seconds }} 秒<span v-if="ready"> · {{ ready.provider_configured ? '可以开始处理数据' : '当前服务商还缺 API Key 或模型' }}</span></p>
    </section>

    <section class="sheet">
      <h2 class="sheet-title">编辑：{{ PRESETS[form.provider].label }}</h2>
      <el-form label-width="96px" label-position="left" @submit.prevent>
        <el-form-item label="服务商">
          <el-radio-group v-model="form.provider">
            <el-radio-button value="deepseek">DeepSeek</el-radio-button>
            <el-radio-button value="qwen">Qwen / 兼容服务</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="Base URL">
          <el-input v-model="form.base_url" :placeholder="PRESETS[form.provider].base_url" />
        </el-form-item>
        <el-form-item label="API Key">
          <el-input v-model="form.api_key" :type="showKey ? 'text' : 'password'" autocomplete="off"
            :placeholder="slot?.key_configured ? `已保存（${slot.key_hint}），留空沿用` : '粘贴 API Key，例如 sk-…'">
            <template #append><el-button @click="showKey = !showKey">{{ showKey ? '隐藏' : '显示' }}</el-button></template>
          </el-input>
        </el-form-item>
        <el-form-item label="模型">
          <div class="model-row">
            <el-select v-model="form.model" filterable allow-create default-first-option placeholder="获取列表后选择，或直接输入模型 ID" class="grow">
              <el-option v-for="m in modelOptions" :key="m" :label="isSlow(m) ? `${m}（推理模型，抽取较慢）` : m" :value="m" />
            </el-select>
            <el-button :loading="fetching" :disabled="!canFetch" @click="fetchModels(false)">获取可用模型</el-button>
          </div>
          <div class="hint">
            <template v-if="fetchErr">{{ fetchErr }}</template>
            <template v-else-if="fetched[form.provider].length">服务商返回 {{ fetched[form.provider].length }} 个模型</template>
            <template v-else>填好 Base URL 和 API Key 后点“获取可用模型”</template>
          </div>
        </el-form-item>
        <el-form-item label="并发数">
          <el-input-number v-model="form.max_concurrency" :min="1" :max="64" />
          <span class="hint" style="margin-left: 10px">同时调用模型的公告数，DeepSeek 建议 16–32</span>
        </el-form-item>
        <el-form-item label="超时（秒）">
          <el-input-number v-model="form.timeout_seconds" :min="5" :max="600" :step="10" />
        </el-form-item>
        <div class="row">
          <el-button type="primary" :loading="saving" :disabled="!canSave" @click="save(true)">保存并测试</el-button>
          <el-button :loading="saving" :disabled="!canSave" @click="save(false)">仅保存</el-button>
          <el-button :loading="testing" :disabled="!!backendErr" @click="test">测试连接</el-button>
        </div>
      </el-form>
      <p class="hint">测试会发送一条简短的 JSON 请求。自建的 OpenAI 兼容服务选“Qwen / 兼容服务”，Base URL 填到 /v1。</p>

      <div v-if="health" class="banner" :class="health.ok ? 'info' : 'err'" style="margin-top: 12px">
        <div>
          <b>{{ health.ok ? '连接成功' : '连接失败' }}</b> · {{ health.provider }} · 耗时 {{ ms(health.elapsed) }}<br />
          <template v-if="!health.ok">{{ REASON[health.reason ?? ''] ?? health.reason }}<span v-if="health.detail">：{{ health.detail }}</span></template>
          <template v-else>模型返回：<span class="mono">{{ health.response }}</span></template>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.model-row { display: flex; gap: 8px; width: 100%; }
.model-row .grow { flex: 1; }
</style>
