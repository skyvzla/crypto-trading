# 部署与运行手册

在源码工作区使用：

```bash
scripts/deploy.sh              # 部署基础服务，不启动策略
scripts/start.sh [SERVICE]     # 启动一个策略 service，默认 spike
scripts/stop.sh [SERVICE]      # 停止一个策略 service，默认 spike
```

这些脚本都从项目根目录执行，并统一使用 `docker compose --profile '*'`。它们不
`source .env`，不会把交易所密钥、数据库密码或事件 token 打到终端。不要使用
`docker compose down -v`，这会删除 PostgreSQL/Redis 数据卷。

目标机无需 checkout 应用源码，可安装 `deploy/deploy-release.sh` 作为单一部署入口；
它只下载版本化 Compose/运维配置包，并运行匹配的 GHCR 镜像。

## 1. 第一次配置

源码工作区需要 Bash、Docker Engine、Docker Compose plugin、Python 3、curl 和 POSIX
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
- 目标机使用发布脚本部署时，镜像版本由 Release tag 选择，不必在 `.env` 中维护镜像地址。

脚本只检查 `.env` 存在、不是符号链接且权限为 `0600`，不会解析或打印密钥。缺少
`.env` 或权限过宽会 fail-closed；策略环境是否合法由目标 service 的 runtime guard 负责。

## 2. 发布应用镜像和部署配置

现有测试流程保持不变。`.github/workflows/publish-image.yml` 只在推送 `v*` tag 时运行；
它要求 tag 指向已合入 `main` 的提交，构建镜像并推到 GHCR，同时创建 GitHub Release。
Release 附带版本镜像、完整 commit SHA 镜像引用、部署 bundle、bundle SHA-256 和单文件
`deploy-release.sh`。bundle 只含 Compose 配置、`.env.example` 和运维脚本，不含 `src/`、
测试、Dockerfile 或构建上下文。

代码已合入并通过现有测试后，在本机基于 `main` 的目标提交创建并推送版本 tag：

```bash
git checkout main
git pull --ff-only
git tag v1.2.3
git push origin v1.2.3
```

等待 GitHub Actions 的 `Publish application image` 成功后，镜像地址为：

```text
ghcr.io/<owner>/<repository>:v1.2.3
```

目标机的 GitHub CLI 和 Docker 登录由运维预先配置；发布脚本只检测已有登录状态，不创建、
保存或传递 token。GitHub CLI 需能读取私有仓库 Release，Docker 需已登录 GHCR 并有镜像
读取权限。登录凭据不要写进部署 bundle 或 `.env`。

## 3. 目标机安装与部署

目标机只需 Bash、GitHub CLI、Docker Engine/Compose、Python 3、curl、tar、sha256sum 和
POSIX 工具。不需要 Git、Python 项目依赖、Node、Dockerfile 或应用源码。第一次从对应
GitHub Release 安装单文件入口：

```bash
DEPLOY_DIR="$HOME/services/trading-platform"
mkdir -p "$DEPLOY_DIR"
gh release download v1.2.3 --repo skyvzla/crypto-trading \
  --pattern deploy-release.sh --dir "$DEPLOY_DIR"
chmod 700 "$DEPLOY_DIR/deploy-release.sh"
```

准备目标机自己的 `.env`。首次运行脚本会从版本 bundle 放置权限为 `0600` 的模板并停止；
设置强 `DB_PASSWORD`（至少 24 字符）及所需环境后再次运行。默认持久化根目录就是入口脚本
所在目录；也可通过 `TRADING_PLATFORM_HOME` 指定其他目录。每次部署会
把选中版本的部署文件留在 `releases/<tag>/`，数据库卷、`.env`、WAL、行情数据、日志和备份
始终留在固定根目录，不随版本切换覆盖。

默认部署最新稳定 GitHub Release：

```bash
"$DEPLOY_DIR/deploy-release.sh"
```

升级可继续部署最新版本，也可固定指定版本：

```bash
"$DEPLOY_DIR/deploy-release.sh" latest
"$DEPLOY_DIR/deploy-release.sh" v1.2.3
```

基础服务部署不会启动策略。需要在同一脚本调用中显式启动某个策略时，使用 `--start`；
策略运行中的升级会被拒绝，且仍须先人工关闭准入、排空、交易所对账：

```bash
"$DEPLOY_DIR/deploy-release.sh" latest --start spike
```

完成准入关闭、排空、对账后，可通过同一个入口调用既有 stop 门禁，再执行升级：

```bash
"$DEPLOY_DIR/deploy-release.sh" --stop spike
"$DEPLOY_DIR/deploy-release.sh" latest
```

