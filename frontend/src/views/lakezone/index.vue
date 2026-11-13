<template>
  <section class="page" data-module="lakezone">
    <header class="page-head">
      <div>
        <h2>湖区分区管理</h2>
        <p class="page-desc">维护湖区分区方案的输入、执行约束与结果记录；归档前检查依赖快照、回填作业与下游聚合视图。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记分区方案</button>
        <button class="btn" type="button" @click="exportRows">导出湖区分区清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <span class="status-chip">{{ row.status }}</span>
          </td>
          <td class="row-actions">
            <button
              v-for="action in availableActions(row)"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无湖区分区数据，可先登记分区方案</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条湖区分区记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <section v-if="feedback" class="archive-panel" :class="{ 'is-error': !feedback.ok }">
      <h3>{{ feedback.title }}</h3>
      <p>{{ feedback.message }}</p>

      <template v-if="feedback.blocking.length">
        <h4>阻塞对象（存在未完成引用）</h4>
        <ul>
          <li v-for="item in feedback.blocking" :key="`${item.module}-${item.id}`">
            {{ item.type }} {{ item.code }} · {{ item.status }} — {{ item.reason }}
          </li>
        </ul>
      </template>

      <template v-if="feedback.dependencies.length">
        <h4>依赖对象（仍可读取，已标记依赖）</h4>
        <ul>
          <li v-for="item in feedback.dependencies" :key="`${item.module}-${item.id}`">
            {{ item.type }} {{ item.code }} · {{ item.status }} — {{ item.reason }}
          </li>
        </ul>
      </template>

      <template v-if="history.length">
        <h4>方案历史（保留天数按当时配置留档）</h4>
        <table class="data-table">
          <thead>
            <tr>
              <th>动作</th>
              <th>状态</th>
              <th>保留天数</th>
              <th>发生时间</th>
              <th>说明</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(item, index) in history" :key="index">
              <td>{{ item['动作'] }}</td>
              <td>{{ item['状态'] }}</td>
              <td>{{ item['保留天数'] ?? '—' }}</td>
              <td>{{ item['发生时间'] }}</td>
              <td>{{ item['说明'] }}</td>
            </tr>
          </tbody>
        </table>
      </template>
    </section>

    <section class="archive-panel">
      <header class="panel-head">
        <div>
          <h3>归档任务清单</h3>
          <p class="page-desc">默认只列待归档与已归档方案；按目标表筛选，避免混入其他业务域的分区。</p>
        </div>
        <form class="filter-bar" @submit.prevent="reloadArchiveTasks">
          <label class="filter-item">
            <span>目标表名</span>
            <input v-model="archiveTargetTable" placeholder="例如 dwd_order_detail" />
          </label>
          <label class="filter-item">
            <span>归档状态</span>
            <select v-model="archiveStatus">
              <option value="">待归档与已归档</option>
              <option value="待归档">待归档</option>
              <option value="已归档">已归档</option>
            </select>
          </label>
          <button class="btn" type="submit">筛选任务</button>
        </form>
      </header>

      <table class="data-table">
        <thead>
          <tr>
            <th>方案编号</th>
            <th>目标表名</th>
            <th>分区值</th>
            <th>归档任务状态</th>
            <th>归档保留天数</th>
            <th>归档时间</th>
            <th>业务域</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="task in archiveTasks" :key="String(task.id)">
            <td>{{ task['方案编号'] }}</td>
            <td>{{ task['归档目标表名'] ?? task['目标表名'] }}</td>
            <td>{{ task['分区值'] ?? '—' }}</td>
            <td>{{ task['归档任务状态'] }}</td>
            <td>{{ task['归档保留天数'] ?? '—' }}</td>
            <td>{{ task['归档时间'] ?? '—' }}</td>
            <td>{{ task['业务域'] ?? '—' }}</td>
          </tr>
          <tr v-if="!archiveTasks.length">
            <td colspan="7" class="empty-state">当前筛选条件下没有归档任务</td>
          </tr>
        </tbody>
      </table>
      <footer class="page-foot">
        <span>共 {{ archiveTotal }} 条归档任务</span>
      </footer>
    </section>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type ReferenceItem = {
  module: string
  type: string
  id: number
  code: string
  status: string
  reason: string
}

