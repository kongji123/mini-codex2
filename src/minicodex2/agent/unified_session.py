from __future__ import annotations

from collections import Counter
import difflib
import hashlib
import json
import os
import platform
import re
import socket
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from uuid import uuid4

from minicodex2.agent.chat_history import ChatHistory
from minicodex2.agent.compaction import CompactionManager
from minicodex2.agent.context import ContextManager
from minicodex2.agent.context import estimate_messages_tokens
from minicodex2.agent.context import estimate_static_payload_tokens
from minicodex2.agent.context_buffer import ContextBufferStore
from minicodex2.agent.code_runtime import CodeRuntimeServices
from minicodex2.agent.events import AgentEventBus
from minicodex2.agent.failure_pack import FailurePack
from minicodex2.agent.logging import RuntimeLogger, redact_payload, redact_text
from minicodex2.agent.memory_extractor import AsyncMemoryExtractor
from minicodex2.agent.memory_extractor import MemoryExtractionJob
from minicodex2.agent.memory_extractor import MemoryExtractionResult
from minicodex2.agent.project_memory import ProjectGuidanceLoader
from minicodex2.agent.project_memory import ProjectMemoryIndex
from minicodex2.agent.project_memory import ProjectMemoryStore
from minicodex2.agent.project_memory import TurnMemoryStore
from minicodex2.agent.project_memory import WorkflowMemoryStore
from minicodex2.agent.project_memory import WorkPlanDocumentParser
from minicodex2.agent.result import AgentTurnResult
from minicodex2.agent.token_usage import TokenUsageTracker
from minicodex2.agent_os.goal_controller import GoalController
from minicodex2.agent_os.state import SessionState
from minicodex2.config.settings import AppSettings
from minicodex2.decision.continuation import ContinuationPlanner
from minicodex2.decision.dependency_policy import DependencyInstallPolicy
from minicodex2.decision.diagnostic_policy import assistant_is_asking_user_for_diagnostics
from minicodex2.decision.diagnostic_policy import diagnose_user_input
from minicodex2.model.adapter import ModelAdapter
from minicodex2.model.messages import ChatMessage, ModelRequest, ModelResponse
from minicodex2.model.messages import TokenUsage
from minicodex2.skills.engineering import CollaborationAdvisor
from minicodex2.skills.registry import SkillRegistry
from minicodex2.tools.command_runner import CommandRunner
from minicodex2.tools.path_safety import PathSafety
from minicodex2.tools.registry import RegisteredTool, ToolRegistry
from minicodex2.tools.runtime_tools import background_service_diagnosis
from minicodex2.tools.results import CommandResult, ToolResult
from minicodex2.verification.plan import VerificationPlan
from minicodex2.verification.result import VerificationResult


@dataclass(slots=True)
class TurnScopeDecision:
    turn_scope: str
    goal_engagement: str
    reason: str = ""

    @property
    def engages_goal(self) -> bool:
        return self.goal_engagement == "engaged"


class _LockedModelAdapter(ModelAdapter):
    def __init__(self, inner: ModelAdapter, lock: threading.RLock) -> None:
        self.inner = inner
        self.lock = lock

    def complete(self, request: ModelRequest) -> ModelResponse:
        with self.lock:
            return self.inner.complete(request)


