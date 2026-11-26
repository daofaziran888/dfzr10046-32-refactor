<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'

import {
  createEntry,
  downloadEntries,
  listEntries,
  moduleMeta,
  runAction as applyAction,
} from '@/api/backend-service'
import { inflight } from '@/api/inflight'
import type { EntryRow } from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
// 同一套工作台组件会被两个模块页复用（重跑任务 / 血缘关系），因此页面元数据
// 必须是响应式的：切页时字段、状态、动作随模块一起换，不能停留在首次挂载的模块。
const meta = computed(() => moduleMeta(props.moduleKey))
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = computed(() => meta.value.fields.slice(0, 3))
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const stats = computed(() => [
  { label: meta.value.metrics[0], value: total.value },
  {
    label: meta.value.metrics[1],
    value: rows.value.filter((row) => row.status === meta.value.statuses[1]).length,
  },
  {
    label: meta.value.metrics[2],
    value: rows.value.filter((row) => row.status === meta.value.statuses[meta.value.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.value.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.value.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.value.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.value.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: EntryRow) {
  // 发起时就按「本页模块 + 记录 + 动作」认领这个在途请求；同号记录在
  // 另一个模块也有动作时，两边的回包各自结算，晚到的结果不会关闭本页提交。
  const moduleKey = meta.value.key
  const token = inflight.claim({ module: moduleKey, id: Number(row.id), action })
  errorMessage.value = ''
  try {
    const result = await applyAction(moduleKey, Number(row.id), action)
    if (!inflight.settle(token, result)) {
      // 回包归属与本页认领不符：按越权/串号处理，不写入本页表格。
      throw new Error(result.message || '动作回包归属与本页不符，已忽略')
    }
    if (!result.ok) throw new Error(result.message)
    // 期间已切到另一个模块页时，只在自己模块仍处于当前页时刷新，
    // 避免把这次动作的结果接到另一模块的表格上。
    if (moduleKey === meta.value.key) await reload()
  } catch (error) {
    inflight.release(token)
    if (moduleKey === meta.value.key) {
      errorMessage.value = error instanceof Error ? error.message : '操作失败'
    }
  }
}

async function reload() {
  const moduleKey = meta.value.key
  errorMessage.value = ''
  try {
    const payload = await listEntries(moduleKey, filters.value)
    // 切页后旧模块的列表回包晚到，不能覆盖当前页的表格。
    if (moduleKey !== meta.value.key) return
    rows.value = payload.items
    total.value = payload.total
  } catch (error) {
    if (moduleKey !== meta.value.key) return
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

// 同一组件在两个模块页之间切换（如 重跑任务 ↔ 血缘关系）时，重置筛选、
// 关掉登记弹窗并按新模块重新拉取；在途动作仍按各自模块认领，互不干扰。
watch(
  () => props.moduleKey,
  () => {
    filters.value = {}
    showCreate.value = false
    errorMessage.value = ''
    void reload()
  },
)

onMounted(reload)
</script>
