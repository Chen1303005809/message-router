import type { components, paths } from './schema'

export type ApiSession = paths['/api/session']['get']['responses'][200]['content']['application/json']
export type IntakeOptions = components['schemas']['IntakeOptionsResponse']
export type IssueSummary = components['schemas']['IssueListItem']
export type IssuePage = components['schemas']['IssueListResponse']
export type IssueDetail = components['schemas']['IssueDetailResponse']
export type IssueDraft = components['schemas']['IssueDraftResponse']
export type ConfirmDraft = components['schemas']['ConfirmIssueDraftRequest']
export type IssueCommand = components['schemas']['IssueCommandResponse']
export type ManagedUser = components['schemas']['ManagedUserResponse']
export type ManagedTeam = components['schemas']['ManagedTeamResponse']
export type AdminOverview = components['schemas']['AdminOverviewResponse']
export type Schedule = components['schemas']['InspectionScheduleResponse']
export type StatusQueue = components['schemas']['StatusQueueResponse']

const APP_BASE = import.meta.env.BASE_URL.endsWith('/')
  ? import.meta.env.BASE_URL
  : `${import.meta.env.BASE_URL}/`

export function appPath(path: string): string {
  return `${APP_BASE}${path.replace(/^\/+/, '')}`
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  if (init.body && !(init.body instanceof FormData) && !headers.has('content-type')) {
    headers.set('content-type', 'application/json')
  }
  headers.set('accept', 'application/json')
  const response = await fetch(appPath(path), {
    ...init,
    credentials: 'same-origin',
    headers,
  })
  if (response.status === 204) return undefined as T
  const contentType = response.headers.get('content-type') || ''
  const payload = contentType.includes('application/json')
    ? await response.json().catch(() => null)
    : null
  if (!response.ok) {
    const detail = typeof payload?.detail === 'string' ? payload.detail : '请求失败，请稍后重试。'
    throw new ApiError(detail, response.status)
  }
  return payload as T
}

function jsonBody(body: unknown): string {
  return JSON.stringify(body)
}

