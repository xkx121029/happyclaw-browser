# Changelog

本文件记录 `happyclaw-browser`（原 `octop-browser`）的版本变更。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 变更

- 对齐 HappyClaw 主仓的分支策略：引入 `develop` 集成分支策略；禁止直推 `main`/`develop`；发版后由 `sync-main-to-develop.yml` 同步；新增 `/publish` skill（发版同步 CHANGELOG / README）。
- 改进 CLI 帮助信息：新增 `--version`、顶层使用示例和子命令帮助提示，并明确 `install-browser` 中 Playwright 仅用于 Chromium 下载引导。


## [1.0.3] - 2026-11-21

本版全部是**新增能力**，公开 API 只做加法：`BrowserSession.create()` 多了关键字
参数、`capture.screenshot()` 多了两个关键字参数，其余签名不变，既有调用不需要改。
面向的场景只有一个：单人自托管跑自动化时，重复任务不再反复冷启动、等待失败时
不用猜页面到底出了什么事、产物不会把磁盘写爆。

### 新增：会话池（`SessionPool`）

- `happyclaw_browser.pool.SessionPool`：按 profile 复用活着的浏览器会话，冷启动从
  "每次任务一次" 变成 "每个 profile 一次"。`browser_tool()` 现在走这把池子，
  调用方代码不变。
- 上限 `BROWSER_USE_POOL_MAX_SESSIONS`（默认 4）。池满时新的 profile **排队等待**，
  不报错；只有显式设置 `BROWSER_USE_POOL_ACQUIRE_TIMEOUT` 才会在超时后抛
  `PoolExhaustedError`，消息里带 live/inflight/max 计数与调参提示。
- 空闲回收：`BROWSER_USE_IDLE_TIMEOUT_MINUTES` 生效于池级别（会话无租约且超时），
  回收时 `close(kill=True)`，把 Chrome 真正杀掉——只断连会占着 profile 锁和调试端口，
  让下次启动直接失败。
- 崩溃自愈：取用会话前检查 `BrowserSession.is_alive`，浏览器已死的会话就地丢弃并重建，
  不再把一个已经断开的会话交给调用方、让调用方在下一个动作上失败。
- 池内新建会话是**串行**的：同一台机器上并发冷启动只会让每一个都更慢，还会抢同一个调试端口。
- 新增动作：`session_info`（报告会话是否存活、实际使用的 CDP 端口，不会启动浏览器）、
  `pool_status`（live / queued / reclaimed / rebuilt 计数），CLI 对应
  `session-info` / `pool-status`，MCP 对应 `browser_session_info` / `browser_pool_status`。
- 池管理的会话不再启动单会话 idle 看门狗（`enable_idle_reaper=False`），避免两个看门狗
  抢同一个浏览器；直接使用 `BrowserSession` 的用户行为不变。

### 新增：等待原语（`waits`）

- `BrowserSession.wait_for_selector(selector, timeout_ms, state)`、
  `wait_for_navigation(url_contains, timeout_ms)`、
  `wait_for_network_idle(idle_ms, timeout_ms)`，CLI 为 `wait-selector` /
  `wait-navigation` / `wait-network-idle`，MCP 同名工具。
- 超时不再只给一句 `Timeout waiting for condition`：`metadata["wait"]` 里带
  `url` / `title` / `ready_state` / `interactive_elements` / `selector_matches` /
  `selector_visible` / `pending_requests` / `pending_urls` / `screenshot`，外加一句
  `hint` 指出最可能的原因（页面还在加载、页面没有可交互元素、选择器没匹配到、
  元素匹配到但不可见、请求一直没停）。`error` 仍然是一行可读摘要。
- 超时默认落一张截图到 `screenshots_dir`（`screenshot_on_timeout=False` 可关），
  因为"按钮被 cookie 横幅挡住了"这类问题，一张图就说明白。
- 轮询退避 20ms 起、250ms 封顶；`wait_for_network_idle` 统计真实在途请求，
  超时报告会点名卡住的请求（通常是分析/埋点域名从这台机器不可达）。
