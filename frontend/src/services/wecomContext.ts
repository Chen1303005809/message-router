import { appPath } from '@/api/client'

type SdkResult = Record<string, unknown> & {
  chatId?: string
  entry?: string
  err_msg?: string
  errMsg?: string
  errmsg?: string
}

type WeComSdk = {
  agentConfig: (options: Record<string, unknown>) => void
  invoke: (method: string, params: Record<string, never>, callback: (result: SdkResult) => void) => void
}

declare global {
  interface Window {
    wx?: WeComSdk
    wecomCustomerGroupLookupStatus?: string
    wecomCustomerGroupLookupError?: string
  }
}

const sdkScripts = [
  'https://res.wx.qq.com/open/js/jweixin-1.2.0.js',
  'https://open.work.weixin.qq.com/wwopen/js/jwxwork-1.0.0.js',
]

function loadScript(src: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const loaded = document.querySelector<HTMLScriptElement>(`script[src="${src}"]`)
    if (loaded && window.wx) {
      resolve()
      return
    }
    const script = loaded || document.createElement('script')
    script.src = src
    script.async = true
    script.onload = () => resolve()
    script.onerror = () => reject(new Error('sdk_script_load_failed'))
    if (!loaded) document.head.append(script)
  })
}

function invoke(method: string): Promise<SdkResult | null> {
  return new Promise((resolve) => {
    let done = false
    const timer = window.setTimeout(() => finish(null), 4000)
    const finish = (result: SdkResult | null) => {
      if (done) return
      done = true
      window.clearTimeout(timer)
      resolve(result)
    }
    try {
      window.wx?.invoke(method, {}, (result) => finish(result || null))
    } catch {
      finish(null)
    }
  })
}

function succeeded(result: SdkResult | null, method: string): boolean {
  return Boolean(result && [result.err_msg, result.errMsg, result.errmsg].includes(`${method}:ok`))
}

export async function lookupCustomerGroup(): Promise<{
  chatId: string
  status: string
  error: string
}> {
  const empty = (status: string, error = '') => ({ chatId: '', status, error })
  if (!/wxwork/i.test(navigator.userAgent || '')) return empty('not_wecom_client')

  const result = await Promise.race([
    (async () => {
      try {
        for (const src of sdkScripts) await loadScript(src)
      } catch {
        return empty('sdk_script_load_failed')
      }
      const sdk = window.wx
      if (!sdk?.agentConfig) return empty('sdk_api_unavailable')
      const pageUrl = window.location.href.split('#', 1)[0]
      let response: Response
      try {
        const configUrl = `${appPath('api/wecom/agent-js-sdk-config')}?url=${encodeURIComponent(pageUrl)}`
        response = await fetch(configUrl, { credentials: 'same-origin' })
      } catch {
        return empty('js_sdk_config_fetch_failed')
      }
      if (!response.ok) return empty('js_sdk_config_http_error')
      const config = await response.json().catch(() => null)
      if (!config?.corpid || !config?.agentid || !config?.signature) {
        return empty('js_sdk_config_invalid')
      }
      const context = await new Promise<SdkResult | null>((resolve) => {
        let finished = false
        const timer = window.setTimeout(() => finish(null), 5000)
        const finish = (value: SdkResult | null) => {
          if (finished) return
          finished = true
          window.clearTimeout(timer)
          resolve(value)
        }
        try {
          sdk.agentConfig({
            corpid: config.corpid,
            agentid: config.agentid,
            timestamp: config.timestamp,
            nonceStr: config.nonceStr,
            signature: config.signature,
            jsApiList: ['getContext', 'getCurExternalChat'],
            success: async () => {
              const value = await invoke('getContext')
              finish(succeeded(value, 'getContext') ? value : null)
            },
            fail: () => finish(null),
          })
        } catch {
          finish(null)
        }
      })
      if (!context) return empty('agent_config_failed')
      if (context.entry !== 'group_chat_tools') return empty('not_group_chat_tools')
      const group = await invoke('getCurExternalChat')
      if (!group) return empty('get_cur_external_chat_timeout')
      if (!succeeded(group, 'getCurExternalChat')) return empty('get_cur_external_chat_failed')
      if (typeof group.chatId !== 'string' || !group.chatId.trim()) return empty('chat_id_missing')
      return { chatId: group.chatId, status: 'success', error: '' }
    })(),
    new Promise<ReturnType<typeof empty>>((resolve) => {
      window.setTimeout(() => resolve(empty('lookup_timeout')), 9000)
    }),
  ])
  window.wecomCustomerGroupLookupStatus = result.status
  window.wecomCustomerGroupLookupError = result.error
  return result
}
