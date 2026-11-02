import {
  api,
  bindImageUploader,
  escapeHtml as h,
  fetchBlob,
  formValues,
  formatDate,
  localDateTime,
  navigate,
  setPage,
  showFlash,
  url,
} from "./client.js";

const root = document.getElementById("app");
const path = currentPath();
const params = new URLSearchParams(window.location.search);
const privateIntakeStorageKey = "kefu_private_intake_token";
const privateIntakeMode = path === "/issues/new" && params.get("private_intake") === "1";
if (path === "/issues/new" && !privateIntakeMode) {
  sessionStorage.removeItem(privateIntakeStorageKey);
}
const issueNumber = path.match(/^\/issues\/([^/]+)$/)?.[1] || "";
const draftId = path.match(/^\/issues\/drafts\/([^/]+)$/)?.[1] || "";

function currentPath() {
  const basePath = new URL(document.querySelector("base").href).pathname.replace(/\/$/, "");
  const pathname = window.location.pathname;
  return pathname.startsWith(basePath) ? pathname.slice(basePath.length) || "/" : pathname;
}

function fail(error) {
  root.innerHTML = `<main class="panel"><h1>页面加载失败</h1><p class="notice">${h(error?.message || "请刷新页面后重试。")}</p><a class="button-link" href="${url("/issues")}">返回问题列表</a></main>`;
}

function valueLabel(value, labels = {}) {
  return labels[value] || value || "未填写";
}

function dateTime(value) {
  return h(formatDate(value));
}

function priorityBadge(priority) {
  const labels = { normal: "普通", urgent: "紧急", severe: "严重" };
  return `<span class="status-badge priority-${h(priority)}">${h(valueLabel(priority, labels))}</span>`;
}

function deadlineBadge(status) {
  const labels = { approaching: "临近期限", overdue: "已过期" };
  if (!status || status === "normal") return "";
  return `<span class="status-badge deadline-${h(status)}">${h(valueLabel(status, labels))}</span>`;
}

function workflowBadge(issue) {
  const labels = {
    pending_confirmation: "待确定",
    in_progress: "处理中",
    waiting_customer: "待客户反馈",
    closed: "关闭",
    suspended: "挂起",
  };
  const css = {
    pending_confirmation: "case-status-pending",
    in_progress: "case-status-progress",
    waiting_customer: "case-status-feedback",
    closed: "status-closed",
    suspended: "case-status-suspended",
  }[issue.workflow_status || issue.status] || "";
  const open = issue.lifecycle_status === "open";
  return `<span class="status-badge ${open ? "status-open" : "status-closed"}">` +
    `${open ? "进行中" : "已关闭"}</span><span class="status-badge ${css}">` +
    `${h(valueLabel(issue.workflow_status || issue.status, labels))}</span>`;
}

function issueStatusClass(status) {
  return {
    "问题未回复": "issue-status-no-reply",
    "已解决": "issue-status-resolved",
    "数据待处理": "issue-status-pending-info",
    "升级需跟进": "issue-status-upgrade-follow-up",
    "需求待受理": "issue-status-request-pending",
    "当前无法解决": "issue-status-unresolvable",
  }[status] || "";
}

function issueStatusBadge(status) {
  return `<span class="status-badge issue-current-status ${issueStatusClass(status)}">${h(status || "问题未回复")}</span>`;
}

function header({ title, eyebrow = "H5 问题管理中心", nav = "" }) {
  return `<header class="topbar"><div><div class="eyebrow">${h(eyebrow)}</div><h1>${h(title)}</h1></div><nav>${nav}</nav></header>`;
}

function listNav(context, active) {
  const tabs = [
    ["open", "问题列表"],
    ["mine", "我的问题"],
    ["closed", "已关闭"],
  ].map(([state, label]) => `<a class="${active === state ? "active" : ""}" href="${url(`/issues?state=${state}`)}">${label}</a>`).join("");
  return `${tabs}<a href="${url("/status-queue")}">异步任务队列</a>` +
    `${context.is_global_admin ? `<a href="${url("/admin")}">组织与授权</a>` : ""}` +
    `${context.can_create_issue ? `<a class="nav-create" href="${url("/issues/new")}">＋ 新建问题</a>` : ""}`;
}

function flashNoticeFromUrl() {
  const notice = params.get("notice");
  const messages = {
    missing_consult_queue: "当前账号没有可用的咨询队列，或咨询队列尚未绑定企业微信群聊，暂时无法新建问题。已为你打开问题列表。",
    missing_dev_team: "当前没有已绑定群聊的研发受理组，暂时无法新建问题。已为你打开问题列表。",
  };
  if (notice && messages[notice]) showFlash(messages[notice]);
  if (params.has("notice")) {
    params.delete("notice");
    history.replaceState(null, "", `${window.location.pathname}${params.size ? `?${params}` : ""}${window.location.hash}`);
  }
}

function inspectionTrigger(context, className = "primary inspection-run-button") {
  const action = context.is_global_admin
    ? 'data-inspection-open="true"'
    : 'data-action="inspect"';
  return `<button class="${className}" type="button" ${action}>立刻巡检</button>`;
}

function inspectionDialog(context, { includeTrigger = true } = {}) {
  if (!context.can_inspect) return "";
  const trigger = includeTrigger ? inspectionTrigger(context) : "";
  const status = '<p class="muted inspection-run-status" data-inspection-status="true" role="status" aria-live="polite" hidden></p>';
  if (!context.is_global_admin) return `${trigger}${status}`;
  const options = `<fieldset><legend>群聊类型（至少选择一种）</legend>
        <label class="inspection-kind-option"><input type="checkbox" name="team_kinds" value="consult_queue" checked> 咨询团队</label>
        <label class="inspection-kind-option"><input type="checkbox" name="team_kinds" value="dev" checked> 研发团队</label></fieldset>`;
  return `${trigger}<dialog class="inspection-dialog" data-inspection-dialog="true">
      <h2>选择巡检群聊类型</h2><p>每个群仍按该团队设置的巡检规则筛选问题。</p>${options}
      <p class="inspection-choice-error form-error" hidden>请至少选择一种群聊类型。</p>
      <div class="inspection-dialog-actions"><button type="button" data-inspection-cancel="true">取消</button>
      <button class="primary" type="button" data-inspection-submit="true">开始巡检</button></div>
    </dialog>${status}`;
}

function renderIssueCard(issue) {
  const ref = encodeURIComponent(issue.issue_number);
  const autoAccept = issue.lifecycle_status === "open" && ["pending_confirmation", "waiting_customer", "suspended"].includes(issue.workflow_status) && (issue.workflow_status !== "pending_confirmation" || issue.can_accept);
  const content = `<div class="case-card-top"><strong class="case-card-title">${h(issue.title)}</strong>
    <div class="case-card-badges">${priorityBadge(issue.priority)}${workflowBadge(issue)}${deadlineBadge(issue.deadline_status)}</div></div>
    <div class="case-customer">客户 / 企业：${h(issue.customer_name || "未录入")}</div>
    <div class="case-card-meta"><span class="case-ref">${h(issue.bug_number || "编号待生成")}</span>
      <span>截止 ${dateTime(issue.deadline)}</span><span>最近更新 ${dateTime(issue.updated_at)}</span></div>`;
  return autoAccept
    ? `<article class="case-card case-card-auto-accept" data-auto-accept="${h(ref)}"><button type="button" class="case-card-hitbox" aria-label="受理并打开问题 ${h(issue.bug_number || issue.issue_number)}"></button><div class="case-card-content">${content}</div></article>`
    : `<article class="case-card"><a class="case-card-link" href="${url(`/issues/${ref}`)}">${content}</a></article>`;
}

