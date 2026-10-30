<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '@/api/client'
import { messageOf } from '@/utils'

const router = useRouter()
const message = ref('正在验证私聊交接…')
const error = ref('')

onMounted(async () => {
  const token = sessionStorage.getItem('kefu_private_intake_token')
  if (!token) {
    message.value = '没有找到有效的交接链接。'
    error.value = '请重新从企业微信机器人私聊打开问题卡片。'
    return
  }
  try {
    await api.openPrivateIntake(token)
    sessionStorage.removeItem('kefu_private_intake_token')
    await router.replace('/issues/new')
  } catch (caught) {
    message.value = '交接链接验证失败'
    error.value = `${messageOf(caught)} 请确认当前登录账号与原私聊发送人相同。`
  }
})
</script>

<template>
  <section class="auth-card">
    <div class="eyebrow">安全打开私聊问题</div>
    <h1>{{ message }}</h1>
    <p v-if="error" class="alert alert-error">{{ error }}</p>
    <p v-else class="muted">验证成功后，原始文字和图片会自动带入新建问题页面。</p>
  </section>
</template>