const ENDPOINT = '/api/lakezone'
const columns = ["方案编号", "目标表名", "分区字段", "分区粒度", "保留天数", "冷热分层", "归档路径", "小文件合并"]
const statuses = ["草稿", "待校验", "已启用", "待归档", "已归档"]
const stats = ref([
  {"label": "分区方案总数", "value": 0},
  {"label": "待归档方案", "value": 0},
  {"label": "已归档方案", "value": 0},
])

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const archiveTasks = ref<Row[]>([])
const archiveTotal = ref(0)
const archiveTargetTable = ref('')
const archiveStatus = ref('')
const taskFilters = ref<Record<string, string>>({})

const feedback = ref<{
  ok: boolean
  title: string
  message: string
  blocking: ReferenceItem[]
  dependencies: ReferenceItem[]
} | null>(null)
const history = ref<Record<string, unknown>[]>([])

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '分区方案登记入口尚未接入审批流'
}

function availableActions(row: Row): string[] {
  const status = String(row.status ?? '')
  if (status === '草稿') return ['提交校验']
  if (status === '待校验') return ['启用方案']
  if (status === '已启用') return ['申请归档']
  if (status === '待归档') return ['确认归档', '再次归档', '恢复方案']
  if (status === '已归档') return ['再次归档', '恢复方案']
  return []
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  // 申请归档前先做一次只读引用检查，满足“存在未完成引用时拒绝并列出阻塞对象”的交互。
  if (action === '申请归档') {
    try {
      const preview = await request(`${ENDPOINT}/${row.id}/references`)
      if (preview.ok) {
        const report = await preview.json()
        if (!report.allowed) {
          feedback.value = {
            ok: false,
            title: '「申请归档」未生效',
            message: `存在未完成引用，拒绝申请归档（拒绝停用）：${(report.blocking ?? [])
              .map((item: ReferenceItem) => `${item.type} ${item.code}（${item.status}）`)
              .join('、')}；快照与聚合视图仅标记为依赖，仍可读取`,
            blocking: report.blocking ?? [],
            dependencies: report.dependencies ?? [],
          }
          await reload()
          await reloadArchiveTasks()
          return
        }
      }
    } catch (error) {
      errorMessage.value = error instanceof Error ? error.message : '归档引用检查失败'
      return
    }
  }
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const payload = await response.json().catch(() => ({}))
    if (!response.ok) {
      throw new Error(payload?.detail ?? '湖区分区动作未生效，请稍后重试')
    }
    feedback.value = {
      ok: Boolean(payload.ok),
      title: payload.ok ? `「${action}」已生效` : `「${action}」未生效`,
      message: String(payload.message ?? ''),
      blocking: payload.blocking ?? [],
      dependencies: payload.dependencies ?? [],
    }
    if (payload.entry?.['方案历史']) {
      history.value = payload.entry['方案历史']
    }
    await reload()
    await reloadArchiveTasks()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '湖区分区操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('分区方案列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    stats.value[1].value = rows.value.filter((row) => row.status === '待归档').length
    stats.value[2].value = rows.value.filter((row) => row.status === '已归档').length
    stats.value[0].value = total.value
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '湖区分区列表读取失败'
  }
}

async function reloadArchiveTasks() {
  taskFilters.value = {}
  if (archiveTargetTable.value.trim()) {
    taskFilters.value.target_table = archiveTargetTable.value.trim()
  }
  if (archiveStatus.value) {
    taskFilters.value.status = archiveStatus.value
  }
  const query = new URLSearchParams(taskFilters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/archive-tasks?${query}`)
    if (!response.ok) {
      throw new Error('归档任务清单读取失败')
    }
    const payload = await response.json()
    archiveTasks.value = payload.items ?? []
    archiveTotal.value = payload.total ?? archiveTasks.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '归档任务清单读取失败'
  }
}

onMounted(async () => {
  await reload()
  await reloadArchiveTasks()
})
</script>

<style scoped>
.archive-panel { margin-top: 16px; background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; }
.archive-panel.is-error { border-color: #f5c2c0; background: #fff7f7; }
.archive-panel h3 { margin: 0 0 6px; font-size: 15px; }
.archive-panel h4 { margin: 10px 0 4px; font-size: 13px; color: var(--muted); }
.archive-panel ul { margin: 0; padding-left: 18px; font-size: 13px; line-height: 1.7; }
.panel-head { display: flex; justify-content: space-between; align-items: flex-end; gap: 12px; flex-wrap: wrap; }
.status-chip { display: inline-block; padding: 2px 8px; border-radius: 10px; background: #eef2f7; font-size: 12px; }
</style>
