<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink, RouterView, useRoute } from 'vue-router'
import { useSessionStore } from '@/stores/session'

const route = useRoute()
const session = useSessionStore()
const isPublicPage = computed(() => Boolean(route.meta.public))
</script>

<template>
  <div class="app-shell" :class="{ 'public-shell': isPublicPage }">
    <header v-if="!isPublicPage" class="app-header">
      <RouterLink class="brand" to="/issues">
        <span class="brand-mark">K</span>
        <span><strong>问题工作台</strong><small>客服与研发协作</small></span>
      </RouterLink>
      <nav class="main-nav" aria-label="主导航">
        <RouterLink to="/issues?state=open">问题列表</RouterLink>
        <RouterLink to="/issues?state=mine">我的问题</RouterLink>
        <RouterLink to="/status-queue">异步任务</RouterLink>
        <RouterLink v-if="session.session?.is_global_admin" to="/admin">组织管理</RouterLink>
      </nav>
      <div class="user-chip">
        <span class="avatar">{{ session.session?.display_name?.slice(0, 1) || '用' }}</span>
        <span>{{ session.session?.display_name || '已登录' }}</span>
      </div>
    </header>
    <main :class="isPublicPage ? 'public-content' : 'page-content'">
      <RouterView />
    </main>
  </div>
</template>