export const api = {
  session: () => request<ApiSession>('api/session'),
  setupStatus: () => request<components['schemas']['SetupStatusResponse']>('api/setup'),
  setup: (body: components['schemas']['SetupRequest']) =>
    request<components['schemas']['SetupResponse']>('api/setup', {
      method: 'POST',
      body: jsonBody(body),
    }),
  intakeOptions: () => request<IntakeOptions>('api/intake/options'),
  openPrivateIntake: (token: string) =>
    request<components['schemas']['PrivateIntakeOpenResponse']>('api/issues/private-intake/open', {
      method: 'POST',
      body: jsonBody({ token }),
    }),
  createDraft: (body: FormData) =>
    request<components['schemas']['IssueDraftCreatedResponse']>('api/issues/drafts', {
      method: 'POST',
      body,
    }),
  getDraft: (id: string) => request<IssueDraft>(`api/issues/drafts/${encodeURIComponent(id)}`),
  confirmDraft: (id: string, body: ConfirmDraft) =>
    request<components['schemas']['IssueConfirmedResponse']>(
      `api/issues/drafts/${encodeURIComponent(id)}/confirm`,
      { method: 'POST', body: jsonBody(body) },
    ),
  issues: (state: string, priority: string, query: string) => {
    const params = new URLSearchParams({ state })
    if (priority) params.set('priority', priority)
    if (query) params.set('q', query)
    return request<IssuePage>(`api/issues?${params.toString()}`)
  },
  issue: (number: string) =>
    request<IssueDetail>(`api/issues/${encodeURIComponent(number)}`),
  acceptIssue: (number: string, expected_version: number) =>
    request<IssueCommand>(`api/issues/${encodeURIComponent(number)}/accept`, {
      method: 'POST',
      body: jsonBody({ expected_version }),
    }),
  postMessage: (
    number: string,
    body: components['schemas']['PostIssueMessageRequest'],
  ) =>
    request<IssueCommand>(`api/issues/${encodeURIComponent(number)}/messages`, {
      method: 'POST',
      body: jsonBody(body),
    }),
  updateMetadata: (
    number: string,
    body: components['schemas']['UpdateIssueMetadataRequest'],
  ) =>
    request<IssueCommand>(`api/issues/${encodeURIComponent(number)}/metadata`, {
      method: 'PATCH',
      body: jsonBody(body),
    }),
  updateCurrentStatus: (
    number: string,
    body: components['schemas']['SetIssueCurrentStatusRequest'],
  ) =>
    request<IssueCommand>(`api/issues/${encodeURIComponent(number)}/current-status`, {
      method: 'PATCH',
      body: jsonBody(body),
    }),
  setDeadline: (number: string, body: components['schemas']['SetIssueDeadlineRequest']) =>
    request<IssueCommand>(`api/issues/${encodeURIComponent(number)}/deadline`, {
      method: 'PATCH',
      body: jsonBody(body),
    }),
  adjustDeadline: (
    number: string,
    body: components['schemas']['AdjustIssueDeadlineRequest'],
  ) =>
    request<IssueCommand>(`api/issues/${encodeURIComponent(number)}/deadline/adjust`, {
      method: 'POST',
      body: jsonBody(body),
    }),
  setApproachingWindow: (
    number: string,
    body: components['schemas']['SetIssueApproachingWindowRequest'],
  ) =>
    request<IssueCommand>(
      `api/issues/${encodeURIComponent(number)}/deadline/approaching-window`,
      { method: 'PATCH', body: jsonBody(body) },
    ),
  updateWorkflowStatus: (
    number: string,
    body: components['schemas']['SetIssueWorkflowStatusRequest'],
  ) =>
    request<IssueCommand>(`api/issues/${encodeURIComponent(number)}/status`, {
      method: 'PATCH',
      body: jsonBody(body),
    }),
  closeIssue: (number: string, expected_version: number) =>
    request<IssueCommand>(`api/issues/${encodeURIComponent(number)}/close`, {
      method: 'POST',
      body: jsonBody({ expected_version }),
    }),
  reopenIssue: (number: string, expected_version: number) =>
    request<IssueCommand>(`api/issues/${encodeURIComponent(number)}/reopen`, {
      method: 'POST',
      body: jsonBody({ expected_version }),
    }),
  transferConsultant: (
    number: string,
    body: components['schemas']['TransferConsultantRequest'],
  ) =>
    request<IssueCommand>(`api/issues/${encodeURIComponent(number)}/transfer-consultant`, {
      method: 'POST',
      body: jsonBody(body),
    }),
  transferDevTeam: (
    number: string,
    body: components['schemas']['TransferDevTeamRequest'],
  ) =>
    request<IssueCommand>(`api/issues/${encodeURIComponent(number)}/transfer-dev-team`, {
      method: 'POST',
      body: jsonBody(body),
    }),
  inspection: (team_kinds?: Array<'consult_queue' | 'dev'>) =>
    request<components['schemas']['IssueInspectionResponse']>('api/issues/inspection/run', {
      method: 'POST',
      body: jsonBody(team_kinds ? { team_kinds } : {}),
    }),
  statusQueue: () => request<StatusQueue>('api/status-queue'),
  adminOverview: () => request<AdminOverview>('api/admin/overview'),
  users: (query = '') =>
    request<{ items: ManagedUser[] }>(`api/admin/users?q=${encodeURIComponent(query)}`),
  createUser: (body: components['schemas']['UserCreateRequest']) =>
    request<ManagedUser>('api/admin/users', { method: 'POST', body: jsonBody(body) }),
  updateUser: (id: string, body: components['schemas']['UserUpdateRequest']) =>
    request<ManagedUser>(`api/admin/users/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      body: jsonBody(body),
    }),
  teams: () => request<{ items: ManagedTeam[] }>('api/admin/teams'),
  createTeam: (body: components['schemas']['TeamCreateRequest']) =>
    request<ManagedTeam>('api/admin/teams', { method: 'POST', body: jsonBody(body) }),
  updateTeam: (id: string, body: components['schemas']['TeamUpdateRequest']) =>
    request<ManagedTeam>(`api/admin/teams/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      body: jsonBody(body),
    }),
  deleteTeam: (id: string) =>
    request<components['schemas']['AdminTeamDeleteResponse']>(
      `api/admin/teams/${encodeURIComponent(id)}`,
      { method: 'DELETE' },
    ),
  deleteIssue: (number: string) =>
    request<components['schemas']['AdminIssueDeleteResponse']>(
      `api/admin/issues/${encodeURIComponent(number)}`,
      { method: 'DELETE' },
    ),
  updateInspectionRule: (
    id: string,
    body: components['schemas']['TeamInspectionRuleUpdateRequest'],
  ) =>
    request<ManagedTeam>(`api/admin/teams/${encodeURIComponent(id)}/inspection-rule`, {
      method: 'PATCH',
      body: jsonBody(body),
    }),
  setMembership: (
    id: string,
    body: components['schemas']['MembershipUpdateRequest'],
  ) =>
    request<ManagedTeam>(`api/admin/teams/${encodeURIComponent(id)}/members`, {
      method: 'POST',
      body: jsonBody(body),
    }),
  removeMembership: (teamId: string, userId: string) =>
    request<ManagedTeam>(
      `api/admin/teams/${encodeURIComponent(teamId)}/members/${encodeURIComponent(userId)}`,
      { method: 'DELETE' },
    ),
  bindChannel: (id: string, chatid: string) =>
    request<{ team_id: string; chatid: string }>(
      `api/admin/teams/${encodeURIComponent(id)}/channel`,
      { method: 'POST', body: jsonBody({ chatid }) },
    ),
  schedule: () => request<Schedule>('api/admin/inspection/schedule'),
  updateSchedule: (times: string[]) =>
    request<Schedule>('api/admin/inspection/schedule', {
      method: 'PATCH',
      body: jsonBody({ times }),
    }),
}

export { APP_BASE }
