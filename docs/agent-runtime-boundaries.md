# Agent Runtime Boundaries / Agent Runtime 边界

This document records the current MiniCodex2 understanding of what belongs in the model, what belongs in Agent Runtime, and what remains missing.

本文记录 MiniCodex2 当前对模型、Agent Runtime 和后续缺口的职责边界理解。

## Core Principle / 核心原则

Agent Runtime should not replace model intelligence. It should provide the model with reliable perception, action, memory, evidence, permissions, and recovery.

Agent Runtime 不应替代模型智能。它应该向模型提供可靠的感知、行动、记忆、证据、权限和恢复机制。

```text
Model:
  Understands requirements.
  Plans work.
  Chooses tools.
  Reasons about root causes.
  Decides whether evidence satisfies the user's goal.

Runtime:
  Collects facts.
  Executes tools.
  Stores durable state.
  Validates permissions and paths.
  Records evidence.
  Feeds structured context back to the model.
  Prevents fake completion and infinite repeated failure.
```

```text
模型：
  理解需求。
  规划工作。
  选择工具。
  分析根因。
  判断证据是否满足用户目标。

Runtime：
  收集事实。
  执行工具。
  存储持久状态。
  校验权限和路径。
  记录证据。
  将结构化上下文回灌给模型。
  防止虚假完成和无限重复失败。
```

The guiding rule:

指导规则：

```text
Do not give Runtime a fake brain.
Give the model reliable body, memory, tools, evidence, and discipline.
```

```text
不要给 Runtime 写一个假的大脑。
要给模型提供可靠的身体、记忆、工具、证据和纪律。
```

## Model Capability Versus Runtime Responsibility / 模型能力与 Runtime 职责

MiniCodex2 should evaluate model capability and runtime design separately.

MiniCodex2 应分别评估模型能力和 Runtime 设计。

Runtime should provide the same loop, tools, evidence, goal memory, and recovery discipline to all models. It should not add narrow project-specific code only because a weaker model failed to infer a root cause.

Runtime 应向所有模型提供同一套循环、工具、证据、目标记忆和恢复纪律。不应因为较弱模型没推理出根因，就添加狭窄的项目特判代码。

Use stronger models to test the upper bound of the runtime:

使用强模型测试 Runtime 能力上限：

- If a strong model also fails at the same point, investigate missing runtime support: tool, context, evidence, permission, verification, or loop control.
- If a strong model succeeds but a weaker model fails, treat the gap primarily as model reasoning or long-task capability unless multiple models expose the same generic runtime weakness.
- Improvements for weak models should normally be prompt/tool-schema/context/evidence improvements, not hard-coded domain guesses.

- 如果强模型也在同一点失败，优先检查 Runtime 是否缺工具、上下文、证据、权限、验证或循环控制。
- 如果强模型成功而弱模型失败，优先归因为模型推理或长任务能力差异，除非多个模型暴露同一个通用 Runtime 缺口。
- 面向弱模型的改进通常应落在 prompt、tool schema、context、evidence 上，而不是写死领域猜测。

This keeps the Agent OS generic while still making model differences measurable.

这样可以保持 Agent OS 通用，同时让模型差异可测量。

## Current Implemented Capabilities / 当前已实现能力

### Perception / 感知

- `list_directory`
- `read_file`
- `find_images`
- `view_image`
- `inspect_port`
- `http_request`
- `ProjectGuidanceLoader`
- `ProjectMemoryIndex`

These let the model inspect local files, images, ports, HTTP entrypoints, `AGENTS.md`, and available project documents.

这些能力让模型可以查看本地文件、图片、端口、HTTP 入口、`AGENTS.md` 和可用项目文档。

### Action / 行动

- `write_file`
- `edit_file`
- `delete_file`
- `run_command`
- `run_python`
- `start_background_command`
- `release_port`
- `inspect_toolchain`
- `install_toolchain`

These are the model's operational tools. Runtime enforces path safety, permission gates, command timeout behavior, and background process handling.

这些是模型的行动工具。Runtime 负责路径安全、权限 gate、命令超时和后台进程管理。

