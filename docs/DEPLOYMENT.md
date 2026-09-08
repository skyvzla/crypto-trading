# 部署与运行手册

运维只需要记住三个入口：

```bash
scripts/deploy.sh              # 部署基础服务，不启动策略
scripts/start.sh [SERVICE]     # 启动一个策略 service，默认 spike
scripts/stop.sh [SERVICE]      # 停止一个策略 service，默认 spike
```

三个脚本都从项目根目录执行，并统一使用 `docker compose --profile '*'`。它们不
`source .env`，不会把交易所密钥、数据库密码或事件 token 打到终端。不要使用
`docker compose down -v`，这会删除 PostgreSQL/Redis 数据卷。

## 1. 第一次配置

宿主机需要 Bash、Docker Engine、Docker Compose plugin、Python 3、curl 和 POSIX
工具。宿主机不需要安装 uv 或项目 Python 依赖。

```bash
cp .env.example .env
chmod 600 .env
$EDITOR .env
```

至少确认：

- 交易策略使用的账户、endpoint 和密钥由对应 Compose service 的 runtime guard 校验；部署入口本身不复制策略环境判断。
- testnet API key/secret 使用专用账户，禁止提现权限；正式网配置必须遵守策略专用上线审批，不能绕过 runtime guard。
- 数据库配置与 Compose 一致；生产环境不要使用 `.env.example` 中的默认密码。
- `logs/`、`data/wal/` 和 `data/market/campaign_snapshots/` 由脚本创建，不要把它们放到临时目录。

脚本只检查 `.env` 存在、不是符号链接且权限为 `0600`，不会解析或打印密钥。缺少
`.env` 或权限过宽会 fail-closed；策略环境是否合法由目标 service 的 runtime guard 负责。

## 2. 部署基础服务

```bash
scripts/deploy.sh
```

部署顺序是：检查依赖和 `.env`、创建运行目录、校验 Compose、构建应用镜像、启动
PostgreSQL/Redis、运行 `ledger-migrate`、再启动 Market、Ledger、notification-worker
和 symbol-sync。脚本会检查：

- PostgreSQL、Redis、Market、Ledger 为 `running + healthy`；
- `ledger-migrate` 为单个 `exited + exit code 0` 容器；
- notification-worker、symbol-sync 为单个 `running` 容器（Compose 当前没有为它们提供 healthcheck）；
- Market `/health` 和 Ledger `/api/v1/health` 可访问；
- notification overview API 可返回有效计数和关键事件路由状态。

部署不会启动 `spike`、`strategy_kline` 或 `strategy_tick`。第一次部署或数据库为空时，
通知 overview 的 `critical_routes_ready` 为 `false` 时只会打印 WARNING，基础服务部署仍算成功；输出会列出
缺失的关键事件类型，后续 `start.sh` 会拒绝启动策略。`routable_policies` 仅是配置结构指标，不代表关键事件
一定会被真实选路并投递。

## 3. 配置并验证通知

在 WebUI 的通知页面配置并启用至少一个 connector、endpoint 和 policy。connector 的
敏感值放在 `.env` 或 Docker secret，通过 `secret_ref` 引用；不要把 token/password 放进
WebUI 的普通 config JSON。

`deploy.sh` 和 `start.sh` 的 overview 检查会按真实 policy 选择规则确认每个关键事件类型都有非 suppress、且至少
一个启用 endpoint/connector；这不证明 secret 可解析，也不证明外部平台能收到消息。配置完成后，必须在 WebUI 对目标 endpoint 执行
一次 endpoint test，并检查 delivery 状态和目标平台收件箱，才算通知链路端到端通过。

## 4. 启动策略服务

默认启动 Spike：

```bash
scripts/start.sh
```

也可以启动 Compose 中的其他策略 service：

```bash
scripts/start.sh --build spike
scripts/start.sh strategy_kline
scripts/start.sh strategy_tick
```

启动脚本先用 `--profile '*' config --services` 校验服务名，再拒绝基础服务和已有
`running`、`restarting`、`paused`、`created`、`removing` 或无法判断状态的重复容器。
基础服务入口（`postgres`、`redis`、`market`、`ledger`、`ledger-migrate`、
`notification-worker`、`symbol-sync`）不能通过 `start.sh` 启动。启动前会确认
PostgreSQL/Redis/Market/Ledger、ledger-migrate、notification-worker 和
symbol-sync 的状态、目录可写性以及通知 overview 的 `critical_routes_ready=true`。缺失事件类型会直接显示在
错误中；`routable_policies` 只作为诊断指标。

真正执行的是：

```bash
docker compose --profile '*' up -d --wait [--build] SERVICE
```

策略配置、V22、strategy id 和 subcategory 规则不由脚本复制一份；这些属于服务自身的
runtime guard。`up` 失败、等待超时或目标状态不是 running 时，脚本会打印 Compose 状态
和目标服务末尾日志；本次启动留下的 running/restarting/paused 容器会停止，created/removing
容器会按目标 service 精确移除，避免半启动服务继续写入或留下残留。

## 5. 停止服务

```bash
scripts/stop.sh              # 默认停止 spike
scripts/stop.sh strategy_kline
```

脚本只接受策略 service，拒绝停止 `notification-worker` 及其他基础服务。若
`notification-worker` 未运行会打印 WARNING 但仍继续停止目标策略；目标服务如果有
多个实例、状态不明或停止后仍为 running/restarting/paused/created/removing，会直接失败。
目标为 `created` 时只执行目标 service 的 `rm -f`；目标为 `removing` 时 fail-closed，等待
Compose 完成移除后再重试。实际停止命令为：

```bash
docker compose --profile '*' stop --timeout 120 SERVICE
```

这个入口只负责 Compose 生命周期，不做 admission、flat preflight、平仓、撤单、备份或
事件处理。对有持仓的策略，操作人员必须先执行该策略对应的准入关闭、排空、交易所对账
和备份预案，再调用 stop。脚本输出的排查位置包括 Docker logs、`logs/`、`data/wal/`
以及账本数据库中的事件记录。

## 6. 常见故障

查看整体状态：

```bash
docker compose --profile '*' ps -a
docker compose --profile '*' logs --tail 100 ledger
docker compose --profile '*' logs --tail 100 spike
```

不要通过裸 `docker compose up` 绕过脚本，也不要删除数据库/Redis volume。若启动失败，
先看脚本打印的目标日志和 Compose 状态；若出现网络结果不明、挂单或仓位无法确认，保持
策略停止并人工对账，不要改 client ID 重下单。
