# MiniCodex2 Agent 开发准则

## 上下文缓存纪律

- 把 provider prompt cache 的稳定性当成一等工程要求。
- 在一个 session 运行期间，不要把会变化的 `system` / `runtime` / 控制消息插入稳定上下文前缀。
- 启动期稳定上下文，包括 runtime protocol、environment summary、skill context，必须在 session 初始化后保持字节级稳定，除非用户修改配置或重启 session。
- 动态 facts、evidence、tool results、memory updates、idle reflections、progress notes 必须 append-only，或者放在稳定历史消息之后。它们不能每轮重写前缀字节。
- 如果某个 context section 需要频繁变化，优先把它持久化为 append-only message / buffer entry，而不是每轮在历史前面重新 build。
- 修改 context assembly 前后必须用日志或测试验证缓存命中。为了“看起来更清晰”而导致 cache hit rate 崩掉，算回归。

## Agent / Model / Memory 职责边界

- Runtime 负责感知器官、执行器官、安全边界、日志、持久化、上下文组织和失败回灌。
- Memory System 负责把用户偏好、项目事实、工作流经验、长期目标、阶段计划、证据记录和压缩摘要持久化，并在合适时机以低噪声方式注入上下文。
- Model 负责语义判断、项目理解、下一步决策、错误归因、是否调用工具、是否写入记忆、是否更新计划。
- 自然语言意图、项目语义、需求优先级、事实是否值得长期记忆这类判断，优先交给模型；不要用脆弱关键词在代码里硬判。
- Runtime 可以给模型更好的提示词、工具 affordance、结构化事实和可写入记忆的接口，但不要把具体项目业务判断写死进 Runtime。
- Memory 不是普通日志。日志记录发生了什么；Memory 记录未来会复用、会影响决策、能减少重复探索的信息。

## Skill Runtime 纪律

- Skill 是工作流指导包，不是运行中的进程实例。
- Agent OS 的硬策略不属于 skill：路径安全、权限、命令超时、后台进程管理、tool schema 校验、写后验证 gate、event log、context buffer、memory store 都必须留在 Runtime / Agent OS 层。
- 使用 skill manifest 元数据决定 `domain`、`role`、`priority` 和冲突选择，不要让自然语言 skill 正文静默决定谁接管主流程。
- 外部 primary skill 不能静默接管某个 domain。除非用户明确配置并信任，否则只能作为 reference 暴露，并且要有 warning。

## 开发纪律

- 优先做通用 agent 机制，不为了当前测试项目写一次性特殊逻辑。
- 如果必须加入启发式，先问它属于 Runtime 硬策略、Code Skill 指导、Memory 经验，还是项目临时逻辑；不要混层。
- 修改代码后必须运行最窄但有意义的验证。
- 验证失败时，把第一个 blocking failure 回灌给模型，而不是继续跑下游无关测试。
- 长驻服务必须使用后台进程工具，普通 `run_command` 只能用于会退出的命令。
- 工具 schema 要尽量容错并给出可恢复反馈，避免模型因为一个参数名或路径格式错误直接 blocked。

## 记忆、目标与计划

- Goal / WorkPlan / Evidence 属于长期任务支撑能力，不是普通聊天文本。
- Project Fact Memory 记录项目事实，例如启动命令、端口、账号、测试路径、工具链位置、常见失败原因。
- Workflow Memory 记录可复用流程，例如“如何启动并联调某项目”“如何验证某类 UI 流程”“某个工具失败时如何恢复”。
- Turn Memory / Context Checkpoint 记录当前长任务的压缩交接信息，帮助模型在长上下文或压缩后继续推进。
- 计划可以随着用户新需求、设计文档更新和验证证据修正，但修改必须可追踪。
- Evidence 应记录真实工具结果、HTTP/browser smoke、命令输出或人工验收来源；不能把模型自称“完成”直接当成同等级证据。
- 可复用的启动命令、测试流程、账号环境、路径规律、失败修复经验，应通过 memory / workflow memory 持久化，避免每轮从头探索。

## 系统文档索引

AGENTS.md 只放高优先级短准则，避免每轮上下文过长。需要细节时再读取下列文档：

- `docs/architecture.md`：总体架构、主循环、模块关系。
- `docs/context.md`：上下文组织、压缩、缓存与消息结构。
- `docs/cache-optimization-notes.md`：prompt cache 命中率问题、排查过程和设计结论。
- `docs/agent-runtime-boundaries.md`：Agent / Runtime / Model 职责边界。
- `docs/code-world-robot.md`：信息世界机器人、idle reflection、主动性和长期记忆构想。
- `docs/verification.md`：验证策略与失败回灌原则。
- `docs/failure-handling.md`：blocked failure、自动修复和恢复逻辑。
- `docs/events.md`：事件流与 TUI/API 状态反馈。
- `docs/configuration.md`：配置项、模型 profile、skill 和 web tools。
- `docs/prd.md`、`docs/milestones.md`：产品需求与里程碑。
