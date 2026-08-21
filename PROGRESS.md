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

## 13. 第三阶段结论：Undo 事务包裹（已完成 ✅）

- **改动**：`DAUnrealMCPBridge.cpp` 的 `ExecuteOnGameThread` 内用 `FScopedTransaction`（作用域块）包住 `ExecPythonCommandEx`；`DAUnrealMCP.Build.cs` 的 `PrivateDependencyModuleNames` 加 `UnrealEd`。
- **验证（UE 5.5.4 实机）**：`spawn_actor_from_class` / `set_actor_location` 执行后 `GEditor->Trans->CanUndo()==true`（日志 `DIAG CanUndo=1`），改动已进 undo buffer，用户在编辑器内 Ctrl+Z 可回滚。
- **关键坑（务必记住）**：`TRANSACTION UNDO` 不能通过 `execute_python` 触发——脚本本身也被包在事务里，`UTransBuffer::CanUndo()` 在 `ActiveCount>0` 时直接返回 false（"Can't undo while action is in progress"）。撤销必须由用户在编辑器内 Ctrl+Z（事务外）触发，这是设计意图，不是 bug。
- **注意**：`EditorActorSubsystem::SpawnActor` 内部不开事务也不调 `Modify`（走 `TryPlacingActorFromObject`），但新 actor 是 `RF_Transactional`，配合外层 `FScopedTransaction` 仍能进 undo buffer（已实测 `CanUndo=1`）。

## 14. 第四阶段结论：异步 + 进度 + 取消（已完成 ✅）

- **AST 变换层**（`server/da_async.py`）：给循环体末尾注入 `yield`、把脚本包成 generator 函数并注入 `global`（保 REPL 持久化），产出 `setup_code` / `step_code` / `line_map` / `steppable`。纯 CPython 12 用例 + UE 真机 12 用例语义等价验证全过。
- **插件侧**（`DAUnrealMCPBridge`）：协议扩展 `action=execute(mode=sync|async)/poll/cancel`；job 表 + FTSTicker 按 4ms 时间预算逐帧推进；取消 = `g.close()` → GeneratorExit → `finally` → 每分片 `FScopedTransaction` 正确 End；单 job 并发限制；poll 返回 `status/slices_done/output/error`。
- **server 侧**：`execute_python(code, ctx, mode="sync|async")`；async 走 submit→轮询（0.5s）→`ctx.report_progress(slices)`；`mode` 默认 sync 完全兼容。
- **实机验证（UE 5.5.4）**：500 万次迭代 job 执行期间 9 次 sync ping 延迟仅 24-36ms（编辑器不冻结）；stdio 端到端 async 成功 + progress 通知 + 结果正确；客户端取消后 job 槽位释放、新 job 可跑。

### 关键坑（务必记住）

1. **`time.monotonic()`/`monotonic_ns()` 在部分 Windows Python 是毫秒粒度**（GetTickCount64 支撑，相邻调用差值 0），会让亚毫秒预算永不触发 → 预算检查必须用 `time.perf_counter_ns()`（QPC，100ns）。
2. **`AddTicker(TCHAR*, float, TFunction)` 重载 5.4/5.5 签名不一致**（`TFunction`→`TUniqueFunction&&`）→ 必须用 `AddTicker(const FTickerDelegate&, float)`，且返回值类型是 `FTSTicker::FDelegateHandle`（非通用 `FDelegateHandle`）。
3. **工具注入的 `Context`（`mcp.server.mcpserver.context`）没有 `cancel_requested`**；mcp 2.0 默认 `peer_cancel_mode="interrupt"`，客户端 `notifications/cancelled` 会让 handler 的 await 抛 `CancelledError` → 捕获后在 `anyio.CancelScope(shield=True)` 里发 cancel 并等确认。
4. **progress 通知需要请求带 `_meta.progressToken`**（不带 token 时 `report_progress` 不产生通知）。
5. **无循环脚本 `steppable=False`**（`_da_gen` 不是 generator，`next()` 会崩）→ server 回退 sync 执行。
6. **AST 变换后 traceback 行号偏移** → `line_map` 做顶层语句粒度映射（`File "<string>", line N` 正则替换还原）。
7. server 多端口：`DAUNREAL_MCP_PORT` 环境变量（5.4 工程 8765 / 5.5 工程 8766 各跑一个实例）。

## 15. 第五阶段结论：知识层（已完成 ✅）

