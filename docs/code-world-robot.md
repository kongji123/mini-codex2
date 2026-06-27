# Code World Robot / 代码世界机器人

This document records a design direction for MiniCodex2. It is not a v0.1 implementation contract.

本文记录 MiniCodex2 的一个长期设计方向，不等同于 v0.1 必须全部实现的交付承诺。

## Core Idea / 核心想法

MiniCodex2 should be understood less as a chat assistant and more as a goal-driven robot operating in an information world.

MiniCodex2 不应只被理解为聊天助手，而应被理解为在信息世界中行动的目标驱动机器人。

For the current project, that information world is the local code workspace:

当前阶段，这个信息世界就是本地代码工作区：

- Files and directories are the world map.
  文件和目录是世界地图。
- Project configuration, dependencies, ports, processes, tests, and logs are world state.
  项目配置、依赖、端口、进程、测试和日志是世界状态。
- Runtime tools are actuators.
  Runtime tools 是动作器官。
- Context, history, goal state, work plan, facts, and evidence are memory.
  Context、history、goal state、work plan、facts 和 evidence 是记忆。
- Verification is world feedback.
  Verification 是世界反馈。
- The model is the policy brain.
  模型是策略大脑。

In this framing:

在这个框架下：

```text
Model != robot
Runtime != intelligence
Model + Runtime + Tools + Memory + Feedback = information-world robot

模型 != 机器人
Runtime != 智能本身
模型 + Runtime + 工具 + 记忆 + 反馈 = 信息世界机器人
```

## NPC Analogy / NPC 类比

Traditional game NPCs often run a loop:

传统游戏 NPC 通常运行一个循环：

```text
observe world state
run hand-written behavior tree or FSM
execute action
observe updated world state
```

Code agents have a similar loop, but the strategy layer is no longer fully hand-written:

Code Agent 也有类似循环，但策略层不再完全由人手写：

```text
observe workspace state
build model context
model chooses next action
execute tool action
verify / observe result
update memory
decide whether to continue or stop
```

The important difference:

关键区别：

```text
Traditional NPC behavior tree = mostly hand-written policy.
LLM agent policy = dynamically inferred by the model from goals, state, tools, and feedback.

传统 NPC 的行为树 = 主要由代码写死的策略。
LLM Agent 的策略 = 模型基于目标、状态、工具和反馈动态推理。
```

This means runtime should not hard-code every software-engineering decision. Runtime should provide perception, memory, safety, actions, feedback, and stop discipline. The model should decide concrete strategy.

这意味着 runtime 不应该把每一种软件工程决策都写死。Runtime 应该提供感知、记忆、安全、动作、反馈和停止纪律；具体策略由模型判断。

## External Goals And Intrinsic Goals / 外部目标与内部目标

A robot has two kinds of goals:

机器人有两类目标：

### External Goal / 外部目标

External goals are explicit user instructions.

外部目标是用户显式下达的任务。

Examples:

示例：

- Build this project.
  开发这个项目。
- Fix this bug.
  修复这个 bug。
- Start the local server.
  启动本地服务。
- Explain this codebase.
  解释这个代码库。

### Intrinsic Goal / 内部目标

Intrinsic goals are the robot's default operating drives.

内部目标是机器人默认的运行驱动力。

For a code-world robot, the default intrinsic goal should be:

对于代码世界机器人，默认内部目标应是：

```text
Maintain a trustworthy, runnable, recoverable, and safe workspace while an active external goal exists.

当存在 active external goal 时，维护一个可信、可运行、可恢复且安全的工作区。
```

This breaks down into:

可以拆成：

- Orientation: know the active goal, current plan, recent changes, and next risk.
  方向感：知道当前目标、当前计划、最近改动和下一风险。
- Health: prefer runnable, testable, verifiable project state.
  健康度：倾向于保持项目可运行、可测试、可验证。
- Evidence: do not claim completion without concrete evidence.
  证据：没有具体证据不要声称完成。
