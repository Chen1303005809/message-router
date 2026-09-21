# 远端部署速查

最后在线核对：2026-09-21

## 连接与入口

- SSH：`ssh bubble`，本机 SSH 配置指向 `root@8.162.12.61` 并选择已配置的密钥。不要把密钥或服务器环境变量写入仓库。
- H5：`https://bubblr.blue/kefu/`。Nginx 将 `/kefu/` 转发到 `127.0.0.1:18000/`。
- Nginx 站点：`/etc/nginx/sites-enabled/bubble`；仓库模板：`deploy/nginx/kefu-h5.conf`。
- H5 页面由 FastAPI 服务端渲染；页面和 API 共用 `kefu-web` 的 18000 端口。机器人由不监听端口的 `kefu-worker` 进程处理。

## 当前远端状态

- 当前 release：`/opt/kefu/releases/20260921-update`；`/opt/kefu/current` 指向该目录。
- Web 和 worker 由 systemd 直接运行，不使用 Docker；Python 虚拟环境为 `/opt/kefu/venv`（Python 3.12）。
- 服务：`kefu-web.service`、`kefu-worker.service`、一次性迁移服务 `kefu-migrate.service`。模板位于 `deploy/systemd/`。
- 运行配置只保存在服务器：`/etc/kefu/web.env`、`/etc/kefu/worker.env`。数据库为 `/var/lib/kefu/kefu.db`，本地媒体目录为 `/var/lib/kefu/media`。
- 当前迁移版本：`d4e6a81c9b20`。上次更新后 web/worker 均 active，迁移结果为 success，端口健康检查返回 HTTP 200。

上次事件数据清理已删除旧事件、时间线、草稿、投递、入站消息和本地媒体对象；用户、团队、成员授权、研发群绑定和登录会话保留。清理前没有数据库或媒体备份；已发送到企业微信群的消息不受影响。

## 一次性迁移到 Git 工作树

当前服务器仍使用 release 上传目录。先把仓库克隆到新目录，再把 `current` 切换过去；原 release、虚拟环境、环境文件和数据目录保留。

```bash
ssh bubble "git clone --branch main https://github.com/Chen1303005809/message-router.git /opt/kefu/repo"
ssh bubble "systemctl stop kefu-web.service kefu-worker.service"
ssh bubble "ln -sfnT /opt/kefu/repo /opt/kefu/current.next && mv -Tf /opt/kefu/current.next /opt/kefu/current"
ssh bubble "/opt/kefu/venv/bin/pip install /opt/kefu/current"
ssh bubble "install -o root -g root -m 0644 /opt/kefu/current/deploy/systemd/*.service /etc/systemd/system/ && systemctl daemon-reload"
ssh bubble "systemctl start kefu-migrate.service"
ssh bubble "systemctl show kefu-migrate.service -p Result -p ExecMainStatus"
```

只有看到 `Result=success` 和 `ExecMainStatus=0` 后才启动服务：

```bash
ssh bubble "systemctl start kefu-web.service kefu-worker.service"
ssh bubble "systemctl is-active kefu-web.service kefu-worker.service; curl -fsS http://127.0.0.1:18000/healthz"
```

`kefu-migrate.service` 是一次性服务；完成后显示 `inactive` 正常，按 `Result` 和 `ExecMainStatus` 判断迁移结果。环境文件与数据不放进 Git 工作树。

## 日常更新

先把代码推送到 GitHub `main`，再在远端执行更新脚本：

```bash
ssh bubble "/opt/kefu/current/deploy/update-remote.sh"
```

脚本会检查工作树、停止 web/worker、拉取 `origin/main`、更新 `/opt/kefu/venv` 中的项目依赖、安装 systemd 模板、运行并核对数据库迁移，成功后启动两个服务并检查健康端点。若 Git 拉取失败，脚本会重新启动旧服务；依赖安装或迁移失败时会保持服务停止，避免新旧代码与数据库状态不一致。脚本不读取或覆盖 `/etc/kefu/*.env`，也不操作 `/var/lib/kefu`。

如更新中途失败，脚本会停在失败步骤并打印迁移状态；先查看服务日志再决定是否恢复旧代码。切回旧 release 不会撤销已完成的数据库迁移或数据清理。

## Docker Compose（仅本地可选）

Docker Compose 只用于本地需要时启动 PostgreSQL、MinIO、web 和模拟 worker；远端日常运行不依赖 Docker。远端 H5 仍通过 Nginx 提供 HTTPS，应用进程直接监听 18000。

```bash
cp .env.example .env
docker compose up --build
```

查看远端最近日志：

```bash
ssh bubble "journalctl -u kefu-web.service -u kefu-worker.service -n 100 --no-pager"
```
