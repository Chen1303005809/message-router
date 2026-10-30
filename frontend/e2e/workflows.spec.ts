import { expect, test, type Page } from '@playwright/test'

const consultQueueId = 'd9b76352-511f-493a-b9a2-328f0fa58173'
const devTeamId = '8be21813-0715-4b18-82ef-cc9d99a3d316'

const authenticatedSession = {
  authenticated: true,
  setup_required: false,
  auth_mode: 'development',
  user_id: 'd4bb0a1e-bd77-4c03-91a3-847c41a9b60d',
  display_name: '咨询甲',
  is_global_admin: false,
  can_inspect: false,
}

const intakeOptions = {
  consult_queues: [{ id: consultQueueId, name: '咨询队列' }],
  dev_teams: [{ id: devTeamId, name: '研发一组' }],
  default_deadline: '2026-10-30T21:00:00+08:00',
  source_group_lookup_enabled: false,
  private_intake: null,
}

function issueDetail(overrides: Record<string, unknown> = {}) {
  return {
    issue_number: 'KF-20261030-0001',
    title: '登录失败',
    issue_description: '客户无法登录',
    initial_acceptance_team_id: devTeamId,
    initial_acceptance_team_name: '研发一组',
    suspected_type: '盘中',
    current_situation: null,
    issue_current_status: '问题未回复',
    bug_number: null,
    first_recorder_id: authenticatedSession.user_id,
    first_recorder_name: '咨询甲',
    issue_source: '工作台',
    initial_expires_at: null,
    customer_name: '测试客户',
    customer_group_name: '',
    customer_contact_name: null,
    customer_contact_method: null,
    priority: 'normal',
    status: 'pending_confirmation',
    lifecycle_status: 'open',
    deadline: '2026-10-30T21:00:00+08:00',
    approaching_window_minutes: 1440,
    deadline_status: null,
    created_at: '2026-10-30T10:00:00+08:00',
    updated_at: '2026-10-30T10:00:00+08:00',
    consult_queue_id: consultQueueId,
    consult_queue_name: '咨询队列',
    current_consultant_id: authenticatedSession.user_id,
    current_consultant_name: '咨询甲',
    consult_handler_name: '咨询甲',
    current_dev_team_id: devTeamId,
    current_dev_team_name: '研发一组',
    current_dev_team_lead_display_name: '一组负责人',
    can_edit_metadata: true,
    can_change_consult_status: true,
    can_accept_pending: false,
    can_extend_deadline: true,
    can_transfer: false,
    transferable_consultants: [],
    can_transfer_dev_team: false,
    transferable_dev_teams: [],
    version: 1,
    entries: [],
    ...overrides,
  }
}

async function mockSession(page: Page, value = authenticatedSession): Promise<void> {
  await page.route('**/api/session', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(value) }),
  )
}

test('建单上传图片、确认草稿并进入详情', async ({ page }) => {
  await mockSession(page)
  await page.route('**/api/intake/options', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(intakeOptions) }),
  )
  await page.route('**/api/issues/drafts', async (route) => {
    expect(route.request().method()).toBe('POST')
    const body = route.request().postDataBuffer()?.toString('utf8') || ''
    expect(body).toContain('客户无法登录')
    expect(body).toContain('support.png')
    await route.fulfill({
      status: 201,
      contentType: 'application/json',
      body: JSON.stringify({
        draft: {
          id: 'draft-100',
          issue_number: 'KF-20261030-0001',
          raw_text: '客户无法登录',
          parts: [{ position: 0, kind: 'text', text: '客户无法登录', media_id: null }],
          consult_queue_id: consultQueueId,
          fields: {
            issue_title: '登录失败',
            issue_description: '客户无法登录',
            customer_name: '测试客户',
            customer_contact_name: '',
            customer_contact_method: '',
            initial_acceptance_team_id: devTeamId,
            suspected_type: '盘中',
            issue_current_status: '问题未回复',
            current_situation: '',
            issue_source: '工作台',
            deadline: '2026-10-30T21:00:00+08:00',
          },
          first_recorder_name: '咨询甲',
          source_group_name: '',
          initial_expires_at: '2026-10-30T21:00:00+08:00',
          expires_at: '2026-10-30T21:00:00+08:00',
          confirmed_issue_id: null,
        },
      }),
    })
  })
  await page.route('**/api/issues/drafts/draft-100', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        id: 'draft-100',
        issue_number: 'KF-20261030-0001',
        raw_text: '客户无法登录',
        parts: [{ position: 0, kind: 'text', text: '客户无法登录', media_id: null }],
        consult_queue_id: consultQueueId,
        fields: {
          issue_title: '登录失败',
          issue_description: '客户无法登录',
          customer_name: '测试客户',
          customer_contact_name: '',
          customer_contact_method: '',
          initial_acceptance_team_id: devTeamId,
          suspected_type: '盘中',
          issue_current_status: '问题未回复',
          current_situation: '',
          issue_source: '工作台',
          deadline: '2026-10-30T21:00:00+08:00',
        },
        first_recorder_name: '咨询甲',
        source_group_name: '',
        initial_expires_at: '2026-10-30T21:00:00+08:00',
        expires_at: '2026-10-30T21:00:00+08:00',
        confirmed_issue_id: null,
      }),
    }),
  )
  await page.route('**/api/issues/drafts/draft-100/confirm', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ issue_number: 'KF-20261030-0001', version: 1, idempotent: false }),
    }),
  )
  await page.route('**/api/issues/KF-20261030-0001', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(issueDetail()) }),
  )

  await page.goto('/issues/new')
  await page.getByLabel('问题材料').fill('客户无法登录')
  await page.locator('input[type=file]').setInputFiles({
    name: 'support.png',
    mimeType: 'image/png',
    buffer: Buffer.from('small-png-fixture'),
  })
  await page.getByRole('button', { name: '上传并整理' }).click()

  await expect(page.getByRole('heading', { name: '核对问题内容' })).toBeVisible()
  await page.getByRole('button', { name: '确认并推送' }).click()
  await expect(page.getByRole('heading', { name: '登录失败' })).toBeVisible()
})

