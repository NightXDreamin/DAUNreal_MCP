# DAUnreal MCP — 进度与恢复文档

> 用途：跨会话 resume 的状态快照。恢复时先读本文件 + README.md，再按「常用命令」继续。

## 1. 目标（第一阶段）

交付一个「脚本直通」式 UE MCP：**不搞一堆离散 toolset，只暴露一个 `execute_python` 直通工具**，让 AI 直接写 `unreal.*` Python 脚本，由 UE 5.4 编辑器内的自定义 C++ 插件在游戏线程执行，结果回传。

验收点：MCP 服务 ↔ TCP 插件 ↔ UE Python VM 端到端连通，能执行脚本并拿到结果。

## 2. 当前状态总览

| 项 | 状态 |
|---|---|
| 架构设计（C++ 插件 = 传输壳，Python 直通执行） | ✅ 完成 |
| C++ 插件编译 / 部署 / 加载 / 监听 8765 | ✅ 验证通过 |
| Python 脚本在 UE 游戏线程执行 | ✅ 验证通过（日志出现 `LogPython: hello from MCP`） |
| MCP 服务（mcp 2.0 MCPServer，stdio） | ✅ 自测通过（initialize / tools/list / tools/call） |
| **响应回传** | ✅ 已修复：`TJsonWriter` 默认 pretty 输出含换行，与 NDJSON 换行分隔冲突；改用 condensed 单行 JSON |

## 3. 关键环境事实

- **UE**：`D:\Epic Games\UE_5.4`（工程 `EngineAssociation: 5.4`）
- **目标工程**：`C:\Users\qingpulou\Documents\Unreal Projects\DAUNrealTest`
- **插件部署位置**：`<工程>\Plugins\DAUnrealMCP\`（由 `deploy.ps1` 从本仓库 `plugin/` 拷入）
- **端口**：`127.0.0.1:8765`（可在工程 `Config\DefaultEngine.ini` 的 `[DAUnrealMCP] Port=` 改）
- **Python**：`C:\Python314`（3.14，默认）、`C:\Users\qingpulou\AppData\Local\Programs\Python\Python311`（3.11，MCP 用）
- **MCP venv**：`server\.venv`（Python 3.11 + `mcp 2.0.0`）
- **工作目录**：`C:\Users\qingpulou\Documents\GitHub\DAUNreal_MCP`
- **Visual Studio**：VS2022 Community（`C:\Program Files\Microsoft Visual Studio\18\Community`）

## 4. 已核实的 UE 5.4 真实 API（重要，别再猜）

- `IPythonScriptPlugin::Get()` 是静态方法，返回 `IPythonScriptPlugin*`；`IsPythonAvailable()`。
- `ExecPythonCommandEx(FPythonCommandEx&)` 返回 `bool`，**结果回写**到入参：
  - `FPythonCommandEx.Command`（代码）、`ExecutionMode`、`FileExecutionScope`、`Flags`
  - `FPythonCommandEx.CommandResult`：成功时 Eval 模式是结果 repr，失败时是 traceback
  - `FPythonCommandEx.LogOutput`：`TArray<FPythonLogOutputEntry>{Type, Output}`
  - 枚举：`EPythonCommandExecutionMode::{ExecuteFile, ExecuteStatement, EvaluateStatement}`、`EPythonFileExecutionScope::{Private, Public}`、`EPythonCommandFlags::None`
- **多语句脚本用 `ExecuteFile` + `FileExecutionScope::Public`**（Public = 共享 console 命名空间 → REPL 持久化）。
- `FTcpListener` 在 `Networking` 模块（头文件 `Common/TcpListener.h`），自带线程，通过 `OnConnectionAccepted()` 委托回调；**委托返回 false → 关闭并销毁连接 socket**。
- `FIPv4Address::InternalLoopback`（127.0.0.1）。
- `FSocketBSD::Recv`：非阻塞无数据时返回 `true` + `BytesRead==0`（WOULDBLOCK）；真正 EOF（对端关闭）返回 `false`。**这是上一轮「连接立刻重置」的根因，已修复**。

## 5. 剩余 bug 的精确证据

编辑日志（`Saved\Logs\DAUNrealTest*.log`）：
```
[DAUnrealMCP] Bridge listening on 127.0.0.1:8765
LogPython: hello from MCP                          ← Python 已执行
[DAUnrealMCP] SendLine length=55                   ← 响应 JSON 完整序列化（55 字符）
[DAUnrealMCP] Send progress 56/56                  ← 55 + '\n' 全部交给 socket
```
客户端（`test_bridge.py` / 原始 dump）实际只收到：
```
RAW BYTES: b'{'    ← 只有 1 字节，随后连接被重置/关闭
```

结论：**序列化没问题、Send 也「成功」了，但数据在连接关闭时被丢弃（RST），客户端只拿到第一个字节。**

## 6. 假设与下一步排查

1. **头号嫌疑**：`FTcpListener` 的委托 `HandleConnection` 返回 `false` → `ConnectionSocket->Close()`（`closesocket`）。若发送缓冲区仍有未投递数据时关闭，Windows 会发 RST 丢弃缓冲数据。需确认「为什么发送后循环就退出了」——理论上 `while(!bStopping)` 里 `SendLine` 之后应继续 `ReadLine` 阻塞等待下一条，不该立刻返回 false。
2. 待查：`FSocketBSD::Close()` 实现（是 `shutdown`+`close` 还是直接 `closesocket`）；`SetNonBlocking(false)` 是否真的生效。
3. 候选修复方向：
   - **放弃 FTcpListener 委托内联模型**，改成自建 accept 循环 + 每连接一个 `FRunnable` 线程，连接保持打开直到客户端断开才 close（最推荐）。
   - 发送后 `shutdown(SD_SEND)` 优雅关闭，或发送后短暂延迟再 close。
   - 检查是否每请求都在新建/关闭连接（应复用长连接）。

## 7. 常用命令（resume 直接抄）

```powershell
# 部署插件到工程
powershell -ExecutionPolicy Bypass -File C:\Users\qingpulou\Documents\GitHub\DAUNreal_MCP\deploy.ps1

