需要访问http://192.168.1.139:23002/launcher/gaoxiang拿token
可以先使用账号密码进行测试拿token 浏览器F12 从应用app_auth_session里面取 这个每30分钟刷新一次

gaoxiang
Sgitg@2026


# 登录身份查询接口文档

本文档描述本服务提供的 **`GET /api/integration/login`** 接口：根据调用方提交的 Bearer token 查询当前用户身份与 i智库同步信息。

---

## 1. 接口概览

| 项 | 值 |
| --- | --- |
| Method | `GET` |
| Path | `/api/integration/login` |
| Content-Type | `application/json` |

---

## 2. 请求

### 2.1 Headers

| Header | 必填 | 说明 |
| --- | --- | --- |
| `Authorization` | **是** | `Bearer <access_token>` |
| `Cookie` | 条件必填 | 若本服务启用了密码/Passkey 鉴权，还需有效 `hermes_session` Cookie |

### 2.2 Query / Body

无。仅支持 `GET`，无请求体。

### 2.3 请求示例

```bash
curl -sS \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  http://127.0.0.1:8787/api/integration/login
```

```javascript
const res = await fetch('/api/integration/login', {
  method: 'GET',
  headers: { Authorization: `Bearer ${accessToken}` },
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

### 3.2 `400` — 缺少 Bearer token

```json
{
  "error": "missing_token"
}
```

### 3.3 `401` — token 无效或会话超时

```json
{
  "detail": "会话不存在或已超时，请重新登录。"
}
```

### 3.4 `403` — 无权查询

```json
{
  "detail": "无权查询其他用户身份。"
}
```

### 3.5 `502` — 身份查询失败

```json
{
  "error": "identity_lookup_failed",
  "message": "connection refused"
}
```

### 3.6 状态码汇总

| HTTP | 说明 |
| --- | --- |
| 200 | 查询成功，返回身份 JSON |
| 400 | 缺少 `Authorization: Bearer` |
| 401 | token 无效或会话超时 |
| 403 | 无权查询 |
| 502 | 身份查询服务不可用 |