class UnifiedAgentSession:
    def __init__(
        self,
        *,
        settings: AppSettings,
        model: ModelAdapter,
        tools: ToolRegistry,
        history: ChatHistory | None = None,
        session_id: str | None = None,
        checkpoint_callback: Callable[["UnifiedAgentSession"], None] | None = None,
        runtime_tools: Any | None = None,
    ) -> None:
        self.settings = settings
        self._model_lock = threading.RLock()
        self.model = _LockedModelAdapter(model, self._model_lock)
        self.tools = tools
        self.runtime_tools = runtime_tools
        self.history = history or ChatHistory()
        self.checkpoint_callback = checkpoint_callback
        self.session_id = session_id or f"session_{uuid4().hex[:12]}"
        self.logger = RuntimeLogger(settings.artifact_root, self.session_id)
        self.context_buffer_store = ContextBufferStore(settings.artifact_root)
        self.history.on_message_added = self._append_history_message_to_context_buffer
        self._context_buffer_extra_message_keys: set[str] = set()
        repaired_messages = self.history.repair_tool_call_sequence()
        if repaired_messages:
            self.logger.write(f"history tool-call sequence repaired count={repaired_messages}")
        self.event_bus = AgentEventBus(self.session_id, logger=self.logger)
        self._cancel_event = threading.Event()
        self.event_bus.emit(
            "session_created",
            {
                "workspace_root": str(settings.workspace_root),
                "permission_mode": settings.permission_mode,
            },
        )
        self.context_manager = ContextManager()
        self.compaction_manager = CompactionManager(self.model, settings.model.model)
        self.token_usage = TokenUsageTracker()
        self.state = SessionState()
        self.goal_controller = GoalController(
            self.state,
            max_steps_per_turn=settings.agent.max_verified_steps_per_turn,
        )
        self.path_safety = PathSafety(settings.workspace_root, settings.paths.projects_root)
        self.project_guidance_loader = ProjectGuidanceLoader()
        self.project_memory_index = ProjectMemoryIndex()
        self.project_memory_store = ProjectMemoryStore(settings.artifact_root)
        self.workflow_memory_store = WorkflowMemoryStore(settings.artifact_root)
        self.turn_memory_store = TurnMemoryStore(settings.artifact_root)
        memory_model = self.model
        if hasattr(self.model, "inner") and hasattr(self.model.inner, "clone_for_parallel_use"):
            memory_model = _LockedModelAdapter(
                self.model.inner.clone_for_parallel_use(), threading.RLock()
            )
        self.memory_extractor = AsyncMemoryExtractor(
            model=memory_model,
            model_name=settings.model.model,
            max_events=settings.agent.memory_extraction_max_events,
            logger=self.logger,
            event_emit=lambda event_type, payload: self.event_bus.emit(event_type, payload),
        )
        self.work_plan_document_parser = WorkPlanDocumentParser()
        self._register_goal_tools()
        self._register_project_memory_tools()
        self._register_workflow_memory_tools()
        self._register_runtime_resource_tools()
        self.skill_registry = SkillRegistry(settings)
        self._register_skill_tools()
        self.code_runtime = CodeRuntimeServices(settings)
        self.continuation_planner = ContinuationPlanner()
        self.dependency_command_runner = CommandRunner()
        self.collaboration_advisor = CollaborationAdvisor()
        self._last_startup_report: dict[str, object] | None = None
        self._pending_verification_changed_files: list[str] = []
        self._model_selected_verification_since_write = False
        self._idle_tick_count = 0
        self._context_dump_index = 0
        self._idle_tick_inflight = False
        self._last_idle_tick_at = 0.0
        self._bind_tool_progress_events()
        if repaired_messages:
            self._checkpoint("history_tool_call_sequence_repaired")

    def cancel_current_turn(self, *, source: str = "external") -> None:
        self._cancel_event.set()
        self.state.result_state = "cancelled"
        self.logger.write(f"turn cancellation requested source={source}")

    def _cancelled_result(self, turn_id: str) -> AgentTurnResult:
        self.state.result_state = "cancelled"
        self.event_bus.emit("turn_cancelled", {"reason": "user requested cancellation"}, turn_id)
        self.event_bus.emit("turn_finished", {"status": "cancelled"}, turn_id)
        self._checkpoint("turn_cancelled")
        return AgentTurnResult(
            status="cancelled",
            response="Turn cancelled by user.",
            changed_files=self.state.all_changed_files(),
            token_usage=self.token_usage.total(),
        )

    def _register_goal_tools(self) -> None:
        string = {"type": "string"}
        integer = {"type": "integer"}
        self.tools.register(
            RegisteredTool(
                "get_goal",
                "Get the current structured goal for this session, including objective, status, and optional token budget.",
                {"type": "object", "properties": {}, "required": []},
                self.goal_controller.get_goal,
            )
        )
        self.tools.register(
            RegisteredTool(
                "get_work_plan",
                (
                    "Get the current structured work plan for the active goal. Defaults to a compact open-step "
                    "view so long plans do not get truncated. Use status='done' or status='all' with limit/offset "
                    "when you need completed history or a specific page. Use detail='full' or step_id only when "
                    "you need exact evidence text for one step; compact/standard views are cheaper and should be "
                    "preferred for progress review."
                ),
                {
                    "type": "object",
                    "properties": {
                        "status": {
                            "type": "string",
                            "enum": ["open", "pending", "in_progress", "blocked", "done", "all"],
                        },
                        "limit": integer,
                        "offset": integer,
                        "step_id": string,
                        "detail": {
                            "type": "string",
                            "enum": ["compact", "standard", "full", "evidence"],
                        },
                        "include_evidence": {"type": "boolean"},
                        "reconcile_evidence": {"type": "boolean"},
                    },
                    "required": [],
                },
                self._get_work_plan,
            )
        )
        self.tools.register(
            RegisteredTool(
                "create_goal",
                (
                    "Create a durable session goal only when the user explicitly requests a broad or long-running objective, "
                    "or when system/developer instructions require goal tracking. Do not infer goals from ordinary chat or narrow tasks. "
                    "If an unfinished active goal already exists, this tool appends the requested objective as a new pending "
                    "WorkPlan item under the current goal instead of replacing it."
                ),
                {
                    "type": "object",
                    "properties": {"objective": string, "token_budget": integer},
                    "required": ["objective"],
                },
                self.goal_controller.create_goal,
            )
        )
        self.tools.register(
            RegisteredTool(
                "update_goal",
                (
                    "Update the current goal status. Use only status='complete' when the objective is achieved, "
                    "or status='blocked' when the same blocking condition repeats and progress cannot continue."
                ),
                {
                    "type": "object",
                    "properties": {
                        "status": {
                            "type": "string",
                            "enum": ["complete", "blocked"],
                        }
                    },
                    "required": ["status"],
                },
                self.goal_controller.update_goal,
            )
        )
        work_step_schema = {
            "type": "object",
            "properties": {
                "id": string,
                "title": string,
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "done", "blocked"],
                },
                "acceptance": string,
                "verification_hint": string,
                "evidence": {"type": "array", "items": string},
                "evidence_ids": {"type": "array", "items": string},
                        "blocker": string,
                        "required": {"type": "boolean"},
                        "source_document": string,
                    },
                    "required": ["title"],
                }
        self.tools.register(
            RegisteredTool(
                "set_work_plan",
                (
                    "Set or replace the structured work plan for the active goal. Use this after creating "
                    "a broad goal or when the user explicitly wants to replace the current plan. "
                    "For document updates, prefer sync_work_plan_from_document because it merges and preserves progress. "
                    "Do not use this for transient diagnostic/checklist substeps. If the current plan came "
                    "from a document, replacement is blocked unless replace_document_plan=true is explicitly "
                    "provided because the user wants to replace that document plan. Do not change the root objective."
                ),
                {
                    "type": "object",
                    "properties": {
                        "steps": {"type": "array", "items": work_step_schema},
                        "replace_document_plan": {"type": "boolean"},
                    },
                    "required": ["steps"],
                },
                self.goal_controller.set_work_plan,
            )
        )
        self.tools.register(
            RegisteredTool(
                "update_work_step",
                (
                    "Update one work-plan step status and evidence. Use this to mark a current step done "
                    "only after concrete acceptance evidence, or blocked with blocker evidence."
                ),
                {
                    "type": "object",
                    "properties": {
                        "step_id": string,
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "done", "blocked"],
                        },
                        "evidence": string,
                        "evidence_ids": {"type": "array", "items": string},
                        "blocker": string,
                        "acceptance": string,
                        "verification_hint": string,
                        "required": {"type": "boolean"},
                    },
                    "required": ["step_id", "status"],
                },
                self.goal_controller.update_work_step,
            )
        )
        self.tools.register(
            RegisteredTool(
                "reconcile_work_plan_evidence",
                (
                    "Reconcile already-collected tool evidence with open WorkPlan steps. Use this before "
                    "rerunning broad verification/test/browser loops for plan items that describe quality "
                    "gates such as automated tests, integration tests, API checks, browser/UI checks, or "
                    "deployment verification. It does not invent evidence; it only attaches matching known "
                    "evidence_ids and marks a step done when the evidence categories satisfy that step."
                ),
                {
                    "type": "object",
                    "properties": {
                        "status": {
                            "type": "string",
                            "enum": ["open", "pending", "in_progress", "blocked", "all"],
                        }
                    },
                    "required": [],
                },
                self.goal_controller.reconcile_work_plan_evidence,
            )
        )
        self.tools.register(
            RegisteredTool(
                "accept_work_step",
                (
                    "Mark one work-plan step done through a non-automated acceptance channel. Use "
                    "source='user_confirmed' only when the user explicitly confirms completion; "
                    "source='document_marked_done' only when a planning/design document marks it complete; "
                    "source='model_reviewed_existing_evidence' when the model is accepting based on existing "
                    "local evidence that could not be attached through update_work_step. This records a distinct "
                    "evidence kind so model review does not masquerade as tool verification or user acceptance."
                ),
                {
                    "type": "object",
                    "properties": {
                        "step_id": string,
                        "reason": string,
                        "source": {
                            "type": "string",
                            "enum": [
                                "user_confirmed",
                                "document_marked_done",
                                "model_reviewed_existing_evidence",
                            ],
                        },
                    },
                    "required": ["step_id", "reason"],
                },
                self.goal_controller.accept_work_step,
            )
        )
        self.tools.register(
            RegisteredTool(
                "set_current_step",
                (
                    "Select the current step from the active work plan. Use this to focus the next loop "
                    "without changing the root objective."
                ),
                {
                    "type": "object",
                    "properties": {"step_id": string},
                    "required": ["step_id"],
                },
                self.goal_controller.set_current_step,
            )
        )
        self.tools.register(
            RegisteredTool(
                "sync_work_plan_from_document",
                (
                    "Read a project planning document and merge it into the active goal's WorkPlan with "
                    "candidate engineering deliverables extracted from headings and checklist/bullet lines. "
                    "Existing matching steps keep their status, evidence, and current progress. "
                    "Document-completed items such as [x], done, completed, or 已完成 are marked done with document_acceptance evidence. "
                    "Use this when a PRD, milestone, architecture, or plan document should become runtime "
                    "working memory. The resulting steps should be feature/implementation/verification "
                    "items with acceptance criteria, not merely document outline sections."
                ),
                {
                    "type": "object",
                    "properties": {
                        "path": string,
                        "max_steps": integer,
                    },
                    "required": ["path"],
                },
                self._sync_work_plan_from_document,
            )
        )
        self.tools.register(
            RegisteredTool(
                "inspect_project_startup",
                (
                    "Inspect high-confidence local startup candidates from project files such as "
                    "package.json, manage.py, Makefile, Cargo.toml, and go.mod, plus currently "
                    "known runtime resources. Use this before guessing service commands or ports."
                ),
                {
                    "type": "object",
                    "properties": {
                        "root": string,
                        "max_depth": integer,
                    },
                    "required": [],
                },
                self._inspect_project_startup,
            )
        )
        self.tools.register(
            RegisteredTool(
                "record_lesson",
                (
                    "Record a reusable lesson learned from a failure, correction, or evidence update. "
                    "Use this for general process/tooling/assumption lessons, not ordinary chat, secrets, "
                    "or one-off implementation details."
                ),
                {
                    "type": "object",
                    "properties": {
                        "summary": string,
                        "trigger": string,
                        "scope": string,
                        "evidence": string,
                    },
                    "required": ["summary"],
                },
                self._record_lesson,
            )
        )
        self.tools.register(
            RegisteredTool(
                "get_lessons",
                (
                    "Retrieve recent or query-matched reusable lessons from agent memory. Use before "
                    "repeating a failed approach or when a current problem resembles prior evidence."
                ),
                {
                    "type": "object",
                    "properties": {
                        "query": string,
                        "limit": integer,
                    },
                    "required": [],
                },
                self._get_lessons,
            )
        )

    def _register_project_memory_tools(self) -> None:
        string = {"type": "string"}
        integer = {"type": "integer"}
        boolean = {"type": "boolean"}
        self.tools.register(
            RegisteredTool(
                "remember_project_fact",
                (
                    "Persist a reusable project fact that will help future turns avoid rediscovery. "
                    "Use only for observed or verified facts such as startup commands, tool paths, "
                    "ports, test/build/smoke commands, project-local environment details, or recurring "
                    "failure fixes. Do not store secrets, ordinary chat, guesses, or one-off code details."
                ),
                {
                    "type": "object",
                    "properties": {
                        "title": string,
                        "content": string,
                        "tags": {"type": "array", "items": string},
                        "scope": string,
                        "confidence": {
                            "type": "string",
                            "enum": ["low", "medium", "high"],
                        },
                        "source": string,
                    },
                    "required": ["title", "content"],
                },
                self._remember_project_fact,
            )
        )
        self.tools.register(
            RegisteredTool(
                "search_project_memory",
                (
                    "Search persistent project memory by query, tags, or scope. Use before rediscovering "
                    "startup commands, environment paths, known test commands, ports, or recurring failures."
                ),
                {
                    "type": "object",
                    "properties": {
                        "query": string,
                        "tags": {"type": "array", "items": string},
                        "scope": string,
                        "include_stale": boolean,
                        "limit": integer,
                    },
                    "required": [],
                },
                self._search_project_memory,
            )
        )
        self.tools.register(
            RegisteredTool(
                "read_project_memory",
                "Read the full content of one persistent project memory fact by id.",
                {
                    "type": "object",
                    "properties": {"memory_id": string},
                    "required": ["memory_id"],
                },
                self._read_project_memory,
            )
        )
        self.tools.register(
            RegisteredTool(
                "update_project_memory",
                (
                    "Update or mark stale one persistent project memory fact. Use this when a remembered "
                    "command/path/port is confirmed obsolete or when a better verified fact replaces it."
                ),
                {
                    "type": "object",
                    "properties": {
                        "memory_id": string,
                        "title": string,
                        "content": string,
                        "tags": {"type": "array", "items": string},
                        "scope": string,
                        "confidence": {
                            "type": "string",
                            "enum": ["low", "medium", "high"],
                        },
                        "stale": boolean,
                    },
                    "required": ["memory_id"],
                },
                self._update_project_memory,
            )
        )
        self.tools.register(
            RegisteredTool(
                "forget_project_memory",
                "Delete one persistent project memory fact by id when it is wrong, unsafe, or no longer useful.",
                {
                    "type": "object",
                    "properties": {"memory_id": string},
                    "required": ["memory_id"],
                },
                self._forget_project_memory,
            )
        )

    def _register_workflow_memory_tools(self) -> None:
        string = {"type": "string"}
        integer = {"type": "integer"}
        boolean = {"type": "boolean"}
        self.tools.register(
            RegisteredTool(
                "remember_workflow",
                (
                    "Persist a reusable project workflow or operating procedure discovered from evidence. "
                    "Use for stable startup, test, build, smoke, integration, environment, or debug procedures "
                    "that will reduce future rediscovery. Do not store secrets, guesses, or one-off commands."
                ),
                {
                    "type": "object",
                    "properties": {
                        "title": string,
                        "cwd": string,
                        "command": string,
                        "purpose": string,
                        "ready_check": string,
                        "steps": {"type": "array", "items": string},
                        "ports": {"type": "array", "items": integer},
                        "tags": {"type": "array", "items": string},
                        "scope": string,
                        "confidence": {
                            "type": "string",
                            "enum": ["low", "medium", "high"],
                        },
                        "source": string,
                        "notes": string,
                    },
                    "required": ["title"],
                },
                self._remember_workflow,
            )
        )
        self.tools.register(
            RegisteredTool(
                "search_workflows",
                (
                    "Search persistent workflow memory before rediscovering startup, test, build, smoke, "
                    "integration, environment, or repeated debugging procedures."
                ),
                {
                    "type": "object",
                    "properties": {
                        "query": string,
                        "tags": {"type": "array", "items": string},
                        "scope": string,
                        "purpose": string,
                        "include_stale": boolean,
                        "limit": integer,
                    },
                    "required": [],
                },
                self._search_workflows,
            )
        )
        self.tools.register(
            RegisteredTool(
                "read_workflow",
                "Read one full persistent workflow memory by id.",
                {
                    "type": "object",
                    "properties": {"workflow_id": string},
                    "required": ["workflow_id"],
                },
                self._read_workflow,
            )
        )
        self.tools.register(
            RegisteredTool(
                "update_workflow",
                (
                    "Update or mark stale one persistent workflow memory. Use when a remembered procedure "
                    "is confirmed obsolete, improved, or should receive fresh success/failure evidence."
                ),
                {
                    "type": "object",
                    "properties": {
                        "workflow_id": string,
                        "title": string,
                        "cwd": string,
                        "command": string,
                        "purpose": string,
                        "ready_check": string,
                        "steps": {"type": "array", "items": string},
                        "ports": {"type": "array", "items": integer},
                        "tags": {"type": "array", "items": string},
                        "scope": string,
                        "confidence": {
                            "type": "string",
                            "enum": ["low", "medium", "high"],
                        },
                        "notes": string,
                        "stale": boolean,
                        "success_delta": integer,
                        "failure_delta": integer,
                    },
                    "required": ["workflow_id"],
                },
                self._update_workflow,
            )
        )
        self.tools.register(
            RegisteredTool(
                "forget_workflow",
                "Delete one persistent workflow memory by id when it is wrong, unsafe, or no longer useful.",
                {
                    "type": "object",
                    "properties": {"workflow_id": string},
                    "required": ["workflow_id"],
                },
                self._forget_workflow,
            )
        )

    def _register_runtime_resource_tools(self) -> None:
        string = {"type": "string"}
        integer = {"type": "integer"}
        self.tools.register(
            RegisteredTool(
                "read_background_log",
                (
                    "Read the tail of a background service log created by start_background_command. "
                    "Use this instead of guessing .minicodex2 log paths with raw shell commands. "
                    "Select by resource_id, port, pid, log_path, or omit selectors to read the latest background log."
                ),
                {
                    "type": "object",
                    "properties": {
                        "resource_id": string,
                        "port": integer,
                        "pid": integer,
                        "log_path": string,
                        "max_lines": integer,
                        "max_bytes": integer,
                    },
                    "required": [],
                },
                self._read_background_log,
            )
        )

    def _register_skill_tools(self) -> None:
        string = {"type": "string"}
        boolean = {"type": "boolean"}
        self.tools.register(
            RegisteredTool(
                "list_skills",
                (
                    "List Agent OS skills and the active skill selection. Use this when you need "
                    "to inspect available workflow guidance without loading full skill text."
                ),
                {
                    "type": "object",
                    "properties": {
                        "domain": string,
                        "role": string,
                        "include_references": boolean,
                    },
                    "required": [],
                },
                self.skill_registry.list_skills,
            )
        )
        self.tools.register(
            RegisteredTool(
                "load_skill",
                (
                    "Load a skill's SKILL.md or a referenced instruction file. Use this when the "
                    "current task needs the full guidance for an available skill."
                ),
                {
                    "type": "object",
                    "properties": {
                        "name": string,
                        "reference": string,
                    },
                    "required": ["name"],
                },
                self.skill_registry.load_skill,
            )
        )

    def _read_background_log(
        self,
        resource_id: str = "",
        port: int | None = None,
        pid: int | None = None,
        log_path: str = "",
        max_lines: int = 80,
        max_bytes: int = 12000,
    ) -> ToolResult:
        resource = self._select_background_log_resource(
            resource_id=resource_id,
            port=port,
            pid=pid,
            log_path=log_path,
        )
        selected_log_path = log_path.strip() if log_path else (resource.log_path if resource else "")
        if not selected_log_path:
            return ToolResult(
                ok=False,
                content="no background log is known for the requested selector",
                metadata={
                    "resource_id": resource_id,
                    "port": port,
                    "pid": pid,
                    "failure_kind": "background_log_not_found",
                },
            )
        try:
            safe = self.path_safety.resolve_workspace_path(selected_log_path)
        except Exception as exc:
            return ToolResult(
                ok=False,
                content=str(exc),
                blocked=True,
                block_reason=str(exc),
                metadata={
                    "resource_id": resource.id if resource else resource_id,
                    "log_path": selected_log_path,
                    "failure_kind": "background_log_outside_workspace",
                },
            )
        if not safe.path.exists():
            return ToolResult(
                ok=False,
                content=f"background log does not exist: {safe.relative}",
                metadata={
                    "resource_id": resource.id if resource else resource_id,
                    "log_path": safe.relative,
                    "failure_kind": "background_log_not_found",
                },
            )
        raw = safe.path.read_bytes()
        max_lines = max(1, min(int(max_lines), 500))
        max_bytes = max(1, min(int(max_bytes), 200_000))
        text = raw[-max_bytes:].decode("utf-8", errors="replace")
        lines = text.splitlines()
        tail = "\n".join(lines[-max_lines:])
        metadata = {
            "resource_id": resource.id if resource else "",
            "path": safe.relative,
            "log_path": safe.relative,
            "port": resource.port if resource else port,
            "pid": resource.pid if resource else pid,
            "returned_lines": min(len(lines), max_lines),
            "returned_bytes": len(tail.encode("utf-8")),
            "size": len(raw),
            "truncated": len(raw) > max_bytes or len(lines) > max_lines,
            "log_tail": tail,
        }
        metadata["diagnosis"] = background_service_diagnosis(
            {
                "ready": False,
                "exit_code": None,
                "port": resource.port if resource else port,
                "pid": resource.pid if resource else pid,
                "log_tail": tail,
                "log_path": safe.relative,
                "command": resource.command if resource else "",
            }
        )
        return ToolResult(
            ok=True,
            content=json.dumps(metadata, ensure_ascii=False, indent=2),
            metadata=metadata,
        )

    def _select_background_log_resource(
        self,
        *,
        resource_id: str = "",
        port: int | None = None,
        pid: int | None = None,
        log_path: str = "",
    ) -> object | None:
        resources = [
            resource
            for resource in self.state.runtime_resources
            if resource.kind == "background_process" and resource.log_path
        ]
        if log_path:
            normalized = log_path.replace("\\", "/").strip()
            for resource in reversed(resources):
                if resource.log_path.replace("\\", "/").strip() == normalized:
                    return resource
            return None
        if resource_id:
            for resource in reversed(resources):
                if resource.id == resource_id:
                    return resource
            return None
        if port is not None:
            for resource in reversed(resources):
                if resource.port == port:
                    return resource
            return None
        if pid is not None:
            for resource in reversed(resources):
                if resource.pid == pid:
                    return resource
            return None
        return resources[-1] if resources else None

    def _record_lesson(
        self,
        summary: str,
        trigger: str = "",
        scope: str = "general",
        evidence: str = "",
    ) -> ToolResult:
        lesson = self.state.add_lesson(
            summary=summary,
            trigger=trigger,
            scope=scope,
            evidence=evidence,
        )
        return ToolResult(
            ok=True,
            content=json.dumps(lesson.to_dict(), ensure_ascii=False, indent=2),
            metadata={"lesson_event": "recorded", "lesson": lesson.to_dict()},
        )

    def _get_lessons(self, query: str = "", limit: int = 5) -> ToolResult:
        lessons = [lesson.to_dict() for lesson in self.state.relevant_lessons(query, limit)]
        return ToolResult(
            ok=True,
            content=json.dumps({"lessons": lessons}, ensure_ascii=False, indent=2),
            metadata={"lesson_event": "retrieved", "lessons": lessons},
        )

    def _remember_project_fact(
        self,
        title: str,
        content: str,
        tags: list[str] | None = None,
        scope: str = "project",
        confidence: str = "medium",
        source: str = "model",
    ) -> ToolResult:
        fact = self.project_memory_store.remember(
            title=title,
            content=content,
            tags=tags or [],
            scope=scope,
            confidence=confidence,
            source=source,
        )
        return ToolResult(
            ok=True,
            content=json.dumps(fact.to_dict(), ensure_ascii=False, indent=2),
            metadata={"project_memory_event": "remembered", "memory": fact.to_dict()},
        )

    def _search_project_memory(
        self,
        query: str = "",
        tags: list[str] | None = None,
        scope: str = "",
        include_stale: bool = False,
        limit: int = 8,
    ) -> ToolResult:
        facts = [
            fact.index_dict()
            for fact in self.project_memory_store.search(
                query=query,
                tags=tags or [],
                scope=scope,
                include_stale=include_stale,
                limit=limit,
            )
        ]
        return ToolResult(
            ok=True,
            content=json.dumps({"memories": facts}, ensure_ascii=False, indent=2),
            metadata={"project_memory_event": "searched", "memories": facts},
        )

    def _read_project_memory(self, memory_id: str) -> ToolResult:
        fact = self.project_memory_store.get(memory_id)
        if fact is None:
            return ToolResult(
                ok=False,
                content=f"project memory not found: {memory_id}",
                blocked=True,
                block_reason="project memory not found",
            )
        return ToolResult(
            ok=True,
            content=json.dumps(fact.to_dict(), ensure_ascii=False, indent=2),
            metadata={"project_memory_event": "read", "memory": fact.to_dict()},
        )

    def _update_project_memory(
        self,
        memory_id: str,
        title: str | None = None,
        content: str | None = None,
        tags: list[str] | None = None,
        scope: str | None = None,
        confidence: str | None = None,
        stale: bool | None = None,
    ) -> ToolResult:
        fact = self.project_memory_store.update(
            memory_id,
            title=title,
            content=content,
            tags=tags,
            scope=scope,
            confidence=confidence,
            stale=stale,
        )
        if fact is None:
            return ToolResult(
                ok=False,
                content=f"project memory not found: {memory_id}",
                blocked=True,
                block_reason="project memory not found",
            )
        return ToolResult(
            ok=True,
            content=json.dumps(fact.to_dict(), ensure_ascii=False, indent=2),
            metadata={"project_memory_event": "updated", "memory": fact.to_dict()},
        )

    def _forget_project_memory(self, memory_id: str) -> ToolResult:
        deleted = self.project_memory_store.forget(memory_id)
        return ToolResult(
            ok=deleted,
            content=json.dumps({"memory_id": memory_id, "deleted": deleted}, ensure_ascii=False, indent=2),
            blocked=not deleted,
            block_reason=None if deleted else "project memory not found",
            metadata={"project_memory_event": "forgotten", "memory_id": memory_id, "deleted": deleted},
        )

    def _remember_workflow(
        self,
        title: str,
        cwd: str = ".",
        command: str = "",
        purpose: str = "workflow",
        ready_check: str = "",
        steps: list[str] | None = None,
        ports: list[int] | None = None,
        tags: list[str] | None = None,
        scope: str = "project",
        confidence: str = "medium",
        source: str = "model",
        notes: str = "",
    ) -> ToolResult:
        workflow = self.workflow_memory_store.remember(
            title=title,
            cwd=cwd,
            command=command,
            purpose=purpose,
            ready_check=ready_check,
            steps=steps or [],
            ports=ports or [],
            tags=tags or [],
            scope=scope,
            confidence=confidence,
            source=source,
            notes=notes,
        )
        return ToolResult(
            ok=True,
            content=json.dumps(workflow.to_dict(), ensure_ascii=False, indent=2),
            metadata={"workflow_memory_event": "remembered", "workflow": workflow.to_dict()},
        )

    def _search_workflows(
        self,
        query: str = "",
        tags: list[str] | None = None,
        scope: str = "",
        purpose: str = "",
        include_stale: bool = False,
        limit: int = 8,
    ) -> ToolResult:
        workflows = [
            workflow.index_dict()
            for workflow in self.workflow_memory_store.search(
                query=query,
                tags=tags or [],
                scope=scope,
                purpose=purpose,
                include_stale=include_stale,
                limit=limit,
            )
        ]
        return ToolResult(
            ok=True,
            content=json.dumps({"workflows": workflows}, ensure_ascii=False, indent=2),
            metadata={"workflow_memory_event": "searched", "workflows": workflows},
        )

    def _read_workflow(self, workflow_id: str) -> ToolResult:
        workflow = self.workflow_memory_store.get(workflow_id)
        if workflow is None:
            return ToolResult(
                ok=False,
                content=f"workflow memory not found: {workflow_id}",
                blocked=True,
                block_reason="workflow memory not found",
            )
        return ToolResult(
            ok=True,
            content=json.dumps(workflow.to_dict(), ensure_ascii=False, indent=2),
            metadata={"workflow_memory_event": "read", "workflow": workflow.to_dict()},
        )

    def _update_workflow(
        self,
        workflow_id: str,
        title: str | None = None,
        cwd: str | None = None,
        command: str | None = None,
        purpose: str | None = None,
        ready_check: str | None = None,
        steps: list[str] | None = None,
        ports: list[int] | None = None,
        tags: list[str] | None = None,
        scope: str | None = None,
        confidence: str | None = None,
        notes: str | None = None,
        stale: bool | None = None,
        success_delta: int = 0,
        failure_delta: int = 0,
    ) -> ToolResult:
        workflow = self.workflow_memory_store.update(
            workflow_id,
            title=title,
            cwd=cwd,
            command=command,
            purpose=purpose,
            ready_check=ready_check,
            steps=steps,
            ports=ports,
            tags=tags,
            scope=scope,
            confidence=confidence,
            notes=notes,
            stale=stale,
            success_delta=success_delta,
            failure_delta=failure_delta,
        )
        if workflow is None:
            return ToolResult(
                ok=False,
                content=f"workflow memory not found: {workflow_id}",
                blocked=True,
                block_reason="workflow memory not found",
            )
        return ToolResult(
            ok=True,
            content=json.dumps(workflow.to_dict(), ensure_ascii=False, indent=2),
            metadata={"workflow_memory_event": "updated", "workflow": workflow.to_dict()},
        )

    def _forget_workflow(self, workflow_id: str) -> ToolResult:
        deleted = self.workflow_memory_store.forget(workflow_id)
        return ToolResult(
            ok=deleted,
            content=json.dumps({"workflow_id": workflow_id, "deleted": deleted}, ensure_ascii=False, indent=2),
            blocked=not deleted,
            block_reason=None if deleted else "workflow memory not found",
            metadata={"workflow_memory_event": "forgotten", "workflow_id": workflow_id, "deleted": deleted},
        )

    def _bind_tool_progress_events(self) -> None:
        for tool in getattr(self.tools, "_tools", {}).values():
            owner = getattr(tool.handler, "__self__", None)
            if owner is not None and hasattr(owner, "progress_sink"):
                owner.progress_sink = self.event_bus.emit

    def _reset_turn_state(self) -> None:
        self.state.changed_files = []
        self.state.verified_files = []
        self.state.previous_failure_signature = None
        self.state.repair_round = 0
        self.state.did_write = False
        self.state.result_state = "running"

    def _add_user_message(self, user_input: str, image_input: dict[str, str] | None) -> None:
        if not image_input:
            self.history.add_user(user_input)
            return
        self.history.add_user_image(
            image_input["prompt"],
            path=image_input["path"],
            mime_type=image_input["mime_type"],
            detail=image_input["detail"],
        )

    def _append_history_message_to_context_buffer(self, message: ChatMessage) -> None:
        """Append durable history messages into the persisted main context buffer.

        This is deliberately attached to ChatHistory instead of the memory
        extractor or idle worker. Background memory extraction sends its own
        standalone ModelRequest and never writes ChatHistory, so it cannot
        contaminate the main coding conversation cache. Idle ticks can append
        normal assistant/tool history just like a user turn, which matches their
        role as low-priority continuation of the current work.
        """
        if not hasattr(self, "context_buffer_store"):
            return
        message_to_append = _normalize_runtime_message_for_main_context(message, allow_initial=False)
        if message_to_append is None:
            self.logger.event(
                "context_buffer_runtime_message_skipped",
                {
                    "role": message.role,
                    "reason": "cache_experiment_strip_empty_runtime_control",
                    "excerpt": message.content[:160],
                },
            )
            return
        if message_to_append is not message:
            self.logger.event(
                "context_buffer_runtime_message_converted",
                {
                    "from_role": message.role,
                    "to_role": message_to_append.role,
                    "reason": "preserve_runtime_guidance_without_midstream_system_role",
                    "excerpt": message.content[:160],
                },
            )
        try:
            appended = self.context_buffer_store.append(
                self.session_id,
                message_to_append,
                channel="main",
                metadata={
                    "source": "chat_history_append",
                    "message_role": message_to_append.role,
                    "original_message_role": message.role,
                },
            )
            if appended:
                self.logger.event(
                    "context_buffer_appended",
                    {"channel": "main", "role": message_to_append.role},
                )
        except Exception as exc:
            self.logger.write(f"context buffer append failed error={exc}")

    def _reset_idle_cycle(self) -> None:
        self._idle_tick_count = 0
        self._idle_tick_inflight = False
        self._last_idle_tick_at = 0.0

    def _pending_permission_exists(self) -> bool:
        runtime = self.runtime_tools
        if runtime is None or not hasattr(runtime, "permission_store"):
            return False
        return bool(runtime.permission_store.pending())

    def _idle_candidate_exists(self) -> bool:
        if self._cancel_event.is_set():
            return False
        if self._pending_permission_exists():
            return False
        # Idle ticks are deliberately scoped to durable project work. Earlier versions also
        # fired after ordinary chat, failures, or completed turns; that made the agent feel
        # like it was interrupting the user instead of continuing a known long-running goal.
        return self.state.goal_status == "active" and bool(self.state.active_goal)

    def should_run_idle_tick(self) -> bool:
        if not self.settings.agent.idle_tick_enabled:
            return False
        if self._idle_tick_inflight:
            return False
        if self._idle_tick_count >= self.settings.agent.idle_tick_max:
            return False
        if not self._idle_candidate_exists():
            return False
        now = time.monotonic()
        return (now - self._last_idle_tick_at) >= self.settings.agent.idle_tick_interval_seconds

    def _claim_idle_tick_slot(self) -> int | None:
        if not self.should_run_idle_tick():
            return None
        self._idle_tick_inflight = True
        self._idle_tick_count += 1
        self._last_idle_tick_at = time.monotonic()
        return self._idle_tick_count

    def _finish_idle_tick(self) -> None:
        self._idle_tick_inflight = False

    def _idle_reflection_message(self, tick_index: int) -> str:
        return (
            "[IDLE REFLECTION]\n"
            "This is an idle reflection tick, not a user message.\n"
            "No new user input is waiting.\n"
            "First check whether any required work, promised follow-up, unresolved blocker, "
            "reported failure target, or unverified change still remains.\n"
            "If required work remains, continue it with concrete tool calls.\n"
            "If no required work remains, reflect on whether there is one low-risk, genuinely useful "
            "next way to help the human partner in the local code and information workspace.\n"
            "Prefer suggestions, summaries, memory updates, read-only inspection, or safe local actions "
            "before higher-risk actions.\n"
            "If nothing useful remains, explicitly accept idle and do not call tools.\n"
            f"Tick: {tick_index}/{self.settings.agent.idle_tick_max}."
        )

    def run_idle_tick(self) -> AgentTurnResult | None:
        tick_index = self._claim_idle_tick_slot()
        if tick_index is None:
            return None
        turn_id = f"idle_{uuid4().hex[:12]}"
        self._cancel_event.clear()
        self._reset_turn_state()
        self.event_bus.emit(
            "idle_tick_started",
            {"tick": tick_index, "max": self.settings.agent.idle_tick_max},
            turn_id,
        )
        self.logger.write(f"run_idle_tick start turn_id={turn_id} tick={tick_index}")
        extra_messages = [
            ChatMessage(
                role="runtime",
                content=self._idle_reflection_message(tick_index),
                metadata={"ephemeral_control": True, "control": "idle_reflection"},
            )
        ]
        turn_scope = (
            TurnScopeDecision(
                turn_scope="continue_current_goal",
                goal_engagement="engaged",
                reason="idle reflection continues active goal",
            )
        )
        goal_turn = self.goal_controller.start_turn(engaged=True)
        try:
            result = self._run_agent_cycle(
                turn_id=turn_id,
                user_input="",
                expected_action=None,
                turn_scope=turn_scope,
                goal_turn=goal_turn,
                requested_toolchain=None,
                diagnostic_decision=None,
                extra_context_messages=extra_messages,
            )
        finally:
            self._finish_idle_tick()
        self.event_bus.emit(
            "idle_tick_finished",
            {"tick": tick_index, "status": result.status},
            turn_id,
        )
        return result

    def run_turn(
        self,
        user_input: str,
        *,
        allow_advice: bool = True,
        expected_action: str | None = None,
    ) -> AgentTurnResult:
        turn_id = f"turn_{uuid4().hex[:12]}"
        turn_started_at = time.monotonic()
        turn_event_start = len(self.event_bus.events())
        self._cancel_event.clear()
        self._reset_idle_cycle()
        self._reset_turn_state()
        self.event_bus.emit("turn_started", {"user_input": user_input[:200]}, turn_id)
        self.logger.write(f"run_turn start turn_id={turn_id}")
        history_command_result = self._handle_history_command(user_input)
        if history_command_result is not None:
            self.event_bus.emit("history_changed", {"message": history_command_result.response}, turn_id)
            self.event_bus.emit("turn_finished", {"status": history_command_result.status}, turn_id)
            return self._finish_turn_with_metrics(
                history_command_result, turn_id, turn_started_at, turn_event_start
            )
        image_input = _parse_image_input(user_input, self.settings.workspace_root)
        if allow_advice:
            advice = self.collaboration_advisor.evaluate(
                user_input,
                workspace_root=self.settings.workspace_root,
                already_advised=self.state.collaboration_advised,
                design_docs_exist=self.state.design_docs_exist,
            )
            if advice.should_advise:
                self.state.collaboration_advised = True
                message = self.collaboration_advisor.format_advice(advice)
                self._add_user_message(user_input, image_input)
                self._checkpoint("user_message_added")
                self.history.add_assistant(message)
                self._checkpoint("assistant_message_added")
                self.event_bus.emit(
                    "collaboration_advice_created",
                    {"score": advice.score, "signals": advice.signals, "mode": advice.suggested_mode},
                    turn_id,
                )
                self.event_bus.emit("turn_finished", {"status": "completed"}, turn_id)
                return self._finish_turn_with_metrics(AgentTurnResult(
                    status="completed",
                    response=message,
                    token_usage=self.token_usage.total(),
                ), turn_id, turn_started_at, turn_event_start)
        self._add_user_message(user_input, image_input)
        if image_input:
            self.event_bus.emit(
                "image_attached",
                {
                    "path": image_input["path"],
                    "mime_type": image_input["mime_type"],
                    "detail": image_input["detail"],
                    "prompt": image_input["prompt"][:200],
                },
                turn_id,
            )
        self._checkpoint("user_message_added")
        if expected_action == "modify_and_verify":
            self.history.add_runtime(self._project_preflight_message())
            self._checkpoint("project_preflight_added")
            self.event_bus.emit("project_preflight_created", {"expected_action": expected_action}, turn_id)
        requested_toolchain = _requested_toolchain_install(user_input)
        diagnostic_decision = diagnose_user_input(user_input) if not image_input else None
        for target in _extract_failure_reproduction_targets(user_input):
            self.state.add_failure_target(**target)
        turn_scope = (
            self._classify_turn_scope(user_input, turn_id)
            if _should_classify_turn_scope(
                user_input,
                state=self.state,
                expected_action=expected_action,
                diagnostic_decision=diagnostic_decision,
            )
            else None
        )
        extra_context_messages: list[ChatMessage] | None = None
        if turn_scope and turn_scope.turn_scope == "status_review":
            extra_context_messages = [
                ChatMessage(
                    role="runtime",
                    content=(
                        "[STATUS REVIEW]\n"
                        "This turn asks for a progress/status review, not automatic implementation.\n"
                        "You may inspect goal state, work plan, evidence, design documents, and relevant files.\n"
                        "Primary deliverable: synthesize the current status for the user.\n"
                        "After gathering enough evidence, answer with completed work, remaining gaps, and notable mismatches.\n"
                        "If the review reveals that durable memory is stale or mismatched, you may realign memory with "
                        "get_work_plan, set_current_step, update_work_step, set_work_plan, or sync_work_plan_from_document.\n"
                        "Persisting corrected goal/plan memory during status review is allowed.\n"
                        "If you verify that a step is done, first make sure fresh verification or tool evidence created real EvidenceStore ids, "
                        "then pass those exact evidence_ids to update_work_step.\n"
                        "Do not auto-continue implementation or resume the active goal unless the user explicitly asks to continue building."
                    ),
                    metadata={"ephemeral_control": True, "control": "status_review"},
                )
            ]
        elif turn_scope and turn_scope.turn_scope in {"modify_current_goal", "create_new_goal"}:
            extra_context_messages = [
                ChatMessage(
                    role="runtime",
                    content=(
                        "[GOAL PLANNING HANDOFF]\n"
                        "The latest user message appears to describe project work that needs durable tracking.\n"
                        "Do not treat it as an isolated one-turn edit. First decide whether it extends an existing "
                        "project/design document, starts a new milestone, or only records a discussion.\n"
                        "- If it extends an existing project/design document, create or reactivate an appropriate "
                        "goal, then use sync_work_plan_from_document or set_work_plan so the new requirement becomes "
                        "a verifiable WorkPlan step before implementation.\n"
                        "- If it is a new phase or independent milestone, create a new goal and a concise WorkPlan.\n"
                        "- If the user is only discussing an idea, answer the discussion and optionally record memory; "
                        "do not create a goal.\n"
                        "After durable goal/plan state is aligned, continue with concrete tools when implementation "
                        "is requested."
                    ),
                    metadata={"ephemeral_control": True, "control": "goal_planning"},
                )
            ]
        goal_turn = self.goal_controller.start_turn(
            engaged=bool(turn_scope and turn_scope.engages_goal)
        )
        result = self._run_agent_cycle(
            turn_id=turn_id,
            user_input=user_input,
            expected_action=expected_action,
            turn_scope=turn_scope,
            goal_turn=goal_turn,
            requested_toolchain=requested_toolchain,
            diagnostic_decision=diagnostic_decision,
            extra_context_messages=extra_context_messages,
        )
        return self._finish_turn_with_metrics(result, turn_id, turn_started_at, turn_event_start)

    def _finish_turn_with_metrics(
        self,
        result: AgentTurnResult,
        turn_id: str,
        started_at: float,
        event_start: int,
    ) -> AgentTurnResult:
        metrics = self._build_turn_metrics(turn_id, started_at, event_start)
        result.metrics = metrics
        self.event_bus.emit("turn_metrics", metrics, turn_id)
        if not turn_id.startswith("idle_"):
            self._record_turn_memory_artifacts(result, turn_id, event_start, metrics)
            self._queue_model_memory_extraction(result, turn_id, event_start, metrics)
        return result

    def _record_turn_memory_artifacts(
        self,
        result: AgentTurnResult,
        turn_id: str,
        event_start: int,
        metrics: dict[str, object],
    ) -> None:
        latest_user = self.history.latest_user_text() or ""
        events = [
            event
            for event in self.event_bus.events()[event_start:]
            if event.turn_id == turn_id
        ]
        records = self.turn_memory_store.record_turn(
            turn_id=turn_id,
            user_input=latest_user,
            result_status=result.status,
            metrics=metrics,
            events=events,
        )
        if not records:
            return
        payload = {
            "count": len(records),
            "records": [record.to_dict() for record in records[:8]],
            "memory_summary_path": str(self.turn_memory_store.summary_path),
            "memory_path": str(self.turn_memory_store.memory_md_path),
        }
        self.logger.event("turn_memory_consolidated", payload)
        self.event_bus.emit("turn_memory_consolidated", payload, turn_id)

    def _queue_model_memory_extraction(
        self,
        result: AgentTurnResult,
        turn_id: str,
        event_start: int,
        metrics: dict[str, object],
    ) -> None:
        if not self.settings.agent.memory_extraction_enabled:
            return
        latest_user = self.history.latest_user_text() or ""
        events = [
            event
            for event in self.event_bus.events()[event_start:]
            if event.turn_id == turn_id
        ]
        submitted = self.memory_extractor.submit(
            MemoryExtractionJob(
                turn_id=turn_id,
                user_input=latest_user,
                result_status=result.status,
                metrics=metrics,
                events=events,
                existing_summary=self.turn_memory_store.summary(max_chars=2400),
            ),
            on_result=self._record_model_memory_extraction,
        )
        if submitted:
            self.event_bus.emit(
                "memory_extraction_queued",
                {"turn_id": turn_id, "events": len(events)},
                turn_id,
            )

    def _record_model_memory_extraction(
        self,
        job: MemoryExtractionJob,
        extraction: MemoryExtractionResult,
    ) -> None:
        usage = extraction.usage
        if usage:
            self.token_usage.record(
                TokenUsage(
                    prompt_tokens=int(usage.get("prompt_tokens") or 0),
                    completion_tokens=int(usage.get("completion_tokens") or 0),
                    total_tokens=int(usage.get("total_tokens") or 0),
                    cached_prompt_tokens=int(usage.get("cached_prompt_tokens") or 0),
                    cache_hit_prompt_tokens=int(usage.get("cache_hit_prompt_tokens") or 0),
                    cache_miss_prompt_tokens=int(usage.get("cache_miss_prompt_tokens") or 0),
                    estimated=False,
                )
            )
        if not extraction.should_write and not extraction.rollout_summary:
            return
        records = self.turn_memory_store.record_model_memories(
            turn_id=job.turn_id,
            user_input=job.user_input,
            rollout_summary=extraction.rollout_summary,
            memories=[
                {
                    "kind": item.kind,
                    "title": item.title,
                    "content": item.content,
                    "scope": item.scope,
                    "tags": item.tags,
                    "confidence": item.confidence,
                    "reuse_rule": item.reuse_rule,
                    "evidence": item.evidence,
                }
                for item in extraction.memories
            ],
        )
        workflow_records = self._record_extracted_workflows(job, extraction)
        if not records and not workflow_records:
            return
        payload = {
            "turn_id": job.turn_id,
            "count": len(records),
            "records": [record.to_dict() for record in records[:8]],
            "workflow_count": len(workflow_records),
            "workflows": [workflow.to_dict() for workflow in workflow_records[:5]],
            "memory_summary_path": str(self.turn_memory_store.summary_path),
            "memory_path": str(self.turn_memory_store.memory_md_path),
            "workflow_memory_path": str(self.workflow_memory_store.path),
            "usage": usage,
        }
        self.logger.event("model_memory_consolidated", payload)
        self.event_bus.emit("model_memory_consolidated", payload, job.turn_id)

    def _record_extracted_workflows(
        self,
        job: MemoryExtractionJob,
        extraction: MemoryExtractionResult,
    ) -> list[Any]:
        workflows: list[Any] = []
        for item in extraction.memories:
            if item.kind != "workflow":
                continue
            notes_parts = [item.content]
            if item.reuse_rule:
                notes_parts.append(f"Reuse rule: {item.reuse_rule}")
            if item.evidence:
                notes_parts.append(f"Evidence: {item.evidence}")
            workflow = self.workflow_memory_store.remember(
                title=item.title,
                cwd=item.cwd or ".",
                command=item.command,
                purpose=item.purpose or "workflow",
                ready_check=item.ready_check,
                steps=item.steps or [item.content],
                ports=item.ports,
                tags=item.tags,
                scope=item.scope,
                confidence=item.confidence,
                source=f"memory_extractor:{job.turn_id}",
                notes="\n".join(part for part in notes_parts if part),
            )
            workflows.append(workflow)
        return workflows

    def _build_turn_metrics(
        self,
        turn_id: str,
        started_at: float,
        event_start: int,
    ) -> dict[str, object]:
        elapsed_seconds = round(max(0.0, time.monotonic() - started_at), 1)
        events = [
            event
            for event in self.event_bus.events()[event_start:]
            if event.turn_id == turn_id
        ]
        event_counts = Counter(event.type for event in events)
        tool_calls: Counter[str] = Counter()
        tool_failures: Counter[str] = Counter()
        required_retries: Counter[str] = Counter()
        verification_failures = 0
        browser_failures = 0
        model_seconds = _paired_event_seconds(events, "model_call_started", "model_call_finished")
        tool_seconds = _paired_event_seconds(events, "tool_call_started", "tool_call_finished")
        verification_seconds = _paired_event_seconds(
            events, "verification_started", {"verification_passed", "verification_failed", "failure_pack_created"}
        )
        for event in events:
            payload = event.payload
            if event.type == "tool_call_started":
                tool_calls[str(payload.get("name") or "tool")] += 1
            elif event.type == "tool_call_finished":
                name = str(payload.get("name") or "tool")
                if not payload.get("ok"):
                    tool_failures[name] += 1
                    if name == "browser_test":
                        browser_failures += 1
            elif event.type == "required_tool_retry":
                required_retries[str(payload.get("expected_action") or "unknown")] += 1
            elif event.type == "verification_step_finished" and not payload.get("ok"):
                verification_failures += 1
        metrics: dict[str, object] = {
            "elapsed_seconds": elapsed_seconds,
            "model_calls": int(event_counts["model_call_started"]),
            "model_seconds": round(model_seconds, 1),
            "tool_calls": int(event_counts["tool_call_started"]),
            "tool_seconds": round(tool_seconds, 1),
            "tool_failures": int(sum(tool_failures.values())),
            "tool_calls_by_name": dict(tool_calls.most_common(8)),
            "tool_failures_by_name": dict(tool_failures.most_common(8)),
            "browser_failures": browser_failures,
            "verification_runs": int(event_counts["verification_started"]),
            "verification_failures": verification_failures,
            "verification_seconds": round(verification_seconds, 1),
            "context_compactions": int(event_counts["context_compaction_finished"]),
            "required_retries": dict(required_retries.most_common(8)),
        }
        self.logger.event("turn_metrics", metrics)
        return metrics

    def _run_agent_cycle(
        self,
        *,
        turn_id: str,
        user_input: str,
        expected_action: str | None,
        turn_scope: TurnScopeDecision | None,
        goal_turn,
        requested_toolchain: str | None,
        diagnostic_decision,
        extra_context_messages: list[ChatMessage] | None,
    ) -> AgentTurnResult:
        final_text = ""
        no_tool_retry_used = False
        file_change_retry_used = False
        did_use_tool = False
        deferred_work_retry_count = 0
        toolchain_install_retry_used = False
        unresolved_tool_failure = False
        unresolved_tool_failure_retry_used = False
        dependency_install_attempted: set[str] = set()
        diagnostic_retry_used = False
        entrypoint_retry_used = False
        startup_fact_retry_used = False
        startup_fact_inspection_batches = 0
        inspection_guidance_used = False
        inspection_only_batches = 0
        seen_inspection_targets: set[str] = set()
        step_evidence_retry_signature: str | None = None
        active_goal_reconcile_retries = 0
        verification_model_decision_retries = 0
        if self.state.goal_status == "active":
            self.event_bus.emit(
                "goal_active",
                {
                    "objective": self.state.active_goal,
                    "status": self.state.goal_status,
                    "turn_scope": turn_scope.turn_scope if turn_scope else "none",
                    "goal_engagement": turn_scope.goal_engagement if turn_scope else "none",
                },
                turn_id,
            )
        status_review_background_only = bool(
            turn_scope
            and turn_scope.turn_scope == "status_review"
            and turn_scope.goal_engagement == "background_only"
        )
        if diagnostic_decision is not None and diagnostic_decision.should_diagnose:
            self.history.add_runtime(self._diagnostic_first_message(user_input, diagnostic_decision.reason))
            self._checkpoint("diagnostic_first_added")
            self.event_bus.emit(
                "required_tool_retry",
                {"expected_action": "diagnose_before_answer", "reason": diagnostic_decision.reason},
                turn_id,
            )

        while True:
            if self._cancel_event.is_set():
                return self._cancelled_result(turn_id)
            repaired_messages = self.history.repair_tool_call_sequence()
            if repaired_messages:
                self.logger.write(f"history tool-call sequence repaired before context count={repaired_messages}")
                self._checkpoint("history_tool_call_sequence_repaired")
            self.logger.write("context build started")
            self.logger.write(
                "context budget "
                f"trigger_auto_compact_limit_tokens={self.settings.context.budget_tokens} "
                f"post_compact_ratio={self.settings.context.compression_threshold} "
                f"baseline_ratio={self.settings.context.baseline_ratio}"
            )
            runtime_summary_started_at = time.monotonic()
            runtime_summary = self._runtime_summary()
            self.logger.write(
                "runtime summary built "
                f"elapsed_seconds={time.monotonic() - runtime_summary_started_at:.3f}"
            )
            tool_schemas = self.tools.schemas()
            tool_schema_tokens = estimate_static_payload_tokens(tool_schemas)
            context_started_at = time.monotonic()
            main_context_buffer_exists = self.context_buffer_store.exists(self.session_id, channel="main")
            compaction_callback = None
            if not main_context_buffer_exists:
                compaction_callback = lambda messages, limit: self._compact_context_with_model(
                    messages,
                    limit,
                    turn_id,
                )
            context = self.context_manager.build_context(
                history=self.history,
                extra_messages=extra_context_messages,
                runtime_summary=runtime_summary,
                token_budget=self.settings.context.budget_tokens,
                compression_threshold=self.settings.context.compression_threshold,
                baseline_ratio=self.settings.context.baseline_ratio,
                runtime_summary_token_budget=self.settings.context.runtime_summary_token_budget,
                runtime_section_token_budget=self.settings.context.runtime_section_token_budget,
                tool_result_raw_keep=self.settings.context.tool_result_raw_keep,
                tool_result_summary_chars=self.settings.context.tool_result_summary_chars,
                static_overhead_tokens=tool_schema_tokens,
                compaction_callback=compaction_callback,
            )
            self.logger.write(
                "context manager build finished "
                f"elapsed_seconds={time.monotonic() - context_started_at:.3f}"
            )
            if context.compaction_checkpoint is not None:
                replacement_history = context.compaction_checkpoint.metadata.get("replacement_history")
                source_messages = context.compaction_checkpoint.metadata.get("source_messages")
                if isinstance(replacement_history, list) and isinstance(source_messages, list):
                    self.history.add_compaction_checkpoint(
                        summary=context.compaction_checkpoint.content,
                        replacement_history=[
                            message for message in replacement_history if isinstance(message, ChatMessage)
                        ],
                        source_messages=[
                            message for message in source_messages if isinstance(message, ChatMessage)
                        ],
                    )
                    self._checkpoint("context_compaction_checkpoint_added")
                    self.event_bus.emit(
                        "context_compacted",
                        {
                            "replacement_messages": len(replacement_history),
                            "source_messages": len(source_messages),
                        },
                        turn_id,
                    )
            if self._cancel_event.is_set():
                return self._cancelled_result(turn_id)
            model_messages = self._messages_for_main_model_request(
                turn_id=turn_id,
                context=context,
                extra_context_messages=extra_context_messages,
                static_overhead_tokens=tool_schema_tokens,
            )
            model_message_estimated_tokens = estimate_messages_tokens(model_messages)
            model_request_estimated_tokens = model_message_estimated_tokens + tool_schema_tokens
            self.event_bus.emit(
                "context_built",
                {
                    "estimated_tokens": model_request_estimated_tokens,
                    "message_estimated_tokens": model_message_estimated_tokens,
                    "built_context_estimated_tokens": context.estimated_tokens,
                    "built_context_message_estimated_tokens": context.message_estimated_tokens,
                    "tool_schema_tokens": tool_schema_tokens,
                    "static_overhead_tokens": context.static_overhead_tokens,
                    "runtime_summary_tokens": context.runtime_summary_tokens,
                    "runtime_section_tokens": context.runtime_section_tokens or {},
                    "cleared_tool_results": context.cleared_tool_results,
                    "model_messages": len(model_messages),
                    "built_messages": len(context.messages),
                },
                turn_id,
            )
            self.logger.write(
                "model call started "
                f"messages={len(model_messages)} "
                f"built_messages={len(context.messages)} "
                f"tools={len(tool_schemas)} "
                f"estimated_tokens={model_request_estimated_tokens} "
                f"message_estimated_tokens={model_message_estimated_tokens} "
                f"built_context_estimated_tokens={context.estimated_tokens} "
                f"built_context_message_estimated_tokens={context.message_estimated_tokens} "
                f"tool_schema_tokens={tool_schema_tokens} "
                f"runtime_summary_tokens={context.runtime_summary_tokens} "
                f"cleared_tool_results={context.cleared_tool_results}"
            )
            self._dump_model_context(
                turn_id=turn_id,
                context=context,
                runtime_summary=runtime_summary,
                messages=model_messages,
                estimated_tokens=model_request_estimated_tokens,
            )
            self.event_bus.emit(
                "model_call_started",
                {
                    "model": self.settings.model.model,
                    "messages": len(model_messages),
                    "tools": len(tool_schemas),
                },
                turn_id,
            )
            response = self.model.complete(
                request=ModelRequest(
                    messages=model_messages,
                    tools=tool_schemas,
                    model=self.settings.model.model,
                    runtime_context=runtime_summary,
                )
            )
            if self._cancel_event.is_set():
                return self._cancelled_result(turn_id)
            self.logger.write(f"model call returned tool_calls={len(response.tool_calls)}")
            self.token_usage.record(response.usage)
            self.event_bus.emit(
                "token_usage_recorded",
                {
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "total_tokens": response.usage.total_tokens,
                    "cached_prompt_tokens": response.usage.cached_prompt_tokens,
                    "cache_hit_prompt_tokens": response.usage.cache_hit_prompt_tokens,
                    "cache_miss_prompt_tokens": response.usage.cache_miss_prompt_tokens,
                    "cache_hit_ratio": response.usage.cache_hit_ratio,
                    "estimated": response.usage.estimated,
                },
                turn_id,
            )
            self.logger.event(
                "model_usage",
                {
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "total_tokens": response.usage.total_tokens,
                    "cached_prompt_tokens": response.usage.cached_prompt_tokens,
                    "cache_hit_prompt_tokens": response.usage.cache_hit_prompt_tokens,
                    "cache_miss_prompt_tokens": response.usage.cache_miss_prompt_tokens,
                    "cache_hit_ratio": response.usage.cache_hit_ratio,
                    "estimated": response.usage.estimated,
                },
            )
            self.event_bus.emit("model_call_finished", {"tool_calls": len(response.tool_calls)}, turn_id)
            if response.tool_calls:
                response.message.metadata["tool_calls"] = [
                    call.to_model_dict() for call in response.tool_calls
                ]
                self.event_bus.emit(
                    "model_action_plan",
                    {
                        "tool_calls": len(response.tool_calls),
                        "actions": [
                            {
                                "name": call.name,
                                "summary": _tool_call_summary(call.name, call.arguments),
                            }
                            for call in response.tool_calls[:8]
                        ],
                        "truncated": len(response.tool_calls) > 8,
                    },
                    turn_id,
                )
            if response.message.content.strip():
                self.event_bus.emit(
                    "assistant_progress",
                    {"content": response.message.content.strip()[:1000]},
                    turn_id,
                )
            self.history.add(response.message)
            self._checkpoint("assistant_message_added")
            final_text = response.message.content

            if response.tool_calls:
                pending_view_images: list[dict[str, object]] = []
                batch_tool_facts: list[str] = []
                retry_after_tool_failure = False
                batch_did_write = False
                batch_changed_files: list[str] = []
                batch_made_verification_progress = False
                batch_inspection_only = _tool_calls_are_inspection_only(response.tool_calls)
                if not batch_inspection_only:
                    inspection_only_batches = 0
                for call in response.tool_calls:
                    if self._cancel_event.is_set():
                        return self._cancelled_result(turn_id)
                    did_use_tool = True
                    if self.state.goal_status == "active" and not goal_turn.requested and (
                        _tool_call_engages_active_goal(call.name)
                        or bool(turn_scope and turn_scope.engages_goal)
                    ):
                        goal_turn = self.goal_controller.start_turn(engaged=True)
                    self.event_bus.emit(
                        "tool_call_started",
                        {"name": call.name, "summary": _tool_call_summary(call.name, call.arguments)},
                        turn_id,
                    )
                    self.logger.event(
                        "tool_call_args",
                        {"name": call.name, "arguments": call.arguments},
                    )
                    result = self.tools.execute(call.name, call.arguments)
                    if self._cancel_event.is_set():
                        return self._cancelled_result(turn_id)
                    self.logger.event(
                        "tool_call_result",
                        {
                            "name": call.name,
                            "ok": result.ok,
                            "blocked": result.blocked,
                            "did_write": result.did_write,
                            "changed_files": result.changed_files,
                            "content": result.content[:1000],
                        },
                    )
                    self.history.add_tool_result(call.id, result.content, name=call.name)
                    self._record_workspace_observation(call.name, call.arguments, result)
                    batch_tool_facts.append(
                        self._tool_result_fact(call.name, call.arguments, result)
                    )
                    if not result.ok or result.blocked:
                        self._record_tool_failure_evidence(call.name, call.arguments, result)
                    image = result.metadata.get("image")
                    if call.name == "view_image" and result.ok and isinstance(image, dict):
                        pending_view_images.append(image)
                    self._checkpoint("tool_result_added")
                    self.event_bus.emit(
                        "tool_call_finished",
                        {
                            "name": call.name,
                            "summary": _tool_call_summary(call.name, call.arguments),
                            "ok": result.ok,
                            "blocked": result.blocked,
                            "content_excerpt": "" if result.ok else result.content[:500],
                            "failure_kind": result.metadata.get("failure_kind"),
                            "failure_stage": result.metadata.get("failure_stage"),
                            "failure_summary": result.metadata.get("failure_summary"),
                        },
                        turn_id,
                    )
                    goal_event = result.metadata.get("goal_event")
                    if isinstance(goal_event, str):
                        goal_payload = result.metadata.get("goal") or self.state.goal_snapshot()
                        if isinstance(goal_payload, dict):
                            for key in ("step", "evidence_records"):
                                value = result.metadata.get(key)
                                if value is not None:
                                    goal_payload[key] = value
                        self.event_bus.emit(
                            f"goal_{goal_event}",
                            goal_payload,
                            turn_id,
                        )
                        goal_turn = self.goal_controller.start_turn(engaged=True)
                    lesson_event = result.metadata.get("lesson_event")
                    if isinstance(lesson_event, str):
                        self.event_bus.emit(
                            f"lesson_{lesson_event}",
                            {
                                "lesson": result.metadata.get("lesson"),
                                "lessons": result.metadata.get("lessons"),
                            },
                            turn_id,
                        )
                    project_memory_event = result.metadata.get("project_memory_event")
                    if isinstance(project_memory_event, str):
                        self.event_bus.emit(
                            f"project_memory_{project_memory_event}",
                            {
                                "memory": result.metadata.get("memory"),
                                "memories": result.metadata.get("memories"),
                                "memory_id": result.metadata.get("memory_id"),
                                "deleted": result.metadata.get("deleted"),
                            },
                            turn_id,
                        )
                    workflow_memory_event = result.metadata.get("workflow_memory_event")
                    if isinstance(workflow_memory_event, str):
                        self.event_bus.emit(
                            f"workflow_memory_{workflow_memory_event}",
                            {
                                "workflow": result.metadata.get("workflow"),
                                "workflows": result.metadata.get("workflows"),
                                "workflow_id": result.metadata.get("workflow_id"),
                                "deleted": result.metadata.get("deleted"),
                            },
                            turn_id,
                        )
                    if result.did_write:
                        batch_did_write = True
                        self.state.did_write = True
                        self.state.changed_files.extend(result.changed_files)
                        batch_changed_files.extend(result.changed_files)
                        for changed_file in result.changed_files:
                            diff = result.metadata.get("diff")
                            if isinstance(diff, dict) and diff.get("path") == changed_file:
                                self.event_bus.emit("file_diff", diff, turn_id)
                            self.event_bus.emit(
                                "file_changed",
                                {"path": changed_file, "tool": call.name},
                                turn_id,
                            )
                    target_failure = self._failure_target_tool_failure(call.name, call.arguments, result)
                    if target_failure is not None:
                        tool_failure = self._handle_tool_failure(call.name, call.arguments, target_failure, turn_id)
                        if tool_failure is not None:
                            return tool_failure
                        unresolved_tool_failure = True
                        retry_after_tool_failure = True
                        break
                    if result.ok:
                        self._record_runtime_resource(call.name, call.arguments, result)
                        self._record_tool_acceptance_evidence(call.name, call.arguments, result)
                        self._record_failure_target_evidence(call.name, call.arguments, result)
                        if _tool_result_counts_as_verification_progress(call.name, result):
                            batch_made_verification_progress = True
                    if not result.ok and not result.blocked:
                        tool_failure = self._handle_tool_failure(call.name, call.arguments, result, turn_id)
                        if tool_failure is not None:
                            return tool_failure
                        unresolved_tool_failure = True
                        retry_after_tool_failure = True
                        break
                    if result.ok:
                        unresolved_tool_failure = False
                    if result.blocked and self._is_recoverable_tool_error(call.name, result):
                        path_facts = _path_failure_facts_message(result.content)
                        if path_facts:
                            payload = _path_failure_payload(result.content) or {}
                            requested_path = str(payload.get("requested_path") or "")
                            parent = str(payload.get("parent") or "")
                            siblings = payload.get("existing_siblings")
                            sibling_names = [
                                str(item.get("name"))
                                for item in siblings
                                if isinstance(item, dict) and isinstance(item.get("name"), str)
                            ] if isinstance(siblings, list) else []
                            self.state.add_engineering_fact(
                                type="missing_path",
                                source=requested_path,
                                summary=f"missing path {requested_path}; parent={parent}; siblings={', '.join(sibling_names[:10])}",
                                data={
                                    "requested_path": requested_path,
                                    "parent": parent,
                                    "parent_exists": payload.get("parent_exists"),
                                    "siblings": sibling_names[:20],
                                },
                                confidence="high",
                            )
                            self.history.add_runtime(path_facts)
                            self._checkpoint("path_failure_facts_added")
                            self.event_bus.emit(
                                "path_failure_facts",
                                {"name": call.name, "reason": result.block_reason or result.content[:300]},
                                turn_id,
                            )
                        else:
                            self.history.add_runtime(
                                "The previous tool call failed, but this is recoverable. "
                                "Do not stop. If a file path does not exist, treat that as evidence and inspect "
                                "nearby directories once instead of repeatedly reading the same missing path. "
                                "Then continue with a concrete action, verification command, or a focused blocker. "
                                "If edit_file failed because old_text was not found, call read_file on the "
                                "target file and retry with an exact current snippet. "
                                "If run_command was blocked because the command is long-running, retry with "
                                "start_background_command and wait for the expected port."
                            )
                            self._checkpoint("recoverable_tool_error_added")
                        self.event_bus.emit(
                            "recoverable_tool_error",
                            {"name": call.name, "reason": result.block_reason or result.content[:300]},
                            turn_id,
                        )
                        unresolved_tool_failure = True
                        retry_after_tool_failure = True
                        break
                    if result.blocked:
                        self._record_tool_result_facts(batch_tool_facts, turn_id)
                        self.state.result_state = "blocked"
                        if result.permission_request_id:
                            self.state.result_state = "waiting_permission"
                            self.event_bus.emit(
                                "permission_requested",
                                {
                                    "permission_request_id": result.permission_request_id,
                                    "reason": result.block_reason,
                                },
                                turn_id,
                            )
                        return AgentTurnResult(
                            status="blocked",
                            response=result.block_reason or result.content,
                            changed_files=self.state.all_changed_files(),
                            token_usage=self.token_usage.total(),
                        )
                if retry_after_tool_failure:
                    self._record_tool_result_facts(batch_tool_facts, turn_id)
                    continue
                self._record_tool_result_facts(batch_tool_facts, turn_id)
                if batch_made_verification_progress:
                    verification_model_decision_retries = 0
                    self._model_selected_verification_since_write = (
                        self._model_selected_verification_since_write
                        or (
                            bool(self._pending_verification_changed_files)
                            and any(
                                _tool_result_satisfies_model_selected_verification(call.name)
                                for call in response.tool_calls
                            )
                        )
                    )
                    self.logger.event(
                        "verification_model_decision_retries_reset",
                        {"reason": "tool progress", "tool_calls": len(response.tool_calls)},
                    )
                for image in pending_view_images:
                    self.history.add_user_image(
                        f"[view_image attached {image.get('relative_path') or image.get('path')}]",
                        path=str(image.get("path") or ""),
                        mime_type=str(image.get("mime_type") or "application/octet-stream"),
                        detail=str(image.get("detail") or "low"),
                    )
                if pending_view_images:
                    self._checkpoint("view_image_added")
                if batch_did_write:
                    self._pending_verification_changed_files = _dedupe_keep_order(batch_changed_files)
                    self._model_selected_verification_since_write = False
                    self.history.add_runtime(
                        "Files were changed by the previous tool batch. Runtime will run verification now "
                        "before allowing further model exploration."
                    )
                    self._checkpoint("verify_after_write_added")
                    self.event_bus.emit(
                        "required_tool_retry",
                        {"expected_action": "verify_after_write"},
                        turn_id,
                    )
                else:
                    step_evidence_signature = self._current_step_unreconciled_evidence_signature()
                    if (
                        goal_turn.requested
                        and step_evidence_signature
                        and step_evidence_signature != step_evidence_retry_signature
                    ):
                        step_evidence_retry_signature = step_evidence_signature
                        self.history.add_runtime(self._current_step_evidence_message())
                        self._checkpoint("step_evidence_reconciliation_added")
                        self.event_bus.emit(
                            "required_tool_retry",
                            {"expected_action": "reconcile_work_step_evidence"},
                            turn_id,
                        )
                        continue
                    if goal_turn.requested and batch_inspection_only:
                        startup_fact_inspection_batches += 1
                        inspection_targets = _inspection_targets_from_tool_calls(response.tool_calls)
                        introduces_new_targets = any(
                            target not in seen_inspection_targets for target in inspection_targets
                        )
                        if inspection_targets:
                            seen_inspection_targets.update(inspection_targets)
                        if introduces_new_targets:
                            inspection_only_batches = 0
                            inspection_guidance_used = False
                        else:
                            inspection_only_batches += 1
                        startup_facts = None
                        if not startup_fact_retry_used and startup_fact_inspection_batches >= 2:
                            startup_facts = self._startup_action_facts_message()
                        if startup_facts:
                            startup_fact_retry_used = True
                            self.history.add_runtime(startup_facts)
                            self._checkpoint("startup_action_facts_added")
                            self.event_bus.emit(
                                "required_tool_retry",
                                {
                                    "expected_action": "act_on_startup_facts",
                                    "batches": inspection_only_batches,
                                },
                                turn_id,
                            )
                        if (
                            not startup_facts
                            and not inspection_guidance_used
                            and inspection_only_batches >= 3
                        ):
                            inspection_guidance_used = True
                            self.history.add_runtime(
                                _inspection_progress_guidance(
                                    inspection_only_batches,
                                    inspection_targets,
                                )
                            )
                            self._checkpoint("inspection_guidance_added")
                            self.event_bus.emit(
                                "inspection_guidance_added",
                                {
                                    "batches": inspection_only_batches,
                                    "targets": inspection_targets[:8],
                                },
                                turn_id,
                            )
                    else:
                        startup_fact_inspection_batches = 0
                        inspection_only_batches = 0
                    continue

            if unresolved_tool_failure:
                if not unresolved_tool_failure_retry_used:
                    unresolved_tool_failure_retry_used = True
                    self._request_corrigibility_reflection(
                        reason="recoverable tool failure was not retried",
                        evidence=(
                            "A recoverable tool failure is unresolved. Continue with tools: fix the command, "
                            "cwd, path, arguments, or environment, then rerun the specific failed operation. "
                            "If it cannot be corrected after retrying, report blocked."
                        ),
                        turn_id=turn_id,
                    )
                    self.event_bus.emit(
                        "required_tool_retry",
                        {"expected_action": "resolve_tool_failure"},
                        turn_id,
                    )
                    continue
                self.state.result_state = "blocked"
                self.event_bus.emit(
                    "blocked",
                    {"reason": "unresolved recoverable tool failure"},
                    turn_id,
                )
                return AgentTurnResult(
                    status="blocked",
                    response=(
                        "A recoverable tool failure was not resolved. "
                        "The agent must retry the failed operation with corrected tools before claiming progress."
                    ),
                    changed_files=self.state.all_changed_files(),
                    token_usage=self.token_usage.total(),
                )

            if not self.state.did_write:
                if self.state.unresolved_failure_targets():
                    if not entrypoint_retry_used:
                        entrypoint_retry_used = True
                        self.history.add_runtime(self._failure_target_message())
                        self._checkpoint("failure_target_retry_added")
                        self.event_bus.emit(
                            "required_tool_retry",
                            {
                                "expected_action": "verify_reported_failure_target",
                                "targets": [target.to_dict() for target in self.state.unresolved_failure_targets()],
                            },
                            turn_id,
                        )
                        continue
                    auto_result = self._auto_verify_failure_target(turn_id)
                    if auto_result is not None:
                        return auto_result
                    continue
                if (
                    diagnostic_decision is not None
                    and diagnostic_decision.should_diagnose
                    and not diagnostic_retry_used
                    and (not did_use_tool or assistant_is_asking_user_for_diagnostics(final_text))
                ):
                    diagnostic_retry_used = True
                    self.history.add_runtime(self._diagnostic_first_message(user_input, "assistant asked user before local diagnosis"))
                    self._checkpoint("diagnostic_no_tool_guard_added")
                    self.event_bus.emit(
                        "required_tool_retry",
                        {"expected_action": "diagnose_locally_with_tools"},
                        turn_id,
                    )
                    continue
                if requested_toolchain and not toolchain_install_retry_used:
                    toolchain_install_retry_used = True
                    self.history.add_runtime(
                        "The user explicitly requested toolchain installation. Continue by calling "
                        f"inspect_toolchain(name='{requested_toolchain}') and then "
                        f"install_toolchain(name='{requested_toolchain}') if it is missing. "
                        "Do not answer with manual installation instructions unless the install tool fails."
                    )
                    self._checkpoint("toolchain_install_retry_added")
                    self.event_bus.emit(
                        "required_tool_retry",
                        {"expected_action": "install_toolchain", "name": requested_toolchain},
                        turn_id,
                    )
                    continue
                if expected_action == "modify_and_verify" and not no_tool_retry_used:
                    no_tool_retry_used = True
                    self.history.add_runtime(
                        "This turn requires modifying existing project files and verification. "
                        "Your previous response did not call tools or change files. Continue by "
                        "inspecting the workspace, reading tests/source files, editing the relevant "
                        "file, and letting verification run."
                    )
                    self._checkpoint("required_tool_retry_added")
                    self.event_bus.emit("required_tool_retry", {"expected_action": expected_action}, turn_id)
                    continue
                if _user_requests_file_change(user_input) and not did_use_tool and not file_change_retry_used:
                    file_change_retry_used = True
                    self.history.add_runtime(
                        "The latest user message asks for a concrete file change. Do not answer only with "
                        "agreement or a promise. Use write_file/edit_file/delete_file as appropriate for the "
                        "requested path, then let runtime verification run. Treat this as the current user "
                        "turn, not as permission to resume an older active goal."
                    )
                    self._checkpoint("file_change_retry_added")
                    self.event_bus.emit(
                        "required_tool_retry",
                        {"expected_action": "perform_requested_file_change"},
                        turn_id,
                    )
                    continue
                goal_decision = self.goal_controller.no_write_decision(goal_turn)
                if (
                    not status_review_background_only
                    and goal_decision
                    and goal_decision.should_continue
                ):
                    self.history.add_runtime(goal_decision.runtime_message)
                    self._checkpoint("goal_controller_no_write_retry_added")
                    self.event_bus.emit(
                        "required_tool_retry",
                        {"expected_action": goal_decision.expected_action},
                        turn_id,
                    )
                    continue
                goal_decision = self.goal_controller.plan_completion_review_decision(goal_turn)
                if (
                    not status_review_background_only
                    and goal_decision
                    and goal_decision.should_continue
                ):
                    self.history.add_runtime(goal_decision.runtime_message)
                    self._checkpoint("goal_controller_completion_review_added")
                    self.event_bus.emit(
                        "required_tool_retry",
                        {
                            "expected_action": goal_decision.expected_action,
                            "count": goal_turn.completion_review_retries,
                        },
                        turn_id,
                    )
                    continue
                if (
                    not status_review_background_only
                    and (
                    goal_turn.requested
                    and goal_turn.verified_steps > 0
                    and self.state.goal_status == "active"
                    )
                ):
                    if active_goal_reconcile_retries < 4:
                        active_goal_reconcile_retries += 1
                        self.history.add_runtime(
                            "The active goal is still open after verified progress. Do not end with a "
                            "status update only. Continue with the next concrete tool action, update the "
                            "current WorkPlan step with evidence, call update_goal(status='complete') if "
                            "all required work is actually complete, or call update_goal(status='blocked') "
                            "with a concrete blocker."
                        )
                        self._checkpoint("active_goal_reconcile_added")
                        self.event_bus.emit(
                            "required_tool_retry",
                            {
                                "expected_action": "reconcile_active_goal_after_verified_progress",
                                "count": active_goal_reconcile_retries,
                            },
                            turn_id,
                        )
                        continue
                    self.state.result_state = "blocked"
                    self.event_bus.emit(
                        "blocked",
                        {"reason": "active goal was not reconciled after verified progress"},
                        turn_id,
                    )
                    return AgentTurnResult(
                        status="blocked",
                        response=(
                            "The active goal still needs reconciliation after verified progress. "
                            "The model must continue work, update the work plan, complete the goal, "
                            "or report a concrete blocker."
                        ),
                        changed_files=self.state.all_changed_files(),
                            token_usage=self.token_usage.total(),
                        )
                if (
                    not status_review_background_only
                    and (
                    goal_turn.requested
                    and self.state.goal_status == "active"
                    and self.state.work_plan
                    and goal_turn.completion_review_retries >= 4
                    )
                ):
                    self.state.result_state = "blocked"
                    self.event_bus.emit(
                        "blocked",
                        {"reason": "active goal was not reconciled after work-plan review"},
                        turn_id,
                    )
                    return AgentTurnResult(
                        status="blocked",
                        response=(
                            "The active goal still needs work-plan review. The model must continue "
                            "concrete work, extend or update the plan, complete the goal, or report "
                            "a concrete blocker."
                        ),
                        changed_files=self.state.all_changed_files(),
                        token_usage=self.token_usage.total(),
                    )
                if (
                    not status_review_background_only
                    and goal_turn.requested
                    and goal_turn.verified_steps > 0
                ):
                    self.event_bus.emit(
                        "goal_turn_finished",
                        {"objective": goal_turn.objective, "status": self.state.goal_status},
                        turn_id,
                    )
                self.state.result_state = "completed"
                self.event_bus.emit("turn_finished", {"status": "completed"}, turn_id)
                return AgentTurnResult(
                    status="completed",
                    response=final_text,
                    changed_files=self.state.all_changed_files(),
                    token_usage=self.token_usage.total(),
                )

            if _assistant_deferred_work_after_changes(final_text) and deferred_work_retry_count < 8:
                deferred_work_retry_count += 1
                self.history.add_runtime(
                    "You changed project files, then ended by asking for confirmation or promising future work. "
                    "Do not stop after a promise to continue. Continue with tools now: inspect, implement the next "
                    "necessary change, run a stronger project-specific verification command, or call update_goal only "
                    "if the whole objective is genuinely complete or blocked."
                )
                self._checkpoint("deferred_work_retry_added")
                self.event_bus.emit(
                    "required_tool_retry",
                    {
                        "expected_action": "continue_after_deferred_work",
                        "count": deferred_work_retry_count,
                    },
                    turn_id,
                )
                continue

            if self.state.unresolved_failure_targets():
                if not entrypoint_retry_used:
                    entrypoint_retry_used = True
                    self.history.add_runtime(self._failure_target_message())
                    self._checkpoint("failure_target_retry_added")
                    self.event_bus.emit(
                        "required_tool_retry",
                        {
                            "expected_action": "verify_reported_failure_target",
                            "targets": [target.to_dict() for target in self.state.unresolved_failure_targets()],
                        },
                        turn_id,
                    )
                    continue
                auto_result = self._auto_verify_failure_target(turn_id)
                if auto_result is not None:
                    return auto_result
                continue

            verification_result = self._verify(turn_id)
            if verification_result.passed:
                self._record_acceptance_evidence(verification_result)
                goal_decision = self.goal_controller.verification_passed_decision(
                    goal_turn,
                    plan_reason=verification_result.plan.reason,
                )
                if goal_decision and goal_decision.should_continue:
                    self.state.begin_next_goal_step()
                    self.history.add_runtime(goal_decision.runtime_message)
                    self._checkpoint("goal_controller_verified_step_added")
                    self.event_bus.emit(
                        "required_tool_retry",
                        {"expected_action": goal_decision.expected_action},
                        turn_id,
                    )
                    continue
                self.state.result_state = "passed"
                self.event_bus.emit("turn_finished", {"status": "passed"}, turn_id)
                return AgentTurnResult(
                    status="passed",
                    response=final_text,
                    changed_files=self.state.all_changed_files(),
                    token_usage=self.token_usage.total(),
                )
            if verification_result.blocked and verification_result.plan.requires_model_decision:
                verification_model_decision_retries += 1
                if verification_model_decision_retries > 3:
                    self.state.result_state = "blocked"
                    self.event_bus.emit(
                        "blocked",
                        {
                            "reason": "verification needed model-selected command but no tool progress was made",
                            "expected_action": "decide_project_specific_verification",
                            "retries": verification_model_decision_retries - 1,
                        },
                        turn_id,
                    )
                    return AgentTurnResult(
                        status="blocked",
                        response=(
                            "Verification needs a project-specific command, but the model did not make "
                            "tool progress after repeated prompts."
                        ),
                        changed_files=self.state.all_changed_files(),
                        token_usage=self.token_usage.total(),
                    )
                self.history.add_runtime(
                    "Verification could not choose a safe default command.\n"
                    f"Reason: {verification_result.plan.reason}\n"
                    "Inspect the relevant project scripts/tests and choose the next "
                    "verification step yourself before continuing."
                )
                self._checkpoint("verification_model_decision_added")
                self.event_bus.emit(
                    "required_tool_retry",
                    {
                        "expected_action": "decide_project_specific_verification",
                        "reason": verification_result.plan.reason,
                    },
                    turn_id,
                )
                continue

            failure_pack = FailurePack.from_verification(verification_result, user_goal=user_input)
            self.event_bus.emit("failure_pack_created", failure_pack.to_dict(), turn_id)
            dependency_result = self._maybe_install_missing_dependency(
                failure_pack,
                verification_root=self._infer_verification_root(),
                attempted=dependency_install_attempted,
                turn_id=turn_id,
            )
            if dependency_result == "installed":
                verification_result = self._verify(turn_id)
                if verification_result.passed:
                    self.state.result_state = "passed"
                    self.event_bus.emit("turn_finished", {"status": "passed"}, turn_id)
                    return AgentTurnResult(
                        status="passed",
                        response=final_text,
                        changed_files=self.state.all_changed_files(),
                        token_usage=self.token_usage.total(),
                    )
                failure_pack = FailurePack.from_verification(verification_result, user_goal=user_input)
                self.event_bus.emit("failure_pack_created", failure_pack.to_dict(), turn_id)
            if isinstance(dependency_result, CommandResult):
                self.state.result_state = "blocked"
                self.event_bus.emit(
                    "blocked",
                    {"reason": "dependency installation failed"},
                    turn_id,
                )
                return AgentTurnResult(
                    status="blocked",
                    response=(
                        "Dependency installation failed.\n"
                        f"Command: {dependency_result.command}\n"
                        f"stdout:\n{dependency_result.stdout}\n"
                        f"stderr:\n{dependency_result.stderr}"
                    ),
                    changed_files=self.state.all_changed_files(),
                    failure_pack=failure_pack,
                    token_usage=self.token_usage.total(),
                )
            decision = self.continuation_planner.decide(
                failure_pack=failure_pack,
                repair_round=self.state.repair_count(failure_pack.signature),
                max_repair_rounds=self.settings.max_repair_rounds,
                previous_signature=self.state.previous_failure_signature,
            )
            if decision.action == "repair_now":
                repair_round = self.state.record_repair_attempt(failure_pack.signature)
                self.history.add_runtime(failure_pack.to_runtime_message())
                self._checkpoint("runtime_failure_added")
                if repair_round > 1:
                    self._request_corrigibility_reflection(
                        reason=f"verification failure repair round {repair_round}",
                        evidence=failure_pack.first_blocking_failure,
                        turn_id=turn_id,
                    )
                self.event_bus.emit(
                    "repair_round_started",
                    {"round": repair_round, "reason": decision.reason},
                    turn_id,
                )
                continue
            self.state.result_state = "blocked"
            self.event_bus.emit("blocked", {"reason": decision.reason}, turn_id)
            return AgentTurnResult(
                status="blocked",
                response=_blocked_response(decision.reason, failure_pack),
                changed_files=self.state.all_changed_files(),
                failure_pack=failure_pack,
                token_usage=self.token_usage.total(),
            )

    def _verify(self, turn_id: str):
        self.event_bus.emit("verification_started", {}, turn_id)
        verification_root = self._infer_verification_root()
        self.logger.write(f"verification_root={verification_root}")
        changed_files = self._pending_verification_changed_files or self.state.changed_files
        self.logger.event(
            "verification_changed_files_selected",
            {
                "source": "pending_batch" if self._pending_verification_changed_files else "session_accumulated",
                "changed_files": changed_files,
            },
        )
        plan = self.code_runtime.build_verification_plan(verification_root, changed_files)
        if plan.requires_model_decision and self._model_selected_verification_since_write:
            plan = VerificationPlan(
                steps=[],
                reason=(
                    "model-selected verification already passed after the latest write; "
                    f"changed_files={', '.join(changed_files) if changed_files else 'none'}"
                ),
                confidence=0.8,
            )
            result = VerificationResult(plan=plan, step_results=[], passed=True)
            self.event_bus.emit("verification_plan_created", plan.summary(), turn_id)
            self.event_bus.emit("verification_passed", {"source": "model_selected_tool"}, turn_id)
            self._pending_verification_changed_files = []
            self._model_selected_verification_since_write = False
            return result
        if verification_root != self.settings.workspace_root:
            relative_root = verification_root.relative_to(self.settings.workspace_root).as_posix()
            for step in plan.steps:
                if step.cwd == ".":
                    step.cwd = relative_root
        self.event_bus.emit("verification_plan_created", plan.summary(), turn_id)
        verification_guidance = self._verification_guidance_message(
            verification_root=verification_root,
            changed_files=changed_files,
            plan=plan,
        )
        if plan.requires_model_decision:
            self.history.add_runtime(verification_guidance)
            self._checkpoint("verification_guidance_added")
        result = self.code_runtime.run_verification(plan, event_bus=self.event_bus, turn_id=turn_id)
        if result.passed:
            self.event_bus.emit("verification_passed", {}, turn_id)
            self._pending_verification_changed_files = []
        elif result.blocked and result.plan.requires_model_decision:
            self.event_bus.emit(
                "verification_needs_model_decision",
                {"reason": result.plan.reason},
                turn_id,
            )
        else:
            self.history.add_runtime(verification_guidance)
            self._checkpoint("verification_guidance_added")
            self.event_bus.emit("verification_failed", {}, turn_id)
        return result

    def _verification_guidance_message(
        self,
        *,
        verification_root: Path,
        changed_files: list[str],
        plan: VerificationPlan,
    ) -> str:
        legacy_guidance = (
            "Verification needs code-task judgment. Match verification to changed files and project scripts. "
            "Do not treat unrelated syntax/build checks as acceptance evidence for user-facing behavior."
        )
        return self._render_skill_hook(
            domain="code",
            hook="verification_guidance",
            legacy_content=legacy_guidance,
            variables={
                "verification_root": str(verification_root),
                "changed_files": changed_files[-12:],
                "plan_reason": plan.reason,
                "plan_steps": [
                    {
                        "name": step.name,
                        "type": step.step_type,
                        "command": step.command,
                        "cwd": step.cwd,
                        "url": step.url,
                    }
                    for step in plan.steps[:6]
                ],
                "requires_model_decision": plan.requires_model_decision,
            },
        )

    def _render_skill_hook(
        self,
        *,
        domain: str,
        hook: str,
        legacy_content: str,
        variables: dict[str, object] | None = None,
    ) -> str:
        rendered = self.skill_registry.render_hook_with_metadata(
            domain=domain,
            hook=hook,
            legacy_content=legacy_content,
            variables=variables,
        )
        self.logger.event("skill_hook_rendered", rendered.metadata)
        self.event_bus.emit("skill_hook_rendered", rendered.metadata)
        return rendered.content

    def _classify_turn_scope(self, user_input: str, turn_id: str) -> TurnScopeDecision:
        goal_snapshot = self.state.goal_snapshot()
        compact_goal = {
            "objective": goal_snapshot.get("objective"),
            "status": goal_snapshot.get("status"),
            "current_step_id": goal_snapshot.get("current_step_id"),
            "work_plan_sources": goal_snapshot.get("work_plan_sources", [])[:6],
            "recent_archived_goals": [
                {
                    "objective": item.get("objective"),
                    "status": item.get("status"),
                    "archive_reason": item.get("archive_reason"),
                    "work_plan_sources": item.get("work_plan_sources", [])[:4],
                }
                for item in goal_snapshot.get("archived_goals", [])[-4:]
                if isinstance(item, dict)
            ],
            "work_plan": [
                {
                    "id": item.get("id"),
                    "title": item.get("title"),
                    "status": item.get("status"),
                    "required": item.get("required"),
                }
                for item in goal_snapshot.get("work_plan", [])[:12]
                if isinstance(item, dict)
            ],
        }
        prompt = (
            "Classify only the latest user message. Decide how this turn relates to the "
            "current or recently archived long-running coding goal. Do not solve the task. "
            "Return compact JSON only.\n\n"
            "Allowed turn_scope values:\n"
            "- chat_only: ordinary conversation; no tools needed.\n"
            "- answer_with_tools: answer a local/project question with tools, but do not advance the goal.\n"
            "- status_review: inspect goal/work-plan/documents/evidence as needed, then summarize progress/status instead of continuing implementation.\n"
            "- side_task: a temporary task that may use tools, but is not part of the active goal.\n"
            "- continue_current_goal: the user asks to continue/resume/finish the active goal.\n"
            "- modify_current_goal: the user asks to update or extend the current/archived project goal or work plan, often by changing the same design/plan document.\n"
            "- create_new_goal: the user asks for a new long-running goal.\n"
            "- pause_goal: the user asks to pause/stop the active goal.\n"
            "- complete_goal: the user says the active goal is complete.\n\n"
            "Allowed goal_engagement values:\n"
            "- none: no relation to the active goal.\n"
            "- background_only: the active goal may be context, but this turn must not auto-continue it.\n"
            "- engaged: this turn should actively advance the current goal and may continue the work plan.\n"
            "- update_only: this turn only updates goal/plan state and should not continue implementation.\n\n"
            "Important rules:\n"
            "- An active goal existing is not enough for engaged.\n"
            "- If no active goal exists but the latest message asks to implement, add, fix, test, or continue a project requirement, do not classify as chat_only. Use modify_current_goal when it likely extends a recent archived project/design document; use create_new_goal when it is a separate new milestone/project.\n"
            "- If a recent archived goal has the same project/design document and the user adds a requirement to that project, classify as modify_current_goal. Use goal_engagement='engaged' when the user wants implementation/testing now; use 'update_only' only when the user merely asks to save or revise the plan.\n"
            "- Questions about files/status are usually answer_with_tools + background_only.\n"
            "- Questions about project progress, completion status, remaining work, or alignment with a design/plan document are usually status_review + background_only.\n"
            "- Requests like 'continue', 'keep going', 'finish the project' are continue_current_goal + engaged.\n"
            "- Requests to save/change only the goal or plan are modify_current_goal + update_only.\n"
            "- Return JSON with keys: turn_scope, goal_engagement, reason."
        )
        payload = json.dumps(
            {
                "active_goal": compact_goal,
                "latest_user_message": user_input,
            },
            ensure_ascii=False,
        )
        self.event_bus.emit("turn_scope_classification_started", {}, turn_id)
        try:
            response = self.model.complete(
                request=ModelRequest(
                    messages=[
                        ChatMessage(role="system", content=prompt),
                        ChatMessage(role="user", content=payload),
                    ],
                    tools=[],
                    model=self.settings.model.model,
                    runtime_context={"purpose": "turn_scope_classification"},
                )
            )
            self.token_usage.record(response.usage)
            decision = _parse_turn_scope_decision(response.message.content)
        except Exception as exc:
            self.logger.write(f"turn scope classification failed error={exc}")
            decision = _fallback_turn_scope_decision(user_input)
        self.logger.event(
            "turn_scope_classified",
            {
                "turn_scope": decision.turn_scope,
                "goal_engagement": decision.goal_engagement,
                "reason": decision.reason[:300],
            },
        )
        self.event_bus.emit(
            "turn_scope_classified",
            {
                "turn_scope": decision.turn_scope,
                "goal_engagement": decision.goal_engagement,
                "reason": decision.reason[:300],
            },
            turn_id,
        )
        return decision

    def _compact_context_with_model(
        self,
        messages: list[ChatMessage],
        limit: int,
        turn_id: str,
    ) -> str | None:
        self.logger.write(
            f"context compaction started messages={len(messages)} target_summary_chars={limit}"
        )
        self.event_bus.emit(
            "context_compaction_started",
            {"messages": len(messages), "target_summary_chars": limit},
            turn_id,
        )
        try:
            summary = self.compaction_manager.compact(messages, max_chars=limit)
        except Exception as exc:
            self.logger.write(f"context compaction failed error={exc}")
            self.event_bus.emit(
                "context_compaction_failed",
                {"error": str(exc)},
                turn_id,
            )
            return None
        self.logger.write(f"context compaction finished chars={len(summary)}")
        self.event_bus.emit(
            "context_compaction_finished",
            {"chars": len(summary)},
            turn_id,
        )
        return summary

    def _messages_for_main_model_request(
        self,
        *,
        turn_id: str,
        context: Any,
        extra_context_messages: list[ChatMessage] | None,
        static_overhead_tokens: int = 0,
    ) -> list[ChatMessage]:
        """Return the persisted main-channel context that should be sent.

        The model cache works best when the request prefix is an append-only
        stream. `build_context()` is still used to initialize or repair the
        snapshot, but ordinary follow-up model calls read the persisted buffer
        instead of re-assembling volatile runtime sections each time.
        """
        channel = "main"
        buffer_exists = self.context_buffer_store.exists(self.session_id, channel=channel)
        path = self.context_buffer_store.path_for(self.session_id, channel=channel)
        wire_path = self.context_buffer_store.wire_path_for(self.session_id, channel=channel)
        existing_messages = self.context_buffer_store.load(self.session_id, channel=channel) if buffer_exists else []
        missing_runtime_anchor = (
            buffer_exists
            and not _has_context_runtime_anchor(existing_messages)
            and _has_context_runtime_anchor(context.messages)
        )
        suspiciously_small_buffer = (
            buffer_exists
            and len(existing_messages) <= 1
            and len(context.messages) >= 8
            and _has_context_runtime_anchor(context.messages)
        )
        initial_messages = _normalize_runtime_messages_for_main_context(context.messages)
        force_compaction_rewrite = context.compaction_checkpoint is not None
        should_reinitialize = (
            not buffer_exists
            or missing_runtime_anchor
            or suspiciously_small_buffer
            or force_compaction_rewrite
            # Existing buffers are intentionally append-only because rewriting
            # them on every control/runtime pass destroys provider prefix-cache
            # reuse. Compaction is the deliberate exception: once
            # build_context() creates a checkpoint, the persisted wire buffer
            # must be rewritten to the compacted replacement history. Otherwise
            # the model request keeps reading the old append-only buffer and
            # never actually shrinks, even though ChatHistory has recorded a
            # compaction checkpoint.
        )
        reason = "existing"
        if should_reinitialize:
            if not buffer_exists:
                reason = "initial"
            elif force_compaction_rewrite:
                reason = "compaction_rewrite"
            elif missing_runtime_anchor:
                reason = "repair_missing_runtime_anchor"
            elif suspiciously_small_buffer:
                reason = "repair_suspiciously_small_buffer"
            path = self.context_buffer_store.save(
                self.session_id,
                initial_messages,
                channel=channel,
                metadata={
                    "turn_id": turn_id,
                    "reason": reason,
                    "filtered_runtime_messages": len(context.messages) - len(initial_messages),
                    "estimated_tokens": context.estimated_tokens,
                    "message_estimated_tokens": context.message_estimated_tokens,
                    "runtime_summary_tokens": context.runtime_summary_tokens,
                    "cleared_tool_results": context.cleared_tool_results,
                },
            )
        elif extra_context_messages:
            appended = 0
            for message in extra_context_messages:
                converted = _normalize_runtime_message_for_main_context(
                    message,
                    allow_initial=False,
                )
                if converted is None:
                    continue
                content_hash = hashlib.sha256(converted.content.encode("utf-8")).hexdigest()[:16]
                key = f"{turn_id}:{converted.role}:{content_hash}"
                if key in self._context_buffer_extra_message_keys:
                    continue
                self._context_buffer_extra_message_keys.add(key)
                self.context_buffer_store.append(
                    self.session_id,
                    converted,
                    channel=channel,
                    metadata={
                        "turn_id": turn_id,
                        "source": "extra_context",
                        "original_message_role": message.role,
                    },
                )
                appended += 1
            self.logger.event(
                "context_buffer_extra_messages_appended",
                {
                    "channel": channel,
                    "turn_id": turn_id,
                    "messages": appended,
                    "reason": "synthetic_user_runtime_observation",
                },
            )
        self.logger.event(
            "context_buffer_written",
            {
                "channel": channel,
                "reason": reason,
                "messages": len(context.messages),
                "path": str(path),
                "wire_path": str(wire_path),
            },
        )
        repair_count = self.context_buffer_store.repair_tool_call_sequence(
            self.session_id,
            channel=channel,
        )
        if repair_count:
            self.logger.event(
                "context_buffer_tool_sequence_repaired",
                {
                    "channel": channel,
                    "repairs": repair_count,
                    "turn_id": turn_id,
                },
            )
            self.logger.write(
                f"context buffer tool-call sequence repaired channel={channel} repairs={repair_count}"
            )
        messages = self.context_buffer_store.load(self.session_id, channel=channel)
        if not messages:
            self.logger.write("context buffer load returned empty; falling back to built context")
            return _normalize_runtime_messages_for_main_context(context.messages)
        messages = _normalize_runtime_messages_for_main_context(messages)
        final_estimated_tokens = estimate_messages_tokens(messages) + max(0, int(static_overhead_tokens))
        if final_estimated_tokens > self.settings.context.budget_tokens:
            self.logger.event(
                "context_buffer_final_compaction_needed",
                {
                    "channel": channel,
                    "turn_id": turn_id,
                    "estimated_tokens": final_estimated_tokens,
                    "trigger_tokens": self.settings.context.budget_tokens,
                    "messages": len(messages),
                },
            )
            compacted_messages, checkpoint = self.context_manager.compact_messages(
                messages,
                token_budget=self.settings.context.budget_tokens,
                static_overhead_tokens=static_overhead_tokens,
                compression_threshold=self.settings.context.compression_threshold,
                baseline_ratio=self.settings.context.baseline_ratio,
                runtime_summary_token_budget=self.settings.context.runtime_summary_token_budget,
                runtime_section_token_budget=self.settings.context.runtime_section_token_budget,
                compaction_callback=lambda compact_messages, limit: self._compact_context_with_model(
                    compact_messages,
                    limit,
                    turn_id,
                ),
                tool_result_raw_keep=self.settings.context.tool_result_raw_keep,
                tool_result_summary_chars=self.settings.context.tool_result_summary_chars,
            )
            messages = _normalize_runtime_messages_for_main_context(compacted_messages)
            path = self.context_buffer_store.save(
                self.session_id,
                messages,
                channel=channel,
                metadata={
                    "turn_id": turn_id,
                    "reason": "final_buffer_compaction_rewrite",
                    "estimated_tokens_before": final_estimated_tokens,
                    "estimated_tokens_after": estimate_messages_tokens(messages)
                    + max(0, int(static_overhead_tokens)),
                    "source_messages": len(compacted_messages),
                    "checkpoint_created": checkpoint is not None,
                },
            )
            reason = "final_buffer_compaction_rewrite"
            if checkpoint is not None:
                self.logger.event(
                    "context_buffer_final_compaction_checkpoint",
                    {
                        "channel": channel,
                        "turn_id": turn_id,
                        "source_messages": len(
                            checkpoint.metadata.get("source_messages")
                            if isinstance(checkpoint.metadata.get("source_messages"), list)
                            else []
                        ),
                        "replacement_messages": len(
                            checkpoint.metadata.get("replacement_history")
                            if isinstance(checkpoint.metadata.get("replacement_history"), list)
                            else []
                        ),
                    },
                )
            self.logger.event(
                "context_buffer_written",
                {
                    "channel": channel,
                    "reason": reason,
                    "messages": len(messages),
                    "path": str(path),
                    "wire_path": str(wire_path),
                },
            )
            repair_count = self.context_buffer_store.repair_tool_call_sequence(
                self.session_id,
                channel=channel,
            )
            if repair_count:
                self.logger.event(
                    "context_buffer_tool_sequence_repaired",
                    {
                        "channel": channel,
                        "repairs": repair_count,
                        "turn_id": turn_id,
                    },
                )
                messages = _normalize_runtime_messages_for_main_context(
                    self.context_buffer_store.load(self.session_id, channel=channel)
                )
        self.logger.write(
            f"context buffer read channel={channel} reason={reason} messages={len(messages)}"
        )
        self._log_context_buffer_wire_snapshot(wire_path, channel=channel, reason=reason)
        return messages

    def _log_context_buffer_wire_snapshot(self, path: Path, *, channel: str, reason: str) -> None:
        try:
            data = path.read_bytes()
        except OSError:
            return
        digest = hashlib.sha256(data[:65536]).hexdigest()[:16]
        self.logger.event(
            "context_buffer_wire_snapshot",
            {
                "channel": channel,
                "reason": reason,
                "bytes": len(data),
                "prefix64k_sha256": digest,
                "path": str(path),
            },
        )

    def _dump_model_context(
        self,
        *,
        turn_id: str,
        context,
        runtime_summary: dict[str, Any],
        messages: list[ChatMessage] | None = None,
        estimated_tokens: int | None = None,
    ) -> None:
        if not self.settings.agent.context_dump_enabled:
            return
        self._context_dump_index += 1
        dump_dir = self.settings.artifact_root / "logs" / "context"
        try:
            dump_dir.mkdir(parents=True, exist_ok=True)
            text = _format_model_context_dump(
                session_id=self.session_id,
                turn_id=turn_id,
                call_index=self._context_dump_index,
                model=self.settings.model.model,
                messages=messages or context.messages,
                tool_count=len(self.tools.schemas()),
                estimated_tokens=estimated_tokens or context.estimated_tokens,
                runtime_summary=runtime_summary,
                max_chars_per_message=self.settings.agent.context_dump_max_chars_per_message,
            )
            path = dump_dir / f"context_{turn_id}_{self._context_dump_index:04d}.md"
            latest = dump_dir / "latest.md"
            path.write_text(text, encoding="utf-8")
            latest.write_text(text, encoding="utf-8")
            self.logger.write(f"context dump written path={path}")
        except OSError as exc:
            self.logger.write(f"context dump failed error={exc}")

    def _record_acceptance_evidence(self, verification_result) -> None:
        plan_reason = getattr(verification_result.plan, "reason", "")
        if plan_reason:
            evidence = f"verification passed: {plan_reason}"
            record = self.state.add_evidence_record(
                kind="verification",
                source_tool="VerificationRunner",
                summary=evidence,
                detail=repr(verification_result.plan.summary()),
            )
            self.state.add_acceptance_evidence(evidence, evidence_id=record.id)
            self._record_evidence_event(record)
        for step_result in verification_result.step_results:
            command = step_result.step.command
            if step_result.ok and command:
                evidence = f"command passed: {command}"
                record = self.state.add_evidence_record(
                    kind="command",
                    source_tool="VerificationRunner",
                    summary=evidence,
                    detail=_verification_step_detail(step_result),
                    command=command,
                    exit_code=getattr(step_result.command_result, "exit_code", None),
                )
                self.state.add_acceptance_evidence(evidence, evidence_id=record.id)
                self._record_evidence_event(record)

    def _record_evidence_event(self, record) -> None:
        lines = [
            "[EVIDENCE EVENT]",
            (
                f"- id={record.id}; kind={record.kind}; source_tool={record.source_tool}; "
                f"status={record.status}; summary={_compact_summary(record.summary, 360)}"
            ),
        ]
        if record.command:
            lines.append(f"- command={_compact_summary(record.command, 260)}")
        if record.url:
            lines.append(f"- url={_compact_summary(record.url, 260)}")
        if record.path:
            lines.append(f"- path={_compact_summary(record.path, 220)}")
        if record.exit_code is not None:
            lines.append(f"- exit_code={record.exit_code}")
        # Evidence is provenance, so it belongs to the append-only event stream.
        # The runtime tail only repeats a tiny recent summary to avoid rebuilding
        # a large changing evidence block on every model request.
        self.history.add_runtime("\n".join(lines))
        self._checkpoint("evidence_event_added")

    def _record_tool_result_facts(self, facts: list[str], turn_id: str) -> None:
        compact_facts = [fact for fact in facts if fact.strip()]
        if not compact_facts:
            return
        # Cache experiment: do not inject derived tool-result facts into the
        # main model message stream. The raw tool role messages already carry
        # the immediate observation, and adding a fresh runtime/system block
        # after every tool batch makes it harder to reason about prefix-cache
        # behavior. Keep the structured facts in events/logs for diagnostics.
        self.event_bus.emit(
            "tool_result_facts_recorded",
            {"count": len(compact_facts), "facts": compact_facts[-12:]},
            turn_id,
        )

    def _request_corrigibility_reflection(
        self,
        *,
        reason: str,
        turn_id: str,
        evidence: str = "",
    ) -> None:
        lines = [
            "[CORRIGIBILITY REFLECTION]",
            "Before continuing, apply the corrigibility principle: seek truth through evidence and preserve the ability to revise wrong assumptions.",
            f"- Trigger: {reason}",
        ]
        if evidence:
            lines.append(f"- Current evidence: {_compact_summary(evidence, 700)}")
        lines.extend(
            [
                "- Answer these briefly in your next assistant progress note, then choose concrete tools:",
                "  1. What assumption may be wrong?",
                "  2. What evidence do we already have?",
                "  3. What evidence is missing?",
                "  4. Are we adding a special-case patch instead of improving perception, tooling, memory, feedback, or verification?",
                "  5. What smallest next action reduces uncertainty?",
                "- If the lesson is reusable, call record_lesson after evidence confirms it.",
            ]
        )
        self.history.add_runtime("\n".join(lines))
        self._checkpoint("corrigibility_reflection_added")
        self.event_bus.emit(
            "reflection_requested",
            {"reason": reason, "evidence": evidence[:500]},
            turn_id,
        )

    def _tool_result_fact(
        self,
        tool_name: str,
        arguments: dict[str, object],
        result: ToolResult,
    ) -> str:
        if tool_name == "inspect_project_startup" and result.ok:
            startup_fact = _startup_report_tool_fact(result.metadata)
            if startup_fact:
                return startup_fact
        status = "ok" if result.ok else "failed"
        if result.blocked:
            status = "blocked"
        details = [
            f"tool={tool_name}",
            f"status={status}",
        ]
        if result.did_write:
            details.append("did_write=true")
        if result.changed_files:
            details.append(f"changed_files={', '.join(result.changed_files[:8])}")
        command = arguments.get("command") or result.metadata.get("command")
        if command:
            details.append(f"command={_compact_summary(str(command), 180)}")
        cwd = arguments.get("cwd") or result.metadata.get("cwd")
        if cwd:
            details.append(f"cwd={_compact_summary(str(cwd), 120)}")
        path = arguments.get("path") or result.metadata.get("path")
        if path:
            details.append(f"path={_compact_summary(str(path), 160)}")
        url = arguments.get("url") or result.metadata.get("url")
        if url:
            details.append(f"url={_compact_summary(str(url), 180)}")
        for key in ("exit_code", "status_code", "pid", "port", "ready"):
            value = result.metadata.get(key)
            if value is not None:
                details.append(f"{key}={value}")
        if result.block_reason:
            details.append(f"block_reason={_compact_summary(result.block_reason, 220)}")
        failure_summary = result.metadata.get("failure_summary")
        if isinstance(failure_summary, str) and failure_summary.strip():
            details.append(f"failure_summary={_compact_summary(failure_summary, 240)}")
        failure_kind = _classify_tool_result_failure(tool_name, arguments, result)
        if failure_kind:
            details.append(f"failure_kind={failure_kind}")
        command_normalization = result.metadata.get("command_normalization")
        if isinstance(command_normalization, str) and command_normalization.strip():
            details.append(f"command_normalization={_compact_summary(command_normalization, 220)}")
        memory_candidate = _project_memory_candidate_note(tool_name, arguments, result)
        if memory_candidate:
            details.append(f"memory_candidate={_compact_summary(memory_candidate, 520)}")
        log_tail = result.metadata.get("log_tail")
        if isinstance(log_tail, str) and log_tail.strip():
            details.append(f"log_tail={_compact_summary(log_tail, 320)}")
        excerpt = _compact_tool_fact_output(tool_name, result)
        if excerpt:
            details.append(f"output={excerpt}")
        return "; ".join(details)

    def _record_workspace_observation(
        self,
        tool_name: str,
        arguments: dict[str, object],
        result: ToolResult,
    ) -> None:
        path_arg = arguments.get("path")
        cwd_arg = arguments.get("cwd")
        if tool_name == "list_directory" and result.ok:
            root = str(path_arg or ".").replace("\\", "/").strip("/")
            root = "" if root == "." else root
            try:
                entries = json.loads(result.content)
            except json.JSONDecodeError:
                entries = []
            if isinstance(entries, list):
                for entry in entries[:80]:
                    if not isinstance(entry, dict):
                        continue
                    name = entry.get("name")
                    kind = entry.get("type")
                    entry_path = entry.get("path")
                    if isinstance(entry_path, str) and entry_path:
                        observed_path = entry_path
                    elif isinstance(name, str):
                        observed_path = f"{root}/{name}".strip("/")
                    else:
                        continue
                    self.state.observe_workspace_entry(
                        path=observed_path,
                        kind=str(kind or "unknown"),
                        source_tool=tool_name,
                        size=entry.get("size") if isinstance(entry.get("size"), int) else None,
                    )
            return
        if tool_name in {"read_file", "view_image"}:
            path = str(path_arg or result.metadata.get("path") or "")
            if not path and isinstance(result.metadata.get("image"), dict):
                image = result.metadata["image"]
                path = str(image.get("relative_path") or image.get("path") or "")
            if path:
                if result.ok:
                    self.state.observe_workspace_entry(
                        path=path,
                        kind="file",
                        source_tool=tool_name,
                        size=len(result.content),
                    )
                    self.state.record_read(
                        path=path,
                        source_tool=tool_name,
                        size=(
                            result.metadata.get("size")
                            if isinstance(result.metadata.get("size"), int)
                            else len(result.content)
                        ),
                        excerpt=result.content,
                        content_hash=str(result.metadata.get("sha256") or ""),
                        mtime=(
                            result.metadata.get("mtime")
                            if isinstance(result.metadata.get("mtime"), (int, float))
                            else None
                        ),
                        truncated=bool(result.metadata.get("truncated")),
                        line_count=_tool_result_line_count(result),
                        start_line=_tool_result_start_line(result),
                        end_line=_tool_result_end_line(result),
                    )
                else:
                    self.state.add_engineering_fact(
                        type="missing_path",
                        source=path,
                        summary=f"{tool_name} failed for path {path}",
                        data={"path": path, "error": result.block_reason or result.content[:300]},
                        confidence="high",
                    )
            return
        if result.did_write:
            action = _change_action_for_tool(tool_name)
            for changed_file in result.changed_files:
                self.state.observe_workspace_entry(
                    path=changed_file,
                    kind="file",
                    source_tool=tool_name,
                )
                self.state.record_change(
                    path=changed_file,
                    source_tool=tool_name,
                    action=action,
                    summary=result.content,
                )
            return
        if tool_name in {"run_command", "run_python"}:
            for changed_file in result.metadata.get("changed_files", []) or []:
                if isinstance(changed_file, str):
                    self.state.observe_workspace_entry(
                        path=changed_file,
                        kind="file",
                        source_tool=tool_name,
                    )
                    self.state.record_change(
                        path=changed_file,
                        source_tool=tool_name,
                        action="changed_by_command",
                        summary=str(arguments.get("command") or result.metadata.get("command") or ""),
                    )
            command = arguments.get("command") or result.metadata.get("command")
            if command:
                self.state.add_runtime_resource(
                    kind="command",
                    source_tool=tool_name,
                    command=str(command),
                    cwd=str(cwd_arg or result.metadata.get("cwd") or "."),
                    exit_code=result.metadata.get("exit_code") if isinstance(result.metadata.get("exit_code"), int) else None,
                    status="passed" if result.ok else "failed",
                    summary=f"command {command} exit_code={result.metadata.get('exit_code')}",
                    data={
                        "timed_out": result.metadata.get("timed_out"),
                        "duration_seconds": result.metadata.get("duration_seconds"),
                        "purpose": result.metadata.get("purpose") or arguments.get("purpose") or "",
                    },
                )
                self.state.add_engineering_fact(
                    type="command",
                    source=str(cwd_arg or result.metadata.get("cwd") or "."),
                    summary=f"command observed: {command}",
                    data={
                        "command": str(command),
                        "cwd": str(cwd_arg or result.metadata.get("cwd") or "."),
                        "exit_code": result.metadata.get("exit_code"),
                        "timed_out": result.metadata.get("timed_out"),
                    },
                    confidence="medium",
                    stale=False,
                )
            return
        if tool_name == "http_request":
            url = str(arguments.get("url") or result.metadata.get("url") or "")
            status_code = result.metadata.get("status_code")
            self.state.add_runtime_resource(
                kind="http",
                source_tool=tool_name,
                url=url,
                method=str(result.metadata.get("method") or arguments.get("method") or "GET"),
                status_code=status_code if isinstance(status_code, int) else None,
                status="passed" if result.ok else "failed",
                summary=(
                    f"HTTP {result.metadata.get('method') or arguments.get('method') or 'GET'} {url} "
                    f"status={status_code} ok={result.ok}"
                ),
                data={
                    "ok": result.ok,
                    "error": result.metadata.get("error") or "",
                    "purpose": result.metadata.get("purpose") or arguments.get("purpose") or "",
                },
            )
            self.state.add_engineering_fact(
                type="http_observation",
                source=url,
                summary=(
                    f"HTTP {result.metadata.get('method') or arguments.get('method') or 'GET'} {url} "
                    f"status={result.metadata.get('status_code')} ok={result.ok}"
                ),
                data={
                    "url": url,
                    "method": result.metadata.get("method") or arguments.get("method") or "GET",
                    "status_code": result.metadata.get("status_code"),
                    "ok": result.ok,
                    "error": result.metadata.get("error") or "",
                },
                confidence="high",
                stale=False,
            )
            return
        if tool_name == "browser_test":
            url = str(arguments.get("url") or result.metadata.get("requested_url") or "")
            final_url = str(result.metadata.get("final_url") or url)
            title = str(result.metadata.get("title") or "")
            screenshot_path = str(result.metadata.get("screenshot_path") or "")
            self.state.add_runtime_resource(
                kind="browser",
                source_tool=tool_name,
                url=final_url,
                method="BROWSER",
                status="passed" if result.ok else "failed",
                summary=f"browser_test url={final_url} title={title!r} ok={result.ok}",
                data={
                    "requested_url": url,
                    "title": title,
                    "page_errors": result.metadata.get("page_errors") or [],
                    "failed_requests": result.metadata.get("failed_requests") or [],
                    "console_errors": result.metadata.get("console_errors") or [],
                    "screenshot_path": screenshot_path,
                    "purpose": result.metadata.get("purpose") or arguments.get("purpose") or "",
                },
            )
            self.state.add_engineering_fact(
                type="browser_observation",
                source=final_url or url,
                summary=f"browser_test observed {final_url or url} title={title!r} ok={result.ok}",
                data={
                    "requested_url": url,
                    "final_url": final_url,
                    "title": title,
                    "page_errors": result.metadata.get("page_errors") or [],
                    "failed_requests": result.metadata.get("failed_requests") or [],
                    "console_errors": result.metadata.get("console_errors") or [],
                    "screenshot_path": screenshot_path,
                },
                confidence="high",
                stale=False,
            )
            return
        if tool_name in {"inspect_port", "release_port"}:
            port = arguments.get("port") or result.metadata.get("port")
            if isinstance(port, int):
                self.state.add_runtime_resource(
                    kind="port",
                    source_tool=tool_name,
                    port=port,
                    status="passed" if result.ok else "failed",
                    summary=f"{tool_name} port={port} ok={result.ok}",
                    data=dict(result.metadata),
                )

    def _evidence_pack(self) -> dict[str, list[str]]:
        latest_tool_facts: list[str] = []
        for message in reversed(self.history.messages()):
            if message.role == "runtime" and "[TOOL RESULT FACTS]" in message.content:
                latest_tool_facts.extend(
                    line.removeprefix("- ").strip()
                    for line in message.content.splitlines()
                    if line.strip().startswith("- tool=")
                )
            if len(latest_tool_facts) >= 12:
                break
        latest_failures = [
            record.summary
            for record in self.state.evidence_records
            if record.status != "passed" or "failure" in record.kind.lower()
        ][-12:]
        latest_commands = [
            resource.summary or f"command {resource.command} exit_code={resource.exit_code}"
            for resource in self.state.runtime_resources
            if resource.kind == "command"
        ][-12:]
        latest_http = [
            resource.summary or f"HTTP {resource.method} {resource.url} status={resource.status_code}"
            for resource in self.state.runtime_resources
            if resource.kind == "http"
        ][-12:]
        latest_changes = [
            f"{record.path}: {record.action} via {record.source_tool}"
            for record in self.state.change_records[-12:]
        ]
        latest_reads = [
            f"{record.path} via {record.source_tool}"
            for record in self.state.read_records[-12:]
        ]
        active_resources = [
            f"{resource.kind}: summary={resource.summary}; command={resource.command}; url={resource.url}; cwd={resource.cwd}; port={resource.port}; status={resource.status}; ready={resource.ready}"
            for resource in self.state.runtime_resources[-12:]
        ]
        return {
            "latest_tool_facts": latest_tool_facts[-12:],
            "latest_failures": latest_failures,
            "latest_commands": latest_commands,
            "latest_http": latest_http,
            "latest_changes": latest_changes,
            "latest_reads": latest_reads,
            "active_resources": active_resources,
        }

    def _record_tool_failure_evidence(
        self,
        tool_name: str,
        arguments: dict[str, object],
        result: ToolResult,
    ) -> None:
        summary = self._tool_result_fact(tool_name, arguments, result)
        command = str(arguments.get("command") or result.metadata.get("command") or "")
        url = str(arguments.get("url") or result.metadata.get("url") or "")
        path = str(arguments.get("path") or result.metadata.get("path") or "")
        exit_code = result.metadata.get("exit_code")
        record = self.state.add_evidence_record(
            kind="tool_failure",
            source_tool=tool_name,
            summary=summary,
            detail=result.content[:2000],
            command=command,
            url=url,
            path=path,
            status="failed",
            exit_code=exit_code if isinstance(exit_code, int) else None,
        )
        self._record_evidence_event(record)

    def _record_tool_acceptance_evidence(
        self,
        tool_name: str,
        arguments: dict[str, object],
        result: ToolResult,
    ) -> None:
        if tool_name == "run_command":
            command = str(arguments.get("command") or "").strip()
            purpose = str(arguments.get("purpose") or result.metadata.get("purpose") or "generic")
            if command and purpose in _ACCEPTANCE_COMMAND_PURPOSES:
                evidence = f"command passed: {command}"
                command_result = result.metadata.get("command_result")
                record = self.state.add_evidence_record(
                    kind=purpose,
                    source_tool=tool_name,
                    summary=evidence,
                    detail=result.content[:2000],
                    command=command,
                    exit_code=getattr(command_result, "exit_code", None),
                )
                self.state.add_acceptance_evidence(evidence, evidence_id=record.id)
                self._record_evidence_event(record)
        elif tool_name == "start_background_command":
            return
        elif tool_name == "http_request":
            url = str(arguments.get("url") or result.metadata.get("url") or "").strip()
            purpose = str(arguments.get("purpose") or result.metadata.get("purpose") or "generic")
            status_code = result.metadata.get("status_code")
            if url and purpose in _ACCEPTANCE_COMMAND_PURPOSES:
                evidence = f"http_request observed: {url} status={status_code}"
                record = self.state.add_evidence_record(
                    kind=purpose,
                    source_tool=tool_name,
                    summary=evidence,
                    detail=result.content[:2000],
                    url=url,
                    status="passed",
                )
                self._record_evidence_event(record)
        elif tool_name == "browser_test":
            url = str(arguments.get("url") or result.metadata.get("requested_url") or "").strip()
            purpose = str(arguments.get("purpose") or result.metadata.get("purpose") or "generic")
            final_url = str(result.metadata.get("final_url") or url)
            title = str(result.metadata.get("title") or "")
            if url and purpose in _ACCEPTANCE_COMMAND_PURPOSES:
                evidence = f"browser_test observed: {final_url} title={title}"
                record = self.state.add_evidence_record(
                    kind=purpose,
                    source_tool=tool_name,
                    summary=evidence,
                    detail=result.content[:2000],
                    url=final_url,
                    status="passed" if result.ok else "failed",
                )
                self._record_evidence_event(record)
        elif tool_name in {"inspect_port", "find_image", "view_image"}:
            return

    def _record_runtime_resource(
        self,
        tool_name: str,
        arguments: dict[str, object],
        result: ToolResult,
    ) -> None:
        if tool_name != "start_background_command":
            return
        command = str(arguments.get("command") or result.metadata.get("command") or "").strip()
        if not command:
            return
        port = result.metadata.get("port") or arguments.get("port")
        pid = result.metadata.get("pid")
        ready = result.metadata.get("ready")
        log_path = str(result.metadata.get("log_path") or "").strip()
        resource = self.state.add_runtime_resource(
            kind="background_process",
            source_tool=tool_name,
            command=command,
            cwd=str(arguments.get("cwd") or "."),
            port=port if isinstance(port, int) else None,
            pid=pid if isinstance(pid, int) else None,
            ready=ready if isinstance(ready, bool) else None,
            log_path=log_path,
            status="ready" if ready is True else "started",
            summary=f"background process {command} ready={ready} port={port}",
            data={
                "restart_port": bool(arguments.get("restart_port")),
                "raw": dict(result.metadata),
            },
        )
        self.event_bus.emit(
            "runtime_resource_recorded",
            resource.to_dict(),
        )

    def _auto_verify_failure_target(self, turn_id: str) -> AgentTurnResult | None:
        target = next(
            (candidate for candidate in self.state.unresolved_failure_targets() if candidate.kind == "http"),
            None,
        )
        if target is None:
            self.state.result_state = "blocked"
            self.event_bus.emit(
                "blocked",
                {
                    "reason": "reported failure target was not reverified",
                    "targets": [candidate.to_dict() for candidate in self.state.unresolved_failure_targets()],
                },
                turn_id,
            )
            return AgentTurnResult(
                status="blocked",
                response=(
                    "Reported failure target was not reverified. "
                    "The agent must rerun the original or equivalent failure entrypoint before claiming completion."
                ),
                changed_files=self.state.all_changed_files(),
                token_usage=self.token_usage.total(),
            )

        method = (target.method or "GET").upper()
        arguments: dict[str, object] = {
            "url": target.entrypoint,
            "method": method,
            "purpose": "smoke",
            "timeout_seconds": 10,
        }
        if method in {"POST", "PUT", "PATCH"}:
            arguments["headers"] = {"Content-Type": "application/json"}
            arguments["body"] = "{}"
        self.history.add_runtime(
            "The model did not rerun the reported HTTP failure entrypoint. "
            "Runtime is executing the exact failure target now and will feed the result back into the repair loop."
        )
        self._checkpoint("failure_target_auto_verify_added")
        self.event_bus.emit(
            "failure_target_auto_verify_started",
            {"target": target.to_dict(), "arguments": arguments},
            turn_id,
        )
        self.event_bus.emit(
            "tool_call_started",
            {"name": "http_request", "summary": _tool_call_summary("http_request", arguments)},
            turn_id,
        )
        self.event_bus.emit(
            "tool_call_args",
            {"name": "http_request", "arguments": arguments},
            turn_id,
        )
        result = self.tools.execute("http_request", arguments)
        self.event_bus.emit(
            "tool_call_result",
            {
                "name": "http_request",
                "ok": result.ok,
                "blocked": result.blocked,
                "did_write": result.did_write,
                "changed_files": result.changed_files,
                "content": result.content[:1000],
            },
            turn_id,
        )
        self.event_bus.emit(
            "tool_call_finished",
            {
                "name": "http_request",
                "summary": _tool_call_summary("http_request", arguments),
                "ok": result.ok,
                "blocked": result.blocked,
                "content_excerpt": "" if result.ok else result.content[:500],
            },
            turn_id,
        )
        if result.ok:
            self._record_tool_acceptance_evidence("http_request", arguments, result)
            self._record_failure_target_evidence("http_request", arguments, result)
            self.history.add_runtime(
                "Reported HTTP failure target was rechecked by runtime and now succeeds.\n\n"
                f"Target: {target.entrypoint}\n"
                f"Result:\n{result.content[:2000]}"
            )
            self._checkpoint("failure_target_auto_verified")
            self.event_bus.emit(
                "failure_target_verified",
                {"target": target.to_dict(), "evidence": target.evidence},
                turn_id,
            )
            return None

        target_failure = self._failure_target_tool_failure("http_request", arguments, result)
        if target_failure is None:
            target_failure = result
        return self._handle_tool_failure("http_request", arguments, target_failure, turn_id)

    def _record_failure_target_evidence(
        self,
        tool_name: str,
        arguments: dict[str, object],
        result: ToolResult,
    ) -> None:
        purpose = str(arguments.get("purpose") or result.metadata.get("purpose") or "generic")
        if purpose not in _ACCEPTANCE_COMMAND_PURPOSES and purpose != "verify":
            return
        for target in self.state.unresolved_failure_targets():
            if target.kind == "http" and tool_name == "http_request" and result.metadata.get("url") == target.entrypoint:
                self.state.mark_failure_target_verified(
                    target.entrypoint,
                    f"http_request succeeded: status={result.metadata.get('status_code')}",
                )
            elif target.kind == "command" and tool_name == "run_command":
                command = str(arguments.get("command") or "")
                if target.entrypoint not in command and target.entrypoint not in result.content:
                    continue
                self.state.mark_failure_target_verified(
                    target.entrypoint,
                    f"{tool_name} succeeded with purpose={purpose}: {_compact_summary(command)}",
                )

    def _failure_target_tool_failure(
        self,
        tool_name: str,
        arguments: dict[str, object],
        result: ToolResult,
    ) -> ToolResult | None:
        purpose = str(arguments.get("purpose") or result.metadata.get("purpose") or "generic")
        if purpose not in _ACCEPTANCE_COMMAND_PURPOSES and purpose != "verify":
            return None
        matched_targets = self._matched_failure_targets(tool_name, arguments, result)
        if not matched_targets:
            return None
        if result.ok:
            return None
        for target in matched_targets:
            self.state.mark_failure_target_reproduced(
                target.entrypoint,
                _failure_target_result_evidence(tool_name, arguments, result),
            )
        reason = _failure_target_result_evidence(tool_name, arguments, result)
        return ToolResult(
            ok=False,
            content=(
                "Reported failure target is still failing. Continue diagnosis and repair; "
                "do not ask the user to debug manually.\n\n"
                f"Reason: {reason}\n"
                f"Tool: {tool_name}\n"
                f"Arguments: {arguments}\n"
                f"Result:\n{result.content[:4000]}"
            ),
            metadata={"failure_target_error": reason},
        )

    def _matched_failure_targets(
        self,
        tool_name: str,
        arguments: dict[str, object],
        result: ToolResult,
    ):
        matched = []
        for target in self.state.unresolved_failure_targets():
            if target.kind == "http" and tool_name == "http_request":
                if result.metadata.get("url") == target.entrypoint:
                    matched.append(target)
            elif target.kind == "command" and tool_name == "run_command":
                command = str(arguments.get("command") or "")
                if target.entrypoint in command or target.entrypoint in result.content:
                    matched.append(target)
        return matched

    def _failure_target_message(self) -> str:
        lines = [
            "The user reported a concrete failure reproduction target. Do not finish by verifying only downstream components.",
            "You may inspect dependencies and internal services, but final acceptance for this turn requires re-running the reported target or an explicitly equivalent entrypoint.",
            "Use tools now. For kind=http targets, call http_request with purpose='smoke' or purpose='verify'. For kind=command targets, call run_command with purpose='smoke' or purpose='verify'.",
            "If the target cannot be executed locally, explain the blocker with evidence.",
            "",
            "Unverified failure targets:",
        ]
        for target in self.state.unresolved_failure_targets():
            lines.append(f"- kind={target.kind}; entrypoint={target.entrypoint}; source={target.source}")
        return "\n".join(lines)

    def _current_step_has_unreconciled_evidence(self) -> bool:
        return self._current_step_unreconciled_evidence_signature() is not None

    def _current_step_unreconciled_evidence_signature(self) -> str | None:
        current = self.state.current_step()
        if current is None:
            return None
        if current.status not in {"pending", "in_progress"}:
            return None
        recent_failed = [
            record
            for record in self.state.evidence_records[-10:]
            if record.status and record.status != "passed"
        ]
        if not current.evidence and not current.evidence_ids and not recent_failed:
            return None
        payload = {
            "step_id": current.id,
            "step_status": current.status,
            "evidence": current.evidence[-8:],
            "evidence_ids": current.evidence_ids[-8:],
            "recent_failed_evidence": [
                {
                    "id": record.id,
                    "status": record.status,
                    "source_tool": record.source_tool,
                    "summary": record.summary,
                    "command": record.command,
                    "url": record.url,
                }
                for record in recent_failed[-6:]
            ],
        }
        return hashlib.sha1(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    def _current_step_evidence_message(self) -> str:
        current = self.state.current_step()
        if current is None:
            return "The current WorkPlan step needs review before continuing."
        evidence_ids = ", ".join(current.evidence_ids[-8:]) or "none"
        evidence = "\n".join(f"- {item}" for item in current.evidence[-8:]) or "- none"
        recent_failed = [
            record
            for record in self.state.evidence_records[-10:]
            if record.status and record.status != "passed"
        ]
        failure_lines = []
        for record in recent_failed[-6:]:
            source = record.command or record.url or record.path or record.source_tool
            failure_lines.append(
                f"- {record.id}: {record.source_tool} status={record.status}; "
                f"{record.summary}; source={source}"
            )
        failures = "\n".join(failure_lines) or "- none"
        return (
            "The current WorkPlan step now has concrete evidence or failed verification evidence but is not reconciled. "
            "Before continuing broad exploration, evaluate the step acceptance against this evidence. "
            "If the latest user message narrows, redirects, or corrects the active focus away from this "
            "CurrentStep, first call set_current_step or update_work_step to realign the WorkPlan; do not "
            "force this step just because it has evidence. "
            "Call update_work_step with status='done' and evidence_ids if acceptance is met; "
            "call update_work_step with status='blocked' and a blocker if progress cannot continue; "
            "or make the smallest implementation/data-setup fix and rerun one targeted acceptance check. "
            "Avoid repeatedly inventing ad hoc scripts with unknown paths/imports; inspect stable project files, "
            "reuse known workflows, or record a concrete blocker.\n\n"
            f"CurrentStep: {current.id} - {current.title}\n"
            f"Acceptance: {current.acceptance or '<none>'}\n"
            f"EvidenceIds: {evidence_ids}\n"
            f"Evidence:\n{evidence}\n"
            f"RecentFailedEvidence:\n{failures}"
        )

    def _maybe_install_missing_dependency(
        self,
        failure_pack: FailurePack,
        *,
        verification_root: Path,
        attempted: set[str],
        turn_id: str,
    ) -> str | CommandResult | None:
        if failure_pack.failure_type != "missing_dependency":
            return None
        if failure_pack.signature in attempted:
            return None
        allow_project_install = (
            self.settings.verification.allow_dependency_install
            or self.settings.permission_mode == "auto"
        )
        policy = DependencyInstallPolicy(allow_project_install=allow_project_install)
        decision = policy.decide_missing_python_dependency(
            verification_root,
            failure_pack.relevant_output,
        )
        if not decision.allowed:
            self.event_bus.emit(
                "dependency_install_skipped",
                {"reason": decision.reason, "action": decision.action},
                turn_id,
            )
            return None
        command = _python_dependency_install_command(verification_root)
        if command is None:
            self.event_bus.emit(
                "dependency_install_skipped",
                {"reason": "no supported Python dependency file found", "action": "blocked"},
                turn_id,
            )
            return None
        attempted.add(failure_pack.signature)
        self.event_bus.emit(
            "dependency_install_started",
            {"command": command, "cwd": str(verification_root), "reason": decision.reason},
            turn_id,
        )
        result = self.dependency_command_runner.run(
            command,
            verification_root,
            timeout_seconds=max(120, self.settings.timeouts.verification_command_seconds),
        )
        self.event_bus.emit(
            "dependency_install_finished",
            {
                "command": command,
                "ok": result.ok,
                "exit_code": result.exit_code,
                "stdout_excerpt": result.stdout[:1000],
                "stderr_excerpt": result.stderr[:1000],
            },
            turn_id,
        )
        if result.ok:
            self.history.add_runtime(
                "Project dependency installation completed after missing dependency failure. "
                "Retry verification before asking the user."
            )
            self._checkpoint("dependency_install_completed")
            return "installed"
        return result

    def _infer_verification_root(self) -> Path:
        workspace = self.settings.workspace_root
        candidates: list[Path] = []
        seen: set[str] = set()
        for changed_path in reversed(self.state.changed_files):
            detected = _nearest_detected_project_root(
                workspace / changed_path,
                workspace,
                self.code_runtime.detect_project,
            )
            if detected is None:
                continue
            key = str(detected.resolve()).lower()
            if key not in seen:
                candidates.append(detected)
                seen.add(key)
        if workspace.resolve().as_posix().lower() not in seen:
            candidates.append(workspace)
        first_parts = {
            Path(path).parts[0]
            for path in self.state.changed_files
            if Path(path).parts and Path(path).parts[0] not in {".minicodex2"}
        }
        if len(first_parts) == 1:
            child = workspace / next(iter(first_parts))
            if child.is_dir():
                key = str(child.resolve()).lower()
                if key not in seen:
                    candidates.append(child)
                    seen.add(key)
        for candidate in candidates:
            profile = self.code_runtime.detect_project(candidate)
            if profile.detected_types:
                return candidate
        return workspace

    def _runtime_summary(self) -> dict[str, object]:
        plan_source_status = self._work_plan_source_status()
        return {
            "environment": _runtime_environment_summary(),
            "workspace_root": str(self.settings.workspace_root),
            "project_guidance": [
                item.to_dict()
                for item in self.project_guidance_loader.load(self.settings.workspace_root)
            ],
            "project_memory_index": [
                item.to_dict()
                for item in self.project_memory_index.build(self.settings.workspace_root)
            ],
            "project_fact_memory_hot": [
                item.to_dict()
                for item in self.project_memory_store.hot(limit=5)
            ],
            "project_fact_memory_index": self.project_memory_store.index(limit=20),
            "workflow_memory_hot": [
                item.to_dict()
                for item in self.workflow_memory_store.hot(limit=5)
            ],
            "workflow_memory_index": self.workflow_memory_store.index(limit=20),
            "turn_memory_summary": self.turn_memory_store.summary(max_chars=2400),
            "skill_context": self.skill_registry.summary(),
            "pending_turn_memories": [
                item.to_dict()
                for item in self.turn_memory_store.important(
                    kinds={"pending_requirement", "product_decision"},
                    limit=10,
                )
            ],
            "changed_files": self.state.changed_files,
            "verified_files": self.state.verified_files,
            "acceptance_evidence": self.state.acceptance_evidence,
            "evidence_records": [item.to_dict() for item in self.state.evidence_records[-16:]],
            "workspace_entries": [item.to_dict() for item in self.state.workspace_entries[-40:]],
            "read_records": [item.to_dict() for item in self.state.read_records[-40:]],
            "change_records": [item.to_dict() for item in self.state.change_records[-24:]],
            "evidence_pack": self._evidence_pack(),
            "engineering_facts": [item.to_dict() for item in self.state.engineering_facts[-32:]],
            "lessons": [item.to_dict() for item in self.state.lessons[-20:]],
            "runtime_resources": [
                _runtime_resource_with_observed_health(item)
                for item in self.state.runtime_resources[-12:]
            ],
            "failure_targets": [target.to_dict() for target in self.state.failure_targets],
            "repair_round": self.state.repair_round,
            "result_state": self.state.result_state,
            "active_goal": self.state.active_goal,
            "goal_status": self.state.goal_status,
            "goal_token_budget": self.state.goal_token_budget,
            "work_plan": [item.to_dict() for item in self.state.work_plan],
            "work_plan_sources": [item.to_dict() for item in self.state.work_plan_sources],
            "work_plan_source_status": plan_source_status,
            "current_step_id": self.state.current_step_id,
        }

    def _get_work_plan(
        self,
        status: str = "open",
        limit: int = 20,
        offset: int = 0,
        step_id: str = "",
        detail: str = "compact",
        include_evidence: bool | None = None,
        reconcile_evidence: bool = True,
    ) -> ToolResult:
        allowed_statuses = {"open", "pending", "in_progress", "blocked", "done", "all"}
        status = status if status in allowed_statuses else "open"
        allowed_details = {"compact", "standard", "full", "evidence"}
        detail = detail if detail in allowed_details else "compact"
        if include_evidence is False and detail in {"standard", "full", "evidence"}:
            detail = "compact"
        elif include_evidence is True and detail == "compact":
            detail = "standard"
        limit = max(1, min(int(limit), 100))
        offset = max(0, int(offset))
        reconciliation: dict[str, object] | None = None
        if reconcile_evidence:
            reconcile_status = status if status in {"open", "pending", "in_progress", "blocked"} else "open"
            reconcile_result = self.goal_controller.reconcile_work_plan_evidence(reconcile_status)
            try:
                reconciliation = json.loads(reconcile_result.content)
            except json.JSONDecodeError:
                reconciliation = {"ok": reconcile_result.ok, "content": reconcile_result.content}
        all_items = [item.to_dict() for item in self.state.work_plan]
        status_counts = Counter(str(item.get("status") or "pending") for item in all_items)
        requested_step_id = " ".join(step_id.strip().split()) if isinstance(step_id, str) else ""
        selected_step = None
        if requested_step_id:
            selected_step = next((item for item in all_items if item.get("id") == requested_step_id), None)
            if selected_step is None:
                return ToolResult(
                    ok=False,
                    content=f"unknown work step: {requested_step_id}",
                    blocked=True,
                    block_reason="unknown work step",
                )
            filtered = [selected_step]
        if status == "all":
            filtered = filtered if requested_step_id else all_items
        elif status == "open":
            if not requested_step_id:
                filtered = [
                    item
                    for item in all_items
                    if item.get("status") in {"pending", "in_progress", "blocked"}
                ]
        else:
            if not requested_step_id:
                filtered = [item for item in all_items if item.get("status") == status]
        page_items = filtered[offset : offset + limit]
        page_items = [self._format_work_plan_item(item, detail=detail) for item in page_items]
        current_step = None
        if self.state.current_step_id:
            raw_current_step = next(
                (item for item in all_items if item.get("id") == self.state.current_step_id),
                None,
            )
            current_step = (
                self._format_work_plan_item(raw_current_step, detail="compact")
                if raw_current_step
                else None
            )
        next_pending = next(
            (item for item in all_items if item.get("status") == "pending"),
            None,
        )
        if next_pending:
            next_pending = self._format_work_plan_item(next_pending, detail="compact")
        payload = {
            "objective": self.state.active_goal,
            "goal_status": self.state.goal_status,
            "current_step_id": self.state.current_step_id,
            "current_step": current_step,
            "next_pending_step": next_pending,
            "summary": {
                "total": len(all_items),
                "status_counts": dict(status_counts),
                "open_count": sum(
                    status_counts.get(item_status, 0)
                    for item_status in ("pending", "in_progress", "blocked")
                ),
                "done_count": status_counts.get("done", 0),
            },
            "page": {
                "status": status,
                "detail": detail,
                "step_id": requested_step_id or None,
                "offset": offset,
                "limit": limit,
                "returned": len(page_items),
                "total_filtered": len(filtered),
                "has_more": offset + limit < len(filtered),
                "next_offset": offset + limit if offset + limit < len(filtered) else None,
            },
            "work_plan": page_items,
            "work_plan_sources": [item.to_dict() for item in self.state.work_plan_sources],
            "work_plan_source_status": self._work_plan_source_status(),
            "evidence_reconciliation": reconciliation,
        }
        return ToolResult(ok=True, content=json.dumps(payload, ensure_ascii=False, indent=2))

    def _format_work_plan_item(self, item: dict[str, object], *, detail: str) -> dict[str, object]:
        evidence_ids = [value for value in item.get("evidence_ids", []) if isinstance(value, str)] \
            if isinstance(item.get("evidence_ids"), list) else []
        evidence = [value for value in item.get("evidence", []) if isinstance(value, str)] \
            if isinstance(item.get("evidence"), list) else []
        base = {
            "id": item.get("id"),
            "title": item.get("title"),
            "status": item.get("status"),
            "required": item.get("required"),
            "acceptance": item.get("acceptance"),
            "verification_hint": item.get("verification_hint"),
            "blocker": item.get("blocker"),
            "source_document": item.get("source_document"),
            "evidence_count": len(evidence_ids) or len(evidence),
        }
        if detail == "compact":
            return {key: value for key, value in base.items() if value not in ("", [], None)}
        recent_ids = evidence_ids[-6:]
        standard = {
            **base,
            "evidence_ids": recent_ids,
            "evidence_summaries": evidence[-3:],
            "has_more_evidence": max(len(evidence_ids), len(evidence)) > max(len(recent_ids), 3),
        }
        if detail == "standard":
            return {key: value for key, value in standard.items() if value not in ("", [], None)}
        evidence_records = [
            record.to_dict()
            for evidence_id in evidence_ids
            if (record := self.state.find_evidence_record(evidence_id)) is not None
        ]
        if detail == "evidence":
            return {
                "id": item.get("id"),
                "title": item.get("title"),
                "status": item.get("status"),
                "evidence": evidence,
                "evidence_ids": evidence_ids,
                "evidence_records": evidence_records,
            }
        return {**item, "evidence_records": evidence_records}

    def _sync_work_plan_from_document(self, path: str, max_steps: int = 20) -> ToolResult:
        try:
            if not self.state.active_goal:
                return ToolResult(
                    ok=False,
                    content="cannot sync work plan because this session has no active goal",
                    blocked=True,
                    block_reason="no active goal",
                )
            safe = self.path_safety.resolve_workspace_path(path)
            text = safe.path.read_text(encoding="utf-8", errors="replace")
            steps = self.work_plan_document_parser.parse(text, max_steps=max(1, min(int(max_steps), 50)))
            for step in steps:
                step["source_document"] = safe.relative
            if not steps:
                return ToolResult(
                    ok=False,
                    content=f"no plan-like steps found in {safe.relative}",
                    blocked=True,
                    block_reason="no plan steps found",
                )
            result = self.goal_controller.merge_work_plan(steps, source_document=safe.relative)
            if not result.ok:
                return result
            self.state.remember_work_plan_source(
                path=safe.relative,
                content=text,
                mtime=safe.path.stat().st_mtime,
            )
            return ToolResult(
                ok=True,
                content=(
                    f"synced {len(steps)} work-plan steps from {safe.relative} with merge semantics\n\n"
                    f"{result.content}"
                ),
                metadata={
                    "goal_event": "plan_merged",
                    "goal": self.state.goal_snapshot(),
                    "source_path": safe.relative,
                    "steps": steps,
                },
            )
        except Exception as exc:
            return ToolResult(ok=False, content=str(exc), blocked=True, block_reason=str(exc))

    def _work_plan_source_status(self) -> list[dict[str, object]]:
        statuses: list[dict[str, object]] = []
        for source in self.state.work_plan_sources:
            path = (source.path or "").strip()
            if not path:
                continue
            try:
                safe = self.path_safety.resolve_workspace_path(path)
                text = safe.path.read_text(encoding="utf-8", errors="replace")
                current_hash = hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()
                current_mtime = safe.path.stat().st_mtime
                stale = current_hash != source.content_hash
                statuses.append(
                    {
                        "path": path,
                        "stale": stale,
                        "synced_at": source.synced_at,
                        "mtime": current_mtime,
                    }
                )
            except Exception as exc:
                statuses.append(
                    {
                        "path": path,
                        "stale": True,
                        "missing": True,
                        "reason": str(exc),
                        "synced_at": source.synced_at,
                    }
                )
        return statuses

    def _inspect_project_startup(self, root: str = ".", max_depth: int = 4) -> ToolResult:
        try:
            safe = self.path_safety.resolve_workspace_path(root or ".")
            report = _discover_startup_candidates(
                safe.path,
                self.settings.workspace_root,
                self.state.runtime_resources,
                max_depth=max(1, min(int(max_depth), 8)),
            )
            report["root"] = safe.relative
            self._last_startup_report = report
            self._record_startup_engineering_facts(report)
            content = json.dumps(report, ensure_ascii=False, indent=2)
            return ToolResult(ok=True, content=content, metadata=report)
        except Exception as exc:
            return ToolResult(ok=False, content=str(exc), blocked=True, block_reason=str(exc))

    def _record_startup_engineering_facts(self, report: dict[str, object]) -> None:
        for item in report.get("config_refs", []):
            if not isinstance(item, dict):
                continue
            source = str(item.get("source") or "")
            target = str(item.get("target") or item.get("url") or "")
            port = item.get("port")
            summary = f"configuration reference {item.get('kind') or 'config'} points to {target or port}"
            self.state.add_engineering_fact(
                type="config_ref",
                source=source,
                summary=summary,
                data={
                    "kind": item.get("kind") or "",
                    "target": target,
                    "port": port,
                    "cwd": item.get("cwd") or "",
                    "reason": item.get("reason") or "",
                },
                confidence="high",
            )
        for item in report.get("candidates", []):
            if not isinstance(item, dict):
                continue
            source = str(item.get("source") or "")
            command = str(item.get("command") or "")
            fact_type = "entrypoint" if item.get("expected_long_running") else "command"
            self.state.add_engineering_fact(
                type=fact_type,
                source=source,
                summary=f"startup candidate {command}",
                data={
                    "kind": item.get("kind") or "",
                    "command": command,
                    "cwd": item.get("cwd") or "",
                    "port": item.get("default_port"),
                    "expected_long_running": bool(item.get("expected_long_running")),
                    "reason": item.get("reason") or "",
                },
                confidence=str(item.get("confidence") or "medium"),
            )
        for item in report.get("runtime_resources", []):
            if not isinstance(item, dict):
                continue
            command = str(item.get("command") or "")
            stale = item.get("observed_status") == "stale"
            self.state.add_engineering_fact(
                type="runtime_resource",
                source=str(item.get("id") or item.get("source_tool") or "runtime"),
                summary=f"observed runtime resource {command}",
                data={
                    "command": command,
                    "cwd": item.get("cwd") or "",
                    "port": item.get("port"),
                    "status": item.get("observed_status") or item.get("status") or "",
                    "ready": item.get("ready"),
                },
                confidence="medium",
                stale=stale,
            )

    def _startup_action_facts_message(self) -> str | None:
        report = self._last_startup_report
        if not report:
            return None
        candidates = [
            item for item in report.get("candidates", [])
            if isinstance(item, dict) and item.get("expected_long_running")
        ]
        stale_resources = [
            item for item in report.get("runtime_resources", [])
            if isinstance(item, dict) and item.get("observed_status") == "stale"
        ]
        if not candidates and not stale_resources:
            return None

        lines = [
            "Startup facts require a concrete action. Runtime is not choosing the framework for you; "
            "it is reporting observed facts from the workspace and ports.",
        ]
        config_refs = [item for item in report.get("config_refs", []) if isinstance(item, dict)]
        warnings = [item for item in report.get("warnings", []) if isinstance(item, dict)]
        if config_refs:
            lines.append("Configuration references discovered from project files:")
            for item in config_refs[:5]:
                lines.append(
                    "- "
                    f"source={item.get('source') or ''}; "
                    f"kind={item.get('kind') or ''}; "
                    f"target={item.get('target') or ''}; "
                    f"port={item.get('port') or ''}"
                )
        if candidates:
            lines.append("Startup candidates discovered from project files:")
            for item in candidates[:5]:
                lines.append(
                    "- "
                    f"kind={item.get('kind') or ''}; "
                    f"cwd={item.get('cwd') or ''}; "
                    f"command={item.get('command') or ''}; "
                    f"default_port={item.get('default_port') or ''}"
                )
        if stale_resources:
            lines.append(
                "Observed stale runtime resources from earlier attempts. These are historical facts, not preferred "
                "startup instructions. Do not copy a stale command or port when project configuration/startup "
                "candidates point elsewhere."
            )
            for item in stale_resources[:5]:
                lines.append(
                    "- "
                    f"cwd={item.get('cwd') or ''}; "
                    f"command={item.get('command') or ''}; "
                    f"port={item.get('port') or ''}; "
                    f"observed_status={item.get('observed_status') or ''}"
                )
        if warnings:
            lines.append("Startup warnings:")
            for item in warnings[:5]:
                lines.append(
                    "- "
                    f"kind={item.get('kind') or ''}; "
                    f"details={json.dumps(item, ensure_ascii=False)}"
                )
        lines.extend(
            [
                "Required next action: avoid repeating the same inspection-only batch without a new hypothesis.",
                "Choose one concrete action now: start or restart a suitable long-running candidate with "
                "start_background_command; run a finite setup/check command if startup prerequisites are missing; "
                "run an HTTP smoke check if a service is already listening; inspect a new high-value target with "
                "a clear reason; or mark the goal/step blocked with the exact blocker and evidence.",
            ]
        )
        return "\n".join(lines)

    def _diagnostic_first_message(self, user_input: str, reason: str) -> str:
        snapshot = _project_context_snapshot(self.settings.workspace_root, self.state.changed_files)
        legacy_guidance = (
            "Required local diagnosis sequence:\n"
            "1. Inspect recent changed_files and relevant entry/config files.\n"
            "2. Inspect project scripts before choosing commands; do not guess start/dev/test commands.\n"
            "3. Run the cheapest applicable verification command, such as build/check/test, when available.\n"
            "4. For runtime service issues, inspect ports/background logs and use start_background_command for servers.\n"
            "5. For cross-layer issues, compare caller references with provider definitions: routes, commands, paths, files, or APIs.\n"
            "6. Fix based on local evidence, then rerun verification or smoke checks."
        )
        guidance = self._render_skill_hook(
            domain="code",
            hook="diagnostic_first",
            legacy_content=legacy_guidance,
            variables={
                "reason": reason,
                "user_report": user_input[:500],
                "workspace_root": str(self.settings.workspace_root),
                "changed_files": self.state.changed_files[-12:],
                "project_context_snapshot": snapshot,
            },
        )
        return (
            "Diagnostic-first policy is active because the user reported an error. "
            "Do not ask the user for screenshots, console output, Network panel details, ports, or project paths "
            "until local evidence is exhausted. Use tools now.\n\n"
            f"Reason: {reason}\n"
            f"User report: {user_input[:500]}\n\n"
            f"{guidance}\n\n"
            f"Project context snapshot:\n{snapshot}"
        )

    def _project_preflight_message(self) -> str:
        profile = self.code_runtime.detect_project(self.settings.workspace_root)
        entries = []
        for child in sorted(self.settings.workspace_root.iterdir(), key=lambda path: path.name.lower()):
            if child.name in set(self.settings.paths.ignore):
                continue
            if child.is_dir():
                nested = sorted(
                    item.name for item in child.iterdir() if item.name not in set(self.settings.paths.ignore)
                )[:8]
                suffix = f" ({', '.join(nested)})" if nested else ""
                entries.append(f"- {child.name}/ {suffix}")
            else:
                entries.append(f"- {child.name}")
            if len(entries) >= 30:
                entries.append("- ...")
                break
        plan = self.code_runtime.build_verification_plan(self.settings.workspace_root, [])
        verification = plan.summary()
        legacy_guidance = "\n".join([
            "- Inspect the workspace before editing; do not guess filenames.",
            "- Prefer reading tests and existing source files before writing.",
            "- Modify files with write_file/edit_file, then stop tool calls so runtime can verify.",
            "- Do not answer with code-only instructions when file changes are required.",
        ])
        guidance = self._render_skill_hook(
            domain="code",
            hook="project_preflight",
            legacy_content=legacy_guidance,
            variables={
                "expected_action": "modify_and_verify",
                "workspace_root": str(self.settings.workspace_root),
                "detected_types": profile.detected_types,
                "key_files": profile.key_files,
                "test_signals": profile.test_signals,
                "verification_reason": verification["reason"],
                "verification_steps": verification["steps"],
            },
        )
        return (
            "Project preflight for this coding turn:\n"
            f"Expected action: modify_and_verify\n"
            f"Workspace root: {self.settings.workspace_root}\n"
            f"Detected types: {profile.detected_types}\n"
            f"Key files: {profile.key_files}\n"
            f"Test signals: {profile.test_signals}\n"
            "Workspace snapshot:\n"
            + "\n".join(entries)
            + "\nVerification hint:\n"
            f"- reason: {verification['reason']}\n"
            f"- steps: {verification['steps']}\n"
            "Rules:\n"
            + guidance
        )

    def _handle_tool_failure(
        self,
        tool_name: str,
        arguments: dict[str, object],
        result: ToolResult,
        turn_id: str,
    ) -> AgentTurnResult | None:
        signature = _tool_failure_signature(tool_name, arguments, result)
        exhausted = self.state.repair_count(signature) >= self.settings.max_repair_rounds
        if exhausted:
            self.state.result_state = "blocked"
            reason = "tool repair limit reached"
            self.event_bus.emit(
                "blocked",
                {"reason": reason, "tool": tool_name, "signature": signature},
                turn_id,
            )
            return AgentTurnResult(
                status="blocked",
                response=(
                    f"{reason}.\n"
                    f"Tool: {tool_name}\n"
                    f"Arguments: {arguments}\n"
                    f"Result:\n{result.content[:4000]}"
                ),
                changed_files=self.state.all_changed_files(),
                token_usage=self.token_usage.total(),
            )
        repair_round = self.state.record_repair_attempt(signature)
        legacy_guidance = (
            "The previous tool call failed. Do not finish with a manual instruction. "
            "Use the tool result to correct the command, cwd, path, arguments, or inspect the project, "
            "then retry with tools. Shell state from a prior command, such as cd, PATH changes, "
            "environment activation, or sourced scripts, does not persist across tool calls unless it is "
            "encoded in the next tool arguments. If a previous command created or used an isolated runtime "
            "environment, use explicit executable paths or run activation and the finite follow-up command "
            "in one command. Do not run long-running servers through run_command. If a browser/UI test failed, "
            "use the captured result, screenshot, console/network details, and current routes/services to "
            "diagnose the next concrete fix or verification step."
        )
        guidance = self._render_skill_hook(
            domain="code",
            hook="tool_failure_repair",
            legacy_content=legacy_guidance,
            variables={
                "tool": tool_name,
                "arguments": arguments,
                "block_reason": result.block_reason,
                "failure_kind": result.metadata.get("failure_kind"),
                "content_excerpt": result.content[:1200],
                "repair_round": repair_round,
            },
        )
        self.history.add_runtime(
            f"{guidance}\n\n"
            f"Tool: {tool_name}\n"
            f"Arguments: {arguments}\n"
            f"Result:\n{result.content[:4000]}"
        )
        self._checkpoint("tool_failure_added")
        self.event_bus.emit(
            "tool_failure_recovery_started",
            {
                "round": repair_round,
                "tool": tool_name,
                "signature": signature,
                "content_excerpt": result.content[:500],
            },
            turn_id,
        )
        return None

    @staticmethod
    def _is_recoverable_tool_error(name: str, result) -> bool:
        content = f"{result.block_reason or ''}\n{result.content or ''}".lower()
        if name == "update_work_step":
            recoverable_goal_markers = (
                "missing work step evidence",
                "unknown evidence ids",
                "cannot mark work step done without known evidence",
                "available recent evidence_ids",
            )
            return any(marker in content for marker in recoverable_goal_markers)
        if name not in {
            "read_file",
            "list_directory",
            "view_image",
            "edit_file",
            "run_command",
            "run_python",
            "http_request",
            "browser_test",
            "start_background_command",
        }:
            return False
        if name in {"read_file", "list_directory", "view_image"}:
            payload = _path_failure_payload(result.content or "")
            if payload and payload.get("requested_path"):
                return True
        recoverable_markers = (
            "old_text not found",
            "missing required tool arguments",
            "invalid tool arguments",
            "command looks long-running",
            "use start_background_command",
            "no such file or directory",
            "does not exist",
            "cannot find the file",
            "cannot find the path",
            "找不到",
            "not found",
            "timed out",
            "timeout",
            "page.goto",
            "net::err_connection_refused",
            "browser action failed",
            "browser runtime error",
            "winerror 2",
            "winerror 3",
            "errno 2",
        )
        return any(marker in content for marker in recoverable_markers)

    def _checkpoint(self, reason: str) -> None:
        if not self.checkpoint_callback:
            return
        try:
            self.checkpoint_callback(self)
            self.logger.write(f"checkpoint saved reason={reason}")
        except Exception as exc:
            self.logger.write(f"checkpoint failed reason={reason} error={exc!r}")

    def _handle_history_command(self, user_input: str) -> AgentTurnResult | None:
        parts = user_input.strip().split()
        if not parts:
            return None
        command = parts[0].lower()
        if command not in {
            "/clear",
            "/drop-first",
            "/drop-last",
            "/drop-images",
            "/stats",
            "/context-dump",
            "/dump-context",
        }:
            return None
        try:
            if command == "/stats":
                response = _format_metrics_text(self._build_session_metrics())
            elif command in {"/context-dump", "/dump-context"}:
                response = self._handle_context_dump_command(parts)
            elif command == "/clear":
                removed = self.history.clear()
                response = f"Cleared {removed} history messages."
            elif command == "/drop-first":
                count = _parse_history_count(parts, command)
                removed = self.history.drop_first(count)
                response = f"Dropped first {removed} history messages."
            elif command == "/drop-last":
                count = _parse_history_count(parts, command)
                removed = self.history.drop_last(count)
                response = f"Dropped last {removed} history messages."
            else:
                count = (
                    None
                    if len(parts) == 1 or parts[1].lower() == "all"
                    else _parse_history_count(parts, command)
                )
                removed = self.history.drop_images(count)
                response = f"Dropped image payloads from {removed} history messages."
        except ValueError as exc:
            response = str(exc)
        self._checkpoint("history_command")
        return AgentTurnResult(
            status="completed",
            response=response,
            changed_files=self.state.all_changed_files(),
            token_usage=self.token_usage.total(),
        )

    def _handle_context_dump_command(self, parts: list[str]) -> str:
        if len(parts) == 1 or parts[1].lower() in {"status", "show"}:
            state = "on" if self.settings.agent.context_dump_enabled else "off"
            latest = self.settings.artifact_root / "logs" / "context" / "latest.md"
            path_text = f" Latest: {latest}" if latest.exists() else ""
            return f"Context dump is {state}.{path_text}"
        action = parts[1].lower()
        if action in {"on", "enable", "enabled", "true", "1"}:
            self.settings.agent.context_dump_enabled = True
            latest = self.settings.artifact_root / "logs" / "context" / "latest.md"
            return f"Context dump enabled. Next model call will write {latest}."
        if action in {"off", "disable", "disabled", "false", "0"}:
            self.settings.agent.context_dump_enabled = False
            return "Context dump disabled."
        raise ValueError("Usage: /context-dump on|off|status")

    def _build_session_metrics(self) -> dict[str, object]:
        events = self.event_bus.events()
        event_counts = Counter(event.type for event in events)
        tool_calls: Counter[str] = Counter()
        tool_failures: Counter[str] = Counter()
        required_retries: Counter[str] = Counter()
        verification_failures = 0
        browser_failures = 0
        cache_hit = 0
        cache_total = 0
        for event in events:
            payload = event.payload
            if event.type == "tool_call_started":
                tool_calls[str(payload.get("name") or "tool")] += 1
            elif event.type == "tool_call_finished":
                name = str(payload.get("name") or "tool")
                if not payload.get("ok"):
                    tool_failures[name] += 1
                    if name == "browser_test":
                        browser_failures += 1
            elif event.type == "required_tool_retry":
                required_retries[str(payload.get("expected_action") or "unknown")] += 1
            elif event.type == "verification_step_finished" and not payload.get("ok"):
                verification_failures += 1
            elif event.type == "token_usage_recorded":
                cache_hit += int(payload.get("cache_hit_prompt_tokens") or 0)
                cache_total += int(payload.get("prompt_tokens") or 0)
        return {
            "turns": int(event_counts["turn_started"]),
            "model_calls": int(event_counts["model_call_started"]),
            "tool_calls": int(event_counts["tool_call_started"]),
            "tool_failures": int(sum(tool_failures.values())),
            "browser_failures": browser_failures,
            "verification_runs": int(event_counts["verification_started"]),
            "verification_failures": verification_failures,
            "context_compactions": int(event_counts["context_compaction_finished"]),
            "tool_calls_by_name": dict(tool_calls.most_common(10)),
            "tool_failures_by_name": dict(tool_failures.most_common(10)),
            "required_retries": dict(required_retries.most_common(10)),
            "cache_hit_tokens": cache_hit,
            "prompt_tokens": cache_total,
            "cache_hit_ratio": round(cache_hit / cache_total, 4) if cache_total else 0.0,
        }


def _discover_startup_candidates(
    root: Path,
    workspace_root: Path,
    runtime_resources: list[object],
    *,
    max_depth: int,
) -> dict[str, object]:
    candidates: list[dict[str, object]] = []
    config_refs: list[dict[str, object]] = []
    warnings: list[dict[str, object]] = []

    for path in _iter_startup_files(root, max_depth=max_depth):
        relative = path.relative_to(workspace_root).as_posix()
        cwd = path.parent.relative_to(workspace_root).as_posix() or "."
        name = path.name
        if name == "package.json":
            _add_package_startup(path, relative, cwd, candidates, config_refs, warnings)
        elif name == "manage.py":
            candidates.append(
                {
                    "kind": "python-django",
                    "source": relative,
                    "cwd": cwd,
                    "command": "python manage.py runserver 127.0.0.1:8000",
                    "expected_long_running": True,
                    "default_port": 8000,
                    "confidence": "high",
                    "reason": "manage.py entrypoint found",
                }
            )
        elif name in {"Makefile", "makefile"}:
            candidates.append(
                {
                    "kind": "make",
                    "source": relative,
                    "cwd": cwd,
                    "command": "make",
                    "expected_long_running": False,
                    "confidence": "medium",
                    "reason": "Makefile found; inspect targets if command purpose is unclear",
                }
            )
        elif name == "Cargo.toml":
            candidates.append(
                {
                    "kind": "rust",
                    "source": relative,
                    "cwd": cwd,
                    "command": "cargo run",
                    "expected_long_running": "unknown",
                    "confidence": "medium",
                    "reason": "Cargo.toml found",
                }
            )
        elif name == "go.mod":
            candidates.append(
                {
                    "kind": "go",
                    "source": relative,
                    "cwd": cwd,
                    "command": "go run .",
                    "expected_long_running": "unknown",
                    "confidence": "medium",
                    "reason": "go.mod found",
                }
            )
    resources = [
        _runtime_resource_with_observed_health(resource)
        for resource in runtime_resources[-20:]
        if hasattr(resource, "to_dict")
    ]
    warnings.extend(_startup_topology_warnings(config_refs, resources))
    return {
        "candidates": candidates,
        "config_refs": config_refs,
        "runtime_resources": resources,
        "warnings": warnings,
        "guidance": [
            "Use start_background_command for candidates expected to be long-running.",
            "Use run_command for finite setup, build, check, and test commands.",
            "If no candidate fits the project, inspect files and create a small project-local run script or documented command, then verify it.",
            "Treat runtime_resources as observed facts, not proof that the user's requested behavior is complete.",
        ],
    }


def _iter_startup_files(root: Path, *, max_depth: int) -> list[Path]:
    names = {"package.json", "manage.py", "Makefile", "makefile", "Cargo.toml", "go.mod"}
    ignored = {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        "node_modules",
        "dist",
        "build",
        "target",
        ".minicodex2",
    }
    found: list[Path] = []
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_file() and entry.name in names:
                found.append(entry)
            elif entry.is_dir() and depth < max_depth and entry.name not in ignored:
                stack.append((entry, depth + 1))
    return sorted(found, key=lambda item: item.as_posix())


def _add_package_startup(
    path: Path,
    relative: str,
    cwd: str,
    candidates: list[dict[str, object]],
    config_refs: list[dict[str, object]],
    warnings: list[dict[str, object]],
) -> None:
    try:
        package = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        warnings.append({"kind": "package_json_parse_failed", "source": relative, "message": str(exc)})
        return
    if not isinstance(package, dict):
        return
    scripts = package.get("scripts")
    if isinstance(scripts, dict):
        for script_name in ("start", "dev", "serve", "preview", "build", "test"):
            script = scripts.get(script_name)
            if not isinstance(script, str) or not script.strip():
                continue
            candidates.append(
                {
                    "kind": "node-script",
                    "source": relative,
                    "cwd": cwd,
                    "command": "npm start" if script_name == "start" else f"npm run {script_name}",
                    "script_name": script_name,
                    "script": script,
                    "expected_long_running": script_name in {"start", "dev", "serve", "preview"},
                    "confidence": "high",
                    "reason": f"package.json scripts.{script_name} found",
                }
            )
    proxy = package.get("proxy")
    if isinstance(proxy, str) and proxy.strip():
        config_refs.append(
            {
                "kind": "node-proxy",
                "source": relative,
                "cwd": cwd,
                "url": proxy.strip(),
                "port": _extract_port(proxy),
                "reason": "package.json proxy target",
            }
        )


def _startup_topology_warnings(
    config_refs: list[dict[str, object]],
    runtime_resources: list[dict[str, object]],
) -> list[dict[str, object]]:
    warnings: list[dict[str, object]] = []
    proxy_ports = {
        int(ref["port"])
        for ref in config_refs
        if ref.get("kind") == "node-proxy" and isinstance(ref.get("port"), int)
    }
    if not proxy_ports:
        return warnings
    for resource in runtime_resources:
        port = resource.get("port")
        command = str(resource.get("command") or "")
        if isinstance(port, int) and port not in proxy_ports and "manage.py" in command:
            warnings.append(
                {
                    "kind": "possible_port_mismatch",
                    "message": (
                        f"package proxy targets port(s) {sorted(proxy_ports)}, "
                        f"but observed manage.py resource uses port {port}"
                    ),
                    "resource": resource,
                }
            )
    return warnings


def _extract_port(url: str) -> int | None:
    match = re.search(r":(\d{2,5})(?:/|$)", url)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _runtime_resource_with_observed_health(resource: object) -> dict[str, object]:
    data = resource.to_dict() if hasattr(resource, "to_dict") else {}
    port = data.get("port")
    if isinstance(port, int):
        open_now = _is_local_port_open(port)
        data["observed_port_open"] = open_now
        if data.get("ready") is True and not open_now:
            data["observed_status"] = "stale"
            data["warning"] = (
                "This persisted runtime resource was previously ready, but the port is not open now. "
                "Treat it as stopped and restart the service before HTTP verification."
            )
        elif open_now:
            data["observed_status"] = "listening"
    return data


def _is_local_port_open(port: int) -> bool:
    for host in ("127.0.0.1", "localhost"):
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return True
        except OSError:
            continue
    return False


def _change_action_for_tool(tool_name: str) -> str:
    if tool_name == "write_file":
        return "wrote"
    if tool_name == "edit_file":
        return "edited"
    if tool_name == "delete_file":
        return "deleted"
    return "changed"


def _path_failure_facts_message(content: str) -> str | None:
    payload = _path_failure_payload(content)
    if payload is None:
        return None
    requested_path = payload.get("requested_path")
    if not isinstance(requested_path, str) or not requested_path:
        return None
    parent = payload.get("parent")
    parent_exists = payload.get("parent_exists")
    siblings = payload.get("existing_siblings")
    sibling_lines: list[str] = []
    if isinstance(siblings, list):
        for item in siblings[:20]:
            if isinstance(item, dict):
                name = item.get("name")
                kind = item.get("type")
                if isinstance(name, str):
                    suffix = f" ({kind})" if isinstance(kind, str) else ""
                    sibling_lines.append(f"  - {name}{suffix}")
    lines = [
        "Path lookup failed facts: the requested path does not exist or cannot be read.",
        f"- missing_requested_path: {requested_path}",
        f"- parent: {parent or ''}",
        f"- parent_exists: {parent_exists}",
    ]
    if sibling_lines:
        lines.append("- existing_siblings:")
        lines.extend(sibling_lines)
        lines.append(
            "Required next action: do not read the same missing path again. Use one of the listed siblings, "
            "list an existing parent, or create the missing file only if the goal requires a new file."
        )
    else:
        lines.append(
            "Required next action: do not guess another nested path under a missing parent. List an existing "
            "ancestor directory or use project startup/context facts to find the real path."
        )
    return "\n".join(lines)


def _path_failure_payload(content: str) -> dict[str, object] | None:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _blocked_response(reason: str, failure_pack: FailurePack) -> str:
    parts = [reason, failure_pack.first_blocking_failure]
    if failure_pack.suggested_next_action:
        parts.append(f"Next: {failure_pack.suggested_next_action}")
    return "\n".join(part for part in parts if part)


def _verification_step_detail(step_result) -> str:
    command_result = getattr(step_result, "command_result", None)
    if command_result is None:
        return ""
    stdout = getattr(command_result, "stdout", "") or ""
    stderr = getattr(command_result, "stderr", "") or ""
    return (
        f"stdout:\n{stdout[:1000]}\n"
        f"stderr:\n{stderr[:1000]}"
    )


def _tool_call_summary(name: str, arguments: dict[str, object]) -> str:
    if name in {"read_file", "write_file", "edit_file", "delete_file"}:
        return str(arguments.get("path") or name)
    if name == "list_directory":
        return str(arguments.get("path") or ".")
    if name in {"find_images", "view_image"}:
        return str(arguments.get("query") or arguments.get("path") or name)
    if name == "browser_test":
        url = _compact_summary(str(arguments.get("url") or name))
        actions = arguments.get("actions")
        count = len(actions) if isinstance(actions, list) else 0
        return f"{url} [{count} actions]"
    if name in {"run_command", "start_background_command"}:
        return _compact_summary(str(arguments.get("command") or name))
    if name == "run_python":
        return "python helper"
    if name in {"inspect_port", "release_port"}:
        return f"port {arguments.get('port')}"
    if name == "inspect_project_startup":
        return str(arguments.get("root") or ".")
    if name in {"inspect_toolchain", "install_toolchain"}:
        return str(arguments.get("name") or name)
    return name


def _paired_event_seconds(events: list[Any], start_type: str, finish_type: str | set[str]) -> float:
    finish_types = {finish_type} if isinstance(finish_type, str) else finish_type
    starts: list[datetime] = []
    total = 0.0
    for event in events:
        if event.type == start_type:
            parsed = _parse_event_timestamp(str(event.timestamp))
            if parsed is not None:
                starts.append(parsed)
        elif event.type in finish_types and starts:
            parsed = _parse_event_timestamp(str(event.timestamp))
            if parsed is not None:
                started = starts.pop(0)
                total += max(0.0, (parsed - started).total_seconds())
    return total


def _parse_event_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    except ValueError:
        return None


def _format_metrics_text(metrics: dict[str, object]) -> str:
    lines = [
        "Session stats:",
        f"- turns: {metrics.get('turns', 0)}",
        f"- model calls: {metrics.get('model_calls', 0)}",
        f"- tool calls: {metrics.get('tool_calls', 0)}",
        f"- tool failures: {metrics.get('tool_failures', 0)}",
        f"- browser failures: {metrics.get('browser_failures', 0)}",
        f"- verification runs: {metrics.get('verification_runs', 0)}",
        f"- verification failures: {metrics.get('verification_failures', 0)}",
        f"- compactions: {metrics.get('context_compactions', 0)}",
        f"- cache hit ratio: {float(metrics.get('cache_hit_ratio') or 0):.1%}",
    ]
    calls = metrics.get("tool_calls_by_name")
    if isinstance(calls, dict) and calls:
        lines.append("- top tools: " + ", ".join(f"{name}={count}" for name, count in calls.items()))
    failures = metrics.get("tool_failures_by_name")
    if isinstance(failures, dict) and failures:
        lines.append("- top tool failures: " + ", ".join(f"{name}={count}" for name, count in failures.items()))
    retries = metrics.get("required_retries")
    if isinstance(retries, dict) and retries:
        lines.append("- required retries: " + ", ".join(f"{name}={count}" for name, count in retries.items()))
    return "\n".join(lines)


def _tool_calls_are_inspection_only(tool_calls: list[object]) -> bool:
    if not tool_calls:
        return False
    inspection_tools = {
        "get_goal",
        "list_directory",
        "read_file",
        "find_images",
        "view_image",
        "inspect_port",
        "inspect_project_startup",
        "inspect_toolchain",
    }
    return all(getattr(call, "name", "") in inspection_tools for call in tool_calls)


def _inspection_progress_guidance(batch_count: int, targets: list[str]) -> str:
    target_lines = "\n".join(f"- {target}" for target in targets[:8])
    if not target_lines:
        target_lines = "- none recorded"
    return (
        "Inspection progress note: recent tool batches only inspected existing context and repeated known targets.\n"
        f"- repeated_inspection_batches: {batch_count}\n"
        "- recent_targets:\n"
        f"{target_lines}\n"
        "Use this as a soft checkpoint, not a blocker. If another focused read will reduce uncertainty, explain the "
        "new question and inspect it. Otherwise continue with the next concrete action: edit, verify, smoke test, "
        "update WorkPlan evidence, or report a concrete blocker."
    )


def _inspection_targets_from_tool_calls(tool_calls: list[object]) -> list[str]:
    targets: list[str] = []
    for call in tool_calls:
        name = getattr(call, "name", "")
        arguments = getattr(call, "arguments", {}) or {}
        if not isinstance(arguments, dict):
            arguments = {}
        target = f"{name}:{_tool_call_summary(name, arguments)}"
        if target not in targets:
            targets.append(target)
    return targets


def _tool_call_engages_active_goal(tool_name: str) -> bool:
    goal_action_tools = {
        "create_goal",
        "set_work_plan",
        "update_work_step",
        "set_current_step",
        "sync_work_plan_from_document",
        "update_goal",
    }
    return tool_name in goal_action_tools


def _should_classify_turn_scope(
    user_input: str,
    *,
    state: SessionState,
    expected_action: str | None,
    diagnostic_decision: object | None,
) -> bool:
    """Decide whether to spend an extra model call on scope classification.

    This is deliberately not the goal/router decision.  It is only a cheap
    cost-control gate so ordinary chat can still use one model call, while
    project-like turns get a model judgment about whether to create/reactivate
    goal memory, update a WorkPlan, or stay conversational.
    """

    if state.goal_status == "active":
        return True
    if expected_action in {"modify_and_verify", "create_project"}:
        return True
    if getattr(diagnostic_decision, "should_diagnose", False):
        return True
    if not (state.archived_goals or state.work_plan_sources or state.design_docs_exist):
        return False
    normalized = user_input.strip().lower()
    if not normalized:
        return False
    project_terms = (
        "project",
        "feature",
        "requirement",
        "design doc",
        "prd",
        "plan",
        "implement",
        "develop",
        "build",
        "fix",
        "test",
        "verify",
        "continue",
        "\u9879\u76ee",
        "\u529f\u80fd",
        "\u9700\u6c42",
        "\u8bbe\u8ba1\u6587\u6863",
        "\u6587\u6863",
        "\u8ba1\u5212",
        "\u5f00\u53d1",
        "\u5b9e\u73b0",
        "\u4fee\u6539",
        "\u6539\u4e0b",
        "\u589e\u52a0",
        "\u65b0\u589e",
        "\u6d4b\u8bd5",
        "\u81ea\u6d4b",
        "\u9a8c\u8bc1",
        "\u8054\u8c03",
        "\u7ee7\u7eed",
    )
    return any(term in normalized for term in project_terms)


def _parse_turn_scope_decision(content: str) -> TurnScopeDecision:
    data = _extract_json_object(content)
    if not isinstance(data, dict):
        return _fallback_turn_scope_decision("")
    allowed_scopes = {
        "chat_only",
        "answer_with_tools",
        "status_review",
        "side_task",
        "continue_current_goal",
        "modify_current_goal",
        "create_new_goal",
        "pause_goal",
        "complete_goal",
    }
    allowed_engagements = {"none", "background_only", "engaged", "update_only"}
    turn_scope = str(data.get("turn_scope") or "").strip()
    goal_engagement = str(data.get("goal_engagement") or "").strip()
    if turn_scope not in allowed_scopes:
        turn_scope = "chat_only"
    if goal_engagement not in allowed_engagements:
        goal_engagement = "background_only"
    if turn_scope in {"continue_current_goal", "complete_goal"}:
        goal_engagement = "engaged"
    elif turn_scope == "modify_current_goal":
        if goal_engagement not in {"engaged", "update_only"}:
            goal_engagement = "update_only"
    elif turn_scope == "pause_goal":
        goal_engagement = "update_only"
    elif turn_scope == "create_new_goal":
        if goal_engagement not in {"engaged", "update_only"}:
            goal_engagement = "engaged"
    elif turn_scope in {"chat_only", "answer_with_tools", "status_review", "side_task"} and goal_engagement == "engaged":
        goal_engagement = "background_only"
    reason = str(data.get("reason") or "").strip()
    return TurnScopeDecision(turn_scope=turn_scope, goal_engagement=goal_engagement, reason=reason)


def _extract_json_object(content: str) -> dict[str, Any] | None:
    stripped = content.strip()
    if not stripped:
        return None
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        value = json.loads(stripped)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        value = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _fallback_turn_scope_decision(user_input: str) -> TurnScopeDecision:
    if _user_requests_active_goal_continuation(user_input):
        return TurnScopeDecision(
            turn_scope="continue_current_goal",
            goal_engagement="engaged",
            reason="fallback detected an explicit continuation request",
        )
    return TurnScopeDecision(
        turn_scope="chat_only",
        goal_engagement="background_only",
        reason="fallback kept active goal as background only",
    )


def _user_requests_active_goal_continuation(user_input: str) -> bool:
    normalized = " ".join(user_input.strip().lower().split())
    if not normalized:
        return False
    exact_phrases = {
        "继续",
        "继续吧",
        "接着",
        "接着做",
        "接着来",
        "开始吧",
        "开工",
        "继续开发",
        "继续做",
        "下一步",
        "往下做",
        "continue",
        "continue working",
        "go on",
        "next",
        "next step",
        "proceed",
    }
    if normalized in exact_phrases:
        return True
    continuation_markers = (
        "继续完成",
        "继续把",
        "接着完成",
        "把项目做完",
        "开发完",
        "完成剩下",
        "继续联调",
        "继续修",
        "continue the project",
        "finish the project",
        "keep working",
        "resume",
    )
    return any(marker in normalized for marker in continuation_markers)


def _user_requests_file_change(user_input: str) -> bool:
    normalized = " ".join(user_input.strip().lower().split())
    if not normalized:
        return False
    file_markers = (
        ".md",
        ".txt",
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".json",
        ".toml",
        ".yaml",
        ".yml",
        ".html",
        ".css",
        "agents.md",
        "agent.md",
    )
    action_markers = (
        "写入",
        "写到",
        "写进",
        "存到",
        "存档",
        "保存到",
        "创建",
        "新建",
        "修改",
        "更新",
        "加入",
        "加到",
        "记录到",
        "write",
        "save",
        "create",
        "update",
        "edit",
        "add",
    )
    return any(marker in normalized for marker in file_markers) and any(
        marker in normalized for marker in action_markers
    )


def _compact_summary(text: str, limit: int = 120) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def _runtime_environment_summary() -> dict[str, object]:
    system = platform.system() or os.name
    is_windows = system.lower().startswith("win") or os.name == "nt"
    shell_path = os.environ.get("COMSPEC") if is_windows else os.environ.get("SHELL")
    shell_name = Path(shell_path).name if shell_path else ""
    shell_family = "windows-cmd" if is_windows else "posix"
    return {
        "os": system,
        "platform": platform.platform(),
        "is_windows": is_windows,
        "shell_family": shell_family,
        "shell_path": shell_path or "",
        "shell_name": shell_name,
        "path_separator": "\\" if is_windows else "/",
        "command_notes": [
            (
                f"run_command/start_background_command use the platform shell via subprocess shell=True; detected shell={shell_path or shell_family}."
                if is_windows
                else f"run_command/start_background_command use the platform shell via subprocess shell=True; detected shell={shell_path or shell_family}."
            ),
            (
                "Do not use Unix-only helpers such as head, tail, grep, sed, awk, xargs, or /bin/sh unless they are explicitly available."
                if is_windows
                else "Standard POSIX shell utilities are usually available."
            ),
            (
                "For output truncation on Windows cmd, prefer built-in MiniCodex tools or explicit powershell -NoProfile -Command \"... | Select-Object -First N\"."
                if is_windows
                else "Use POSIX pipelines when appropriate."
            ),
            (
                "PowerShell cmdlets such as Test-Path, Get-ChildItem, Select-Object, and Where-Object must be run through explicit powershell -NoProfile -Command \"...\"; do not call them bare through run_command."
                if is_windows
                else "Shell builtins and utilities must match the detected shell."
            ),
        ],
    }


def _compact_tool_fact_output(tool_name: str, result: ToolResult) -> str:
    """Summarize dynamic tool output before it becomes durable runtime facts.

    The full ToolResult is still available to the model immediately after a
    tool call. This function only controls the small fact line that gets
    carried into later context windows, where volatile HTML, screenshots,
    timestamps, PIDs, and response bodies would otherwise break prompt caching.
    """
    if tool_name in {"read_file", "list_directory", "search_files", "find_images", "view_image"}:
        return _workspace_fact_output(tool_name, result)
    if tool_name == "browser_test":
        return _browser_fact_output(result)
    if tool_name == "http_request":
        return _http_fact_output(result)
    if tool_name in {"start_background_command", "read_background_log"}:
        return _service_fact_output(result)
    return _compact_summary(result.content, 240)


def _workspace_fact_output(tool_name: str, result: ToolResult) -> str:
    """Keep workspace observation facts structural instead of duplicating data.

    read_file/list_directory/search_files already return their full payload as
    the immediate `tool` message. Re-copying file bodies or directory listings
    into the durable runtime facts makes every later request larger and less
    cacheable. The fact stream should preserve provenance and paging hints only;
    the model can re-read a range when it needs the raw content again.
    """

    metadata = result.metadata
    parts: list[str] = []
    path = metadata.get("path") or metadata.get("root") or metadata.get("requested_path")
    if isinstance(path, str) and path:
        parts.append(f"path={_compact_summary(path, 160)}")
    root = metadata.get("root")
    if isinstance(root, str) and root and root != path:
        parts.append(f"root={_compact_summary(root, 160)}")
    if tool_name == "read_file":
        for key in (
            "size",
            "line_count",
            "start_line",
            "end_line",
            "returned_lines",
            "offset",
            "returned_bytes",
            "next_start_line",
            "next_offset",
            "truncated",
        ):
            value = metadata.get(key)
            if value is not None:
                parts.append(f"{key}={value}")
        sha = metadata.get("sha256")
        if isinstance(sha, str) and sha:
            parts.append(f"sha256={sha[:12]}")
        return "; ".join(parts)
    if tool_name == "list_directory":
        entries = metadata.get("entries")
        if isinstance(entries, list):
            files = sum(1 for item in entries if isinstance(item, dict) and item.get("type") == "file")
            dirs = sum(1 for item in entries if isinstance(item, dict) and item.get("type") == "dir")
            sample = [
                str(item.get("path") or item.get("name"))
                for item in entries[:8]
                if isinstance(item, dict) and (item.get("path") or item.get("name"))
            ]
            parts.append(f"entries={len(entries)}")
            parts.append(f"files={files}")
            parts.append(f"dirs={dirs}")
            if sample:
                parts.append(f"sample={_compact_summary(', '.join(sample), 240)}")
        for key in ("recursive", "max_depth", "max_entries", "truncated", "glob"):
            value = metadata.get(key)
            if value not in (None, ""):
                parts.append(f"{key}={value}")
        return "; ".join(parts)
    if tool_name == "search_files":
        for key in ("query", "glob", "case_sensitive", "recursive", "candidate_files", "match_count", "truncated", "root_kind"):
            value = metadata.get(key)
            if value not in (None, ""):
                parts.append(f"{key}={_compact_summary(str(value), 160)}")
        results = metadata.get("results")
        if isinstance(results, list) and results:
            matches = []
            for item in results[:8]:
                if not isinstance(item, dict):
                    continue
                match_path = item.get("path")
                line = item.get("line")
                if match_path:
                    matches.append(f"{match_path}:{line}" if line else str(match_path))
            if matches:
                parts.append(f"matches={_compact_summary(', '.join(matches), 260)}")
        return "; ".join(parts)
    if tool_name == "find_images":
        try:
            images = json.loads(result.content)
        except Exception:
            images = []
        if isinstance(images, list):
            parts.append(f"matches={len(images)}")
            sample = [
                str(item.get("path"))
                for item in images[:6]
                if isinstance(item, dict) and item.get("path")
            ]
            if sample:
                parts.append(f"sample={_compact_summary(', '.join(sample), 240)}")
        return "; ".join(parts)
    if tool_name == "view_image":
        for key in ("path", "mime_type", "detail", "bytes"):
            value = metadata.get(key)
            if value not in (None, ""):
                parts.append(f"{key}={_compact_summary(str(value), 160)}")
        if not parts and result.content:
            parts.append(_compact_summary(result.content, 160))
        return "; ".join(parts)
    return ""


def _tool_result_line_count(result: ToolResult) -> int | None:
    raw_line_count = result.metadata.get("line_count")
    if isinstance(raw_line_count, int) and raw_line_count > 0:
        return raw_line_count
    if result.ok and result.content and not result.metadata.get("truncated"):
        return len(result.content.splitlines())
    return None


def _tool_result_start_line(result: ToolResult) -> int | None:
    raw_start = result.metadata.get("start_line")
    if isinstance(raw_start, int) and raw_start > 0:
        return raw_start
    if result.ok and result.content and not result.metadata.get("truncated"):
        return 1
    return None


def _tool_result_end_line(result: ToolResult) -> int | None:
    raw_end = result.metadata.get("end_line")
    if isinstance(raw_end, int) and raw_end > 0:
        return raw_end
    if result.ok and result.content and not result.metadata.get("truncated"):
        return len(result.content.splitlines())
    return None


def _browser_fact_output(result: ToolResult) -> str:
    metadata = result.metadata
    parts = []
    for key in ("requested_url", "final_url", "title", "semantic_failure"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(f"{key}={_compact_summary(value, 120)}")
    failure_summary = metadata.get("failure_summary")
    if isinstance(failure_summary, str) and failure_summary.strip():
        parts.append(f"failure={_compact_summary(failure_summary, 180)}")
    network_hint = _latest_browser_network_summary(metadata)
    if network_hint:
        parts.append(f"network={network_hint}")
    action_results = metadata.get("action_results")
    if isinstance(action_results, list):
        failed_action = next(
            (item for item in action_results if isinstance(item, dict) and item.get("ok") is False),
            None,
        )
        if isinstance(failed_action, dict):
            parts.append(
                "failed_action="
                + _compact_summary(
                    f"{failed_action.get('type') or ''} {failed_action.get('selector') or ''} "
                    f"{failed_action.get('error') or ''}",
                    180,
                )
            )
    return "; ".join(parts[:6])


def _latest_browser_network_summary(metadata: dict[str, object]) -> str:
    responses = metadata.get("network_responses")
    if not isinstance(responses, list):
        return ""
    interesting: list[dict[str, object]] = []
    for item in responses:
        if not isinstance(item, dict):
            continue
        resource_type = str(item.get("resource_type") or "")
        method = str(item.get("method") or "GET").upper()
        status = item.get("status")
        if resource_type not in {"document", "fetch", "xhr"}:
            continue
        if method != "GET" or (isinstance(status, int) and status >= 300):
            interesting.append(item)
    if not interesting:
        return ""
    latest = interesting[-1]
    return _compact_summary(
        f"{latest.get('method') or ''} {latest.get('url') or ''} status={latest.get('status')}",
        180,
    )


def _http_fact_output(result: ToolResult) -> str:
    metadata = result.metadata
    parts = []
    for key in ("method", "url", "status_code", "error"):
        value = metadata.get(key)
        if value is not None and str(value).strip():
            parts.append(f"{key}={_compact_summary(str(value), 140)}")
    body = metadata.get("body_excerpt")
    if isinstance(body, str) and body.strip():
        parts.append(f"body={_compact_summary(body, 160)}")
    return "; ".join(parts)


def _service_fact_output(result: ToolResult) -> str:
    metadata = result.metadata
    parts = []
    for key in ("command", "cwd", "port", "ready", "exit_code", "pid", "log_path"):
        value = metadata.get(key)
        if value is not None and str(value).strip():
            limit = 160 if key in {"command", "log_path"} else 100
            parts.append(f"{key}={_compact_summary(str(value), limit)}")
    log_tail = metadata.get("log_tail")
    if isinstance(log_tail, str) and log_tail.strip():
        parts.append(f"log_tail={_compact_summary(log_tail, 180)}")
    return "; ".join(parts)


def _startup_report_tool_fact(metadata: dict[str, object]) -> str:
    candidates = [item for item in metadata.get("candidates", []) if isinstance(item, dict)]
    config_refs = [item for item in metadata.get("config_refs", []) if isinstance(item, dict)]
    warnings = [item for item in metadata.get("warnings", []) if isinstance(item, dict)]
    if not candidates and not config_refs and not warnings:
        return ""
    parts = ["tool=inspect_project_startup", "status=ok"]
    if candidates:
        candidate_parts = []
        for item in candidates[:8]:
            candidate_parts.append(
                "kind={kind}; cwd={cwd}; command={command}; long_running={long}; port={port}; source={source}".format(
                    kind=_compact_summary(str(item.get("kind") or ""), 40),
                    cwd=_compact_summary(str(item.get("cwd") or ""), 80),
                    command=_compact_summary(str(item.get("command") or ""), 120),
                    long=bool(item.get("expected_long_running")),
                    port=item.get("default_port") or "",
                    source=_compact_summary(str(item.get("source") or ""), 100),
                )
            )
        parts.append("startup_candidates=[" + " | ".join(candidate_parts) + "]")
    if config_refs:
        ref_parts = []
        for item in config_refs[:6]:
            ref_parts.append(
                "source={source}; kind={kind}; target={target}; port={port}".format(
                    source=_compact_summary(str(item.get("source") or ""), 100),
                    kind=_compact_summary(str(item.get("kind") or ""), 40),
                    target=_compact_summary(str(item.get("target") or item.get("url") or ""), 120),
                    port=item.get("port") or "",
                )
            )
        parts.append("config_refs=[" + " | ".join(ref_parts) + "]")
    if warnings:
        warning_parts = []
        for item in warnings[:4]:
            warning_parts.append(
                "kind={kind}; source={source}; expected={expected}; observed={observed}".format(
                    kind=_compact_summary(str(item.get("kind") or ""), 60),
                    source=_compact_summary(str(item.get("source") or ""), 80),
                    expected=item.get("expected_port") or item.get("expected") or "",
                    observed=item.get("observed_port") or item.get("observed") or "",
                )
            )
        parts.append("warnings=[" + " | ".join(warning_parts) + "]")
    return "; ".join(parts)


_ACCEPTANCE_COMMAND_PURPOSES = {"verify", "build", "test", "smoke", "check", "lint"}


def _failure_target_result_evidence(
    tool_name: str,
    arguments: dict[str, object],
    result: ToolResult,
) -> str:
    if tool_name == "http_request":
        return (
            f"http status={result.metadata.get('status_code')} "
            f"error={result.metadata.get('error')}"
        )
    if tool_name == "run_command":
        return f"command failed: {_compact_summary(str(arguments.get('command') or ''))}"
    return result.content[:500]


def _project_memory_candidate_note(
    tool_name: str,
    arguments: dict[str, object],
    result: ToolResult,
) -> str | None:
    command = str(result.metadata.get("command") or arguments.get("command") or "").strip()
    cwd = str(result.metadata.get("cwd") or arguments.get("cwd") or "").strip()
    purpose = str(arguments.get("purpose") or result.metadata.get("purpose") or "").strip().lower()
    command_normalization = result.metadata.get("command_normalization")
    if isinstance(command_normalization, str) and command_normalization.strip() and command:
        return (
            "project command/path convention observed; if this normalized command is the stable way to run "
            f"the project, remember_workflow with command={command!r} cwd={cwd or '.'!r}"
        )
    nearby_venvs = result.metadata.get("nearby_python_venvs")
    if isinstance(nearby_venvs, list) and nearby_venvs:
        candidates = [str(item) for item in nearby_venvs[:3]]
        return (
            "project Python environment candidate observed after dependency/startup failure; if verified, "
            f"remember the intended interpreter/venv path candidates={candidates}"
        )
    if tool_name == "start_background_command" and result.ok and command:
        port = result.metadata.get("port") or arguments.get("port")
        ready = result.metadata.get("ready")
        if ready is True or port:
            return (
                "verified project startup workflow observed; remember_workflow if this service command/port should "
                f"be reused command={command!r} cwd={cwd or '.'!r} port={port}"
            )
    if tool_name == "run_command" and result.ok and command:
        if purpose in {"verify", "build", "test", "smoke", "check", "lint", "install"}:
            return (
                "verified reusable project workflow observed; remember_workflow if it is a stable project "
                f"{purpose or 'command'} procedure command={command!r} cwd={cwd or '.'!r}"
            )
    if tool_name == "http_request" and result.ok and purpose in {"verify", "smoke"}:
        method = str(result.metadata.get("method") or arguments.get("method") or "GET").upper()
        url = str(result.metadata.get("url") or arguments.get("url") or "").strip()
        status_code = result.metadata.get("status_code")
        headers = arguments.get("headers")
        has_auth = isinstance(headers, dict) and any(str(key).lower() == "authorization" for key in headers)
        return (
            "verified reusable HTTP/API workflow step observed; remember_workflow if this endpoint, method, "
            "auth shape, and expected status are part of a stable integration or smoke flow. "
            f"Use steps instead of storing secrets: method={method!r} url={url!r} status_code={status_code} "
            f"auth_header_present={has_auth}"
        )
    if tool_name == "browser_test" and purpose in {"verify", "smoke"}:
        url = str(result.metadata.get("url") or result.metadata.get("requested_url") or arguments.get("url") or "").strip()
        actions = arguments.get("actions")
        action_types: list[str] = []
        if isinstance(actions, list):
            for action in actions[:12]:
                if isinstance(action, dict):
                    action_type = action.get("type")
                    if isinstance(action_type, str):
                        action_types.append(action_type)
        status = "verified" if result.ok else "observed"
        return (
            f"{status} browser/UI workflow step observed; remember_workflow if this route and action sequence "
            "is a stable UI smoke or integration procedure. Store selectors/action types, not passwords or tokens. "
            f"url={url!r} action_types={action_types}"
        )
    return None


def _classify_tool_result_failure(
    tool_name: str,
    arguments: dict[str, object],
    result: ToolResult,
) -> str | None:
    if result.ok:
        return None
    metadata_failure_kind = result.metadata.get("failure_kind")
    if isinstance(metadata_failure_kind, str) and metadata_failure_kind.strip():
        return metadata_failure_kind
    text = " ".join(
        part
        for part in (
            result.content,
            str(result.block_reason or ""),
            str(result.metadata.get("error") or ""),
            str(result.metadata.get("body_excerpt") or ""),
        )
        if part
    ).lower()
    if "recovered history:" in text:
        return "interrupted_batch"
    if tool_name == "http_request":
        status_code = result.metadata.get("status_code")
        if status_code == 404:
            return "route_missing"
        if status_code in {401, 403}:
            return "auth_missing"
        if isinstance(status_code, int) and status_code >= 500:
            return "server_error"
        if any(
            marker in text
            for marker in (
                "winerror 10061",
                "connection refused",
                "actively refused",
                "failed to establish a new connection",
                "cannot assign requested address",
                "name or service not known",
                "nodename nor servname provided",
                "temporary failure in name resolution",
            )
        ):
            return "service_unreachable"
        if "timed out" in text or "timeout" in text:
            return "timeout"
    if tool_name == "start_background_command":
        if result.metadata.get("ready") is False:
            return "startup_failed"
    if tool_name in {"run_command", "run_python", "browser_test"}:
        if result.metadata.get("timed_out") or "timed out" in text or "timeout" in text:
            return "timeout"
    if result.blocked:
        return "tool_blocked"
    return None


def _extract_failure_reproduction_targets(user_input: str) -> list[dict[str, str]]:
    targets: list[dict[str, str]] = []
    for target in _extract_http_targets(user_input):
        targets.append(target)
    for command in _extract_explicit_failure_commands(user_input):
        targets.append({"kind": "command", "entrypoint": command, "source": "user_report"})
    return targets


def _extract_http_targets(text: str) -> list[dict[str, str]]:
    targets: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in re.finditer(r"https?://[^\s'\"<>）)]+", text):
        url = match.group(0).rstrip(".,;:")
        if url not in seen:
            target = {"kind": "http", "entrypoint": url, "source": "user_report"}
            prefix = text[max(0, match.start() - 12) : match.start()].strip().upper()
            method_match = re.search(r"(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s*$", prefix)
            if method_match:
                target["method"] = method_match.group(1)
            targets.append(target)
            seen.add(url)
    return targets


def _extract_explicit_failure_commands(text: str) -> list[str]:
    commands: list[str] = []
    patterns = (
        r"(?:command|cmd|命令)\s*[:：]\s*([^\r\n]+)",
        r"`([^`\r\n]+)`",
    )
    command_heads = (
        "python ",
        "pytest",
        "npm ",
        "pnpm ",
        "yarn ",
        "go ",
        "cargo ",
        "gcc ",
        "clang ",
        "cmake ",
        "make ",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            raw = match.group(1).strip()
            raw = re.split(r"\s+(?:failed|失败|报错|error[:：]?)\b", raw, maxsplit=1, flags=re.IGNORECASE)[0]
            candidate = " ".join(raw.split())
            if candidate.lower().startswith(command_heads) and candidate not in commands:
                commands.append(candidate)
    return commands


def _tool_failure_signature(tool_name: str, arguments: dict[str, object], result: ToolResult) -> str:
    stable_args = "|".join(f"{key}={arguments[key]}" for key in sorted(arguments))
    diagnostic = _failure_diagnostic_line(result.content)
    return f"tool:{tool_name}:{stable_args}:{diagnostic}"[:500]


def _first_non_empty_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _failure_diagnostic_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines()]
    non_empty = [line for line in lines if line]
    if not non_empty:
        return ""

    section_headers = {"errors:", "failures:"}
    for index, line in enumerate(non_empty):
        if line.lower() in section_headers:
            for candidate in non_empty[index + 1 :]:
                lowered_candidate = candidate.lower()
                if lowered_candidate not in section_headers and not lowered_candidate.startswith(
                    ("command:", "exit_code:", "stdout:", "stderr:")
                ):
                    return candidate

    markers = (
        "assertionerror",
        "modulenotfounderror",
        "importerror",
        "syntaxerror",
        "typeerror",
        "valueerror",
        "keyerror",
        "attributeerror",
        "runtimeerror",
        "error:",
        "errors:",
        "failed",
        "exception",
        "semantic_failure:",
        "block_reason:",
    )
    generic_markers = ("systemcheckerror", "traceback")
    ignored_prefixes = ("command:", "exit_code:", "stdout:", "stderr:")
    generic_fallback = ""
    for line in non_empty:
        lowered = line.lower()
        if lowered.startswith(ignored_prefixes):
            continue
        if any(marker in lowered for marker in markers):
            return line
        if not generic_fallback and any(marker in lowered for marker in generic_markers):
            generic_fallback = line

    if generic_fallback:
        return generic_fallback

    for line in reversed(non_empty):
        lowered = line.lower()
        if not lowered.startswith(ignored_prefixes):
            return line
    return non_empty[0]


def _assistant_deferred_work_after_changes(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if "?" in stripped or "？" in stripped:
        return True
    # This is an assistant-output contract after concrete file changes, not a
    # user-intent router. At this point a no-tool response means the agent is
    # about to stop despite saying more work remains.
    lowered = stripped.lower()
    english_markers = (
        "i will continue",
        "i'll continue",
        "i will keep",
        "i'll keep",
        "i will proceed",
        "i'll proceed",
        "next i will",
        "then i will",
        "i will next",
        "will continue",
        "continue with",
        "continue to",
    )
    chinese_markers = (
        "我会继续",
        "我将继续",
        "我继续",
        "接下来我会",
        "接下来我将",
        "接下来继续",
        "下一步我会",
        "下一步我将",
        "后续我会",
        "后续继续",
        "继续开发",
        "继续实现",
        "继续完善",
    )
    return any(marker in lowered for marker in english_markers) or any(
        marker in stripped for marker in chinese_markers
    )


def _assistant_is_waiting_for_user_after_work(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if "?" in stripped or "？" in stripped:
        return True
    return False


def _parse_history_count(parts: list[str], command: str) -> int:
    if len(parts) != 2:
        raise ValueError(f"Usage: {command} <count>")
    try:
        count = int(parts[1])
    except ValueError as exc:
        raise ValueError(f"Usage: {command} <count>") from exc
    if count < 0:
        raise ValueError("count must be non-negative")
    return count


def _parse_image_input(user_input: str, workspace_root: Path) -> dict[str, str] | None:
    stripped = user_input.strip()
    explicit = stripped.lower().startswith(("/image ", "/img "))
    if not explicit and not _looks_like_image_reference(stripped):
        return None
    if explicit:
        _command, rest = stripped.split(maxsplit=1)
    else:
        rest = stripped
    image_path, prompt = _resolve_image_reference(rest, workspace_root)
    if not image_path.exists() or not image_path.is_file():
        raise ValueError(f"image file not found: {image_path}")
    suffix = image_path.suffix.lower()
    mime_types = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }
    if suffix not in mime_types:
        raise ValueError("unsupported image type; use png, jpg, jpeg, webp, or gif")
    max_bytes = 5 * 1024 * 1024
    size = image_path.stat().st_size
    if size > max_bytes:
        raise ValueError(f"image is too large: {size} bytes; max is {max_bytes} bytes")
    return {
        "path": str(image_path),
        "mime_type": mime_types[suffix],
        "detail": "low",
        "prompt": prompt or "\u8bf7\u5206\u6790\u8fd9\u5f20\u56fe\u7247\u7684\u5185\u5bb9\u3002",
    }


def _looks_like_image_reference(text: str) -> bool:
    lower = text.lower()
    if re.search(r"\.(png|jpg|jpeg|webp|gif)\b", lower):
        return True
    # Natural-language image attachment is intentionally conservative.  Product
    # requirements often mention "image/video upload"; those should remain text
    # requirements for the model, not silently attach the latest local image.
    product_feature_terms = (
        "upload image",
        "image upload",
        "upload picture",
        "picture upload",
        "\u4e0a\u4f20\u56fe\u7247",
        "\u56fe\u7247\u4e0a\u4f20",
        "\u4e0a\u4f20\u56fe\u50cf",
        "\u56fe\u50cf\u4e0a\u4f20",
        "\u4e0a\u4f20\u89c6\u9891",
        "\u89c6\u9891\u4e0a\u4f20",
        "\u56fe\u7247\u548c\u89c6\u9891",
        "\u56fe\u7247/\u89c6\u9891",
        "\u56fe\u7247\u89c6\u9891",
    )
    if any(term in lower for term in product_feature_terms):
        return False

    screenshot_markers = ("screenshot", "screen shot", "\u622a\u56fe", "\u6700\u65b0\u56fe")
    if any(marker in lower for marker in screenshot_markers):
        return True

    image_markers = ("image", "picture", "\u56fe\u7247", "\u56fe\u50cf")
    inspect_intents = (
        "look",
        "see",
        "check",
        "inspect",
        "analyze",
        "analyse",
        "describe",
        "what is",
        "error",
        "issue",
        "\u770b",
        "\u67e5\u770b",
        "\u68c0\u67e5",
        "\u5206\u6790",
        "\u8bc6\u522b",
        "\u91cc\u9762",
        "\u62a5\u9519",
        "\u95ee\u9898",
    )
    return any(marker in lower for marker in image_markers) and any(
        intent in lower for intent in inspect_intents
    )


def _resolve_image_reference(text: str, workspace_root: Path) -> tuple[Path, str]:
    image_path_text, prompt = _split_image_command(text)
    if image_path_text:
        image_path = Path(image_path_text)
        if image_path.is_absolute():
            if image_path.exists():
                return image_path.resolve(), prompt
        else:
            for root in _image_search_roots(workspace_root):
                candidate = (root / image_path).resolve()
                if candidate.exists():
                    return candidate, prompt
        fuzzy = _find_image_fuzzy(image_path_text, workspace_root)
        if fuzzy:
            return fuzzy, prompt
        return (workspace_root / image_path_text).resolve(), prompt

    latest = _latest_image(workspace_root)
    if latest:
        return latest, text.strip()
    raise ValueError("usage: /image <path-or-image-name> [prompt]")


def _split_image_command(text: str) -> tuple[str, str]:
    stripped = text.strip()
    if not stripped:
        return "", ""
    if stripped[0] in {"'", '"'}:
        quote = stripped[0]
        end = stripped.find(quote, 1)
        if end == -1:
            raise ValueError("image path quote is not closed")
        return stripped[1:end], stripped[end + 1 :].strip()
    match = re.search(
        r"([A-Za-z0-9_.:/\\ -]+\.(?:png|jpg|jpeg|webp|gif))",
        stripped,
        flags=re.IGNORECASE,
    )
    if match:
        path = match.group(1).strip()
        prompt = (stripped[: match.start()] + stripped[match.end() :]).strip()
        return path, prompt
    return "", stripped


def _image_search_roots(workspace_root: Path) -> list[Path]:
    roots = [workspace_root.resolve(), workspace_root.resolve().parent, Path.cwd().resolve()]
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root).lower()
        if root.exists() and key not in seen:
            unique.append(root)
            seen.add(key)
    return unique


def _iter_images(workspace_root: Path, *, broad: bool = True) -> list[Path]:
    ignored = {".git", ".venv", "node_modules", ".minicodex2", ".pytest_cache", ".ruff_cache"}
    suffixes = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    found: dict[str, Path] = {}
    roots = _image_search_roots(workspace_root) if broad else [workspace_root.resolve()]
    for root in roots:
        for path in root.rglob("*"):
            if any(part in ignored for part in path.parts):
                continue
            if path.is_file() and path.suffix.lower() in suffixes:
                found[str(path.resolve()).lower()] = path.resolve()
    return list(found.values())


def _find_image_fuzzy(reference: str, workspace_root: Path) -> Path | None:
    requested = Path(reference.strip()).name.lower()
    requested_stem = Path(requested).stem
    images = _iter_images(workspace_root, broad=False) or _iter_images(workspace_root, broad=True)
    for image in images:
        if image.name.lower() == requested:
            return image
    for image in images:
        if image.stem.lower() == requested_stem:
            return image
    for image in images:
        stem = image.stem.lower()
        if requested_stem and (requested_stem in stem or stem in requested_stem):
            return image
    names = {image.name.lower(): image for image in images}
    matches = difflib.get_close_matches(requested, list(names), n=1, cutoff=0.72)
    if matches:
        return names[matches[0]]
    return None


def _latest_image(workspace_root: Path) -> Path | None:
    images = _iter_images(workspace_root, broad=False) or _iter_images(workspace_root, broad=True)
    if not images:
        return None
    return max(images, key=lambda path: path.stat().st_mtime)


def _format_model_context_dump(
    *,
    session_id: str,
    turn_id: str,
    call_index: int,
    model: str,
    messages: list[ChatMessage],
    tool_count: int,
    estimated_tokens: int,
    runtime_summary: dict[str, Any],
    max_chars_per_message: int,
) -> str:
    lines = [
        "# MiniCodex2 Model Context Dump",
        "",
        "This file shows the message list assembled immediately before the model call.",
        "It is for local debugging only. `runtime` messages are sent to OpenAI-compatible APIs as `system` messages.",
        "",
        "## Request",
        "",
        f"- session_id: `{session_id}`",
        f"- turn_id: `{turn_id}`",
        f"- model_call_index: `{call_index}`",
        f"- model: `{model}`",
        f"- messages: `{len(messages)}`",
        f"- tools: `{tool_count}`",
        f"- estimated_tokens: `{estimated_tokens}`",
        "",
        "## Runtime Context Keys",
        "",
    ]
    if runtime_summary:
        for key in sorted(runtime_summary):
            value = runtime_summary[key]
            if isinstance(value, (str, int, float, bool)) or value is None:
                rendered = repr(value)
            elif isinstance(value, list):
                rendered = f"list[{len(value)}]"
            elif isinstance(value, dict):
                rendered = f"dict[{len(value)}]"
            else:
                rendered = type(value).__name__
            lines.append(f"- `{key}`: {rendered}")
    else:
        lines.append("- none")
    lines.extend(["", "## Messages", ""])
    for index, message in enumerate(messages, start=1):
        model_role = "system" if message.role == "runtime" else message.role
        metadata = _context_dump_metadata(message)
        lines.extend(
            [
                f"### {index}. role={message.role} model_role={model_role}",
                "",
            ]
        )
        if message.name:
            lines.append(f"- name: `{message.name}`")
        if message.tool_call_id:
            lines.append(f"- tool_call_id: `{message.tool_call_id}`")
        if metadata:
            lines.append("- metadata:")
            lines.append("```json")
            lines.append(json.dumps(redact_payload(metadata), ensure_ascii=False, indent=2))
            lines.append("```")
        lines.extend(["", "```text"])
        lines.append(_context_dump_content(message, max_chars=max_chars_per_message))
        lines.extend(["```", ""])
    return "\n".join(lines).rstrip() + "\n"


def _context_dump_metadata(message: ChatMessage) -> dict[str, Any]:
    metadata = dict(message.metadata)
    if "tool_calls" in metadata:
        tool_calls = metadata["tool_calls"]
        if isinstance(tool_calls, list):
            metadata["tool_calls"] = [
                {
                    "id": call.get("id"),
                    "type": call.get("type"),
                    "function": {
                        "name": (call.get("function") or {}).get("name"),
                        "arguments": (call.get("function") or {}).get("arguments"),
                    },
                }
                if isinstance(call, dict)
                else call
                for call in tool_calls
            ]
    return metadata


def _context_dump_content(message: ChatMessage, *, max_chars: int) -> str:
    content = message.content
    image = message.metadata.get("image")
    if isinstance(image, dict):
        content += (
            "\n\n[image metadata only in dump; model adapter may attach image bytes]\n"
            f"path={image.get('path')}\n"
            f"mime_type={image.get('mime_type')}\n"
            f"detail={image.get('detail')}"
        )
    content = redact_text(content)
    content = _make_context_dump_content_readable(content)
    if len(content) <= max_chars:
        return content
    omitted = len(content) - max_chars
    return content[:max_chars] + f"\n\n[... {omitted} characters omitted from context dump only ...]"


def _has_context_runtime_anchor(messages: list[ChatMessage]) -> bool:
    for message in messages[:8]:
        if message.role not in {"runtime", "system"}:
            continue
        content = message.content or ""
        if (
            "[RUNTIME PROTOCOL]" in content
            or "[ENVIRONMENT]" in content
            or "[PROJECT GUIDANCE]" in content
            or "[CONTEXT CHECKPOINT HANDOFF]" in content
        ):
            return True
    return False


def _normalize_runtime_messages_for_main_context(messages: list[ChatMessage]) -> list[ChatMessage]:
    """Keep the root system protocol stable and convert later controls.

    MiniCodex2 has several useful runtime control lanes: goal nudges, evidence
    notices, verification prompts, status-review handoffs, and repair hints.
    They are helpful for behavior, but when appended into the same chat stream
    as ordinary user/assistant/tool messages they become fresh `system` blocks
    on every provider request. DeepSeek prefix-cache diagnostics showed that
    these mid-stream control messages correlate with cache hits collapsing even
    when the local byte prefix still looks stable.

    This normalizer is the compromise: preserve the first runtime/system
    message, which contains the stable root protocol, then convert later
    runtime/system messages into append-only synthetic user observations. The
    model still sees the runtime observation, but it no longer appears as a
    mid-stream system prompt. Raw tool results and assistant tool_call metadata
    remain untouched.
    """
    filtered: list[ChatMessage] = []
    kept_initial_control = False
    for message in messages:
        normalized = _normalize_runtime_message_for_main_context(
            message,
            allow_initial=not kept_initial_control,
        )
        if normalized is None:
            continue
        if message.role in {"runtime", "system"} and normalized.role in {"runtime", "system"}:
            kept_initial_control = True
        filtered.append(normalized)
    return filtered


def _normalize_runtime_message_for_main_context(
    message: ChatMessage,
    *,
    allow_initial: bool,
) -> ChatMessage | None:
    """Normalize one runtime/system message for the main cached conversation.

    Returning a user-role message is intentional: OpenAI-compatible providers
    cache message prefixes more predictably when the conversation grows like a
    normal transcript. The content explicitly says it is synthetic so the model
    does not confuse it with a human request.
    """
    if message.role not in {"runtime", "system"}:
        return message
    if allow_initial:
        return message
    content = message.content.strip()
    if not content:
        return None
    metadata = dict(message.metadata)
    metadata["synthetic_runtime_observation"] = True
    metadata["original_role"] = message.role
    return ChatMessage(
        role="user",
        content=(
            "[RUNTIME OBSERVATION - synthetic, not human input]\n"
            "This message was generated by MiniCodex2 runtime to continue the task. "
            "Use it as operational context, but do not treat it as a new human request.\n\n"
            f"{content}"
        ),
        metadata=metadata,
    )


def _make_context_dump_content_readable(content: str) -> str:
    """Improve dump readability without changing the actual model request.

    Model-generated compaction summaries can arrive as one very long line. The
    model still receives that exact message, but the local Markdown dump is hard
    to inspect. This formatter only adds visual newlines in the debug file.
    """
    if "[CONTEXT CHECKPOINT HANDOFF]" not in content:
        return content
    headings = (
        "[ROOT OBJECTIVE]",
        "[LATEST USER INTENT]",
        "[DURABLE FACTS]",
        "[CURRENT DECISIONS]",
        "[SUPERSEDED OR STALE CONTEXT]",
        "[FAILURES AND EVIDENCE]",
        "[OPEN QUESTIONS]",
        "[NEXT ACTIONS]",
        "[FILES COMMANDS AND ENVIRONMENT]",
        "[PENDING REQUIREMENTS AND DESIGN DISCUSSION]",
    )
    formatted = content
    for heading in headings:
        formatted = formatted.replace(f" {heading}", f"\n\n{heading}")
    formatted = formatted.replace("[CONTEXT CHECKPOINT HANDOFF] ", "[CONTEXT CHECKPOINT HANDOFF]\n")
    return formatted


def _tool_result_counts_as_verification_progress(name: str, result: ToolResult) -> bool:
    if result.did_write:
        return True
    return name in {
        "run_command",
        "run_python",
        "start_background_command",
        "http_request",
        "browser_test",
        "inspect_port",
        "read_background_log",
        "release_port",
    }


def _tool_result_satisfies_model_selected_verification(name: str) -> bool:
    return name in {"run_command", "run_python", "http_request", "browser_test"}


def _dedupe_keep_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _requested_toolchain_install(user_input: str) -> str | None:
    text = user_input.lower()
    install_markers = ("安装", "帮我安装", "install")
    if not any(marker in text for marker in install_markers):
        return None
    toolchain_markers = {
        "go": ("go", "golang"),
    }
    for name, markers in toolchain_markers.items():
        if any(marker in text for marker in markers):
            return name
    if "工具链" in text or "toolchain" in text:
        return "go"
    return None


def _project_context_snapshot(workspace_root: Path, changed_files: list[str]) -> str:
    important_names = {
        "package.json",
        "pyproject.toml",
        "requirements.txt",
        "manage.py",
        "Makefile",
        "CMakeLists.txt",
        "Cargo.toml",
        "go.mod",
        "README.md",
        "AGENTS.md",
    }
    ignored = {".git", ".venv", "venv", "node_modules", ".minicodex2", "build", "dist", "__pycache__"}
    files: list[str] = []
    try:
        for path in workspace_root.rglob("*"):
            if any(part in ignored for part in path.parts):
                continue
            if path.is_file() and path.name in important_names:
                files.append(path.relative_to(workspace_root).as_posix())
            if len(files) >= 40:
                break
    except OSError:
        pass
    lines = [
        f"- workspace_root: {workspace_root}",
        "- recent_changed_files: " + (", ".join(changed_files[-20:]) if changed_files else "none"),
        "- important_project_files:",
    ]
    if files:
        lines.extend(f"  - {path}" for path in files[:40])
    else:
        lines.append("  - none detected")
    return "\n".join(lines)


def _nearest_detected_project_root(
    start: Path,
    workspace_root: Path,
    detect_project: Callable[[Path], Any],
) -> Path | None:
    workspace = workspace_root.resolve()
    candidate = start.resolve()
    if candidate.is_file():
        candidate = candidate.parent
    while True:
        try:
            candidate.relative_to(workspace)
        except ValueError:
            return None
        profile = detect_project(candidate)
        if getattr(profile, "detected_types", None):
            return candidate
        if candidate == workspace:
            return None
        candidate = candidate.parent


def _python_dependency_install_command(root: Path) -> str | None:
    if (root / "requirements.txt").exists():
        return "python -m pip install -r requirements.txt"
    if (root / "pyproject.toml").exists():
        return "python -m pip install -e ."
    return None