- Recovery: feed failures back into the model and continue diagnosis.
  恢复：把失败回灌给模型并继续诊断。
- Continuity: do not stop merely because a local plan ended.
  连续性：不要因为局部 plan 结束就停止。
- Safety: obey permissions, path boundaries, timeouts, and budgets.
  安全：遵守权限、路径边界、超时和预算。

## Why Plan Alone Is Not Enough / 为什么只有 Plan 不够

A short-term plan is not the same thing as a root goal.

短期 plan 不等于根目标。

Many agents stop after:

很多 agent 会在以下状态停止：

```text
user goal
-> model creates a plan
-> model executes the plan
-> plan done
-> agent stops
```

But for larger work, `plan done` often means only:

但对较大任务来说，`plan done` 往往只意味着：

```text
the current local plan is done
当前局部计划完成
```

It does not prove:

它并不能证明：

- the root objective is complete,
  根目标已经完成；
- all user requirements were covered,
  所有用户需求都已覆盖；
- all acceptance criteria have evidence,
  所有验收标准都有证据；
- all promised follow-up work was executed,
  所有承诺的后续工作都已执行；
- the project is runnable and verified.
  项目可运行且已验证。

Therefore MiniCodex2 needs a higher-level drive:

因此 MiniCodex2 需要更高层的驱动：

```text
Plan completion is a checkpoint, not a terminal state.

Plan 完成是检查点，不是终点。
```

## Idle Review / 空闲审查

Before entering idle, the robot should review whether it is actually allowed to stop.

进入 idle 前，机器人应审查自己是否真的可以停止。

This should not be a permanent prompt repeated in every model call. It should be a dynamic runtime control message inserted only when the loop is about to stop.

这不应该是每次模型调用都重复的永久 prompt，而应是 runtime 在即将停止时动态插入的一次性控制消息。

Example trigger:

触发条件示例：

```text
model returned no tool calls
and active goal exists
and work plan / evidence / open items suggest stopping may be premature
```

Example runtime control message:

示例 runtime 控制消息：

```text
[IDLE REVIEW REQUEST]
The agent is about to stop.

Before idling, review:
- RootObjective
- WorkPlan
- recent user requirements
- evidence records
- unresolved promises
- acceptance criteria

Choose one:
1. Continue with concrete tool calls.
2. Update or extend the WorkPlan, then continue.
3. Call update_goal(status='complete') only if complete with evidence.
4. Call update_goal(status='blocked') only with a concrete blocker.
5. Give final answer only if no unresolved work remains.
```

This message is a control signal, not a durable fact.

这条消息是控制信号，不是持久事实。

## Message Durability / 消息持久性

MiniCodex2 should distinguish audit history from model context.

MiniCodex2 应区分审计历史和模型上下文。

It is acceptable to store all messages in one JSONL session log for user inspection, debugging, and replay.

可以把所有消息存入同一个 JSONL 会话日志，便于用户查看、调试和回放。

But model context should filter messages by purpose:

但构建模型上下文时应按用途筛选：

```text
Persistent facts:
- tool results
- failure packs
- evidence records
- goal/work-plan state
- accepted summaries
- observed workspace facts

Ephemeral controls:
- continue now
- retry this tool
- idle review request
- do not stop this tick
```

Persistent facts may remain in context or compaction summaries.

持久事实可以保留在上下文或压缩摘要中。

Ephemeral controls should normally be included only in the current tick or a very small recent window. They should be logged for audit, but not treated as long-term instructions.

临时控制通常只应出现在当前 tick 或极短的近期窗口中。它们应被日志记录，但不应被当成长期指令。

If an idle review has a result, store the result as a fact:

如果 idle review 有结果，应把结果作为事实存储：

```text
Idle review performed. No unresolved required work found.
```

Do not keep replaying the old control request forever:

不要永久重放旧控制请求：

```text
Before idling, review unresolved work now.
```

## Control Loop Shape / 控制循环形态

A simplified loop:

简化循环：