### Memory / 记忆

- `ChatHistory`
- `HistoryStore`
- `GoalController`
- `RootObjective`
- `WorkPlan`
- `CurrentStep`
- `ProjectGuidance`
- `ProjectMemoryIndex`

Chat history is a record, not a stable plan. Durable objectives and execution steps live in structured Goal/WorkPlan state.

聊天历史是记录，不是稳定计划。持久目标和执行步骤应该存储在结构化的 Goal/WorkPlan 状态中。

### Context Bus / 上下文总线

`ContextManager` currently builds model context with these sections:

`ContextManager` 当前用这些分区构建模型上下文：

```text
[RUNTIME PROTOCOL]
[PROJECT GUIDANCE]
[WORK MEMORY]
[PROJECT MEMORY INDEX]
[EXECUTION STATE]
Compressed older context
```

This is the main bus between Runtime and model. If information does not enter this bus or the tool schema, the model cannot reliably use it.

这是 Runtime 和模型之间的主要总线。如果信息没有进入这个总线或工具 schema，模型就不能稳定使用它。

### Verification And Repair / 验证与修复

- Code changes trigger verification.
- Verification failures create `FailurePack`.
- Blocking failure is fed back to the model.
- Repair rounds are bounded.
- Repeated failure can block the turn.
- Long-running commands are redirected to background execution.

当前已经实现：

- 代码修改会触发验证。
- 验证失败会生成 `FailurePack`。
- blocking failure 会回灌给模型。
- 修复轮次有上限。
- 重复失败会阻塞当前回合。
- 长驻命令会被引导到后台执行。

### Project Guidance And Plan Sync / 项目规则与计划同步

- `AGENTS.md` is loaded as durable project guidance.
- `README.md`, `AGENTS.md`, and `docs/*.md` are indexed as project memory.
- `sync_work_plan_from_document` can convert a planning document into runtime `WorkPlan`.

当前已经实现：

- `AGENTS.md` 会作为项目长期规则加载。
- `README.md`、`AGENTS.md` 和 `docs/*.md` 会进入项目记忆索引。
- `sync_work_plan_from_document` 可以把计划文档同步成运行时 `WorkPlan`。

## Implemented Generic Logic / 已实现通用逻辑

### EvidenceStore

Runtime now stores structured evidence records.

Runtime 现在会存储结构化证据记录。

```text
EvidenceRecord:
  id
  kind
  source_tool
  summary
  detail
  command/url/path
  status/exit_code
  created_at
```

Evidence can be created from verification runs and verification-like tool calls.

证据可以从验证执行和验证类工具调用中创建。

### Step Evidence Gate

`update_work_step(status="done")` now requires known evidence.

`update_work_step(status="done")` 现在要求已知证据。

Allowed evidence:

允许的证据：

- `evidence_ids` that exist in `EvidenceStore`
- exact existing acceptance evidence
- existing evidence already attached to the step from restored state

### Goal Completion Gate

`update_goal(status="complete")` now checks the WorkPlan.

`update_goal(status="complete")` 现在会检查 WorkPlan。

Completion requires:

完成要求：

- acceptance evidence exists
- all required WorkPlan steps are `done`
- required done steps have evidence

### WorkPlan Metadata

`WorkPlanItem` now supports:

`WorkPlanItem` 现在支持：

```text
verification_hint
required
evidence_ids
source_document
```

## Remaining Missing Logic / 剩余缺失逻辑

### 1. Broader Evidence Sources

Runtime should create evidence records from more successful verification-like tool results:

Runtime 应从更多成功的验证类工具结果中自动创建证据：

Runtime 应该从成功的验证类工具结果中自动创建证据：

- `run_command` with purpose `test`, `build`, `verify`, `smoke`, `check`, or `lint`
- `http_request` with successful status
- `VerificationRunner` pass
- background service ready where relevant

### 2. Evidence Quality

Runtime currently verifies that evidence exists. Future versions should also classify evidence strength.

Runtime 当前只校验证据存在。后续可以进一步区分证据强度。