# 原位编译插件（增量，~5s）
& "D:\Epic Games\UE_5.4\Engine\Binaries\ThirdParty\DotNet\6.0.302\windows\dotnet.exe" `
  "D:\Epic Games\UE_5.4\Engine\Binaries\DotNET\UnrealBuildTool\UnrealBuildTool.dll" `
  UnrealEditor Win64 Development `
  -Project="C:\Users\qingpulou\Documents\Unreal Projects\DAUNrealTest\DAUNrealTest.uproject" `
  -plugin="C:\Users\qingpulou\Documents\Unreal Projects\DAUNrealTest\Plugins\DAUnrealMCP\DAUnrealMCP.uplugin" -WaitMutex

# 启动编辑器
& "D:\Epic Games\UE_5.4\Engine\Binaries\Win64\UnrealEditor.exe" "C:\Users\qingpulou\Documents\Unreal Projects\DAUNrealTest\DAUNrealTest.uproject"

# MCP 服务 stdio 自测（不依赖编辑器）
& "C:\Users\qingpulou\Documents\GitHub\DAUNreal_MCP\server\.venv\Scripts\python.exe" "C:\Users\qingpulou\Documents\GitHub\DAUNreal_MCP\server\selftest.py"

# 直连插件测试（需编辑器已启动）
& "C:\Users\qingpulou\Documents\GitHub\DAUNreal_MCP\server\.venv\Scripts\python.exe" "C:\Users\qingpulou\Documents\GitHub\DAUNreal_MCP\server\test_bridge.py" "unreal.log('hello from MCP')"
```

改完插件后：**kill 占用 8765 的编辑器进程 → deploy.ps1 → 原位编译 → 重新启动编辑器 → 轮询 8765 → test_bridge.py**。

## 8. 文件清单

- `plugin/DAUnrealMCP.uplugin` — 插件描述（已声明依赖 PythonScriptPlugin）
- `plugin/Source/DAUnrealMCP/DAUnrealMCP.Build.cs` — 依赖 Sockets/Networking/Json/PythonScriptPlugin
- `plugin/Source/DAUnrealMCP/Public/DAUnrealMCP.h` — 模块类
- `plugin/Source/DAUnrealMCP/Private/DAUnrealMCP.cpp` — StartupModule 读端口 + 启桥
- `plugin/Source/DAUnrealMCP/Private/DAUnrealMCPBridge.h/.cpp` — FTcpListener + 游戏线程执行 + NDJSON（**当前 bug 所在**）
- `server/server.py` — mcp 2.0 `MCPServer` + `execute_python` 工具 + TCP client
- `server/selftest.py` — stdio 端到端自测
- `server/test_bridge.py` — 直连插件连通性测试
- `deploy.ps1` — 部署脚本
- `README.md` — 使用说明（已基本齐全）

## 9. 待接入：参考 MCP

用户手头有一个「一堆 tool」的参考 MCP，下一步对照它：
- 看它的 socket 通信 / 线程模型 / 连接生命周期（重点解决第 6 点的关闭时机问题）
- 看它的结果序列化（UObject → JSON）与工具组织方式
- 决定是否采纳其「多 tool」做法，还是维持我们「单一直通 + 少量必要工具（≤10）」的既定方向

## 11. 第一阶段结论（已连通 ✅）

**端到端已打通**：MCP 服务（stdio）→ `execute_python` → TCP(NDJSON) → 插件 → UE Python VM → 结果回传。
验证结果：`execute_python("unreal.log('hello')")` 返回 `{"ok":true,"log":"hello from MCP"}`；REPL 持久化跨连接生效。

**响应回传 bug 的真正根因**（之前一直误判为 socket 关闭时机问题）：
- `TJsonWriterFactory<>::Create(&Str)` 默认是 **pretty 输出**（含 `\r\n\t` 换行缩进），而我们的 NDJSON 用 `\n` 作分隔符。
- 客户端读到 pretty JSON 第一个字段后的 `\n` 就停，只拿到 `{`。
- 修复：改用 `TJsonWriter<TCHAR, TCondensedJsonPrintPolicy<TCHAR>>` 输出**单行** JSON（见 `SerializeCondensed`）。

**顺带做的重构**（借鉴 Mochi 的 `FMochiHttpServer`）：
- 弃用 `FTcpListener` 委托，改为自管 `FRunnable` accept 循环（`HasPendingConnection` 轮询）。
- 每请求一连接（one-shot）：读一行 → 游戏线程执行 → 发一行 → 关 socket。
- listen/accepted socket 显式 `SetNonBlocking(false)`。

## 12. 第二阶段结论（已完成 ✅）

- `da` 序列化 helper：MCP 服务自动注入编辑器命名空间（`da.dump`/`da.dumps` + `u`/`cls`/`selected`/`all_actors`/`reset`），UObject → 可读 JSON 已验证（`{"type":"Object","class":"WorldDataLayers",...}`）。
- `python_help(target)` 内省工具：`dir` + doc，已验证（EditorActorSubsystem 56 成员）。
- `reset_session()`：清空用户变量、保留 `unreal`/`da`，已验证。
- 工具总数 3（execute_python / python_help / reset_session），均为环境元工具，符合脚本直通定位。

## 10. 已定的后续路线（未实现）

- 流式输出 / 长任务异步
- PIE / 运行时支持（加 Runtime 模块）