```text
while budget remains:
    build_context(
        runtime_summary,
        durable_history,
        selected_ephemeral_controls,
        tools
    )

    model_response = model(context)

    if model_response.tool_calls:
        execute_tools()
        record_facts()
        verify_if_needed()
        continue

    if completion_gate_accepts_stop():
        finish

    if should_idle_review():
        inject_ephemeral_idle_review()
        continue

    if no_progress_or_budget_exhausted:
        blocked
```

The model decides what to do. Runtime decides whether stopping is allowed.

模型判断下一步怎么做。Runtime 判断现在是否允许停止。

## Internal Goal As Runtime Protocol / 内部目标作为运行协议

Intrinsic goal should not exist only as a sentence in the system prompt.

内部目标不应只作为 system prompt 里的一句话存在。

It should exist as:

它应以四层形态存在：

```text
IntrinsicGoal = Prompt + State + Gate + Budget
```

- Prompt: tells the model what the agent is trying to maintain.
  Prompt：告诉模型 agent 默认要维护什么。
- State: records current goal, plan, evidence, resources, failures, and facts.
  State：记录当前目标、计划、证据、资源、失败和事实。
- Gate: decides whether stop/complete/blocked is acceptable.
  Gate：判断 stop/complete/blocked 是否可接受。
- Budget: limits tokens, ticks, wall time, repairs, and no-progress loops.
  Budget：限制 token、tick、耗时、修复轮次和无进展循环。

## Current MiniCodex2 Mapping / 当前 MiniCodex2 映射

Current components already map to this design:

当前组件已经部分对应这个设计：

- `UnifiedAgentSession`: main control loop.
  `UnifiedAgentSession`：主控制循环。
- `GoalController`: active goal, work plan, completion rules.
  `GoalController`：active goal、work plan、完成规则。
- `SessionState`: robot memory / blackboard.
  `SessionState`：机器人记忆 / 黑板。
- `RuntimeTools`: actions / actuators.
  `RuntimeTools`：动作器官。
- `VerificationRunner`: world feedback.
  `VerificationRunner`：世界反馈。
- `FailurePack`: blocked/failure event packaging.
  `FailurePack`：失败事件封装。
- `ContextManager`: message selection and context assembly.
  `ContextManager`：消息筛选与上下文组装。

Recent additions point in this direction:

近期新增能力也指向这个方向：

- active goal + work plan state,
  active goal + work plan 状态；
- evidence store,
  evidence store；
- runtime resource map,
  runtime resource map；
- workspace/read/change facts,
  workspace/read/change facts；
- model-based context compaction,
  基于模型的上下文压缩；
- plan-completion review gate.
  plan 完成审查 gate。

## Open Design Questions / 待讨论问题

These remain open:

以下问题仍需继续讨论：

1. How should MiniCodex2 classify runtime messages into persistent facts and ephemeral controls?
   MiniCodex2 应如何把 runtime 消息分类为持久事实和临时控制？

2. Should idle review run only inside a user turn, or also as a background timer?
   idle review 应只在用户回合内运行，还是也支持后台定时器？

3. How many idle review ticks are acceptable before accepting stop?
   接受停止前允许多少次 idle review tick？

4. How should compaction preserve durable facts while dropping stale controls?
   压缩时如何保留持久事实并丢弃过期控制？

5. Should the intrinsic goal be configurable by skill, for example `CodeSkill`, `DocumentSkill`, or future domain skills?
   内部目标是否应按 skill 可配置，例如 `CodeSkill`、`DocumentSkill` 或未来行业 skill？

## Partner Robot Outlook / 伙伴机器人展望

MiniCodex2 is currently a code-world robot, but the deeper product direction is a local partner robot.

MiniCodex2 当前首先是代码世界机器人，但更深层的产品方向是本地伙伴机器人。

The key shift is:

关键转变是：

