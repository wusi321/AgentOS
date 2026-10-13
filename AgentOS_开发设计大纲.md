# AgentOS：面向 Linux 的原生智能体操作环境

> 项目开发大纲与系统集成路线
>
> **文档版本：v0.2**  
> **更新日期：2026-10-13**  
> **推荐仓库名：`agentos`**  
> **首发系统：Ubuntu 22.04 LTS Desktop / GNOME Shell 42 / x86_64**  
> **首发 Agent Runtime：DeepSeek Harness（DSH）**  
> **交付目标：可安装 `.deb` 软件包 + 预装 AgentOS 的定制 ISO**

---

## 0. 本次版本的关键决策

| 决策项 | v0.2 决策 | 原因与约束 |
|---|---|---|
| 系统基线 | Ubuntu 22.04 LTS Desktop | 先固定单一、可复现的桌面环境，避免同时适配多个 GNOME 大版本 |
| GNOME 集成 | GJS Shell Extension + 独立桌面应用 | 先深度集成用户体验，不立即维护 GNOME Shell Fork |
| 桌面应用 | GTK4 + libadwaita + PyGObject | 与 GNOME 视觉、通知、设置和桌面生态一致 |
| Agent Runtime | DSH 首发 | 复用现有 Harness 的模型、工具、会话和插件能力 |
| Runtime 扩展 | 从第一版定义 `AgentRuntime` 协议 | 核心业务不直接依赖 DSH 私有接口，未来可增加 Hermes、Pi 等适配器 |
| DSH 对接 | 优先验证 DSH 的 SDK/JSON-RPC stdio profile；CLI headless 用于冒烟测试 | DSH 接口快速迭代，需先确认事件流、取消、会话和错误处理能力 |
| 后台服务 | 独立 systemd 服务；Agent Runtime 使用非 root 身份 | UI 关闭或用户退出后，允许符合策略的后台任务继续运行 |
| 本地通信 | GNOME 扩展通过用户会话 D-Bus；核心服务使用受控 D-Bus/Unix Socket API | 避免把管理接口开放成局域网 HTTP 服务 |
| 权限控制 | AgentOS Policy Engine + 最小化特权执行器 + polkit | LLM 输出不是授权；首版不开放任意 root Shell |
| 打包 | 标准 Debian packaging，先完成 `.deb` | 软件包可靠后再将包集成进 ISO |
| ISO | 先使用 Cubic 验证原型，再选可重复构建的发行流程 | 原型速度与长期自动化构建分开处理 |
| 项目策略 | 复用 Ubuntu 用户空间组件，暂不修改内核 | 只有现有接口确实无法满足需求时才考虑底层改动 |

**重要约束：** DSH 上游目前仍标注为预览阶段，接口可能发生不兼容变化。必须固定版本/提交、锁定依赖并在 CI 中验证；不要把 `npx @deepseek-ai/dsh@latest` 当作生产安装方式。

## 1. 项目愿景与定位

AgentOS 不是重新实现 Linux 内核，而是构建在 Ubuntu 之上的 **Agent-Native Operating Environment**：让 AI Agent 成为一个具有独立身份、后台生命周期、系统可观测能力、任务管理界面与受控执行边界的系统级主体。

### 1.1 目标体验

用户可以在 GNOME 顶栏打开 AgentOS 面板，提出类似下述的请求：

> 检查系统为什么变慢，列出最可能的原因，给出修复方案；在我确认前不要修改系统。

AgentOS 应当能够：

1. 采集 CPU、内存、磁盘、指定进程、systemd 服务和获准读取的日志。
2. 通过 DSH 分析状态并生成结构化诊断结果。
3. 将任务、事件、工具调用和当前状态展示在桌面 UI。
4. 将可能产生副作用的建议转化为结构化操作请求。
5. 由独立 Policy Engine 校验权限；必要时等待人类审批。
6. 仅通过受控接口执行获批操作，并验证执行结果。
7. 将任务状态与审计记录持久化；桌面窗口关闭后，符合策略的任务仍可运行。

### 1.2 设计原则

- **Userspace-first：** 优先利用现有 Linux 用户空间接口。
- **GNOME-native：** 使用 Shell Extension、GTK、libadwaita、GSettings、Gio/D-Bus 等桌面生态组件。
- **Least privilege：** 模型进程与特权操作分离，默认拒绝。
- **Human-in-the-loop：** 高风险操作需显式审批，审批与操作参数绑定。
- **Backend/UI separation：** 后台服务不依赖窗口或 GNOME Shell 的存活。
- **Runtime-agnostic：** DSH 是首发后端，不是 AgentOS 内部协议本身。
- **Event-driven：** 规则引擎先过滤事件，确需诊断时才调用模型。
- **Auditable：** 任务创建、权限决策、审批和执行结果均可追溯。
- **Reproducible builds：** 依赖、版本、软件包和 ISO 构建输入可追溯。
- **Fail safely：** Agent/模型不可用时，桌面和 Linux 基本功能仍可用。

### 1.3 非目标

首版不尝试：

- 编写或维护自有 Linux 内核。
- Fork 整个 GNOME Shell 或重写 Ubuntu 安装器。
- 让 AI 自动管理所有系统服务、用户、软件包或网络配置。
- 默认向 Agent 提供 root Shell、任意命令执行或完整用户目录访问权。
- 同时集成 DSH、Hermes、OpenClaw、Pi 四套 Runtime。
- 将 eBPF、语音、跨发行版、Windows 后端或复杂多 Agent 编排设为 MVP 前置条件。

## 2. 目标平台与兼容策略

### 2.1 首发平台

- Ubuntu 22.04 LTS Desktop，x86_64。
- GNOME Shell 42 系列；以目标测试镜像实际安装的更新版本为准。
- 桌面扩展明确声明支持的 Shell 版本，使用与 GNOME 42 兼容的扩展入口和 API。
- Python Core 使用 Ubuntu 22.04 系统 Python 3.10 作为部署基线；开发环境可使用 venv。
- DSH 使用上游当前要求的 Node.js 版本。上游版本要求会变化，构建时必须校验固定版本的 `package.json`，不能假设 Ubuntu 22.04 仓库自带 Node.js 一定满足要求。