- **resource**（`@server.resource`）：`daunreal://subsystems`（8 个常用 Editor Subsystem 对照表）、`daunreal://deprecated-api`（Editor*Library → Editor*Subsystem 映射）、`daunreal://conventions`（工程约定）。内容经实测 `dir(unreal)`/`dir(subsystem)` 背书，不编造 API。
- **`python_search(keyword)`**（`@server.tool`）：`dir(unreal)` 大小写不敏感子串匹配，分类（class/函数）+ 排序 + 每类截断 40。只搜顶层名——方法名（如 `spawn_actor_from_class`）需先搜类名再 `python_help`。
- **prompt**（`@server.prompt`）：`batch-process-assets`（async 顶层循环 + `AssetRegistryHelpers.get_asset_registry().get_all_assets()` + `unreal.load_asset(package_name.asset_name)`）、`scene-inspection`（`EditorActorSubsystem.get_all_level_actors()` + `da.dumps`）。
- **独立 QA 修复收口**（另一个 AI）：`_collect_stmt` 补 `ast.While` 分支（否则 while 体内赋值丢 global）；async 路径补 `DA_PRELUDE` 注入（独立请求发送，不能拼进 setup_code）；`ast.Lambda` 死代码清理 + walrus 统一 `_collect_walrus`；`execute_python` docstring 写明三个 `steppable=False` 限制。新增 `test_da_async_edgecases.py`（41 项）+ `test_async_realeditor.py`（18 项真机）可进 CI。
- **验证**：三套测试 12+41+18 全绿；resource/python_search/prompt 均经 stdio 真机验证。

### 关键坑（知识层新增）
1. `AssetData` 没有 `object_path` 属性（实测报错）；取路径用 `str(a.package_name) + "." + str(a.asset_name)`，加载用 `unreal.load_asset(path)`。
2. `python_search` 只搜 `unreal` 模块**顶层**名字，方法名搜不到（那是 `python_help` 对具体类 `dir` 的职责）。
3. `@server.prompt` 返回 `str` 时 SDK 包装成 `{"role":"user","content":{"text":...}}`，客户端取内容要兼容 dict。

## 16. 第六阶段结论：dry_run + token 鉴权 + 审计日志（已完成 ✅）
- **dry_run**（server 纯 Python）：`execute_python(dry_run=True)` 用 AST 静态分析（`DRY_RUN_DANGEROUS` 集合：delete_asset/destroy_actor/set_editor_property/spawn/save/rename/duplicate/modify 等），报告危险调用 + 循环/import 结构 + 风险分级（HIGH/MEDIUM/LOW），不连 bridge、不执行。真机验证 actor 138→138。
- **token 鉴权**（插件 C++ + server）：插件启动生成 `FGuid` token 写 `<Saved>/DAUnrealMCP/endpoint.json`（含 token+port），每请求验证 `token` 字段（不匹配返回 unauthorized；写失败时 `AuthToken` 置空禁用鉴权）。server 读 `DAUNREAL_MCP_ENDPOINT` 环境变量指向的 endpoint.json（按 mtime 缓存，编辑器重启换 token 自动刷新），每请求带 token。
- **审计日志**（插件 C++）：`LogHistory()` 每次执行写 `<Saved>/DAUnrealMCP/history.jsonl`（追加，ts/mode/code/ok/error）；sync 记录原始 code，async 记录 server 传的原始 `code` 字段。
- **验证**：无/错 token → unauthorized、正确 token → 执行成功；history.jsonl 落盘 2 条（sync+async 含原始 code）；sync/async 回归全过。

### 关键坑（第六阶段新增）
1. **`FFileHelper::SaveStringToFile` 的 FileManager 参数不能传 `nullptr`**：UE5.5 签名 `(FStringView, const TCHAR*, EEncodingOptions, IFileManager* = &IFileManager::Get(), uint32 WriteFlags)`，实现第一行直接 `FileManager->CreateFileWriter(...)` 无空检查，传 `nullptr` → EXCEPTION_ACCESS_VIOLATION。要么省略（用默认 `&IFileManager::Get()`），要么显式传 `&IFileManager::Get()`。
2. token 鉴权是「防本机其他进程乱连」的轻量防护，非强安全（endpoint.json 本机可读）；`DAUNREAL_MCP_ENDPOINT` 未设置时请求不带 token，插件 token 文件写失败时也会自动禁用鉴权（向后兼容）。

## 17. 第七阶段结论：截图回传（已完成 ✅）

