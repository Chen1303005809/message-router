# 客户问题流转机器人

这是企业微信客户问题流转机器人的 MVP 实现。咨询人员把客户问题交给研发责任团队，团队负责人展示名只作说明，不关联企业微信成员账号。当前代码完成了：

- 追加式问题时间线、团队路由、权限、乐观版本控制和纠错记录；
- 客户资料、三档紧急程度、时间线变更审计和清晰的投递/处理状态；
- 文本与图文混排的有序内容片段、图片转存记录和私有对象读取；
- PostgreSQL 待投递束、逐项幂等 `req_id`、失败重试与延迟状态切换；
- 企业微信智能机器人长连接适配器：认证、心跳/重连、入站文本与图文混排、图片下载解密、临时素材上传、群主动推送和被动回复；
- 可自动验证的假企业微信传输、引用标记解析、群聊 `@机器人` 校验与研发/咨询群绑定；
- 已绑定咨询群中的新问题受理：`@机器人` 后由 Agent 整理图文消息中的文字，多轮引用提案补充或修改问题、研发群和必填模块；收到“确认派发”后才创建正式问题并通过持久化投递队列派发，原图按顺序作为附件保留；
- 问题管理、问题巡检、消息接收、消息推送和 Agent 五个可由 Python 程序直接调用的模块；Agent 通过 OpenAI Chat Completions function tools 调用相同接口；
- 服务端渲染 H5：进入 `/` 直接上传问题材料，Agent 生成可编辑草稿，确认后事务化创建问题和首条投递；问题沟通保留群聊引用、详情、跟进、关闭/重开和交接 API；
- 每天 08:55、11:30、21:30（上海时区）按当前研发责任团队汇总未关闭问题；每个有问题的团队群只收到一条仅含 BUG 编号和客户企业的消息，投递具有幂等键和失败重试；
- H5 组织与授权管理中心：总管理员初始化、成员与咨询/研发团队管理、团队角色维护、研发负责人展示名及团队群聊绑定；
- PostgreSQL、MinIO、web、bot-worker 的 Docker Compose 本地运行形态。

Agent 草稿需要在环境变量中配置 `AGENT_BASE_URL`、`AGENT_API_KEY` 和 `AGENT_MODEL`。`AGENT_BASE_URL` 使用 OpenAI 兼容接口的 API 基础地址，例如 `https://api.openai.com/v1`。图片只会保存为附件，不会发送给 Agent。初次过期时间默认为 3 小时，临期窗口默认为提前 24 小时，分别通过 `ISSUE_DEFAULT_EXPIRY_HOURS` 和 `ISSUE_APPROACHING_WINDOW_HOURS` 配置。未关闭问题汇总的三个推送时点固定为上海时间 08:55、11:30 和 21:30；没有未关闭问题的团队不发送消息。

面向咨询人员、研发人员和各级管理员的产品使用说明见[操作手册](USER_GUIDE.md)。
产品的业务背景、协作矛盾、核心问题和解决方案见[功能说明](PRODUCT_FUNCTION_DESCRIPTION.md)。

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

## 远端直接运行（不使用 Docker）

本项目没有单独的前端进程：H5 页面由 FastAPI 服务端渲染，页面和 API 一起由 `kefu-web` 提供，远端监听 `127.0.0.1:18000`；Nginx 负责 HTTPS 域名和 `/kefu/` 路径转发。企业微信机器人由不监听端口的 `kefu-worker` 后台进程处理。数据库、媒体存储和环境变量按服务器配置使用；当前实例使用 SQLite 与 `/var/lib/kefu/media`，环境文件位于 `/etc/kefu/`。

远端不需要启动 Docker。首次迁移到 Git 工作树及后续更新命令见[远端部署速查](REMOTE_DEPLOYMENT.md)。日常更新使用服务器上的虚拟环境、Alembic 迁移和 systemd 服务。

## 远端部署记录

当前远端实例的 SSH 入口、服务和数据路径、清理状态，以及从旧 release 上传方式迁移到 Git 工作树的步骤见[远端部署速查](REMOTE_DEPLOYMENT.md)（最后状态核对：2026-09-21）。

