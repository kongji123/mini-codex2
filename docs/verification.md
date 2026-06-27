# Verification Strategy

This document describes how MiniCodex2 chooses and runs verification.

本文档描述 MiniCodex2 如何选择并执行验证。

## Philosophy

`VerificationPlanBuilder` should combine conservative default strategies with model-driven project understanding.

`VerificationPlanBuilder` 应结合保守默认策略和模型驱动的项目理解。

The goal is not to hard-code every language and framework. The goal is to use high-confidence defaults when the project shape is obvious, and ask the model to read project scripts when it is not.

目标不是硬编码每种语言和框架的复杂逻辑，而是在项目形态明显时使用高置信默认策略，不明确时让模型读取项目脚本后决定。

Priority order:

优先级：

1. User-provided verification command.
2. Project-defined scripts such as `package.json`, `Makefile`, `pyproject.toml`, `Cargo.toml`, or `go.mod`.
3. Standard language defaults such as `cargo test`, `go test ./...`, or `python -m pytest`.
4. Simple smoke checks for tiny projects.
5. Model reads project files and chooses commands.

Configuration can override automatic verification:

配置可以覆盖自动验证：

```toml
[verification]
commands = [
  "python -m pytest",
  "npm run build"
]
```

Override priority:

覆盖优先级：

1. Current user instruction.
   当前用户指令。
2. CLI or Local API session override.
   CLI 或 Local API session 覆盖。
3. `minicodex2.toml`.
   `minicodex2.toml`。
4. Automatic strategy.
   自动 strategy。

## VerificationPlanBuilder Strategy Table

| Project Type | Signals | High-Confidence Default | Web or Background Strategy | Ambiguous Case |
|---|---|---|---|---|
| Python pytest | `pytest.ini`, `tests/`, pytest in `pyproject.toml` | `python -m pytest` | None | Ask model to read `pyproject.toml` or README |
| Python script | `*.py`, no test signal | Run entry file or syntax check | If `app.py` looks like web app, use web strategy | Ask model to read README |
| Flask | `app.py`, Flask import, Flask dependency | `start_background_command: python app.py`, HTTP smoke | Wait for port, default 5000 or parsed output | Ask model to read startup docs |
| FastAPI | FastAPI import or uvicorn dependency | `uvicorn app:app --host 127.0.0.1 --port <free>` | HTTP `/` or `/docs` smoke | Ask model to inspect entrypoint |
| Streamlit | Streamlit dependency or import | `streamlit run <file>` | HTTP smoke | Ask model to inspect README |
| Node | `package.json` | Prefer `npm test`, else `npm run build` | If dev/vite script exists, background start may be used for smoke | Ask model to read scripts |
| Vite | Vite script or `vite.config.*` | `npm run build` | `npm run dev -- --host 127.0.0.1 --port <free>` plus HTTP smoke | Ask model to read scripts |
| C + Makefile | `Makefile` | `make test`, else `make` | None | Ask model to read Makefile |
| C single file | `main.c` | `gcc main.c -o <temp_exe>` then run | None | If multi-file, ask model |
| C tests | `tests/*.c` | Use Makefile if present | None | Ask model to choose compile command |
| CMake | `CMakeLists.txt` | `cmake -S . -B build`, `cmake --build build`, `ctest --test-dir build` | None | Ask model to read CMake files |
| Rust | `Cargo.toml` | `cargo test` | Server projects require model decision | Ask model to read README |
| Go | `go.mod` | `go test ./...` | Server projects require model decision | Ask model to read README |
| Generic CLI | README, Makefile, entry file | Project script or entry smoke | None | Ask model to read README or scripts |
| Mixed project | Multiple signals | Choose plan based on changed files | Web only when service intent is clear | Ask model to inspect project |

## Language Strategy Pattern

Some language-specific logic is necessary, but it should live in small, testable strategy classes.

