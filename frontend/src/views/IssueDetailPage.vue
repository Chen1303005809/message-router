<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import AlertMessage from '@/components/AlertMessage.vue'
import PageHeading from '@/components/PageHeading.vue'
import { api, type IssueDetail } from '@/api/client'
import { useSessionStore } from '@/stores/session'
import {
  dateTimeApiValue,
  dateTimeLocal,
  deadlineLabel,
  formatDateTime,
  issueStatusOptions,
  mediaPath,
  messageOf,
  priorityLabel,
  workflowLabel,
} from '@/utils'

const route = useRoute()
const session = useSessionStore()
const issue = ref<IssueDetail | null>(null)
const loading = ref(true)
const saving = ref(false)
const error = ref('')
const notice = ref('')
const metadataOpen = ref(false)
const messageText = ref('')
const messageIntent = ref<'handoff' | 'sync'>('handoff')
const messageSide = ref<'' | 'consult' | 'dev'>('')
const currentStatus = ref('')
const workflowStatus = ref<'pending_confirmation' | 'waiting_customer' | 'in_progress' | 'suspended'>('in_progress')
const metadata = reactive({
  customer_name: '',
  customer_group_name: '',
  customer_contact_name: '',
  customer_contact_method: '',
  priority: 'normal' as 'normal' | 'urgent' | 'severe',
})
const transferConsultantId = ref('')
const transferDevTeamId = ref('')
const deadlineValue = ref('')
const deadlineAdjustmentHours = ref('0.5')
const approachingHours = ref('24')

const issueNumber = computed(() => String(route.params.issueNumber))
const canManage = computed(() => issue.value?.can_change_consult_status ?? false)
const isGlobalAdmin = computed(() => session.session?.is_global_admin ?? false)
const statusOptions = issueStatusOptions()
const workflowOptions = [
  { value: 'pending_confirmation', label: '待确认' },
  { value: 'in_progress', label: '处理中' },
  { value: 'waiting_customer', label: '等待客户' },
  { value: 'suspended', label: '已暂停' },
]

function syncFormValues(data: IssueDetail): void {
  currentStatus.value = data.issue_current_status
  workflowStatus.value = data.status === 'closed' ? 'in_progress' : data.status as typeof workflowStatus.value
  metadata.customer_name = data.customer_name
  metadata.customer_group_name = data.customer_group_name
  metadata.customer_contact_name = data.customer_contact_name || ''
  metadata.customer_contact_method = data.customer_contact_method || ''
  metadata.priority = data.priority as typeof metadata.priority
  deadlineValue.value = dateTimeLocal(data.deadline)
  approachingHours.value = String(Math.max(data.approaching_window_minutes / 60, 0.5))
}

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    let data = await api.issue(issueNumber.value)
    issue.value = data
    syncFormValues(data)
    const canAutoAccept =
      data.lifecycle_status === 'open' &&
      ['pending_confirmation', 'waiting_customer', 'suspended'].includes(data.status) &&
      (data.status !== 'pending_confirmation' || data.can_accept_pending)
    if (canAutoAccept) {
      await api.acceptIssue(data.issue_number, data.version)
      data = await api.issue(issueNumber.value)
      issue.value = data
      syncFormValues(data)
    }
  } catch (caught) {
    error.value = messageOf(caught)
  } finally {
    loading.value = false
  }
}

async function perform(action: () => Promise<unknown>, success: string): Promise<void> {
  if (!issue.value || saving.value) return
  saving.value = true
  error.value = ''
  notice.value = ''
  try {
    await action()
    notice.value = success
    await load()
  } catch (caught) {
    error.value = messageOf(caught)
  } finally {
    saving.value = false
  }
}

