# Configuration / 配置

This document defines MiniCodex2 configuration rules.

本文档定义 MiniCodex2 的配置规则。

## Configuration File / 配置文件

MiniCodex2 supports a project-local `minicodex2.toml`.

MiniCodex2 支持项目本地的 `minicodex2.toml`。

Configuration should remove repeated project preferences from prompts and command-line flags.

配置文件用于保存重复使用的项目偏好，避免每次都通过 prompt 或命令行参数指定。

## Precedence / 优先级

Configuration is resolved in this order:

配置按以下顺序解析：

1. Built-in defaults.
2. `minicodex2.toml`.
3. Environment variables.
4. CLI arguments.
5. Local API session overrides.
6. Explicit user instruction in the current turn.

Higher priority values override lower priority values.

高优先级覆盖低优先级。

## Model Profiles / 模型配置组

MiniCodex2 treats a model profile as one complete runnable endpoint.

MiniCodex2 把 `model profile` 视为一套完整可运行的模型端点配置。

This is preferred over splitting `provider`, `model`, `base_url`, and `api_key` across several sections, because local agent development often compares many model/key/proxy combinations.

相比把 `provider`、`model`、`base_url`、`api_key` 拆到多个 section，profile 更适合本地 Agent 开发，因为我们经常要比较不同模型、不同 key、不同代理服务。

```toml
default_model_profile = "deepseek_flash"

[model_profiles.openai_gpt41]
name = "OpenAI GPT-4.1 mini"
provider = "openai"
model = "gpt-4.1-mini"
base_url = "https://api.openai.com/v1"
wire_api = "chat"
api_key_env = "OPENAI_API_KEY"

[model_profiles.deepseek_flash]
name = "DeepSeek V4 Flash"
provider = "deepseek"
model = "deepseek-v4-flash"
base_url = "https://api.deepseek.com"
wire_api = "chat"
api_key_env = "DEEPSEEK_API_KEY"

[model_profiles.local_responses_proxy]
name = "Local Responses Proxy"
provider = "openai-compatible"
model = "deepseek-v4-pro"
base_url = "http://127.0.0.1:5005/v1"
wire_api = "responses"
api_key_env = "CODEX_PROXY_API_KEY"
reasoning_effort = "medium"
timeout_seconds = 120
```

Select a profile with CLI:

使用 CLI 选择 profile：

```powershell
minicodex tui --model-profile deepseek_flash
minicodex tui --model-profile openai_gpt41
```

Or with an environment variable:

或使用环境变量：

```bat
set MINICODEX2_MODEL_PROFILE=deepseek_flash
run_tui.bat
```

`wire_api` currently distinguishes endpoint protocols:

`wire_api` 用于区分端点协议：

- `chat`: OpenAI-compatible `/chat/completions`.
- `responses`: OpenAI Responses-style `/responses` endpoint. Profiles can declare this now; full Responses adapter behavior should be implemented separately before using it with MiniCodex2 tool loops.

## Legacy Model Section / 旧模型配置

The legacy `[model]` section is still supported as defaults and for backward compatibility:

旧版 `[model]` 仍然支持，作为默认值和兼容入口：

```toml
[model]
provider = "openai_compatible"
base_url = "https://api.openai.com/v1"
model = "default"
wire_api = "chat"
api_key_env = "OPENAI_API_KEY"
```

If `default_model_profile` or `--model-profile` is set, that selected profile overrides `[model]`. CLI arguments such as `--model`, `--base-url`, `--wire-api`, and `--api-key` override both.

如果设置了 `default_model_profile` 或 `--model-profile`，选中的 profile 会覆盖 `[model]`。`--model`、`--base-url`、`--wire-api`、`--api-key` 等 CLI 参数会覆盖它们。

## Sensitive Credentials / 敏感凭据

API keys should normally come from environment variables or local-only files.

API key 通常应来自环境变量或本地私有文件。

Recommended:

推荐：