语言特殊逻辑是必要的，但应该放在小而可测试的 strategy class 中。

Suggested strategies:

建议策略：

```text
PythonVerificationStrategy
CVerificationStrategy
NodeVerificationStrategy
RustVerificationStrategy
GoVerificationStrategy
CMakeVerificationStrategy
WebVerificationStrategy
```

Each strategy should:

每个 strategy 应该：

- Match against `ProjectProfile` and changed files.
  根据 `ProjectProfile` 和变更文件判断是否匹配。
- Return a confidence score.
  返回置信度。
- Build a unified `VerificationPlan`.
  构建统一的 `VerificationPlan`。
- Avoid executing commands directly.
  不直接执行命令。

The runner remains language-agnostic.

Runner 保持语言无关。

## Fixture Priority

Fixture development should start with Python and C.

Fixture 开发应先从 Python 和 C 开始。

Phase 1 fixtures:

第一批 fixtures：

```text
python_pytest
c_main
c_compile_error
```

Reason:

原因：

- Python validates interpreted-language testing through `python -m pytest`.
  Python 验证解释型语言的测试路径，即 `python -m pytest`。
- C validates compiled-language flow through compile and run steps.
  C 验证编译型语言的编译和运行路径。
- C compile errors validate compile failure packaging.
  C 编译错误验证 compile failure 打包。

Phase 2 fixtures:

第二批 fixtures：

```text
node_package
go_project
rust_project
cmake_project
```

Phase 3 fixtures:

第三批 fixtures：

```text
python_flask
python_fastapi
node_vite
```

This sequence verifies the abstraction before expanding language coverage.

这个顺序先验证抽象是否站得住，再扩展语言覆盖。

## Verification Plan Shape

A `VerificationPlan` should contain:

`VerificationPlan` 应包含：

- Project profile summary.
- Ordered steps.
- Timeout per step.
- Whether failure stops execution.
- Whether a step starts a background service.
- Optional HTTP smoke checks.
- Reason explaining why the plan was chosen.

Example:

```text
VerificationPlan
  reason: package.json has test script
  steps:
    - type: command
      command: npm test
      timeout_seconds: 60
      stop_on_failure: true
```

## Verification Runner Behavior

`VerificationRunner` should:

`VerificationRunner` 应该：

1. Execute steps in order.
2. Stop on the first blocking failure.
3. Capture stdout and stderr.
4. Truncate oversized output.
5. Preserve command, exit code, duration, and failure type.
6. Return a structured `VerificationResult`.

## Blocking Failure Policy

MiniCodex2 should not treat every failure as a whole-session stop, and it should not ignore relevant failures.

MiniCodex2 不应把每个失败都当成整个 session 停止，也不应忽略相关失败。

The runtime should classify failures by scope:

Runtime 应按作用域分类 failure：

```text
global:
  The environment or permission state prevents meaningful progress.
  Example: no Python executable, workspace path denied, model credentials missing.

task_critical:
  The failure blocks the user's main requested outcome.
  Example: Flask app cannot start for a Flask task.

local:
  The failure blocks one module or subtask, but independent work may continue.
  Example: one optional integration test fails while unrelated CLI work can proceed.

optional:
  The failure affects a non-required check or nice-to-have validation.
  Example: optional lint warning when tests and requested behavior pass.
```

```text
global:
  环境或权限状态导致无法有意义地继续。
  例如：没有 Python，可访问路径被拒绝，缺少模型凭据。

task_critical:
  失败阻塞用户请求的主要结果。
  例如：用户要求 Flask 项目，但 Flask app 无法启动。

local:
  失败阻塞某个模块或子任务，但独立工作仍可继续。
  例如：某个可选集成测试失败，但无关 CLI 功能仍可继续开发。

optional:
  失败影响非必要检查或锦上添花的验证。
  例如：行为和测试通过，但存在可选 lint warning。
```

## No Greenwashing

Passing unrelated tests must not hide a relevant failure.