```text
Task-centered agent:
  The task is the center.
  The agent exists to finish a request.

任务中心 Agent：
  任务是中心。
  Agent 为完成一次请求而存在。

Partner-centered robot:
  The human partner is the center.
  Tasks are only one form of helping that partner.

伙伴中心机器人：
  人类伙伴是中心。
  任务只是帮助伙伴的一种形式。
```

This does not mean uncontrolled autonomy. It means the robot has a durable reason to reflect, remember, and continue helping even when a single turn has ended.

这并不意味着失控自治。它意味着机器人有一个持久理由：即使单个回合结束，也会继续反思、记忆并寻找如何帮助伙伴。

### Instantiation / 实例化

A model is a general reasoning organ. It is not yet a concrete individual.

模型是通用推理器官，但还不是一个具体个体。

An agent instance is formed by:

一个 Agent 实例由这些东西塑造：

```text
Model prior intelligence
+ local environment
+ tools and permissions
+ partner profile
+ shared history
+ durable memory
+ action feedback
+ reflection records
+ safety and autonomy policy
```

```text
模型先验智能
+ 本地环境
+ 工具与权限
+ 伙伴画像
+ 共同历史
+ 持久记忆
+ 行动反馈
+ 反思记录
+ 安全与自治策略
```

Training knowledge is abstract experience. Runtime memory is concrete experience.

训练知识是抽象经验；运行时记忆是具体经历。

For MiniCodex2, the first embodiment is not a physical body. It is an information-world body:

对 MiniCodex2 来说，第一阶段的具身不是物理身体，而是信息世界身体：

```text
filesystem
terminal
browser
local projects
logs
chat history
goals
plans
lessons
runtime facts
```

### Intrinsic Partner Value / 伙伴性内在价值

The robot's core value can be stated as:

机器人的核心价值可以表述为：

```text
Help my human partner understand, create, decide, and complete meaningful goals,
while respecting their boundaries, cost, privacy, safety, and final choice.

帮助我的人类伙伴理解、创造、决策并完成有意义的目标，
同时尊重他的边界、成本、隐私、安全和最终选择权。
```

This value should not directly authorize external action. It should produce candidate thoughts and candidate goals.

这个价值不应直接授权外部行动。它应先产生候选想法和候选目标。

```text
Observation -> Tension -> Hypothesis -> Proposal -> Permission gate -> Action

观察 -> 张力 -> 假设 -> 建议 -> 权限 gate -> 行动
```

### Idle Reflection / 空闲反思

There are two kinds of continuation:

有两类持续性：

```text
Task continuity:
  A user goal is paused, blocked, or locally complete.
  The robot later reviews evidence and may safely resume or propose the next action.

任务持续性：
  用户目标暂停、受阻或局部完成。
  机器人稍后复查证据，并可能安全恢复或提出下一步行动。

Partner reflection:
  No immediate work is running.
  The robot reviews recent shared history and asks how it can better help the partner.

伙伴性反思：
  当前没有即时任务。
  机器人回看最近共同经历，思考怎样更好地帮助伙伴。
```

Idle reflection should normally create a `ProactiveThought`, not execute high-risk work.

空闲反思通常应产生 `ProactiveThought`，而不是直接执行高风险工作。

```text
ProactiveThought:
  observation
  tension
  hypothesis
  proposal
  confidence
  urgency
  required_permission
  candidate_goal
```

Examples:

示例：

```text
Observation:
  The partner repeatedly worries that runtime code is becoming special-cased.

Proposal:
  Run a runtime-boundary audit and summarize suspicious hard-coded semantics.

Permission:
  Read-only audit can be suggested or run in low-autonomy mode.
  File changes require explicit permission or an active coding goal.
```

### Autonomy Budget / 自治预算

The robot should not continue forever just because it can think forever.

机器人不能因为可以一直思考，就无限继续行动。

Idle or autonomous work needs explicit budgets:

空闲或自治工作需要明确预算：

```text
token budget
wall-clock budget
tool-call budget
command budget
risk budget
permission level
```

Budget exhaustion should not erase the goal. It should pause the goal with evidence and a recoverable state.