async function renderList() {
  setPage("问题中心", "event-page");
  const state = ["mine", "dev", "closed", "open"].includes(params.get("state")) ? params.get("state") : "open";
  const priority = params.get("priority") || "";
  const query = params.get("q") || "";
  const queryString = new URLSearchParams({ state });
  if (priority) queryString.set("priority", priority);
  if (query) queryString.set("q", query);
  const [page, context] = await Promise.all([
    api(`/api/issues?${queryString}`),
    api("/api/ui/context"),
  ]);
  const priorities = [["", "全部等级"], ["normal", "普通"], ["urgent", "紧急"], ["severe", "严重"]];
  root.innerHTML = `${header({ title: "问题工作台", nav: listNav(context, state) })}
    ${inspectionDialog(context)}
    <section class="filter-panel"><form class="issue-search-form" method="get" action="${url("/issues")}">
      <input type="hidden" name="state" value="${h(state)}"><input type="hidden" name="priority" value="${h(priority)}">
      <label class="sr-only" for="issue-search">搜索问题</label><input id="issue-search" type="search" name="q" value="${h(query)}" placeholder="搜索 BUG 编号或客户名称">
      <button type="submit">搜索</button>${query ? `<a class="button-link" href="${url(`/issues?state=${state}&priority=${encodeURIComponent(priority)}`)}">清除</a>` : ""}</form>
      <div class="filter-label">紧急程度</div><nav class="priority-filters">${priorities.map(([value, label]) => `<a class="${priority === value ? "active" : ""}" href="${url(`/issues?state=${state}&priority=${encodeURIComponent(value)}&q=${encodeURIComponent(query)}`)}">${label}</a>`).join("")}</nav>
    </section><main class="event-list">${page.items.length ? page.items.map(renderIssueCard).join("") : '<p class="empty">当前筛选下没有问题。</p>'}</main>`;
  flashNoticeFromUrl();
}

function imageUploader({ compact = false } = {}) {
  return compact
    ? `<div class="image-upload-control intake-image-control" data-image-uploader="true" data-max-image-bytes="20971520">
        <label for="issue-images">＋ 图片<input id="issue-images" class="intake-image-input" name="images" type="file" accept="image/*" multiple data-image-input="true" aria-label="添加问题图片"></label>
        <ul class="image-upload-list" data-image-list="true" aria-live="polite"></ul><p class="image-upload-error" data-image-error="true" role="alert" hidden></p></div>`
    : `<div class="image-upload-control" data-image-uploader="true" data-max-image-bytes="20971520">
        <label for="issue-images">问题图片（可拖拽、多选或粘贴，单张不超过 20 MiB）</label>
        <input id="issue-images" name="images" type="file" accept="image/*" multiple data-image-input="true" aria-describedby="issue-images-hint">
        <div class="image-drop-zone" data-image-drop-zone="true">将图片拖到此处，或用上方按钮选择文件</div>
        <p id="issue-images-hint" class="muted image-upload-hint">图片会按 [图片] 占位符顺序匹配；未匹配的多余图片会追加到末尾。</p>
        <ul class="image-upload-list" data-image-list="true" aria-live="polite"></ul><p class="image-upload-error" data-image-error="true" role="alert" hidden></p></div>`;
}

async function renderNewIssue() {
  setPage("上传问题材料", "issue-intake-page");
  const privateIntakeToken = privateIntakeMode
    ? sessionStorage.getItem(privateIntakeStorageKey)
    : null;
  if (privateIntakeMode && !privateIntakeToken) {
    root.innerHTML = `<main class="panel"><h1>私聊交接不可用</h1><p class="notice">请重新从机器人私聊打开交接卡片。</p><a class="button-link" href="${url("/issues/new")}">改为普通建单</a></main>`;
    return;
  }
  const intakeOptionsPath = privateIntakeMode
    ? "/api/issues/intake-options?private_intake=1"
    : "/api/issues/intake-options";
  const privateHeaders = privateIntakeMode
    ? { "X-Private-Intake-Token": privateIntakeToken }
    : {};
  const [options, context] = await Promise.all([
    api(intakeOptionsPath, { headers: privateHeaders }),
    api("/api/ui/context"),
  ]);
  if (!options.queues.length) {
    navigate("/issues?notice=missing_consult_queue");
    return;
  }
  if (!options.dev_teams.length) {
    navigate("/issues?notice=missing_dev_team");
    return;
  }
  const privateIntake = options.private_intake;
  if (privateIntakeMode && !privateIntake) {
    throw new Error("私聊交接内容不可用，请重新从机器人私聊打开交接卡片。");
  }
  if (options.wecom_group_context_enabled) await import("./wecom.js");
  const queueControl = options.queues.length === 1
    ? `<input type="hidden" name="consult_queue_id" value="${h(options.queues[0].id)}">`
    : `<label class="intake-queue"><span class="visually-hidden">咨询队列</span><select name="consult_queue_id" required aria-label="咨询队列"><option value="" disabled selected>选择队列</option>${options.queues.map((queue) => `<option value="${h(queue.id)}">${h(queue.name)}</option>`).join("")}</select></label>`;
  const images = privateIntake?.images?.map((_, index) => `<img class="intake-image" alt="私聊图片" data-private-image="${index}" loading="lazy">`).join("") || "";
  root.innerHTML = `<main class="create-main intake-page"><header class="page-heading intake-heading"><h1>新建问题</h1><div class="intake-heading-actions">${options.can_inspect ? inspectionTrigger(options, "primary") : ""}<a class="intake-back-link" href="${url("/issues")}">返回问题列表</a></div></header>
    ${inspectionDialog(options, { includeTrigger: false })}<form class="panel create-form intake-form" data-api-form="true" data-form-kind="issue-draft" data-private-intake="${privateIntake ? "true" : "false"}" enctype="multipart/form-data">
      ${privateIntake ? '<p class="muted">已带入机器人私聊内容及图片。提交后会进入现有问题整理和审核流程。</p>' : ""}
      <textarea name="pasted_text" required maxlength="50000" rows="8" autofocus aria-label="粘贴问题内容，也可将图片拖入此处" data-image-drop-zone="true" placeholder="粘贴问题内容；图片可拖入此处…">${h(privateIntake?.raw_text || "")}</textarea>
      <div class="private-intake-images">${images}</div><input type="hidden" name="source_chat_id" value=""><input type="hidden" name="source_chat_status" value="not_reported"><input type="hidden" name="source_chat_error" value="">
      <div class="intake-actions">${imageUploader({ compact: true })}${queueControl}<button class="primary" type="submit" data-issue-draft-submit="true">上传</button></div>
      <p class="draft-submit-status muted" data-issue-draft-status="true" role="status" aria-live="polite" hidden><span class="draft-loading-spinner" aria-hidden="true"></span>正在整理…</p></form></main>`;
  bindImageUploader(root);
  if (privateIntake) await loadPrivateIntakeImages(privateIntake.images, privateIntakeToken);
}

