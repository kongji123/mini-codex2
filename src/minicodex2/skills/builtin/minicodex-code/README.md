# MiniCodex Code Skill / MiniCodex 代码 Skill

## Purpose / 目的

This package externalizes MiniCodex2's current built-in coding workflow into a skill package.

这个包把 MiniCodex2 当前内置的代码开发工作流外置成一个 skill 包。

## Runtime Model / 运行模型

`SKILL.md` and files under `references/` are execution instructions for the model. They are written in Chinese to match the main development language of this project and to avoid duplicated bilingual prompt tokens.

`SKILL.md` 和 `references/` 下的文件是给模型执行用的指令。它们使用中文，以匹配本项目主要开发语言，并避免双语重复消耗 token。

This `README.md` is for human readers. It is bilingual so developers can understand the intent without loading execution prompts into every model turn.

本 `README.md` 面向人类读者，因此使用英中双语。这样开发者可以理解设计意图，同时不会把双语解释塞进每次模型执行上下文。

## Boundaries / 边界

The skill controls workflow guidance only. Agent OS hard policy remains outside the skill:

这个 skill 只控制工作流指导。Agent OS 的硬策略不属于 skill：

- path safety / 路径安全
- permissions / 权限
- command timeouts / 命令超时
- background process management / 后台进程管理
- tool schema validation / 工具 schema 校验
- write-after-verify gate / 写后验证门禁
- event log / 事件日志
- context buffer / 上下文缓存
- memory store / 记忆系统

## Migration Modes / 迁移模式

- `legacy`: current built-in code guidance remains primary.
- `overlay`: built-in guidance remains primary, external skill is visible as reference.
- `external`: configured external skill becomes primary.

- `legacy`：当前内置代码指导仍是 primary。
- `overlay`：内置指导仍是 primary，外置 skill 作为 reference 可见。
- `external`：配置指定的外置 skill 成为 primary。
