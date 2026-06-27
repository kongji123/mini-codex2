# MiniCodex 代码 Skill

当任务需要读取项目、修改文件、验证行为、调试失败，或推进多步骤工作计划时，使用这个 skill。

这个 skill 是 MiniCodex2 当前内置代码开发指导的外置化形态。它只描述代码开发工作流，不替代 Agent OS 的硬约束。路径安全、权限、命令超时、后台进程、工具 schema 校验、写后验证、事件日志、上下文缓存和记忆系统仍由 Agent OS 负责。

## 职责边界

- Agent OS 提供感知器官、执行器官、安全边界、日志、持久化、上下文缓存和失败回灌。
- 代码 Skill 提供如何做代码任务的工作流指导，例如项目探测、修改闭环、验证、联调、计划和工作流记忆。
- Model 负责理解项目语义、判断下一步、决定调用哪些工具、归因失败和更新计划。
- 不要把具体项目、语言或框架的业务判断写死进 Runtime。可以读取项目事实、加载本 skill 的参考文件，并让模型基于事实决策。

## 运行原则

- 以用户目标作为最高事实来源。
- 只读取完成下一个连贯步骤所需的项目证据。
- 优先使用项目已有脚本、文档、包元数据和既有约定。
- 如果相关修改不依赖新的证据，就作为一个连贯批次一起修改。
- 修改代码后，运行最窄但有意义的验证。
- 如果验证失败，把第一个阻塞性失败作为下一步诊断目标。
- 对 UI/API 联调问题，验证用户真实经历的路径，而不是只验证语法。
- 将稳定的启动、测试、冒烟和调试流程记录为 workflow memory。

## 何时加载参考

不要每轮加载所有参考文件。当前任务需要更细指导时再调用 `load_skill`：

- 项目入口、目录和启动方式不清楚：加载 `references/project-inspection.md`
- 需要成批修改代码：加载 `references/code-change-loop.md`
- 写后验证或验证失败：加载 `references/verification-strategy.md`
- 前后端、API、浏览器或服务联调：加载 `references/integration-debug.md`
- 宽目标、二期目标、计划状态或 evidence：加载 `references/work-plan-and-evidence.md`
- 发现可复用流程、路径、命令或调试经验：加载 `references/workflow-memory.md`

## 可加载参考

- `references/project-inspection.md`
- `references/code-change-loop.md`
- `references/verification-strategy.md`
- `references/integration-debug.md`
- `references/work-plan-and-evidence.md`
- `references/workflow-memory.md`