async function loadPrivateIntakeImages(images, token) {
  const results = await Promise.allSettled(images.map((image) =>
    fetchBlob(image.url, {
      headers: { "X-Private-Intake-Token": token },
    }).then((blob) => URL.createObjectURL(blob))));
  const failed = results.find((result) => result.status === "rejected");
  if (failed) {
    results.forEach((result) => {
      if (result.status === "fulfilled") URL.revokeObjectURL(result.value);
    });
    throw failed.reason;
  }

  const objectUrls = results.map((result) => result.value);
  root.querySelectorAll("[data-private-image]").forEach((image, index) => {
    image.src = objectUrls[index];
  });
  window.addEventListener("pagehide", () => {
    objectUrls.forEach((objectUrl) => URL.revokeObjectURL(objectUrl));
  }, { once: true });
}

function renderEntry(entry, number) {
  const sideLabels = { consult: "咨询侧", dev: "研发侧", system: "问题记录" };
  const kindLabels = {
    issue_created: "问题创建",
    formal_message: "正式消息",
    status_changed: "状态变化",
    metadata_changed: "资料更新",
    deadline_changed: "期限调整",
    consultant_transferred: "经办人转交",
    dev_team_transferred: "研发团队变更",
    issue_closed: "问题关闭",
    issue_reopened: "问题重新打开",
  };
  const sideClass = entry.side === "system" ? "system" : `side-${h(entry.side)}`;
  const deliveries = (entry.deliveries || []).map((delivery) => {
    const labels = { pending: "待发送", sent: "已发送", failed: "发送失败" };
    return `<span class="delivery-badge delivery-${h(delivery.status)}">${h(valueLabel(delivery.status, labels))}</span>`;
  }).join("");
  const parts = (entry.parts || []).map((part) => part.kind === "text"
    ? `<p class="part">${h(part.text || "")}</p>`
    : `<img alt="问题图片" loading="lazy" src="${url(`/issues/${encodeURIComponent(number)}/media/${encodeURIComponent(part.media_id)}`)}">`).join("");
  const description = entry.metadata?.description || entry.metadata?.reason || "";
  return `<article class="entry ${sideClass}"><div class="entry-heading"><div class="entry-author"><strong>${h(entry.actor_name)}</strong><span>${h(sideLabels[entry.side] || entry.side)} · ${h(valueLabel(entry.kind, kindLabels))}</span></div><time datetime="${h(entry.created_at)}">${dateTime(entry.created_at)}</time></div>
    <div class="entry-chips">${entry.intent === "sync" ? '<span class="entry-intent">仅同步</span>' : ""}${deliveries}</div>${description ? `<p class="entry-description">${h(description)}</p>` : ""}<div class="entry-content">${parts}</div></article>`;
}

function issueDetailControls(issue, context) {
  const number = encodeURIComponent(issue.issue_number);
  const apiBase = `/api/issues/${number}`;
  let output = "";
  if (issue.can_edit_metadata) {
    output += `<details class="metadata-editor panel"><summary>编辑客户资料与紧急程度</summary><form data-api-form="true" data-api="${apiBase}/metadata" data-method="PATCH">
      <input type="hidden" name="version" value="${issue.version}"><div class="form-row"><label>客户 / 企业名称<input name="customer_name" required maxlength="256" value="${h(issue.customer_name)}"></label><label>客户群聊名称（选填）<input name="customer_group_name" maxlength="256" value="${h(issue.customer_group_name)}"></label><label>联系人（选填）<input name="customer_contact_name" maxlength="256" value="${h(issue.customer_contact_name)}"></label></div>
      <div class="form-row"><label>联系方式（选填）<input name="customer_contact_method" maxlength="256" value="${h(issue.customer_contact_method)}"></label><label>紧急程度<select name="priority">${[["normal", "普通"], ["urgent", "紧急"], ["severe", "严重"]].map(([value, label]) => `<option value="${value}" ${issue.priority === value ? "selected" : ""}>${label}</option>`).join("")}</select></label></div><button class="primary" type="submit">保存变更</button></form></details>`;
  }
  if (issue.can_transfer) {
    const candidates = issue.transferable_consultants || [];
    output += `<section class="transfer-controls panel" aria-label="咨询经办人转交"><h2>咨询经办人转交</h2><p class="transfer-controls-intro">当前咨询经办人、咨询队列管理员或研发团队管理员可以转交；接收人必须是当前咨询队列中的在岗成员。</p><div class="transfer-control-grid"><div class="transfer-control-card"><h3>转交咨询处理人</h3><p>目标范围：${h(issue.consult_queue_name)}</p>${candidates.length ? `<form data-api-form="true" data-api="${apiBase}/transfer-consultant" data-method="POST"><input type="hidden" name="version" value="${issue.version}"><label>新的处理人<select name="new_consultant_id" required><option value="" selected disabled>请选择本团队成员</option>${candidates.map((person) => `<option value="${h(person.id)}">${h(person.display_name)}</option>`).join("")}</select></label><button class="primary" type="submit">转交咨询处理人</button></form>` : '<p class="transfer-control-empty">当前团队没有其他在岗成员可选。</p>'}</div></div></section>`;
  }
  if (issue.can_transfer_dev_team) {
    const teams = issue.transferable_dev_teams || [];
    output += `<section class="transfer-controls panel" aria-label="研发归属团队变更"><h2>研发归属团队变更</h2><p class="transfer-controls-intro">当前研发团队成员、当前咨询经办人、咨询队列管理员或总管理员可以变更；提交后会记录在时间线，并通知新旧研发群。</p><div class="transfer-control-grid"><div class="transfer-control-card"><h3>更改研发归属团队</h3><p>当前归属：${h(issue.current_dev_team_name)}；仅显示已启用并绑定研发群的团队。</p>${teams.length ? `<form data-api-form="true" data-api="${apiBase}/transfer-dev-team" data-method="POST"><input type="hidden" name="version" value="${issue.version}"><label>新的研发归属团队<select name="new_dev_team_id" required><option value="" selected disabled>请选择研发团队</option>${teams.map((team) => `<option value="${h(team.id)}">${h(team.name)}</option>`).join("")}</select></label><button class="primary" type="submit">更改研发归属团队</button></form>` : '<p class="transfer-control-empty">当前没有其他已绑定研发群的启用团队可选。</p>'}</div></div></section>`;
  }
  if (issue.can_extend_deadline && issue.lifecycle_status !== "closed") {
    output += `<section class="deadline-controls panel" aria-label="期限管理"><h2>期限管理</h2><p class="deadline-controls-intro">截止时间和临近期限提醒是两项独立设置，分别保存，互不改变。</p><div class="deadline-control-grid"><div class="deadline-control-card"><h3>调整当前截止时间</h3><p>选择新的截止日期和时间，不会修改临近期限提醒。</p><form data-api-form="true" data-api="${apiBase}/deadline" data-method="PATCH"><input type="hidden" name="version" value="${issue.version}"><label>新的截止日期和时间<input type="datetime-local" name="deadline" value="${localDateTime(issue.deadline)}" required></label><p class="muted">此操作只改变截止时间，不会改变临近期限提醒设置。</p><button class="primary" type="submit">保存截止时间</button></form></div><div class="deadline-control-card"><h3>设置临近期限提醒</h3><p>设置距截止时间多久时开始显示“临近期限”，不改变截止时间。</p><form data-api-form="true" data-api="${apiBase}/deadline/approaching-window" data-method="PATCH"><input type="hidden" name="version" value="${issue.version}"><label>提前多久进入临近期限（小时）<input type="number" name="approaching_hours" min="0.5" step="0.5" value="${Number(issue.approaching_window_minutes) / 60}" required></label><p class="muted">默认提前 24 小时，按至少 30 分钟递增。</p><p class="muted">此操作只改变临近期限提醒设置，不会调整截止时间。</p><button class="primary" type="submit">保存提醒设置</button></form></div></div></section>`;
  }
  if (issue.lifecycle_status !== "closed" && issue.can_change_consult_status) {
    output = `<form class="top-close-control" data-api-form="true" data-api="${apiBase}/close" data-method="POST" data-confirm="确认关闭该问题？"><input type="hidden" name="version" value="${issue.version}"><button type="submit" class="status-action-close">关闭问题</button></form>` + output;
  }
  if (context.is_global_admin && issue.lifecycle_status === "closed") {
    output = `<form class="content-delete-control" data-api-form="true" data-api="/api/admin/issues/${number}" data-method="DELETE" data-success-path="/issues?state=closed" data-confirm="永久删除该已关闭问题及其时间线和附件？此操作不可恢复。"><button type="submit" class="danger-button">删除已关闭问题</button></form>` + output;
  }
  return output;
}

