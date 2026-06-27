# MiniCodex2

[English](README.md) | [中文](README.zh-CN.md)

MiniCodex2 is a learning-driven experimental coding agent and the current prototype of the CodexRobot vision.

CodexRobot is the long-term idea: a digital robot for the information world. MiniCodex2 starts from software development because codebases, terminals, files, browsers, APIs, documents, and developer workflows are today's most accessible environment for AI agents.

This project studies and inherits ideas from leading coding agents and open-source agent systems, while exploring our own hypotheses about memory, tools, skills, plugins, permissions, verification, prompt-cache-safe context, and self-evolving capabilities.

This project is independent and is not affiliated with, endorsed by, or sponsored by OpenAI or the Codex project.

## Current Focus

- Local-first coding-agent runtime
- Agent runtime boundaries for context, tools, safety, logs, persistence, and permissions
- Skill and plugin boundaries for domain workflows and extensible capabilities
- Prompt-cache-safe context assembly
- Work planning, evidence, verification, and failure feedback loops
- Early experiments around agents that can turn repeated workflows into reusable tools or skills

## Architecture Direction

MiniCodex2 separates responsibilities deliberately:

- **Runtime / Agent OS layer**: context assembly, tool execution, permissions, logs, persistence, events, memory, cache discipline, failure feedback, and background process control.
- **Skills**: workflow guidance and domain operating manuals.
- **Plugins**: trusted bundles that can provide tools, hooks, skills, and integrations.
- **Tools**: structured executable capabilities exposed to the model.
- **Model**: semantic judgment, project understanding, next-step decisions, failure attribution, and whether to update plans or memory.

Agent OS is not the final product goal. It is the reusable runtime extracted while building CodexRobot and future agent projects.

## Installation

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,cli,tui,api]"
```

## Basic Checks

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m compileall src tests
```

## Run Scripts

Windows convenience scripts are included for local experiments:

```powershell
.\run_tui.bat
.\run_tui_gpt4mini.bat
.\run_tui_gpt55.bat
.\run_tui_deepseek.bat
.\run_tui_aihub2api_openai.bat
.\run_benchmark_fake.bat
.\run_benchmark_real.bat
```

The scripts may prompt for API keys or read keys from local environment variables. Files such as `*.local.bat`, `.env`, logs, and session data are intentionally ignored.

## Configuration

Copy the example configuration and add your local provider settings:

```powershell
Copy-Item minicodex2.example.toml minicodex2.toml
```

Do not commit local API keys, `.local` scripts, logs, or session data.

## Documentation

- [Architecture](docs/architecture.md)
- [Context and Cache Discipline](docs/context.md)
- [Agent Runtime Boundaries](docs/agent-runtime-boundaries.md)
- [Agent OS Outlook](docs/agent-os-outlook.md)
- [Code World Robot](docs/code-world-robot.md)
- [Skill and Plugin Boundary](docs/agentos-skill-boundary.md)
- [Verification](docs/verification.md)
- [Failure Handling](docs/failure-handling.md)
- [Configuration](docs/configuration.md)
- [Events](docs/events.md)
- [Popular Science: Code Agent](docs/wechat-code-agent-popular-science.md)
- [Agent Development Guide](AGENTS.md)

Some documents are design notes from an active experiment. They may describe planned or partial behavior as well as implemented behavior.
