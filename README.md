# DAUnreal MCP (Editor Automation Suite)

基于 Model Context Protocol (MCP) 协议的 Unreal Engine 编辑器联动控制台，采用「脚本直通」架构：外部 AI 客户端（如 Claude、Cursor、VS Code）直接编写 `unreal.*` Python 脚本，由编辑器内自定义 C++ 插件在游戏线程执行并回传结果，而非封装一组离散的固定功能。

---

## 🌟 核心特性

- **脚本直通 (Script Pass-through)**：核心工具 `execute_python` 在 UE 编辑器 Python VM 直接执行任意 Python 脚本，让 AI 自由调用完整的 `unreal.*` API，而非受限的预定义工具集。
- **游戏线程执行 + 命名空间持久化**：插件以 `EPythonFileExecutionScope::Public` 在游戏线程同步执行，变量与 import 跨调用持久化（REPL 风格），会话状态可复用。
- **内置 `da` 序列化 helper**：自动注入编辑器命名空间，`da.dump` / `da.dumps` 将 UObject / struct / 数组转成可读 dict / JSON。
- **API 内省工具**：`python_help` 通过 `dir` + docstring 帮助 AI 在写脚本前发现 `unreal` API，降低试错成本。
- **本地回环 TCP 桥**：C++ 插件在 `127.0.0.1:8765` 提供 NDJSON-over-TCP 桥，仅监听回环地址，无外网暴露。
- **极简环境元工具**：工具总数仅 3 个，均为环境元工具，符合脚本直通定位。

---

## 🚀 快速开始

### 0. 前置要求
- **Unreal Engine 5.4**：工程 `EngineAssociation` 为 `5.4`，需启用 **Python Editor Script Plugin**（插件已声明该依赖）。
- **Python 3.11+**：独立安装的 Python（驱动后台 MCP 服务，与 UE 内置 Python 无关）。
- **一个 MCP 客户端**：Claude Desktop、Cursor 或 VS Code 等。

### 1. 部署插件到工程
用 PowerShell 运行 `deploy.ps1`，将本仓库 `plugin/` 拷入目标工程的 `Plugins` 目录：

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy.ps1 -ProjectDir "C:\...\DAUNrealTest"
```

> 默认目标工程为 `C:\Users\qingpulou\Documents\Unreal Projects\DAUNrealTest`，可用 `-ProjectDir` 覆盖。

### 2. 编译并启动编辑器
首次启动工程时 UE 会提示编译该插件（或手动编译）。启动后插件自动在 `127.0.0.1:8765` 监听。

端口可在工程 `Config\DefaultEngine.ini` 修改：

```ini
[DAUnrealMCP]
Port=8765
```

### 3. 创建虚拟环境并安装依赖
后台 MCP 服务依赖隔离安装在 `server\.venv` 中：

**Windows（PowerShell / CMD）：**
```bash
cd server
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

**macOS / Linux：**
```bash
cd server
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

### 4. 生成 MCP 客户端配置
将后台服务指向 `server/server.py`（stdio 传输）：

```json
{
  "mcpServers": {
    "da-unreal-mcp": {
      "command": "C:\\...\\DAUNreal_MCP\\server\\.venv\\Scripts\\python.exe",
      "args": ["C:\\...\\DAUNreal_MCP\\server\\server.py"]
    }
  }
}
```

### 5. 自测
MCP 服务 stdio 端到端自测（不依赖编辑器）：

```powershell
server\.venv\Scripts\python.exe server\selftest.py
```

直连插件连通性测试（需编辑器已启动）：

```powershell
server\.venv\Scripts\python.exe server\test_bridge.py "unreal.log('hello from MCP')"
```

---

## 🛠️ 工具列表

### 脚本执行与内省
- `execute_python(code)`：核心直通通道。在 UE 编辑器 Python VM 执行任意 Python 脚本，命名空间跨调用持久化（REPL 风格），自动注入 `da` helper。使用 `print(...)` 返回输出。
- `python_help(target)`：内省对象 / 类 / 函数，返回类型、公开成员（`dir`）与 docstring，用于写脚本前发现 `unreal` API。
- `reset_session()`：清空共享命名空间中的用户变量，保留 `unreal` 与 `da` helper。

### 内置 `da` helper
- `da.dump(obj, depth=3)` / `da.dumps(obj, depth=3)`：UObject / struct / 数组 → 可读 dict / JSON。
- `da.u(path)`：加载资产（`unreal.load_asset`）。
- `da.cls(name)`：加载类（`unreal.load_class`）。
- `da.selected()` / `da.all_actors()`：列出当前选择 / 全部关卡 actor。
- `da.reset()`：重置命名空间。

---