async function renderIssueDetail() {
  setPage("问题详情", "case-detail-page");
  const [issue, context] = await Promise.all([
    api(`/api/issues/${encodeURIComponent(issueNumber)}`),
    api("/api/ui/context"),
  ]);
  document.title = issue.title;
  const showSide = context.is_global_admin;
  const statusValues = context.issue_statuses || ["问题未回复"];
  const statusControl = issue.can_edit_metadata
    ? `<form class="issue-status-editor" data-api-form="true" data-api="/api/issues/${encodeURIComponent(issueNumber)}/issue-current-status" data-method="PATCH"><input type="hidden" name="version" value="${issue.version}"><select name="issue_current_status" required aria-label="选择问题状态">${statusValues.map((value) => `<option ${issue.issue_current_status === value ? "selected" : ""}>${h(value)}</option>`).join("")}</select><button type="submit">保存状态</button></form>`
    : `<strong>${h(issue.issue_current_status)}</strong>`;
  const reopen = issue.lifecycle_status === "closed" && issue.can_change_consult_status
    ? `<form data-api-form="true" data-api="/api/issues/${encodeURIComponent(issueNumber)}/reopen" data-method="POST"><input type="hidden" name="version" value="${issue.version}"><label>重新打开后由谁处理<select name="waiting_on"><option value="dev">待研发处理</option><option value="consult">待咨询处理</option></select></label><button type="submit">重新打开</button></form>`
    : "";
  const message = issue.lifecycle_status !== "closed"
    ? `<section><h2>发送正式消息</h2><form data-api-form="true" data-api="/api/issues/${encodeURIComponent(issueNumber)}/messages" data-method="POST"><input type="hidden" name="version" value="${issue.version}"><label>内容<textarea name="text" required></textarea></label>${showSide ? '<label>发送身份<select name="side"><option value="consult">咨询</option><option value="dev">研发</option></select></label>' : ""}<label class="check-label"><input type="checkbox" name="intent" value="sync"> 仅同步进展</label><button class="primary" type="submit">发送</button></form></section>`
    : "";
  root.innerHTML = `<main class="case-main"><a class="back-link" href="${url("/issues")}">← 返回问题工作台</a>
    <header class="case-titlebar"><div><h1>${h(issue.title)}</h1><p class="muted">${h(issue.bug_number || issue.issue_number)}</p></div><div class="case-state-badges">${deadlineBadge(issue.deadline_status)}${priorityBadge(issue.priority)}${workflowBadge(issue)}</div></header>
    <section class="issue-fields panel"><h2>问题信息</h2><div class="issue-fields-grid"><div class="issue-field issue-field-wide"><span>问题描述</span><p>${h(issue.issue_description || issue.title)}</p></div><div class="issue-field"><span>初次受理组</span><strong>${h(issue.initial_acceptance_team_name || issue.current_dev_team_name)}</strong></div><div class="issue-field"><span>研发归属团队</span><strong>${h(issue.current_dev_team_name)}</strong></div><div class="issue-field"><span>怀疑类型（问题所属模块）</span><strong>${h(issue.suspected_type || "未填写")}</strong></div><div class="issue-field"><span>现状说明</span><p>${h(issue.current_situation || "未填写")}</p></div><div class="issue-field"><span>问题状态</span>${statusControl}</div><div class="issue-field"><span>问题初次过期时间</span><strong>${dateTime(issue.initial_expires_at || issue.deadline)}</strong></div><div class="issue-field"><span>BUG 编号</span><strong>${h(issue.bug_number || "不适用")}</strong></div><div class="issue-field"><span>首次记录人</span><strong>${h(issue.first_recorder_name)}</strong></div></div></section>
    <section class="case-overview panel"><div class="case-overview-heading"><h2>客户与责任信息</h2><div class="updated-at">最近更新 ${dateTime(issue.updated_at)}</div></div><div class="case-meta-grid"><div><span>客户 / 企业</span><strong>${h(issue.customer_name || "未录入")}</strong></div><div><span>客户群聊名称</span><strong>${h(issue.customer_group_name || "未获取")}</strong></div><div><span>咨询团队</span><strong>${h(issue.consult_queue_name)}</strong></div><div><span>创建时间</span><strong>${dateTime(issue.created_at)}</strong></div><div><span>截止时间</span><strong>${dateTime(issue.deadline)}</strong></div><div><span>临近期限提醒</span><strong>提前 ${Math.floor(issue.approaching_window_minutes / 60)}小时${issue.approaching_window_minutes % 60 ? `${issue.approaching_window_minutes % 60}分钟` : ""}</strong></div></div></section>
    ${issueDetailControls(issue, context)}<section class="timeline-section"><div class="section-heading"><h2>问题时间线</h2><span>${issue.entries.length} 条记录</span></div><div class="timeline">${issue.entries.map((entry) => renderEntry(entry, issueNumber)).join("")}</div></section>
    ${(message || reopen) ? `<section class="action-panel panel">${message}${reopen}</section>` : ""}</main>`;
}

function roleLabel(role) {
  return role === "admin" ? "团队管理员" : "成员";
}

function renderAdminUser(user) {
  const memberships = user.memberships?.length
    ? user.memberships.map((membership) => `<span class="tag ${membership.active ? "" : "tag-muted"}">${h(membership.team_kind === "dev" ? "研发团队" : "咨询团队")} · ${h(membership.team_name)} · ${h(roleLabel(membership.role))}</span>`).join("")
    : '<span class="muted">尚未加入团队</span>';
  return `<details class="managed-row"><summary><span class="avatar">${h(user.display_name.slice(0, 1))}</span><span class="user-summary"><strong>${h(user.display_name)}</strong><small>${h(user.wecom_userid)}</small></span><span class="badge ${user.active ? "badge-green" : "badge-gray"}">${user.active ? "在岗" : "已停用"}</span>${user.is_global_admin ? '<span class="badge badge-amber">总管理员</span>' : ""}<span class="chevron">⌄</span></summary><div class="managed-detail"><div class="tag-list">${memberships}</div><form class="edit-form" data-api-form="true" data-api="/api/admin/users/${h(user.id)}" data-method="PATCH"><label>显示名称<input name="display_name" required maxlength="256" value="${h(user.display_name)}"></label><label class="check-label"><input type="checkbox" name="active" ${user.active ? "checked" : ""}> 在岗，可登录</label><label class="check-label"><input type="checkbox" name="is_global_admin" ${user.is_global_admin ? "checked" : ""}> 总管理员</label><button type="submit">保存成员权限</button></form></div></details>`;
}