### 2.2 兼容策略

| 范围 | 首版策略 |
|---|---|
| Ubuntu 22.04 Desktop / GNOME 42 | 唯一正式验收基线 |
| Ubuntu 24.04 / 更高 GNOME 版本 | 作为后续适配目标，不承诺首版兼容 |
| Wayland | 默认重点测试环境 |
| Xorg 会话 | 在条件允许时做回归测试 |
| x86_64 | 首发架构 |
| ARM64 | 后续根据 DSH、Node.js 与桌面依赖情况评估 |
| 无模型凭据 / 无网络 | 系统监控、桌面和任务查看必须可用；模型任务给出明确错误 |

## 3. 系统架构

```text
┌──────────────────────────────────────────────────────────────┐
│ GNOME Shell                                                  │
│ AgentOS Extension：顶栏入口 / 快捷面板 / 通知 / 审批提示      │
└───────────────────────────┬──────────────────────────────────┘
                            │ 用户会话 D-Bus
┌───────────────────────────▼──────────────────────────────────┐
│ AgentOS Desktop + Session Broker                              │
│ GTK4/libadwaita：Chat / Tasks / Dashboard / Approvals         │
│ 每个登录会话单独运行，不持有系统 root 权限                     │
└───────────────────────────┬──────────────────────────────────┘
                            │ 受控本地 IPC / system D-Bus
┌───────────────────────────▼──────────────────────────────────┐
│ AgentOS Core Service（systemd 系统服务）                      │
│ Task Manager · Event Bus · Monitor · Audit · Policy           │
└────────────────┬──────────────────────────────┬──────────────┘
                 │                               │
┌────────────────▼────────────────┐  ┌──────────▼───────────────┐
│ Agent Runtime Worker             │  │ System Bridge            │
│ AgentRuntime API                 │  │ /proc · journald · D-Bus │
│ DshAdapter（首发）               │  │ systemd 状态和白名单操作 │
│ MockAdapter（测试）              │  └──────────┬───────────────┘
└────────────────┬────────────────┘             │
                 │ SDK / JSON-RPC stdio          │
┌────────────────▼────────────────┐  ┌──────────▼───────────────┐
│ DSH Runtime                     │  │ Privileged Executor      │
│ 单独进程、非 root、版本锁定       │  │ 后续阶段加入，范围最小化  │
└─────────────────────────────────┘  └──────────┬───────────────┘
                                                │ polkit / 明确授权
                                      ┌─────────▼───────────────┐
                                      │ Ubuntu / systemd / D-Bus│
                                      └─────────────────────────┘
```

### 3.1 进程边界

1. **GNOME Shell Extension：** 只做桌面呈现、通知和用户操作转发；不运行模型，不执行系统命令，不持有特权。
2. **AgentOS Desktop / Session Broker：** 负责当前用户的聊天窗口、会话呈现和 UI 状态。关闭窗口不应终止 Core 任务。
3. **AgentOS Core：** 管理任务状态、系统事件、策略决策和审计。作为独立 systemd 服务运行，不直接以 root 身份执行模型生成的操作。
4. **Agent Runtime Worker：** 与 DSH 隔离运行。若需后台跨注销运行，服务身份、凭据存储和用户授权必须明确，不假设 GNOME 密钥环在注销后仍可访问。
5. **System Bridge：** 提供结构化的只读查询与白名单操作，不将任意 Shell 作为 API。
6. **Privileged Executor：** 仅在受控写操作阶段引入；只接受操作 ID 与参数 schema，执行前重新校验策略。

### 3.2 通信方式

- Extension 到当前桌面会话：优先使用用户会话 D-Bus（例如 `org.agentos.Session1`）。
- Desktop/Session Broker 到 Core：受控 D-Bus 接口或权限受限的 Unix Domain Socket。
- Core 到特权执行器：单独的 system D-Bus 方法并配置 polkit action；每个操作均进行目标和参数校验。
- Core 到 DSH：优先评估 DSH 的 SDK profile（JSON-RPC over stdio）；headless CLI 用于最小冒烟与单次任务验证。
- 不默认开启监听 `0.0.0.0` 的 HTTP 服务。开发 Web UI 如需 HTTP，应只绑定 `127.0.0.1`，并单独处理认证和 CSRF/Origin 等风险。

### 3.3 统一事件格式

建议所有核心模块使用版本化 JSON 事件：

```json
{
  "schema_version": 1,
  "event_id": "uuid",
  "event_type": "task.state_changed",
  "timestamp": "RFC3339 timestamp",
  "source": "agentos-core",
  "task_id": "task-id-or-null",
  "session_id": "session-id-or-null",
  "severity": "info",
  "payload": {},
  "sensitivity": "normal"
}
```

实际数据结构应通过 Pydantic/JSON Schema 校验。`payload` 禁止直接存储 API Key、完整环境变量或未经脱敏的敏感命令输出。

## 4. 技术栈与选型

