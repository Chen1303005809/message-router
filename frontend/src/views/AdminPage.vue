<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { RouterLink } from 'vue-router'
import AlertMessage from '@/components/AlertMessage.vue'
import PageHeading from '@/components/PageHeading.vue'
import { api, type AdminOverview, type ManagedTeam, type ManagedUser, type Schedule } from '@/api/client'
import { messageOf } from '@/utils'

const overview = ref<AdminOverview | null>(null)
const users = ref<ManagedUser[]>([])
const allUsers = ref<ManagedUser[]>([])
const teams = ref<ManagedTeam[]>([])
const schedule = ref<Schedule | null>(null)
const loading = ref(true)
const saving = ref(false)
const error = ref('')
const notice = ref('')
const search = ref('')
const scheduleText = ref('')
const newUser = reactive({ display_name: '', wecom_userid: '', is_global_admin: false })
const newTeam = reactive({ kind: 'consult_queue' as 'consult_queue' | 'dev', name: '', lead_display_name: '' })
const teamEdits = reactive<Record<string, { name: string; lead_display_name: string; active: boolean }>>({})

function setError(value: unknown): void {
  error.value = messageOf(value)
  notice.value = ''
}

async function load(query = ''): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const [loadedOverview, loadedUsers, loadedAllUsers, loadedTeams, loadedSchedule] = await Promise.all([
      api.adminOverview(), api.users(query), api.users(), api.teams(), api.schedule(),
    ])
    overview.value = loadedOverview
    users.value = loadedUsers.items
    allUsers.value = loadedAllUsers.items
    teams.value = loadedTeams.items
    schedule.value = loadedSchedule
    scheduleText.value = loadedSchedule.times.join(', ')
    for (const team of loadedTeams.items) {
      teamEdits[team.id] = {
        name: team.name,
        lead_display_name: team.lead_display_name || '',
        active: team.active,
      }
    }
  } catch (caught) {
    setError(caught)
  } finally {
    loading.value = false
  }
}

async function perform(action: () => Promise<unknown>, success: string): Promise<void> {
  saving.value = true
  error.value = ''
  notice.value = ''
  try {
    await action()
    notice.value = success
    await load(search.value)
  } catch (caught) {
    setError(caught)
  } finally {
    saving.value = false
  }
}

async function createUser(): Promise<void> {
  await perform(() => api.createUser({ ...newUser }), '成员已添加。')
  newUser.display_name = ''
  newUser.wecom_userid = ''
  newUser.is_global_admin = false
}

async function createTeam(): Promise<void> {
  const body = {
    kind: newTeam.kind,
    name: newTeam.name,
    lead_display_name: newTeam.kind === 'dev' ? newTeam.lead_display_name : null,
  }
  await perform(() => api.createTeam(body), '团队已创建。')
  newTeam.name = ''
  newTeam.lead_display_name = ''
}

async function saveUser(event: SubmitEvent, user: ManagedUser): Promise<void> {
  const form = new FormData(event.currentTarget as HTMLFormElement)
  await perform(
    () => api.updateUser(user.id, {
      display_name: String(form.get('display_name') || ''),
      active: form.has('active'),
      is_global_admin: form.has('is_global_admin'),
    }),
    '成员信息已保存。',
  )
}

async function saveTeam(team: ManagedTeam): Promise<void> {
  const edit = teamEdits[team.id]
  const body = {
    name: edit.name,
    active: edit.active,
    ...(team.kind === 'dev' && edit.lead_display_name
      ? { lead_display_name: edit.lead_display_name }
      : {}),
  }
  await perform(() => api.updateTeam(team.id, body), '团队信息已保存。')
}

async function deleteTeam(team: ManagedTeam): Promise<void> {
  const accepted = window.confirm(
    `删除“${team.name}”并同时删除其关联的已关闭问题及未确认会话？仍有未关闭问题引用时会被拒绝。此操作不可恢复。`,
  )
  if (accepted) await perform(() => api.deleteTeam(team.id), '团队和可删除的已关闭问题已删除。')
}

