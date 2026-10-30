<script setup lang="ts">
defineProps<{ eyebrow?: string; title: string; description?: string }>()
</script>

<template>
  <div class="page-heading">
    <div>
      <div v-if="eyebrow" class="eyebrow">{{ eyebrow }}</div>
      <h1>{{ title }}</h1>
      <p v-if="description" class="muted">{{ description }}</p>
    </div>
    <slot />
  </div>
</template>