| 组件 | 选型 | 用途与注意事项 |
|---|---|---|
| 操作系统 | Ubuntu 22.04 LTS Desktop | 固定发行版基线 |
| GNOME 扩展 | GJS / JavaScript | 对 GNOME 42 开发，`metadata.json` 声明 Shell 版本；初期避免依赖新版 Shell 专属 API |
| 桌面应用 | GTK4 + libadwaita + PyGObject | 原生窗口、列表、任务详情、设置、通知；视觉一致且便于桌面整合 |
| 用户会话服务 | Python 或 GJS，首版尽量薄 | 仅承担当前用户 UI/session broker 工作 |
| 核心服务 | Python 3.10 + asyncio | 任务、策略、事件、系统监控 API |
| 数据模型 | Pydantic + JSON Schema | 事件、工具参数、审批请求、Runtime 事件校验 |
| 本地 IPC | D-Bus / Unix Domain Socket | 根据调用方身份和操作敏感度选择 |
| 运行时适配 | Python `Protocol` 或 ABC | 统一 DSH、Mock、未来其他 Runtime 的接口 |
| DSH | 固定上游版本/commit，锁定依赖 | 独立运行；优先验证 SDK JSON-RPC stdio profile；具体可用能力以锁定版本文档和测试为准 |
| Node.js | 使用 DSH 锁定版本所要求的受支持版本 | 上游要求可能变化；不要依赖未验证的系统 Node.js 版本 |
| 任务/审计存储 | SQLite | WAL、schema migration、事务、备份与保留策略 |
| 系统指标 | `/proc`、`/sys`、psutil | 第一版只读优先 |
| 服务与日志 | systemd D-Bus、journald | 查询限定的服务与日志，不默认赋予任意日志读取权限 |
| 授权 | 自定义 Policy Engine + polkit | Core 做业务策略，polkit 做系统级授权边界 |
| 后台管理 | systemd unit | Restart、资源限制、沙箱和依赖关系 |
| Python 测试 | pytest、pytest-asyncio | 单元、集成、安全与故障测试 |
| Node 测试 | DSH 上游测试 + AgentOS 适配层测试 | 锁定 DSH 时执行可复现测试子集 |
| Debian 包 | `dpkg-buildpackage`、`debuild`、`lintian` | 包构建、检查、安装/升级/卸载测试 |
| ISO 原型 | Cubic | 快速验证预装包与首次启动体验 |
| ISO 自动化 | 基于最终安装模式选择构建工具 | 评估 `livecd-rootfs`、`ubuntu-cdimage` 或适配 Ubuntu Desktop 安装镜像的自动化流程，不预设 `live-build` 可直接替代 Ubuntu Desktop 官方构建链 |

### 4.1 为什么使用 GTK4/libadwaita，而不是首版使用 PySide6？

AgentOS 的目标是 GNOME 深度集成，而不只是跨平台聊天应用。GTK4/libadwaita 在 GNOME 的窗口行为、系统主题、通知、设置组件和无障碍支持方面更贴近平台。PySide6 仍是可行备选，但应在 ADR 中记录后再切换，避免同时维护两套桌面技术栈。

### 4.2 DSH 版本锁定策略

- 保存 DSH 的精确版本号和 Git commit；保存对应的 `package.json`、lockfile 与许可证信息。
- 每次升级 DSH 都执行一组适配器契约测试，不直接自动升级生产版本。
- 生产安装包不调用 `npx ...@latest`。
- 在正式封装前，验证目标 DSH 版本的 Node.js 引擎要求、SDK profile、事件流、会话持久化、取消语义和错误处理。
- 如果 SDK profile 无法满足所需的稳定性，应先将 DSH Web/CLI 作为独立子进程做有限适配，不能假装它原生提供未验证的接口。

## 5. Runtime 适配层设计

### 5.1 目标

AgentOS Core 不应知道 DSH 的插件对象、Cordis 内部实现或 CLI 私有参数。它只与 AgentOS 自己定义的 `AgentRuntime` 协议交互。

首版必须实现：

- `DshAdapter`：连接锁定版本的 DSH。
- `MockRuntime`：无网络、无 API Key、可注入错误的测试后端。

后续可选：`HermesAdapter`、`PiAdapter`。不得为未实现的 Runtime 提前造空壳功能。

### 5.2 接口草案

```python
from typing import Protocol, AsyncIterator

class AgentRuntime(Protocol):
    async def start(self) -> None: ...
    async def create_session(self, *, workspace: str) -> str: ...
    async def send_message(
        self, session_id: str, message: str
    ) -> AsyncIterator["RuntimeEvent"]: ...
    async def cancel(self, task_id: str) -> None: ...
    async def health_check(self) -> "RuntimeHealth": ...
    async def capabilities(self) -> "RuntimeCapabilities": ...
    async def close(self) -> None: ...
```

上述是 AgentOS 内部接口草案，不表示 DSH 原生 API 与之完全一致。适配层还需要定义统一的错误类型、超时、重试政策、流式输出、取消语义和能力差异。

### 5.3 事件抽象

最小事件类型：

- `session.created`
- `message.delta`
- `message.completed`
- `tool.call.started`
- `tool.call.completed`
- `approval.required`
- `runtime.warning`
- `runtime.error`
- `runtime.exited`

事件必须附带 `runtime_id`、版本、session/task 关联 ID 和时间戳。对 Runtime 原生不支持的事件，适配器应明确返回 `unsupported` 或能力标志，不得伪造事件。

### 5.4 DSH 接入验证清单

P0 阶段需写一个可删除的 spike/原型，逐项验证：

1. 是否可在不启动 DSH 自带 Web UI 的情况下建立程序化会话。
2. SDK/JSON-RPC stdio 的输入输出协议、初始化和关闭方式。
3. 是否可以接收增量消息与工具调用事件。
4. 是否支持中断正在运行的任务。
5. 会话是否可持久化，并能恢复/继续。
6. Runtime 崩溃、无模型密钥、网络断开、端口冲突时返回什么错误。
7. 默认工具集合和沙箱行为是否符合 AgentOS 安全模型。
8. DSH 日志是否可能写出 prompt、工具参数、文件内容或密钥。
9. 目标运行环境是否满足 DSH 固定版本的 Node.js 要求。

**通过门槛：** 只有在上述关键行为有可复现的测试证明后，才把 DSH 作为正式适配器；否则先保留 Mock Runtime，继续完成 Core 的独立开发。

## 6. GNOME 深度集成方案

### 6.1 第一层：Shell Extension

安装在 GNOME Shell 的系统扩展目录或按策略部署的扩展目录中，具有唯一、稳定的扩展 UUID。扩展首期提供：

- 顶栏 AgentOS 图标与运行状态。
- 快速面板：活跃任务、待审批项目、最近告警。
- 点击打开 AgentOS Desktop。
- 对任务完成、失败与等待审批的 GNOME 通知。
- 在扩展失效或 Core 不可用时保持 GNOME Shell 稳定。

约束：扩展不执行 `bash -c`，不读写任意系统文件，不直接启动带高权限的进程。系统数据经 D-Bus API 获取；模型交互由 Core 处理。

