<template>
  <section class="page locator-page" data-module="pavement">
    <header class="page-head">
      <div>
        <h2>桩号桶定位器</h2>
        <p class="page-desc">
          按道路方向建立连续桶，依次完成建索引、校验桩号、切换定位；巡线结论同步到巡查待办、桥隧限载清单和病害台账。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="exportRows">导出病害台账</button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">定位阶段</span>
        <strong class="stat-value">{{ locator?.stage ?? '待建索引' }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">现行版本</span>
        <strong class="stat-value">v{{ locator?.version ?? 1 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">现行桶内病害</span>
        <strong class="stat-value">{{ locator?.defects?.length ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">旧桩号快照</span>
        <strong class="stat-value">{{ locator?.snapshots?.length ?? 0 }}</strong>
      </article>
    </div>

    <section class="locator-panel">
      <div class="panel-head">
        <div>
          <h3>受状态栅栏控制的巡线流程</h3>
          <p>
            当前道路：{{ locator?.roadName ?? road }} · 方向：{{ locator?.direction ?? '顺桩' }} ·
            桶游标：{{ activeCursor || '未选择' }}
          </p>
        </div>
        <button class="btn primary" type="button" :disabled="loading" @click="buildIndex">
          1. 建立桶索引
        </button>
      </div>

      <ol class="stage-rail">
        <li v-for="stage in stageLabels" :key="stage" :class="{ active: locator?.stage === stage }">
          <span>{{ stage }}</span>
        </li>
      </ol>

      <div class="control-grid">
        <form class="control-card" @submit.prevent="validateStation">
          <h4>2. 校验桩号</h4>
          <label>
            <span>桩号</span>
            <input v-model="stationInput" placeholder="例如 K0+220" />
          </label>
          <button class="btn" type="submit" :disabled="loading">校验并选中桶头</button>
        </form>

        <form class="control-card" @submit.prevent="locateDisease">
          <h4>3. 切换定位</h4>
          <label>
            <span>病害 ID</span>
            <input v-model.number="locateDiseaseId" type="number" min="1" placeholder="使用校验桶内病害 ID" />
          </label>
          <button class="btn primary" type="submit" :disabled="loading">切换定位进入巡线</button>
        </form>

        <form class="control-card wide" @submit.prevent="syncConclusion">
          <h4>巡线结论同步</h4>
          <label>
            <span>结论</span>
            <input v-model="conclusionInput" placeholder="例如：坑槽伴松散，建议限载30t并限期修补" />
          </label>
          <div class="inline-fields">
            <label>
              <span>桥梁限载提示</span>
              <input v-model="bridgeLimitInput" placeholder="可覆盖同桶桥梁提示" />
            </label>
            <label>
              <span>隧道限载提示</span>
              <input v-model="tunnelLimitInput" placeholder="可覆盖同桶隧道提示" />
            </label>
          </div>
          <button class="btn primary" type="submit" :disabled="loading">同步到三类清单</button>
        </form>
      </div>

      <form class="control-card realign-card" @submit.prevent="realignRoute">
        <h4>并发改线版本栅栏</h4>
        <p class="hint">旧病害保留改线前桩号快照；旧桶只归档，不能覆盖现行路线。</p>
        <div class="inline-fields">
          <label>
            <span>提交版本</span>
            <input v-model.number="realignVersion" type="number" min="1" />
          </label>
          <label>
            <span>新起点</span>
            <input v-model="realignStart" />
          </label>
          <label>
            <span>新终点</span>
            <input v-model="realignEnd" />
          </label>
          <label>
            <span>方向</span>
            <select v-model="realignDirection">
              <option>顺桩</option>
              <option>逆桩</option>
            </select>
          </label>
        </div>
        <button class="btn" type="submit" :disabled="loading">按版本改线并重建索引</button>
      </form>
    </section>

    <section class="workspace-grid">
      <div class="bucket-column">
        <div ref="bucketRailEl" class="bucket-rail" @scroll.passive="onBucketScroll">
          <button
            v-for="bucket in locator?.buckets ?? []"
            :key="bucket.cursor"
            :ref="(el) => setBucketRef(el, bucket.cursor)"
            type="button"
            class="bucket-card"
            :class="{ active: bucket.cursor === activeCursor }"
            @click="selectBucket(bucket.cursor)"
          >
            <strong>{{ bucket.head }}</strong>
            <span>{{ bucket.direction }} · {{ bucket.diseaseIds.length }} 条病害</span>
            <small>{{ bucket.diseaseIds.join('、') || '空桶连续占位' }}</small>
          </button>
        </div>

        <form class="preview-card" @submit.prevent="previewDisease">
          <h3>相邻病害预览</h3>
          <label>
            <span>滚过桶头后按病害编号预览</span>
            <input v-model="previewCode" placeholder="例如 PAVE-0002" />
          </label>
          <button class="btn" type="submit">预览相邻记录</button>
          <div v-if="previewResult" class="preview-result">
            <p :class="{ stale: previewResult.stale }">
              当前：{{ previewResult.disease['病害编号'] }} · {{ previewResult.disease['病害类型'] }}
            </p>
            <p>上一条：{{ previewResult.previous?.['病害编号'] ?? '无' }}</p>
            <p>下一条：{{ previewResult.next?.['病害编号'] ?? '无' }}</p>
            <p v-if="previewResult.message" class="hint">{{ previewResult.message }}</p>
            <button v-if="originCursor" class="link" type="button" @click="returnToOrigin">跳回原位置</button>
          </div>
        </form>
      </div>

      <div class="synced-column">
        <article class="sync-card">
          <h3>病害列表（定位同步）</h3>
          <ul>
            <li v-for="disease in locator?.defects ?? []" :key="String(disease.id)" :class="{ located: locator?.currentDiseaseId === disease.id }">
              <strong>{{ disease['病害编号'] }}</strong>
              <span>{{ disease['桩号快照'] || disease['起止桩号'] }} · {{ disease['病害类型'] }}</span>
              <em>{{ disease['同步状态'] || disease['status'] }}</em>
            </li>
            <li v-if="!(locator?.defects ?? []).length" class="empty-inline">现行里程暂无病害</li>
          </ul>
        </article>

        <article class="sync-card">
          <h3>巡查待办与巡查详情</h3>
          <ul>
            <li v-for="patrol in locator?.patrolTodos ?? []" :key="String(patrol.id)">
              <strong>{{ patrol['巡查编号'] }}</strong>
              <span>{{ patrol['巡线结论'] || patrol['发现问题'] }}</span>
              <em>{{ patrol['关联桶位'] || patrol['巡查状态'] }}</em>
            </li>
            <li v-if="!(locator?.patrolTodos ?? []).length" class="empty-inline">定位后生成巡线待办</li>
          </ul>
        </article>

        <article class="sync-card">
          <h3>桥隧限载提示</h3>
          <ul>
            <li v-for="item in locator?.loadRestrictions ?? []" :key="`${item.结构类型}-${item.id}`">
              <strong>{{ item.结构类型 }}：{{ item['桥梁名称'] || item['隧道名称'] }}</strong>
              <span>{{ item['限载提示'] || item['设计荷载'] || item['隧道状态'] }}</span>
              <em>{{ item['关联桶位'] || item['桩号位置'] }}</em>
            </li>
            <li v-if="!(locator?.loadRestrictions ?? []).length" class="empty-inline">同路线暂无桥隧限载记录</li>
          </ul>
        </article>

        <article class="sync-card snapshot-card">
          <h3>旧桩号快照</h3>
          <ul>
            <li v-for="snapshot in locator?.snapshots ?? []" :key="String(snapshot.id)">
              <strong>{{ snapshot['病害编号'] }}</strong>
              <span>{{ snapshot['桩号快照'] || snapshot['起止桩号'] }} · v{{ snapshot['桩号版本'] }}</span>
              <em>{{ snapshot['旧桩号说明'] || '不随现行里程重算' }}</em>
            </li>
          </ul>
        </article>
      </div>
    </section>

    <form class="add-card" @submit.prevent="addDisease">
      <h3>新增现行路线病害（入桶后可撤销）</h3>
      <div class="inline-fields">
        <label><span>病害编号</span><input v-model="newDisease['病害编号']" /></label>
        <label><span>病害类型</span><input v-model="newDisease['病害类型']" /></label>
        <label><span>桩号</span><input v-model="newDisease['桩号']" /></label>
        <label><span>严重程度</span><input v-model="newDisease['严重程度']" /></label>
        <label><span>面积</span><input v-model="newDisease['面积']" /></label>
        <button class="btn primary" type="submit" :disabled="loading">加入索引</button>
      </div>
    </form>

    <table class="data-table ledger-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>台账同步状态</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row['同步状态'] ?? '待巡线' }}</td>
          <td><button class="link" type="button" @click="undoDisease(Number(row.id))">撤销</button></td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条病害台账记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { nextTick, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null | boolean | undefined>
interface Bucket { cursor: string; head: string; end: string; label: string; direction: string; diseaseIds: number[]; defects: Row[] }
interface PreviewResult { disease: Row; bucket?: Bucket | null; previous?: Row | null; next?: Row | null; originBucket?: Bucket | null; stale?: boolean; message?: string }
interface LocatorState {
  road: string
  roadName: string
  stage: string
  version: number
  direction: string
  currentDiseaseId?: number | null
  cursor?: string
  cursorMigrated?: boolean
  buckets: Bucket[]
  defects: Row[]
  snapshots: Row[]
  patrolTodos: Row[]
  loadRestrictions: Row[]
  lastSync?: Record<string, unknown> | null
}

const ENDPOINT = '/api/pavement'
const LOCATOR_ENDPOINT = `${ENDPOINT}/locator`
const road = 'ROAD-0001'
const columns = ['病害编号', '所属路段', '病害类型', '严重程度', '起止桩号', '面积', '发现日期', '病害状态']
const stageLabels = ['已建索引', '已校验桩号', '已切换定位']

const rows = ref<Row[]>([])
const total = ref(0)
const loading = ref(false)
const errorMessage = ref('')
const locator = ref<LocatorState | null>(null)
const activeCursor = ref('')
const originCursor = ref('')
const stationInput = ref('K0+220')
const locateDiseaseId = ref<number | null>(2)
const conclusionInput = ref('')
const bridgeLimitInput = ref('')
const tunnelLimitInput = ref('')
const previewCode = ref('PAVE-0002')
const previewResult = ref<PreviewResult | null>(null)
const realignVersion = ref(1)
const realignStart = ref('K0+000')
const realignEnd = ref('K0+450')
const realignDirection = ref('顺桩')
const newDisease = ref<Record<string, string>>({
  病害编号: 'PAVE-0004',
  病害类型: '龟裂',
  桩号: 'K0+150',
  严重程度: '一般',
  面积: '6㎡',
})

const bucketRailEl = ref<HTMLElement | null>(null)
const bucketRefs: Record<string, HTMLElement | null> = {}

function setBucketRef(element: Element | { $el?: Element } | null, cursor: string) {
  bucketRefs[cursor] = element instanceof HTMLElement ? element : null
}

async function postJson(path: string, payload: Record<string, unknown>): Promise<unknown> {
  const response = await request(path, { method: 'POST', body: JSON.stringify(payload) })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(typeof data.detail === 'string' ? data.detail : '定位器操作被服务端拒绝')
  }
  return data
}

function stateFromResult(result: unknown): LocatorState {
  const state = result as LocatorState
  locator.value = state
  realignVersion.value = state.version
  const nextCursor = state.cursor || (activeCursor.value && state.buckets.some((bucket) => bucket.cursor === activeCursor.value) ? activeCursor.value : state.buckets[0]?.cursor) || ''
  activeCursor.value = nextCursor
  return state
}

async function callLocator(action: () => Promise<unknown>) {
  errorMessage.value = ''
  loading.value = true
  try {
    stateFromResult(await action())
    await Promise.all([reloadLedger(), refreshState()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '定位器操作失败'
  } finally {
    loading.value = false
  }
}

async function refreshState(cursor = activeCursor.value) {
  const query = new URLSearchParams({ road })
  if (cursor) query.set('cursor', cursor)
  const response = await request(`${LOCATOR_ENDPOINT}/state?${query.toString()}`)
  if (!response.ok) throw new Error('定位器状态读取失败')
  const data = (await response.json()) as LocatorState
  locator.value = data
  if (data.version) realignVersion.value = data.version
  if (cursor && data.buckets.some((bucket) => bucket.cursor === cursor)) {
    activeCursor.value = cursor
  } else {
    activeCursor.value = data.buckets[0]?.cursor ?? ''
  }
}

async function buildIndex() {
  await callLocator(() => postJson(`${LOCATOR_ENDPOINT}/build-index`, { road, expectedVersion: locator.value?.version }))
}

async function validateStation() {
  await callLocator(() => postJson(`${LOCATOR_ENDPOINT}/validate-station`, {
    road,
    station: stationInput.value,
    expectedVersion: locator.value?.version,
  }))
}

async function locateDisease() {
  if (locateDiseaseId.value === null) return
  await callLocator(() => postJson(`${LOCATOR_ENDPOINT}/locate`, {
    road,
    diseaseId: locateDiseaseId.value,
    expectedVersion: locator.value?.version,
  }))
}

async function syncConclusion() {
  if (!conclusionInput.value.trim()) {
    errorMessage.value = '请填写巡线结论'
    return
  }
  await callLocator(() => postJson(`${LOCATOR_ENDPOINT}/patrol-conclusion`, {
    road,
    expectedVersion: locator.value?.version,
    conclusion: conclusionInput.value,
    bridgeLimit: bridgeLimitInput.value || undefined,
    tunnelLimit: tunnelLimitInput.value || undefined,
  }))
  conclusionInput.value = ''
  bridgeLimitInput.value = ''
  tunnelLimitInput.value = ''
}

async function realignRoute() {
  await callLocator(() => postJson(`${LOCATOR_ENDPOINT}/realign`, {
    road,
    expectedVersion: realignVersion.value,
    newStart: realignStart.value,
    newEnd: realignEnd.value,
    direction: realignDirection.value,
  }))
}

async function addDisease() {
  await callLocator(async () => {
    const response = await request(`${LOCATOR_ENDPOINT}/defects`, {
      method: 'POST',
      body: JSON.stringify({ expectedVersion: locator.value?.version, values: { ...newDisease.value, 所属路段: road } }),
    })
    const data = await response.json()
    if (!response.ok) throw new Error(data.message ?? '新增病害失败')
    return data.state
  })
}

async function undoDisease(id: number) {
  await callLocator(() => postJson(`${LOCATOR_ENDPOINT}/defects/${id}/undo`, { expectedVersion: locator.value?.version }))
}

async function previewDisease() {
  errorMessage.value = ''
  originCursor.value = activeCursor.value
  try {
    const result = (await postJson(`${LOCATOR_ENDPOINT}/preview`, {
      road,
      diseaseCode: previewCode.value,
      expectedVersion: locator.value?.version,
    })) as PreviewResult
    previewResult.value = result
    if (result.bucket?.cursor) activeCursor.value = result.bucket.cursor
    await nextTick(scrollToActiveBucket)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '相邻病害预览失败'
  }
}

async function returnToOrigin() {
  activeCursor.value = originCursor.value
  await nextTick(scrollToActiveBucket)
}

function selectBucket(cursor: string) {
  activeCursor.value = cursor
  void refreshState(cursor)
}

function onBucketScroll() {
  const rail = bucketRailEl.value
  if (!rail) return
  const railTop = rail.getBoundingClientRect().top
  let visible = activeCursor.value
  for (const bucket of locator.value?.buckets ?? []) {
    const element = bucketRefs[bucket.cursor]
    if (element && element.getBoundingClientRect().top - railTop <= 72) visible = bucket.cursor
  }
  if (visible !== activeCursor.value) {
    activeCursor.value = visible
    void refreshState(visible)
  }
}

function scrollToActiveBucket() {
  bucketRefs[activeCursor.value]?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}

async function reloadLedger() {
  const response = await request(`${ENDPOINT}?size=200`)
  if (!response.ok) throw new Error('病害台账读取失败')
  const payload = await response.json()
  rows.value = payload.items ?? []
  total.value = payload.total ?? rows.value.length
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

onMounted(async () => {
  try {
    await buildIndex()
    await nextTick(scrollToActiveBucket)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '初始化定位器失败'
  }
})
</script>

<style scoped>
.locator-page .locator-panel,
.preview-card,
.sync-card,
.add-card {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 12px;
  margin-bottom: 12px;
}
.panel-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.panel-head h3, .preview-card h3, .sync-card h3, .add-card h3 { margin: 0 0 4px; }
.panel-head p, .hint { margin: 0; color: var(--muted); font-size: 12px; }
.stage-rail { list-style: none; padding: 0; margin: 12px 0; display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
.stage-rail li { border: 1px dashed var(--border); border-radius: 8px; padding: 8px; color: var(--muted); text-align: center; }
.stage-rail li.active { border-style: solid; border-color: var(--brand); color: var(--brand); font-weight: 600; }
.control-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.control-card { border: 1px solid var(--border); border-radius: 8px; padding: 10px; background: #fbfdff; }
.control-card.wide, .realign-card { grid-column: span 2; }
.control-card h4 { margin: 0 0 8px; }
.control-card label, .preview-card label, .add-card label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.control-card input, .control-card select, .preview-card input, .add-card input { margin-bottom: 8px; padding: 6px; border: 1px solid var(--border); border-radius: 6px; }
.inline-fields { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }
.realign-card { margin-top: 10px; }
.workspace-grid { display: grid; grid-template-columns: minmax(260px, 0.8fr) minmax(360px, 1.2fr); gap: 12px; }
.bucket-column { display: flex; flex-direction: column; gap: 12px; }
.bucket-rail { max-height: 360px; overflow-y: auto; display: flex; flex-direction: column; gap: 8px; padding-right: 4px; }
.bucket-card { text-align: left; border: 1px solid var(--border); border-left: 4px solid var(--border); background: #fff; border-radius: 8px; padding: 10px; cursor: pointer; }
.bucket-card strong, .bucket-card span, .bucket-card small { display: block; }
.bucket-card span, .bucket-card small { color: var(--muted); margin-top: 4px; }
.bucket-card.active { border-left-color: var(--brand); box-shadow: 0 0 0 2px rgba(31, 111, 235, 0.12); }
.synced-column { display: flex; flex-direction: column; gap: 12px; }
.sync-card ul { list-style: none; margin: 8px 0 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.sync-card li { display: grid; grid-template-columns: 120px 1fr auto; gap: 8px; align-items: center; font-size: 13px; border-bottom: 1px dashed var(--border); padding-bottom: 6px; }
.sync-card li.located { background: #eef6ff; border-radius: 6px; padding: 6px; }
.sync-card em { color: var(--muted); font-style: normal; font-size: 12px; }
.empty-inline { color: var(--muted); font-size: 12px; }
.stale { color: #b54708; font-weight: 600; }
.ledger-table { margin-top: 12px; }
</style>