Examples:

示例：

```text
weak: service started
medium: build passed
strong: targeted test or HTTP smoke passed
```

### 3. Evidence UI

The TUI should show evidence records and their linked WorkPlan steps.

TUI 应显示证据记录及其绑定的 WorkPlan step。

### 4. Project Guidance Scope

Current guidance loading finds `AGENTS.md`, but scope rules are still simple.

当前 guidance 加载能找到 `AGENTS.md`，但作用域规则还比较简单。

Future behavior should support:

未来应支持：

- root `AGENTS.md`
- nested `AGENTS.md`
- closest-scope precedence
- reload after file changes
- truncation or summarization for very large guidance files

### 5. Context Budget Discipline

Context compression exists, but it is still heuristic.

上下文压缩已经存在，但仍是启发式。

Future behavior should preserve, in priority order:

后续压缩应按优先级保留：

1. Runtime protocol
2. Project guidance
3. Active goal and current step
4. Current failure and evidence
5. Recent user request
6. Relevant document index
7. Older chat summary

### 6. Richer UI Evidence Display

The TUI currently shows events and status, but should eventually show:

TUI 当前能显示事件和状态，但后续应显示：

- active goal
- current step
- work plan status
- evidence attached to each step
- blockers
- verification history

This makes the agent's reasoning support visible to the user without exposing raw internal noise.

这样可以让用户看到 Agent 的执行支撑状态，而不是只看到原始内部噪声。

### 7. Benchmarks For One-Turn Completeness

The benchmark suite should test both small-turn completion and plan-step completion.

Benchmark 应同时测试小回合完备性和计划步骤完备性。

Needed benchmark categories:

需要的 benchmark 类别：

- small code repair with verification
- missing dependency diagnosis
- tool failure recovery
- document-to-plan sync
- step evidence requirement
- goal completion rejection without evidence
- multi-step project continuation
- real-model smoke tests

## Three-Level Completion Model / 三层完成模型

MiniCodex2 should treat completion as a hierarchy:

MiniCodex2 应把完成视为层级结构：

```text
Turn completion:
  The current user turn reached a concrete answer, verification pass, or blocked state.

Step completion:
  The current WorkPlan step has implementation progress and concrete evidence.

Goal completion:
  The overall WorkPlan is complete, required steps have evidence, and acceptance evidence exists.
```

```text
回合完成：
  当前用户回合达到明确回答、验证通过或 blocked 状态。

步骤完成：
  当前 WorkPlan step 有实现进展，并绑定了具体证据。

目标完成：
  整体 WorkPlan 完成，必要步骤都有证据，并存在验收证据。
```

This is the generalized version of "after code changes, run tests".

这是“改代码后必须测试”的泛化版本。

```text
Small change:
  edit -> verify -> repair -> pass

Plan step:
  implement -> verify -> attach evidence -> mark done

Goal:
  all required steps done with evidence -> mark complete
```

```text
小修改：
  编辑 -> 验证 -> 修复 -> 通过

计划步骤：
  实现 -> 验证 -> 绑定证据 -> 标记完成

目标：
  所有必要步骤都有证据地完成 -> 标记整体完成
```

## Design Boundary Test / 设计边界判断

When adding new logic, use this test:

新增逻辑时，用这个判断：

```text
Is this fact collection, state storage, permission safety, evidence verification, or failure recovery?
  -> Runtime

Is this semantic understanding, root-cause reasoning, task decomposition, or business acceptance judgment?
  -> Model

Is this a reusable domain workflow?
  -> Skill

Is this a durable repo convention?
  -> AGENTS.md / ProjectGuidance

Is this visible process feedback?
  -> Events / UI
```

```text
这是事实采集、状态存储、权限安全、证据校验或失败恢复吗？
  -> Runtime

这是语义理解、根因推理、任务拆解或业务验收判断吗？
  -> Model

这是可复用领域流程吗？
  -> Skill

这是持久仓库约定吗？
  -> AGENTS.md / ProjectGuidance

这是用户可见过程反馈吗？
  -> Events / UI
```