### 6.2 第二层：原生 Desktop App

GTK4/libadwaita 窗口建议包含：

- **Chat：** 对话与运行时选择；显示模型/Runtime 状态。
- **Tasks：** 队列、运行中、等待审批、失败、已完成任务。
- **System：** CPU、内存、磁盘、进程与服务的受限视图。
- **Approvals：** 操作说明、目标、参数、权限等级、批准/拒绝/撤销。
- **Activity：** Agent 事件、工具调用和审计摘要。
- **Settings：** 模型连接、Runtime、权限、数据保留与通知。

对危险操作必须显示结构化操作及影响范围，而不是只显示模型给出的自然语言摘要。

### 6.3 第三层：桌面体验增强（后续）

在 Shell Extension 与 Core 稳定后再评估：

- 全局快捷键（遵循 GNOME 配置及权限机制）。
- 快速设置集成、搜索/命令面板入口。
- Workspace 与任务视图联动。
- 登录后欢迎页和首次配置向导。
- 针对 GNOME Shell 上游版本的兼容测试矩阵。

除非某项体验无法通过公开扩展 API 实现，否则不维护 GNOME Shell Fork。确需修改 Shell 时，新增独立 ADR，并评估安全更新合并成本。

### 6.4 GNOME 扩展验收

- `metadata.json` 声明首版支持的 Shell 版本。
- 扩展启用、禁用、升级和卸载均可完成。
- Core 进程退出或 D-Bus 请求失败时，扩展不会抛出未处理异常导致 Shell 不稳定。
- 退出登录、Shell 重启和重新登录后，任务视图重新同步。
- 不在扩展中存储模型密钥、审计数据库或敏感配置。
- 测试 Wayland 默认会话；在可用环境下覆盖 Xorg 会话。

## 7. 系统身份、目录与权限

### 7.1 身份模型

| 身份 | 建议形态 | 职责 |
|---|---|---|
| Human Session Identity | 实际登录用户 UID | 对话、界面状态、审批操作、用户偏好 |
| AgentOS Core Identity | 专用系统用户，例如 `agentos` | 持续运行的任务管理、事件与策略服务；非 root |
| Runtime Worker Identity | 受限的 Agent 运行账户或工作区隔离身份 | 运行 DSH 和工具；权限范围不得超过任务授权 |
| Privileged Executor Identity | 最小化特权的独立服务 | 只执行明确列出的系统操作；后续阶段才引入 |
| Logical Agent Identity | Core 数据中的 Agent ID/能力清单 | 逻辑能力、任务归属、配额和审计，不等同 Linux UID |

### 7.2 路径建议

- 程序文件：`/usr/lib/agentos/` 与 `/usr/libexec/agentos/`。
- 默认配置：`/etc/agentos/`，仅管理员可写。
- 系统级任务/状态：`/var/lib/agentos/`，由专用服务账户拥有。
- 系统缓存：`/var/cache/agentos/`。
- 用户配置：`$XDG_CONFIG_HOME/agentos/`。
- 用户状态：`$XDG_STATE_HOME/agentos/`。
- 用户数据：`$XDG_DATA_HOME/agentos/`。

程序、配置、缓存、状态和用户数据必须分离。`.deb` 卸载默认不删除用户任务与审计数据；完全清除应由用户明确确认。

### 7.3 密钥管理

- 用户交互式云模型凭据优先通过桌面 Secret Service/libsecret 管理。
- 不将密钥写入 SQLite、普通日志、Git、示例配置或 ISO。
- 不把用户登录密钥环视为注销后仍可用的后台服务凭据。
- 若要让 Agent 在用户注销后继续访问云模型，需在后续设计专门的服务级凭据流程，明确所有者、文件权限、解锁策略、撤销方式和威胁模型。
- 安装脚本与 ISO 首次启动向导不得要求用户把密钥写入命令行参数或 shell history。

### 7.4 权限级别

- **P0 只读：** 查询明确允许的系统指标、进程、服务状态和日志。
- **P1 受限写入：** 写入专属工作区、修改 AgentOS 自身非特权配置、创建或取消自身任务。
- **P2 受控操作：** 针对固定白名单目标重启服务、结束明确进程或执行限定修复；需要按策略审批。
- **P3 管理操作：** 安装软件、修改网络、修改系统配置、管理用户、关机等；默认禁用，需更强审批和身份确认。

### 7.5 安全原则

- 模型输出永远不直接成为授权凭证。
- 所有工具调用都经 Policy Engine；策略检查发生在执行前，而不是只在 UI 层。
- 特权操作接受结构化操作 ID 和经过 schema 校验的参数。
- 禁止用模型拼接的任意 Shell 字符串绕过白名单。
- 审批记录绑定用户、操作 ID、目标、规范化参数摘要、有效期和一次性 nonce。
- 执行时重新检查权限、对象状态和审批是否仍有效。
- 对提示注入、网页、日志、文件内容和工具输出一律视为不可信输入。
- systemd 服务使用合理的 `NoNewPrivileges`、`ProtectSystem`、`ProtectHome`、`PrivateTmp`、Capabilities、资源限制等沙箱选项；须按功能测试，不能复制配置后假设安全。
- 有限访问日志时，只给必要组/策略；不为图省事把 Agent 加入所有高权限系统组。
- 审计日志做敏感信息脱敏并设置容量/保留策略。

## 8. 关键模块与职责

| 模块/包 | 职责 | 第一版 |
|---|---|---|
| `agentos-core` | 生命周期、任务协调、配置、API、健康检查 | 必须 |
| `agentos-runtime-api` | Runtime、事件、能力与错误协议 | 必须 |
| `agentos-runtime-dsh` | 锁定版本的 DSH SDK/CLI 适配 | 必须（通过 P0 验证后） |
| `agentos-runtime-mock` | 模拟消息、事件、超时与故障 | 必须 |
| `agentos-system-bridge` | 系统指标、允许的服务/日志查询 | 必须 |
| `agentos-policy` | 工具能力声明、策略与审批规则 | 必须；P0 先只读 |
| `agentos-audit` | 结构化审计、脱敏与查询 | 必须 |
| `agentos-task-manager` | 状态机、队列、取消和恢复 | 必须 |
| `agentos-session` | 用户会话服务、D-Bus 与桌面会话桥接 | 必须 |
| `agentos-desktop` | GTK4/libadwaita 窗口 | 必须 |
| `agentos-gnome-extension` | 顶栏、快速面板、通知 | P2 纳入正式范围 |
| `agentos-event-engine` | 规则、事件关联、通知节流 | P3 |
| `agentos-privileged-executor` | 白名单特权执行器 | P4 |
| `agentos-meta` | 安装整套系统所需包的元包 | P5 |
| `agentos-iso` | ISO 配置、镜像构建和验证流程 | P6 |