- **`screenshot(width=1280, height=720)`**（server 纯 Python，零 C++）：生成唯一文件名 → 触发 `unreal.AutomationLibrary.take_high_res_screenshot(w, h, fn)` → 打印 `unreal.Paths.screen_shot_dir()` 解析目录 → 轮询文件落盘（10s 超时）→ 返回 `Image(path)`。
- **图片回传**：mcp 2.0 的 `Image(path).to_image_content()` 自动转 `ImageContent(type="image", data=base64, mime_type="image/png")`（`func_metadata._convert_to_content` 支持 Image helper）；客户端直接显示。
- **验证**：真机截图返回 image content（1413912 base64，PNG 魔数）；失败容错（连接失败返回 ERROR 文本不崩溃）；三套测试 12+41+18 回归全绿；selftest tools 现 5 个。

### 关键坑（第七阶段新增）
1. **截图命令是 `AutomationLibrary.take_high_res_screenshot`（不是 HighResShot）**：`unreal.HighResShot` 不是 Python 函数（HighResShot 只是 console 命令）；`unreal.AutomationLibrary.take_high_res_screenshot(res_x, res_y, filename)` 是 `UAutomationBlueprintFunctionLibrary` 的 Python 别名，截图存到 `unreal.Paths.screen_shot_dir()`（`Saved/Screenshots/WindowsEditor/`），默认 `force_game_view=True` 在编辑器态可用。
2. **双编辑器进程会导致 token 冲突**：旧进程占端口 + 新进程写新 endpoint.json，server 读到新 token 却被旧进程拒（unauthorized）。单实例前提（用户明确）下，切进程要确保先杀干净（`tasklist //FI "IMAGENAME eq UnrealEditor.exe"` 确认无残留）再启。

## 17. QA 复审（2026-08-17，发现并修复 2 个真实缺陷）

对第三、四阶段成果做独立 QA（32 项纯 CPython 对抗性用例 + 18 项 UE 5.5.4 真机用例），**发现 2 个此前测试未覆盖的真实缺陷，已修复并回归验证**。

### 缺陷 1（较严重）：`_collect_stmt` 缺 `ast.While` 分支 → while 循环体赋值静默丢失

- **现象**：`while` 循环体内的赋值不会进入 `global` 声明，变换后这些变量**不落回共享命名空间**，静默破坏 REPL 持久化。
  ```python
  counter = 0
  while counter < 4:
      doubled = counter * 2   # <- 变换前生成 global counter, collected（缺 doubled）
      counter += 1
  ```
  实测：原版 ns 有 `doubled=6`，变换后**丢失**。
- **为什么之前没发现**：`_collect_stmt` 有 `For` 分支但完全没有 `While` 分支；原回归测试的 "while loop" 用例只检查了循环变量本身（`n`），没有检查循环体内新赋的变量。缺陷表现为"少一个变量"而非报错，非常隐蔽。
- **修复**：补 `ast.While` 分支（递归 body/orelse）。

### 缺陷 2：async 路径未注入 `DA_PRELUDE` → async 脚本用不了 `da.*`

- **现象**：`_run_async` 直接提交 `transformed.setup_code`，不含 `da` helper 定义。新会话（编辑器刚启动 / 刚 `reset_session`）里 async 脚本调 `da.all_actors()` 会 `NameError`。而 `execute_python` 的 docstring 明确宣传了 `da` 可用 —— sync 能用 async 不能用，行为不一致。
- **修复**：submit 前把 `DA_PRELUDE` 作为**独立请求**执行（不能拼进 `setup_code`：那是 AST 变换的产物，再解析会把 helper 包进 generator 并给其内部循环注入 yield）。prelude 自带 `if "da" not in globals()` 幂等保护，开销可忽略。

### 顺带清理

- `_collect_stmt` / `_inject_stmt` 的 `isinstance(..., ast.Lambda)` 分支是**死代码 + 潜在崩溃点**：`ast.Lambda` 是表达式不是语句（`f = lambda x: x` 解析为 `Assign`），永远不会走到；且 `ast.Lambda` **没有 `.name` 属性**，一旦走到就 `AttributeError`。已移除。
- walrus 收集原先只扫 `ast.Expr` 内的 `NamedExpr`，漏掉 `while`/`if` 测试式、`for` 迭代式、`with` 上下文式、`match` subject 里的 walrus。已抽出 `_collect_walrus()` 统一处理。
- 删除未使用的 `_DA_RELEASE_SENTINEL` 常量。

### QA 覆盖范围（供后续回归参考）

- **AST 层 32 项**（`qa_da_async.py`）：for/while + break/continue/else、for-else 有无 break、4 层嵌套、try/finally、`except as e`、with、walrus、comprehension 独立作用域、循环变量泄漏、tuple 解包、下标 augassign、match、用户已有 `global`、全部绑定形式的名字收集、line_map 与 traceback 映射、SyntaxError 透传、空脚本。
- **真机 18 项**（`qa_fixes_realeditor.py`）：两个修复验证 + async 端到端（da 可用 / progress 通知 / 错误 traceback / 无循环回退）+ job 生命周期（submit/poll/cancel）+ 250 万次迭代期间同步 ping **10–34ms**（编辑器不冻结）+ 未知 job_id 不崩桥。

