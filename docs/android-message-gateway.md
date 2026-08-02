# Android 消息网关

## 定位

Android 网关只负责稳定取得自有闲鱼账号的新消息、保持目标聊天页、执行一次文本回复，
业务规则仍由本仓库负责。服务端收到 Android 事件后会：

1. 校验账号白名单、HMAC 签名和事件幂等键；
2. 以 Android 已确认的发送者标签和正文构造稳定、脱敏的会话上下文；
3. 直接复用现有关键词、默认回复和 AI 决策链，不连接旧 WebSocket 消息通道；
4. 返回 `reply`、`noop` 或 `unsupported` 决策；
5. 等待 Android 的发送回执，再登记默认回复“一次性”状态和发出的聊天记录。

Android 通知和聊天页当前不能稳定提供真实买家 ID、商品 ID 或 WebSocket 会话 ID，所以
这条通道使用 `android:<hash>` 形式的稳定会话/发送者 ID，并把商品 ID 留空。通用关键词、
默认回复和 AI 规则可正常工作；依赖精确商品 ID 或真实买家 ID 的规则不会在信息不足时
猜测匹配。图片规则当前返回 `unsupported`，Android 网关不会把图片标记误发成文本。

## 配置

在服务器 `.env` 中设置：

```dotenv
ANDROID_GATEWAY_SHARED_SECRET=使用密码生成器产生的长随机值
ANDROID_GATEWAY_ACCOUNT_IDS=服务端Cookie ID
```

然后重建或重启 Compose 服务，使变量进入容器：

```bash
docker compose up -d --force-recreate
```

`ANDROID_GATEWAY_ACCOUNT_IDS` 中的每个账号必须已经在管理后台导入并保留有效 Cookie，
但不要求旧 WebSocket 账号实例处于运行状态。
该变量同时关闭这些账号的 WebSocket 自动回复发送，防止 WebSocket 偶尔恢复时与 Android
重复回复。WebSocket 仍可服务于未迁移账号或其他既有业务，但 Android 网关的
`event → decision → receipt` 链路不再调用它。

Android 侧建议通过 Tailscale 地址访问服务。协议还会对每个请求使用
`HMAC-SHA256(timestamp + "\n" + raw_body)` 签名；服务器只接受五分钟内的请求。事件 ID
在 SQLite 的 `android_gateway_events` 表中唯一，重复投递返回已经缓存的决策；发送回执也
是幂等的。

健康检查：

```bash
curl http://127.0.0.1:8090/api/android-gateway/v1/health
```

`enabled: false` 表示共享密钥没有进入容器。

## API

- `GET /api/android-gateway/v1/health`
- `POST /api/android-gateway/v1/events`
- `POST /api/android-gateway/v1/events/{event_id}/receipt`

两个 POST 请求必须包含：

- `X-Gateway-Timestamp`
- `X-Gateway-Signature`
- `Content-Type: application/json`

服务端保存事件、决策和回执状态，用于审计与幂等恢复。共享密钥不得写入仓库、日志或
请求正文。

## 发送一致性

服务端负责“同一事件只做一次业务决策”，Android 端负责“同一事件最多点击一次发送”。
Android 会在点击前持久化 `sending` 阶段；如果进程恰在点击边界崩溃，恢复时会回报
`send_unconfirmed`，不会自动再次点击。这个选择优先避免重复回复，未确认事件需要人工
检查聊天页。