function renderTeam(team, users) {
  const activeMembers = team.members.filter((member) => member.active).length;
  const channelLabel = team.kind === "dev" ? "研发群" : "咨询群";
  const channel = `<div class="channel-box"><div><strong>${channelLabel}通道</strong>${team.channel_initialized ? `<span class="muted">当前群：${h(team.channel_chatid)}</span>` : `<span class="warning-text">尚未绑定${channelLabel}</span>`}</div><form class="inline-form" data-api-form="true" data-api="/api/admin/teams/${h(team.id)}/channel" data-method="POST"><input name="chatid" required maxlength="256" value="${h(team.channel_chatid || "")}" placeholder="粘贴企业微信群 chatid"><button type="submit">绑定</button></form></div>`;
  const members = team.members.length ? team.members.map((member) => `<div class="member-row"><div><span class="status-dot small ${member.active ? "" : "off"}"></span><span><strong>${h(member.display_name)}</strong><small>${h(member.wecom_userid)} · ${h(roleLabel(member.role))}</small></span></div><div class="member-actions"><form class="role-form" data-api-form="true" data-api="/api/admin/teams/${h(team.id)}/members" data-method="POST"><input type="hidden" name="user_id" value="${h(member.user_id)}"><select name="role"><option value="member" ${member.role === "member" ? "selected" : ""}>成员</option><option value="admin" ${member.role === "admin" ? "selected" : ""}>管理员</option></select><button class="subtle-button" type="submit">保存</button></form><button class="danger-button" type="button" data-api-delete="/api/admin/teams/${h(team.id)}/members/${h(member.user_id)}">移除</button></div></div>`).join("") : '<p class="empty">还没有授权成员。</p>';
  const memberOptions = users.filter((user) => user.active && !team.members.some((member) => member.user_id === user.id)).map((user) => `<option value="${h(user.id)}">${h(user.display_name)}（${h(user.wecom_userid)}）</option>`).join("") || '<option value="">请先添加在岗成员</option>';
  return `<article class="team-card"><div class="team-header"><div><span class="eyebrow">${team.kind === "dev" ? "研发团队" : "咨询团队"}</span><h3>${h(team.name)}</h3></div><span class="badge ${team.active ? "badge-green" : "badge-gray"}">${team.active ? "启用" : "已停用"}</span></div>
    <form class="team-edit-form" data-api-form="true" data-api="/api/admin/teams/${h(team.id)}" data-method="PATCH"><input name="name" required maxlength="256" value="${h(team.name)}">${team.kind === "dev" ? `<input name="lead_display_name" maxlength="256" value="${h(team.lead_display_name || "")}" placeholder="研发负责人展示名">` : ""}<label class="check-label"><input type="checkbox" name="active" ${team.active ? "checked" : ""}> 启用</label><button class="subtle-button" type="submit">保存</button></form>
    <button class="danger-button" type="button" data-api-delete="/api/admin/teams/${h(team.id)}" data-confirm="删除该团队，并同时删除其关联的已关闭问题及未确认会话？如果仍有未关闭问题引用，此操作会被拒绝。此操作不可恢复。">删除团队及已关闭问题</button>${channel}
    <form class="team-inspection-form" data-api-form="true" data-api="/api/admin/teams/${h(team.id)}/inspection-rule" data-method="PATCH" data-form-kind="inspection-rule"><strong>巡检筛选规则</strong><label class="inspection-age-toggle"><input type="checkbox" name="age_enabled" data-inspection-age-toggle="true" ${team.inspection_age_hours ? "checked" : ""}> 创建超过</label><label class="inspection-hours-label"><input type="number" name="age_hours" min="1" step="1" value="${team.inspection_age_hours || ""}" placeholder="小时" data-inspection-hours="true">小时且仍未关闭</label><label>问题归属<select name="own_only"><option value="true" ${team.inspection_own_only ? "selected" : ""}>仅本团队归属的问题</option><option value="false" ${!team.inspection_own_only ? "selected" : ""}>全部团队的问题</option></select></label><button class="subtle-button" type="submit">保存巡检规则</button></form>
    <div class="member-heading"><strong>授权成员</strong><span>${activeMembers} 人在岗</span></div><div class="member-list">${members}</div><form class="add-member-form" data-api-form="true" data-api="/api/admin/teams/${h(team.id)}/members" data-method="POST"><select name="user_id" required>${memberOptions}</select><select name="role"><option value="member">成员</option><option value="admin">团队管理员</option></select><button type="submit">授权加入</button></form></article>`;
}