- `wait_for_navigation` 成功时会作废 ref 缓存，避免把导航前的 nodeId 交给调用方。

### 新增：统一产物 API（`artifacts`）

- `BrowserSession.artifact(kind="screenshot"|"pdf"|"dom", ...)`：一次调用拿截图、
  print-to-PDF 或 DOM 快照，返回 `ArtifactRef`（`kind`/`path`/`format`/`size_bytes`/
  `max_bytes`/`url`/`title`/尺寸），`content` 是文件路径——不再把 base64 塞给调用方。
- 路径规范：`<artifacts_dir>/<kind>/happyclaw-<kind>-<UTC 时间戳>-<序号>.<ext>`，
  `artifacts_dir` 默认 `~/.happyclaw-browser/artifacts`（`BROWSER_USE_ARTIFACTS_DIR` 可改）。
- 体积上限 `BROWSER_USE_ARTIFACT_MAX_BYTES`（默认 16 MiB）：超限的产物**不写盘**并回报真实
  字节数；截图超限时先降级成 JPEG（quality 60）重试一次，仍超限才拒绝。旧版
  `screenshot()` 行为不变（仍写 `screenshots_dir`、无上限），新 API 才带天花板。
- `capture.screenshot()` 新增关键字参数 `image_format` / `quality`（默认值保持 PNG 行为）。

### 新增：调试端口自动避让

- 分配端口前真的去 bind 一下：`BROWSER_USE_CDP_PORT_START` 只表示扫描起点，
  被别的进程（另一个线程的 Chrome、残留浏览器、无关服务）占用就顺延，
  扫描窗口 `BROWSER_USE_CDP_PORT_SCAN`（默认 200）。窗口耗尽时抛
  `RuntimeError` 并说明该改哪个环境变量。
- 启动前再检查一次：若端口被非 CDP 进程占用（`/json/version` 不应答），
  启动器把该 profile 挪到空闲端口并持久化新映射，日志说明换了哪个端口。
  这解决了"profile 建好了、端口后来被别人抢走"的场景。
- 回报**实际**端口：`Profile.cdp_port` 不再等于"环境变量 + 序号"，
  `session_info` 与 `BrowserSession.cdp_port` 给出真实值。

### 修复：启动失败不再留下半初始化的 profile

- Chrome 在绑定端口前死掉（缺库、沙箱拒绝、无桌面会话）会留下 `Local State`、
  `SingletonLock` 和半个 profile，下一次启动直接 exit 21，用户看到同一个错两次。
  现在失败路径会清 pid，并且：**从未成功启动过**的 profile 整个删掉，
  **已经有登录态**的 profile 只清锁文件。测试用"假 Chrome"（会立刻退出的解释器）
  驱动真实失败路径。

### 变更：de-octop（上游标识全量清理）

- 状态目录 `~/.octop-browser`（以及更早的 `~/.harness-browser`）现在**一次性复制**到
  `~/.happyclaw-browser`，profile、cookie、录制一并带过去，并打印一条迁移说明；
  旧目录原样保留供用户自行删除。复制失败（浏览器占着 profile 锁、权限问题）时继续使用
  旧目录并警告，**绝不从空目录开始**——那看起来就是"升级后所有登录都没了"。
  1.0.2 的行为只是"沿用旧目录并提示"，用户还得自己搬；现在不用了。
  复制先落到 `<新目录>.migrating` 再原子改名，中途中断不会留下一个看起来完整的半成品目录。
- 代码中剩余的 octop 字样只保留**只读兼容**，删除会让老用户丢端口映射或登录态：
  `OCTOP_SKIP_PLAYWRIGHT_PIP` / `OCTOP_MULTI_PIP_BOOTSTRAP`（新名优先，旧名仍认并抛
  `DeprecationWarning`）、`.octop-browser-profiles.json` / `.octop-browser-target.json` /
  `.octop-browser-chrome.pid` 三个旧状态文件名（只在读时回退，写入一律 `.happyclaw-browser-*`）。
