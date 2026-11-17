<template>
  <section class="page" data-module="dimension-mapping">
    <header class="page-head">
      <div>
        <h2>维度成员映射 · 版本化刷新</h2>
        <p class="page-desc">
          清洗维度成员映射与关联数据集刷新记录，按映射版本做成员级稳定更新；写入与读取共享同一提交边界，
          连续刷新只落一个有效版本，成员归属刷新前后保持一致。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="export-mapping" type="button" @click="exportMapping">导出维度成员映射表</button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">当前生效成员</span>
        <strong class="stat-value">{{ stats.effective }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">当前映射版本</span>
        <strong class="stat-value" data-testid="head-version">{{ stats.headVersion || '—' }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">提交边界标识</span>
        <strong class="stat-value" style="font-size: 14px">{{ stats.commit || '—' }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">待核对冲突文件</span>
        <strong class="stat-value" :class="{ 'error-text': stats.pending > 0 }" data-testid="pending-count">{{ stats.pending }}</strong>
      </article>
    </div>

    <nav class="tab-bar">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        class="tab-btn"
        :class="{ active: activeTab === tab.key }"
        :data-testid="`tab-${tab.key}`"
        type="button"
        @click="switchTab(tab.key)"
      >
        {{ tab.label }}
      </button>
    </nav>

    <div v-if="message" class="notice" :class="messageKind" data-testid="notice">{{ message }}</div>

    <!-- ============ 成员映射 ============ -->
    <div v-show="activeTab === 'mapping'">
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="col in mappingColumns" :key="col">{{ col }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in mappings" :key="String(row.id)" :data-testid="`mapping-row-${row.成员编码}`"
              :class="{ inactive: row.status === '已停用' }">
            <td v-for="col in mappingColumns" :key="col">{{ row[col] ?? '—' }}</td>
          </tr>
          <tr v-if="!mappings.length">
            <td :colspan="mappingColumns.length" class="empty-state">暂无维度成员映射数据</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- ============ 数据集刷新 ============ -->
    <div v-show="activeTab === 'refresh'">
      <form class="analysis-form" @submit.prevent="submitRefresh">
        <label>维度编号
          <input v-model="refreshForm.dimension_code" data-testid="refresh-dimension" required />
        </label>
        <label>关联数据集
          <input v-model="refreshForm.dataset_code" data-testid="refresh-dataset" required />
        </label>
        <label>期望前置版本（并发核对，可留空）
          <input v-model="refreshForm.expected_previous_version" data-testid="refresh-expected"
                 :placeholder="stats.headVersion || '留空表示不校验'" />
        </label>
        <div style="flex-basis: 100%">
          <label style="display: block; margin-bottom: 4px">
            成员输入（每行一个：成员编码,成员名称,父级编码；根节点父级留空；重复编码与空编码自动清洗）
          </label>
          <textarea v-model="memberText" data-testid="refresh-members" rows="8" class="member-input"></textarea>
        </div>
        <button class="btn primary" data-testid="submit-refresh" type="submit" :disabled="refreshing">
          {{ refreshing ? '提交中…' : '按映射版本稳定刷新' }}
        </button>
        <button class="btn" type="button" data-testid="burst-refresh" :disabled="refreshing" @click="burstRefresh">
          模拟连续 5 次快速刷新
        </button>
      </form>

      <table class="data-table">
        <thead>
          <tr>
            <th v-for="col in refreshColumns" :key="col">{{ col }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in refreshRecords" :key="String(row.id)"
              :data-testid="`refresh-row-${row.刷新序号}`"
              :class="{ 'head-row': row.刷新状态 === '当前版本' }">
            <td v-for="col in refreshColumns" :key="col">{{ row[col] ?? '—' }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- ============ 对照文件 ============ -->
    <div v-show="activeTab === 'checkpoint'">
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="col in checkpointColumns" :key="col">{{ col }}</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="cp in checkpoints" :key="String(cp.id)"
              :data-testid="`cp-row-${cp.id}`" :data-state="String(cp.对照状态)">
            <td v-for="col in checkpointColumns" :key="col">{{ cp[col] ?? '—' }}</td>
            <td class="row-actions">
              <button class="link" type="button" :data-testid="`cp-open-${cp.id}`" @click="openCheckpoint(cp)">打开</button>
              <button v-if="cp.对照状态 === '待核对'" class="link" type="button"
                      :data-testid="`cp-verify-${cp.id}`" @click="verifyCp(cp)">核对主版本边界</button>
              <button class="link" type="button" :data-testid="`cp-export-${cp.id}`" @click="exportCheckpoint(cp)">导出对照</button>
            </td>
          </tr>
          <tr v-if="!checkpoints.length">
            <td :colspan="checkpointColumns.length + 1" class="empty-state">暂无维度刷新对照文件</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- ============ 分析与回退 ============ -->
    <div v-show="activeTab === 'analyze'">
      <form class="analysis-form" @submit.prevent="runAnalyze">
        <label>消费方
          <input v-model="analyzeConsumer" data-testid="analyze-consumer" value="维度分析接口" />
        </label>
        <button class="btn primary" data-testid="run-analyze" type="button" @click="runAnalyze">消费当前对照文件并持久化</button>
        <button class="btn" type="button" data-testid="rollback-btn" @click="rollbackHead">回退到上一历史版本</button>
      </form>
      <div v-if="analysis" class="analysis-card" data-testid="analysis-result">
        <p><strong>对照文件：</strong>{{ analysis.对照文件编号 }} ｜ <strong>映射版本：</strong>{{ analysis.映射版本 }}</p>
        <p><strong>提交边界：</strong>{{ analysis.提交边界标识 }} ｜ <strong>成员数：</strong>{{ analysis.成员数 }}</p>
        <p><strong>层级分布：</strong>{{ formatMap(analysis.层级分布) }}</p>
        <p><strong>消费持久化时间：</strong>{{ analysis.消费持久化时间 }}（{{ analysis.消费方 }}）</p>
      </div>
    </div>

    <!-- 对照文件详情弹层：来源输入可追溯 -->
    <div v-if="detail" class="modal-backdrop" data-testid="cp-modal" @click.self="detail = null">
      <div class="modal" style="width: min(820px, 100%)">
        <h3>维度刷新对照文件 {{ detail.对照文件编号 }}</h3>
        <dl class="detail-grid">
          <dt>映射版本</dt><dd>{{ detail.映射版本 }}</dd>
          <dt>主版本序号</dt><dd>{{ detail.主版本序号 || '—（冲突文件）' }}</dd>
          <dt>提交边界标识</dt><dd>{{ detail.提交边界标识 || '—' }}</dd>
          <dt>对照状态</dt><dd>{{ detail.对照状态 }}</dd>
          <dt>来源输入</dt><dd>{{ detail.来源输入 }}</dd>
          <dt>回退自版本</dt><dd>{{ detail.回退自版本 || '—' }}</dd>
          <dt>分析消费</dt><dd>{{ detail.分析消费时间 ? `${detail.分析消费时间} · ${detail.分析消费方}` : '尚未消费' }}</dd>
        </dl>
        <h4>刷新前后父级对照</h4>
        <table class="data-table">
          <thead>
            <tr><th>成员编码</th><th>成员名称</th><th>刷新前父级</th><th>刷新后父级</th><th>变更类型</th></tr>
          </thead>
          <tbody>
            <tr v-for="d in detail.对照明细" :key="d.成员编码">
              <td>{{ d.成员编码 }}</td><td>{{ d.成员名称 }}</td>
              <td>{{ d.刷新前父级 }}</td><td>{{ d.刷新后父级 }}</td><td>{{ d.变更类型 }}</td>
            </tr>
            <tr v-if="!detail.对照明细?.length">
              <td colspan="5" class="empty-state">该文件无成员级差异（冲突文件等待核对）</td>
            </tr>
          </tbody>
        </table>
        <div class="page-actions" style="margin-top: 12px">
          <button class="btn" type="button" @click="detail = null">关闭</button>
        </div>
      </div>
    </div>

    <footer class="page-foot">
      <span>成员归属以已提交映射版本为准，重新进入页面保持不变</span>
      <span v-if="message" :class="messageKind === 'error-text' ? 'error-text' : ''">{{ message }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type MappingRow = Record<string, string | number | null> & { status?: string, 成员编码: string }
type Checkpoint = Record<string, any>

const ENDPOINT = '/api/dimension'
const tabs = [
  { key: 'mapping', label: '维度成员映射表' },
  { key: 'refresh', label: '数据集刷新记录' },
  { key: 'checkpoint', label: '维度刷新对照文件' },
  { key: 'analyze', label: '分析消费与回退' },
]
const mappingColumns = ['成员编码', '成员名称', '父级编码', '父级名称', '层级', 'status', '映射版本', '首次写入版本', '最后更新版本']
const refreshColumns = ['刷新记录编号', '刷新序号', '映射版本', '刷新前版本', '刷新状态', '刷新时间', '成员数', '提交边界标识', '变更摘要']
const checkpointColumns = ['对照文件编号', '映射版本', '主版本序号', '对照状态', '来源输入', '提交边界标识', '生成时间', '回退自版本']

const activeTab = ref('mapping')
const mappings = ref<MappingRow[]>([])
const refreshRecords = ref<MappingRow[]>([])
const checkpoints = ref<Checkpoint[]>([])
const message = ref('')
const messageKind = ref('')
const refreshing = ref(false)
const memberText = ref('')
const detail = ref<Checkpoint | null>(null)
const analysis = ref<Record<string, any> | null>(null)
const analyzeConsumer = ref('维度分析接口')
const refreshForm = ref({
  dimension_code: 'DIME-0001',
  dimension_name: '区域维度',
  dataset_code: 'DATA-0003',
  dataset_name: '区域销售数据集',
  expected_previous_version: '',
})

const stats = computed(() => {
  const effective = mappings.value.filter((r) => r.status === '生效')
  const head = refreshRecords.value.find((r) => r.刷新状态 === '当前版本')
  const pending = checkpoints.value.filter((c) => c.对照状态 === '待核对').length
  return {
    effective: effective.length,
    headVersion: String(head?.映射版本 ?? ''),
    commit: String(head?.提交边界标识 ?? ''),
    pending,
  }
})

function notify(text: string, ok = true) {
  message.value = text
  messageKind.value = ok ? '' : 'error-text'
}

function formatMap(map: Record<string, number> | undefined): string {
  return map ? Object.entries(map).map(([k, v]) => `${k}:${v}`).join(' / ') : '—'
}

async function loadAll() {
  const [mRes, rRes, cRes] = await Promise.all([
    request(`${ENDPOINT}/mappings`), request(`${ENDPOINT}/refresh-records`),
    request(`${ENDPOINT}/checkpoints`),
  ])
  if (!mRes.ok || !rRes.ok || !cRes.ok) {
    notify('维度成员映射数据读取失败', false)
    return
  }
  const [mJson, rJson, cJson] = await Promise.all([mRes.json(), rRes.json(), cRes.json()])
  mappings.value = mJson.items ?? []
  refreshRecords.value = rJson.items ?? []
  checkpoints.value = cJson.items ?? []
}

function switchTab(key: string) {
  activeTab.value = key
  void loadAll()
}

function parseMembers(): { 成员编码: string; 成员名称: string; 父级编码: string }[] {
  const seen = new Set<string>()
  const members: { 成员编码: string; 成员名称: string; 父级编码: string }[] = []
  for (const line of memberText.value.split('\n')) {
    const parts = line.split(',').map((s) => s.trim())
    const code = parts[0] ?? ''
    if (!code || seen.has(code)) continue // 与后端同一口径：空编码丢弃、重复编码去重
    seen.add(code)
    members.push({ 成员编码: code, 成员名称: parts[1] || code, 父级编码: parts[2] ?? '' })
  }
  return members
}

async function submitRefresh() {
  refreshing.value = true
  try {
    const members = parseMembers()
    if (!members.length) {
      notify('成员输入为空或全部被清洗，请检查每行格式：成员编码,成员名称,父级编码', false)
      return
    }
    const response = await request(`${ENDPOINT}/refresh`, {
      method: 'POST',
      body: JSON.stringify({ ...refreshForm.value, members }),
    })
    const payload = await response.json()
    if (payload.conflict) {
      notify(`${payload.message}（冲突文件：${payload.冲突对照文件编号}）`, false)
    } else if (!payload.ok) {
      notify(payload.message || '刷新未生效', false)
    } else {
      notify(payload.idempotent
        ? `幂等命中：${payload.message}（${payload.映射版本}）`
        : `${payload.message}，成员级变更 ${payload.变更数 ?? 0} 条`)
    }
    await loadAll()
  } catch (error) {
    notify(error instanceof Error ? error.message : '刷新请求失败', false)
  } finally {
    refreshing.value = false
  }
}

async function burstRefresh() {
  // 连续快速刷新同一份输入：后端锁内串行 + 摘要幂等，最终只落一个有效版本
  refreshing.value = true
  const members = parseMembers()
  if (!members.length) {
    notify('请先填写成员输入再模拟连续刷新', false)
    refreshing.value = false
    return
  }
  const results = await Promise.all(Array.from({ length: 5 }, () =>
    request(`${ENDPOINT}/refresh`, {
      method: 'POST',
      body: JSON.stringify({ ...refreshForm.value, expected_previous_version: null, members }),
    }).then((r) => r.json())))
  const committed = results.filter((r) => r.ok && !r.idempotent).length
  const skipped = results.filter((r) => r.idempotent).length
  const conflicted = results.filter((r) => r.conflict).length
  notify(`连续 5 次刷新完成：新落有效版本 ${committed} 个、幂等跳过 ${skipped} 个、冲突 ${conflicted} 个`)
  refreshing.value = false
  await loadAll()
}

async function verifyCp(cp: Checkpoint) {
  const response = await request(`${ENDPOINT}/checkpoints/${cp.id}/verify`, { method: 'POST' })
  const payload = await response.json()
  notify(payload.message, payload.ok)
  await loadAll()
}

async function openCheckpoint(cp: Checkpoint) {
  const response = await request(`${ENDPOINT}/checkpoints/${cp.id}`)
  detail.value = await response.json()
}

async function runAnalyze() {
  const response = await request(`${ENDPOINT}/analyze`, {
    method: 'POST',
    body: JSON.stringify({ consumer: analyzeConsumer.value }),
  })
  const payload = await response.json()
  if (!payload.ok) {
    notify(payload.message || '分析消费失败', false)
    return
  }
  analysis.value = payload
  notify(`分析接口已消费 ${payload.对照文件编号}，消费事实已持久化`)
  await loadAll()
}

async function rollbackHead() {
  const history = refreshRecords.value
    .filter((r) => r.刷新状态 === '历史版本')
    .sort((a, b) => Number(b.刷新序号) - Number(a.刷新序号))
  const target = history[0]?.映射版本
  if (!target) {
    notify('没有可回退的历史映射版本', false)
    return
  }
  const response = await request(`${ENDPOINT}/rollback`, {
    method: 'POST',
    body: JSON.stringify({ target_version: target, operator: analyzeConsumer.value }),
  })
  const payload = await response.json()
  notify(payload.message, payload.ok)
  await loadAll()
}

function download(path: string, filename: string) {
  return request(path).then(async (response) => {
    if (!response.ok) throw new Error('导出失败')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = filename
    link.click()
    URL.revokeObjectURL(url)
  })
}

async function exportMapping() {
  try {
    await download(`${ENDPOINT}/mappings/export`, 'dimension_member_mapping.csv')
    notify('维度成员映射表已导出，可对照刷新前后差异')
  } catch (error) {
    notify(error instanceof Error ? error.message : '导出失败', false)
  }
}

async function exportCheckpoint(cp: Checkpoint) {
  try {
    await download(`${ENDPOINT}/checkpoints/${cp.id}/export`, `checkpoint_${cp.对照文件编号}.csv`)
  } catch (error) {
    notify(error instanceof Error ? error.message : '导出失败', false)
  }
}

onMounted(loadAll)
</script>

<style scoped>
.tab-bar { display: flex; gap: 8px; margin-bottom: 12px; }
.tab-btn { border: 1px solid var(--border); background: #fff; border-radius: 6px 6px 0 0; padding: 8px 14px; cursor: pointer; font-size: 13px; }
.tab-btn.active { background: var(--brand); border-color: var(--brand); color: #fff; }
.notice { padding: 8px 12px; border-radius: 6px; margin-bottom: 10px; font-size: 13px; background: #ecfdf3; border: 1px solid #abefc6; color: #067647; }
.notice.error-text { background: #fef3f2; border-color: #fda29b; color: #b42318; }
.inactive { color: var(--muted); background: #f8fafc; }
.head-row { background: #eff8ff; font-weight: 600; }
.member-input { width: 100%; font-family: monospace; border: 1px solid var(--border); border-radius: 6px; padding: 8px; }
.detail-grid { display: grid; grid-template-columns: 120px 1fr; gap: 6px 10px; font-size: 13px; }
.detail-grid dt { color: var(--muted); }
.detail-grid dd { margin: 0; }
</style>