async function renderAdmin() {
  setPage("组织与授权", "admin-page");
  const [overview, schedule, usersResult, teamsResult, context] = await Promise.all([
    api("/api/admin/overview"),
    api("/api/admin/inspection/schedule"),
    api("/api/admin/users"),
    api("/api/admin/teams"),
    api("/api/ui/context"),
  ]);
  const users = usersResult.items || [];
  const teams = teamsResult.items || [];
  const query = params.get("q") || "";
  const visibleUsers = users.filter((user) => !query || `${user.display_name} ${user.wecom_userid}`.toLowerCase().includes(query.toLowerCase()));
  root.innerHTML = `${header({ title: "组织与授权", nav: `<a href="${url("/issues")}">返回问题中心</a><a class="active" href="${url("/admin")}">管理设置</a>` })}<main class="admin-main">
    <section class="hero-banner"><div><span class="status-dot"></span><strong>权限配置已启用</strong><p>先录入企业微信成员，再将成员加入咨询团队或研发团队；团队管理员可以处理本团队的交接和群绑定。</p></div><span class="badge badge-blue">总管理员 · ${h(context.user_id.slice(0, 8))}</span></section>
    <section class="stats-grid"><div class="stat-card"><span>在岗成员</span><strong>${overview.active_user_count}</strong><small>共 ${overview.user_count} 个身份</small></div><div class="stat-card"><span>咨询团队</span><strong>${overview.consult_queue_count}</strong><small>按团队隔离可见范围</small></div><div class="stat-card"><span>研发团队</span><strong>${overview.dev_team_count}</strong><small>${overview.dev_team_count ? `${overview.initialized_dev_team_count}/${overview.dev_team_count} 个研发群已绑定` : "暂未创建研发团队"}</small></div><div class="stat-card"><span>总管理员</span><strong>${overview.global_admin_count}</strong><small>至少保留 1 名在岗管理员</small></div></section>
    <section class="panel inspection-settings" id="inspection-settings"><div class="panel-heading"><div><div class="eyebrow">巡检推送</div><h2>每日时间点</h2></div><span class="count-pill">上海时间</span></div><p class="panel-intro">定时巡检使用上海时区。输入一个或多个 HH:mm 时间点，以逗号分隔；至少保留一个且不能重复。</p><form class="inspection-schedule-form" data-api-form="true" data-api="/api/admin/inspection/schedule" data-method="PATCH"><label>每日巡检时间<input name="times" required value="${h(schedule.times.join(", "))}" placeholder="08:55, 11:30, 21:30"></label><button class="primary" type="submit">保存时间表</button></form></section>
    <section class="admin-grid"><div class="panel" id="users"><div class="panel-heading"><div><div class="eyebrow">Step 1</div><h2>成员与总权限</h2></div><span class="count-pill">${overview.user_count}</span></div><p class="panel-intro">企业微信 userid 用于 H5 登录、账号级权限及群聊作者匹配；群聊路由按群绑定侧别和问题标记决定。</p><form class="search-form" method="get" action="${url("/admin")}"><input name="q" value="${h(query)}" placeholder="搜索姓名或 userid"><button type="submit">搜索</button>${query ? `<a class="button-link" href="${url("/admin")}">清除</a>` : ""}</form><details class="add-box" open><summary>＋ 新增企业成员</summary><form class="form-grid" data-api-form="true" data-api="/api/admin/users" data-method="POST"><label>显示名称<input name="display_name" required maxlength="256" placeholder="例如 张三"></label><label>企业微信 userid<input name="wecom_userid" required maxlength="128" placeholder="例如 zhangsan"></label><label class="check-label"><input type="checkbox" name="is_global_admin"> 同时设为总管理员</label><button class="primary" type="submit">添加成员</button></form></details><div class="managed-list">${visibleUsers.length ? visibleUsers.map(renderAdminUser).join("") : '<p class="empty">没有匹配的成员。</p>'}</div></div>
      <div class="panel" id="teams"><div class="panel-heading"><div><div class="eyebrow">Step 2</div><h2>团队与授权</h2></div><span class="count-pill">${overview.team_count}</span></div><p class="panel-intro">咨询团队负责问题归属和咨询侧可见范围；研发团队负责问题路由，负责人姓名仅作展示，不关联成员账号。</p><details class="add-box" open><summary>＋ 新增团队</summary><form class="form-grid team-create-form" data-api-form="true" data-api="/api/admin/teams" data-method="POST"><label>团队类型<select name="kind" data-team-kind-select="true"><option value="consult_queue">咨询团队</option><option value="dev">研发团队</option></select></label><label>团队名称<input name="name" required maxlength="256" placeholder="例如 平台研发组"></label><label class="team-lead-display-name-field" hidden>研发负责人展示名<input name="lead_display_name" maxlength="256" data-team-lead-input="true" disabled placeholder="仅显示，不关联企业微信成员"></label><button class="primary" type="submit">创建团队</button></form></details><div class="managed-list">${teams.length ? teams.map((team) => renderTeam(team, users)).join("") : '<p class="empty">还没有团队，请先创建咨询团队或研发团队。</p>'}</div></div></section>
    <section class="permission-note"><strong>授权规则</strong><span>成员 = 可查看和处理团队问题；团队管理员 = 可在本团队内执行交接、转交和群聊绑定；总管理员 = 可管理全组织并查看全部问题。</span></section></main>`;
}

function renderStatusItem(item) {
  const labels = { pending: "待发送", processing: "处理中", retry: "等待重试", completed: "完成" };
  return `<article class="queue-card"><div class="queue-card-heading"><a href="${url(`/issues/${encodeURIComponent(item.issue_number)}`)}">${h(item.bug_number || item.issue_number)} · ${h(item.title)}</a><span class="queue-state queue-${h(item.state)}">${h(valueLabel(item.state, labels))}</span></div><p>客户：${h(item.customer_name || "未录入")} · 第 ${item.entry_sequence || "—"} 条消息 · 尝试 ${item.attempts} 次</p><p>下次尝试：${dateTime(item.next_attempt_at)}</p>${item.last_error ? `<p class="queue-error">${h(item.last_error)}</p>` : ""}</article>`;
}

function renderStatusChange(item) {
  return `<article class="status-change-card"><div><a href="${url(`/issues/${encodeURIComponent(item.issue_number)}`)}">${h(item.bug_number || item.issue_number)} · ${h(item.title)}</a><span class="status-change-field">${h(item.field)}</span></div><p>${h(item.from_status || "空")} → ${h(item.to_status || "空")}</p><small>${h(item.actor_name)} · ${dateTime(item.created_at)}</small></article>`;
}

function renderDeletion(item) {
  const outcome = item.outcome === "succeeded" ? "已完成" : item.outcome === "failed" ? "失败" : item.outcome;
  return `<article class="status-change-card"><div><strong>${h(item.target_label)}</strong><span class="status-change-field">${h(item.target_kind)}</span><span class="status-change-field">${h(outcome)}</span></div><p>${h(item.error_message || "")}</p><small>${h(item.actor_name)} · ${dateTime(item.created_at)}</small></article>`;
}

async function renderStatusQueue() {
  setPage("异步任务队列", "status-queue-page");
  const [queue, context] = await Promise.all([api("/api/status-queue"), api("/api/ui/context")]);
  const tasks = [...(queue.draft_tasks || []), ...(queue.items || [])];
  root.innerHTML = `<main class="status-queue-main">${header({ title: "异步任务队列", eyebrow: "问题工作台 · 后台任务", nav: `<a href="${url("/status-queue")}">立即刷新</a><a href="${url("/issues")}">问题工作台</a>${context.is_global_admin ? `<a href="${url("/admin")}">组织与授权</a>` : ""}` })}
    <p class="muted">这里显示 Agent 草稿整理、问题状态判定任务和管理员删除记录；页面每 5 秒刷新。</p><section class="queue-section panel"><div class="section-heading"><h2>当前任务</h2><span>显示 ${tasks.length} 条，最多各 100 条</span></div><div class="queue-stack">${tasks.length ? tasks.map((item) => item.type === "issue_draft" ? `<article class="queue-card"><div class="queue-card-heading"><strong>${h(item.title)}</strong><span class="queue-state queue-processing">处理中</span></div><p>${dateTime(item.started_at)}</p></article>` : renderStatusItem(item)).join("") : '<p class="empty">当前没有排队或执行中的任务。</p>'}</div></section>
    <section class="queue-section panel"><div class="section-heading"><h2>最近状态变化</h2><span>最多 50 条</span></div><div class="status-history">${queue.recent_status_changes.length ? queue.recent_status_changes.map(renderStatusChange).join("") : '<p class="empty">暂无状态变化记录。</p>'}</div></section>
    ${context.is_global_admin ? `<section class="queue-section panel"><div class="section-heading"><h2>最近删除记录</h2><span>最多 50 条</span></div><div class="status-history">${queue.recent_deletions.length ? queue.recent_deletions.map(renderDeletion).join("") : '<p class="empty">暂无删除记录。</p>'}</div></section>` : ""}</main>`;
  window.setTimeout(() => {
    if (currentPath() === "/status-queue" && !document.hidden) renderStatusQueue().catch(fail);
  }, 5000);
}

