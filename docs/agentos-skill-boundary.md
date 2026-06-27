# AgentOS / Skill Boundary

## 中文

MiniCodex2 的长期方向是把系统拆成 `Agent OS + Skill / Plugin`，但第一阶段不要过度拆分。当前实现采用渐进式边界：

- **Agent OS** 负责稳定能力：路径安全、权限、命令超时、后台进程、工具 schema 校验、事件日志、上下文缓存、记忆系统、失败回灌和持久化。
- **Code Skill** 负责代码任务的方法论：项目探测、代码修改闭环、验证策略、联调调试、工作计划、evidence 使用和工作流记忆。
- **Model** 负责语义智能：理解用户目标、判断项目结构、选择工具、归因错误、决定是否更新记忆或计划。

这条边界的核心原则是：Runtime 提供器官和稳定支撑，Skill 提供流程指导，Model 做语义判断。自然语言意图、项目语义、需求优先级和框架细节不应由脆弱关键词在 Runtime 中硬判。

目前 Memory 和 WorkPlan 仍保留在 Agent OS 内部，因为它们是跨领域能力；Code Skill 只提醒模型什么时候使用这些能力。等接口稳定后，可以再把更强的 Planning / Memory 能力演进成系统插件。

## English

MiniCodex2 is moving toward an `Agent OS + Skill / Plugin` architecture, but the first phase should stay incremental.

- **Agent OS** owns stable runtime capabilities: path safety, permissions, command timeouts, background processes, tool schema validation, event logs, context buffering, memory, failure feedback, and persistence.
- **Code Skill** owns code-task workflow guidance: project inspection, code-change loops, verification strategy, integration debugging, work plans, evidence usage, and workflow memory.
- **Model** owns semantic intelligence: understanding user goals, interpreting project structure, selecting tools, diagnosing errors, and deciding whether to update memory or plans.

The boundary is: Runtime provides organs and stable support, Skills provide workflow guidance, and the Model performs semantic judgment. Natural-language intent, project semantics, requirement priority, and framework details should not be hard-coded into Runtime with brittle keyword rules.

Memory and WorkPlan remain internal Agent OS capabilities for now because they are cross-domain. Code Skill only guides the model on when to use them. Stronger Planning / Memory can become system plugins later after the interfaces settle.

