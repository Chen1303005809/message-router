<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ApiError, appPath } from '@/api/client'
import { useSessionStore } from '@/stores/session'

const route = useRoute()
const router = useRouter()
const session = useSessionStore()
const loading = ref(true)
const error = ref('')
const current = computed(() => session.session)
const nextPath = computed(() => String(route.query.next || '/issues'))

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const value = await session.load(true)
    if (value.setup_required) await router.replace('/setup')
    else if (value.authenticated) await router.replace(nextPath.value)
  } catch (caught) {
    error.value = caught instanceof ApiError ? caught.message : '无法连接问题工作台。'
  } finally {
    loading.value = false
  }
}

function startLogin(): void {
  const target = appPath(`auth/login?next=${encodeURIComponent(appPath(nextPath.value))}`)
  window.location.assign(target)
}

onMounted(() => void load())
</script>

<template>
  <section class="auth-card">
    <div class="brand brand-large">
      <span class="brand-mark">K</span>
      <span><strong>问题工作台</strong><small>客服与研发协作</small></span>
    </div>
    <div class="eyebrow">安全登录</div>
    <h1>使用企业微信身份继续</h1>
    <p class="muted">问题工作台通过企业微信 OAuth 验证成员身份和访问权限。</p>
    <div v-if="loading" class="loading-card">正在检查登录状态…</div>
    <div v-else-if="error" class="alert alert-error">{{ error }}</div>
    <template v-else-if="current?.auth_mode === 'wecom_oauth'">
      <button class="button button-primary button-wide" @click="startLogin">企业微信登录</button>
      <p class="auth-footnote">若尚未开通访问权限，请联系问题工作台管理员。</p>
    </template>
    <div v-else class="notice-box">
      本地开发环境请在 Compose 配置 <code>DEV_WECOM_USERID</code> 后刷新页面。
    </div>
  </section>
</template>
