# Failure Handling

This document defines how MiniCodex2 balances root-cause repair with continued progress.

本文档定义 MiniCodex2 如何平衡根因修复和继续推进。

## Core Problem

A code agent should not stop too early, but it also must not pretend unrelated passing tests mean the task is done.

Code Agent 不应过早停止，但也不能假装无关测试通过就代表任务完成。

The correct behavior is scoped failure handling.

正确行为是按作用域处理 failure。

## Principles

### Relevant Failure First

The most relevant blocking failure should be addressed first.

应优先处理最相关的 blocking failure。

### No Greenwashing

Do not use unrelated passing tests to hide a relevant failing integration path.

不要用无关通过测试掩盖相关失败的集成路径。

### Scoped Blocking

Failures should be classified by impact.

failure 应按影响范围分类。

```text
global
task_critical
local
optional
```

### Continue Independent Work

When a blocker is local and independent work remains, the agent may continue.

当 blocker 是局部的，并且仍有独立工作可做时，Agent 可以继续。

The final result must be marked `partial`, not `passed`.

最终结果必须标记为 `partial`，而不是 `passed`。

## Result States

```text
passed:
  Relevant verification passed.

partial:
  Some work completed, but one or more local blockers remain.

blocked:
  The main requested outcome cannot be completed without user input, permission, dependency, or environment change.
```

```text
passed:
  相关验证通过。

partial:
  部分工作完成，但仍有一个或多个 local blockers。

blocked:
  主要请求结果无法在没有用户输入、权限、依赖或环境变化的情况下完成。
```

## BlockedItem

When continuing after a local blocker, the runtime should record a `BlockedItem`.

在 local blocker 后继续推进时，Runtime 应记录 `BlockedItem`。

Suggested fields:

建议字段：

```text
id
scope
summary
failure_pack
affected_files
affected_verification
can_continue_independent_work
```

## Tests

Required tests:

必需测试：

- `test_task_critical_failure_is_not_hidden_by_unrelated_pass`
- `test_global_blocker_stops_turn`
- `test_local_blocker_allows_independent_work`
- `test_partial_result_reports_blocked_items`
- `test_missing_dependency_becomes_failure_pack`
- `test_project_declared_dependency_install_attempted_once_when_allowed`