## 9. 推荐仓库结构

```text
agentos/
├── README.md
├── LICENSE
├── CONTRIBUTING.md
├── SECURITY.md
├── pyproject.toml
├── pnpm-workspace.yaml              # 仅当 AgentOS 自有 TS 包确有需要时启用
├── docs/
│   ├── architecture.md
│   ├── roadmap.md
│   ├── dsh-integration.md
│   ├── gnome-integration.md
│   ├── runtime-api.md
│   ├── threat-model.md
│   ├── permissions.md
│   ├── packaging.md
│   ├── iso-build.md
│   └── adr/
├── configs/
│   ├── agentos.example.toml
│   ├── policies/default-policy.yaml
│   └── dsh/                         # 只存可公开的默认配置模板
├── apps/
│   └── desktop/                     # GTK4/libadwaita 应用
├── gnome-extension/
│   ├── metadata.json
│   ├── extension.js                 # 遵循 GNOME 42 扩展格式
│   ├── stylesheet.css
│   ├── prefs.js
│   └── schemas/
├── packages/
│   ├── runtime-api/
│   ├── runtime-dsh/
│   ├── runtime-mock/
│   └── shared-schemas/
├── services/
│   ├── core/
│   ├── session-broker/
│   ├── system-bridge/
│   ├── policy-engine/
│   ├── task-manager/
│   ├── event-engine/
│   └── privileged-executor/         # 后续阶段实现
├── packaging/
│   ├── debian/
│   │   ├── agentos-core/
│   │   ├── agentos-runtime-dsh/
│   │   ├── agentos-system-bridge/
│   │   ├── agentos-desktop/
│   │   ├── agentos-gnome-extension/
│   │   └── agentos-meta/
│   ├── systemd/
│   ├── dbus/
│   ├── polkit/
│   ├── dconf/
│   └── iso/
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── security/
│   ├── packaging/
│   └── vm/
├── scripts/
│   ├── dev-setup.sh
│   ├── check-dsh-compat.sh
│   ├── build-debs.sh
│   ├── build-iso.sh
│   └── vm-smoke-test.sh
└── third_party/
    └── notices/                     # 上游 DSH 与打包依赖的许可证/声明
```

这只是目标目录图，不要求第一天就创建所有目录。随阶段新增模块，避免空模块和过早抽象。

## 10. 软件包与运行时依赖设计

### 10.1 建议的软件包

| 包名 | 内容 | 依赖/注意事项 |
|---|---|---|
| `agentos-core` | 核心服务、配置 schema、systemd unit、D-Bus policy | Python 系统依赖明确，安装脚本无网络副作用 |
| `agentos-runtime-api` | Runtime 协议与数据 schema | 作为内部稳定接口，可与 Core 同版发布 |
| `agentos-runtime-dsh` | DSH adapter、锁定版本元数据、启动器 | 要求经验证的 DSH 版本和合适 Node.js runtime |
| `agentos-system-bridge` | 只读系统 API | 不因安装而自动授予广泛 sudo 权限 |
| `agentos-desktop` | GTK 应用、图标、`.desktop` 文件、GSettings schema | 用户级启动，避免依赖 root UI |
| `agentos-gnome-extension` | GJS 扩展文件和元数据 | 放入正确的 GNOME extensions 路径；启用策略单独处理 |
| `agentos-policy` | 默认规则、polkit action、审批 schema | 默认策略是只读 |
| `agentos-meta` | 整套 AgentOS 的依赖元包 | ISO 可安装元包；允许用户只安装基础子集 |

MVP 可合并部分包，稳定后再拆分。不要单纯为了“模块化”让初期依赖链过度复杂。

### 10.2 DSH 与 Node.js 的打包

DSH 的当前上游主分支要求可能比 Ubuntu 22.04 基础仓库中的 Node.js 版本新。不能只写 `Depends: nodejs` 就认为依赖满足。

候选策略：

1. **首选评估：** 将已验证的 DSH 发行包及其锁定依赖打入 `agentos-runtime-dsh`，并通过独立、版本化的 Runtime 包提供满足要求的 Node.js 运行时；随包附带上游许可证与第三方声明。
2. 或者维护一个明确、受支持的 Node.js APT 来源，但安装过程将依赖网络与外部源状态，ISO 离线安装也需要额外处理。
3. 开发机可使用受控版本管理器，但生产 systemd 服务不依赖某个开发者的 `nvm` 环境或个人 PATH。

若随包分发 Node.js，必须制定安全更新政策并尽快更新含漏洞的运行时。最终选择应在 ADR 中说明体积、离线能力和安全更新成本。

### 10.3 软件包构建与验收工具

开发期建议使用：

```bash
sudo apt install build-essential devscripts debhelper dh-python \
  python3-all python3-venv python3-pytest lintian fakeroot
```

命令示例：

```bash
# 在准备好的 Debian source tree 中构建
 dpkg-buildpackage -us -uc -b

# 检查生成的 .deb
 lintian ../*.deb

# 安装测试（仅在测试机/虚拟机中）
 sudo apt install ./agentos-*.deb
```

实际 `Build-Depends`、`Depends`、维护脚本与路径应由包的 `debian/control`、`debian/rules`、`.install` 和服务文件明确表达。不要依赖 postinst 从公网拉取源码或执行 `npm install` 获取任意最新版本。

### 10.4 安装与升级要求