成功部署后会在持久化根目录记录当前 Release。`--stop SERVICE` 只使用本机已安装的
Release 配置，不访问 GitHub 或 GHCR，因此网络或仓库暂时不可用时仍能按原 stop 门禁停策略。

发布脚本会检查 GitHub CLI 登录、Docker daemon 和 Compose、目标目录、`.env` 权限和数据库
密码强度；下载后验证 SHA-256、Release tag 和 bundle 文件白名单，并确认 release Compose
所有应用服务都没有源码构建上下文。之后已有的 `scripts/deploy.sh` 会确保 PostgreSQL/Redis
就绪、运行并验证数据库备份（真正空库会明确跳过），再拉镜像、执行迁移、检查健康接口和
通知状态。私有 GitHub/GHCR 访问沿用目标机已配置的 CLI/Docker 登录，不改造凭据流程。

## 4. 本地源码部署

不设置 `TRADING_PLATFORM_RELEASE_COMPOSE_DIR` 且在源码 checkout 中运行时，原有本地开发流程
保持不变，`scripts/deploy.sh` 仍从源码构建：

```bash
scripts/deploy.sh
```

全新且尚无任何业务表的数据库会明确报告跳过备份；若已有业务表却缺少有效迁移历史则拒绝部署。

部署顺序是：检查依赖和 `.env`、创建运行目录、校验 Compose、本地构建或拉取应用镜像、启动
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

## 5. 配置并验证通知

在 WebUI 的通知页面配置并启用至少一个 connector、endpoint 和 policy。Telegram Bot
token 和 Webhook 认证密钥直接在连接器表单中填写，由通知系统单独持久化；读取接口、
投递快照和日志不会回显明文。编辑连接器时密钥留空表示保留现有值，无需修改容器环境或
重启 worker。已有 `secret_ref` 连接器仍可从 `.env` 或 Docker secret 解析，供平滑迁移；
不要把 token/password 放进普通 config JSON。

浏览器写通知配置时，带 `Origin` 的请求必须与 Ledger 的 `Host` 同源；内部 worker/CLI 不带
`Origin` 仍可调用。该校验只提供同源 CSRF 防护，不是登录或权限系统，Ledger 仍应部署在受控的内网边界内。

`deploy.sh` 和 `start.sh` 的 overview 检查会按真实 policy 选择规则确认每个关键事件类型都有非 suppress、且至少
一个启用 endpoint/connector；这不证明 secret 可解析，也不证明外部平台能收到消息。配置完成后，必须在 WebUI 对目标 endpoint 执行
一次 endpoint test，并检查 delivery 状态和目标平台收件箱，才算通知链路端到端通过。

## 6. 启动策略服务

默认启动 Spike：

```bash
scripts/start.sh
```

也可以启动 Compose 中的其他策略 service：

```bash
scripts/start.sh --build spike
scripts/start.sh --build long_breakout
scripts/start.sh strategy_kline
scripts/start.sh strategy_tick
```

GHCR 模式下，策略启动复用 `.env` 中配置且已由 `deploy.sh` 拉取的镜像，不会在目标机
构建源码。切换版本时先完成基础服务部署，再按准入、排空和对账要求启动策略。

`spike` 和 `long_breakout` 可以分别启动并同时运行，但必须使用不同的逻辑账户、
Binance testnet 子账户凭证和 WAL。long_breakout 默认 `ENTRY_ENABLED=false`；运行健康
不等于允许开仓，只有环境开关、Ledger 的 `long_breakout` subcategory、资金、杠杆、
全仓模式、行情、执行流和 worker 门禁同时通过时才会产生 BUY 入场。

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

## 7. 停止服务

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

## 8. 回滚与常见故障

源码部署环境可将 `.env` 中的 `TRADING_PLATFORM_IMAGE` 改回上一版本 tag，再运行
`scripts/deploy.sh`。无源码目标机使用
`"$DEPLOY_DIR/deploy-release.sh" v1.2.2` 固定回滚到上一版。迁移可能改变数据库 schema；
回滚前必须确认旧应用兼容当前 schema，否则应按数据库恢复预案处理，不能只把镜像 tag 改回去。

查看整体状态：

```bash
docker compose --profile '*' ps -a
docker compose --profile '*' logs --tail 100 ledger
docker compose --profile '*' logs --tail 100 spike
docker compose --profile '*' logs --tail 100 long_breakout
```

不要通过裸 `docker compose up` 绕过脚本，也不要删除数据库/Redis volume。若启动失败，
先看脚本打印的目标日志和 Compose 状态；若出现网络结果不明、挂单或仓位无法确认，保持
策略停止并人工对账，不要改 client ID 重下单。