升级到问题追踪工作台时，独立的一次性 Alembic 迁移会清空升级前的问题、时间线、消息、草稿、投递、回调和媒体记录，并删除对象存储 `media/` 前缀下的全部对象（包括版本）；人员、团队、成员授权、研发/咨询群绑定和登录会话会保留。该清理不可回滚。已有实例升级前先停止旧版 web/worker，再运行迁移并启动新版本，避免清理期间仍有进程写入旧问题。Docker Compose 会先运行 `migrate` 再启动 web 和 worker；systemd 部署需一并安装 `deploy/systemd/kefu-migrate.service`，并在升级时先执行 `systemctl stop kefu-web kefu-worker`，再启动这两个服务。其他部署方式需在停掉 web/worker 后运行一次 `alembic upgrade head`。使用 S3 时，迁移凭据需能列举并删除 `media/` 前缀下的对象版本。

群聊流程升级迁移会增加咨询经办显示名、移除旧私聊草稿和机器人身份绑定字段，并把可路由的待处理个人投递改投对应咨询群。没有唯一活动咨询群的历史个人投递会标记失败，避免继续向个人发送。

## 企业微信真实链路

长连接采用企业微信智能机器人的 API 模式，而不是 URL 回调。需要在企业微信管理端把机器人配置为“使用长连接”，并取得 **Bot ID** 和该机器人的 **Secret**；这两个值不同于 H5 自建应用的 `WECOM_CORP_SECRET`。本项目使用企业微信维护的 [Python 长连接 SDK](https://github.com/WecomTeam/wecom-aibot-python-sdk) 处理认证、心跳、重连和媒体解密。

首次试点的最小步骤如下：

1. 把真实值写入部署机器的 `.env`（不要提交）：

   ```dotenv
   WECOM_TRANSPORT=long_connection
   WECOM_BOT_ID=...
   WECOM_BOT_SECRET=...
   WECOM_BOT_MENTION_NAME=客户问题机器人
   WEB_BASE_URL=https://issues.example.com
   ```

   机器人只在群聊中响应 `@机器人`。私聊回调会被忽略；群聊路由不读取发送人的 userid。H5 仍通过企业微信 OAuth 登录，并按成员和团队权限控制页面操作。

2. 可先用人工维护的通讯录/团队配置初始化路由。每个研发团队需配置纯文本 `lead_display_name` 作为研发经办显示名；它不绑定用户或授权。`channels` 可以为空，因为群 `chatid` 会在后续由机器人回调取得：

   ```bash
   kefu-bootstrap routing.example.json
   ```

3. 启动 `web` 并初始化总管理员（见下方说明）。总管理员在“组织与授权”中登记 H5 登录身份、姓名和团队角色；也可在此处创建团队。这些身份只用于 H5 登录和账号级权限。

4. 启动 `bot-worker` 并将机器人加入目标群。在目标群发送 `@机器人 绑定研发团队 平台研发组` 或 `@机器人 绑定咨询队列 华东咨询队列`。绑定命令只检查团队名称、团队类型和群聊绑定冲突，不检查发言人的 userid、成员关系或管理员角色。创建可往返转发的问题前必须绑定咨询群和研发群；H5 新建问题时填写咨询经办显示名，研发显示名沿用团队负责人字段。

   已绑定咨询群还支持群内直接受理新问题：成员发送 `@机器人` 并附上文字或图文混排，Agent 会按文字整理问题提案，列出缺失的模块或研发目标选项。成员引用包含会话标记的提案并 `@机器人`，可以多轮修改；只有回复固定口令 `确认派发` 且必填信息齐全后才创建问题。每轮提案需要在两小时内引用继续；同一咨询群中的任一成员均可接续，系统记录为咨询队列归属，不保存个人发言人身份。图片不做视觉理解，原始图文顺序会随正式问题一起投递。

   已有问题仍通过引用机器人问题消息并 `@机器人` 跟进。任一已绑定咨询侧或研发侧群中的成员都可以引用带唯一有效问题标记的机器人消息，系统按群绑定侧别记账并转发到问题当前归属的对侧群，不核对来源群是否对应该问题当前团队。咨询经办交接通知发送到咨询群。

排查群聊问题时，搜索 `[GROUP-TRACE]`。同一 `chatid_fp` 可关联群回调、绑定提交、路由结果和投递目标；`route_lookup` 会说明未命中的原因（例如群未登记、绑定未初始化、绑定停用或团队停用）。`web_app_configured` 与 `bot_worker_starting` 的 `database_target_fp` 应一致。指纹不会暴露原始群聊标识。

```bash
journalctl -u kefu-web -u kefu-worker -f | grep '\[GROUP-TRACE\]'
# Docker Compose 部署：
docker compose logs -f web bot-worker | grep '\[GROUP-TRACE\]'
```

如果看到 `bot_connection_displaced`，说明当前连接收到了同一 Bot ID 的新连接顶替问题。检查所有主机上的容器和 systemd 进程，确保只运行一个使用该 Bot ID 的 `bot-worker`；被顶替的 worker 会退出，避免自动重连后继续争抢连接。Docker Compose 的 worker 使用 `on-failure`，不会在这类正常退出后立即重启。

### 首次初始化总管理员

总管理员是企业微信成员身份，不使用额外密码。部署时可以在 `.env` 设置
`INITIAL_ADMIN_WECOM_USERID` 和 `INITIAL_ADMIN_DISPLAY_NAME`，web 进程启动时会幂等创建/提升该身份；也可以设置一次性的 `ADMIN_BOOTSTRAP_TOKEN`，打开 H5 的 `/setup` 页面完成初始化。进入 `/admin` 后，成员、咨询队列、研发责任团队、团队角色和研发/咨询群绑定均可通过可视化表单维护，不再需要手工编辑路由 JSON。

生产环境还需要：

- `DATABASE_URL`、S3/MinIO 的 `S3_*` 配置；
- `AUTH_MODE=wecom_oauth`、`WEB_BASE_URL`、`WECOM_CORP_ID`、`WECOM_AGENT_ID` 和 `WECOM_CORP_SECRET`；
- 让 `WEB_BASE_URL` 成为企业微信 OAuth/模板卡片信任的 HTTPS 域名；
- `WECOM_BOT_ID`、`WECOM_BOT_SECRET`，以及推荐配置的 `WECOM_BOT_MENTION_NAME`。如果平台回调提供明确的 @ 标识，后者不是必须；否则它用于严格拒绝未 @ 机器人的群聊消息。
- 每个可新建问题的咨询队列和每个可用研发团队都需要一个活动群聊绑定。群聊机器人操作不依赖成员 userid；H5 登录和账号级操作继续由企业微信 OAuth 与团队权限控制。

长连接模式不需要 URL 回调的 Token 或 EncodingAESKey。企业微信机器人凭据和真实企业成员信息都不会写进仓库；未配置 Bot ID/Secret 时，worker 会在启动阶段明确失败，而不会错误地回退到模拟传输或 URL 回调。

### 延迟被动回复实验

如需验证“研发群 `@机器人` 后先保存回调 `req_id`，等咨询侧回复后再用原回调帧被动回复”的行为，可在 worker 环境中临时开启：

```dotenv
WECOM_DEFERRED_PASSIVE_REPLY_ENABLED=true
WECOM_DEFERRED_PASSIVE_REPLY_TTL_SECONDS=300
```

实验支持两种入口：研发群消息引用带问题编号的机器人文字消息，或触发“请引用机器人发送的文字片段后再 @机器人 回复”的无引用测试消息。收到同一问题的咨询侧引用回复后，worker 会把咨询侧文字通过原研发群回调上下文发送；无引用入口只有在当前没有其他无引用待处理项时才会关联，避免猜测消息归属。回调上下文会短期保存到数据库，包含原始回调 frame 和过期时间，可跨 worker 重启读取；超过 TTL 后不会尝试使用旧 `req_id`。该开关默认关闭。