async function saveMembership(event: SubmitEvent, team: ManagedTeam, userId?: string): Promise<void> {
  const form = new FormData(event.currentTarget as HTMLFormElement)
  const selectedUserId = userId || String(form.get('user_id') || '')
  const role = String(form.get('role') || 'member') as 'member' | 'admin'
  if (!selectedUserId) return
  await perform(() => api.setMembership(team.id, { user_id: selectedUserId, role }), '团队授权已保存。')
}

async function removeMembership(team: ManagedTeam, user: ManagedTeam['members'][number]): Promise<void> {
  if (!window.confirm(`将 ${user.display_name} 从“${team.name}”移除？`)) return
  await perform(() => api.removeMembership(team.id, user.user_id), '成员授权已移除。')
}

async function saveChannel(event: SubmitEvent, team: ManagedTeam): Promise<void> {
  const form = new FormData(event.currentTarget as HTMLFormElement)
  await perform(() => api.bindChannel(team.id, String(form.get('chatid') || '')), '群聊绑定已保存。')
}

async function saveInspectionRule(event: SubmitEvent, team: ManagedTeam): Promise<void> {
  const form = new FormData(event.currentTarget as HTMLFormElement)
  const enabled = form.has('age_limited')
  const hoursValue = String(form.get('age_hours') || '')
  await perform(
    () => api.updateInspectionRule(team.id, {
      age_hours: enabled ? Number(hoursValue) : null,
      own_only: String(form.get('own_only')) === 'true',
    }),
    '巡检筛选规则已保存。',
  )
}

async function saveSchedule(): Promise<void> {
  const times = scheduleText.value.split(',').map((part) => part.trim()).filter(Boolean)
  await perform(() => api.updateSchedule(times), '巡检时间表已保存。')
}

async function searchUsers(): Promise<void> {
  await load(search.value)
}

onMounted(() => void load())
</script>