function saveMetadata(): Promise<void> {
  if (!issue.value) return Promise.resolve()
  return perform(
    () => api.updateMetadata(issue.value!.issue_number, {
      expected_version: issue.value!.version,
      customer_name: metadata.customer_name,
      customer_group_name: metadata.customer_group_name || null,
      customer_contact_name: metadata.customer_contact_name || null,
      customer_contact_method: metadata.customer_contact_method || null,
      priority: metadata.priority,
    }),
    '问题信息已保存。',
  )
}

function saveCurrentStatus(): Promise<void> {
  if (!issue.value) return Promise.resolve()
  return perform(
    () => api.updateCurrentStatus(issue.value!.issue_number, {
      expected_version: issue.value!.version,
      issue_current_status: currentStatus.value,
    }),
    '问题状态已更新。',
  )
}

function sendMessage(): Promise<void> {
  if (!issue.value) return Promise.resolve()
  return perform(
    () => api.postMessage(issue.value!.issue_number, {
      expected_version: issue.value!.version,
      text: messageText.value,
      intent: messageIntent.value,
      side: messageSide.value || null,
    }),
    '正式消息已加入发送队列。',
  ).then(() => {
    messageText.value = ''
  })
}

function saveDeadline(): Promise<void> {
  if (!issue.value) return Promise.resolve()
  return perform(
    () => api.setDeadline(issue.value!.issue_number, {
      expected_version: issue.value!.version,
      deadline: dateTimeApiValue(deadlineValue.value),
    }),
    '截止时间已更新。',
  )
}

function adjustDeadline(direction: 'advance' | 'delay'): Promise<void> {
  if (!issue.value) return Promise.resolve()
  const amount = Math.round(Number(deadlineAdjustmentHours.value) * 60)
  if (!Number.isFinite(amount) || amount < 30 || amount % 30 !== 0) {
    error.value = '期限调整需要以至少 30 分钟为单位。'
    return Promise.resolve()
  }
  return perform(
    () => api.adjustDeadline(issue.value!.issue_number, {
      expected_version: issue.value!.version,
      adjustment_minutes: direction === 'advance' ? -amount : amount,
    }),
    '截止时间已调整。',
  )
}

function saveApproachingWindow(): Promise<void> {
  if (!issue.value) return Promise.resolve()
  const minutes = Math.round(Number(approachingHours.value) * 60)
  if (!Number.isFinite(minutes) || minutes < 30 || minutes % 30 !== 0) {
    error.value = '临近期限提醒需要以至少 30 分钟为单位。'
    return Promise.resolve()
  }
  return perform(
    () => api.setApproachingWindow(issue.value!.issue_number, {
      expected_version: issue.value!.version,
      approaching_window_minutes: minutes,
    }),
    '临近期限提醒已更新。',
  )
}

function saveWorkflowStatus(): Promise<void> {
  if (!issue.value) return Promise.resolve()
  return perform(
    () => api.updateWorkflowStatus(issue.value!.issue_number, {
      expected_version: issue.value!.version,
      status: workflowStatus.value,
    }),
    '处理状态已更新。',
  )
}

function transferConsultant(): Promise<void> {
  if (!issue.value) return Promise.resolve()
  return perform(
    () => api.transferConsultant(issue.value!.issue_number, {
      expected_version: issue.value!.version,
      new_consultant_id: transferConsultantId.value,
    }),
    '咨询经办人已转交。',
  )
}

function transferDevTeam(): Promise<void> {
  if (!issue.value) return Promise.resolve()
  return perform(
    () => api.transferDevTeam(issue.value!.issue_number, {
      expected_version: issue.value!.version,
      new_dev_team_id: transferDevTeamId.value,
    }),
    '研发归属团队已更新。',
  )
}

function closeIssue(): Promise<void> {
  if (!issue.value || !window.confirm('确认关闭此问题？')) return Promise.resolve()
  return perform(() => api.closeIssue(issue.value!.issue_number, issue.value!.version), '问题已关闭。')
}

