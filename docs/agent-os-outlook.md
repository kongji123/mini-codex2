# Agent OS Outlook / Agent OS 展望

This document records a forward-looking discussion about agent runtime, one-turn completeness, and future AI software industry layers.

本文档记录关于 Agent Runtime、一回合完备性和未来 AI 软件工业分层的长期思考。

It is not an immediate MiniCodex2 implementation requirement. It should guide long-term product and architecture thinking without expanding the current milestone scope.

它不是 MiniCodex2 当前阶段的立即实现要求。它只用于指导长期产品和架构思考，不应扩大当前里程碑范围。

---

## Core Thesis / 核心论点

Agent capability is not equal to model capability.

Agent 能力不等于模型能力。

```text
Agent capability ~= Model capability x Agent Runtime x Harness x Domain Skill x Evaluation
Agent 能力 ~= 模型能力 x Agent Runtime x Harness x 领域 Skill x 评测
```

The model determines the capability ceiling.

模型决定能力上限。

The agent runtime determines repeatable delivery.

Agent Runtime 决定可重复交付能力。

Evaluation proves whether the model and runtime actually work together.

真实评测决定模型与 runtime 是否真正配合成功。

---

## Agent OS Analogy / Agent OS 类比

A large language model can be viewed as an intelligent CPU.

大语言模型可以被看作智能 CPU。

An agent runtime can be viewed as an intelligent operating system.

Agent Runtime 可以被看作智能操作系统。

The model is responsible for reasoning, understanding, generation, and repair suggestions.

模型负责推理、理解、生成和修复建议。

The Agent OS is responsible for:

Agent OS 负责：

- session and history / 会话和历史
- memory and context management / 记忆和上下文管理
- tool calling / 工具调用
- permission gates / 权限门禁
- path safety / 路径安全
- task state / 任务状态
- event streams / 事件流
- failure handling / 失败处理
- recovery loops / 恢复循环
- background jobs / 后台任务
- artifact storage / 产物存储
- human-in-the-loop control / 人在回路控制
- evaluation harnesses / 评测 harness

AI competition is therefore not only CPU competition. It is also operating system competition.

因此 AI 竞争不仅是 CPU 竞争，也是操作系统竞争。

---

## Code Agent As The First Strong Skill / Code Agent 作为第一个强 Skill

Code agents may not be the final form of agents.

Code Agent 未必是 Agent 的终局形态。

They may be the first high-value skill running on top of an emerging Agent OS.

它们可能是运行在新兴 Agent OS 之上的第一个高价值 Skill。

Code succeeds early because it has unusually clear verification mechanisms:

代码领域之所以最先成功，是因为它有异常清晰的验证机制：

- compile / 编译
- test / 测试
- lint / 静态检查
- build / 构建
- HTTP smoke tests / HTTP 烟测
- benchmark tasks / benchmark 任务

A useful future layering may look like this:

一种可能的未来分层是：

```text
LLM
-> Agent OS
-> Workflow Harness
-> Code Skill / Document Skill / Data Skill / Legal Skill / Ops Skill
-> Application UI
```

MiniCodex2 currently sits near:

MiniCodex2 当前所处的位置更接近：

```text
Agent OS + Code Skill + partial Engineering Skill
Agent OS + Code Skill + 部分 Engineering Skill
```

---

## One-Turn Completeness / 一回合完备性

One-turn completeness is not only a code-agent metric.

一回合完备性不只是 Code Agent 的指标。

It is a core metric for execution-oriented agents.

它是所有执行型 Agent 的核心指标。

Definition:

定义：

```text
After a user states a goal,
the agent completes the required understanding, execution, verification, repair, and delivery
with minimal human intervention.

用户提出一个目标后，
Agent 在最少人工介入下，
完成必要的理解、执行、验证、修复和交付。
```

Generic formula:

通用公式：

```text
One-turn completeness =
Goal achieved
+ Domain acceptance passed
+ Failure handled
+ Deliverable produced
+ Human intervention minimized

一回合完备性 =
目标完成
+ 领域验收通过
+ 失败被处理
+ 交付物产生
+ 人工介入最小化
```

However, what counts as complete is domain-specific.

但是，什么算“完备”必须由领域 Skill 定义。

Code domain completeness:

代码领域完备性：

- code is written / 代码已写完
- tests pass / 测试通过
- service starts / 服务能启动
- build succeeds / 构建成功
- obvious regressions are avoided / 避免明显回归

Document domain completeness:

文档领域完备性：

