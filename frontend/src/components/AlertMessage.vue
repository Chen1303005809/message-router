<script setup lang="ts">
defineProps<{ message: string; tone?: 'error' | 'success' | 'notice' }>()
</script>

<template>
  <div class="alert" :class="`alert-${tone || 'error'}`" role="status">{{ message }}</div>
</template>