test('详情打开时通过幂等接口自动受理待确认问题', async ({ page }) => {
  await mockSession(page)
  let reads = 0
  let accepts = 0
  await page.route('**/api/issues/KF-20261030-0002', (route) => {
    reads += 1
    const body = reads > 1
      ? issueDetail({ issue_number: 'KF-20261030-0002', status: 'in_progress', can_accept_pending: false, version: 2 })
      : issueDetail({ issue_number: 'KF-20261030-0002', can_accept_pending: true })
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
  })
  await page.route('**/api/issues/KF-20261030-0002/accept', (route) => {
    accepts += 1
    expect(route.request().postDataJSON()).toEqual({ expected_version: 1 })
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ issue_number: 'KF-20261030-0002', version: 2, idempotent: false, accepted: true }),
    })
  })

  await page.goto('/issues/KF-20261030-0002')
  await expect(page.getByRole('heading', { name: '登录失败' })).toBeVisible()
  expect(accepts).toBe(1)
  expect(reads).toBe(2)
})

test('重新打开问题时前端不提供等待方，由服务端固定进入研发流程', async ({ page }) => {
  await mockSession(page)
  let reopened = false
  await page.route('**/api/issues/KF-20261030-0003', (route) => {
    const body = reopened
      ? issueDetail({
          issue_number: 'KF-20261030-0003',
          lifecycle_status: 'open',
          status: 'in_progress',
          can_change_consult_status: true,
          version: 2,
        })
      : issueDetail({
          issue_number: 'KF-20261030-0003',
          lifecycle_status: 'closed',
          status: 'closed',
          can_change_consult_status: true,
        })
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
  })
  await page.route('**/api/issues/KF-20261030-0003/reopen', async (route) => {
    expect(route.request().method()).toBe('POST')
    expect(route.request().postDataJSON()).toEqual({ expected_version: 1 })
    reopened = true
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ issue_number: 'KF-20261030-0003', version: 2, idempotent: false }),
    })
  })
  page.on('dialog', (dialog) => dialog.accept())

  await page.goto('/issues/KF-20261030-0003')
  await page.getByRole('button', { name: '重新打开并转研发' }).click()

  await expect(page.getByText('问题已重新打开并进入研发处理。')).toBeVisible()
  await expect(page.getByText('处理中', { exact: true }).first()).toBeVisible()
  expect(reopened).toBe(true)
})

test('非管理员访问管理页会回到列表', async ({ page }) => {
  await mockSession(page)
  await page.route('**/api/issues?*', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ items: [], offset: 0, next_offset: null }),
    }),
  )

  await page.goto('/admin')
  await expect(page.getByRole('heading', { name: '问题列表' })).toBeVisible()
  await expect(page.getByRole('heading', { name: '组织与授权' })).toHaveCount(0)
})

test('旧版 /events/new 地址会跳转到新建问题页面', async ({ page }) => {
  await mockSession(page)
  await page.route('**/api/intake/options', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(intakeOptions) }),
  )

  await page.goto('/events/new')

  await expect(page).toHaveURL(/\/issues\/new$/)
  await expect(page.getByRole('heading', { name: '新建问题' })).toBeVisible()
})

test('OAuth 登录按钮将原页面作为服务端回跳地址', async ({ page }) => {
  let loginTarget = ''
  await page.route('**/api/session', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ authenticated: false, setup_required: false, auth_mode: 'wecom_oauth' }),
    }),
  )
  await page.route('**/auth/login*', async (route) => {
    loginTarget = route.request().url()
    await route.fulfill({ status: 200, contentType: 'text/plain', body: 'OAuth is handled by the backend' })
  })

  await page.goto('/login?next=%2Fissues')
  await page.getByRole('button', { name: '企业微信登录' }).click()
  await expect(page).toHaveURL(/\/auth\/login\?next=/)
  expect(new URL(loginTarget).searchParams.get('next')).toBe('/issues')
})

test('私聊交接 token 经 API 验证后带入建单页面', async ({ page }) => {
  await mockSession(page)
  await page.route('**/api/issues/private-intake/open', (route) => {
    expect(route.request().postDataJSON()).toEqual({ token: 'private-token' })
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ opened: true }) })
  })
  await page.route('**/api/intake/options', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        ...intakeOptions,
        private_intake: {
          raw_text: '来自机器人私聊的内容',
          media: [],
          expires_at: '2026-10-30T21:00:00+08:00',
        },
      }),
    }),
  )

  await page.goto('/issues/private-intake#private-token')
  await expect(page.getByLabel('问题材料')).toHaveValue('来自机器人私聊的内容')
  await expect(page).toHaveURL(/\/issues\/new$/)
})