- required sections exist / 必要章节存在
- formatting is consistent / 格式一致
- export succeeds / 导出成功
- layout has no overflow / 排版无溢出
- citations and references are preserved / 引用和参考不丢失

Data domain completeness:

数据领域完备性：

- data source loads / 数据源加载成功
- schema and field types are understood / schema 和字段类型被理解
- metric definitions are clear / 指标口径清晰
- charts are generated / 图表生成
- conclusions are traceable to data / 结论可追溯到数据

Ops domain completeness:

运维领域完备性：

- issue is reproduced / 故障能复现
- logs provide evidence / 日志提供证据
- root cause is identified / 根因被定位
- fix is applied / 修复动作已执行
- health check passes / 健康检查通过
- rollback path is clear / 回滚路径清晰

---

## Engineering Skill / 工程 Skill

Current code agents can already feel like strong programmers.

当前的 Code Agent 已经很像强力程序员。

They do not always feel like tech leads.

但它们并不总像技术负责人。

The missing layer is Engineering Skill:

缺失的一层是 Engineering Skill：

- requirement clarification / 需求澄清
- system design / 系统设计
- task breakdown / 任务拆解
- test matrix design / 测试矩阵设计
- risk assessment / 风险评估
- acceptance criteria / 验收标准
- release checks / 发布检查
- retrospectives / 复盘沉淀

Tools such as OpenSpec, Superpowers, and gstack can be interpreted as early prototypes of Engineering Skill rather than replacements for agents.

OpenSpec、Superpowers、gstack 这类工具可以被理解为 Engineering Skill 的早期原型，而不是 Agent 的替代品。

Possible mapping:

可能的映射：

- OpenSpec: requirement and specification skill / 需求与规格 Skill
- Superpowers: engineering discipline skill / 工程纪律 Skill
- gstack: delivery, QA, ship, and retro skill / 交付、QA、发布和复盘 Skill

---

## Why Open Source Runtime Does Not Mean Runtime Is Unimportant / 为什么开源 Runtime 不代表 Runtime 不重要

Open source does not imply low importance.

开源不代表不重要。

Linux, Kubernetes, and PyTorch are all important and open source.

Linux、Kubernetes 和 PyTorch 都非常重要，也都是开源的。

The better question is where value is captured.

更好的问题是：价值最终沉淀在哪一层。

OpenAI may open-source Codex CLI/runtime because:

OpenAI 可能选择开源 Codex CLI/runtime，是因为：

1. It expands the market. / 它能扩大市场。
2. It encourages community innovation around agent runtime and workflows. / 它能鼓励社区在 agent runtime 和 workflow 上创新。
3. It keeps the deepest moat at the model and inference-service layer. / 它把最深的护城河留在模型和推理服务层。

Possible business structure:

可能的商业结构：

```text
Runtime / Harness: open ecosystem
Model / inference service / cloud infrastructure: commercial service

Runtime / Harness：开源生态
模型 / 推理服务 / 云基础设施：商业服务
```

This means open-sourcing Codex runtime does not imply runtime is unimportant.

因此，开源 Codex Runtime 不代表 Runtime 不重要。

It may imply runtime is important enough that the industry benefits from shared infrastructure.

它反而可能说明：Runtime 重要到需要整个行业共同建设基础设施。

---

## Codex As An Agent OS Prototype / Codex 作为 Agent OS 原型

OpenAI open-sourcing Codex CLI/runtime may not only be about releasing a code tool.

OpenAI 开源 Codex CLI/runtime，可能不只是为了发布一个代码工具。

It may also be a way to show how to connect the LLM as an intelligent CPU to real-world execution surfaces:

它也可能是在展示如何把 LLM 这个智能 CPU 接到真实世界的执行界面上：

- files / 文件
- commands / 命令
- permissions / 权限
- tools / 工具
- context / 上下文
- verification / 验证
- recovery / 恢复
- human interaction / 人机交互

The open-source value of Codex is therefore not limited to Code Agent itself.

因此，Codex 的开源价值不只在 Code Agent 本身。

It also provides an engineering sample for agent runtime design:

它还提供了一个 agent runtime 设计的工程样本：

- how to organize an agent runtime / 如何组织 agent runtime
- how to design tool protocols / 如何设计工具协议
- how to enforce execution and permission boundaries / 如何执行命令与权限边界
- how to expose event streams / 如何暴露事件流
- how to provide TUI status feedback / 如何提供 TUI 状态反馈
- how to manage context / 如何管理上下文
- how to execute commands / 如何执行命令
- how to recover from failure / 如何从失败中恢复

