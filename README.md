# observability

`cn-hk-grafana` 的自建 Grafana、Prometheus、Loki 和 PostgreSQL 配置仓库。

## 目录映射

| 仓库路径 | 服务器路径 |
|----------|------------|
| `compose.yaml`、`grafana/`、`loki/`、`prometheus/` | `/home/ubuntu/docker_projects/observability/` |
| `caddy/Caddyfile` | `/home/ubuntu/docker_projects/.caddy/Caddyfile` |
| `alloy/caddy.alloy` | `/home/ubuntu/docker_projects/.caddy/caddy.alloy` |
| `alloy/config.alloy` | `/etc/alloy/config.alloy` |
| `restic/` | `/home/ubuntu/docker_projects/.restic/` |
| `system/` | 对应的 `/etc/` 配置或 systemd override |
| `senders/` | 既有监控节点双写部署工具，不直接放入主栈目录 |

所有容器持久化数据使用 `/home/ubuntu/docker_projects/observability/<组件>/data/` bind mount，不进入 Git。

## 初始化凭据

克隆后复制模板并填写真实值：

```text
.env.example                              -> .env
caddy/grafana.env.example                 -> /etc/caddy/grafana.env
caddy/ingest-credentials.env.example      -> /home/ubuntu/docker_projects/.caddy/ingest-credentials.env
senders/selfhosted.env.example            -> /etc/alloy/selfhosted.env（每台发送节点）
restic/restic.env.example                 -> /home/ubuntu/.restic.env
```

这些目标文件均应设置为 `600`，不得提交。Caddy 的 `grafana.env` 只保存 bcrypt hash；节点的 ingest password 只保存在发送端和服务器本地凭据清单。

## 基本校验

```bash
docker compose config
docker compose ps
curl -fsS http://127.0.0.1:3000/api/health
```

变更 Grafana provisioning 后，可重启 Grafana，或使用管理员凭据调用对应的 provisioning reload API。正式部署前应再次检查 `git status`，确认没有 secret 或运行数据进入暂存区。