- README / README_CN 同步改写迁移说明，并补齐本轮新增的 5 个环境变量
  （`BROWSER_USE_CDP_PORT_SCAN`、`BROWSER_USE_POOL_MAX_SESSIONS`、
  `BROWSER_USE_POOL_ACQUIRE_TIMEOUT`、`BROWSER_USE_ARTIFACTS_DIR`、
  `BROWSER_USE_ARTIFACT_MAX_BYTES`）。

### 兼容性

- 无破坏性变更。`BrowserSession.create()`、`capture.screenshot()` 只是多了带默认值的
  关键字参数；`tool_interface._registry` 仍是"profile → 会话"的字典，只是底层换成池的存储。
- 端口分配语义变了：以前固定 `cdp_port_start + index`，现在跳过被占用的端口。
  依赖"端口必须是 9222"的脚本要改成读 `session_info` / `BrowserSession.cdp_port`。


## [1.0.2] - 2026-11-21

本版把命名从 Octop 全量切换到 HappyClaw。**这是一个 breaking release**：
分发名与导入名都变了，升级需要改 `pip install` 目标与所有 `import` 行。
对 Octop 上游的署名见 [ATTRIBUTION.md](ATTRIBUTION.md)。

### 破坏性变更

- **分发名** `octop-browser` → **`happyclaw-browser`**。
  升级：`pip uninstall octop-browser && pip install happyclaw-browser`。
- **导入包** `octop_browser` → **`happyclaw_browser`**。
  所有 `import octop_browser` / `from octop_browser import ...` 需改为新名，
  例如 `from happyclaw_browser import BrowserSession`。
- **CLI 入口** 由 `octop-browser` 改为 `happyclaw-browser`（子命令与参数不变）。
- **配置类** `OctopSettings` → `HappyclawSettings`（字段与构造参数不变），
  同时是 `__all__` 里的导出名变更。
- **MCP 服务标识** 由 `octop-browser` 改为 `happyclaw-browser`。按服务名
  做白名单或配置的 MCP 客户端需要同步更新，否则会认为服务端不见了。
- **logger 名** 随包名变为 `happyclaw_browser.*`；按旧名配置 `logging` 的地方需更新。

### 兼容性

- **环境变量**：`OCTOP_SKIP_PLAYWRIGHT_PIP` → `HAPPYCLAW_SKIP_PLAYWRIGHT_PIP`，
  `OCTOP_MULTI_PIP_BOOTSTRAP` → `HAPPYCLAW_MULTI_PIP_BOOTSTRAP`。
  新名优先；只设旧名时仍然生效，但会抛一次 `DeprecationWarning`，后续版本移除。
  更早的 `HARNESS_*` 同名前缀同样按此规则兜底。
- **数据目录**改为 `~/.happyclaw-browser/`（profiles、screenshots 与其下的状态
  文件一并改名）。升级时若旧的 `~/.octop-browser/` 仍然存在，会**继续使用它**，
  并抛一次 `UserWarning` 告诉你新路径在哪（默认可见，应用可自行过滤）；**不会自动
  搬动任何数据**。
  把旧目录整体 `mv` 到 `~/.happyclaw-browser` 即可完成切换，登录态与历史录制随之过去。
  更早的 `~/.harness-browser/` 在两者都不存在时同样会被沿用。
- **旧目录里的状态文件**同样兼容读取：`.octop-browser-profiles.json`（端口映射）、
  `.octop-browser-target.json`（目标标签页）、`.octop-browser-chrome.pid`（受管
  Chrome 的进程号）会被当作回退来源，新写入一律用 `.happyclaw-browser-*` 新名。
- **`BROWSER_USE_*` 环境变量不变**，取值语义与默认值全部保持。

### 其他

- 新增 [ATTRIBUTION.md](ATTRIBUTION.md)（中英双语），明确本项目基于
  Octop Browser（`TencentCloud/octop-browser`，版权方 OrcaKit，MIT），并说明
  HappyClaw 维护独立分支。
- `LICENSE` 在保留原始版权行的同时追加 `Copyright (c) 2026 xkx121029`。
- README / README_CN 全量切换新名；截图产物文件名前缀由 `octop-` 改为
  `happyclaw-`，Chrome stderr 临时文件前缀与 Chrome 临时 user-data-dir 前缀同步改名。


