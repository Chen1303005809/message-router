<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import AlertMessage from '@/components/AlertMessage.vue'
import PageHeading from '@/components/PageHeading.vue'
import { api, type StatusQueue } from '@/api/client'
import { useSessionStore } from '@/stores/session'
import { formatDateTime, messageOf, workflowLabel } from '@/utils'

const session = useSessionStore()
const data = ref<StatusQueue | null>(null)
const loading = ref(true)
const error = ref('')
let timer: number | undefined

async function load(quiet = false): Promise<void> {
  if (!quiet) loading.value = true
  try {
    data.value = await api.statusQueue()
    error.value = ''
  } catch (caught) {
    error.value = messageOf(caught)
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  void load()
  timer = window.setInterval(() => void load(true), 5000)
})
onUnmounted(() => {
  if (timer) window.clearInterval(timer)
})
</script>

<template>
  <PageHeading
    eyebrow="问题工作台 / 后台任务"
    title="异步任务队列"
    description="Agent 草稿整理和问题状态判定任务每 5 秒刷新一次。"
  >
    <RouterLink class="button button-soft" to="/issues">问题工作台</RouterLink>
  </PageHeading>
  <AlertMessage v-if="error" :message="error" />
  <div v-if="loading" class="loading-card">正在加载任务…</div>
  <template v-else-if="data">
    <section class="panel queue-section">
      <div class="section-heading"><h2>当前任务</h2><span>最多显示 100 条</span></div>
      <div v-if="!data.items.length && !data.draft_tasks.length" class="empty-inline">当前没有排队或执行中的任务。</div>
      <div v-for="item in data.draft_tasks" :key="item.id" class="queue-row">
        <span class="queue-dot processing" />
        <div class="queue-row-main"><strong>{{ item.title }}</strong><small>Agent 整理草稿 · {{ formatDateTime(item.started_at) }}</small></div>
        <span class="badge workflow-pending">处理中</span>
      </div>
      <div v-for="item in data.items" :key="item.id" class="queue-row">
        <span class="queue-dot" :class="`queue-${item.state}`" />
        <div class="queue-row-main">
          <RouterLink :to="`/issues/${encodeURIComponent(item.issue_number)}`">
            <strong>{{ item.title }}</strong>
          </RouterLink>
          <small>{{ item.bug_number || item.issue_number }} · 第 {{ item.entry_sequence }} 条 · {{ item.attempts }} 次尝试</small>
        </div>
        <div class="queue-timing"><span>{{ item.state }}</span><small>下次尝试 {{ formatDateTime(item.next_attempt_at) }}</small></div>
      </div>
    </section>

    <section class="panel queue-section">
      <div class="section-heading"><h2>最近状态变化</h2><span>最多 50 条</span></div>
      <div v-if="!data.recent_status_changes.length" class="empty-inline">暂无状态变化记录。</div>
      <div v-for="(item, index) in data.recent_status_changes" :key="`${item.issue_number}-${item.created_at}-${index}`" class="history-row">
        <div class="history-main">
          <RouterLink :to="`/issues/${encodeURIComponent(item.issue_number)}`"><strong>{{ item.title }}</strong></RouterLink>
          <small>{{ item.bug_number || item.issue_number }} · {{ item.field }} · {{ item.actor_name }}</small>
        </div>
        <div class="history-change"><span>{{ item.from_status || '—' }}</span><b>→</b><span>{{ item.to_status || workflowLabel(item.to_status) }}</span><small>{{ formatDateTime(item.created_at) }}</small></div>
      </div>
    </section>

    <section v-if="session.session?.is_global_admin" class="panel queue-section">
      <div class="section-heading"><h2>最近删除记录</h2><span>最多 50 条</span></div>
      <div v-if="!data.recent_deletions.length" class="empty-inline">暂无删除记录。</div>
      <div v-for="item in data.recent_deletions" :key="item.id" class="history-row">
        <div class="history-main"><strong>{{ item.target_label }}</strong><small>{{ item.target_kind }} · {{ item.actor_name }}</small></div>
        <div class="history-change"><span>{{ item.outcome }}</span><small>{{ formatDateTime(item.created_at) }}</small></div>
        <p v-if="item.error_message" class="history-error">{{ item.error_message }}</p>
      </div>
    </section>
  </template>
</template>