async function renderSetup() {
  setPage("初始化总管理员", "setup-page");
  const state = await api("/api/setup");
  if (state.initialized) {
    navigate("/admin");
    return;
  }
  const hint = state.requires_token
    ? "请输入部署时配置的初始化令牌。"
    : "开发模式使用与表单一致的 X-WeCom-UserId 请求头；生产环境必须先配置 ADMIN_BOOTSTRAP_TOKEN。";
  root.innerHTML = `<main class="setup-card"><div class="eyebrow">首次使用 · H5 问题管理中心</div><h1>初始化总管理员</h1><p class="muted">总管理员使用企业微信 userid 登录，不单独设置密码。初始化完成后，人员授权、团队配置和研发/咨询群绑定都可以在管理中心可视化完成。</p><div class="notice">${h(hint)}</div><form data-api-form="true" data-api="/api/setup" data-method="POST" data-form-kind="setup"><label>企业微信 userid<input name="wecom_userid" required maxlength="128" placeholder="例如 zhangsan"></label><label>显示名称<input name="display_name" required maxlength="256" value="问题中心总管理员"></label><label>初始化令牌<input name="token" type="password" autocomplete="off" placeholder="生产环境必填"></label><button class="primary" type="submit">完成初始化并进入管理中心</button></form></main>`;
}

async function renderLegacyCreate() {
  setPage("创建问题", "case-create-page");
  const options = await api("/api/issues/legacy-intake-options");
  const moduleSelect = options.modules.map((value) => `<option value="${h(value)}">${h(value)}</option>`).join("");
  const statusSelect = options.issue_statuses.map((value) => `<option value="${h(value)}">${h(value)}</option>`).join("");
  root.innerHTML = `<main class="create-main"><a class="back-link" href="${url("/issues")}">← 返回问题中心</a><header class="page-heading"><div class="eyebrow">H5 问题中心</div><h1>创建新问题</h1><p class="muted">填写问题和客户信息，补充图片后直接交给研发跟进。</p></header><form class="panel create-form" data-api-form="true" data-form-kind="legacy-create" enctype="multipart/form-data"><h2>问题信息</h2><label>问题标题<input name="title" required maxlength="512" placeholder="例如：订单提交后页面报错"></label><label>咨询经办显示名<input name="consult_handler_name" required maxlength="256" placeholder="可填写姓名或团队内使用的称呼"></label><label>问题描述<textarea name="description" required maxlength="20000" rows="6" placeholder="描述客户现象、发生时间、影响范围和复现步骤"></textarea></label><div class="form-row"><label>怀疑类型（问题所属模块）<select name="suspected_type" required>${moduleSelect}</select></label><label>问题状态<select name="issue_current_status" required>${statusSelect}</select></label></div>${imageUploader()}<label>客户 / 企业名称<input name="customer_name" required maxlength="256" placeholder="例如：杭州某某科技"></label><label>客户群聊名称（选填）<input name="customer_group_name" maxlength="256" placeholder="客户所在企业微信群聊名称"></label><div class="form-row"><label>联系人（选填）<input name="customer_contact_name" maxlength="256"></label><label>联系方式（选填）<input name="customer_contact_method" maxlength="256" placeholder="电话、邮箱或其他联系方法"></label></div><div class="form-row"><label>紧急程度<select name="priority"><option value="normal" selected>普通</option><option value="urgent">紧急</option><option value="severe">严重</option></select></label><label>咨询团队<select name="consult_queue_id" required><option value="" disabled selected>选择咨询团队</option>${options.queues.map((queue) => `<option value="${h(queue.id)}">${h(queue.name)}</option>`).join("")}</select></label></div><label>研发处理团队<select name="dev_team_id" required><option value="" disabled selected>选择研发处理团队</option>${options.dev_teams.map((team) => `<option value="${h(team.id)}">${h(team.name)}（经办人：${h(team.lead_display_name || team.name)}）</option>`).join("")}</select></label><label>截止时间（选填）<input name="deadline" type="datetime-local" step="60"></label><p class="muted">咨询经办显示名由本问题填写，独立于登录账号；研发经办显示名沿用团队设置。</p><p class="muted">描述文字必填，图片可选。截止时间按中国标准时间填写；留空时默认为创建后 3 小时。</p><button class="primary" type="submit">创建并发送给研发</button></form></main>`;
  bindImageUploader(root);
}

async function renderDraftReview() {
  setPage("确认问题", "issue-review-page");
  const draft = await api(`/api/issues/drafts/${encodeURIComponent(draftId)}`);
  const fields = draft.fields;
  const modules = ["问题模块未填写", "系统", "客户资料", "行情", "交易", "其他"];
  const moduleOptions = (draft.modules || modules).map((value) => `<option value="${h(value)}" ${fields.suspected_type === value ? "selected" : ""}>${h(value)}</option>`).join("");
  root.innerHTML = `<main class="create-main intake-review-page"><a class="back-link" href="${url("/issues/new")}">← 重新上传材料</a><form class="panel create-form" data-api-form="true" data-form-kind="draft-confirm"><label>问题标题<input name="issue_title" required maxlength="512" value="${h(fields.issue_title)}" placeholder="Agent 生成的简短问题概括"></label><label>转发正文<textarea name="issue_description" required maxlength="20000" rows="8">${h(fields.issue_description)}</textarea></label><p class="muted intake-review-hint">标题和正文会转发给初次受理组，图片按原顺序作为附件转发。</p><div class="intake-key-fields"><label>客户名称<input name="customer_name" maxlength="256" value="${h(fields.customer_name)}" placeholder="请输入客户名称"></label><div class="intake-readonly-field"><span>客户群聊名称</span><strong>${h(draft.source_group_name || "未获取")}</strong></div><label>初次受理组<select name="initial_acceptance_team_id" required><option value="">请选择初次受理组</option>${draft.dev_teams.map((team) => `<option value="${h(team.id)}" ${fields.initial_acceptance_team_id === team.id ? "selected" : ""}>${h(team.name)}</option>`).join("")}</select></label><label>怀疑类型<select name="suspected_type" required>${moduleOptions}</select></label></div><details class="panel intake-source"><summary>收到的材料</summary><div class="intake-source-content"><p class="muted">咨询队列：${h(draft.consult_queue_name)}</p>${draft.parts.map((part) => part.kind === "text" ? `<pre class="intake-part">${h(part.text)}</pre>` : `<img class="intake-image" src="${h(part.url)}" alt="问题图片附件">`).join("")}</div></details><input type="hidden" name="customer_contact_name" value="${h(fields.customer_contact_name)}"><input type="hidden" name="customer_contact_method" value="${h(fields.customer_contact_method)}"><input type="hidden" name="issue_current_status" value="${h(fields.issue_current_status)}"><input type="hidden" name="current_situation" value="${h(fields.current_situation)}"><input type="hidden" name="issue_source" value="${h(fields.issue_source)}"><input type="hidden" name="deadline" value="${h(fields.deadline)}"><button class="primary" type="submit">确认并推送给初次受理组</button></form></main>`;
}

async function collectDraftForm(form) {
  const formData = new FormData(form);
  if (form.dataset.privateIntake !== "true" && window.getWeComCustomerGroupId) {
    try {
      const chatId = await window.getWeComCustomerGroupId();
      formData.set("source_chat_id", chatId || "");
      formData.set("source_chat_status", window.wecomCustomerGroupLookupStatus || "unknown");
      formData.set("source_chat_error", window.wecomCustomerGroupLookupError || "sdk_error_field_empty");
    } catch (_) {
      formData.set("source_chat_id", "");
      formData.set("source_chat_status", "client_exception");
      formData.set("source_chat_error", "client_exception");
    }
  }
  return formData;
}