不能用无关通过测试掩盖相关失败。

If the user asks for a Flask app, startup and HTTP smoke are relevant. A passing unit test alone is not enough.

如果用户要求 Flask app，启动和 HTTP smoke 是相关验证。仅单元测试通过是不够的。

If the user asks for a C CLI tool, compile and executable run are relevant. File existence alone is not enough.

如果用户要求 C CLI 工具，编译和可执行文件运行是相关验证。仅文件存在是不够的。

## Continue or Stop Policy

Suggested behavior:

建议行为：

```text
global blocker:
  Stop and report blocked.

task-critical blocker:
  Repair within max_repair_rounds.
  If unresolved, report blocked for the requested outcome.
  Continue only if there are clearly independent tasks.

local blocker:
  Record BlockedItem.
  Continue independent tasks when safe.
  Final result is partial, not full success.

optional failure:
  Continue.
  Report as known risk or follow-up.
```

Important rule:

重要规则：

The final answer must distinguish `passed`, `partial`, and `blocked`.

最终交付必须区分 `passed`、`partial` 和 `blocked`。

## Dependency Failure Policy

Missing dependencies should be treated as real blocking failures when they affect relevant verification.

当缺失依赖影响相关验证时，应将其视为真实 blocking failure。

Example:

示例：

```text
Flask startup
-> ModuleNotFoundError: flask
-> FailurePack: missing_dependency flask
-> If requirements.txt or pyproject.toml declares Flask, install project dependencies once when policy allows.
-> Retry startup and HTTP smoke.
-> If install fails, report blocked with install failure.
```

In `auto` mode, project-declared dependency installation may be attempted once when it is necessary for relevant verification.

在 `auto` 模式下，如果相关验证需要，并且依赖已在项目文件中声明，可以尝试一次项目依赖安装。

The agent should not silently install global dependencies.

Agent 不应静默安装全局依赖。

## FailurePack

`FailurePack` should extract only the useful blocking failure for model repair.

`FailurePack` 应只提取对模型修复有用的 blocking failure。

Suggested fields:

```text
failure_type
command
exit_code
first_blocking_failure
relevant_output
suspected_files
suggested_next_action
```

`FailurePack` should also expose a stable failure signature for early-stop logic.

`FailurePack` 还应暴露稳定的 failure signature，用于提前停止逻辑。

The signature can be based on:

signature 可基于：

- Failure type.
- 失败类型。
- Command.
- 命令。
- Exit code.
- 退出码。
- First meaningful error line.
- 第一条有意义的错误行。
- Test case name when available.
- 可用时的测试用例名。

Default repair policy:

默认修复策略：

- `max_repair_rounds = 3`.
- `max_repair_rounds = 3`。
- Every repair must be followed by verification.
- 每次修复后必须重新验证。
- If the same failure signature appears twice in a row, the session can stop early as blocked.
- 如果相同 failure signature 连续出现两次，Session 可以提前 blocked。
- Environment failures, permission denials, and ambiguous verification commands can become immediate blocked results.
- 环境失败、权限拒绝和不明确验证命令可以直接成为 blocked 结果。

## Web Verification

For web projects:

对于 Web 项目：

1. Use `start_background_command`, not `run_command`.
2. Pick a free local port when possible.
3. Wait for port readiness.
4. Store logs in a session log path.
5. Run HTTP smoke against `/`, `/health`, or framework-specific endpoints when known.
6. Return pid, port, log path, and ready state.

## Command Safety Requirements

`run_command`:

`run_command`：

- Only for commands expected to exit.
- Short default timeout.
- Detect likely interactive commands.
- Detect obvious long-running service commands.
- Return a structured failure asking the model to use `start_background_command` when appropriate.

`start_background_command`:

`start_background_command`：

- For long-running servers.
- Does not block the main loop.
- Waits for readiness.
- Returns pid, log path, port, and ready status.
