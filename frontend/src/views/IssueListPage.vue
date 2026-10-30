<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter, type LocationQueryRaw } from 'vue-router'
import AlertMessage from '@/components/AlertMessage.vue'
import PageHeading from '@/components/PageHeading.vue'
import { api, type IssueSummary } from '@/api/client'
import { useSessionStore } from '@/stores/session'
import { deadlineLabel, formatDateTime, messageOf, priorityLabel, workflowLabel } from '@/utils'

const route = useRoute()
const router = useRouter()
const session = useSessionStore()
const loading = ref(false)
const error = ref('')
const items = ref<IssueSummary[]>([])
const search = ref('')
const inspecting = ref(false)
const inspectionMessage = ref('')
const inspectionError = ref('')
const inspectionDialog = ref<HTMLDialogElement | null>(null)
const selectedKinds = ref<Array<'consult_queue' | 'dev'>>(['consult_queue', 'dev'])

const state = computed(() => (['mine', 'closed'].includes(String(route.query.state)) ? String(route.query.state) : 'open'))
const priority = computed(() => String(route.query.priority || ''))
const query = computed(() => String(route.query.q || ''))
const tabs = [
  { key: 'open', label: '问题列表' },
  { key: 'mine', label: '我的问题' },
  { key: 'closed', label: '已关闭' },
]
const priorities = [
  { key: '', label: '全部等级' },
  { key: 'normal', label: '普通' },
  { key: 'urgent', label: '紧急' },
  { key: 'severe', label: '严重' },
]

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const page = await api.issues(state.value, priority.value, query.value)
    items.value = page.items
  } catch (caught) {
    error.value = messageOf(caught)
  } finally {
    loading.value = false
  }
}

function setFilter(nextState = state.value, nextPriority = priority.value): void {
  const nextQuery: LocationQueryRaw = { ...route.query, state: nextState }
  if (nextPriority) nextQuery.priority = nextPriority
  else delete nextQuery.priority
  void router.push({ path: '/issues', query: nextQuery })
}

function submitSearch(): void {
  const nextQuery = { ...route.query }
  if (search.value.trim()) nextQuery.q = search.value.trim()
  else delete nextQuery.q
  void router.push({ path: '/issues', query: nextQuery })
}

async function runInspection(): Promise<void> {
  inspecting.value = true
  inspectionMessage.value = ''
  inspectionError.value = ''
  try {
    const result = session.session?.is_global_admin
      ? await api.inspection(selectedKinds.value)
      : await api.inspection()
    inspectionMessage.value = `巡检完成：扫描 ${result.scanned_issues} 个未关闭问题，已加入推送队列 ${result.summary_messages} 条，未解析 ${result.unresolved_targets} 个群聊路由。`
    inspectionDialog.value?.close()
  } catch (caught) {
    inspectionError.value = messageOf(caught)
  } finally {
    inspecting.value = false
  }
}

function openInspection(): void {
  if (session.session?.is_global_admin) inspectionDialog.value?.showModal()
  else void runInspection()
}

watch(
  () => [route.query.state, route.query.priority, route.query.q],
  () => {
    search.value = query.value
    void load()
  },
)
onMounted(() => {
  search.value = query.value
  void load()
})
</script>

<template>
  <PageHeading
    eyebrow="问题中心"
    :title="state === 'mine' ? '我的问题' : state === 'closed' ? '已关闭问题' : '问题列表'"
    description="查看问题进展、处理正式回复，并将问题交接给对应团队。"
  >
    <div class="heading-actions">
      <button v-if="session.session?.can_inspect" class="button button-soft" @click="openInspection">
        立刻巡检
      </button>
      <RouterLink class="button button-primary" to="/issues/new">＋ 新建问题</RouterLink>
    </div>
  </PageHeading>

  <div v-if="inspectionMessage" class="alert alert-success">{{ inspectionMessage }}</div>
  <AlertMessage v-if="inspectionError" :message="inspectionError" />

  <section class="filter-panel">
    <div class="tabs" role="tablist" aria-label="问题范围">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        class="tab"
        :class="{ active: state === tab.key }"
        role="tab"
        :aria-selected="state === tab.key"
        @click="setFilter(tab.key)"
      >
        {{ tab.label }}
      </button>
    </div>
    <form class="search-row" @submit.prevent="submitSearch">
      <label class="sr-only" for="issue-search">搜索问题</label>
      <input id="issue-search" v-model="search" type="search" placeholder="搜索 BUG 编号或客户名称" />
      <button class="button button-soft" type="submit">搜索</button>
      <button v-if="query" class="button button-ghost" type="button" @click="search = ''; submitSearch()">
        清除
      </button>
    </form>
    <div class="filter-label">紧急程度</div>
    <div class="filter-chips">
      <button
        v-for="item in priorities"
        :key="item.key"
        class="chip"
        :class="{ active: priority === item.key }"
        @click="setFilter(state, item.key)"
      >
        {{ item.label }}
      </button>
    </div>
  </section>

  <AlertMessage v-if="error" :message="error" />
  <div v-if="loading" class="loading-card" role="status">正在加载问题…</div>
  <div v-else-if="items.length" class="issue-list">
    <RouterLink
      v-for="item in items"
      :key="item.issue_number"
      class="issue-card"
      :to="`/issues/${encodeURIComponent(item.issue_number)}`"
    >
      <div class="issue-card-top">
        <h2>{{ item.title }}</h2>
        <div class="badge-row">
          <span class="badge" :class="`priority-${item.priority}`">{{ priorityLabel(item.priority) }}</span>
          <span class="badge" :class="`workflow-${item.workflow_status}`">{{ workflowLabel(item.workflow_status) }}</span>
          <span class="badge issue-current">{{ item.issue_current_status }}</span>
          <span v-if="item.deadline_status" class="badge" :class="`deadline-${item.deadline_status}`">
            {{ deadlineLabel(item.deadline_status) }}
          </span>
        </div>
      </div>
      <p class="issue-customer">客户 / 企业：{{ item.customer_name || '未录入' }}</p>
      <div class="issue-card-meta">
        <span class="issue-number">{{ item.bug_number || item.issue_number }}</span>
        <span>截止 {{ formatDateTime(item.deadline) }}</span>
        <span>最近更新 {{ formatDateTime(item.updated_at) }}</span>
      </div>
    </RouterLink>
  </div>
  <section v-else-if="!loading && !error" class="empty-state">
    <div class="empty-icon">✓</div>
    <h2>当前筛选下没有问题</h2>
    <p>试试调整搜索条件，或创建一条新的问题记录。</p>
    <RouterLink class="button button-primary" to="/issues/new">新建问题</RouterLink>
  </section>

  <dialog ref="inspectionDialog" class="modal-card">
    <form method="dialog" class="modal-close-row"><button class="icon-button" aria-label="关闭">×</button></form>
    <div class="eyebrow">定时巡检</div>
    <h2>选择巡检群聊类型</h2>
    <p class="muted">系统会按每个团队设置的巡检规则筛选问题。</p>
    <label class="check-row"><input v-model="selectedKinds" type="checkbox" value="consult_queue" /> 咨询团队</label>
    <label class="check-row"><input v-model="selectedKinds" type="checkbox" value="dev" /> 研发团队</label>
    <div class="modal-actions">
      <button class="button button-soft" @click="inspectionDialog?.close()">取消</button>
      <button class="button button-primary" :disabled="inspecting || !selectedKinds.length" @click="runInspection">
        {{ inspecting ? '正在巡检…' : '开始巡检' }}
      </button>
    </div>
  </dialog>
</template>