### 已确认的设计限制（非缺陷，但应写进文档告知用户）

1. **循环在函数内 → `steppable=False`**：`def f(): for ...` 然后 `f()`，顶层无循环，无法分片，回退 sync 执行会阻塞。
2. **纯 comprehension → `steppable=False`**：`[x for x in range(100000)]` 是独立作用域，不能注入 yield，仍会阻塞游戏线程。
3. **单条巨型阻塞调用无切点**：如一次 `build_lighting()`，无法分片。
4. 以上三种情况建议在 `execute_python` docstring 里明说，让 AI 写脚本时把循环放在顶层。（已于第五阶段写入 docstring ✅）

## 18. QA 复审：知识层（2026-08-17，38 项全绿，0 缺陷）

知识层的风险点不是崩溃，而是**内容准确性**——resource 里若写了不存在的 API，会主动误导 AI，比没有知识层更糟。所以本轮 QA 的核心是**把文档里提到的每个类和方法都拿到运行中的 UE 里核对**（`server/test_knowledge_layer.py`，38 项，UE 5.5.4 真机）。

### 事实核对结果（全部通过）

| 核对项 | 方法 | 结果 |
|---|---|---|
| `daunreal://subsystems` 的 9 个 Subsystem 类 | `hasattr(unreal, cls)` | **0 处不存在** |
| 同资源里声明的 **40 个方法** | `method in dir(cls)` 逐一比对 | **0 处不存在** |
| `daunreal://deprecated-api` 的 4 个旧类 + 6 个新类 | 双侧 `hasattr` | 全部存在 |
| 7 组旧→新方法映射（spot-check） | 两侧 `dir()` 均需命中 | 全部解析成功 |
| `daunreal://conventions` 声明的 7 个 `da.*` helper | `hasattr(da, m)` | 全部存在 |
| `batch-process-assets` 模板 | 原样执行 | 遍历 **7349** 个资产，`load_asset` 成功 |
| `scene-inspection` 模板 | 原样执行 | 输出 **138** actor + `da.dumps` 正常 |
| 已记录的坑「`AssetData` 无 `object_path`」 | `hasattr` | 仍然成立 ✅ |

### MCP 表面与工具行为

- `initialize` 正确宣告 `resources` / `prompts` capability；`resources/list`+`read`、`prompts/list`+`get`、`tools/list` 全部可用。
- 工具数仍为 **4**（execute_python / python_help / python_search / reset_session），保持精简定位。
- `python_search`：命中、大小写不敏感、未命中回 `(none)` 而非报错、宽泛搜索（`"a"` 命中 6814 类）**正确截断且输出仅 1534 字符**（不会灌爆上下文）、文档声明的「方法名搜不到」限制成立。

### 新增契约验证（本轮补的用例）

- **`reset_session` 可重复调用**：其实现依赖 `da.reset()`，若 `da` 把自己删掉则第二次调用会 `NameError`。实测 `da` 与 `unreal` 在 reset 后均保留、用户变量被清空 ✅

### QA 方法论踩坑（写给下次的自己）

- **`da` 只在 `execute_python` 路径注入**（`_run(code)` 默认 `prelude=True`）。直连 TCP 桥接测 `da.*` 会得到 `NameError`——那是**测试方法错**，不是产品缺陷。要测 `da` 必须自己拼 `DA_PRELUDE`（`test_knowledge_layer.py` 里的 `call_with_prelude()`）。
- `python_help` / `python_search` 走 `prelude=False`，它们只用 `unreal` 与内建，不依赖 `da`，符合设计。
- 从插件回传的日志行**带 `\r`**（`log` 里是 `\r\n`），用 `startswith("MARKER:")` 取值后必须 `.strip()`，否则 `== "NONE"` 恒为假，产生一批假 FAIL。
- 用正则从 markdown 抓类名时注意占位符：`unreal.<Subsystem>` 会被 `(\w*Subsystem)` 抓成裸词 `Subsystem`，需排除。

### 文档修正

- 原有两个章节都编号为「## 15」（知识层、QA 复审），已把后者改为「## 16」，本节为「## 17」。

## 19. QA 复审：第六阶段安全层（2026-08-17，发现并修复 5 个缺陷 + 1 处测试基建断裂）

