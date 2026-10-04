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

`GRAFANA_ADMIN_USER` 和 `GRAFANA_ADMIN_PASSWORD` 只在首次创建 Grafana 数据库时初始化管理员。之后在 UI/数据库中修改的密码不会因更新 `.env` 或重建容器而被覆盖；不要把 `.env` 中的 bootstrap password 当作现有账号密码的权威来源。

## 基本校验

```bash
docker compose config
docker compose ps
curl -fsS http://127.0.0.1:3000/api/health
```

变更 Grafana provisioning 后，可重启 Grafana，或使用管理员凭据调用对应的 provisioning reload API。正式部署前应再次检查 `git status`，确认没有 secret 或运行数据进入暂存区。

容器 metrics 和 logs 的 `job` 标签统一为 `integrations/docker`。本机 2026-09-07 修正前的容器日志使用 `docker`，错误日志告警保留两种标签的匹配以覆盖历史窗口。Grafana/Loki 的查询审计日志可能包含 `error` 字样，因此这两个容器按 logfmt 的实际 `level` 筛选错误，避免查询文本触发告警。

文件 provisioning 的恢复保持时间字段必须使用 `keepFiringFor`（如 `5m`）；普通规则 API 返回的 `keep_firing_for` 不能直接照搬进 provisioning 文件。部署后同时检查规则导出和运行时配置，确认保持时间已生效。

服务器静态 hostname 使用完整域名，并由 `system/cloud.cfg.d/99-preserve-hostname.cfg` 阻止 cloud-init 在重启时改回云平台下发的短名。Alloy 的 `constants.hostname` 会把该值写入 metrics 和 logs 的 `instance` 标签；修改 hostname 后需 reload Alloy。依赖历史数据的查询应在保留期内兼容旧标签。

## 更新流程

所有配置修改先在本地仓库完成并验证，然后 commit、push；服务器不直接修改仓库中的跟踪文件：

```powershell
Set-Location D:\ProjectsLocal\observability
git status
git add <files>
git commit
git push origin main
```

服务器只通过 fast-forward pull 接收版本：

```bash
ssh cn-hk-grafana
cd /home/ubuntu/docker_projects/observability
git pull --ff-only
git status --short --branch
```

Pull 完成后根据变更范围执行对应的 `docker compose up -d`、服务 reload，或将仓库中的辅助配置同步到上表所列的实际路径。`.env`、`data/` 和其他本地 secret/运行数据受 `.gitignore` 保护，不由 Git 管理。

## 磁盘空间告警验证

`node-fs-filling-{warning,critical}` 保留原来的空间门槛（40% / 20%）、
预测期限（24h / 4h）和持续时间（1h），同时要求 6h 与最近 1h 的
`predict_linear` 都预测在对应期限内耗尽。这样部署后已经稳定的单次空间阶跃
不会仅因仍在 6h 回归窗口中而持续告警；持续增长仍被检测。最近一小时尚未形成
稳定趋势时，预测可能波动，不应把线性外推当成准确耗尽时间。

独立的 `node-fs-low-space-{warning,critical}` 继续按余量 <5% / <3%、持续 30m
告警，不受趋势确认影响。四条空间规则均保留可写文件系统筛选、UID、标签、
`NoData=OK` 与错误处理策略；主机失联继续由 Instance Down 负责。

四条规则的查询 A 保留布尔归一化值，供 B/C 判断；新增瞬时查询 D 返回
`100 * node_filesystem_avail_bytes / node_filesystem_size_bytes`，通知使用
`{{ printf "%.2f" $values.D.Value }}%`。不能把 A/B/C 的 0/1 当成百分比，
也不能直接让真实百分比经 `>0.5` 决定告警，否则剩余 0% 时反而可能不告警。

测试脚本从实际 provisioning 文件读取表达式，生成 13 个场景、105 项 PromQL
断言，包括部署阶跃、持续慢/快增长、低余量、0% 余量、阈值边界、空间恢复、
只读文件系统、缺失数据及两种采集 job。使用已有 Prometheus 的 promtool，无需安装：

```bash
python3 tests/test_filesystem_alerts.py | docker exec -i observability-prometheus-1 promtool test rules /dev/stdin
```

2026-10-04 验证：promtool 全部通过；Grafana `/api/v1/eval` 回放 05:31:30 UTC，
旧趋势条件为 1，新条件不匹配，D=33.482314%。使用 `/api/v1/rule/test/grafana`
无保存预览验证四条规则的模板，显示真实 33.46%；预览临时强制条件成立以覆盖模板，
不保存规则、不发送通知，不能当作线上已发布的证明。

按上述 Git 流程发布后，使用现有服务器管理员 Basic Auth 调用
`POST /api/admin/provisioning/alerting/reload` 热重载，无需重启 Grafana。
已有服务账号可读取规则及执行预览，但不能调用服务器管理员 API；文件来源的规则
不得通过更改 provenance、删除重建或直接修改数据库来规避热重载认证要求。
发布前核对旧版本与工作树，发布后通过规则导出及运行时状态验证四条规则，
确认其他规则未变。回滚使用修复提交的 `git revert`，push、fast-forward pull
并再次热重载；不要直接覆盖服务器文件。

参考：[Grafana annotation variables](https://grafana.com/docs/grafana/latest/alerting/alerting-rules/templates/reference/)、
[provisioning reload API](https://grafana.com/docs/grafana/latest/developer-resources/api-reference/http-api/api-legacy/admin/#reload-provisioning-configurations)。
