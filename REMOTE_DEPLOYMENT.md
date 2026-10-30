# Compose 正式部署

生产环境统一使用 Docker Compose 运行 PostgreSQL、MinIO、FastAPI Web API、Worker 和 Vue 前端。宿主机只发布前端 `18084` 端口；前端容器将 `/api`、OAuth 和受保护媒体请求代理到内部 `web:8000`。Compose 不启动 Nginx 容器，现有外层 Nginx 继续负责 HTTPS 与 `/kefu/` 转发。

## 地址和端口

- H5：`https://bubblr.blue/kefu/`
- Compose 前端：宿主机 `127.0.0.1:18084`，由外层 Nginx 转发
- Web API、PostgreSQL、MinIO：只在 Compose 网络内供其他服务访问
- 项目目录：`/opt/kefu/repo`
- Compose 配置：`/opt/kefu/repo/.env`（仅保存在服务器，不进入 Git）
- PostgreSQL 数据：`/opt/kefu/repo/postgres-data`
- MinIO 数据：`/opt/kefu/repo/minio-data`

更新脚本默认从 GitHub `main` 快进拉取，并执行 `docker compose up -d --build --remove-orphans`。它会等待 `http://127.0.0.1:18084/healthz` 健康检查；失败时输出 Compose 状态和最近日志。

## 首次部署或从旧进程切换

先准备代码目录和服务器专用 `.env`。可从仓库模板复制后填写生产值：

```dotenv
POSTGRES_DB=kefu
POSTGRES_USER=kefu
POSTGRES_PASSWORD=<strong-password>
MINIO_ROOT_USER=<minio-user>
MINIO_ROOT_PASSWORD=<strong-password>
S3_BUCKET=kefu-media
OBJECT_STORAGE_BACKEND=s3
AUTH_MODE=wecom_oauth
WEB_BASE_URL=https://bubblr.blue/kefu
VITE_BASE_PATH=/kefu/
SESSION_COOKIE_SECURE=true
WECOM_CORP_ID=<corp-id>
WECOM_AGENT_ID=<agent-id>
WECOM_CORP_SECRET=<self-built-app-secret>
WECOM_TRANSPORT=long_connection
WECOM_BOT_ID=<bot-id>
WECOM_BOT_SECRET=<bot-secret>
```

也设置 Agent 配置、首次管理员初始化方式和其他业务参数。不要把真实凭据或 `.env` 提交到仓库。

旧 systemd 版本仍在运行时，先停止旧 Web 和 Worker，并确认 Nginx 不再转发到 `18000`。然后在仓库目录启动：

```bash
docker compose up -d --build
docker compose ps
curl -fsS http://127.0.0.1:18084/healthz
```

生产前端镜像会在构建时生成一次 Vue 静态资源；浏览器请求通过同源前端转发到后端。对外 H5 地址需匹配 `WEB_BASE_URL` 和 `VITE_BASE_PATH`。若使用 `/kefu/`，安装或更新外层 Nginx 站点配置：

```bash
install -m 0644 deploy/nginx/kefu-h5.conf /etc/nginx/snippets/kefu-h5.conf
nginx -t && systemctl reload nginx
```

该模板将 `/kefu/` 转发到 `127.0.0.1:18084/` 并移除公开路径前缀。证书与 HTTPS server block 仍由服务器现有 Nginx 配置管理。

## 日常更新

先将变更推送到 `main`，再在服务器执行：

```bash
ssh bubble "/opt/kefu/repo/deploy/update-remote.sh"
```

脚本会拒绝有本地修改的工作树，快进更新代码，再由 Compose 构建并滚动应用服务。Compose 会等待 PostgreSQL 和 MinIO 初始化完成，运行 Alembic 迁移后启动 Web API 和 Worker。持久数据保存在 `postgres-data/` 和 `minio-data/`，更新不会删除这些目录。

查看服务状态和日志：

```bash
ssh bubble "cd /opt/kefu/repo && docker compose ps"
ssh bubble "cd /opt/kefu/repo && docker compose logs -f frontend web bot-worker"
```

如果部署检查失败，先查看脚本输出的容器状态和日志，再决定是否恢复代码。回滚应用镜像不会撤销已经完成的数据库迁移。

## 旧数据说明

Compose 正式数据源是 PostgreSQL 和 MinIO。SQLite 测试库及旧 systemd 实例中的 SQLite 数据不会自动导入 Compose；部署前需确认正式业务数据已在目标 PostgreSQL/MinIO 中。已有 Compose 持久数据会继续使用，不新建业务表或执行数据清理迁移。

## 开发热更新

本地开发覆盖配置挂载前端源码并运行 Vite：

```bash
cp .env.example .env
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
```

编辑 `frontend/` 中的 Vue、TypeScript 或 CSS 文件即可触发热更新。停止开发覆盖后，单独使用 `docker-compose.yml` 会构建并运行生产前端镜像。
