需要访问http://192.168.1.139:23002/launcher/gaoxiang拿token
可以先使用账号密码进行测试拿token 浏览器F12 从应用app_auth_session里面取 这个每30分钟刷新一次

gaoxiang
Sgitg@2026


# 登录身份查询接口文档

本文档描述本服务提供的 **`GET /api/integration/webui_login`** 接口：根据调用方提交的 Bearer token 查询当前用户身份与 i智库同步信息；首次成功查询后身份会缓存在 WebUI 进程内存，后续可无 Bearer 读取。

---

## 1. 接口概览

| 项 | 值 |
| --- | --- |
| Method | `GET` |
| Path | `/api/integration/webui_login` |
| Content-Type | `application/json` |

**双模式：**

| 模式 | 请求 | 行为 |
| --- | --- | --- |
| 刷新/种子 | 带 `Authorization: Bearer <access_token>` | 转发 Control Plane lookup；`200` 时写入内存缓存、同步各 Profile `ithink_kb_mcp` headers，并返回身份 |
| 读缓存 | 无 Bearer | 返回内存中的身份 JSON（**不含** `access_token`） |

进程重启后缓存清空，需至少再调用一次带 Bearer 的查询。`POST /api/integration/webui_logout` 会清空缓存。

### MCP headers 同步（登录成功时）

带 Bearer 且 Control Plane 返回 `200` 时，WebUI 会遍历所有可见 Profile 的 `config.yaml`，仅当存在 `mcp_servers.ithink_kb_mcp` 时更新其 headers：

| Header | 取值来源（优先级） |
| --- | --- |
| `X-IThink-Account` | `ithinktank.account` → `ithinktank_account` |
| `X-IThink-UUID` | `ithinktank.uuid` → `ithinktank.userId` → `ithinktank_user_id` |

其它 headers（例如 `X-IThink-IsPersonal`）保持不变。缺 account/uuid 时整次跳过写盘；单个 Profile 写失败不影响登录 JSON 响应（仍为 `200`）。无 Bearer 读缓存、以及非 `200` 的 lookup 结果，都不会触发同步。

---

## 2. 请求

### 2.1 Headers

| Header | 必填 | 说明 |
| --- | --- | --- |
| `Authorization` | 条件必填 | 刷新缓存时：`Bearer <access_token>`；读缓存时可省略 |
| `Cookie` | 条件必填 | 若本服务启用了密码/Passkey 鉴权，还需有效 `hermes_session` Cookie |

### 2.2 Query / Body

无。仅支持 `GET`，无请求体。

### 2.3 环境变量（可选）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ZHILING_IDENTITY_CACHE_TTL_SECONDS` | `1800` | 非 JWT token 或 JWT 无 `exp` 时的缓存 TTL（秒） |

### 2.4 请求示例

**首次 / 刷新（带 token）：**

```bash
curl -sS \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  http://127.0.0.1:8787/api/integration/webui_login
```

**后续（读缓存，无 token）：**

```bash
curl -sS http://127.0.0.1:8787/api/integration/webui_login
```

```javascript
// 首次：带 token 写入缓存
const seeded = await fetch('/api/integration/webui_login', {
  method: 'GET',
  headers: { Authorization: `Bearer ${accessToken}` },
  credentials: 'include',
});

// 后续：无 token 读缓存
const res = await fetch('/api/integration/webui_login', {
  method: 'GET',
  credentials: 'include',
});
const data = await res.json();
```

---

## 3. 响应

### 3.1 `200` — 查询成功

返回当前用户身份与 i智库同步信息。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `username` | string | 用户名 |
| `display_name` | string | 显示名 |
| `organization` | object | 组织信息，含 `id`、`name` 等 |
| `positions` | array | 岗位列表，元素含 `id`、`name` 等 |
| `roles` | array | 角色列表 |
| `is_admin` | boolean | 是否管理员 |
| `ithinktank` | object | i智库身份，含 `account`、`userId` 等 |
| `ithinktank_account` | string | i智库账号 |
| `ithinktank_user_id` | string | i智库用户 ID |

示例：

```json
{
  "username": "zhangsan",
  "display_name": "张三",
  "organization": {
    "id": "org-001",
    "name": "示例组织"
  },
  "positions": [
    { "id": "pos-1", "name": "工程师" }
  ],
  "roles": [],
  "is_admin": false,
  "ithinktank": {
    "account": "zhangsan",
    "userId": "abc-123"
  }
}
```

### 3.2 `401` — 无缓存 / 缓存过期 / token 无效

**读缓存（无 Bearer）时：**

```json
{ "error": "not_registered" }
```

```json
{ "error": "session_expired" }
```

**带 Bearer 且 Control Plane 拒绝时（透传）：**

```json
{
  "detail": "会话不存在或已超时，请重新登录。"
}
```

### 3.3 `403` — 无权查询

```json
{
  "detail": "无权查询其他用户身份。"
}
```

### 3.4 `502` — 身份查询失败

```json
{
  "error": "identity_lookup_failed",
  "message": "connection refused"
}
```

### 3.5 状态码汇总

| HTTP | 说明 |
| --- | --- |
| 200 | 查询成功，返回身份 JSON |
| 401 | 无缓存（`not_registered`）、缓存过期（`session_expired`），或 Bearer token 无效（`detail`） |
| 403 | 无权查询 |
| 502 | 身份查询服务不可用 |
