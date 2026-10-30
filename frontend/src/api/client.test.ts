import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, appPath } from './client'

describe('same-origin API client', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('uses the configured SPA base path and same-origin credentials', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          authenticated: true,
          setup_required: false,
          auth_mode: 'development',
          user_id: 'd4bb0a1e-bd77-4c03-91a3-847c41a9b60d',
          display_name: '客服甲',
          is_global_admin: false,
          can_inspect: false,
        }),
        { status: 200, headers: { 'content-type': 'application/json' } },
      ),
    )
    vi.stubGlobal('fetch', fetchMock)

    expect(appPath('/issues')).toBe('/issues')
    await api.session()

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/session',
      expect.objectContaining({ credentials: 'same-origin' }),
    )
    expect(fetchMock.mock.calls[0]?.[1]?.headers).toBeInstanceOf(Headers)
  })

  it('leaves multipart content type to the browser when creating a draft', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ draft: { id: 'draft-id' } }), {
        status: 201,
        headers: { 'content-type': 'application/json' },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const body = new FormData()
    body.set('pasted_text', '客户反馈')

    await api.createDraft(body)

    const init = fetchMock.mock.calls[0]?.[1] as RequestInit
    const headers = new Headers(init.headers)
    expect(init.body).toBe(body)
    expect(headers.get('content-type')).toBeNull()
  })

  it('surfaces the backend detail for failed requests', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: '版本冲突，请刷新' }), {
          status: 409,
          headers: { 'content-type': 'application/json' },
        }),
      ),
    )

    await expect(api.session()).rejects.toMatchObject({
      message: '版本冲突，请刷新',
      status: 409,
    })
  })
})
