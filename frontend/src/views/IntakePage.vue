<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import AlertMessage from '@/components/AlertMessage.vue'
import PageHeading from '@/components/PageHeading.vue'
import { api, type IntakeOptions } from '@/api/client'
import { lookupCustomerGroup } from '@/services/wecomContext'
import { formatDateTime, mediaPath, messageOf } from '@/utils'

const router = useRouter()
const options = ref<IntakeOptions | null>(null)
const loading = ref(true)
const submitting = ref(false)
const error = ref('')
const pastedText = ref('')
const consultQueueId = ref('')
const selectedImages = ref<Array<{ file: File; url: string }>>([])
const dragOver = ref(false)
const privateIntake = computed(() => options.value?.private_intake || null)

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    options.value = await api.intakeOptions()
    if (!consultQueueId.value) consultQueueId.value = options.value.consult_queues[0]?.id || ''
    if (!pastedText.value && privateIntake.value) pastedText.value = privateIntake.value.raw_text
    if (!options.value.consult_queues.length) error.value = '你当前没有可用的咨询队列，请联系管理员授权。'
    else if (!options.value.dev_teams.length) error.value = '当前没有可用的初次受理组，请联系管理员配置研发团队。'
  } catch (caught) {
    error.value = messageOf(caught)
  } finally {
    loading.value = false
  }
}

function appendImages(files: FileList | File[]): void {
  const incoming = Array.from(files).filter((file) => file.type.startsWith('image/'))
  const tooLarge = incoming.find((file) => file.size > 20 * 1024 * 1024)
  if (tooLarge) {
    error.value = `${tooLarge.name} 超过 20 MiB，无法上传。`
    return
  }
  error.value = ''
  const current = new Set(selectedImages.value.map(({ file }) => `${file.name}:${file.size}:${file.lastModified}`))
  for (const file of incoming) {
    const key = `${file.name}:${file.size}:${file.lastModified}`
    if (!current.has(key)) {
      current.add(key)
      selectedImages.value.push({ file, url: URL.createObjectURL(file) })
    }
  }
}

function removeImage(index: number): void {
  const [removed] = selectedImages.value.splice(index, 1)
  if (removed) URL.revokeObjectURL(removed.url)
}

function pasteImages(event: ClipboardEvent): void {
  const files = Array.from(event.clipboardData?.items || [])
    .filter((item) => item.kind === 'file' && item.type.startsWith('image/'))
    .map((item) => item.getAsFile())
    .filter((file): file is File => file !== null)
  if (files.length) appendImages(files)
}

async function submit(): Promise<void> {
  if (!options.value || submitting.value) return
  if (!pastedText.value.trim()) {
    error.value = '请粘贴问题内容后再上传。'
    return
  }
  if (!consultQueueId.value) {
    error.value = '请选择咨询队列。'
    return
  }
  submitting.value = true
  error.value = ''
  try {
    let group = { chatId: '', status: 'not_reported', error: '' }
    if (options.value.source_group_lookup_enabled && !privateIntake.value) {
      group = await lookupCustomerGroup()
    }
    const form = new FormData()
    form.set('pasted_text', pastedText.value)
    form.set('consult_queue_id', consultQueueId.value)
    form.set('source_chat_id', group.chatId)
    form.set('source_chat_status', group.status)
    form.set('source_chat_error', group.error)
    for (const image of selectedImages.value) form.append('images', image.file, image.file.name)
    const result = await api.createDraft(form)
    await router.push(`/issues/drafts/${result.draft.id}`)
  } catch (caught) {
    error.value = messageOf(caught)
  } finally {
    submitting.value = false
  }
}

onMounted(() => void load())
onUnmounted(() => selectedImages.value.forEach(({ url }) => URL.revokeObjectURL(url)))
</script>

<template>
  <PageHeading
    eyebrow="问题中心 / 建单"
    title="新建问题"
    description="粘贴客户反馈和补充材料，系统整理为草稿供你核对。"
  >
    <div class="heading-actions">
      <RouterLink class="button button-soft" to="/issues">返回问题列表</RouterLink>
    </div>
  </PageHeading>

  <AlertMessage v-if="error" :message="error" />
  <div v-if="loading" class="loading-card">正在准备建单…</div>
  <form v-else class="panel intake-form" @submit.prevent="submit" @paste="pasteImages">
    <div v-if="privateIntake" class="notice-box">
      已从机器人私聊带入材料。图片和文字会进入同一审核流程，确认前不会创建正式问题。
      <span v-if="privateIntake.expires_at">交接有效至 {{ formatDateTime(privateIntake.expires_at) }}</span>
    </div>
    <label for="pasted-text">问题材料</label>
    <textarea
      id="pasted-text"
      v-model="pastedText"
      rows="10"
      maxlength="50000"
      required
      autofocus
      placeholder="粘贴问题内容；可把图片拖入此处，也可以使用下方按钮选择图片。"
      @dragover.prevent="dragOver = true"
      @dragleave.prevent="dragOver = false"
      @drop.prevent="(event) => { dragOver = false; if (event.dataTransfer?.files) appendImages(event.dataTransfer.files) }"
      :class="{ 'drop-active': dragOver }"
    />
    <div class="upload-toolbar">
      <label class="button button-soft file-button">
        ＋ 添加图片
        <input
          type="file"
          accept="image/*"
          multiple
          @change="(event) => { const input = event.target as HTMLInputElement; if (input.files) appendImages(input.files); input.value = '' }"
        />
      </label>
      <span class="muted">可拖拽、多选或粘贴，单张不超过 20 MiB</span>
      <span class="spacer" />
      <label class="queue-select">
        <span>咨询队列</span>
        <select v-model="consultQueueId" required>
          <option v-if="options?.consult_queues.length !== 1" value="" disabled>选择队列</option>
          <option v-for="queue in options?.consult_queues" :key="queue.id" :value="queue.id">
            {{ queue.name }}
          </option>
        </select>
      </label>
      <button class="button button-primary" type="submit" :disabled="submitting || !options?.dev_teams.length">
        {{ submitting ? '正在整理…' : '上传并整理' }}
      </button>
    </div>
    <p class="muted form-hint">图片会按正文中的 [图片] 标记匹配；没有标记的图片会追加到材料末尾。</p>
    <div v-if="privateIntake?.media.length" class="image-grid">
      <figure v-for="item in privateIntake.media" :key="item.media_id" class="image-preview">
        <img :src="mediaPath(`issues/private-intakes/media/${item.media_id}`)" alt="私聊图片附件" />
        <figcaption>来自企微私聊</figcaption>
      </figure>
    </div>
    <div v-if="selectedImages.length" class="image-grid">
      <figure v-for="(image, index) in selectedImages" :key="image.url" class="image-preview">
        <img :src="image.url" :alt="image.file.name" />
        <figcaption>
          <span>{{ image.file.name }}</span>
          <button class="text-button danger-text" type="button" @click="removeImage(index)">移除</button>
        </figcaption>
      </figure>
    </div>
  </form>
</template>
