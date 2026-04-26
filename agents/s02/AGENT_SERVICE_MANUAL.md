# agent_service 使用手册

本文档面向 `agents/s02/agent_service.py` 的调用方与部署方，覆盖从启动到接口联调的完整流程。

## 1. 服务概览

- 服务文件：`agents/s02/agent_service.py`
- 默认监听：`0.0.0.0:5015`
- 主要接口：
- `POST /chat`：在指定会话中继续对话
- `POST /new`：重置用户当前会话并返回新 `session_id`

服务内部基于 `agents/s02/s02_handwrite.py` 的 `chat_with_tools()` 执行对话逻辑，并在内存中维护会话状态（历史消息、实体缓存、临时状态等）。

## 2. 环境准备

### 2.1 安装依赖

在仓库根目录执行：

```bash
pip install -r requirements.txt
```

### 2.2 关键环境变量

| 变量名 | 默认值 | 说明 |
|---|---|---|
| `SERVICE_HOST` | `0.0.0.0` | 服务绑定地址 |
| `SERVICE_PORT` | `5015` | 服务端口 |
| `SESSION_TTL_SEC` | `1800` | 会话 TTL（秒），默认 30 分钟 |
| `SESSION_MAX_ITEMS` | `1024` | 内存中最多保存会话数 |
| `SESSION_CLEANUP_INTERVAL_SEC` | `30` | 后台清理线程扫描周期（秒） |

同时你还需要为底层模型能力准备环境变量（由 `s02_handwrite.py` 使用），常见包括：

- `ANTHROPIC_API_KEY`
- `ANTHROPIC_BASE_URL`
- `MODEL` 或 `MODEL_ID`

## 3. 启动与停止

### 3.1 启动命令

在仓库根目录执行：

```bash
python agents/s02/agent_service.py
```

启动成功后可见类似日志：

```text
INFO:     Uvicorn running on http://0.0.0.0:5015
```

### 3.2 停止服务

- 前台运行：`Ctrl + C`
- 进程托管（如 Supervisor、systemd、容器）按平台策略停止

## 4. 会话模型与设计约定

### 4.1 会话键

- 会话唯一键：`user_id:session_id`
- 同一个 `user_id` 可存在多个 `session_id`（并行会话）

### 4.2 会话上下文内容

每个会话维护：

- `history`：历史消息（用户与助手）
- `entities`：实体缓存（预留字段）
- `temp_state`：临时状态（预留字段）

### 4.3 TTL 与 LRU

- TTL 过期规则：按 `last_access_at` 判断，超时后清理
- 容量上限规则：超过 `SESSION_MAX_ITEMS` 时按 LRU 淘汰最老会话
- 清理时机：
- 后台线程按 `SESSION_CLEANUP_INTERVAL_SEC` 周期清理
- `get_or_create` / `reset_user_session` 也会触发一次过期清理

## 5. 接口详解

## 5.1 `POST /chat`

### 5.1.1 用途

在指定会话中发送一条用户消息，得到模型回复，并更新该会话历史。

### 5.1.2 请求头

- `Content-Type: application/json`

### 5.1.3 请求体

```json
{
  "user_id": "u1001",
  "session_id": "s20260426a",
  "message": "请帮我总结这个仓库结构"
}
```

字段说明：

| 字段 | 类型 | 必填 | 规则 |
|---|---|---|---|
| `user_id` | string | 是 | 非空字符串 |
| `session_id` | string | 是 | 非空字符串 |
| `message` | string | 是 | 非空字符串 |

### 5.1.4 成功响应（200）

```json
{
  "status": "ok",
  "session_status": "ACTIVE",
  "user_id": "u1001",
  "session_id": "s20260426a",
  "reply": "这是模型返回文本",
  "metrics": {
    "llm_calls": 1,
    "llm_ms": 235.5,
    "tool_calls": 2,
    "tool_ms": 140.2,
    "loop_break_reason": ""
  }
}
```

说明：

- `session_status` 当前固定返回 `ACTIVE`
- `metrics` 内容取决于底层 `chat_with_tools()` 返回结构

### 5.1.5 调用示例

#### curl（Linux/macOS/Git Bash）

```bash
curl -X POST "http://127.0.0.1:5015/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id":"u1001",
    "session_id":"s20260426a",
    "message":"hello"
  }'
```

