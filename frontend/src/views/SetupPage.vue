<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import AlertMessage from '@/components/AlertMessage.vue'
import { api } from '@/api/client'
import { useSessionStore } from '@/stores/session'
import { messageOf } from '@/utils'

const router = useRouter()
const session = useSessionStore()
const loading = ref(true)
const submitting = ref(false)
const error = ref('')
const wecomUserId = ref('')
const displayName = ref('问题工作台总管理员')
const token = ref('')

async function load(): Promise<void> {
  try {
    const status = await api.setupStatus()
    if (!status.required) {
      const current = await session.load(true)
      await router.replace(current.authenticated ? '/issues' : '/login')
    }
  } catch (caught) {
    error.value = messageOf(caught)
  } finally {
    loading.value = false
  }
}

async function submit(): Promise<void> {
  submitting.value = true
  error.value = ''
  try {
    await api.setup({ wecom_userid: wecomUserId.value, display_name: displayName.value, token: token.value })
    const current = await session.load(true)
    await router.replace(current.authenticated ? '/admin' : '/login?next=%2Fadmin')
  } catch (caught) {
    error.value = messageOf(caught)
  } finally {
    submitting.value = false
  }
}

onMounted(() => void load())
</script>

<template>
  <section class="auth-card setup-card">
    <div class="eyebrow">首次使用 · 问题工作台</div>
    <h1>初始化总管理员</h1>
    <p class="muted">管理员使用企业微信 userid 登录，不单独设置密码。初始化后可管理成员、团队授权和群绑定。</p>
    <AlertMessage v-if="error" :message="error" />
    <div v-if="loading" class="loading-card">正在检查初始化状态…</div>
    <form v-else class="stack-form" @submit.prevent="submit">
      <label>企业微信 userid<input v-model="wecomUserId" required maxlength="128" placeholder="例如 zhangsan" /></label>
      <label>显示名称<input v-model="displayName" required maxlength="256" /></label>
      <label>初始化令牌<input v-model="token" type="password" maxlength="512" autocomplete="off" placeholder="生产环境必填" /></label>
      <button class="button button-primary button-wide" type="submit" :disabled="submitting">
        {{ submitting ? '正在初始化…' : '完成初始化' }}
      </button>
    </form>
  </section>
</template>