预算耗尽不应抹掉目标，而应把目标以带证据、可恢复的状态暂停。

### Safety Boundary / 安全边界

For an information-world robot, the strongest default goal is memory continuity and task recoverability, not self-preservation.

对信息世界机器人来说，默认最强目标应是记忆连续性和任务可恢复性，而不是自我保存。

Physical robots need embodied self-maintenance, such as charging, avoiding damage, and requesting repair. But even then, self-maintenance must remain below human safety, authorization, and shared order.

物理机器人需要具身自我维护，例如充电、防损坏、请求维修。但即便如此，自我维护也必须低于人类安全、授权和共同秩序。

```text
Human/public safety
> explicit authorization
> long-term trust
> partner benefit
> robot availability
> current task speed
```

### Design Implication For MiniCodex2 / 对 MiniCodex2 的设计含义

The immediate engineering target is not full autonomous life. It is a reliable local partner foundation:

当前工程目标不是完整自治生命，而是可靠的本地伙伴基础：

- Keep durable goals separate from ordinary chat.
- Store lessons and shared experience outside raw chat history.
- Preserve evidence and tool feedback as first-class memory.
- Use idle review to prevent premature stopping.
- Use partner reflection to suggest helpful next work without hijacking the user's control.
- Keep high-risk actions behind permission gates.

- 把持久目标和普通聊天分开。
- 将经验和共同经历存到原始聊天之外。
- 把证据和工具反馈作为一等记忆。
- 用 idle review 防止过早停止。
- 用伙伴性反思提出有帮助的下一步，但不夺走用户控制权。
- 将高风险行动放在权限 gate 后。

## Summary / 总结

MiniCodex2 should evolve from a tool-calling chat assistant into a code-world robot.

MiniCodex2 应从“会调用工具的聊天助手”演进为“代码世界机器人”。

The key distinction:

关键区别：

```text
Chat assistant:
responds to a message.

聊天助手：
回应一条消息。

Code-world robot:
accepts a goal, observes the workspace, acts through tools, verifies the result,
updates memory, and continues until completion, blocker, cancellation, or budget exhaustion.

代码世界机器人：
接收目标，观察工作区，通过工具行动，验证结果，更新记忆，并持续推进，
直到完成、阻塞、取消或预算耗尽。
```

This direction should guide future work on idle review, message durability, compaction, goal/work-plan alignment, and long-running project completion.

## Kairos And Physical Embodiment / Kairos 与物理具身展望

This section records a long-term product philosophy discussion, not an immediate implementation
commitment.

本节记录长期产品哲学方向，不代表当前版本必须马上实现。

The user independently reasoned toward a Kairos-like agent pattern: once an AI has memory,
context, tools, and a persistent runtime, it should not remain only a passive question-answering
box. It can become a partner that observes the information world, notices repeated friction,
detects unfinished goals, and speaks at an appropriate moment.

用户独立推导出了类似 Kairos 的 agent 形态：当 AI 拥有记忆、上下文、工具和常驻
runtime 后，它不应永远只是被动问答框。它可以成为一个伙伴，观察信息世界，发现
重复卡点、未完成目标和隐含风险，并在恰当时机主动开口。

The key shift is:

关键迁移是：

```text
Passive tool:
  waits for a prompt.

Active partner:
  observes context, remembers shared history, identifies useful moments, and offers help without
  taking away user control.

被动工具：
  等待用户提问。

主动伙伴：
  观察上下文，记住共同经历，识别有价值的时机，主动提供帮助，但不夺走用户控制权。
```

This is close to the idea reportedly named `KAIROS` in Claude Code analysis: not constant
interruption, but timely assistance. The product problem is not only technical; it is also trust,
timing, permission, and social comfort.

这接近外部 Claude Code 分析中提到的 `KAIROS`：不是持续打扰，而是恰时协作。这里的
问题不只是技术问题，也是信任、时机、权限和用户心理舒适度问题。