function bindEvents() {
  root.addEventListener("change", (event) => {
    if (event.target.matches('[data-team-kind-select="true"]')) {
      const form = event.target.form;
      const field = form?.querySelector(".team-lead-display-name-field");
      const input = form?.querySelector('[data-team-lead-input="true"]');
      const isDev = event.target.value === "dev";
      if (field) field.hidden = !isDev;
      if (input) input.disabled = !isDev;
    }
    if (event.target.matches('[data-inspection-age-toggle="true"]')) {
      const hours = event.target.form?.querySelector('[data-inspection-hours="true"]');
      if (hours) {
        hours.disabled = !event.target.checked;
        hours.required = event.target.checked;
      }
    }
  });

  root.addEventListener("click", async (event) => {
    const inspect = event.target.closest('[data-action="inspect"]');
    if (inspect) {
      const dialog = root.querySelector('[data-inspection-dialog="true"]');
      if (dialog && typeof dialog.showModal === "function") dialog.showModal();
      else runInspection(false).catch((error) => showFlash(error.message, { error: true }));
      return;
    }
    if (event.target.closest('[data-inspection-open="true"]')) {
      root.querySelector('[data-inspection-dialog="true"]')?.showModal();
      return;
    }
    if (event.target.closest('[data-inspection-cancel="true"]')) {
      root.querySelector('[data-inspection-dialog="true"]')?.close();
      return;
    }
    if (event.target.closest('[data-inspection-submit="true"]')) {
      const checked = Array.from(root.querySelectorAll('input[name="team_kinds"]:checked'), (input) => input.value);
      if (!checked.length) {
        const choiceError = root.querySelector('.inspection-choice-error');
        if (choiceError) choiceError.hidden = false;
        return;
      }
      runInspection(true, checked).catch((error) => showFlash(error.message, { error: true }));
      return;
    }
    const accept = event.target.closest("[data-auto-accept]");
    if (accept) {
      event.preventDefault();
      try {
        await api(`/api/issues/${accept.dataset.autoAccept}/open`, { method: "POST", body: {} });
        navigate(`/issues/${accept.dataset.autoAccept}`);
      } catch (error) {
        showFlash(error.message, { error: true });
      }
      return;
    }
    const remove = event.target.closest("[data-api-delete]");
    if (remove) {
      if (remove.dataset.confirm && !window.confirm(remove.dataset.confirm)) return;
      try {
        await api(remove.dataset.apiDelete, { method: "DELETE" });
        window.location.reload();
      } catch (error) {
        showFlash(error.message, { error: true });
      }
    }
  });

  root.addEventListener("submit", async (event) => {
    const form = event.target.closest('[data-api-form="true"]');
    if (!form) return;
    event.preventDefault();
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) return;
    const submit = form.querySelector('[type="submit"]');
    if (submit) submit.disabled = true;
    try {
      const kind = form.dataset.formKind;
      if (kind === "issue-draft") {
        const status = form.querySelector('[data-issue-draft-status="true"]');
        if (status) {
          status.hidden = false;
          status.lastChild.textContent = "正在整理…";
        }
        if (submit) submit.textContent = "正在上传…";
        const privateIntake = form.dataset.privateIntake === "true";
        const privateIntakeToken = privateIntake
          ? sessionStorage.getItem(privateIntakeStorageKey)
          : null;
        if (privateIntake && !privateIntakeToken) {
          throw new Error("私聊交接已失效，请重新从机器人私聊打开交接卡片。");
        }
        const response = await api(
          privateIntake ? "/api/issues/drafts?private_intake=1" : "/api/issues/drafts",
          {
            method: "POST",
            headers: privateIntake
              ? { "X-Private-Intake-Token": privateIntakeToken }
              : {},
            body: await collectDraftForm(form),
          },
        );
        if (privateIntake) sessionStorage.removeItem(privateIntakeStorageKey);
        navigate(`/issues/drafts/${response.draft_id}`);
        return;
      }
      if (kind === "legacy-create") {
        const formData = new FormData(form);
        const response = await api("/api/issues", { method: "POST", body: formData });
        navigate(`/issues/${response.issue_number}`);
        return;
      }
      if (kind === "draft-confirm") {
        const response = await api(`/api/issues/drafts/${encodeURIComponent(draftId)}/confirm`, {
          method: "POST",
          body: formValues(form),
        });
        navigate(`/issues/${response.issue_number}`);
        return;
      }
      if (kind === "setup") {
        await api("/api/setup", { method: "POST", body: formValues(form) });
        navigate("/admin");
        return;
      }
      let values = formValues(form);
      if (kind === "inspection-rule") {
        values = {
          age_hours: values.age_enabled ? Number(values.age_hours) : null,
          own_only: values.own_only === "true",
        };
      }
      if (kind === "deadline-adjust") {
        values = { ...values, deadline: `${values.deadline_date}T${values.deadline_time}` };
      }
      if (form.dataset.api.endsWith("/messages")) {
        values.intent = form.querySelector('[name="intent"]')?.checked ? "sync" : "handoff";
      }
      await api(form.dataset.api, {
        method: form.dataset.method || "POST",
        body: form.dataset.method === "DELETE" ? undefined : values,
      });
      if (form.dataset.successPath) navigate(form.dataset.successPath);
      else window.location.reload();
    } catch (error) {
      showFlash(error.message || "操作失败，请稍后重试。", { error: true });
      if (submit) submit.disabled = false;
    }
  });
}

async function runInspection(selectable, teamKinds = []) {
  const status = root.querySelector('[data-inspection-status="true"]');
  const submit = root.querySelector('[data-inspection-submit="true"]');
  if (status) {
    status.hidden = false;
    status.textContent = selectable ? "正在执行巡检…" : "正在巡检所管理的团队群聊…";
  }
  if (submit) submit.disabled = true;
  try {
    const result = await api("/api/issues/inspection/run", {
      method: "POST",
      body: selectable ? { team_kinds: teamKinds } : {},
    });
    if (status) status.textContent = `巡检完成：扫描 ${result.scanned_issues} 个未关闭问题，已加入推送队列 ${result.summary_messages} 条${selectable ? `，${result.unresolved_targets} 个群聊路由未解析` : ""}。`;
    root.querySelector('[data-inspection-dialog="true"]')?.close();
  } catch (error) {
    if (status) status.textContent = error.message || "巡检失败，请稍后重试。";
  } finally {
    if (submit) submit.disabled = false;
  }
}

async function renderPage() {
  if (path === "/setup") return renderSetup();
  if (path === "/issues" || path === "/" || path === "/settings/routing") return path === "/settings/routing" ? renderAdmin() : renderList();
  if (path === "/issues/new") return renderNewIssue();
  if (path === "/status-queue") return renderStatusQueue();
  if (path === "/admin") return renderAdmin();
  if (path === "/legacy/events/new") return renderLegacyCreate();
  if (draftId) return renderDraftReview();
  if (issueNumber) return renderIssueDetail();
  navigate("/issues");
}

bindEvents();
renderPage().catch(fail);