Code is the easiest domain to validate, so this pattern appears first as Codex.

代码领域最容易验证，所以这种模式最先以 Codex 的形态出现。

However, Codex is not yet a fully independent, domain-neutral Agent OS SDK.

但是，Codex 还不是一个完全独立、领域无关的 Agent OS SDK。

It is closer to:

它更接近：

```text
Codex =
Agent OS prototype
+ Code Skill
+ CLI/TUI product
+ OpenAI model integration

Codex =
Agent OS 原型
+ Code Skill
+ CLI/TUI 产品
+ OpenAI 模型接入
```

Future layering may separate these responsibilities:

未来这些职责可能会进一步分层：

```text
Agent OS Core
- session
- context
- tools
- permissions
- events
- recovery
- artifact store
- human approval

Domain Skill SDK
- code skill
- document skill
- data skill
- ops skill
- finance skill

Application Shell
- CLI
- TUI
- desktop
- web app
```

From this perspective, Codex open source can be understood as an Agent OS prototype carried by a Code Agent.

从这个角度看，Codex 开源可以被理解为：一个以 Code Agent 为载体的 Agent OS 原型。

It allows the ecosystem to learn how to operate the LLM "CPU" through runtime, tools, permissions, verification, and interaction design.

它让生态可以学习如何通过 runtime、工具、权限、验证和交互设计来操作 LLM 这个“CPU”。

MiniCodex2 should follow the same learning path conservatively:

MiniCodex2 应该保守地沿着同样的学习路径前进：

1. Start from Code Skill because it is verifiable. / 从 Code Skill 开始，因为它最可验证。
2. Strengthen one-turn delivery through runtime discipline. / 通过 runtime 纪律强化一回合交付。
3. Identify which parts are generic Agent OS mechanisms. / 识别哪些部分是通用 Agent OS 机制。
4. Avoid prematurely claiming to be a universal Agent OS. / 避免过早宣称自己是通用 Agent OS。

---

## Current Industry Stage / 当前行业阶段

Many agent products today look like bundled systems:

今天很多 Agent 产品像是绑定在一起的系统：

```text
Agent product =
custom Agent OS
+ custom domain skill
+ custom UI
+ custom evaluation

Agent 产品 =
自定义 Agent OS
+ 自定义领域 Skill
+ 自定义 UI
+ 自定义评测
```

This resembles an early bare-machine era of computing, where applications, runtimes, and operating-system-like responsibilities are tightly mixed.

这很像计算机早期的裸机时代：应用、runtime 和类操作系统职责紧密绑在一起。

Over time, the industry may separate into:

随着时间发展，行业可能逐步分化为：

```text
LLM
-> Agent OS
-> Workflow Harness
-> Domain Skill
-> Application
```

Companies may eventually stop rebuilding memory, context, recovery, permission, and tool orchestration for every vertical agent.

公司最终可能不再为每个垂直 Agent 重复实现记忆、上下文、恢复、权限和工具编排。

Instead, they may build domain skills on top of shared Agent OS layers.

相反，它们可能会在共享 Agent OS 层之上构建领域 Skill。

---

## Implication For MiniCodex2 / 对 MiniCodex2 的启示

MiniCodex2 should not try to implement this whole future immediately.

MiniCodex2 不应试图立刻实现这个完整未来。

The practical path is:

更务实的路径是：

1. Make Code Skill reliable first. / 先把 Code Skill 做可靠。
2. Strengthen one-turn code delivery through tests, verification, failure packs, repair loops, and benchmark tasks. / 通过测试、验证、failure pack、修复循环和 benchmark 强化代码一回合交付能力。
3. Mark which parts are code-specific and which are generic agent runtime mechanisms. / 标记哪些是代码领域特有逻辑，哪些是通用 agent runtime 机制。
4. Later, test a second skill such as document or data workflows to discover what truly belongs in Agent OS. / 后续尝试第二个 Skill，例如文档或数据 workflow，以发现什么真正属于 Agent OS。

Agent OS should not be invented purely by abstraction.

Agent OS 不应只靠抽象想象出来。

It should emerge from repeated patterns across high-completeness skills.

它应该从多个高完备性 Skill 中重复出现的模式里生长出来。

Final summary:

最终总结：

```text
We thought we were building a code agent.
In practice, code agents may be the most testable path toward discovering Agent OS,
Engineering Skill, and future domain-skill layering.

我们以为自己在做 Code Agent。
实际上，Code Agent 可能是探索 Agent OS、Engineering Skill
和未来领域 Skill 分层的最可验证路径。
```

