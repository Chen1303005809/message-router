import { APP_BASE } from '@/api/client'

export function mediaPath(path: string): string {
  return `${APP_BASE}${path.replace(/^\/+/, '')}`
}

export function loginUrl(next = '/issues'): string {
  return `${mediaPath(`auth/login?next=${encodeURIComponent(mediaPath(next))}`)}`
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date)
}

export function dateTimeLocal(value: string | null | undefined): string {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const parts = new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date)
  return parts.replace(' ', 'T')
}

export function dateTimeApiValue(value: string): string {
  return value.length === 16 ? `${value}:00` : value
}

export function priorityLabel(value: string): string {
  return ({ normal: '普通', urgent: '紧急', severe: '严重' } as Record<string, string>)[value] || value
}

export function workflowLabel(value: string): string {
  return (
    {
      pending_confirmation: '待确认',
      in_progress: '处理中',
      waiting_customer: '等待客户',
      suspended: '已暂停',
      closed: '已关闭',
    } as Record<string, string>
  )[value] || value
}

export function deadlineLabel(value: string | null | undefined): string {
  return (
    { approaching: '临近期限', overdue: '已逾期' } as Record<string, string>
  )[value || ''] || ''
}

export function issueStatusOptions(): string[] {
  return ['问题未回复', '需求待受理', '已解决', '升级需跟进', '数据待处理', '当前无法解决']
}

export function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : '操作失败，请稍后重试。'
}