对 dry_run / token 鉴权 / 审计日志做对抗性 QA（85 项：47 项纯 Python + 38 项 UE 5.5.4 真机）。
安全功能的 QA 原则是**尝试绕过**，而不是确认 happy path。

### 缺陷 1（安全，已修复）：token 比较大小写不敏感

- **现象**：token 是全大写 GUID（`D192D547-...`），但**小写版本同样被接受**。
  实测 `exact` / `UPPER` / `lower` 三种写法全部 `ok=True`。
- **根因**：`FString::operator==` 在 UE 里是**大小写不敏感**的。直接 `Token == AuthToken`
  让 GUID 的十六进制字母位 A↔a 等价，凭空缩小猜测空间。
- **修复**：改用 `Token.Equals(AuthToken, ESearchCase::CaseSensitive)`；并把
  `GetStringField` 换成 `TryGetStringField`（字段缺失时明确返回 false，不依赖默认值行为）。

### 缺陷 2（健壮性，已修复）：`da` prelude 无法自愈，坏了只能重启编辑器

- **现象**：prelude 的守卫是 `if "da" not in globals()`。一旦 `da` 存在但它依赖的
  模块级私有名（`_da_json`）丢失，守卫短路 → **重新注入也修不回来**，`da.dumps`
  从此永久 `NameError`，唯一出路是重启编辑器。
- **修复**：① 守卫改为**完整性探测**（真的调一次 `da.dumps({"_":1})`，异常即重建）；
  ② `dumps` 改用函数内 `import json as _j`，不再依赖模块级私有名。
- **验证**：删掉 `_da_json` 后 `dumps` 仍正常（本地 import 免疫）；把 `da` 换成 `None`
  后重新注入能自动重建 ✅

### 缺陷 3（健壮性，已修复）：`reset()` 同样依赖易失的模块级名

- 上面修好 `dumps` 对 `_da_json` 的依赖后，**同类问题在 `reset()` 复现**：它依赖
  `_da_protected`，删掉后 `da.reset()` 永久 `NameError`。而当时的完整性守卫只探测
  `dumps`，所以检测不到 `reset` 已损坏。
- 这暴露了一个模式：**只要 helper 依赖模块级私有名，就存在同一类脆弱性**。
- 修复：`_da_protected` 的快照改存到类属性 `_Da._protected`（不在 globals 里，不会被
  清理逻辑误删）；完整性守卫同时校验 `dumps` 可用 **且** `_Da._protected` 是 set。
- **验证 5 种损坏场景全部自愈**：删 `_da_protected` / 删 `_da_json` / `da` 换成坏对象 /
  `da` 整个删除 / 连续 reset 两次 —— 全部 ok ✅
- 附带修掉一个测试污染问题：此前 security 套件跑完会让 knowledge 套件挂 3 项，
  现在按任意顺序连续跑六套都全绿。

### 缺陷 4（健壮性，已修复）：`_load_token` 只捕获 `OSError`

- 半写入 / 被截断的 `endpoint.json`（编辑器启动那一瞬就是这个状态）会抛
  `json.JSONDecodeError`（`ValueError` 子类），逃出 except → **每个请求都炸**。
- 修复：捕获 `(OSError, ValueError, UnicodeDecodeError)` 并校验顶层是 dict，
  任何异常都降级为「无 token」而非抛出。

### 缺陷 5（可维护性，已修复）：审计日志无大小上限

- 单条 28896 字符原样落盘。`history.jsonl` 是 append-only，长期无界增长，
  且把脚本里的字符串逐字留在磁盘上。
- 修复：`LogHistory` 对 `code` / `error` 各截断到 4000 字符并标注
  `... [truncated, N chars total]`。实测 28896 → 4034 ✅

### 测试基建断裂（重要教训）

鉴权上线后，**两个既有真机套件大面积失败**（`test_knowledge_layer.py` 19/38 fail、
`test_async_realeditor.py` 9/18 fail），因为它们直连桥接不带 token。这不是产品缺陷，
但不修的话，以后每轮 QA 都会被假 FAIL 淹没。

- 修复：两个套件加 `_auth_token()`（按 `DAUNREAL_MCP_ENDPOINT` → 项目 `Saved/` 顺序解析，
  并**校验 endpoint.json 里的 port 与被测端口一致**），在 `raw()` 里统一注入；
  spawn 的 stdio server 子进程也补 `DAUNREAL_MCP_ENDPOINT`。
- **教训：新增鉴权 / 协议字段时，必须同步升级所有既有测试套件**，否则测试资产会静默腐化。

### dry_run 覆盖面补强（19 个漏报 → 0）

