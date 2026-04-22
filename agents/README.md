# TypeScript Migration - s01_ollama2

## 迁移说明

本目录包含从 Python 迁移到 TypeScript 的 Agent 实现。

### 文件对照表

| Python | TypeScript | 说明 |
|---------|------------|------|
| `s01_ollama2.py` | `s01_ollama2.ts` | 主实现文件 |
| - | `types.ts` | 共享类型定义 |
| - | `s01_ollama2.test.ts` | 单元测试 |
| - | `package.json` | 依赖配置 |
| - | `tsconfig.json` | TypeScript 配置 |
| - | `eslint.config.js` | ESLint 配置 |
| - | `prettier.config.js` | Prettier 配置 |

### 主要差异

1. **并发模型**: Python 的 `subprocess.run` → TypeScript 的 `exec` + `promisify`
2. **HTTP 客户端**: Python 的 `anthropic` SDK → 原生 `fetch` API (兼容 Ollama)
3. **REPL 循环**: Python 的 `input()` → `readline` + async/await
4. **类型系统**: Python 的 `List[Any]` → TypeScript 联合类型 + 泛型约束

### 运行步骤

```bash
# 安装依赖
cd agents
npm install

# 类型检查
npm run typecheck

# 运行测试
npm test

# 代码格式化
npm run format

# 一键检查（类型 + lint）
npm run check
```

### 环境变量

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `ANTHROPIC_API_KEY` | `ollama` | API 密钥 |
| `ANTHROPIC_BASE_URL` | `http://192.168.1.99:11434` | Ollama API 地址 |
| `MODEL` | `qwen3:14b` | 模型名称 |

### 测试覆盖率

测试覆盖以下场景：
- ✅ 危险命令拦截 (`rm -rf`, `sudo`, `shutdown`)
- ✅ 多格式参数解析 (`command`, `cmd` 列表, `cmd` 字符串)
- ✅ Shell 前缀剥离 (`bash -lc`, `bash -c`)
- ✅ 文本提取 (text, thinking 块)
- ✅ 工具执行结果格式化
- ✅ 异步 HTTP 请求错误处理