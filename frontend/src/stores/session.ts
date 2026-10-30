import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api, type ApiSession } from '@/api/client'

export const useSessionStore = defineStore('session', () => {
  const session = ref<ApiSession | null>(null)
  const loaded = ref(false)
  let pending: Promise<ApiSession> | null = null

  async function load(force = false): Promise<ApiSession> {
    if (loaded.value && !force && session.value) return session.value
    if (pending) return pending
    pending = api.session()
      .then((result) => {
        session.value = result
        loaded.value = true
        return result
      })
      .finally(() => {
        pending = null
      })
    return pending
  }

  function clear(): void {
    session.value = null
    loaded.value = false
  }

  return { session, loaded, load, clear }
})