#### curl（PowerShell）

```powershell
curl.exe -X POST "http://127.0.0.1:5015/chat" `
  -H "Content-Type: application/json" `
  -d "{\"user_id\":\"u1001\",\"session_id\":\"s20260426a\",\"message\":\"hello\"}"
```

## 5.2 `POST /new`

### 5.2.1 用途

重置指定用户当前会话上下文，返回全新 `session_id`。  
用于“新建对话”或“清空上下文重新开始”。

### 5.2.2 请求头

- `Content-Type: application/json`

### 5.2.3 请求体

`session_id` 可选：

```json
{
  "user_id": "u1001",
  "session_id": "s20260426a"
}
```

或仅提供用户：

```json
{
  "user_id": "u1001"
}
```

行为说明：

- 如果传了 `session_id`：优先重置该会话
- 如果没传 `session_id`：重置该用户当前记录的会话（若存在）
- 如果用户不存在：静默创建新会话并返回新 ID

### 5.2.4 成功响应（200）

```json
{
  "status": "reset_ok",
  "session_id": "4f9f1d7a-3e6b-49b5-8d5b-2d84bfad2a2b"
}
```

### 5.2.5 调用示例

```bash
curl -X POST "http://127.0.0.1:5015/new" \
  -H "Content-Type: application/json" \
  -d '{"user_id":"u1001","session_id":"s20260426a"}'
```

## 6. 错误处理规范

统一错误响应结构：

```json
{
  "error_code": "INVALID_ARGUMENT",
  "message": "Field `message` is required and must be non-empty."
}
```

常见错误码：

| HTTP 状态码 | error_code | 场景 |
|---|---|---|
| 400 | `INVALID_JSON` | 请求体 JSON 语法错误 |
| 400 | `INVALID_REQUEST` | JSON 顶层不是对象（例如数组） |
| 400 | `INVALID_ARGUMENT` | 缺少必填字段或字段为空 |
| 500 | `INTERNAL_ERROR` | 服务器内部异常 |

## 7. 结构化日志说明

服务会输出结构化日志（字符串化 dict），典型字段：

- `ts`：时间戳
- `endpoint`：接口路径（如 `/chat`）
- `user_id`
- `session_id`
- `duration_ms`：耗时
- `message`：状态说明
- `error_code`：错误码（仅异常时）
- `stack`：异常堆栈（仅内部异常时）

示例：

```text
{'ts': '2026-04-26T12:00:00+0800', 'endpoint': '/chat', 'user_id': 'u1', 'session_id': 's1', 'duration_ms': 512.3, 'message': 'status=200'}
```

## 8. 典型接入流程（推荐）

1. 客户端初始化时生成一个 `session_id`（或后端生成）
2. 每次聊天调用 `/chat`，保持 `user_id + session_id` 不变
3. 用户点击“新对话”时调用 `/new`
4. 用 `/new` 返回的新 `session_id` 继续调用 `/chat`

## 9. 兼容性与注意事项

- `agent_service.py` 已处理模块路径，建议仍在仓库根目录启动
- 默认是内存会话，不会持久化到磁盘/数据库
- 服务重启后会话全部丢失（符合内存缓存设计）
- 当前为单进程内存态；如多进程/多实例部署，需要改造为共享会话存储（Redis 等）

## 10. 快速自检清单

- 依赖安装成功：`pip install -r requirements.txt`
- 模型配置正确：`ANTHROPIC_*` / `MODEL*` 已设置
- 服务已启动：`http://127.0.0.1:5015`
- `/chat` 返回 `status=ok`
- `/new` 返回 `status=reset_ok`
- 重置后用新 `session_id` 调 `/chat`，上下文从空开始

## 11. 常见问题排查

### 11.1 `ModuleNotFoundError: No module named 'agents'`

- 确保使用当前版本的 `agent_service.py`（已内置 `sys.path` 修复）
- 建议在仓库根目录执行启动命令

### 11.2 `/chat` 返回 500

- 查看服务控制台日志中的 `error_code` 与 `stack`
- 优先检查模型连接配置（API Key、Base URL、Model）

### 11.3 会话“意外丢失”

可能原因：

- 超过 `SESSION_TTL_SEC` 被清理
- 超过 `SESSION_MAX_ITEMS` 被 LRU 淘汰
- 服务重启（内存态数据丢失）