- 安装只部署已构建的文件，不在线编译 DSH。
- 配置模板与用户配置分离；升级不得静默覆盖用户设置。
- 数据库变更采用版本迁移，失败时提供可恢复策略。
- `postinst`、`prerm`、`postrm` 必须可重复执行，且不要假设安装时已有活跃 GNOME 会话。
- 启用 Shell Extension 不能只依赖系统级 postinst 在某个用户会话中运行；通过首登向导、dconf 默认值或明确的用户级启用步骤完成。
- `.deb` 安装/升级/卸载分别测试；卸载保留用户数据，purge 的行为和提示要明确。

## 11. ISO 构建策略

### 11.1 先做原型，再做可复现构建

**原型阶段：Cubic。** 使用官方 Ubuntu 22.04 Desktop ISO，把经过测试的 AgentOS `.deb` 和默认配置集成进去，验证桌面启动、扩展加载、首次运行和模型设置流程。Cubic 适合早期快速验证，但其 GUI 操作不能自动视为可审计的生产构建流水线。

**自动化阶段：锁定构建链。** 根据选择的交付目标（保留原版 Ubuntu Desktop 安装流程、定制 Live 环境或实现自动化安装）选择合适的工具。应评估 Canonical 的 `livecd-rootfs`/`ubuntu-cdimage` 相关流程，或其他确实兼容所选 Ubuntu Desktop 安装介质的工具。`live-build` 可以作为研究候选，但不能假设它是 Ubuntu Desktop 官方 ISO 构建链的直接替代品。

### 11.2 ISO 管理内容

- ISO 基础版本及 SHA-256。
- 预装 `.deb` 包及版本清单。
- GNOME 扩展部署和默认启用策略。
- 默认 dconf/GSettings 设置。
- 首次启动向导和隐私/密钥说明。
- 软件源与离线依赖清单。
- 包版本、源码 commit、构建参数和校验和。
- 安装与升级测试报告。

### 11.3 ISO 安全约束

- 镜像里不包含开发者个人 API Key、SSH 私钥、测试数据库或用户会话。
- 默认策略只读，不预授权系统写操作。
- 首次启动时生成或初始化实例数据，不能把构建机 ID、机器密钥或实例状态复制给所有安装用户。
- 默认服务不可用时，Ubuntu 登录、GNOME 桌面、网络设置和终端仍能正常使用。
- ISO 不应强制用户使用特定模型供应商；模型配置为可选的首次运行步骤。

### 11.4 ISO 构建流水线

```text
Source Commit + DSH Version + Ubuntu Base ISO
                  │
                  ▼
        Lint / Unit / Security Tests
                  │
                  ▼
        Build Debian Packages (.deb)
                  │
                  ▼
       Install/Upgrade Test on Clean VM
                  │
                  ▼
        Build ISO from Versioned Inputs
                  │
                  ▼
   Fresh VM Install + GNOME + Runtime Smoke Test
                  │
                  ▼
       Artifact Manifest + SHA-256 + Release Notes
```

## 12. 任务状态机与数据流

### 12.1 任务状态

建议至少包含：

`CREATED -> QUEUED -> RUNNING -> WAITING_APPROVAL -> SUCCEEDED / FAILED / CANCELLED`

并支持 `PAUSED`、`RECOVERING`、`TIMED_OUT`。非法状态转换由 Task Manager 拒绝；持久化前后要使用事务，进程重启后恢复任务时必须判断操作是否可以安全重试。

### 12.2 用户指令流程

```text
用户指令
 -> Session Broker / Desktop
 -> Core 身份验证
 -> Task Manager 创建任务
 -> DSH Adapter 处理会话和事件
 -> Tool Router 生成结构化工具请求
 -> Policy Engine 校验能力/参数/用户授权
 -> 必要时 WAITING_APPROVAL
 -> System Bridge 或特权执行器执行
 -> 验证副作用与返回码
 -> Audit Store 记录
 -> Desktop / Extension 更新状态
```

Runtime 不得直接绕过 Policy Engine 调用系统特权工具。对于 DSH 自带工具，应评估并禁用或沙箱化与 AgentOS 权限模型冲突的工具，尤其是 Shell、文件系统、网络和电脑控制能力。

### 12.3 系统事件流程

```text
系统采集器
 -> 事件 schema 校验
 -> 规则阈值 / 去重 / 节流
 -> Event Bus
 -> 关联服务、PID 与允许访问的日志
 -> 必要时调用 DSH 分析
 -> 通知 / 诊断建议 / 审批请求
```

基础规则告警不依赖大模型；大模型只是诊断与解释模块，不成为系统监控的单点故障。

## 13. 分阶段路线与验收标准

开发顺序应从底层闭环开始。每个阶段完成可验证的最小增量，再进入下一阶段。

### P0：可行性验证与威胁建模

**工作项**

- 固定 Ubuntu 22.04 VM、GNOME Shell 版本及 DSH 版本/commit。
- 编写 `docs/architecture.md`、`docs/threat-model.md`、`docs/dsh-integration.md`。
- 建立 `AgentRuntime` 接口和 `MockRuntime`。
- 验证 DSH SDK/JSON-RPC stdio、headless CLI、Node.js 引擎版本和会话/事件/取消能力。
- 明确默认工具集合及如何限制 DSH 自身的高风险工具。

**验收标准**

- [ ] DSH 的版本、Node.js 要求和构建步骤可复现。
- [ ] 能用一个小程序完成会话初始化、发送请求、接收响应、清理退出。
- [ ] 已记录流式事件、取消、持久化及异常情况的真实支持程度。
- [ ] MockRuntime 与 DshAdapter 共用一套契约测试。
- [ ] 识别默认 Shell/文件/网络工具的安全风险。
- [ ] 形成第一份威胁模型与权限默认值。

### P1：AgentOS Core、任务状态与只读系统接口

**工作项**

- 实现 Python Core 包、配置、日志和健康检查。
- 实现任务状态机、SQLite 持久化、取消与恢复。
- 实现 CPU、内存、磁盘和进程采集。
- 实现限定的 systemd 服务状态与日志读取。
- 为 Runtime 进程设置独立工作目录、超时、资源预算和环境白名单。
- 添加最小 systemd unit，服务以非 root 身份运行。