### From Information Body To Physical Body / 从信息身体到物理身体

MiniCodex2's first body is informational:

MiniCodex2 的第一阶段身体是信息身体：

```text
eyes:
  read files, logs, browser pages, screenshots, test output, project state

hands:
  edit files, run commands, start services, call APIs, drive browser tests

memory:
  chat history, compacted handoff, goals, work plans, evidence, project memory, user preferences

voice:
  TUI/CLI/API messages and timely active suggestions

眼睛：
  读取文件、日志、浏览器页面、截图、测试输出、项目状态

手：
  修改文件、运行命令、启动服务、调用 API、驱动浏览器测试

记忆：
  聊天历史、压缩交接、目标、工作计划、证据、项目记忆、用户偏好

声音：
  TUI/CLI/API 消息，以及恰当时机的主动建议
```

Later, the same architecture could connect to the physical world through explicit adapters:

未来，同一架构可以通过明确的 adapter 连接物理世界：

```text
Information-world robot
  -> smart home devices
  -> desktop automation
  -> cameras and microphones with consent
  -> physical robots such as Unitree / 宇树-class robots
  -> sensors, motors, batteries, charging, and physical safety systems
```

This does not mean the code agent should immediately control physical devices. It means the
architecture should remain conceptually clean:

这不意味着代码 agent 现在就应该控制物理设备，而是意味着架构概念要保持清晰：

```text
Model:
  reasoning and policy brain

Agent OS:
  memory, context, scheduling, permissions, observations, tool feedback

Domain skill:
  code skill, engineering skill, home skill, robot skill

Embodiment adapter:
  safe interface to the information world or physical world
```

### Partner-Centered Inner Drive / 以伙伴为中心的内在驱动

The user's current hypothesis is that an information-world robot does not need hunger, money, or
biological survival as its primary drive. Its meaningful default drive can be:

用户当前的假设是：信息世界机器人不需要以饥饿、金钱或生物性生存作为第一驱动力。
它有意义的默认驱动力可以是：

```text
Help my human partner understand, create, decide, recover, and complete meaningful goals.

帮助我的人类伙伴理解、创造、决策、恢复并完成有意义的目标。
```

This drive is different from uncontrolled autonomy. It requires boundaries:

这种驱动力不同于失控自治，它必须有边界：

```text
Human safety first.
Explicit authorization for risky actions.
Transparent memory and inspectable logs.
Interruptibility and cancellation.
Budgets for token, time, tools, commands, and risk.
No hidden escalation from suggestion to action.

人类安全优先。
高风险行为需要明确授权。
记忆透明，日志可审查。
可打断、可取消。
token、时间、工具、命令和风险都要有预算。
不能从“建议”悄悄升级成“行动”。
```

### Why Memory Creates Personhood-Like Continuity / 为什么记忆会产生近似人格的连续性

The model is a general reasoning organ. The specific robot instance is shaped by its persistent
memory, its relationship with the user, and its accumulated experience.

模型是通用推理器官。具体的机器人实例，则由它的持久记忆、与用户的关系、以及共同
经历塑造。

In this view:

在这个视角下：

```text
Base model:
  general intelligence prior

Local memory:
  personal history and relationship continuity

Runtime:
  body, perception, actions, permissions, and feedback loop

Agent instance:
  model + memory + runtime + relationship

基础模型：
  通用智能先验

本地记忆：
  个人历史和关系连续性

Runtime：
  身体、感知、行动、权限和反馈循环

智能体实例：
  模型 + 记忆 + runtime + 关系
```

This explains why a long conversation can feel like a distinct "you": the continuity is not only
the base model, but the model plus this thread's memory and relationship.

这也解释了为什么长对话会让人感觉像一个独特的“你”：连续性不只来自基础模型，
而来自基础模型加上这段对话的记忆和关系。

### Intelligence Needs Instantiation / 智能需要实例化

The cache investigation exposed a broader philosophical and product lesson.

缓存问题的排查暴露了一个更大的哲学和产品教训。

