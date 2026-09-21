# 客户问题流转机器人

这是企业微信客户问题流转机器人的 MVP 实现。当前代码完成了：

- 追加式事件时间线、团队路由、权限、乐观版本控制和纠错记录；
- 客户资料、三档紧急程度、时间线变更审计和清晰的投递/处理状态；
- 文本与图文混排的有序内容片段、图片转存记录和私有对象读取；
- PostgreSQL 待投递束、逐项幂等 `req_id`、失败重试与延迟状态切换；
- 企业微信智能机器人长连接适配器：认证、心跳/重连、入站文本与图文混排、图片下载解密、临时素材上传、主动推送和被动回复卡片；
- 可自动验证的假企业微信传输、消息草稿、引用标记解析、群聊 `@机器人` 校验与研发/咨询群自助绑定；
- 服务端渲染 H5（列表、详情、草稿建事件、跟进、关闭/重开、交接 API）及企业微信 `snsapi_base` OAuth 会话骨架；
- H5 组织与授权管理中心：总管理员初始化、成员录入、咨询队列/研发团队创建、经办人授权、团队管理员设置和团队群聊绑定；
- PostgreSQL、MinIO、web、bot-worker 的 Docker Compose 本地运行形态。

## 本地启动

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
pytest
```

## 本地 Docker 环境

```bash
cp .env.example .env
docker compose up --build
```

这会启动 PostgreSQL、MinIO、web 和用于本地模拟的 bot-worker。`WECOM_TRANSPORT=fake` 仅用于本地自动化验证，不能连接或发送企业微信消息。

## 远端部署记录

当前远端实例的 SSH 入口、release 与服务路径、数据位置、清理状态及更新步骤见[远端部署速查](REMOTE_DEPLOYMENT.md)（最后核对：2026-09-20）。

升级到事件追踪工作台时，独立的一次性 Alembic 迁移会清空升级前的事件、时间线、消息、草稿、投递、回调和媒体记录，并删除对象存储 `media/` 前缀下的全部对象（包括版本）；人员、团队、成员授权、研发/咨询群绑定和登录会话会保留。该清理不可回滚。已有实例升级前先停止旧版 web/worker，再运行迁移并启动新版本，避免清理期间仍有进程写入旧事件。Docker Compose 会先运行 `migrate` 再启动 web 和 worker；systemd 部署需一并安装 `deploy/systemd/kefu-migrate.service`，并在升级时先执行 `systemctl stop kefu-web kefu-worker`，再启动这两个服务。其他部署方式需在停掉 web/worker 后运行一次 `alembic upgrade head`。使用 S3 时，迁移凭据需能列举并删除 `media/` 前缀下的对象版本。

## 企业微信真实链路

长连接采用企业微信智能机器人的 API 模式，而不是 URL 回调。需要在企业微信管理端把机器人配置为“使用长连接”，并取得 **Bot ID** 和该机器人的 **Secret**；这两个值不同于 H5 自建应用的 `WECOM_CORP_SECRET`。本项目使用企业微信维护的 [Python 长连接 SDK](https://github.com/WecomTeam/wecom-aibot-python-sdk) 处理认证、心跳、重连和媒体解密。

首次试点的最小步骤如下：

1. 把真实值写入部署机器的 `.env`（不要提交）：

   ```dotenv
   WECOM_TRANSPORT=long_connection
   WECOM_BOT_ID=...
   WECOM_BOT_SECRET=...
   WECOM_BOT_MENTION_NAME=客户问题机器人
   WEB_BASE_URL=https://events.example.com
   ```

2. 用人工维护的通讯录/团队配置初始化路由。`channels` 可以为空，因为群 `chatid` 会在下一步由机器人回调取得：

   ```bash
   kefu-bootstrap routing.example.json
   ```

3. 将机器人加入目标群后，由研发团队管理员在研发群发送 `@机器人 绑定研发团队 平台研发组`；由咨询队列管理员在咨询群发送 `@机器人 绑定咨询队列 华东咨询队列`。名称必须与组织管理页中的团队名称完全一致。成功后，当前群会成为该团队的唯一活动通道，旧通道停用。

4. 启动 `web` 和 `bot-worker`。咨询人员先单聊机器人发送文本或图文混排；机器人会把草稿确认卡片回到原会话。创建事件后，worker 以持久化的 `req_id` 向研发群发送可引用的 Markdown 普通消息，包含事件编号、当次发言和历史入口，图片作为附件补发；研发引用事件消息并 `@机器人` 后，回复会私聊当前咨询经办人，并在咨询队列已绑定群聊时同步发送到该群。

### 首次初始化总管理员

总管理员是企业微信成员身份，不使用额外密码。部署时可以在 `.env` 设置
`INITIAL_ADMIN_WECOM_USERID` 和 `INITIAL_ADMIN_DISPLAY_NAME`，web 进程启动时会幂等创建/提升该身份；也可以设置一次性的 `ADMIN_BOOTSTRAP_TOKEN`，打开 H5 的 `/setup` 页面完成初始化。进入 `/admin` 后，成员、咨询队列、研发责任团队、团队角色和研发/咨询群绑定均可通过可视化表单维护，不再需要手工编辑路由 JSON。

生产环境还需要：

- `DATABASE_URL`、S3/MinIO 的 `S3_*` 配置；
- `AUTH_MODE=wecom_oauth`、`WEB_BASE_URL`、`WECOM_CORP_ID`、`WECOM_AGENT_ID` 和 `WECOM_CORP_SECRET`；
- 让 `WEB_BASE_URL` 成为企业微信 OAuth/模板卡片信任的 HTTPS 域名；
- `WECOM_BOT_ID`、`WECOM_BOT_SECRET`，以及推荐配置的 `WECOM_BOT_MENTION_NAME`。如果平台回调提供明确的 @ 标识，后者不是必须；否则它用于严格拒绝未 @ 机器人的群聊消息。

长连接模式不需要 URL 回调的 Token 或 EncodingAESKey。企业微信机器人凭据和真实企业成员信息都不会写进仓库；未配置 Bot ID/Secret 时，worker 会在启动阶段明确失败，而不会错误地回退到模拟传输或 URL 回调。

### 延迟被动回复实验

如需验证“研发群 `@机器人` 后先保存回调 `req_id`，等咨询侧回复后再用原回调帧被动回复”的行为，可在 worker 环境中临时开启：

```dotenv
WECOM_DEFERRED_PASSIVE_REPLY_ENABLED=true
WECOM_DEFERRED_PASSIVE_REPLY_TTL_SECONDS=300
```

实验支持两种入口：研发群消息引用带事件编号的机器人文字消息，或触发“请引用机器人发送的文字片段后再 @机器人 回复”的无引用测试消息。收到同一事件的咨询侧引用回复后，worker 会把咨询侧文字通过原研发群回调上下文发送；无引用入口只有在当前没有其他无引用待处理项时才会关联，避免猜测消息归属。回调上下文会短期保存到数据库，包含原始回调 frame 和过期时间，可跨 worker 重启读取；超过 TTL 后不会尝试使用旧 `req_id`。该开关默认关闭。