**验收标准**

- [ ] Core 与模型服务断开后仍能返回系统监控结果。
- [ ] 服务重启后任务状态可恢复，不重复执行未确认的副作用操作。
- [ ] MockRuntime 可完整跑通端到端任务。
- [ ] 只读 API 无法修改系统状态或任意用户目录。
- [ ] 单元/集成测试覆盖主要状态转换与错误路径。

### P2：GNOME Extension + Desktop 第一条完整链路

**工作项**

- 编写 GNOME 42 兼容的 GJS Extension。
- 建立用户会话 D-Bus 接口。
- 使用 GTK4/libadwaita 制作聊天、任务、状态与通知界面。
- 打通 Extension → Session Broker → Core → Mock/DSH Runtime。
- 显示实时任务状态和 Runtime 健康状态。

**验收标准**

- [ ] 点击顶栏入口能打开 AgentOS Desktop。
- [ ] 发送指令后，任务状态能同步到面板。
- [ ] 关闭窗口不停止后台任务；重新打开可恢复状态。
- [ ] Core 不可用时扩展保持稳定并显示可理解的错误。
- [ ] 扩展启用/禁用、Shell 重启、注销/重新登录有测试记录。
- [ ] 用户会话以外的账户不能通过 IPC 控制当前用户任务。

### P3：事件总线与主动诊断

**工作项**

- 定义版本化系统事件 schema。
- 加入 CPU、内存、磁盘、服务和任务异常规则。
- 实现告警去重、节流、静默时间与用户通知。
- 支持定时任务与事件触发任务。
- 将规则检测与 LLM 诊断解耦。

**验收标准**

- [ ] 模拟异常可触发告警并关联正确上下文。
- [ ] 同一异常不会持续刷屏。
- [ ] 模型不可用时基本监控和告警仍正常。
- [ ] 事件数据有大小限制，敏感字段会脱敏。

### P4：权限策略与受控执行

**工作项**

- 建立工具 capability manifest、参数 schema 和风险等级。
- 实现审批请求、过期、拒绝、撤销、重复请求保护。
- 加入独立特权执行器和最小化 polkit action。
- 首批仅支持极少数容易验证的白名单操作。
- 记录操作者、参数摘要、审批、执行退出码和结果。

**验收标准**

- [ ] 未授权操作必然被拒绝并记录。
- [ ] 审批只对指定操作、目标与参数生效。
- [ ] 参数变化、审批过期和重放请求无法越权。
- [ ] 特权执行器不接受任意 Shell 字符串。
- [ ] Agent Runtime 与普通 Core 不持有 root 权限。
- [ ] 对工具调用、拒绝路径和失败执行完成安全测试。

### P5：`.deb` 软件包化

**工作项**

- 建立 Debian packaging 文件和包依赖关系。
- 安装 systemd unit、D-Bus policy、polkit action、GSettings schema 和 GNOME 扩展文件。
- 管理 DSH 与 Node.js Runtime 的固定版本。
- 实现安装、升级、卸载和数据保留策略。
- 在干净 Ubuntu 22.04 VM 中做 package smoke test。

**验收标准**

- [ ] 从源码构建出所有必要 `.deb`，版本可追踪。
- [ ] `lintian` 检查结果经过审阅，没有未解释的严重问题。
- [ ] 在全新 Ubuntu 22.04 中可安装并启动。
- [ ] 升级不覆盖用户配置或密钥。
- [ ] 卸载可停止服务且默认保留用户数据。
- [ ] 安装脚本不在线下载未锁定源码或执行不受控安装。
- [ ] DSH 不满足版本要求时能明确失败，而不是静默使用不兼容环境。

### P6：定制 ISO 与首次启动流程

**工作项**

- 使用官方 Ubuntu 22.04 Desktop ISO + Cubic 做首个可安装原型。
- 集成已构建 `.deb` 和默认 GNOME 配置。
- 设计首次启动流程、密钥设置提示、扩展启用与诊断工具。
- 记录基础 ISO、软件包清单、源码 commit 和构建校验和。
- 评估后续采用的自动化 ISO 构建链。

**验收标准**

- [ ] 从 ISO 安装后无需运行开发脚本即可启动 AgentOS。
- [ ] 未配置 API Key 时系统桌面正常，Agent 明确提示未配置模型。
- [ ] 模型/网络不可用不会阻止 GNOME 登录与基本使用。
- [ ] 新安装不会带入构建者的密钥、配置和数据库。
- [ ] 自动测试覆盖安装、首次登录、扩展启动、Core 状态和 Runtime smoke test。
- [ ] 发布记录包含 ISO SHA-256、包清单、版本号和已知问题。

### P7：发布质量与高级能力

可选功能：多 Runtime（Hermes/Pi）、多 Agent、任务 DAG、语音、更多系统集成、资源配额、远程管理和 eBPF。只有当 P0-P6 稳定后再评估。

**验收标准**

- [ ] 高级功能不能绕过现有权限策略。
- [ ] 新 Runtime 通过公共契约测试。
- [ ] GNOME 扩展或 AgentOS 服务崩溃不会阻止系统启动与登录。
- [ ] 软件包和 ISO 均有回归测试、恢复方案与版本发布记录。

## 14. 测试体系

### 14.1 自动化测试类型

- **单元测试：** 状态机、参数 schema、策略判定、脱敏、事件去重。
- **Runtime 契约测试：** MockRuntime 与 DshAdapter 共用测试集。
- **集成测试：** Core、SQLite、IPC、systemd 和 D-Bus。
- **安全测试：** 越权调用、路径穿越、注入、审批重放、恶意日志、敏感数据泄漏。
- **故障测试：** DSH 崩溃、Node.js 缺失、API Key 缺失、网络断连、数据库损坏、UI 断连。
- **GNOME 测试：** 扩展 enable/disable、Shell 重启、登录/注销、Wayland/Xorg。
- **Debian 包测试：** install/upgrade/remove/purge、依赖缺失、配置保留。
- **VM/ISO 测试：** 从 ISO 全新安装、第一次登录、服务可用、卸载与恢复。
- **资源测试：** 长期运行内存、任务队列上限、日志/SQLite 增长、资源限制。

