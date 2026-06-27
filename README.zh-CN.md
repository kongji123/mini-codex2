# MiniCodex2

[English](README.md) | [中文](README.zh-CN.md)

MiniCodex2 是一个学习和实验性质的代码 Agent，也是 CodexRobot 愿景当前阶段的原型。

CodexRobot 是长期目标：一个能够在信息世界中工作的数字机器人。MiniCodex2 从软件开发场景开始，是因为代码库、终端、文件系统、浏览器、API、文档和开发工作流，是今天 AI Agent 最容易进入和行动的信息世界环境。

这个项目学习并继承优秀代码 Agent、开源 Agent 系统和商业 Agent 产品中的思想，同时探索我们自己关于记忆、工具、技能、插件、权限、验证、prompt cache 友好上下文，以及能力自我进化的设想。

本项目是独立项目，与 OpenAI 或 Codex 项目没有官方关联，也未获得其认可或赞助。

## 当前重点

- 本地优先的代码 Agent runtime
- 面向上下文、工具、安全、日志、持久化和权限的 Agent runtime 边界
- 面向领域工作流和可扩展能力的 Skill / Plugin 边界
- 对 prompt cache 友好的上下文组装
- 工作计划、证据、验证和失败回灌闭环
- 早期探索：Agent 如何把重复工作流沉淀成可复用工具或 Skill

## 架构方向

MiniCodex2 会刻意拆分职责：

- **Runtime / Agent OS 层**：上下文组装、工具执行、权限、日志、持久化、事件、记忆、缓存纪律、失败回灌和后台进程控制。
- **Skills**：工作流指导和领域操作手册。
- **Plugins**：可信能力包，可以提供工具、Hook、Skill 和集成。
- **Tools**：暴露给模型的结构化可执行能力。
- **Model**：语义判断、项目理解、下一步决策、失败归因，以及是否更新计划或记忆。

Agent OS 不是最终目标。它是在构建 CodexRobot 以及未来更多 Agent 项目过程中沉淀出来的公共 runtime。

## 安装

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,cli,tui,api]"
```

## 基础检查

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m compileall src tests
```

## 运行脚本

仓库包含几个 Windows 便捷脚本，用于本地实验：

```powershell
.\run_tui.bat
.\run_tui_gpt4mini.bat
.\run_tui_gpt55.bat
.\run_tui_deepseek.bat
.\run_tui_aihub2api_openai.bat
.\run_benchmark_fake.bat
.\run_benchmark_real.bat
```

这些脚本可能会提示输入 API key，或者从本地环境变量读取 key。`*.local.bat`、`.env`、日志和 session 数据不会提交到仓库。

## 配置

复制示例配置，然后填入你本地的 provider 设置：

```powershell
Copy-Item minicodex2.example.toml minicodex2.toml
```

不要提交本地 API key、`.local` 脚本、日志或 session 数据。

## 文档

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

部分文档是活跃实验中的设计笔记，可能同时描述已实现、部分实现和计划中的行为。
