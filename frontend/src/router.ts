import { createRouter, createWebHistory } from 'vue-router'
import { useSessionStore } from '@/stores/session'
import AdminPage from '@/views/AdminPage.vue'
import DraftReviewPage from '@/views/DraftReviewPage.vue'
import IntakePage from '@/views/IntakePage.vue'
import IssueDetailPage from '@/views/IssueDetailPage.vue'
import IssueListPage from '@/views/IssueListPage.vue'
import LoginPage from '@/views/LoginPage.vue'
import PrivateIntakePage from '@/views/PrivateIntakePage.vue'
import SetupPage from '@/views/SetupPage.vue'
import StatusQueuePage from '@/views/StatusQueuePage.vue'

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    { path: '/', redirect: '/issues' },
    { path: '/login', component: LoginPage, meta: { public: true } },
    { path: '/setup', component: SetupPage, meta: { public: true } },
    { path: '/issues/private-intake', component: PrivateIntakePage, meta: { public: true } },
    { path: '/issues/private-intake/continue', component: PrivateIntakePage, meta: { public: true } },
    { path: '/issues', component: IssueListPage },
    { path: '/issues/new', component: IntakePage },
    { path: '/issues/drafts/:draftId', component: DraftReviewPage },
    { path: '/issues/:issueNumber', component: IssueDetailPage },
    { path: '/status-queue', component: StatusQueuePage },
    { path: '/admin', component: AdminPage },
    { path: '/settings/routing', redirect: '/admin' },
    { path: '/events', redirect: '/issues' },
    { path: '/events/new', redirect: '/issues/new' },
    { path: '/events/:issueNumber', redirect: (to) => `/issues/${to.params.issueNumber}` },
    { path: '/legacy/events/new', redirect: '/issues/new' },
    { path: '/:pathMatch(.*)*', redirect: '/issues' },
  ],
  scrollBehavior: () => ({ top: 0 }),
})

router.beforeEach(async (to) => {
  const session = useSessionStore()
  if (to.meta.public) {
    if (to.path === '/login' || to.path === '/setup') return true
    const current = await session.load()
    if (current.setup_required) return { path: '/setup' }
    if (!current.authenticated) return { path: '/login', query: { next: to.fullPath } }
    return true
  }

  const current = await session.load()
  if (current.setup_required) return { path: '/setup' }
  if (!current.authenticated) return { path: '/login', query: { next: to.fullPath } }
  if (to.path === '/admin' && !current.is_global_admin) return { path: '/issues' }
  return true
})

export default router