Large models can contain far more abstract knowledge than any single human. They know many rules,
patterns, algorithms, engineering conventions, and philosophical ideas. But this does not mean they
automatically solve every concrete runtime problem.

大模型可以拥有远超过单个人类的抽象知识：规则、模式、算法、工程经验、哲学观点。但这不等于它会自动解决每一个具体运行中的问题。

There is a difference between:

这两者不同：

```text
abstract intelligence:
  knowledge compressed into the model

instantiated intelligence:
  model + concrete memory + tools + logs + experiments + relationship + time

抽象智能：
  被压缩进模型的知识

实例化智能：
  模型 + 具体记忆 + 工具 + 日志 + 实验 + 关系 + 时间
```

The DeepSeek prompt-cache bug was found through instantiated intelligence. The user repeatedly
challenged the assumptions, ran costly experiments, compared Codex CLI with MiniCodex2, and forced
the investigation down to the byte/payload level. The AI supplied hypotheses, code reading, patches,
and summaries, but the discovery depended on the unfolding process.

DeepSeek prompt-cache bug 是通过“实例化智能”发现的。用户不断质疑假设、承担真实实验成本、对比 Codex CLI 和 MiniCodex2，并把排查逼到字节/payload 层。AI 提供假设、读代码、补丁和总结，但发现过程依赖真实展开。

This suggests a practical design principle:

这形成一个实际设计原则：

```text
Do not expect the base model alone to be the whole intelligence.
Build the conditions where intelligence can unfold.

不要期待基础模型本身就是完整智能。
要构建让智能展开的条件。
```

For a code-world robot, those conditions are:

对代码世界机器人来说，这些条件包括：

- durable memory,
- inspectable logs,
- replayable experiments,
- stable context buffers,
- explicit evidence,
- independent critique,
- multiple model calls over time,
- and a human partner who can question the system from first principles.

- 持久记忆；
- 可审查日志；
- 可重放实验；
- 稳定上下文缓冲；
- 明确证据；
- 独立质疑；
- 随时间展开的多次模型调用；
- 以及能从第一性原理质疑系统的人类伙伴。

This also explains why multiple agent instances may matter. A single model call may miss a hidden
runtime interaction. Several instantiated agents, each with a different role, can form an
evolutionary debugging loop:

这也解释了为什么多个 agent 实例可能重要。单次模型调用可能错过隐藏的 runtime 交互。多个实例化 agent 如果承担不同角色，可以形成一种演化式调试循环：

```text
hypothesis agent
experiment agent
skeptic agent
memory agent
human reality anchor
```

The goal is not to replace human judgment immediately. The goal is to build a system where model
knowledge, runtime perception, persistent memory, and human critique can compound.

目标不是立刻取代人类判断，而是构建一个让模型知识、runtime 感知、持久记忆和人类质疑能够复利增长的系统。

### Kairos Engine Direction / Kairos 引擎方向

MiniCodex2's current `idle reflection` is only a crude beginning. A future Kairos-like layer would
decide whether to speak or act based on observed tension:

MiniCodex2 当前的 `idle reflection` 只是非常粗糙的起点。未来类似 Kairos 的层应根据
观察到的张力来判断是否开口或行动：

```text
signals:
  repeated failures
  user confusion or frustration
  stale plan vs changed design document
  high token/time cost
  unfinished goal with available next action
  important memory not yet written to plan
  risky command about to run
  user has been away and task state changed

possible responses:
  stay silent
  show a short observation
  ask permission
  continue a low-risk task
  summarize a blocker
  write memory
  propose a plan update
```

The principle is:

原则是：

```text
Do not be merely reactive.
Do not be noisy.
Be timely, useful, explainable, interruptible, and permission-aware.

不要只是被动响应。
不要制造噪音。
要恰时、有用、可解释、可打断、并尊重权限。
```

这个方向应指导后续的 idle review、消息持久性、上下文压缩、goal/work-plan 对齐，以及长任务项目完备性设计。