## [1.0.1] - 2026-11-21

本版把本库纳入 HappyClaw 自维护体系，并修掉若干在 Windows 上会直接影响
可用性的缺陷。

### 修复

- **`close_session --kill` 在 Windows 上不生效**。优雅关闭（CDP
  `Browser.close`）超时后的唯一兜底 `pkill_chrome_profile` 在非 POSIX 平台
  直接返回 `False`，于是浏览器进程从未被停止：user-data-dir 被占、CDP 端口
  被占，之后同一 profile 的每次启动都以
  `Chrome exited immediately (returncode=0)` 失败。现在启动时记录本库自己
  spawn 的 Chrome pid（`.octop-browser-chrome.pid`），优雅关闭失败后用
  `taskkill /T`（Windows）或 `killpg`（POSIX）终止，并在成功后清除记录。
- **录制守护进程在 Windows 上抛出 `AttributeError`**。`send_request` 直接调用
  `asyncio.open_unix_connection`，该属性在 Windows 不存在且不在原有
  `except` 覆盖范围内，调用方看到的是裸 AttributeError 而不是「守护进程未运行」
  的可操作提示；服务端 `run()` 同样问题。两处都改为先探测能力再给出明确说明。
- **`mcp` 依赖约束与代码自相矛盾**。`pyproject.toml` 写 `mcp>=1.9,<3`，而同段
  注释与 `mcp_server.py` 的实现都要求 `<2`（2.x 移除了导入期注册的
  `Server.list_tools()` / `call_tool()`）。干净安装会解析到 2.x 并让 MCP 接口
  整体不可用。约束改为 `mcp>=1.9,<2`。
- **沙箱探测未防护 `os.getuid`**。`_should_disable_sandbox()` 在 Linux 分支裸调
  该函数，与同模块 `launch_prep.py` 的写法不一致，非 POSIX 环境下抛
  AttributeError。
- **启动失败会留下孤儿 Chrome**。`launch_or_attach` 只在「exit 21 / 单例」这一
  个分支回收子进程，其它异常（包括任务取消）逃出时子进程无人持有句柄。
- **CDP 关闭不释放等待者**。`close()` 之后仍挂起的命令要等满 `cdp_timeout`
  （默认 30 s）才返回；被取消的命令会永久留下一条 pending 记录；事件监听器返回
  的协程没有任何引用，可能在执行中途被回收。

### 性能

同一台机器、同一脚本（`scripts/bench_hot_paths.py`）前后对比：

| 指标 | 1.0.0 | 1.0.1 |
| --- | --- | --- |
| 等待 Chrome 绑定端口的轮询间隔 | `[0.25, 0.25, 0.25]` s | `[0.01, 0.02, 0.04]` s |
| 重新接入已有浏览器的 `/json/version` 请求数 | 2 | 1 |
| `connect()` 的串行 CDP 往返次数 | 4 | 1 |
| `dom_tree()` 的串行 CDP 往返次数 | 2 | 1 |
| `wait()` 的轮询间隔 | `[0.25, 0.25, 0.25]` s | `[0.02, 0.04, 0.08]` s |
| `close()` 释放阻塞调用方耗时 | 5015 ms | 0.0 ms |
| `import octop_browser` 加载的模块数 | 390 | 267 |
| `import octop_browser` 加载的第三方包根数 | 15 | 5 |

- 启动轮询改为首次 10 ms、逐次翻倍至配置上限，且全程复用同一个
  `aiohttp.ClientSession`。
- 重新接入只发一次 `/json/version`（原先 `_port_in_use` + `_get_ws_url` 各发
  一次）。
- `Page.enable` / `DOM.enable` / `Runtime.enable` 与视口设置并发下发；
  `dom_tree()` 的 `DOM.getDocument` 与 `Runtime.evaluate` 并发下发。
- `wait()` 轮询改为 20 ms 起、翻倍至 250 ms。
- aiohttp 与 websockets 改为按需导入，顶层 `import octop_browser` 不再拉入
  传输层。