原 `DRY_RUN_DANGEROUS` 只有约 20 个名字，实测 **19/19 个变更类操作漏报**：

- 变换类：`set_actor_location` / `set_actor_transform` / `set_actor_rotation` /
  `set_actor_scale3d`（悄悄移动东西，肉眼最难发现）
- 关卡类：`new_level` / `load_level`（**丢弃未保存改动**）/ `save_current_level` /
  `save_all_dirty_levels`
- 资产类：`make_directory` / `rename_directory` / `create_asset` / `import_asset_tasks`
- 其他：`delete_actor` / `destroy_component` / `attach_to_actor` / `build_light_maps` /
  `editor_request_begin_play`（启动 PIE）/ undo-redo

并补上**逃逸手段的显式披露**（原先 7/8 完全静默通过）：

- 新增 `DRY_RUN_OPAQUE`（`eval` / `exec` / `getattr` / `__import__` 等动态派发）→
  风险标为 **UNKNOWN**，不再冒充 MEDIUM。
- 新增 `DRY_RUN_EXTERNAL`（`os.remove` / `shutil.rmtree` / `subprocess` 等 unreal
  之外的破坏）→ 风险 HIGH，单独一节列出。
- 报告结尾固定附上「name-based heuristic, **not a sandbox**」告知；docstring 同步写明
  「a clean dry-run report is not a guarantee」，避免 AI 过度信任。

### 已验证的正确行为（真机）

- dry_run **完全不接触 bridge**（spy 拦截到 0 请求）、无本地副作用、**不写审计日志**；
  destroy-all 脚本 dry_run 后 actor 数 138 → 138 未变。
- 鉴权覆盖**所有 action**：execute / poll / cancel / async submit，无 token 一律 unauthorized。
- 空 token、错 token、截断 token 均被拒；缺 `code` 字段等畸形请求不崩桥。
- 审计日志：成功与失败都记录，失败带 error；async 记录的是**原始 code 而非变换后的
  generator**；中文不乱码；每行均为合法 JSON。
- server 通过 `DAUNREAL_MCP_ENDPOINT` 自动鉴权；不设该变量时确实拿到 unauthorized
  （证明鉴权真的在生效，而不是恰好都放行）。

### 保留的观察项（非缺陷）

- 风险分级里「任何调用 → MEDIUM」，所以纯读脚本也是 MEDIUM。真正有区分度的是
  HIGH / UNKNOWN 与其余，LOW 仅在完全无调用时出现。够用，暂不改。

### 测试套件现状：六套 194 项全绿

| 套件 | 需编辑器 | 项数 |
|---|---|---|
| `test_ast_transform.py` | no | 12 |
| `test_da_async_edgecases.py` | no | 41 |
| `test_dryrun_token.py` | no | 47 |
| `test_knowledge_layer.py` | yes | 38 |
| `test_async_realeditor.py` | yes | 18 |
| `test_security_realeditor.py` | yes | 38 |

真机套件用 `DAUNREAL_MCP_PORT` 选端口，并会自动按端口匹配对应工程的 `endpoint.json`
（8765 = 5.4 工程，8766 = 5.5 工程）。

## 20. 环境事实：UE 5.5 Python 反射限制（2026-08-18 实测）

用 `execute_python` 直通在 5.5.4 实测「蓝图 / UMG 资产创建」的边界，结论记录如下
（避免后续再用 5.4 时代的写法）：

| 能力 | 5.5 实测结果 |
|---|---|
| 创建蓝图资产（`BlueprintFactory` + `parent_class`） | ✅ `create_asset` → `compile_blueprint` → `save_asset` → `generated_class()` 生成 `*_C` |
| 蓝图成员变量（`BlueprintEditorLibrary.add_member_variable`） | ❌ 需要属性类对象，但 **5.5 已从 `unreal` 模块移除基础 Property 类**（`IntProperty/FloatProperty/StrProperty/BoolProperty/DoubleProperty/ByteProperty/Int64Property` 全部 `hasattr==False`） |
| 创建 UMG 资产（`WidgetBlueprintFactory`） | ✅ 资产本身能创建 |
| UMG 设计时树（往 WidgetBlueprint 加控件） | ❌ `wb.widget_tree` 不存在；`get_editor_property("WidgetTree")` → **protected**；`unreal.WidgetTree` 顶层类不存在；`unreal_editor` 模块不可用 |
| 运行时 UI（PIE） | ✅ `WidgetBlueprintLibrary.create_widget` 等运行时 API 可用（但非设计时资产） |

