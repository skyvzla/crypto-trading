# 通知系统

通知配置由 PostgreSQL 持久化，发送由独立 `notification-worker` 执行。交易信号循环只写入事件事实，不直接调用 Telegram 或 Webhook。

## 配置模型

- **Connector**：发送身份和共享协议配置。类型为 `telegram` 或 `webhook`，密钥只保存 `secret_ref`，不保存明文 token/secret。
- **Endpoint**：Connector 下的一个具体接收目标。Telegram 的 `address` 是 Chat ID，Webhook 的 `address` 是 URL。
- **职责组**：一组 Endpoint，例如 `risk-oncall`、`strategy-signal`。
- **路由策略**：事件模式、重要级别、优先级、抑制开关和一个或多个职责组。
- **事件/投递**：事件只落库一次；每个 Endpoint 生成独立投递，分别记录租约、尝试次数、重试和死信。

同一个 Connector 可以配置多个 Webhook URL，只需创建多个 Endpoint：

```text
webhook connector: ops-webhook
  endpoint: incident  -> https://incident.example.com/hooks/trading
  endpoint: audit     -> https://audit.example.com/events
  endpoint: backup    -> https://backup.example.net/notify
```

每个 URL 是独立投递目标；某个 URL 失败只重试该 URL，不会阻塞同一事件的其他目标。相同 Endpoint 被多个职责组引用时，事件仍只创建一条投递。

Telegram 多账户使用多个 Connector，每个 Bot 的 token 通过不同的 `secret_ref` 注入；一个 Telegram Connector 也可以配置多个 Chat/Topic Endpoint。这样可以分别承担值班、信号、风控等职责。

## 单渠道与多渠道

- 单渠道：策略选择一个职责组，职责组只包含一个 Endpoint。
- 多渠道：策略选择多个职责组，或一个职责组包含多个 Endpoint。
- `severity` 分别配置 `info`、`warning`、`critical`。同一重要级别下精确事件模式优先于 glob 模式，再按 `priority` 选择策略。
- 没有匹配策略的事件仍会落库为 `unrouted`，可在通知中心检查。

## API 示例

```http
POST /api/v1/notifications/connectors
Content-Type: application/json

{
  "name": "ops-webhook",
  "type": "webhook",
  "secret_ref": "env:OPS_WEBHOOK_SECRET",
  "config": {"auth_type": "hmac_sha256", "timeout_seconds": 8},
  "enabled": true
}
```

```http
POST /api/v1/notifications/endpoints
Content-Type: application/json

{
  "connector_id": "<connector-id>",
  "name": "incident",
  "address": "https://incident.example.com/hooks/trading",
  "config": {"headers": {"X-Tenant": "trading"}},
  "enabled": true
}
```

Webhook 默认要求 HTTPS，并在发送前进行基础 SSRF 地址校验。网络错误、408、429、5xx 会指数退避重试；明确的 4xx 会进入死信。投递语义是至少一次，接收方应按 `Idempotency-Key` 或事件/投递 ID 去重。

生产迁移序列中 `0008_web_performance_indexes.sql` 是既有 Web 索引，通知表位于 `0009_notifications.sql`，不要重写已应用迁移。