<template>
  <PageHeading
    eyebrow="问题工作台 / 总管理员"
    title="组织与授权"
    description="管理企业成员、团队范围、群聊绑定和定时巡检规则。"
  >
    <RouterLink class="button button-soft" to="/issues">返回工作台</RouterLink>
  </PageHeading>

  <AlertMessage v-if="error" :message="error" />
  <div v-if="notice" class="alert alert-success">{{ notice }}</div>
  <div v-if="loading" class="loading-card">正在加载组织设置…</div>
  <template v-else>
    <section v-if="overview" class="stats-grid">
      <article class="stat-card"><span>在岗成员</span><strong>{{ overview.active_user_count }}</strong><small>共 {{ overview.user_count }} 个身份</small></article>
      <article class="stat-card"><span>咨询团队</span><strong>{{ overview.consult_queue_count }}</strong><small>按团队隔离可见范围</small></article>
      <article class="stat-card"><span>研发团队</span><strong>{{ overview.dev_team_count }}</strong><small>{{ overview.initialized_dev_team_count }} 个已绑定群聊</small></article>
      <article class="stat-card"><span>总管理员</span><strong>{{ overview.global_admin_count }}</strong><small>至少保留 1 名在岗管理员</small></article>
    </section>

    <section class="panel admin-schedule">
      <div class="section-heading"><div><div class="eyebrow">巡检推送</div><h2>每日时间点</h2></div><span class="count-pill">{{ schedule?.timezone || 'Asia/Shanghai' }}</span></div>
      <p class="muted">输入一个或多个 HH:mm 时间点，以逗号分隔；至少保留一个且不能重复。</p>
      <form class="inline-edit-form" @submit.prevent="saveSchedule">
        <label class="grow">每日巡检时间<input v-model="scheduleText" required placeholder="08:55, 11:30, 21:30" /></label>
        <button class="button button-primary" :disabled="saving">保存时间表</button>
      </form>
    </section>

    <div class="admin-grid">
      <section class="panel admin-panel" id="users">
        <div class="section-heading"><div><div class="eyebrow">Step 1</div><h2>成员与总权限</h2></div><span class="count-pill">{{ overview?.user_count || 0 }}</span></div>
        <p class="muted">登记企业微信 userid，授权成员加入咨询团队或研发团队。总管理员可以管理全部问题和组织设置。</p>
        <form class="search-row admin-search" @submit.prevent="searchUsers">
          <input v-model="search" placeholder="搜索姓名或 userid" />
          <button class="button button-soft">搜索</button>
          <button v-if="search" class="button button-ghost" type="button" @click="search = ''; searchUsers()">清除</button>
        </form>
        <details class="add-box" open>
          <summary>＋ 新增企业成员</summary>
          <form class="stack-form compact-form" @submit.prevent="createUser">
            <label>显示名称<input v-model="newUser.display_name" required maxlength="256" placeholder="例如 张三" /></label>
            <label>企业微信 userid<input v-model="newUser.wecom_userid" required maxlength="128" /></label>
            <label class="check-row"><input v-model="newUser.is_global_admin" type="checkbox" /> 同时设为总管理员</label>
            <button class="button button-primary" :disabled="saving">添加成员</button>
          </form>
        </details>
        <div class="managed-list">
          <details v-for="user in users" :key="user.id" class="managed-row">
            <summary>
              <span class="avatar">{{ user.display_name.slice(0, 1) }}</span>
              <span class="user-summary"><strong>{{ user.display_name }}</strong><small>{{ user.wecom_userid }}</small></span>
              <span class="badge" :class="user.active ? 'badge-green' : 'badge-gray'">{{ user.active ? '在岗' : '已停用' }}</span>
              <span v-if="user.is_global_admin" class="badge badge-amber">总管理员</span>
              <span class="chevron">⌄</span>
            </summary>
            <div class="managed-detail">
              <div class="tag-list">
                <span v-for="membership in user.memberships" :key="membership.team_id" class="tag" :class="{ 'tag-muted': !membership.active }">
                  {{ membership.team_kind === 'dev' ? '研发团队' : '咨询团队' }} · {{ membership.team_name }} · {{ membership.role === 'admin' ? '管理员' : '成员' }}
                </span>
                <span v-if="!user.memberships.length" class="muted">尚未加入团队</span>
              </div>
              <form class="edit-form" @submit.prevent="(event) => saveUser(event, user)">
                <label>显示名称<input name="display_name" required maxlength="256" :value="user.display_name" /></label>
                <label class="check-row"><input name="active" type="checkbox" :checked="user.active" /> 在岗，可登录</label>
                <label class="check-row"><input name="is_global_admin" type="checkbox" :checked="user.is_global_admin" /> 总管理员</label>
                <button class="button button-soft" :disabled="saving">保存成员权限</button>
              </form>
            </div>
          </details>
          <p v-if="!users.length" class="empty-inline">没有找到匹配成员。</p>
        </div>
      </section>

      <section class="panel admin-panel" id="teams">
        <div class="section-heading"><div><div class="eyebrow">Step 2</div><h2>团队与授权</h2></div><span class="count-pill">{{ overview?.team_count || 0 }}</span></div>
        <p class="muted">咨询团队负责问题归属和咨询侧可见范围；研发团队负责问题路由和研发群消息转发。</p>
        <details class="add-box" open>
          <summary>＋ 新增团队</summary>
          <form class="stack-form compact-form" @submit.prevent="createTeam">
            <label>团队类型<select v-model="newTeam.kind"><option value="consult_queue">咨询团队</option><option value="dev">研发团队</option></select></label>
            <label>团队名称<input v-model="newTeam.name" required maxlength="256" placeholder="例如 平台研发组" /></label>
            <label v-if="newTeam.kind === 'dev'">研发负责人展示名<input v-model="newTeam.lead_display_name" required maxlength="256" placeholder="仅显示，不关联企业微信成员" /></label>
            <button class="button button-primary" :disabled="saving">创建团队</button>
          </form>
        </details>

        <div class="team-list">
          <article v-for="team in teams" :key="team.id" class="team-card">
            <div class="team-card-heading">
              <div><div class="eyebrow">{{ team.kind === 'dev' ? '研发团队' : '咨询团队' }}</div><h3>{{ team.name }}</h3></div>
              <span class="badge" :class="team.active ? 'badge-green' : 'badge-gray'">{{ team.active ? '启用' : '已停用' }}</span>
            </div>
            <form class="team-edit-form" @submit.prevent="saveTeam(team)">
              <label>团队名称<input v-model="teamEdits[team.id].name" required maxlength="256" /></label>
              <label v-if="team.kind === 'dev'">负责人展示名<input v-model="teamEdits[team.id].lead_display_name" maxlength="256" /></label>
              <label class="check-row"><input v-model="teamEdits[team.id].active" type="checkbox" /> 启用</label>
              <button class="button button-soft" :disabled="saving">保存</button>
            </form>
            <button class="text-button danger-text" :disabled="saving" @click="deleteTeam(team)">删除团队及已关闭问题</button>

            <form class="inline-edit-form channel-form" @submit.prevent="(event) => saveChannel(event, team)">
              <label class="grow">{{ team.kind === 'dev' ? '研发群' : '咨询群' }} chatid
                <input name="chatid" required maxlength="256" :value="team.channel_chatid || ''" placeholder="粘贴企业微信群 chatid" />
              </label>
              <button class="button button-soft" :disabled="saving">绑定</button>
            </form>
            <div class="muted channel-status">
              {{ team.channel_initialized ? `已绑定：${team.channel_chatid}` : '尚未绑定群聊' }}
            </div>

            <form class="inspection-rule-form" @submit.prevent="(event) => saveInspectionRule(event, team)">
              <strong>巡检筛选规则</strong>
              <label class="check-row"><input name="age_limited" type="checkbox" :checked="team.inspection_age_hours !== null" /> 创建时间限制</label>
              <label>超过小时<input name="age_hours" type="number" min="1" step="1" :value="team.inspection_age_hours || ''" /></label>
              <label>问题归属<select name="own_only"><option value="true" :selected="team.inspection_own_only">仅本团队归属</option><option value="false" :selected="!team.inspection_own_only">全部团队</option></select></label>
              <button class="button button-ghost" :disabled="saving">保存规则</button>
            </form>

            <div class="member-heading"><strong>授权成员</strong><span>{{ team.members.filter((member) => member.active).length }} 人在岗</span></div>
            <div v-for="member in team.members" :key="member.user_id" class="member-row">
              <div class="member-identity">
                <span class="status-dot" :class="{ off: !member.active }" />
                <span><strong>{{ member.display_name }}</strong><small>{{ member.wecom_userid }} · {{ member.role === 'admin' ? '团队管理员' : '成员' }}</small></span>
              </div>
              <div class="member-actions">
                <form class="role-form" @submit.prevent="(event) => saveMembership(event, team, member.user_id)">
                  <select name="role"><option value="member" :selected="member.role === 'member'">成员</option><option value="admin" :selected="member.role === 'admin'">管理员</option></select>
                  <button class="button button-ghost" :disabled="saving">保存</button>
                </form>
                <button class="text-button danger-text" :disabled="saving" @click="removeMembership(team, member)">移除</button>
              </div>
            </div>
            <form class="add-member-form" @submit.prevent="(event) => saveMembership(event, team)">
              <select name="user_id" required>
                <option value="" disabled selected>选择在岗成员</option>
                <option v-for="user in allUsers.filter((item) => item.active)" :key="user.id" :value="user.id">
                  {{ user.display_name }}（{{ user.wecom_userid }}）
                </option>
              </select>
              <select name="role"><option value="member">成员</option><option value="admin">团队管理员</option></select>
              <button class="button button-soft" :disabled="saving">授权加入</button>
            </form>
          </article>
          <p v-if="!teams.length" class="empty-inline">还没有团队，请先创建一个咨询团队或研发团队。</p>
        </div>
      </section>
    </div>
  </template>
</template>