### 14.2 MVP 定义

MVP 完成意味着：

- [ ] DSH Runtime 可通过统一适配层被 Core 驱动。
- [ ] MockRuntime 可独立完成所有核心测试。
- [ ] GNOME 顶栏入口与桌面应用可用。
- [ ] Core 与 UI 分离，UI 关闭后任务状态仍存在。
- [ ] 可查看 CPU、内存、磁盘、进程与受限服务信息。
- [ ] 任务状态与审计数据持久化。
- [ ] 所有工具调用通过策略检查。
- [ ] 默认没有任何模型可直接获得 root 权限。
- [ ] 至少一个 `.deb` 安装/升级/卸载链路通过测试。
- [ ] 具有可以在虚拟机验证的定制 ISO 原型。

## 15. 第一周开发计划

### 第 1 天：建立固定基线

- 创建 `agentos` 仓库、README、目录框架和 ADR。
- 建立 Ubuntu 22.04 GNOME 42 虚拟机快照。
- 记录 DSH commit/version、Node.js 引擎要求与许可证。

### 第 2 天：DSH 可行性验证

- 运行上游推荐的最小启动流程。
- 验证 SDK/JSON-RPC stdio 或备选 headless 接口。
- 记录消息输出、错误、取消和退出行为。

### 第 3 天：Runtime API 与 Mock

- 定义统一 Runtime/RuntimeEvent/RuntimeCapabilities。
- 编写 MockRuntime 与契约测试。

### 第 4 天：系统只读 API

- 实现 CPU、内存、磁盘和进程查询。
- 为工具输入定义 schema，拒绝自由形式危险参数。

### 第 5 天：任务与审计

- 实现 SQLite 任务表、状态机、审计摘要。
- 测试服务重启与任务恢复。

### 第 6 天：systemd 服务

- 建立非 root 的开发服务单元。
- 验证资源限制、日志和健康检查。

### 第 7 天：端到端闭环

- 实现一个最小 CLI 或临时 GTK 客户端。
- 完成：用户指令 → Runtime → 只读工具 → 策略检查 → 结果 → 审计记录。
- 复盘 DSH API 是否满足长期集成条件，再决定下一阶段 UI 工作量。

第一周不以完整 GNOME 扩展或 ISO 为目标；先证明 Runtime 与系统服务的接口可行。

## 16. 架构决策记录（ADR）

在 `docs/adr/` 记录重要选择，每份 ADR 至少包括背景、候选方案、决策、理由、安全影响、维护成本与复审条件。

- ADR-001：为什么基于 Ubuntu 用户空间，而不是修改内核。
- ADR-002：为什么选择 GNOME Extension + GTK4/libadwaita。
- ADR-003：AgentOS Core、Runtime Worker 与 Desktop 的进程身份边界。
- ADR-004：AgentRuntime 协议与 DSH SDK/JSON-RPC stdio 的适配方式。
- ADR-005：DSH 与 Node.js 的版本锁定和打包策略。
- ADR-006：用户会话 D-Bus 与系统服务 IPC 的认证/授权策略。
- ADR-007：特权执行器、polkit 与审批模型。
- ADR-008：任务持久化、幂等操作与崩溃恢复。
- ADR-009：`.deb` 拆包、用户数据保留与升级迁移。
- ADR-010：Cubic 原型与生产 ISO 构建流程。
- ADR-011：云端密钥、本地密钥环与注销后任务的凭据边界。

## 17. 仓库命名与项目描述

### 推荐仓库名：`agentos`

**项目标题：** `AgentOS — An Agent-Native Operating Environment for Linux`

**中文描述：** 基于 Ubuntu/GNOME 的原生智能体操作环境，将持久运行的 AI Agent、系统可观测性、任务调度、桌面交互与权限受控的系统操作整合为可安装的 Linux 用户空间平台。

**GitHub About：**

> An agent-native operating environment for Linux with GNOME integration, persistent agents, system observability, task orchestration, and policy-controlled actions.

备选名称：

| 仓库名 | 定位 |
|---|---|
| `agentos` | 主项目，短且可扩展（首选） |
| `agent-os-linux` | 突出 Linux 平台 |
| `agentos-gnome` | 仅当项目长期聚焦 GNOME 桌面层时考虑 |
| `linux-agent-runtime` | 更偏后台运行时，不充分表达桌面与发行交付 |

发布前另行检查 GitHub 同名仓库、商标与包名冲突。

## 18. 官方参考资料

以下链接用于后续实施前核对接口与构建约束。上游项目可能更新，应优先查阅锁定版本对应的文档。

1. DeepSeek Harness 官方仓库：<https://github.com/deepseek-ai/deepseek-harness>
2. DSH 官方文档：<https://deepseek-harness.github.io/deepseek-harness/>
3. DSH CLI 说明（包括 profile 与 SDK/ACP 模式）：<https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/cli/README.md>
4. GNOME Shell Extension 指南：<https://gjs.guide/extensions/>
5. Ubuntu 22.04 `gnome-shell-extensions` 包信息：<https://packages.ubuntu.com/jammy/gnome-shell-extensions>
6. Debian Maintainer Guide — 构建软件包：<https://www.debian.org/doc/manuals/maint-guide/build.en.html>
7. Ubuntu 镜像构建概览：<https://ubuntu.com/project/docs/processes/image-builds/>

---

## 最终实施顺序

**P0 验证 DSH → P1 Core/只读监控 → P2 GNOME 扩展与桌面 → P3 事件诊断 → P4 受控操作 → P5 `.deb` → P6 定制 ISO。**

优先级原则：先证明 Runtime 接口与系统权限边界，再投入 GNOME 体验；先让软件包在干净 Ubuntu 中稳定安装与升级，再将软件包集成进 ISO。无论 Agent 或模型是否可用，AgentOS 都不应破坏 Ubuntu 的基础登录、桌面与恢复能力。
