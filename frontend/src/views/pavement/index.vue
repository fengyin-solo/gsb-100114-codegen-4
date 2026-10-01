<template>
  <section class="page locator-page" data-module="pavement">
    <header class="page-head">
      <div>
        <h2>桩号桶定位器</h2>
        <p class="page-desc">
          按道路方向形成连续桩号桶；服务端按“建立桶索引 → 校验桩号 → 切换定位”的状态栅栏顺序放行，
          定位结果同步进入病害列表、巡查详情和桥隧限载提示。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="exportRows">导出台账</button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">现行桶数</span>
        <strong class="stat-value">{{ selectedRoute?.bucket_count ?? buckets.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">现行病害</span>
        <strong class="stat-value">{{ currentRows.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">旧线快照</span>
        <strong class="stat-value">{{ snapshotRows.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">限载/限速提示</span>
        <strong class="stat-value">{{ loadLimits.length }}</strong>
      </article>
    </div>

    <div class="locator-grid">
      <article class="panel protocol-panel">
        <header class="panel-head">
          <h3>定位状态栅栏</h3>
          <span :class="['stage-pill', session?.stage || 'idle']">{{ stageText }}</span>
        </header>

        <div class="route-controls">
          <label>
            <span>道路方向</span>
            <select v-model="selectedRoad" @change="onRouteChange">
              <option v-for="route in routes" :key="route.road" :value="route.road">
                {{ route.road }} · {{ route.direction }}
              </option>
            </select>
          </label>
          <button class="btn primary" type="button" :disabled="!selectedRoad" @click="buildIndex(false)">
            1. 建立桶索引
          </button>
          <button class="btn" type="button" :disabled="!selectedRoute?.indexed" @click="buildIndex(true)">
            重建索引
          </button>
          <button class="btn" type="button" :disabled="!selectedRoute?.indexed" @click="startSession">
            开启巡线
          </button>
        </div>

        <ol class="stage-track">
          <li v-for="stage in stageItems" :key="stage.key" :class="{ active: isStageActive(stage.key), done: isStageDone(stage.key) }">
            <b>{{ stage.index }}</b>
            <span>{{ stage.label }}</span>
          </li>
        </ol>

        <div v-if="selectedRoute" class="route-meta">
          <span>现行版本 v{{ selectedRoute.version }} / 索引 r{{ selectedRoute.index_revision }}</span>
          <span>{{ selectedRoute.start_label }} - {{ selectedRoute.end_label }}</span>
          <span>桶长 {{ selectedRoute.bucket_size }}m</span>
        </div>

        <div class="validate-row">
          <label>
            <span>2. 校验桩号</span>
            <input v-model="stakeInput" placeholder="例如 K0+235" @keyup.enter="validateStake" />
          </label>
          <label>
            <span>同桶病害（可选）</span>
            <select v-model="validateDiseaseId">
              <option :value="null">不指定病害</option>
              <option v-for="row in currentRows" :key="String(row.id)" :value="Number(row.id)">
                {{ row.病害编号 }} · {{ row.起止桩号 }}
              </option>
            </select>
          </label>
          <button class="btn primary" type="button" :disabled="session?.stage !== 'indexed'" @click="validateStake">
            校验桩号
          </button>
          <button class="btn primary" type="button" :disabled="session?.stage !== 'validated'" @click="locate">
            3. 切换定位
          </button>
        </div>

        <div v-if="session" class="session-box">
          <div><b>会话：</b>{{ session.session_id.slice(0, 8) }}…</div>
          <div><b>当前桶：</b>{{ currentBucket?.label || '—' }}</div>
          <div v-if="session.stake"><b>校验桩号：</b>{{ session.stake }}</div>
          <div v-if="session.stage === 'stale'" class="error-text">{{ session.stale_reason }}</div>
        </div>
      </article>

      <article class="panel reroute-panel">
        <header class="panel-head">
          <h3>改线版本栅栏</h3>
          <span>旧桶不能覆盖新路线</span>
        </header>
        <div class="reroute-form">
          <label><span>期望版本</span><input v-model="rerouteForm.expected_version" placeholder="1" /></label>
          <label><span>新起点</span><input v-model="rerouteForm.start" placeholder="K0+000" /></label>
          <label><span>新终点</span><input v-model="rerouteForm.end" placeholder="K2+200" /></label>
          <label><span>桶长</span><input v-model="rerouteForm.bucket_size" placeholder="100" /></label>
          <label class="wide"><span>道路方向</span><input v-model="rerouteForm.direction" placeholder="由西向东（桩号递增）" /></label>
          <button class="btn" type="button" :disabled="!selectedRoad" @click="reroute">提交改线</button>
        </div>
        <p class="hint">改线后旧病害仍保留原桩号快照，只有新增/现行版本病害进入新桶。</p>
      </article>
    </div>

    <article class="panel bucket-panel">
      <header class="panel-head">
        <div>
          <h3>连续桶巡线</h3>
          <p>横向滚过桶头即预览该桶第一条病害的相邻记录；定位后可一键跳回原位置。</p>
        </div>
        <button class="btn" type="button" :disabled="session?.stage !== 'located'" @click="returnToOrigin">
          跳回原位置 {{ originBucket?.label || '' }}
        </button>
      </header>
      <div ref="bucketRailRef" class="bucket-rail">
        <button
          v-for="bucket in buckets"
          :key="bucket.cursor"
          :ref="el => setBucketRef(el, bucket.index)"
          :data-index="bucket.index"
          class="bucket-card"
          :class="{ current: bucket.index === session?.current_bucket, origin: bucket.index === session?.origin_bucket, previewed: bucket.index === preview?.bucket_index }"
          type="button"
          @click="previewBucket(bucket)"
        >
          <span class="bucket-head">#{{ bucket.index }}</span>
          <strong>{{ bucket.start }}</strong>
          <small>{{ bucket.disease_count }} 条病害</small>
        </button>
      </div>
    </article>

    <div v-if="preview" class="preview-strip panel">
      <header class="panel-head">
        <h3>相邻病害编号预览 · {{ preview.bucket_label }}</h3>
        <span>预览不改变定位桶</span>
      </header>
      <div class="neighbor-grid">
        <div class="neighbor-card">
          <span>上一条</span>
          <strong>{{ preview.previous?.病害编号 || '—' }}</strong>
          <small>{{ preview.previous?.桩号 || '已是桶序起点' }}</small>
        </div>
        <div class="neighbor-card current-neighbor">
          <span>当前</span>
          <strong>{{ preview.current?.病害编号 || '—' }}</strong>
          <small>{{ preview.current?.桩号 }} · {{ preview.current?.病害类型 }}</small>
        </div>
        <div class="neighbor-card">
          <span>下一条</span>
          <strong>{{ preview.next?.病害编号 || '—' }}</strong>
          <small>{{ preview.next?.桩号 || '已是桶序终点' }}</small>
        </div>
      </div>
    </div>

    <div class="content-grid">
      <article class="panel disease-panel">
        <header class="panel-head">
          <h3>病害台账 / 定位列表</h3>
          <span>{{ tableRows.length }} 条进入当前列表</span>
        </header>
        <form class="create-form" @submit.prevent="createDisease">
          <input v-model="createForm.病害编号" placeholder="病害编号 PAVE-0005" />
          <input v-model="createForm.病害类型" placeholder="病害类型" />
          <input v-model="createForm.严重程度" placeholder="严重程度" />
          <input v-model="createForm.起止桩号" placeholder="起止桩号 K0+300" />
          <input v-model="createForm.面积" placeholder="面积" />
          <button class="btn primary" type="submit">新增病害</button>
        </form>
        <table class="data-table">
          <thead>
            <tr>
              <th>病害编号</th><th>类型</th><th>程度</th><th>桩号</th><th>面积</th><th>状态</th><th>版本</th><th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in tableRows" :key="String(row.id)" :class="{ located: Number(row.id) === Number(session?.disease_id) }">
              <td>{{ row.病害编号 }}</td>
              <td>{{ row.病害类型 }}</td>
              <td>{{ row.严重程度 }}</td>
              <td>{{ row.起止桩号 }}</td>
              <td>{{ row.面积 || '—' }}</td>
              <td>{{ row.status }}</td>
              <td>{{ row.现行体系状态 || '未建索引' }}</td>
              <td class="row-actions">
                <button class="link" type="button" @click="runAction('派发修复', row)">派发</button>
                <button class="link danger" type="button" @click="removeDisease(row)">撤销</button>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-if="snapshotRows.length" class="snapshot-box">
          <b>旧桩号快照：</b>
          <span v-for="row in snapshotRows" :key="String(row.id)">{{ row.病害编号 }}（{{ row.起止桩号 }}） </span>
        </div>
      </article>

      <div class="side-panels">
        <article class="panel">
          <header class="panel-head"><h3>巡查详情</h3><span>{{ patrols.length }} 条</span></header>
          <div v-for="patrol in patrols" :key="String(patrol.id)" class="list-card">
            <strong>{{ patrol.巡查编号 }} · {{ patrol.status }}</strong>
            <p>{{ patrol.发现问题 }}</p>
            <small>{{ patrol.巡查日期 }} · {{ patrol.巡查人员 }} · {{ patrol.桩号桶巡线 || patrol.巡查路段 }}</small>
          </div>
          <p v-if="!patrols.length" class="hint">定位后这里显示同路或同病害巡查待办。</p>
        </article>

        <article class="panel">
          <header class="panel-head"><h3>桥隧限载提示</h3><span>{{ loadLimits.length }} 条</span></header>
          <div v-for="item in loadLimits" :key="`${item.kind}-${item.id}`" class="list-card warning">
            <strong>{{ item.kind }} · {{ item.名称 }} · {{ item.状态 }}</strong>
            <p>{{ item.提示 || item.限制 }}</p>
            <small>{{ item.编号 }} · {{ item.关联桩号 }}</small>
          </div>
          <p v-if="!loadLimits.length" class="hint">定位后这里显示桥梁限载和隧道限速/限高提示。</p>
        </article>

        <article class="panel conclusion-panel">
          <header class="panel-head"><h3>巡线结论同步</h3></header>
          <textarea v-model="conclusionForm.conclusion" placeholder="定位后填写巡线结论，将同步到巡查待办、桥隧限载清单和病害台账"></textarea>
          <div class="conclusion-row">
            <select v-model="conclusionForm.target_type">
              <option value="">不同步桥隧对象</option>
              <option value="bridge_info">桥梁档案</option>
              <option value="tunnel">隧道管养</option>
            </select>
            <input v-model="conclusionForm.target_id" placeholder="桥/隧 ID" />
            <button class="btn primary" type="button" :disabled="session?.stage !== 'located'" @click="conclude">
              同步结论
            </button>
          </div>
        </article>
      </div>
    </div>

    <footer class="page-foot">
      <span>共 {{ ledgerRows.length }} 条病害台账；状态跳级、倒序切换和旧版本改线均由服务端拒绝。</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      <span v-else-if="successMessage" class="success-text">{{ successMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Json = Record<string, string | number | boolean | null>
type Route = Json & {
  road: string
  direction: string
  version: number
  index_revision: number
  bucket_count: number
  indexed?: boolean
  start_label?: string
  end_label?: string
  bucket_size?: number
}
type Bucket = {
  index: number
  start: string
  end: string
  label: string
  cursor: string
  disease_count: number
  diseases: Array<{ id: number; 病害编号: string; 病害类型: string; 桩号: string }>
}
type Session = Json & {
  session_id: string
  road: string
  stage: 'indexed' | 'validated' | 'located' | 'stale'
  current_bucket: number
  origin_bucket: number
  disease_id: number | null
  stake?: string
  stale_reason?: string
}
type Disease = Json
type Patrol = Json
type LoadLimit = Json
type PositionPayload = {
  session: Session
  bucket: Bucket
  disease: Disease | null
  disease_list: Disease[]
  patrols: Patrol[]
  load_limits: LoadLimit[]
}
type NeighborRecord = {
  id?: number
  病害编号?: string
  桩号?: string
  病害类型?: string
}
type NeighborResult = {
  previous: NeighborRecord | null
  current: NeighborRecord
  next: NeighborRecord | null
}
type Preview = {
  bucket_index: number
  bucket_label: string
  previous: NeighborRecord | null
  current: NeighborRecord
  next: NeighborRecord | null
}

const routes = ref<Route[]>([])
const selectedRoad = ref('')
const buckets = ref<Bucket[]>([])
const session = ref<Session | null>(null)
const currentBucket = ref<Bucket | null>(null)
const ledgerRows = ref<Disease[]>([])
const locatedRows = ref<Disease[] | null>(null)
const patrols = ref<Patrol[]>([])
const loadLimits = ref<LoadLimit[]>([])
const preview = ref<Preview | null>(null)
const errorMessage = ref('')
const successMessage = ref('')
const stakeInput = ref('K0+235')
const validateDiseaseId = ref<number | null>(null)
const bucketRailRef = ref<HTMLElement | null>(null)
const bucketRefs = new Map<number, HTMLElement>()
let observer: IntersectionObserver | null = null
let suppressPreviewUntil = 0

const createForm = ref<Record<string, string>>({
  病害编号: '',
  病害类型: '',
  严重程度: '',
  起止桩号: '',
  面积: '',
})
const rerouteForm = ref({ expected_version: '1', start: 'K0+000', end: 'K2+200', bucket_size: '100', direction: '由西向东（桩号递增）' })
const conclusionForm = ref<{ conclusion: string; target_type: string; target_id: string }>({
  conclusion: '',
  target_type: '',
  target_id: '',
})

const stageItems = [
  { key: 'indexed', index: 1, label: '桶索引已建立' },
  { key: 'validated', index: 2, label: '桩号已校验' },
  { key: 'located', index: 3, label: '定位已切换' },
]

const selectedRoute = computed<Route | null>(() => routes.value.find(item => item.road === selectedRoad.value) || null)
const currentIds = computed(() => new Set(buckets.value.flatMap(bucket => bucket.diseases.map(item => item.id))))
const currentRows = computed(() => ledgerRows.value.filter(row => currentIds.value.has(Number(row.id))))
const snapshotRows = computed(() => ledgerRows.value.filter(row => !currentIds.value.has(Number(row.id))))
const tableRows = computed(() => locatedRows.value ?? currentRows.value)
const originBucket = computed(() => buckets.value.find(bucket => bucket.index === session.value?.origin_bucket) || null)
const stageText = computed(() => {
  const map: Record<string, string> = {
    indexed: '① 桶索引已建立',
    validated: '② 桩号已校验',
    located: '③ 定位已切换',
    stale: '版本已过期',
  }
  return session.value ? map[session.value.stage] || '未开始' : '未开始'
})

function isStageActive(key: string) {
  return session.value?.stage === key
}
function isStageDone(key: string) {
  const order = ['indexed', 'validated', 'located']
  return session.value ? order.indexOf(session.value.stage) > order.indexOf(key) : false
}

async function apiJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await request(path, init)
  if (!response.ok) {
    let detail = `接口返回 ${response.status}`
    try {
      const payload = await response.json()
      detail = typeof payload.detail === 'string' ? payload.detail : detail
    } catch {
      // 保留默认 HTTP 错误说明
    }
    throw new Error(detail)
  }
  return response.json() as Promise<T>
}

function notice(message: string, ok = false) {
  errorMessage.value = ok ? '' : message
  successMessage.value = ok ? message : ''
}

async function loadRoutes(preferRoad = '') {
  const payload = await apiJson<{ items: Route[] }>('/api/stake-buckets/routes')
  routes.value = payload.items
  if (preferRoad && routes.value.some(item => item.road === preferRoad)) selectedRoad.value = preferRoad
  else if (!selectedRoad.value && routes.value.length) selectedRoad.value = routes.value[0].road
  if (selectedRoute.value) {
    rerouteForm.value.expected_version = String(selectedRoute.value.version)
  }
}

async function loadBuckets() {
  if (!selectedRoad.value) return
  const cursor = session.value?.cursor ? String(session.value.cursor) : undefined
  const payload = await apiJson<{ route: Route; buckets: Bucket[] }>(`/api/stake-buckets/routes/${encodeURIComponent(selectedRoad.value)}/buckets${cursor ? `?cursor=${cursor}` : ''}`)
  routes.value = routes.value.map(route => route.road === payload.route.road ? payload.route : route)
  buckets.value = payload.buckets
  currentBucket.value = payload.buckets.find(item => item.index === session.value?.current_bucket) || null
  await loadLedger()
}

async function loadLedger() {
  if (!selectedRoad.value) return
  const query = new URLSearchParams({ road: selectedRoad.value, size: '200' })
  const payload = await apiJson<{ items: Disease[] }>(`/api/pavement?${query.toString()}`)
  ledgerRows.value = payload.items
}

async function onRouteChange() {
  session.value = null
  locatedRows.value = null
  patrols.value = []
  loadLimits.value = []
  preview.value = null
  if (selectedRoute.value) {
    rerouteForm.value.expected_version = String(selectedRoute.value.version)
    if (selectedRoute.value.indexed) await loadBuckets()
    else buckets.value = []
  }
}

async function buildIndex(rebuild: boolean) {
  if (!selectedRoad.value) return
  try {
    const path = rebuild ? '/api/stake-buckets/rebuild-index' : '/api/stake-buckets/indexes'
    const body: Record<string, unknown> = { road: selectedRoad.value }
    const payload = await apiJson<{ route: Route; buckets: Bucket[] }>(path, {
      method: 'POST',
      body: JSON.stringify(body),
    })
    routes.value = routes.value.map(route => route.road === payload.route.road ? payload.route : route)
    buckets.value = payload.buckets
    rerouteForm.value.expected_version = String(payload.route.version)
    await startSession()
    notice(rebuild ? '索引已重建，既有浏览游标已兼容到新桶' : '桶索引已建立，可以校验桩号', true)
  } catch (error) {
    notice(error instanceof Error ? error.message : '桶索引建立失败')
  }
}

async function startSession() {
  if (!selectedRoad.value) return
  try {
    const payload = await apiJson<{ session: Session; route: Route }>(`/api/stake-buckets/routes/${encodeURIComponent(selectedRoad.value)}/sessions`, {
      method: 'POST',
      body: JSON.stringify({ cursor: session.value?.cursor || buckets.value[Number(session.value?.origin_bucket ?? 0)]?.cursor }),
    })
    session.value = payload.session
    currentBucket.value = buckets.value.find(item => item.index === payload.session.current_bucket) || null
    locatedRows.value = null
    patrols.value = []
    loadLimits.value = []
    preview.value = null
    notice('新巡线会话已建立，请先校验桩号', true)
  } catch (error) {
    notice(error instanceof Error ? error.message : '开启巡线失败')
  }
}

async function validateStake() {
  if (!session.value) return
  try {
    const payload = await apiJson<{ session: Session; bucket: Bucket }>(`/api/stake-buckets/sessions/${session.value.session_id}/validate-stake`, {
      method: 'POST',
      body: JSON.stringify({ stake: stakeInput.value, disease_id: validateDiseaseId.value }),
    })
    session.value = payload.session
    currentBucket.value = payload.bucket
    await scrollToBucket(payload.bucket.index)
    notice('桩号已校验，可以切换定位', true)
  } catch (error) {
    notice(error instanceof Error ? error.message : '桩号校验失败')
  }
}

async function locate() {
  if (!session.value) return
  try {
    const payload = await apiJson<PositionPayload>(`/api/stake-buckets/sessions/${session.value.session_id}/locate`, {
      method: 'POST',
      body: JSON.stringify({ disease_id: validateDiseaseId.value, origin_bucket: session.value.origin_bucket }),
    })
    applyPosition(payload)
    await scrollToBucket(payload.bucket.index)
    if (payload.disease) await previewDisease(Number(payload.disease.id), payload.bucket)
    notice('定位已切换，病害列表、巡查详情和桥隧限载提示已联动', true)
  } catch (error) {
    notice(error instanceof Error ? error.message : '定位切换失败')
  }
}

function applyPosition(payload: PositionPayload) {
  session.value = payload.session
  currentBucket.value = payload.bucket
  locatedRows.value = payload.disease_list
  patrols.value = payload.patrols
  loadLimits.value = payload.load_limits
  ledgerRows.value = mergeRows(ledgerRows.value, payload.disease_list)
}

async function returnToOrigin() {
  if (!session.value) return
  try {
    const payload = await apiJson<{ session: Session; bucket: Bucket }>(`/api/stake-buckets/sessions/${session.value.session_id}/return`, { method: 'POST' })
    session.value = payload.session
    currentBucket.value = payload.bucket
    await scrollToBucket(payload.bucket.index)
    await previewBucket(payload.bucket)
    notice('已跳回原位置', true)
  } catch (error) {
    notice(error instanceof Error ? error.message : '跳回失败')
  }
}

async function previewBucket(bucket: Bucket) {
  if (!session.value || !bucket.diseases.length) {
    preview.value = {
      bucket_index: bucket.index,
      bucket_label: bucket.label,
      previous: null,
      current: { 病害编号: '空桶', 桩号: bucket.start, 病害类型: '该桶暂无现行病害' },
      next: null,
    }
    return
  }
  await previewDisease(bucket.diseases[0].id, bucket)
}

async function previewDisease(diseaseId: number, bucket?: Bucket) {
  if (!session.value) return
  try {
    const targetBucket = bucket || buckets.value.find(item => item.diseases.some(disease => disease.id === diseaseId))
    const payload = await apiJson<NeighborResult>(
      `/api/stake-buckets/sessions/${session.value.session_id}/diseases/${diseaseId}/neighbors`,
    )
    preview.value = {
      bucket_index: targetBucket?.index ?? Number(session.value.current_bucket),
      bucket_label: targetBucket?.label || currentBucket.value?.label || '',
      previous: (payload.previous as Preview['previous']) || null,
      current: payload.current as Preview['current'],
      next: (payload.next as Preview['next']) || null,
    }
  } catch (error) {
    notice(error instanceof Error ? error.message : '相邻病害预览失败')
  }
}

async function scrollToBucket(index: number) {
  await nextTick()
  suppressPreviewUntil = Date.now() + 500
  bucketRefs.get(index)?.scrollIntoView({ behavior: 'smooth', inline: 'center', block: 'nearest' })
}

function setBucketRef(element: Element | unknown, index: number) {
  if (element instanceof HTMLElement) {
    bucketRefs.set(index, element)
    observer?.observe(element)
  } else {
    bucketRefs.delete(index)
  }
}

function setupObserver() {
  if (!bucketRailRef.value) return
  observer = new IntersectionObserver((entries) => {
    if (Date.now() < suppressPreviewUntil || session.value?.stage !== 'located') return
    const visible = entries
      .filter(entry => entry.isIntersecting)
      .sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0]
    if (!visible) return
    const index = Number((visible.target as HTMLElement).dataset.index)
    const bucket = buckets.value[index]
    if (bucket && preview.value?.bucket_index !== index) void previewBucket(bucket)
  }, { root: bucketRailRef.value, threshold: [0.65, 0.9] })
}

async function reroute() {
  if (!selectedRoad.value) return
  try {
    const payload = await apiJson<{ route: Route; buckets: Bucket[] }>(`/api/stake-buckets/reroutes?road=${encodeURIComponent(selectedRoad.value)}`, {
      method: 'POST',
      body: JSON.stringify({
        expected_version: Number(rerouteForm.value.expected_version),
        start: rerouteForm.value.start,
        end: rerouteForm.value.end,
        direction: rerouteForm.value.direction,
        bucket_size: Number(rerouteForm.value.bucket_size),
      }),
    })
    routes.value = routes.value.map(route => route.road === payload.route.road ? payload.route : route)
    buckets.value = payload.buckets
    rerouteForm.value.expected_version = String(payload.route.version)
    session.value = null
    locatedRows.value = null
    patrols.value = []
    loadLimits.value = []
    preview.value = null
    await loadLedger()
    notice('改线成功：请基于新版本重新开启巡线；旧病害已保留桩号快照', true)
  } catch (error) {
    notice(error instanceof Error ? error.message : '改线被拒绝')
  }
}

async function createDisease() {
  try {
    const result = await apiJson<{ ok: boolean; message?: string; entry: Disease }>('/api/pavement', {
      method: 'POST',
      body: JSON.stringify({
        values: { ...createForm.value, 所属路段: selectedRoad.value, 发现日期: new Date().toISOString().slice(0, 10) },
      }),
    })
    if (result.ok === false) throw new Error(result.message || '新增病害失败')
    ledgerRows.value = mergeRows(ledgerRows.value, [result.entry])
    await loadBuckets()
    createForm.value = { 病害编号: '', 病害类型: '', 严重程度: '', 起止桩号: '', 面积: '' }
    notice('病害已新增并进入现行桶，未产生重复项', true)
  } catch (error) {
    notice(error instanceof Error ? error.message : '新增病害失败')
  }
}

async function removeDisease(row: Disease) {
  try {
    const result = await apiJson<{ ok: boolean; message?: string }>(`/api/pavement/${row.id}`, { method: 'DELETE' })
    if (result.ok === false) throw new Error(result.message || '撤销病害失败')
    ledgerRows.value = ledgerRows.value.filter(item => item.id !== row.id)
    locatedRows.value = locatedRows.value?.filter(item => item.id !== row.id) || null
    await loadBuckets()
    if (session.value?.disease_id === row.id) await startSession()
    notice('病害已撤销，桶索引浏览结果同步刷新', true)
  } catch (error) {
    notice(error instanceof Error ? error.message : '撤销病害失败')
  }
}

async function runAction(action: string, row: Disease) {
  try {
    const result = await apiJson<{ ok: boolean; message?: string; entry: Disease }>(`/api/pavement/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (result.ok === false) throw new Error(result.message || '病害动作失败')
    ledgerRows.value = mergeRows(ledgerRows.value, [result.entry])
    locatedRows.value = locatedRows.value ? mergeRows(locatedRows.value, [result.entry]) : locatedRows.value
    notice(`病害已${action}`, true)
  } catch (error) {
    notice(error instanceof Error ? error.message : '病害动作失败')
  }
}

async function conclude() {
  if (!session.value) return
  try {
    const payload = await apiJson<{
      message: string
      disease: Disease
      patrol: Patrol
      load_limit: LoadLimit | null
    }>(`/api/stake-buckets/sessions/${session.value.session_id}/conclusion`, {
      method: 'POST',
      body: JSON.stringify({
        conclusion: conclusionForm.value.conclusion,
        target_type: conclusionForm.value.target_type || null,
        target_id: conclusionForm.value.target_id ? Number(conclusionForm.value.target_id) : null,
      }),
    })
    ledgerRows.value = mergeRows(ledgerRows.value, [payload.disease])
    locatedRows.value = locatedRows.value ? mergeRows(locatedRows.value, [payload.disease]) : locatedRows.value
    patrols.value = [payload.patrol, ...patrols.value]
    if (payload.load_limit) loadLimits.value = [payload.load_limit, ...loadLimits.value]
    conclusionForm.value.conclusion = ''
    notice(payload.message, true)
  } catch (error) {
    notice(error instanceof Error ? error.message : '巡线结论同步失败')
  }
}

function mergeRows(current: Disease[], incoming: Disease[]) {
  const map = new Map(current.map(row => [Number(row.id), row]))
  incoming.forEach(row => map.set(Number(row.id), row))
  return Array.from(map.values()).sort((a, b) => Number(a.id) - Number(b.id))
}

function exportRows() {
  window.open('/api/pavement/export', '_blank')
}

onMounted(async () => {
  try {
    await loadRoutes()
    if (selectedRoute.value?.indexed) await loadBuckets()
    await nextTick()
    setupObserver()
  } catch (error) {
    notice(error instanceof Error ? error.message : '桩号桶定位器初始化失败')
  }
})

onBeforeUnmount(() => observer?.disconnect())
</script>

<style scoped>
.locator-grid,
.content-grid {
  display: grid;
  grid-template-columns: minmax(0, 2fr) minmax(280px, 1fr);
  gap: 12px;
  margin-bottom: 12px;
}
.panel {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
}
.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
}
.panel-head h3 { margin: 0; font-size: 15px; }
.panel-head p { margin: 4px 0 0; color: var(--muted); font-size: 12px; }
.route-controls,
.validate-row,
.reroute-form {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: flex-end;
}
.route-controls label,
.validate-row label,
.reroute-form label,
.create-form input {
  font-size: 12px;
}
.route-controls label span,
.validate-row span,
.reroute-form span {
  display: block;
  color: var(--muted);
  margin-bottom: 3px;
}
select,
input,
textarea {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 8px;
  font: inherit;
}
.stage-track {
  list-style: none;
  padding: 0;
  margin: 12px 0;
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
}
.stage-track li {
  border: 1px dashed var(--border);
  border-radius: 6px;
  padding: 8px;
  color: var(--muted);
  font-size: 12px;
}
.stage-track li b {
  display: inline-grid;
  place-items: center;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background: #e2e8f0;
  margin-right: 6px;
}
.stage-track li.active,
.stage-track li.done {
  color: #1f2937;
  border-color: var(--brand);
  background: #eff6ff;
}
.stage-track li.active b,
.stage-track li.done b { background: var(--brand); color: #fff; }
.stage-pill {
  padding: 4px 8px;
  border-radius: 999px;
  background: #e2e8f0;
  font-size: 12px;
}
.stage-pill.located { background: #dcfce7; color: #166534; }
.stage-pill.validated { background: #fef9c3; color: #854d0e; }
.stage-pill.stale { background: #fee2e2; color: #991b1b; }
.route-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  color: var(--muted);
  font-size: 12px;
}
.session-box {
  margin-top: 10px;
  padding: 8px;
  border-radius: 6px;
  background: #f8fafc;
  font-size: 12px;
  display: grid;
  gap: 4px;
}
.reroute-form label { flex: 1 1 110px; }
.reroute-form .wide { flex-basis: 100%; }
.reroute-form input,
.reroute-form select { width: 100%; }
.hint { color: var(--muted); font-size: 12px; }
.bucket-panel { margin-bottom: 12px; }
.bucket-rail {
  display: flex;
  gap: 8px;
  overflow-x: auto;
  padding: 8px 2px 12px;
}
.bucket-card {
  min-width: 118px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: #f8fafc;
  padding: 10px;
  text-align: left;
  cursor: pointer;
}
.bucket-head {
  display: block;
  color: var(--muted);
  font-size: 12px;
  margin-bottom: 6px;
}
.bucket-card strong,
.bucket-card small { display: block; }
.bucket-card.current { border-color: var(--brand); box-shadow: 0 0 0 2px rgba(31, 111, 235, .15); }
.bucket-card.origin { background: #ecfdf5; }
.bucket-card.previewed { outline: 2px solid #f59e0b; }
.preview-strip { margin-bottom: 12px; }
.neighbor-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
}
.neighbor-card {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 10px;
  background: #f8fafc;
}
.neighbor-card span,
.neighbor-card small { display: block; color: var(--muted); }
.neighbor-card strong { display: block; margin: 4px 0; }
.current-neighbor { background: #eff6ff; border-color: var(--brand); }
.disease-panel { min-width: 0; }
.create-form {
  display: grid;
  grid-template-columns: repeat(4, minmax(90px, 1fr)) auto;
  gap: 8px;
  margin-bottom: 10px;
}
.create-form input { min-width: 0; }
.data-table tr.located { background: #eff6ff; }
.danger { color: #b42318; }
.snapshot-box {
  margin-top: 10px;
  padding: 8px;
  border-radius: 6px;
  background: #fffbeb;
  color: #92400e;
  font-size: 12px;
}
.side-panels {
  display: grid;
  gap: 12px;
  align-content: start;
}
.list-card {
  border-left: 3px solid var(--brand);
  padding: 8px 10px;
  background: #f8fafc;
  border-radius: 4px;
  margin-bottom: 8px;
}
.list-card.warning { border-color: #d97706; background: #fffbeb; }
.list-card p { margin: 4px 0; font-size: 13px; }
.list-card small { color: var(--muted); }
.conclusion-panel textarea {
  width: 100%;
  min-height: 82px;
  resize: vertical;
}
.conclusion-row {
  display: grid;
  grid-template-columns: 1fr 90px auto;
  gap: 8px;
  margin-top: 8px;
}
.success-text { color: #166534; }
@media (max-width: 1100px) {
  .locator-grid,
  .content-grid { grid-template-columns: 1fr; }
  .create-form { grid-template-columns: 1fr 1fr; }
}
</style>
