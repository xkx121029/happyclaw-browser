# Changelog

本文件记录 `happyclaw-browser`（原 `octop-browser`）的版本变更。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 变更

- 对齐 HappyClaw 主仓的分支策略：引入 `develop` 集成分支策略；禁止直推 `main`/`develop`；发版后由 `sync-main-to-develop.yml` 同步；新增 `/publish` skill（发版同步 CHANGELOG / README）。
- 改进 CLI 帮助信息：新增 `--version`、顶层使用示例和子命令帮助提示，并明确 `install-browser` 中 Playwright 仅用于 Chromium 下载引导。


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
