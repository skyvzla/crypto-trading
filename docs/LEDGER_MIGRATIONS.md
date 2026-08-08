# 账本 PostgreSQL 迁移

账本 schema 的唯一权威来源是
`src/trading_platform/ledger/db/migrations/NNNN_name.sql`。禁止重新引入或手工执行完整
`schema.sql`。

## 执行

部署脚本会在启动 ledger 前自动运行：

```bash
docker compose run --rm --no-deps ledger \
  python -m trading_platform.ledger.db.migrations migrate
```

本机使用 `DB_HOST`、`DB_PORT`、`DB_USER`、`DB_PASSWORD`、`DB_DATABASE` 配置：

```bash
uv run ledger-migrate migrate
uv run ledger-migrate status
```

也可向两个命令显式传入 `--dsn`。ledger 服务启动时会再次并发安全地应用待执行迁移，
然后校验数据库已处于当前代码版本；版本缺失、超前或已应用 SQL 的校验和改变都会拒绝启动。
Compose 中 `ledger` 与 `ledger-migrate` 强制使用同一个镜像制品，避免定向构建后迁移 runner
仍停留在旧版本并把已升级数据库误判为“高于当前构建”。

## 新增迁移

1. 新建下一个连续四位版本，例如 `0003_add_example.sql`。
2. 迁移只能向前兼容，不得修改已经应用的 SQL 文件。
3. 迁移须能在一个 PostgreSQL 事务内执行；不要使用 `CREATE INDEX CONCURRENTLY` 等禁止
   在事务内运行的语句。
4. 先在现有数据副本验证，再运行真实 PostgreSQL 迁移测试。

runner 使用事务级 PostgreSQL advisory lock 串行化并发实例；全部待执行版本、版本记录和
校验在同一事务中完成，任一失败会整批回滚。`0001_initial.sql` 使用幂等 DDL 接管迁移机制
上线前已存在的当前数据库，不删除或重建业务表，也不清理已有数据。

`0002_campaign_attribution.sql` 为订单和成交增加可空 `campaign_id` 及查询索引。列保持
可空是有意设计：升级前的历史退出单无法可靠证明 Campaign 归属，禁止按时间推测回填。

当前版本为 `0003_strategy_runtime_status.sql`，保存每个账户和策略的最新实例状态及心跳。
不同实例只有更晚的 `started_at` 才能接管，同一实例只接受不早于当前记录的心跳，防止旧进程
或乱序更新覆盖新状态。

`0004_exchange_symbols.sql` 保存 Binance USD-M 交易对生命周期元数据，供每日同步与退市
入场门禁使用。

## 备份恢复演练

以下命令对当前 Compose PostgreSQL 创建权限为 `0600` 的 custom-format 归档，恢复到随机命名
的临时数据库，逐项核对 9 张业务/迁移表的行数和全部迁移文件校验和，随后删除且只删除该
临时验证库。目标文件已存在时命令拒绝覆盖：

```bash
bash scripts/verify_ledger_backup_restore.sh \
  backups/ledger_$(date -u +%Y%m%dT%H%M%SZ).dump
```

成功输出必须包含 `BACKUP_RESTORE_OK`。归档位于 Git 忽略的 `backups/`，包含真实账本数据，
不得提交或发送到未授权位置。恢复演练不会替代生产备份保留、加密和异地存储策略。