结论：
- **蓝图**：Python 直通能创建/编译/保存，但加不了成员变量（属性类被移除）。
- **UMG**：设计时控件树在 5.5 的 Python 反射里被封死，纯 Python 无法填充。
- 替代方案：a) 在本插件 C++ 侧加一个「widget tree 填充 / 蓝图变量添加」action（插件本来就是传输壳，加 action 不违反架构）；b) 在 5.4 工程实测反射是否放开（未验证）；c) 运行时 UI 走 PIE。
- 另一个环境事实：**编辑器每次重启会重新生成 token**（endpoint.json 重写），直连脚本要重读；server 侧按 mtime 缓存自动适配，无影响。

## 21. 环境事实：游戏线程同步导入资产 → TaskGraph 断言崩溃（2026-08-19 实测）

`execute_python` 在**游戏线程**执行脚本。在此线程内调用
`AssetToolsHelpers.get_asset_tools().import_asset_tasks([...])` 导入 FBX 会触发：

```
LogInterchangeEngine: Display: Interchange start importing source [...]
LogWindows: Error: appError called: Assertion failed: ++Queue(QueueIndex).RecursionGuard == 1
[File:...\TaskGraph.cpp] [Line: 677]
```

**编辑器直接崩溃**（两次验证：默认路径与 `FbxImportUI` legacy options 均如此——5.5 里 AssetImportTask 一律走 Interchange）。
根因：Interchange 导入流程内部向 TaskGraph 派发任务并**同步等待**，在游戏线程内同步等待任务图任务 → 递归入队断言。
尝试过的替代：
- `FbxImportUI`/`FbxFactory` legacy options → 仍走 Interchange，同样崩溃。
- `InterchangeManager.scripted_import_asset_async` → 存在但参数结构体 `InterchangeImportAssetParameters` 未暴露给 Python，无法构造调用。

**结论/约定**：MCP 直通**不做资产导入**。FBX 等导入由用户在 Content Browser 手动拖入（iwiki SOP 本来就是这么写的）；MCP 负责导入后的资产构建/生成/验证流程。若未来要脚本化导入，需在插件 C++ 侧加一个「后台线程执行导入」的 action（传输壳加 action 不违反架构）。

## 22. 反射通道验证 + 生成类操作死锁边界（2026-08-19 实测）

**反射通道（`unreal.UObject.call_method`）实测结论**：
- `atf.call_method("GetRelatedAssets")` 成功调用 **Python stub 未暴露的 static 非 BlueprintCallable** 函数（AudioToolFunctions），返回 5 个关联资产 ✅
- 二进制引擎（Launcher 版 5.5.4）可用，无需源码（UClass 反射元数据是运行时数据）✅
- 两种形式都 OK：`call_method("Name")` 与 `call_method("Name", (), {})`
- **settings 跨 execute_python 调用持久性**：此前"跨调用丢失"实为 AssetData 查询用了无后缀路径（写入的就是无效数据）；`get_asset_by_object_path` 必须传完整路径 `/Game/X.X`。完整路径 set 后 + `GetRelatedAssets` 内部 `GetMutableDefault` 写入均跨调用保留 ✅
- `GetRelatedAssets` 自动填充 `skeletonRelatedAssets`（省去手动 set 绕路）

**生成类操作死锁边界修正（重要）**：
- 之前判定 `generate_pose_asset` 稳定安全——**错误**。本次 `call_method("GeneratePoseAsset")` 触发 `FlushRenderingCommands called recursively` **死锁**（成功 3 次后第 4 次死锁，概率性）。
- **规律**：凡涉及「创建资产 + 内部渲染/预览刷新」（PoseAsset 创建、蓝图编译）的操作，在游戏线程都有死锁风险，只是概率不同（导入=必崩，编译=高概率，资产创建=低概率）。
- 弹窗坑：`CheckSlected` 优先读 **Content Browser 选区**，选区有 1 个非目标类型资产就弹模态窗卡死游戏线程（"Please select the asset type of..."）。**约定：调 AudioToolFunctions 走 CheckSlected 的函数前必须先 `sync_browser_to_objects([])` 清选区**；且超时断连的脚本会在编辑器里继续执行，弹窗关闭后会继续跑到死锁——杀进程前先看日志确认。

**架构方向更新**：与其为导入/编译各写专用 action，不如做一个**通用后台执行 action（`run_worker`）**——把任意脚本调度到插件专用工作线程执行（游戏线程不阻塞），一个 action 覆盖全部"生成类/线程敏感"操作。风险：unreal API 在非游戏线程的线程安全性需实验验证（Interchange 导入与蓝图编译官方支持后台，普通 API 部分线程安全）。待验证：工作线程执行 unreal Python 的最小实验。

