# 远端部署速查

最后核对：2026-09-20

## 连接与入口

- SSH：`ssh bubble`。本机 SSH 配置中的 `bubble` 指向 `root@8.162.12.61` 并选择已配置的密钥；优先用别名连接。密钥和服务器环境变量不要复制到仓库或文档。
- 服务器主机名：`iZn4acqxx7hntshktobjylZ`。
- H5：`https://bubblr.blue/kefu/`（Nginx 也配置了 `www.bubblr.blue`）。公网 `/kefu/` 转发到 `127.0.0.1:18000/`。
- Nginx 站点：`/etc/nginx/sites-enabled/bubble`；仓库模板：`deploy/nginx/kefu-h5.conf`。

## 当前部署

- 发布方式：新建 release 目录、上传代码，再切换 `current` 符号链接；本次不是 `git pull` 或 Docker 部署。
- 当前 release：`/opt/kefu/releases/20260920-event-tracker`。
- 活动代码：`/opt/kefu/current` → 上述 release；旧 release 目录仍保留。
- Python 虚拟环境：`/opt/kefu/venv`（Python 3.12）。
- systemd 服务：`kefu-web.service`、`kefu-worker.service`、`kefu-migrate.service`；服务定义位于 `/etc/systemd/system/`，仓库模板位于 `deploy/systemd/`。
- 环境文件：`/etc/kefu/web.env`、`/etc/kefu/worker.env`。只在服务器上维护，不读取或同步其内容。
- 数据：SQLite `/var/lib/kefu/kefu.db`；本地媒体目录 `/var/lib/kefu/media`，对象前缀为 `media/`。
- 数据库迁移版本：`d4e6a81c9b20`。

## 事件数据清理状态

本次部署已清空旧事件、时间线、草稿、投递、入站消息和本地媒体对象；完成后复核相关事件表和 `media/` 前缀均为空。用户、团队、成员授权、研发群绑定和登录会话保留。清理前没有另做数据库或媒体备份；旧 release 代码仍在，但不能恢复已删除的数据。已发送到企业微信群的消息不受服务器清理影响。

## 下次更新步骤

从仓库根目录执行；`release` 每次使用新的日期和简短标识。先上传到新目录，避免覆盖当前版本：

```bash
release=YYYYMMDD-short-label
rsync -a \
  --exclude='.env' --exclude='.venv' --exclude='.git' --exclude='var/' \
  --exclude='.pytest_cache' --exclude='.ruff_cache' --exclude='__pycache__' \
  --exclude='*.pyc' --exclude='*.egg-info' --exclude='.DS_Store' \
  ./ "bubble:/opt/kefu/releases/${release}/"
```

如果服务定义有变化，从新 release 安装并重载 systemd：

```bash
ssh bubble "install -o root -g root -m 0644 /opt/kefu/releases/${release}/deploy/systemd/*.service /etc/systemd/system/ && systemctl daemon-reload"
```

切换或执行数据库迁移前先停 web/worker；切换符号链接后运行迁移，成功后再启动服务：

```bash
ssh bubble "systemctl stop kefu-web.service kefu-worker.service"
ssh bubble "ln -sfnT /opt/kefu/releases/${release} /opt/kefu/current.next && mv -Tf /opt/kefu/current.next /opt/kefu/current"
ssh bubble "systemctl start kefu-migrate.service"
ssh bubble "systemctl show kefu-migrate.service -p Result -p ExecMainStatus"
ssh bubble "systemctl start kefu-web.service kefu-worker.service"
```

确认迁移输出 `Result=success`、`ExecMainStatus=0`，再检查服务、release 和 H5 上游：

```bash
ssh bubble "readlink -f /opt/kefu/current; systemctl is-active kefu-web.service kefu-worker.service; curl -fsS -o /dev/null -w 'HTTP %{http_code}\\n' http://127.0.0.1:18000/docs"
```

查看最近日志：

```bash
ssh bubble "journalctl -u kefu-web.service -u kefu-worker.service -n 100 --no-pager"
```

`kefu-migrate.service` 是一次性服务；完成后显示 `inactive` 属正常，检查 `Result` 和 `ExecMainStatus` 判断结果。若更新失败，可把 `/opt/kefu/current` 切回上一个 release 并重启 web/worker；切回代码不会撤销已经执行的数据库迁移或数据清理。