### 变更

- 仓库身份迁移到 `xkx121029/octop-browser`：作者、project-urls、README 徽章与
  互链、CONTRIBUTING / SECURITY / issue 模板 / skills 文档全部更新；原 MIT
  版权声明保留。
- 新增 `scripts/bench_hot_paths.py`，用于度量上述指标（支持切到任意版本对比）。
- 分发名与导入名 `octop-browser` / `octop_browser` 保持不变；`BROWSER_USE_*`
  环境变量与 `~/.octop-browser` 状态目录为兼容既有安装而保留。

## [1.0.0] - 2026-09-24

`octop-browser` 的首个正式版本：一个面向 Agent 的浏览器自动化工具，直连
**Chrome DevTools Protocol（CDP）**，不经 Playwright 中间层，因此元素定位基于
实时 DOM 引用（ref），点击更准、token 开销更低、登录态可长期保持。

### 核心能力

**CDP 直连架构**
- 启动真实 Chromium 并直接通过 CDP 通信，去掉中间层，动作语义与浏览器行为一致。
- 按 profile 复用浏览器进程与端口，跨命令自动重新接入同一标签页。

**DOM 四档粒度**
- `minimal` / `interactive` / `structured` / `full` 四个级别，按需取用。
- Agent 常用 `interactive` 只取可交互元素并返回稳定 ref，显著降低 token 消耗；
  `structured` 输出 JSON 供脚本消费。

**浏览器动作**
- 导航：`navigate` · `go_back` · `go_forward` · `reload`
- 交互：`click` · `type` · `fill` · `select` · `hover` · `press` · `scroll`
- 观察：`dom_tree` · `screenshot` · `eval_js` · `wait`
- 标签页：`new_tab` · `switch_tab` · `close_tab` · `list_tabs`
- 会话：`close_session`（默认仅断开连接，`kill=True` 同时结束浏览器进程）
- 元素既可用 ref 定位，也可用 CSS selector 定位。

**登录态持久化**
- 命名 profile 各自拥有独立的 Chrome user-data-dir，登录一次即可长期复用。
- profile 与 CDP 端口的映射持久化在磁盘，进程重启后仍能对应。

**录制与回放**
- `record start/stop` 录制真实浏览过程，自动把原始事件归纳为语义化步骤。
- `record generate-steps` 导出可复用步骤，`record generate-skill` 生成 Agent Skill 草稿。
- `replay run` 回放录制，支持 `--input` 注入参数实现参数化重放。
- 三档隐私模式（`--privacy`）：默认对密码、token、密钥等敏感字段自动脱敏，
  录制产物中不落明文；事件文件以 `600` 权限写入。

**MCP 服务**
- 通过 stdio 提供 MCP 协议，兼容主流 Agent 客户端。
- 每个动作对应一个独立工具（`browser_navigate`、`browser_click`、
  `browser_dom_tree`、`browser_close_session` 等），参数与 Python API 一致。

**CLI 与 Python API**
- `octop-browser` 命令覆盖全部动作，适合脚本与手工调试。
- Python 侧提供 `BrowserSession`（持久会话）与 `browser_tool`（单次无状态调用）
  两种入口，统一返回 `ToolResult`（`success` / `content` / `error` / `metrics` / `metadata`）。
- `OctopSettings` 集中管理配置，17 个 `BROWSER_USE_*` 环境变量均可覆盖默认值。
- 钩子机制（`before_action` / `after_action` / `action_error`）便于接入日志与观测。

**浏览器自动安装**
- `install-browser` 自动下载并校验受管 Chromium，无需预装浏览器。
- 提供流式安装事件，便于外部展示进度。

[Unreleased]: https://github.com/xkx121029/happyclaw-browser/compare/v1.0.2...HEAD
[1.0.2]: https://github.com/xkx121029/happyclaw-browser/compare/v1.0.1...v1.0.2
[1.0.1]: https://github.com/xkx121029/octop-browser/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/xkx121029/octop-browser/releases/tag/v1.0.0