```toml
[model_profiles.deepseek_flash]
model = "deepseek-v4-flash"
base_url = "https://api.deepseek.com"
wire_api = "chat"
api_key_env = "DEEPSEEK_API_KEY"
```

Avoid committing:

避免提交：

- Raw API keys.
- Local `run_*.bat` files that embed keys, unless intentionally ignored or local-only.
- Logs or history containing secrets.

## Other Settings / 其他配置

```toml
[permissions]
mode = "auto"

[context]
trigger_auto_compact_limit_tokens = 50000
post_compact_ratio = 0.60
baseline_ratio = 0.25
model_context_limit_tokens = 128000

[repair]
max_rounds = 3

[agent]
max_verified_steps_per_turn = 8
idle_tick_enabled = true
idle_tick_interval_seconds = 20
idle_tick_max = 2
memory_extraction_enabled = false

[timeouts]
run_command_seconds = 30
verification_command_seconds = 60
background_ready_seconds = 15
full_turn_seconds = 300

[verification]
commands = []
prefer_project_scripts = true
allow_dependency_install = false

[web_search]
enabled = false
provider = "duckduckgo_html"
api_key_env = "BRAVE_SEARCH_API_KEY"
timeout_seconds = 15
max_results = 8
cache_ttl_seconds = 86400

[skills]
code_guidance_mode = "legacy"
active_code_skill = "legacy-builtin-code"
external_dirs = []
allow_third_party_primary = false

[paths]
projects_root = ""
artifact_dir = ".minicodex2"
ignore = [".git", "node_modules", ".venv", "dist", "build", ".minicodex2"]

[ui]
locale = "zh-CN"
```

Legacy context aliases are still accepted:

旧上下文别名仍然支持：

- `request_soft_limit_tokens`
- `budget_tokens`
- `compression_threshold`

## Skills / Skill 系统

`skills` controls workflow guidance packages. Agent OS hard policy is not a
skill and is never disabled by these settings.

`skills` 用来控制工作流指导包。Agent OS 的硬约束不属于 skill，因此不会被这些配置关闭。

- `code_guidance_mode = "external"` is the default: `active_code_skill` is the primary code workflow.
- `code_guidance_mode = "overlay"` keeps legacy active while exposing external skills as references.
- `code_guidance_mode = "legacy"` keeps the old built-in code workflow active for compatibility.
- `external_dirs` can point to local directories that contain `SKILL.md` and optional `skill.toml`.
- `allow_third_party_primary = false` is the safe default for future install flows.

Code Skill migration is intentionally incremental. Runtime still owns safety,
execution, logging, context buffering, memory, failure feedback, and persistence.
The bundled `minicodex-code` skill is model-facing workflow guidance: it tells the
model how to approach code tasks, when to load deeper references, and when to use
work-plan / memory tools. See `docs/agentos-skill-boundary.md`.

Code Skill 的拆分是渐进式的。Runtime 仍然负责安全、执行、日志、上下文缓存、
记忆、失败回灌和持久化。内置的 `minicodex-code` 是给模型看的代码工作流指导：
它告诉模型如何推进代码任务、什么时候加载更细参考、什么时候使用工作计划和记忆工具。
详见 `docs/agentos-skill-boundary.md`。

## Web Tools / Web 工具

`web_search` and `fetch_web_page` are public-web tools. They are separate from
local service validation: use `http_request` or `browser_test` for localhost,
127.0.0.1, private IPs, and project dev servers.

`web_search` 和 `fetch_web_page` 是公共网页工具，不替代本地服务联调。
localhost、127.0.0.1、私有 IP、项目开发服务器应继续使用 `http_request`
或 `browser_test`。

The first implementation supports:

第一版支持：

- `provider = "duckduckgo_html"` as a no-key best-effort search provider.
- `provider = "brave"` with `BRAVE_SEARCH_API_KEY` as an optional paid/stable API provider.
- `provider = "mock"` for offline tests.
- Search/page cache under `.minicodex2/web_cache`.
- Private/local URL blocking in `fetch_web_page`.