function reopenIssue(): Promise<void> {
  if (!issue.value || !window.confirm('重新打开后将固定进入研发处理流程。继续吗？')) return Promise.resolve()
  return perform(() => api.reopenIssue(issue.value!.issue_number, issue.value!.version), '问题已重新打开并进入研发处理。')
}

function deleteIssue(): Promise<void> {
  if (!issue.value || !window.confirm('永久删除该已关闭问题及其时间线和附件？此操作不可恢复。')) return Promise.resolve()
  return perform(() => api.deleteIssue(issue.value!.issue_number), '已删除关闭的问题。').then(() => {
    window.location.assign('/issues?state=closed')
  })
}

watch(issueNumber, () => void load())
onMounted(() => void load())
</script>

<template>
  <div class="detail-toolbar">
    <RouterLink class="back-link" :to="{ path: '/issues', query: { state: issue?.lifecycle_status === 'closed' ? 'closed' : 'open' } }">← 返回问题列表</RouterLink>
    <span v-if="issue" class="issue-number">{{ issue.bug_number || issue.issue_number }}</span>
  </div>
  <AlertMessage v-if="error" :message="error" />
  <div v-if="notice" class="alert alert-success">{{ notice }}</div>
  <div v-if="loading" class="loading-card">正在加载问题…</div>
  <template v-else-if="issue">
    <PageHeading :eyebrow="issue.consult_queue_name" :title="issue.title">
      <div class="heading-actions">
        <span class="badge" :class="`priority-${issue.priority}`">{{ priorityLabel(issue.priority) }}</span>
        <span class="badge" :class="`workflow-${issue.status}`">{{ workflowLabel(issue.status) }}</span>
        <span v-if="issue.deadline_status" class="badge" :class="`deadline-${issue.deadline_status}`">{{ deadlineLabel(issue.deadline_status) }}</span>
      </div>
    </PageHeading>

    <section class="panel case-overview">
      <div class="section-heading"><h2>问题概览</h2><span>版本 {{ issue.version }} · 更新于 {{ formatDateTime(issue.updated_at) }}</span></div>
      <div class="case-meta-grid">
        <div><span>客户 / 企业</span><strong>{{ issue.customer_name || '未录入' }}</strong></div>
        <div><span>客户群聊名称</span><strong>{{ issue.customer_group_name || '未获取' }}</strong></div>
        <div><span>咨询团队</span><strong>{{ issue.consult_queue_name }}</strong></div>
        <div><span>当前咨询经办</span><strong>{{ issue.current_consultant_name || issue.consult_handler_name || '未指定' }}</strong></div>
        <div><span>当前研发团队</span><strong>{{ issue.current_dev_team_name }}</strong></div>
        <div><span>初次受理组</span><strong>{{ issue.initial_acceptance_team_name || '未指定' }}</strong></div>
        <div><span>怀疑类型</span><strong>{{ issue.suspected_type || '未填写' }}</strong></div>
        <div><span>业务问题状态</span><strong>{{ issue.issue_current_status }}</strong></div>
        <div><span>创建时间</span><strong>{{ formatDateTime(issue.created_at) }}</strong></div>
        <div><span>截止时间</span><strong>{{ formatDateTime(issue.deadline) }}</strong></div>
        <div><span>临近期限提醒</span><strong>提前 {{ Math.round(issue.approaching_window_minutes / 60 * 2) / 2 }} 小时</strong></div>
        <div><span>首次记录人</span><strong>{{ issue.first_recorder_name }}</strong></div>
      </div>
      <div v-if="issue.issue_description" class="description-box"><span>问题描述</span><p>{{ issue.issue_description }}</p></div>
      <div v-if="issue.current_situation" class="description-box"><span>当前情况</span><p>{{ issue.current_situation }}</p></div>
    </section>

    <details v-if="issue.can_edit_metadata" class="panel metadata-editor" :open="metadataOpen" @toggle="metadataOpen = ($event.target as HTMLDetailsElement).open">
      <summary>编辑客户信息与紧急程度</summary>
      <form class="review-grid metadata-grid" @submit.prevent="saveMetadata">
        <label>客户名称<input v-model="metadata.customer_name" maxlength="256" /></label>
        <label>客户群聊名称<input v-model="metadata.customer_group_name" maxlength="256" /></label>
        <label>客户联系人<input v-model="metadata.customer_contact_name" maxlength="256" /></label>
        <label>联系方式<input v-model="metadata.customer_contact_method" maxlength="512" /></label>
        <label>紧急程度<select v-model="metadata.priority"><option value="normal">普通</option><option value="urgent">紧急</option><option value="severe">严重</option></select></label>
        <button class="button button-primary" :disabled="saving">保存问题信息</button>
      </form>
    </details>

    <section v-if="issue.can_transfer || issue.can_transfer_dev_team" class="panel detail-controls">
      <div class="section-heading"><h2>问题交接</h2><span>版本冲突时请刷新后重试</span></div>
      <div class="control-grid">
        <form v-if="issue.can_transfer && issue.transferable_consultants.length" class="control-card" @submit.prevent="transferConsultant">
          <h3>转交咨询经办</h3>
          <p class="muted">当前 {{ issue.current_consultant_name || '未指定' }}</p>
          <label>新经办人<select v-model="transferConsultantId" required><option value="" disabled>请选择经办人</option><option v-for="person in issue.transferable_consultants" :key="person.id" :value="person.id">{{ person.name }}</option></select></label>
          <button class="button button-soft" :disabled="saving">转交</button>
        </form>
        <form v-if="issue.can_transfer_dev_team && issue.transferable_dev_teams.length" class="control-card" @submit.prevent="transferDevTeam">
          <h3>更改研发归属团队</h3>
          <p class="muted">当前 {{ issue.current_dev_team_name }}</p>
          <label>新的研发团队<select v-model="transferDevTeamId" required><option value="" disabled>请选择研发团队</option><option v-for="team in issue.transferable_dev_teams" :key="team.id" :value="team.id">{{ team.name }}</option></select></label>
          <button class="button button-soft" :disabled="saving">更改归属</button>
        </form>
      </div>
    </section>

    <section v-if="issue.can_extend_deadline" class="panel detail-controls">
      <div class="section-heading"><h2>期限设置</h2><span>截止 {{ formatDateTime(issue.deadline) }}</span></div>
      <div class="control-grid">
        <form class="control-card" @submit.prevent="saveDeadline">
          <h3>设置截止时间</h3>
          <label>新的截止时间<input v-model="deadlineValue" type="datetime-local" required /></label>
          <button class="button button-soft" :disabled="saving">保存截止时间</button>
        </form>
        <div class="control-card">
          <h3>调整截止时间</h3>
          <label>调整小时数<input v-model="deadlineAdjustmentHours" type="number" min="0.5" step="0.5" /></label>
          <div class="button-row"><button class="button button-soft" :disabled="saving" @click="adjustDeadline('advance')">提前</button><button class="button button-soft" :disabled="saving" @click="adjustDeadline('delay')">延后</button></div>
        </div>
        <form class="control-card" @submit.prevent="saveApproachingWindow">
          <h3>临近期限提醒</h3>
          <label>提前小时数<input v-model="approachingHours" type="number" min="0.5" step="0.5" required /></label>
          <p class="muted">只调整提醒时间，不改变截止时间。</p>
          <button class="button button-soft" :disabled="saving">保存提醒</button>
        </form>
      </div>
    </section>

    <section v-if="issue.lifecycle_status === 'open' && issue.can_change_consult_status" class="panel detail-controls">
      <div class="section-heading"><h2>处理状态</h2><span>状态变更会检查问题版本</span></div>
      <div class="control-grid">
        <form class="control-card" @submit.prevent="saveCurrentStatus">
          <h3>业务问题状态</h3>
          <label>当前问题状态<select v-model="currentStatus"><option v-for="status in statusOptions" :key="status" :value="status">{{ status }}</option></select></label>
          <button class="button button-soft" :disabled="saving">保存状态</button>
        </form>
        <form class="control-card" @submit.prevent="saveWorkflowStatus">
          <h3>工作流状态</h3>
          <label>处理状态<select v-model="workflowStatus"><option v-for="status in workflowOptions" :key="status.value" :value="status.value">{{ status.label }}</option></select></label>
          <button class="button button-soft" :disabled="saving">更新处理状态</button>
        </form>
      </div>
    </section>

    <section v-if="issue.lifecycle_status === 'open'" class="panel message-panel">
      <div class="section-heading"><h2>发送正式消息</h2><span>消息会按当前路由投递给相关团队</span></div>
      <form class="stack-form" @submit.prevent="sendMessage">
        <label>消息内容<textarea v-model="messageText" required maxlength="20000" rows="5" placeholder="输入要同步或交接的正式内容…" /></label>
        <div class="inline-edit-form message-options">
          <label>消息用途<select v-model="messageIntent"><option value="handoff">正式交接</option><option value="sync">仅同步进展</option></select></label>
          <label>发送身份<select v-model="messageSide"><option value="">按当前问题路由</option><option value="consult">咨询</option><option value="dev">研发</option></select></label>
          <button class="button button-primary" :disabled="saving || !messageText.trim()">发送消息</button>
        </div>
      </form>
    </section>

    <section class="timeline-section">
      <div class="section-heading"><h2>问题时间线</h2><span>{{ issue.entries.length }} 条记录</span></div>
      <div v-if="!issue.entries.length" class="empty-inline">暂无时间线记录。</div>
      <article v-for="entry in issue.entries" :key="entry.id" class="timeline-entry" :class="{ 'entry-system': entry.kind !== 'formal_message' }">
        <div class="timeline-heading">
          <div><strong>{{ entry.actor_name }}</strong><span class="muted"> · {{ entry.side === 'consult' ? '咨询' : entry.side === 'dev' ? '研发' : '系统' }}</span></div>
          <time>{{ formatDateTime(entry.created_at) }}</time>
        </div>
        <div v-if="entry.intent" class="entry-intent" :class="entry.intent === 'sync' ? 'intent-sync' : 'intent-handoff'">{{ entry.intent === 'sync' ? '进展同步' : '正式交接' }}</div>
        <div class="timeline-parts">
          <template v-for="part in entry.parts" :key="part.position">
            <p v-if="part.kind === 'text'" class="timeline-text">{{ part.text }}</p>
            <img v-else-if="part.media_id" class="timeline-image" :src="mediaPath(`issues/${issue.issue_number}/media/${part.media_id}`)" alt="问题图片附件" loading="lazy" />
          </template>
        </div>
        <div v-if="entry.deliveries.length" class="delivery-list">
          <span v-for="(delivery, index) in entry.deliveries" :key="index" class="badge" :class="`delivery-${delivery.status}`">
            {{ delivery.destination_type === 'chat' ? '群聊' : '成员' }} · {{ delivery.status }} · {{ delivery.attempts }} 次
          </span>
        </div>
      </article>
    </section>

    <section class="panel bottom-actions">
      <button v-if="issue.lifecycle_status === 'open' && canManage" class="button button-danger" :disabled="saving" @click="closeIssue">关闭问题</button>
      <button v-if="issue.lifecycle_status === 'closed' && canManage" class="button button-primary" :disabled="saving" @click="reopenIssue">重新打开并转研发</button>
      <button v-if="issue.lifecycle_status === 'closed' && isGlobalAdmin" class="button button-danger" :disabled="saving" @click="deleteIssue">永久删除</button>
      <span v-if="issue.lifecycle_status === 'closed'" class="muted">重新打开将固定进入研发处理流程。</span>
    </section>
  </template>
</template>