**run_worker 最小实验结论（2026-08-19）：方案不可行**：
- 在 `execute_python` 内用 `threading.Thread` 起 Python 线程调 unreal API：`unreal.load_asset` 在非游戏线程**返回 None**（加载依赖游戏线程泵），`AssetRegistryHelpers` 也不可用——**unreal Python 绑定大量依赖游戏线程上下文**，通用"后台跑任意 Python"路线证伪。
- **最终架构定稿**：线程问题必须走 **C++ 专用 action**（引擎原生线程模型，不依赖 Python 线程）：
  - `import_assets`：C++ 调 Interchange 异步导入
  - `compile_assets`：C++ 后台编译蓝图（可并入 PoseAsset 等"生成类"——C++ 侧从后台线程调 CoreLink 创建资产，渲染 flush 是正常等待）
  - 反射通道（`call_method`）保留：解决 stub 可见性（只读/设置类操作零 C++）
- 定位：MCP = execute_python（常规/反射）+ 2 个 C++ 线程封装 action；action 数稳定不膨胀。

## 21. 第八阶段：EUW / UMG Helper 落地（已完成 ✅）

- **背景**：在 UE 5.5 中通过 Python 构建 Editor Utility Widget (EUW) 时，资产创建、子控件添加、布局、样式、事件绑定、编译与弹窗全通，唯独 `UWidgetTree::RootWidget` 和 `UWidget::bIsVariable` 在 Python 侧为 protected / 未导出。
- **C++ Helper（`UDAUMGHelper`）**：
  - `SetWidgetTreeRoot(UWidgetTree* Tree, UWidget* RootWidget)` — 解决根控件无法设置导致子控件被 GC 回收的关键痛点。
  - `SetWidgetIsVariable(UWidget* Widget, bool bIsVariable = true)` — 解决控件无法在蓝图中标记为变量的问题。
  - `GetAllWidgets(UWidgetTree* Tree)` — 遍历获取控件树全部控件。
  - 依赖模块：`UMG`、`UMGEditor`。
- **Python 封装**：`DA_PRELUDE` 注入 `da.set_root(tree, root)` 与 `da.set_variable(widget, is_var=True)`；并在 `daunreal://conventions` 资源中建立文档。
- **验证**：部署到 `DAUNrealTest55` 并通过 UE 5.5 UBT 编译通过（0 error, 0 warning）。

## 23. 原生 job：import_assets / compile_assets（2026-08-19 已实现并实测通过 ✅）

**问题**：Interchange 导入在游戏线程请求回调栈崩溃（§21），蓝图编译死锁（§22），`run_worker` 后台线程方案被实验证伪（unreal Python 绑定依赖游戏线程，§22）。

**方案落地**：原生 job 在 **FTSTicker tick 回调**执行（非 TaskGraph 任务上下文），复用现有 job 表 + poll：
- `FDaMCPJob` 加 `Kind`（Python/Import/Compile）+ ImportFilenames/ImportDestinations/CompilePaths/NativeResults
- `SubmitNativeJob` / `RunNativeJob`（tick 里跑）`HasRunningJob`
- 协议：`{"action":"import_assets","tasks":[{"filename","destination_path"}]}` 与 `{"action":"compile_assets","paths":[...]}` → 返回 job_id → 复用 poll/cancel
- C++ 实现：Import 用 `UAssetImportTask` + `IAssetTools::ImportAssetTasks`（**5.5 无 bImportSucceeded，用 `GetObjects().Num()>0` 判成功**）；Compile 用 `FKismetEditorUtilities::CompileBlueprint`
- Build.cs 加 `AssetTools`、`Kismet` 依赖
- server.py：注册 `import_assets` / `compile_assets` 工具 + `_run_native_job` submit→poll 封装

**实测（UE 5.5.4 真机）**：
- `compile_assets` 编译 AnimBlueprint：submit → 1 次 poll 即 done → `ok: /Game/...` → **编辑器存活**
- `import_assets` 导入 Rajesh skeleton.fbx → 3s done → 17 材质 + Skeleton/Mesh/PhysicsAsset 全部落盘 `/Game/RajeshImport/` → **编辑器存活**

**关键验证**：tick 栈（非 AsyncTask(GameThread) 请求回调栈）执行 Interchange 导入与蓝图编译均不再崩溃/死锁 —— TaskGraph 同步等待与 FlushRenderingCommands 在正常 tick 上下文是安全的。**之前"必须手动拖入 FBX / 手动生成蓝图"的两步现在可全托管。**