This boundary is not final. It should evolve through real benchmarks and project failures.

这个边界不是最终答案。它应通过真实 benchmark 和项目失败案例继续演化。

## Hard-Won Boundary Lesson / 关键边界教训

This project exposed an important failure mode in agent development: when the model misses a root cause, it is tempting to compensate by adding more runtime special cases. That feels productive in the short term, but it can quietly turn the Agent OS into a pile of fragile project-specific heuristics.

这个项目暴露了一个重要问题：当模型没有找出根因时，很容易想用 runtime 特殊逻辑补上。短期看好像有效，但长期会把 Agent OS 写成一堆脆弱的项目特例。

Wrong direction:

错误方向：

- If the model misses a frontend/backend integration issue, do not hard-code a frontend/backend repair path into the Agent OS.
- If the model misses a project root or entry point, do not solve it by hard-coding framework-specific directory guessing as semantic truth.
- If the model fails to derive a WorkPlan from a PRD, do not solve it with natural-language keyword tables inside runtime code.
- If a weaker model fails where a stronger model succeeds, do not immediately compensate with domain-specific branches in core runtime.

- 如果模型没发现前后端联调问题，不要把前后端修复流程硬编码进 Agent OS。
- 如果模型没找到项目根目录或入口，不要把框架目录猜测写成 runtime 的语义真理。
- 如果模型不能从 PRD 推导 WorkPlan，不要用自然语言关键词表塞进 runtime 解析器来假装理解。
- 如果弱模型失败而强模型成功，不要马上用领域特化分支去弥补核心 runtime。

Correct direction:

正确方向：

- Runtime should provide better facts: directory trees, file metadata, changed files, read excerpts, command output, HTTP observations, screenshots, logs, process state, and verification evidence.
- Runtime should provide better tools: bounded recursive listing, targeted reads, command execution, background services, HTTP checks, browser/image inspection, and safe write/delete operations.
- Runtime should provide better memory: RootObjective, WorkPlan, CurrentStep, evidence records, runtime facts, project guidance, compacted history, and recoverable full history.
- Runtime should provide better discipline through prompts and schemas: verify after changes, align verification with changed files, avoid unrelated checks as acceptance evidence, and continue active goals unless truly complete or blocked.
- The model should keep semantic ownership: project type, relevant files, root cause, next action, acceptance judgment, and whether evidence satisfies the user's goal.

- Runtime 应提供更好的事实：目录树、文件元数据、变更文件、读取片段、命令输出、HTTP 观察、截图、日志、进程状态和验证证据。
- Runtime 应提供更好的工具：有界递归目录、定向读文件、命令执行、后台服务、HTTP 检查、浏览器/图片检查、安全写入和删除。
- Runtime 应提供更好的记忆：RootObjective、WorkPlan、CurrentStep、证据记录、runtime facts、项目指导、压缩历史和可追溯完整历史。
- Runtime 应通过 prompt 和 schema 提供更好的工程纪律：修改后验证、验证要对齐变更文件、不能把无关检查当验收证据、除非真正完成或 blocked 否则继续推进 active goal。
- 模型应保留语义判断权：项目类型、相关文件、根因、下一步动作、验收判断，以及证据是否满足用户目标。

Short rule:

简短规则：

```text
Runtime does not provide intelligence.
Runtime provides perception, action, memory, evidence, constraints, and feedback.
Prompt provides engineering discipline.
Model provides semantic judgment.
```

```text
Runtime 不提供智能。
Runtime 提供感知、行动、记忆、证据、约束和反馈。
Prompt 提供工程纪律。
Model 提供语义判断。
```

This lesson came from real project failures, user critique, and comparison with Codex's design style. It should be treated as a development principle for MiniCodex2: prefer general sensing, execution, memory, and feedback mechanisms over special-case semantic runtime code.

这条教训来自真实项目失败、用户质疑，以及对 Codex 设计方式的对照。它应作为 MiniCodex2 的开发原则：优先建设通用的感知、执行、记忆和反馈机制，而不是把语义判断写成特殊 runtime 代码。
