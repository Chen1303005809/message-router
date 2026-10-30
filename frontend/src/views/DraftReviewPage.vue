<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import AlertMessage from '@/components/AlertMessage.vue'
import PageHeading from '@/components/PageHeading.vue'
import { api, type IntakeOptions, type IssueDraft } from '@/api/client'
import { dateTimeApiValue, dateTimeLocal, formatDateTime, mediaPath, messageOf } from '@/utils'

const route = useRoute()
const router = useRouter()
const draft = ref<IssueDraft | null>(null)
const options = ref<IntakeOptions | null>(null)
const loading = ref(true)
const submitting = ref(false)
const error = ref('')
const values = reactive({
  issue_title: '',
  issue_description: '',
  customer_name: '',
  customer_contact_name: '',
  customer_contact_method: '',
  initial_acceptance_team_id: '',
  suspected_type: '',
  issue_current_status: '问题未回复',
  current_situation: '',
  issue_source: '工作台',
  deadline: '',
})

const id = computed(() => String(route.params.draftId))
const modules = ['盘中', '日常使用', '交易', '风控端', '结算', '绩效', '核算']
const currentStatuses = ['问题未回复', '需求待受理', '已解决', '升级需跟进', '数据待处理', '当前无法解决']

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const [loadedDraft, loadedOptions] = await Promise.all([api.getDraft(id.value), api.intakeOptions()])
    draft.value = loadedDraft
    options.value = loadedOptions
    Object.assign(values, {
      ...loadedDraft.fields,
      initial_acceptance_team_id: loadedDraft.fields.initial_acceptance_team_id || '',
      deadline: dateTimeLocal(loadedDraft.fields.deadline || loadedDraft.initial_expires_at),
    })
  } catch (caught) {
    error.value = messageOf(caught)
  } finally {
    loading.value = false
  }
}

async function submit(): Promise<void> {
  if (!draft.value || submitting.value) return
  submitting.value = true
  error.value = ''
  try {
    const result = await api.confirmDraft(id.value, {
      issue_title: values.issue_title,
      issue_description: values.issue_description,
      customer_name: values.customer_name,
      customer_contact_name: values.customer_contact_name,
      customer_contact_method: values.customer_contact_method,
      initial_acceptance_team_id: values.initial_acceptance_team_id,
      suspected_type: values.suspected_type,
      issue_current_status: values.issue_current_status,
      current_situation: values.current_situation,
      issue_source: values.issue_source,
      deadline: dateTimeApiValue(values.deadline),
    })
    await router.replace(`/issues/${encodeURIComponent(result.issue_number)}`)
  } catch (caught) {
    error.value = messageOf(caught)
  } finally {
    submitting.value = false
  }
}

onMounted(() => void load())
</script>

<template>
  <PageHeading
    eyebrow="问题中心 / 草稿审核"
    title="核对问题内容"
    description="确认后才会创建问题并推送给初次受理组。"
  >
    <div class="heading-actions">
      <RouterLink class="button button-soft" to="/issues/new">重新上传材料</RouterLink>
    </div>
  </PageHeading>

  <AlertMessage v-if="error" :message="error" />
  <div v-if="loading" class="loading-card">正在加载草稿…</div>
  <form v-else-if="draft" class="panel review-form" @submit.prevent="submit">
    <div class="draft-meta">
      <span>草稿编号 <strong>{{ draft.issue_number }}</strong></span>
      <span>草稿有效至 {{ formatDateTime(draft.expires_at) }}</span>
    </div>
    <label>
      问题标题
      <input v-model="values.issue_title" required maxlength="512" placeholder="问题的简短概括" />
    </label>
    <label>
      转发正文
      <textarea v-model="values.issue_description" required maxlength="20000" rows="8" />
    </label>
    <p class="muted form-hint">标题和正文会转发给初次受理组，图片按原顺序作为附件发送。</p>
    <div class="review-grid">
      <label>客户名称<input v-model="values.customer_name" maxlength="256" /></label>
      <label>
        客户群聊名称
        <span class="readonly-field">{{ draft.source_group_name || '未获取' }}</span>
      </label>
      <label>
        初次受理组
        <select v-model="values.initial_acceptance_team_id" required>
          <option value="" disabled>请选择初次受理组</option>
          <option v-for="team in options?.dev_teams" :key="team.id" :value="team.id">{{ team.name }}</option>
        </select>
      </label>
      <label>
        怀疑类型
        <select v-model="values.suspected_type" required>
          <option value="" disabled>请选择问题类型</option>
          <option v-for="module in modules" :key="module" :value="module">{{ module }}</option>
        </select>
      </label>
      <label>当前问题状态
        <select v-model="values.issue_current_status">
          <option v-for="status in currentStatuses" :key="status" :value="status">{{ status }}</option>
        </select>
      </label>
      <label>截止时间<input v-model="values.deadline" type="datetime-local" required /></label>
    </div>
    <details class="source-material">
      <summary>收到的材料 · 咨询队列</summary>
      <p class="muted">{{ options?.consult_queues.find((queue) => queue.id === draft?.consult_queue_id)?.name || '已登记咨询队列' }}</p>
      <template v-for="(part, index) in draft.parts" :key="`${part.media_id || 'text'}-${index}`">
        <pre v-if="part.kind === 'text'" class="source-text">{{ part.text }}</pre>
        <img
          v-else-if="part.media_id"
          class="source-image"
          :src="mediaPath(`issues/drafts/${draft.id}/media/${part.media_id}`)"
          alt="问题图片附件"
        />
      </template>
    </details>
    <div class="form-actions">
      <RouterLink class="button button-soft" to="/issues/new">返回修改材料</RouterLink>
      <button class="button button-primary" type="submit" :disabled="submitting">
        {{ submitting ? '正在提交…' : '确认并推送' }}
      </button>
    </div>
  </form>
</template>
