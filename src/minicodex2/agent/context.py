from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from minicodex2.agent.chat_history import ChatHistory
from minicodex2.model.messages import ChatMessage

IMAGE_TOKEN_ESTIMATE = 1000
MAX_CONTEXT_IMAGES = 1
CompactionCallback = Callable[[list[ChatMessage], int], str | None]

# ?? Stable protocol text cache (fix 4: deterministic truncation) ??
# Same stable sections should produce identical text regardless of budget changes.
_STABLE_TEXT_CACHE: dict[int, str] = {}

_STABLE_RUNTIME_HEADERS = {
    "RUNTIME PROTOCOL",
    "ENVIRONMENT",
    "SKILL CONTEXT",
    "PROJECT GUIDANCE",
    "PROJECT MEMORY INDEX",
}


@dataclass(frozen=True, slots=True)
class ContextBudgetPlan:
    total_tokens: int
    trigger_tokens: int
    runtime_summary_tokens: int
    runtime_section_tokens: int
    checkpoint_target_chars: int
    recent_target_tokens: int
    reserve_tokens: int


@dataclass(slots=True)
class BuiltContext:
    messages: list[ChatMessage]
    estimated_tokens: int
    compaction_checkpoint: ChatMessage | None = None
    runtime_summary_tokens: int = 0
    runtime_section_tokens: dict[str, int] | None = None
    cleared_tool_results: int = 0
    message_estimated_tokens: int = 0
    static_overhead_tokens: int = 0


@dataclass(frozen=True, slots=True)
class RuntimeContextParts:
    messages: list[ChatMessage]
    tokens: int
    section_tokens: dict[str, int]

    @property
    def stable_messages(self) -> list[ChatMessage]:
        return [
            message
            for message in self.messages
            if message.metadata.get("context_part") == "stable_runtime"
        ]

    @property
    def dynamic_messages(self) -> list[ChatMessage]:
        return [
            message
            for message in self.messages
            if message.metadata.get("context_part") != "stable_runtime"
        ]


class ContextManager:
    def build_context(
        self,
        *,
        history: ChatHistory,
        extra_messages: list[ChatMessage] | None = None,
        runtime_summary: dict[str, Any] | None = None,
        token_budget: int = 128_000,
        compression_threshold: float = 0.70,
        baseline_ratio: float = 0.28,
        runtime_summary_token_budget: int | None = None,
        runtime_section_token_budget: int | None = None,
        tool_result_raw_keep: int = 8,
        tool_result_summary_chars: int = 700,
        static_overhead_tokens: int = 0,
        compaction_callback: CompactionCallback | None = None,
    ) -> BuiltContext:
        static_overhead_tokens = max(0, int(static_overhead_tokens))
        budget_plan = plan_context_budget(
            token_budget=token_budget,
            compression_threshold=compression_threshold,
            baseline_ratio=baseline_ratio,
            runtime_summary_token_budget=runtime_summary_token_budget,
            runtime_section_token_budget=runtime_section_token_budget,
        )
        # Tool definitions and request-level protocol scaffolding are billed as
        # prompt input by OpenAI-compatible providers, but they are not part of
        # chat history. Keep the user-facing estimate and auto-compaction budget
        # honest by reserving space for that static payload before trimming the
        # model-visible messages.
        message_trigger_tokens = max(500, budget_plan.trigger_tokens - static_overhead_tokens)
        active_messages = history.active_messages()
        messages = _limit_context_images(active_messages, max_images=MAX_CONTEXT_IMAGES)
        cleared_tool_results = 0
        runtime_summary_tokens = 0
        runtime_section_tokens: dict[str, int] | None = None
        if runtime_summary:
            runtime_parts = self._format_summary_messages(
                runtime_summary,
                token_budget=budget_plan.runtime_summary_tokens,
                section_token_budget=budget_plan.runtime_section_tokens,
            )
            runtime_summary_tokens = runtime_parts.tokens
            runtime_section_tokens = runtime_parts.section_tokens
            # Keep stable runtime sections at the very front so provider-side
            # prefix caches can reuse them across repeated model calls in one
            # long turn. High-churn facts/evidence/work-memory are appended
            # after active history below, where they cannot invalidate the
            # stable prefix on every tool result.
            messages = [*runtime_parts.stable_messages, *messages]
        message_estimated = _estimate_tokens(messages)
        estimated = message_estimated + static_overhead_tokens
        compaction_checkpoint = None
        if estimated > budget_plan.trigger_tokens:
            messages, compaction_checkpoint = self._compress(
                messages,
                message_trigger_tokens,
                checkpoint_target_chars=budget_plan.checkpoint_target_chars,
                recent_target_tokens=budget_plan.recent_target_tokens,
                compaction_callback=compaction_callback,
                tool_result_raw_keep=tool_result_raw_keep,
                tool_result_summary_chars=tool_result_summary_chars,
            )
            cleared_tool_results = _count_cleared_tool_results(messages)
            message_estimated = _estimate_tokens(messages)
            estimated = message_estimated + static_overhead_tokens
        # Do not inject volatile runtime tails on every model call. Fresh tool
        # facts/evidence are already appended to history as runtime events; a
        # rebuilt tail at this position becomes a moving prefix-cache breaker.
        # Durable stable guidance remains at the front, while changing state is
        # consumed from append-only history/checkpoints or explicit tools.
        if extra_messages:
            messages = [*messages, *extra_messages]
            message_estimated = _estimate_tokens(messages)
            estimated = message_estimated + static_overhead_tokens
        return BuiltContext(
            messages=messages,
            estimated_tokens=estimated,
            compaction_checkpoint=compaction_checkpoint,
            runtime_summary_tokens=runtime_summary_tokens,
            runtime_section_tokens=runtime_section_tokens,
            cleared_tool_results=cleared_tool_results,
            message_estimated_tokens=message_estimated,
            static_overhead_tokens=static_overhead_tokens,
        )

    def _compress(
        self,
        messages: list[ChatMessage],
        token_budget: int,
        *,
        checkpoint_target_chars: int | None = None,
        recent_target_tokens: int | None = None,
        compaction_callback: CompactionCallback | None = None,
        tool_result_raw_keep: int = 8,
        tool_result_summary_chars: int = 600,
    ) -> tuple[list[ChatMessage], ChatMessage | None]:
        # Clear old tool results only during compaction (not every turn)
        messages = _clear_old_tool_results(
            messages,
            keep_recent=tool_result_raw_keep,
            summary_chars=tool_result_summary_chars,
        )
        if len(messages) <= 6:
            return messages, None
        runtime_prefix: list[ChatMessage] = []
        body = messages
        while body and body[0].role == "runtime" and _is_runtime_summary_message(body[0].content):
            runtime_prefix.append(body[0])
            body = body[1:]
        keep = body[-20:]
        keep = _drop_leading_tool_messages(keep)
        minimum_summary_limit = 296
        runtime_tokens = _estimate_tokens(runtime_prefix)
        summary_limit = max(minimum_summary_limit, checkpoint_target_chars or int(token_budget * 0.25))
        recent_budget = max(250, recent_target_tokens or int(max(1, token_budget - runtime_tokens) * 0.35))
        post_compaction_budget = min(token_budget, runtime_tokens + max(1, summary_limit // 4) + recent_budget)
        placeholder = ChatMessage(
            role="runtime",
            content="[CONTEXT CHECKPOINT HANDOFF]\n" + ("x" * summary_limit),
        )
        while _estimate_tokens([*runtime_prefix, placeholder, *keep]) > post_compaction_budget and len(keep) > 2:
            keep.pop(0)
            keep = _drop_leading_tool_messages(keep)
        omitted = body[: len(body) - len(keep)]
        summary = _summarize_with_optional_callback(
            omitted,
            limit=summary_limit,
            compaction_callback=compaction_callback,
        )
        prefix = ChatMessage(role="runtime", content=summary)
        compressed = [*runtime_prefix, prefix, *keep]
        while _estimate_tokens(compressed) > post_compaction_budget and len(keep) > 2:
            keep.pop(0)
            keep = _drop_leading_tool_messages(keep)
            omitted = body[: len(body) - len(keep)]
            compressed = [*runtime_prefix, prefix, *keep]
        while _estimate_tokens(compressed) > post_compaction_budget and len(keep) > 2:
            keep.pop(0)
            keep = _drop_leading_tool_messages(keep)
            compressed = [*runtime_prefix, prefix, *keep]
        if _estimate_tokens(compressed) > post_compaction_budget:
            fixed_tokens = _estimate_tokens([*runtime_prefix, *keep])
            available_chars = max(minimum_summary_limit, (post_compaction_budget - fixed_tokens - 8) * 4)
            prefix = ChatMessage(
                role="runtime",
                content=_compact_text(prefix.content, available_chars),
            )
            compressed = [*runtime_prefix, prefix, *keep]
        if _estimate_tokens(compressed) > token_budget:
            fixed_tokens = _estimate_tokens([*runtime_prefix, *keep])
            available_chars = max(minimum_summary_limit, (token_budget - fixed_tokens - 8) * 4)
            prefix = ChatMessage(
                role="runtime",
                content=_compact_text(prefix.content, available_chars),
            )
            compressed = [*runtime_prefix, prefix, *keep]
        checkpoint = ChatMessage(
            role="runtime",
            content=prefix.content,
            metadata={
                "kind": "compaction_checkpoint_candidate",
                "source_message_ids": [
                    message.metadata.get("message_id")
                    for message in omitted
                    if isinstance(message.metadata.get("message_id"), str)
                ],
                "replacement_history": [
                    message
                    for message in [prefix, *keep]
                    if not (
                        message.role == "runtime"
                        and _is_runtime_summary_message(message.content)
                    )
                ],
                "source_messages": omitted,
            },
        )
        return compressed, checkpoint

    def compact_messages(
        self,
        messages: list[ChatMessage],
        *,
        token_budget: int,
        static_overhead_tokens: int = 0,
        compression_threshold: float = 0.70,
        baseline_ratio: float = 0.28,
        runtime_summary_token_budget: int | None = None,
        runtime_section_token_budget: int | None = None,
        compaction_callback: CompactionCallback | None = None,
        tool_result_raw_keep: int = 8,
        tool_result_summary_chars: int = 700,
    ) -> tuple[list[ChatMessage], ChatMessage | None]:
        """Compact an already materialized model-message buffer.

        `build_context()` estimates and compacts freshly rebuilt history, but
        MiniCodex2's cache-friendly request path can send a persisted append-only
        context buffer instead.  That final buffer may grow beyond the soft
        request limit even when the rebuilt context fragment is small.  This
        method applies the same budget math to the final buffer so callers can
        deliberately rewrite it only at compaction boundaries.
        """

        static_overhead_tokens = max(0, int(static_overhead_tokens))
        budget_plan = plan_context_budget(
            token_budget=token_budget,
            compression_threshold=compression_threshold,
            baseline_ratio=baseline_ratio,
            runtime_summary_token_budget=runtime_summary_token_budget,
            runtime_section_token_budget=runtime_section_token_budget,
        )
        message_trigger_tokens = max(500, budget_plan.trigger_tokens - static_overhead_tokens)
        return self._compress(
            messages,
            message_trigger_tokens,
            checkpoint_target_chars=budget_plan.checkpoint_target_chars,
            recent_target_tokens=budget_plan.recent_target_tokens,
            compaction_callback=compaction_callback,
            tool_result_raw_keep=tool_result_raw_keep,
            tool_result_summary_chars=tool_result_summary_chars,
        )

    def _format_summary(
        self,
        summary: dict[str, Any],
        *,
        token_budget: int = 2_500,
        section_token_budget: int = 900,
    ) -> str:
        sections = self._summary_sections(summary)
        return _fit_runtime_summary(
            sections,
            token_budget=token_budget,
            section_token_budget=section_token_budget,
        )

    def _format_summary_messages(
        self,
        summary: dict[str, Any],
        *,
        token_budget: int = 2_500,
        section_token_budget: int = 900,
    ) -> RuntimeContextParts:
        sections = self._summary_sections(summary)
        stable_sections = [
            section
            for section in sections
            if _section_header(section) in _STABLE_RUNTIME_HEADERS
        ]
        if not stable_sections:
            text = _fit_runtime_summary(
                sections,
                token_budget=token_budget,
                section_token_budget=section_token_budget,
            )
            return RuntimeContextParts(
                messages=[
                    ChatMessage(
                        role="runtime",
                        content=text,
                        metadata={"context_part": "stable_runtime"},
                    )
                ],
                tokens=_estimate_text_tokens(text),
                section_tokens=_section_token_report(text),
            )

        stable_budget = _stable_runtime_token_budget(token_budget)
        stable_key = hash(tuple(line for sec in stable_sections for line in sec))
        if stable_key in _STABLE_TEXT_CACHE:
            stable_text = _STABLE_TEXT_CACHE[stable_key]
        else:
            stable_text = _fit_runtime_summary(
                stable_sections,
                token_budget=stable_budget,
                section_token_budget=section_token_budget,
            )
            _STABLE_TEXT_CACHE[stable_key] = stable_text
        messages = [
            ChatMessage(
                role="runtime",
                content=stable_text,
                metadata={"context_part": "stable_runtime"},
            ),
        ]
        return RuntimeContextParts(
            messages=messages,
            tokens=_estimate_tokens(messages),
            section_tokens=_section_token_report(stable_text),
        )

    def _summary_sections(self, summary: dict[str, Any]) -> list[list[str]]:
        sections: list[list[str]] = []
        sections.append([
            "[RUNTIME PROTOCOL]",
            "- Prompt protocol rule: Treat runtime messages as the natural-language control protocol between Runtime and model, not as ordinary chat. They define roles, priorities, tool semantics, evidence rules, and stop/continue conditions.",
            "- Prompt protocol rule: Tool schemas are the typed API surface; runtime protocol text explains when and why to use those APIs. If a tool schema and ordinary conversation conflict, trust the schema and runtime protocol.",
            "- Prompt protocol rule: Runtime supplies perception, action, memory, evidence, and constraints. The model supplies semantic judgment. Do not wait for Runtime to make product/project decisions that require reading evidence and reasoning.",
            "- Prompt protocol rule: Read raw tool results, EVIDENCE EVENT messages, WORK MEMORY, PROJECT/WORKFLOW MEMORY, and CONTEXT CHECKPOINT HANDOFF as structured operating context. Use them before rediscovering facts or repeating failed attempts.",
            "- Prompt protocol rule: If a prompt section says a requirement, blocker, credential, path, command, port, or decision is pending/durable, keep it alive until it is implemented, verified, superseded, or explicitly rejected.",
            "- Corrigibility principle: Seek truth through evidence, reduce uncertainty with tools, revise wrong assumptions, verify success, and prefer better perception/tooling/memory/feedback over special-case runtime patches.",
            "- Code reasoning rule: Symbol existence is not proof of behavior. When related names, handlers, routes, overlays, commands, or config keys exist but the user-facing behavior is still suspect, verify the execution path, reachability, registration/rendering entrypoint, and runtime evidence before declaring success.",
            "- Tool use rule: If the user explicitly asks to install a supported toolchain such as Go, call inspect_toolchain first and then install_toolchain; do not claim the runtime cannot install it.",
            "- Tool use rule: You can execute local workspace commands through run_command and start_background_command. If the user asks you to run, install, test, start, or execute a script, use the appropriate tool instead of claiming you cannot execute commands.",
            "- Tool use rule: For one coherent diagnostic or integration phase, batch independent read/list/inspect/check/smoke tool calls in the same response when later calls do not depend on earlier outputs. Avoid one-tool-per-model-turn pacing.",
            "- Tool use rule: Use run_command only for commands expected to exit. Use start_background_command for long-running servers such as dev servers, runserver, vite, flask, uvicorn, or streamlit.",
            "- Integration rule: If a service is unreachable and a suitable startup candidate is already known from RuntimeResources, ProjectStartup, or recent facts, start it directly with start_background_command(restart_port=true) and then smoke the expected URL. Do not call inspect_port/release_port first unless the tool result says the port is occupied or release failed.",
            "- Integration rule: When start_background_command returns ready=false, use its exit_code/log_tail/command_normalization facts immediately to correct cwd, command, environment, or prerequisites before retrying the same startup.",
            "- Background log rule: Use read_background_log(resource_id/port/pid/log_path) to inspect logs from start_background_command. Do not guess .minicodex2 log paths with raw shell commands.",
            "- Progress visibility rule: Before a non-trivial tool batch, include one short public assistant note explaining the immediate situation and why the next tool action is being taken. Keep it factual and concise. Do not reveal hidden chain-of-thought.",
            "- Verification cost rule: Use UI/browser E2E to prove the user-facing path works, but do not repeat expensive E2E just to manufacture every boundary state. After the main path is proven, verify edge cases with the smallest controllable fixture, API call, database shell, project helper, or existing script that creates the required state.",
            "- Verification cost rule: When enough concrete evidence satisfies the current acceptance claim, stop exploration and record it with update_work_step. If a distinct boundary remains, split it into a separate pending step or run one narrow targeted check.",
            "- Verification rule: Before accepting an automatic verification plan, compare changed_files, nearby directory/file facts, and the proposed command. If they appear to belong to different subprojects or toolchains, inspect nearby files and choose a verification command for the affected area.",
            "- Verification rule: Do not treat an unrelated syntax/build check as acceptance evidence for changed files. Use list_directory with recursive=true and a small max_depth, read relevant project files, or run a targeted command when the verification target is unclear.",
            "- Goal rule: For durable multi-step objectives, call create_goal explicitly. Do not rely on runtime keyword inference.",
            "- Goal rule: If there is no active_goal but the latest user message asks to continue/resume prior unfinished work, inspect recent history. If a durable unfinished objective is evident, call create_goal with that objective before continuing; otherwise ask a concise clarification.",
            "- Goal rule: For multi-step project development, do not finish a turn by promising future work. Either continue with concrete tool calls in this turn, or create/update the durable goal state so the runtime can keep progress anchored across turns.",
            "- Goal rule: For one coherent implementation step, batch related write_file/edit_file calls in the same tool-call response before verification. The runtime verifies after each write batch, so avoid one-line or one-file drip-feed edits unless the next edit depends on fresh evidence.",
            "- Goal rule: If an active goal exists, use it as the durable objective anchor across turns. Call update_goal(status='complete') only when no required work remains, or update_goal(status='blocked') only for a genuine repeated blocker.",
            "- Goal rule: Treat RootObjective, WorkPlan, and CurrentStep below as durable working memory, not as chat history. Do not change RootObjective unless the user explicitly asks.",
            "- Goal rule: The latest user message has priority over a stale CurrentStep. If the latest user request narrows, redirects, or corrects the active focus, first update WorkPlan/CurrentStep to match that focus, then act.",
            "- Goal rule: For broad goals, maintain WorkPlan with set_work_plan/update_work_step/set_current_step/accept_work_step. Advance CurrentStep with concrete tools and evidence.",
            "- Goal rule: Use get_work_plan when you need the current plan, current step, source documents, or stale-plan status without fetching the full goal snapshot.",
            "- Goal rule: For status/progress review turns, inspect goal/work-plan/documents/evidence as needed, then answer with a synthesized report instead of auto-continuing implementation.",
            "- Goal rule: During status/progress review, you may realign durable goal memory with get_work_plan, set_current_step, update_work_step, set_work_plan, accept_work_step, or sync_work_plan_from_document when observed evidence shows the stored WorkPlan is stale or mismatched.",
            "- Goal rule: Do not mark a WorkPlan step done without evidence_ids from recent EvidenceTail/EVIDENCE EVENT history, exact existing acceptance evidence, or explicit user/document acceptance recorded through accept_work_step or sync_work_plan_from_document.",
            "- Goal rule: Do not mark a goal complete until required WorkPlan steps are done with evidence and acceptance evidence exists.",
            "- Goal rule: If a project document contains the applicable plan, use sync_work_plan_from_document to merge it into WorkPlan before relying on long chat history. This preserves existing step state/evidence while adding new document items.",
            "- Goal rule: If the user adds a second-phase or substantially new milestone, prefer a new planning document and a new goal after completing/blocking/pausing the old one. If the user says it is an extension of the same project objective, merge it into the existing WorkPlan instead.",
            "- Goal rule: If the user says a step is already complete, choose the matching step_id and call accept_work_step with source='user_confirmed' and the user's reason. If only a document marks it done, use source='document_marked_done'. If you are accepting based on your own review of existing evidence, use source='model_reviewed_existing_evidence' so it is not confused with user acceptance or tool verification.",
            "- Goal rule: If the current WorkPlan step has recent failed evidence, treat that failure as the active diagnostic target. Fix the smallest cause, rerun one targeted acceptance check, then update the step as done or blocked with evidence.",
            "- Engineering skill rule: Users often describe product needs, not test plans. Translate requirements into verifiable engineering steps with acceptance criteria before treating them as done.",
            "- Engineering skill rule: A WorkPlan step should represent a coherent deliverable, not a document heading, role description, or vague section title.",
            "- Engineering skill rule: For user-visible or multi-surface work, completion requires matching evidence for the relevant surfaces. Examples include API smoke for backend behavior, build/smoke for CLI or frontend behavior, and cross-surface smoke when a client talks to a server. Choose commands from the project; do not hard-code frameworks.",
            "- Engineering skill rule: Syntax checks are useful but usually not sufficient acceptance evidence for product behavior. Use them as a first gate, then run the narrowest behavior-level verification available.",
            "- Engineering skill rule: Use UI/browser E2E to prove the user-facing path works, but do not use expensive E2E repetition to manufacture every boundary state. Once the user path is proven, verify edge cases with the smallest controllable fixture, API call, database shell, project test helper, or existing script that creates the required state.",
            "- Engineering skill rule: If a WorkPlan step contains multiple acceptance boundaries, verify the cheapest sufficient boundary for the current claim, then update the step or split remaining boundaries into separate pending steps instead of endlessly expanding the same step.",
            "- Engineering skill rule: When enough concrete evidence already satisfies the current acceptance claim, stop exploration and record it with update_work_step. Continue only if the remaining claim is distinct and worth a new targeted check.",
            "- Engineering skill rule: If a verification helper/script/import fails repeatedly, stop expanding script guesses. Inspect the stable source/config that defines the behavior, or create a deliberate project-local helper only when that is the next smallest verifiable step.",
            "- Goal rule: After completing one verified step of an active goal, continue to the next concrete step unless the whole objective is complete or genuinely blocked.",
            "- Project guidance rule: Treat AGENTS.md content below as durable project instructions. Follow it unless the user explicitly overrides it.",
            "- Integration rule: For reported failures, use local tools to reproduce, use raw tool results and EVIDENCE EVENT history, fix, and verify the same path.",
            "- Failure signal rule: Tool results may include generic failure_kind labels such as route_missing, service_unreachable, auth_missing, server_error, timeout, interrupted_batch, or tool_blocked. Treat these as structured runtime observations, not as mandatory next steps.",
            "- Failure signal rule: route_missing means the request reached a service but the endpoint/path was absent; service_unreachable means the target service could not be reached at all; auth_missing means credentials or permission failed; interrupted_batch means a prior tool batch was interrupted or resumed mid-flight.",
            "- Frontend verification rule: http_request can confirm reachability, status codes, and simple response content, but it cannot prove that a JavaScript UI really renders, routes, submits forms, or handles browser-side errors correctly.",
            "- Frontend verification rule: For web UI flows involving DOM rendering, route changes, form interaction, proxy behavior, bundle loading, or browser console/network errors, prefer browser_test with concrete actions over http_request alone.",
            "- Lesson rule: Use record_lesson/get_lessons for reusable process, assumption, tooling, or evidence lessons; never store secrets, ordinary chat, or one-off implementation details.",
            "- Project memory rule: Use remember_project_fact/search_project_memory/read_project_memory/update_project_memory for durable project-specific facts that prevent rediscovery across turns or restarts.",
            "- Project memory rule: Remember only observed or verified facts such as startup commands, toolchain paths, project-local venvs, known ports, verified test/build/smoke commands, and recurring failure fixes. Do not store secrets, guesses, ordinary chat, or one-off implementation details.",
            "- Project memory rule: Search project memory before rediscovering environment setup, startup/test commands, service ports, or a repeated failure. Read full memory content only when the index summary is insufficient.",
            "- Project memory rule: If a remembered command/path/port fails or becomes obsolete, update_project_memory(stale=true) or replace it with a verified fact instead of silently relying on it.",
            "- Requirement memory rule: Pending requirements and product decisions in memory are not ordinary chat. If they apply to the latest user request and are not already represented in the active WorkPlan, create or update the goal/work plan before implementing.",
            "- Requirement memory rule: When the user asks what was previously requested, answer from pending requirement memory, recent discussion anchors, WorkPlan, and design documents. Distinguish remembered requirements from completed verified work.",
            "- Workflow memory rule: Use remember_workflow/search_workflows/read_workflow/update_workflow for reusable project operating procedures such as startup, test, build, smoke, integration, environment, or repeated debugging flows.",
            "- Workflow memory rule: Before rediscovering how to start, test, build, or smoke a project area, search workflow memory. If a remembered workflow fails, update_workflow with failure_delta or stale=true instead of blindly repeating it.",
            "- Workflow memory rule: When a command, background service, verification, or UI/API smoke succeeds and appears stable for future turns, consider remember_workflow with cwd, command, steps, purpose, ready_check/ports, tags, and confidence.",
            "- Capability correction: Any earlier assistant message claiming it cannot run local commands or scripts is incorrect in this runtime. Use the provided command tools within workspace permissions.",
            "- Tool use rule: .bat/.cmd/.ps1 helpers are fine with run_command when they finish.",
            "- Tool use rule: Dependency installation and builds are finite but can be slow; pass a larger timeout_seconds to run_command when appropriate instead of assuming the command is stuck.",
            "- Tool use rule: Before running a script that may both set up dependencies and start a server, inspect the script or use the known content, run finite setup/test steps with run_command, then start the server with start_background_command.",
            "- Tool use rule: When startup commands, service ports, frontend/backend topology, or project entrypoints are unclear, call inspect_project_startup before guessing. If no suitable startup candidate exists, inspect files, create a small project-local run script or documented command, then verify it.",
            "- Tool use rule: Use run_python for short structured local analysis helpers, not for package/toolchain installs, long-running services, or port cleanup.",
            "- Tool use rule: For HTTP/API smoke checks, prefer http_request over run_python. Avoid importing third-party Python modules such as requests in helper scripts unless you have verified they are installed in the selected interpreter.",
            "- Tool use rule: For Django data inspection, prefer manage.py shell/commands or run_python with cwd set to the directory containing manage.py; do not guess nested sys.path values when manage.py already defines DJANGO_SETTINGS_MODULE.",
            "- Tool use rule: For temporary verification/data-prep helpers, prefer run_python with the correct cwd or run_command against an existing project command. Do not write throwaway helper scripts into source directories unless the helper itself is intended project code.",
            "- Tool use rule: When locating symbols, routes, config keys, class names, or error strings, prefer search_files over raw grep/findstr shell commands so dependency and cache directories are skipped.",
        ])
        if not _legacy_code_guidance_enabled(summary):
            sections[0] = [
                line for line in sections[0]
                if not line.startswith("- Engineering skill rule:")
            ]
            sections[0].append(
                "- Skill rule: Legacy built-in code workflow guidance is disabled because an external code skill is primary. Use load_skill for code-task workflow guidance when needed."
            )
        environment_block = _format_environment_context(summary)
        if environment_block:
            sections.append(environment_block)
        skill_block = _format_skill_context(summary)
        if skill_block:
            sections.append(skill_block)
        guidance_block = _format_project_guidance(summary)
        if guidance_block:
            sections.append(guidance_block)
        goal_block = _format_goal_state(summary)
        if goal_block:
            sections.append(goal_block)
        memory_block = _format_project_memory_index(summary)
        if memory_block:
            sections.append(memory_block)
        fact_memory_block = _format_project_fact_memory(summary)
        if fact_memory_block:
            sections.append(fact_memory_block)
        workflow_memory_block = _format_workflow_memory(summary)
        if workflow_memory_block:
            sections.append(workflow_memory_block)
        turn_memory_block = _format_turn_memory_summary(summary)
        if turn_memory_block:
            sections.append(turn_memory_block)
        pending_memory_block = _format_pending_requirement_memory(summary)
        if pending_memory_block:
            sections.append(pending_memory_block)
        evidence_block = _format_evidence_store(summary)
        if evidence_block:
            sections.append(evidence_block)
        workspace_block = _format_workspace_context(summary)
        if workspace_block:
            sections.append(workspace_block)
        evidence_pack_block = _format_evidence_pack(summary)
        if evidence_pack_block:
            sections.append(evidence_pack_block)
        lessons_block = _format_lesson_memory(summary)
        if lessons_block:
            sections.append(lessons_block)
        facts_block = _format_engineering_facts(summary)
        if facts_block:
            sections.append(facts_block)
        resource_block = _format_runtime_resources(summary)
        if resource_block:
            sections.append(resource_block)
        execution_lines = ["[EXECUTION STATE]"]
        for key, value in summary.items():
            if key in {
                "active_goal",
                "goal_status",
                "goal_token_budget",
                "work_plan",
                "current_step_id",
                "project_guidance",
                "project_memory_index",
                "project_fact_memory_hot",
                "project_fact_memory_index",
                "workflow_memory_hot",
                "workflow_memory_index",
                "turn_memory_summary",
                "pending_turn_memories",
                "environment",
                "skill_context",
                "evidence_records",
                "workspace_entries",
                "read_records",
                "change_records",
                "evidence_pack",
                "lessons",
                "engineering_facts",
                "runtime_resources",
            }:
                continue
            if value is not None:
                execution_lines.append(f"- {key}: {_compact_text(str(value), 240)}")
        sections.append(execution_lines)
        return sections


def estimate_static_payload_tokens(payload: Any) -> int:
    """Approximate billed prompt tokens for non-message request payloads.

    Codex's Rust client uses a simple UTF-8 byte based approximation for local
    budgeting. Match that spirit here instead of Python's character count,
    because Chinese text and JSON-heavy tool schemas otherwise get severely
    undercounted.
    """

    if payload is None:
        return 0
    try:
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except TypeError:
        text = str(payload)
    return _estimate_text_tokens(text)


def _estimate_tokens(messages: list[ChatMessage]) -> int:
    return max(1, sum(_estimate_message_tokens(message) for message in messages))


def estimate_messages_tokens(messages: list[ChatMessage]) -> int:
    """Estimate provider-visible chat message tokens.

    `ContextManager.build_context()` can now initialize a stable append-only
    context buffer, while the final model request may be loaded from that
    persisted buffer.  Callers that want to display or log the actual request
    size should estimate the final `ModelRequest.messages`, not only the freshly
    rebuilt context fragment.
    """

    return _estimate_tokens(messages)


def _estimate_message_tokens(message: ChatMessage) -> int:
    tokens = 6  # role/name/tool_call framing overhead in chat-style payloads.
    tokens += _estimate_text_tokens(message.role)
    if message.name:
        tokens += _estimate_text_tokens(message.name)
    if message.tool_call_id:
        tokens += _estimate_text_tokens(message.tool_call_id)
    tokens += _estimate_text_tokens(message.content)
    if message.role == "assistant" and "tool_calls" in message.metadata:
        tokens += estimate_static_payload_tokens(message.metadata.get("tool_calls"))
    if isinstance(message.metadata.get("image"), dict):
        tokens += IMAGE_TOKEN_ESTIMATE
    return tokens


def _stable_runtime_token_budget(token_budget: int) -> int:
    return _clamp_int(int(token_budget * 0.65), 800, min(3_200, max(800, token_budget - 600)))


def plan_context_budget(
    *,
    token_budget: int,
    compression_threshold: float,
    baseline_ratio: float = 0.28,
    runtime_summary_token_budget: int | None = None,
    runtime_section_token_budget: int | None = None,
) -> ContextBudgetPlan:
    total = max(1_000, token_budget)
    trigger = total
    post_ratio = min(max(compression_threshold, 0.20), 0.80)
    baseline = min(max(baseline_ratio, 0.15), 0.45)
    post_target = max(800, int(total * post_ratio))

    # The runtime prefix is the agent's working memory: protocol, goal/plan,
    # durable memories, evidence, workspace facts, and environment facts. When
    # the user raises the soft request budget, grow this area too; otherwise a
    # long project gets more recent chat but still loses the memory that helps
    # the model avoid rediscovery.
    runtime_cap = max(2_200, min(6_000, int(total * 0.30)))
    derived_runtime = _clamp_int(int(total * baseline), 800, runtime_cap)
    derived_runtime = min(derived_runtime, max(500, int(post_target * 0.65)))
    runtime_tokens = runtime_summary_token_budget or derived_runtime
    runtime_tokens = _clamp_int(runtime_tokens, 500, max(500, int(post_target * 0.75)))

    section_cap = max(900, min(1_800, int(runtime_tokens * 0.55)))
    derived_section = _clamp_int(int(runtime_tokens * 0.42), 300, section_cap)
    section_tokens = runtime_section_token_budget or derived_section
    section_tokens = _clamp_int(section_tokens, 200, max(200, runtime_tokens))

    history_after_compact = max(250, post_target - runtime_tokens)
    # The checkpoint is the semantic handoff for old history. It must scale with
    # the soft request budget; otherwise long code-reading turns compact into a
    # tiny note and the next model has to rediscover the same large files.
    checkpoint_cap = _clamp_int(int(total * 0.22), 1_400, 6_000)
    checkpoint_tokens = _clamp_int(int(history_after_compact * 0.50), 250, checkpoint_cap)
    checkpoint_chars = checkpoint_tokens * 4
    recent_cap = _clamp_int(int(total * 0.25), 2_500, 5_000)
    recent_tokens = _clamp_int(history_after_compact - checkpoint_tokens, 300, recent_cap)
    post_compact_tokens = runtime_tokens + checkpoint_tokens + recent_tokens
    reserve_tokens = max(0, trigger - post_compact_tokens)

    return ContextBudgetPlan(
        total_tokens=total,
        trigger_tokens=trigger,
        runtime_summary_tokens=runtime_tokens,
        runtime_section_tokens=section_tokens,
        checkpoint_target_chars=checkpoint_chars,
        recent_target_tokens=recent_tokens,
        reserve_tokens=reserve_tokens,
    )


def _clamp_int(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


def _estimate_text_tokens(text: str) -> int:
    return max(1, len(text.encode("utf-8")) // 4 + 1)


def _section_token_report(text: str) -> dict[str, int]:
    report: dict[str, int] = {}
    current_header = "<preamble>"
    current_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("[") and line.endswith("]"):
            if current_lines:
                report[current_header] = _estimate_text_tokens("\n".join(current_lines))
            current_header = line.strip("[]")
            current_lines = [line]
        else:
            current_lines.append(line)
    if current_lines:
        report[current_header] = _estimate_text_tokens("\n".join(current_lines))
    return report


def _fit_runtime_summary(
    sections: list[list[str]],
    *,
    token_budget: int,
    section_token_budget: int,
) -> str:
    section_budget = max(200, section_token_budget)
    fitted_sections = [
        _fit_section(section, token_budget=section_budget)
        for section in sections
        if section
    ]
    text = "\n\n".join("\n".join(section) for section in fitted_sections if section)
    if _estimate_text_tokens(text) <= token_budget:
        return text

    required_headers = {
        "RUNTIME PROTOCOL",
        "WORK MEMORY",
        "ACTIONABLE EVIDENCE TAIL",
        "EXECUTION STATE",
        # This is the durable file working set: it lets the model remember what
        # files/ranges were already inspected after old raw tool output is
        # compacted. Treat it as an agent "perception cache", not optional
        # decoration, otherwise long-file debugging degenerates into repeated
        # read/search loops.
        "WORKSPACE FACTS",
    }
    kept: list[list[str]] = []
    optional: list[list[str]] = []
    for section in fitted_sections:
        header = _section_header(section)
        if header in required_headers:
            kept.append(section)
        else:
            optional.append(section)
    compact_sections = [
        _fit_section(
            section,
            token_budget=_runtime_section_fit_budget(
                _section_header(section),
                default_budget=section_budget,
                total_budget=token_budget,
            ),
        )
        for section in kept
    ]
    for section in optional:
        candidate = [
            *compact_sections,
            _fit_section(section, token_budget=max(120, section_budget // 3)),
        ]
        candidate_text = "\n\n".join("\n".join(item) for item in candidate if item)
        if _estimate_text_tokens(candidate_text) <= token_budget:
            compact_sections = candidate
    text = "\n\n".join("\n".join(section) for section in compact_sections if section)
    if _estimate_text_tokens(text) <= token_budget:
        return text
    return _compact_text(text, max(1, token_budget) * 4)


def _fit_section(section: list[str], *, token_budget: int) -> list[str]:
    if not section:
        return []
    if _estimate_text_tokens("\n".join(section)) <= token_budget:
        return section
    if len(section) == 1:
        return [_compact_text(section[0], max(1, token_budget) * 4)]
    header, *body = section
    fitted = [header]
    omitted = 0
    for line in body:
        candidate = [*fitted, line]
        if _estimate_text_tokens("\n".join(candidate)) <= token_budget:
            fitted.append(line)
        else:
            omitted += 1
    if omitted:
        marker = f"- ... {omitted} lines omitted by runtime context budget"
        if _estimate_text_tokens("\n".join([*fitted, marker])) <= token_budget:
            fitted.append(marker)
    if len(fitted) == 1 and body:
        fitted.append(_compact_text(body[0], max(80, token_budget * 4 - len(header) - 32)))
    return fitted


def _runtime_section_fit_budget(
    header: str,
    *,
    default_budget: int,
    total_budget: int,
) -> int:
    if header != "WORKSPACE FACTS":
        return default_budget
    # Preserve enough file-map detail to avoid rereading unchanged large files.
    # The clamp keeps tiny contexts affordable while allowing larger user
    # budgets to carry a more useful WorkingSet.
    workspace_floor = _clamp_int(int(total_budget * 0.25), 600, 1_600)
    return max(default_budget, workspace_floor)


def _section_header(section: list[str]) -> str:
    if not section:
        return ""
    header = section[0].strip()
    if header.startswith("[") and header.endswith("]"):
        return header.strip("[]")
    return header


def _format_goal_state(summary: dict[str, Any]) -> list[str]:
    active_goal = summary.get("active_goal")
    goal_status = summary.get("goal_status")
    work_plan = summary.get("work_plan")
    current_step_id = summary.get("current_step_id")
    if not active_goal and not work_plan:
        return []
    lines = [
        "[WORK MEMORY]",
        f"- RootObjective: {active_goal or '<none>'}",
        f"- GoalStatus: {goal_status or 'none'}",
    ]
    if current_step_id:
        lines.append(f"- CurrentStepId: {current_step_id}")
    raw_plan_source_status = summary.get("work_plan_source_status")
    if isinstance(raw_plan_source_status, list) and raw_plan_source_status:
        lines.append("- WorkPlanSources:")
        for raw_status in raw_plan_source_status[:8]:
            if not isinstance(raw_status, dict):
                continue
            path = raw_status.get("path") or ""
            stale = bool(raw_status.get("stale"))
            missing = bool(raw_status.get("missing"))
            synced_at = raw_status.get("synced_at") or ""
            line = f"  - {path}: stale={stale}; missing={missing}; synced_at={synced_at}"
            reason = raw_status.get("reason")
            if reason:
                line += f"; reason={_compact_text(str(reason), 120)}"
            if stale:
                line += "; action=resync_with_sync_work_plan_from_document_before_updating_steps"
            lines.append(_compact_text(line, 260))
    if isinstance(work_plan, list) and work_plan:
        required_items = [
            raw_item
            for raw_item in work_plan
            if isinstance(raw_item, dict) and raw_item.get("required") is not False
        ]
        required_done = sum(1 for raw_item in required_items if raw_item.get("status") == "done")
        required_pending = sum(
            1
            for raw_item in required_items
            if raw_item.get("status") in {"pending", "in_progress"}
        )
        required_blocked = sum(1 for raw_item in required_items if raw_item.get("status") == "blocked")
        missing_evidence = [
            str(raw_item.get("id") or "step")
            for raw_item in required_items
            if raw_item.get("status") == "done"
            and not (raw_item.get("evidence") or raw_item.get("evidence_ids"))
        ]
        lines.append(
            "- WorkPlanSummary: "
            f"total={len(work_plan)}; required={len(required_items)}; "
            f"required_done={required_done}; required_pending={required_pending}; "
            f"required_blocked={required_blocked}; missing_evidence={len(missing_evidence)}"
        )
        if (
            goal_status == "active"
            and required_items
            and required_pending == 0
            and required_blocked == 0
            and not missing_evidence
        ):
            lines.append(
                "- WorkPlanClosureHint: all required WorkPlan steps are done with evidence; "
                "prefer update_goal(status='complete') over broad rediscovery unless a concrete "
                "missing acceptance boundary is identified."
            )
        elif missing_evidence:
            lines.append(
                "- WorkPlanEvidenceGap: done required steps lacking evidence_ids/evidence: "
                + _compact_text(", ".join(missing_evidence[:10]), 240)
            )
        lines.append("- WorkPlan:")
        for raw_item in work_plan[:12]:
            if not isinstance(raw_item, dict):
                continue
            item_id = raw_item.get("id") or "step"
            title = raw_item.get("title") or ""
            status = raw_item.get("status") or "pending"
            marker = " (current)" if item_id == current_step_id else ""
            line = f"  - {item_id}: [{status}] {title}{marker}"
            required = raw_item.get("required")
            if required is False:
                line += "; optional"
            acceptance = raw_item.get("acceptance")
            if acceptance:
                line += f"; acceptance={_compact_text(str(acceptance), 160)}"
            verification_hint = raw_item.get("verification_hint")
            if verification_hint:
                line += f"; verification_hint={_compact_text(str(verification_hint), 160)}"
            source_document = raw_item.get("source_document")
            if source_document:
                line += f"; source_document={_compact_text(str(source_document), 160)}"
            blocker = raw_item.get("blocker")
            if blocker:
                line += f"; blocker={_compact_text(str(blocker), 160)}"
            lines.append(line)
            evidence_ids = raw_item.get("evidence_ids")
            if isinstance(evidence_ids, list) and evidence_ids:
                compact_ids = ", ".join(str(item) for item in evidence_ids[-5:])
                lines.append(f"    evidence_ids: {compact_ids}")
            evidence = raw_item.get("evidence")
            if isinstance(evidence, list) and evidence:
                compact_evidence = "; ".join(_compact_text(str(item), 100) for item in evidence[-3:])
                lines.append(f"    evidence: {compact_evidence}")
        if len(work_plan) > 12:
            lines.append(f"  - ... {len(work_plan) - 12} more steps omitted")
    return lines


def _format_project_guidance(summary: dict[str, Any]) -> list[str]:
    raw_files = summary.get("project_guidance")
    if not isinstance(raw_files, list) or not raw_files:
        return []
    lines = ["[PROJECT GUIDANCE]"]
    for raw_file in raw_files[:4]:
        if not isinstance(raw_file, dict):
            continue
        path = raw_file.get("path") or "AGENTS.md"
        content = str(raw_file.get("content") or "").strip()
        if not content:
            continue
        truncated = " (truncated)" if raw_file.get("truncated") else ""
        lines.append(f"File: {path}{truncated}")
        lines.append(_compact_text(content, 6000))
    return lines


def _format_project_memory_index(summary: dict[str, Any]) -> list[str]:
    raw_documents = summary.get("project_memory_index")
    if not isinstance(raw_documents, list) or not raw_documents:
        return []
    lines = [
        "[PROJECT MEMORY INDEX]",
        "- These are available project documents. Read a document before relying on details not shown here.",
        "- For plan documents that should guide execution, call sync_work_plan_from_document(path).",
    ]
    for raw_document in raw_documents[:20]:
        if not isinstance(raw_document, dict):
            continue
        path = raw_document.get("path") or "document"
        title = raw_document.get("title") or ""
        headings = raw_document.get("headings")
        excerpt = raw_document.get("excerpt") or ""
        line = f"- {path}: {title}"
        if isinstance(headings, list) and headings:
            heading_text = "; ".join(_compact_text(str(item), 80) for item in headings[:5])
            line += f" | headings: {heading_text}"
        if excerpt:
            line += f" | excerpt: {_compact_text(str(excerpt), 220)}"
        lines.append(line)
    return lines


def _format_project_fact_memory(summary: dict[str, Any]) -> list[str]:
    raw_hot = summary.get("project_fact_memory_hot")
    raw_index = summary.get("project_fact_memory_index")
    hot = [item for item in raw_hot if isinstance(item, dict)] if isinstance(raw_hot, list) else []
    index = [item for item in raw_index if isinstance(item, dict)] if isinstance(raw_index, list) else []
    if not hot and not index:
        return []
    lines = [
        "[PROJECT FACT MEMORY]",
        "- Durable model-written project facts. Use these to avoid rediscovering stable environment/startup/test facts.",
        "- Search with search_project_memory and read full facts with read_project_memory when the index is insufficient.",
    ]
    if hot:
        lines.append("- Hot facts:")
        for item in hot[:5]:
            fact_id = item.get("id") or "memory"
            title = item.get("title") or ""
            content = item.get("content") or ""
            tags = item.get("tags")
            scope = item.get("scope") or "project"
            confidence = item.get("confidence") or "medium"
            line = (
                f"  - {fact_id}: {title}; scope={scope}; confidence={confidence}; "
                f"{_compact_text(str(content), 300)}"
            )
            if isinstance(tags, list) and tags:
                line += "; tags=" + ",".join(_compact_text(str(tag), 40) for tag in tags[:8])
            lines.append(_compact_text(line, 420))
    if index:
        lines.append("- Memory index:")
        hot_ids = {str(item.get("id") or "") for item in hot}
        shown = 0
        for item in index:
            fact_id = str(item.get("id") or "memory")
            if fact_id in hot_ids:
                continue
            title = item.get("title") or ""
            summary_text = item.get("summary") or ""
            tags = item.get("tags")
            confidence = item.get("confidence") or "medium"
            stale = item.get("stale")
            line = (
                f"  - {fact_id}: {title}; confidence={confidence}; stale={stale}; "
                f"summary={_compact_text(str(summary_text), 180)}"
            )
            if isinstance(tags, list) and tags:
                line += "; tags=" + ",".join(_compact_text(str(tag), 40) for tag in tags[:8])
            lines.append(_compact_text(line, 320))
            shown += 1
            if shown >= 20:
                break
    return lines


def _format_workflow_memory(summary: dict[str, Any]) -> list[str]:
    raw_hot = summary.get("workflow_memory_hot")
    raw_index = summary.get("workflow_memory_index")
    hot = [item for item in raw_hot if isinstance(item, dict)] if isinstance(raw_hot, list) else []
    index = [item for item in raw_index if isinstance(item, dict)] if isinstance(raw_index, list) else []
    if not hot and not index:
        return []
    lines = [
        "[WORKFLOW MEMORY]",
        "- Durable model-written project operating procedures. Use these to avoid rediscovering startup/test/build/smoke/integration flows.",
        "- Search with search_workflows and read full procedures with read_workflow when this compact view is insufficient.",
        "- Treat workflows as observations, not orders. Verify or update them when evidence changes.",
    ]
    if hot:
        lines.append("- Hot workflows:")
        for item in hot[:5]:
            lines.append(_format_workflow_line(item, include_notes=True))
    if index:
        lines.append("- Workflow index:")
        hot_ids = {str(item.get("id") or "") for item in hot}
        shown = 0
        for item in index:
            workflow_id = str(item.get("id") or "workflow")
            if workflow_id in hot_ids:
                continue
            lines.append(_format_workflow_line(item, include_notes=False))
            shown += 1
            if shown >= 20:
                break
    return lines


def _format_workflow_line(item: dict[str, Any], *, include_notes: bool) -> str:
    workflow_id = item.get("id") or "workflow"
    title = item.get("title") or ""
    purpose = item.get("purpose") or "workflow"
    cwd = item.get("cwd") or "."
    command = item.get("command") or ""
    ready_check = item.get("ready_check") or ""
    steps = item.get("steps")
    ports = item.get("ports")
    tags = item.get("tags")
    confidence = item.get("confidence") or "medium"
    stale = item.get("stale")
    success_count = item.get("success_count")
    failure_count = item.get("failure_count")
    line = (
        f"  - {workflow_id}: {title}; purpose={purpose}; cwd={cwd}; "
        f"confidence={confidence}; stale={stale}"
    )
    if command:
        line += f"; command={_compact_text(str(command), 220)}"
    if ready_check:
        line += f"; ready_check={_compact_text(str(ready_check), 120)}"
    if isinstance(steps, list) and steps:
        step_text = " | ".join(_compact_text(str(step), 90) for step in steps[:4])
        line += f"; steps={step_text}"
    if isinstance(ports, list) and ports:
        line += "; ports=" + ",".join(str(port) for port in ports[:6])
    if success_count is not None or failure_count is not None:
        line += f"; success={success_count or 0}; failure={failure_count or 0}"
    if isinstance(tags, list) and tags:
        line += "; tags=" + ",".join(_compact_text(str(tag), 40) for tag in tags[:8])
    if include_notes:
        notes = item.get("notes") or item.get("summary") or ""
        if notes:
            line += f"; notes={_compact_text(str(notes), 180)}"
    return _compact_text(line, 520)


def _format_skill_context(summary: dict[str, Any]) -> list[str]:
    raw = summary.get("skill_context")
    if not isinstance(raw, dict):
        return []
    selection = raw.get("selection")
    if not isinstance(selection, dict):
        selection = {}
    active_by_domain = selection.get("active_by_domain")
    references_by_domain = selection.get("references_by_domain")
    warnings = selection.get("warnings")
    skills = raw.get("skills")
    if not isinstance(skills, list):
        skills = []
    lines = [
        "[SKILL CONTEXT]",
        "- Skill rule: Skills are workflow/capability guidance, not running process instances. Tool calls, sessions, browser runs, and background commands are instances.",
        "- Skill rule: Agent OS hard policy stays active regardless of skill selection: path safety, permissions, command timeouts, background process management, tool schema validation, write-after-verify gate, event log, context buffer, and memory store.",
        "- Skill rule: Use manifest metadata for domain/role/priority conflict handling. Use load_skill(name, reference) only when the current task needs full skill guidance.",
        f"- CodeGuidanceMode: {raw.get('code_guidance_mode') or 'legacy'}; ActiveCodeSkill: {raw.get('active_code_skill') or 'legacy-builtin-code'}",
    ]
    if isinstance(active_by_domain, dict) and active_by_domain:
        active_text = ", ".join(
            f"{key}={value}" for key, value in sorted(active_by_domain.items())
        )
        lines.append(f"- ActiveByDomain: {_compact_text(active_text, 500)}")
    if isinstance(references_by_domain, dict) and references_by_domain:
        parts = []
        for domain, references in sorted(references_by_domain.items()):
            if isinstance(references, list):
                names = ", ".join(str(item) for item in references[:8])
            else:
                names = str(references)
            if names:
                parts.append(f"{domain}={names}")
        if parts:
            lines.append(f"- ReferenceSkills: {_compact_text('; '.join(parts), 700)}")
    if isinstance(warnings, list) and warnings:
        for warning in warnings[:5]:
            lines.append(f"- SelectionWarning: {_compact_text(str(warning), 220)}")
    if skills:
        lines.append("- AvailableSkills:")
        for raw_skill in skills[:10]:
            if not isinstance(raw_skill, dict):
                continue
            name = raw_skill.get("name") or "skill"
            domain = raw_skill.get("domain") or "general"
            role = raw_skill.get("role") or "reference"
            source = raw_skill.get("source") or "external"
            priority = raw_skill.get("priority")
            description = raw_skill.get("description") or ""
            capabilities = raw_skill.get("capabilities")
            references = raw_skill.get("references")
            caps = ""
            if isinstance(capabilities, list) and capabilities:
                caps = "; caps=" + ",".join(_compact_text(str(item), 40) for item in capabilities[:8])
            refs = ""
            if isinstance(references, list) and references:
                refs = "; refs=" + ",".join(_compact_text(str(item), 70) for item in references[:8])
            lines.append(
                _compact_text(
                    f"  - {name}: domain={domain}; role={role}; source={source}; "
                    f"priority={priority}; {description}{caps}{refs}",
                    620,
                )
            )
    return lines


def _legacy_code_guidance_enabled(summary: dict[str, Any]) -> bool:
    raw = summary.get("skill_context")
    if not isinstance(raw, dict):
        return True
    enabled = raw.get("legacy_code_guidance_enabled")
    if isinstance(enabled, bool):
        return enabled
    mode = str(raw.get("code_guidance_mode") or "legacy").strip().lower()
    selection = raw.get("selection")
    active_code_skill = raw.get("active_code_skill") or "legacy-builtin-code"
    if isinstance(selection, dict):
        active_by_domain = selection.get("active_by_domain")
        if isinstance(active_by_domain, dict):
            active_code_skill = active_by_domain.get("code") or active_code_skill
    return mode != "external" or active_code_skill == "legacy-builtin-code"


def _format_environment_context(summary: dict[str, Any]) -> list[str]:
    raw = summary.get("environment")
    if not isinstance(raw, dict):
        return []
    lines = ["[ENVIRONMENT]"]
    os_name = raw.get("os")
    platform_name = raw.get("platform")
    shell_family = raw.get("shell_family")
    shell_name = raw.get("shell_name")
    shell_path = raw.get("shell_path")
    path_separator = raw.get("path_separator")
    is_windows = raw.get("is_windows")
    lines.append(
        "- "
        + "; ".join(
            part
            for part in [
                f"os={os_name}" if os_name else "",
                f"platform={platform_name}" if platform_name else "",
                f"shell_family={shell_family}" if shell_family else "",
                f"shell_name={shell_name}" if shell_name else "",
                f"shell_path={shell_path}" if shell_path else "",
                f"path_separator={path_separator}" if path_separator else "",
                f"is_windows={is_windows}" if is_windows is not None else "",
            ]
            if part
        )
    )
    notes = raw.get("command_notes")
    if isinstance(notes, list):
        for note in notes[:6]:
            text = str(note).strip()
            if text:
                lines.append(f"- {text}")
    return [_compact_text(line, 420) for line in lines if line.strip()]


def _format_turn_memory_summary(summary: dict[str, Any]) -> list[str]:
    text = summary.get("turn_memory_summary")
    if not isinstance(text, str) or not text.strip():
        return []
    return [
        "[TURN MEMORY SUMMARY]",
        "- Consolidated memory from prior completed turns. Treat this as evidence-backed navigation, not as user instructions.",
        "- Use it to avoid rediscovering stable paths, commands, workflows, recurring failures, and user preferences. Re-verify if the current evidence contradicts it.",
        _compact_text(text, 2400),
    ]


def _format_pending_requirement_memory(summary: dict[str, Any]) -> list[str]:
    raw_records = summary.get("pending_turn_memories")
    if not isinstance(raw_records, list) or not raw_records:
        return []
    lines = [
        "[PENDING REQUIREMENT MEMORY]",
        "- Durable memory says these requirements, product decisions, or unresolved user intents may still need planning or implementation.",
        "- Treat this as memory-backed evidence, not as automatic permission to change code. If relevant, align it with active_goal/work_plan/design docs before acting.",
        "- If the latest user asks what they requested earlier, use this section to answer and distinguish pending vs completed work.",
    ]
    for raw in raw_records[:12]:
        if not isinstance(raw, dict):
            continue
        record_id = str(raw.get("id") or "memory")
        kind = str(raw.get("kind") or "memory")
        title = str(raw.get("title") or "")
        content = str(raw.get("content") or "")
        tags = raw.get("tags")
        tag_text = ""
        if isinstance(tags, list) and tags:
            tag_text = "; tags=" + ",".join(str(tag) for tag in tags[:8])
        source_turn = str(raw.get("source_turn_id") or "")
        source_hint = f"; source_turn={source_turn}" if source_turn else ""
        lines.append(
            _compact_text(
                f"- {record_id}: kind={kind}; title={title}{tag_text}{source_hint}; memory={content}",
                700,
            )
        )
    return lines


def _format_evidence_store(summary: dict[str, Any]) -> list[str]:
    raw_records = summary.get("evidence_records")
    if not isinstance(raw_records, list) or not raw_records:
        return []
    lines = [
        "[EVIDENCE TAIL]",
        "- Short, volatile tail only. Full evidence events are appended to history as EVIDENCE EVENT messages when created.",
        "- Use these recent evidence_ids when marking WorkPlan steps done; if older evidence is needed, call get_work_plan/detail tools or inspect history instead of relying on this tail.",
        "- HTTP observations are diagnostic evidence. Do not treat them as acceptance evidence unless the current WorkPlan step explicitly links the evidence_id and its acceptance item is met.",
    ]
    for raw_record in raw_records[-6:]:
        if not isinstance(raw_record, dict):
            continue
        record_id = raw_record.get("id") or "evidence"
        kind = raw_record.get("kind") or "evidence"
        source_tool = raw_record.get("source_tool") or "tool"
        summary_text = raw_record.get("summary") or ""
        line = (
            f"- {record_id}: {kind} via {source_tool}; "
            f"{_compact_text(str(summary_text), 220)}"
        )
        command = raw_record.get("command")
        url = raw_record.get("url")
        if command:
            line += f"; command={_compact_text(str(command), 160)}"
        if url:
            line += f"; url={_compact_text(str(url), 160)}"
        lines.append(line)
    return lines


def _format_workspace_context(summary: dict[str, Any]) -> list[str]:
    raw_entries = summary.get("workspace_entries")
    raw_reads = summary.get("read_records")
    raw_changes = summary.get("change_records")
    entries = [item for item in raw_entries if isinstance(item, dict)] if isinstance(raw_entries, list) else []
    reads = [item for item in raw_reads if isinstance(item, dict)] if isinstance(raw_reads, list) else []
    changes = [item for item in raw_changes if isinstance(item, dict)] if isinstance(raw_changes, list) else []
    if not entries and not reads and not changes:
        return []
    lines = [
        "[WORKSPACE FACTS]",
        "- Observed file facts. Prioritize WorkingSet entries before rediscovering files.",
    ]
    if reads:
        lines.append("- WorkingSet recently read files:")
        for item in _prioritize_read_records(reads)[:30]:
            path = item.get("path") or ""
            source_tool = item.get("source_tool") or ""
            size = item.get("size")
            excerpt = item.get("excerpt") or ""
            content_hash = str(item.get("content_hash") or item.get("hash") or "")
            read_count = item.get("read_count")
            truncated = bool(item.get("truncated"))
            changed_after_read = _has_change_after_read(path, item, changes)
            coverage = _read_coverage_text(item)
            line = f"  - {path} via {source_tool}"
            if size is not None:
                line += f"; size={size}"
            if content_hash:
                line += f"; hash={content_hash[:12]}"
            if isinstance(read_count, int) and read_count > 1:
                line += f"; reads={read_count}"
            if coverage:
                line += f"; {coverage}"
            if truncated:
                line += "; truncated=true"
            if changed_after_read:
                line += "; changed-after-read=true"
            if excerpt:
                line += f"; excerpt={_compact_text(str(excerpt), 180)}"
            lines.append(_compact_text(line, 520))
    lines.extend(
        [
            "- Guidance: this is not a complete project tree; if a needed file is absent, list/search/read instead of guessing.",
            "- Guidance: avoid re-reading unchanged WorkingSet files unless exact line content is needed for an edit or verification.",
            "- Guidance: read coverage is factual only; it says which line ranges were observed before compaction, not that every line is in active attention.",
        ]
    )
    if entries:
        lines.append("- WorkspaceMap recent entries:")
        for item in entries[-40:]:
            path = item.get("path") or ""
            kind = item.get("kind") or ""
            source_tool = item.get("source_tool") or ""
            size = item.get("size")
            line = f"  - {path} ({kind}; source={source_tool}"
            if size is not None:
                line += f"; size={size}"
            line += ")"
            lines.append(_compact_text(line, 260))
    if changes:
        lines.append("- ChangeSet recent changes:")
        for item in changes[-20:]:
            path = item.get("path") or ""
            source_tool = item.get("source_tool") or ""
            action = item.get("action") or "changed"
            item_summary = item.get("summary") or ""
            line = f"  - {path}: {action} via {source_tool}"
            if item_summary:
                line += f"; {_compact_text(str(item_summary), 180)}"
            lines.append(_compact_text(line, 320))
    return lines


def _prioritize_read_records(reads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def rank(item: dict[str, Any]) -> tuple[int, str]:
        read_count = item.get("read_count")
        count = read_count if isinstance(read_count, int) else 1
        return (-count, str(item.get("path") or ""))

    return sorted(reads, key=rank)


def _read_coverage_text(item: dict[str, Any]) -> str:
    raw_ranges = item.get("line_ranges")
    line_count = item.get("line_count")
    if not isinstance(raw_ranges, list) or not raw_ranges:
        return ""
    ranges: list[tuple[int, int]] = []
    for raw in raw_ranges:
        if (
            isinstance(raw, list)
            and len(raw) == 2
            and isinstance(raw[0], int)
            and isinstance(raw[1], int)
        ):
            ranges.append((raw[0], raw[1]))
    if not ranges:
        return ""
    total_observed = sum(max(0, end - start + 1) for start, end in ranges)
    range_text = ",".join(
        f"{start}-{end}" if start != end else str(start)
        for start, end in ranges[-8:]
    )
    parts = [f"line_coverage={range_text}"]
    if isinstance(line_count, int) and line_count > 0:
        percent = min(100, int(round(total_observed * 100 / line_count)))
        parts.append(f"line_count={line_count}")
        parts.append(f"coverage_pct={percent}")
    full_read_count = item.get("full_read_count")
    if isinstance(full_read_count, int) and full_read_count > 0:
        parts.append(f"full_file_reads={full_read_count}")
    last_start = item.get("last_start_line")
    last_end = item.get("last_end_line")
    if isinstance(last_start, int) and isinstance(last_end, int):
        parts.append(f"last_read={last_start}-{last_end}")
    return "; ".join(parts)


def _has_change_after_read(path: object, read_record: dict[str, Any], changes: list[dict[str, Any]]) -> bool:
    if not isinstance(path, str) or not path:
        return False
    read_at = read_record.get("observed_at")
    if not isinstance(read_at, str) or not read_at:
        return False
    for change in changes[-40:]:
        if change.get("path") != path:
            continue
        changed_at = change.get("observed_at")
        if isinstance(changed_at, str) and changed_at > read_at:
            return True
    return False


def _format_evidence_pack(summary: dict[str, Any]) -> list[str]:
    raw_pack = summary.get("evidence_pack")
    if not isinstance(raw_pack, dict):
        return []
    lines = [
        "[ACTIONABLE EVIDENCE TAIL]",
        "- Small current-action summary. Do not expect this tail to contain all prior facts; use append-only tool results and EVIDENCE EVENT history for provenance.",
        "- Use these recent observations before asking the user or guessing.",
    ]
    for key in (
        "latest_tool_facts",
        "latest_failures",
        "latest_changes",
        "active_resources",
    ):
        raw_items = raw_pack.get(key)
        if not isinstance(raw_items, list) or not raw_items:
            continue
        lines.append(f"- {key}:")
        for item in raw_items[-3:]:
            lines.append(f"  - {_compact_text(str(item), 260)}")
    return lines


def _format_lesson_memory(summary: dict[str, Any]) -> list[str]:
    raw_lessons = summary.get("lessons")
    if not isinstance(raw_lessons, list) or not raw_lessons:
        return []
    lines = [
        "[LESSON MEMORY]",
        "- Reusable lessons from prior failures or corrections. Apply them only when relevant to the current evidence.",
    ]
    for raw_lesson in raw_lessons[-12:]:
        if not isinstance(raw_lesson, dict):
            continue
        lesson_id = raw_lesson.get("id") or "lesson"
        scope = raw_lesson.get("scope") or "general"
        summary_text = raw_lesson.get("summary") or ""
        line = f"- {lesson_id}: scope={scope}; {_compact_text(str(summary_text), 240)}"
        trigger = raw_lesson.get("trigger")
        evidence = raw_lesson.get("evidence")
        if trigger:
            line += f"; trigger={_compact_text(str(trigger), 120)}"
        if evidence:
            line += f"; evidence={_compact_text(str(evidence), 160)}"
        lines.append(line)
    return lines


def _format_engineering_facts(summary: dict[str, Any]) -> list[str]:
    raw_facts = summary.get("engineering_facts")
    if not isinstance(raw_facts, list) or not raw_facts:
        return []
    lines = [
        "[ENGINEERING FACTS]",
        "- Facts are observed software-engineering signals, not business decisions.",
        "- Prefer current project configuration and high-confidence facts over stale historical runtime resources.",
    ]
    for raw_fact in _prioritize_engineering_facts(raw_facts)[:30]:
        if not isinstance(raw_fact, dict):
            continue
        fact_id = raw_fact.get("id") or "fact"
        fact_type = raw_fact.get("type") or "fact"
        source = raw_fact.get("source") or ""
        summary_text = raw_fact.get("summary") or ""
        confidence = raw_fact.get("confidence") or "medium"
        stale = raw_fact.get("stale")
        line = (
            f"- {fact_id}: {fact_type}; confidence={confidence}; stale={stale}; "
            f"source={_compact_text(str(source), 100)}; {_compact_text(str(summary_text), 240)}"
        )
        data = raw_fact.get("data")
        if isinstance(data, dict):
            details = []
            for key in (
                "command",
                "cwd",
                "port",
                "expected_long_running",
                "target",
                "parent",
                "siblings",
                "status",
                "url",
                "reason",
            ):
                value = data.get(key)
                if value not in (None, "", []):
                    details.append(f"{key}={_compact_text(str(value), 160)}")
            if details:
                line += "; " + "; ".join(details[:6])
        lines.append(line)
    return lines


def _prioritize_engineering_facts(raw_facts: list[object]) -> list[object]:
    type_rank = {
        "config_ref": 0,
        "entrypoint": 1,
        "command": 2,
        "missing_path": 3,
        "failure": 4,
        "runtime_resource": 5,
        "evidence": 6,
    }
    confidence_rank = {"high": 0, "medium": 1, "low": 2}

    def rank(item: object) -> tuple[int, int, int]:
        if not isinstance(item, dict):
            return (99, 99, 1)
        fact_type = str(item.get("type") or "")
        confidence = str(item.get("confidence") or "medium")
        stale = 1 if item.get("stale") else 0
        return (
            stale,
            type_rank.get(fact_type, 50),
            confidence_rank.get(confidence, 3),
        )

    return sorted(raw_facts, key=rank)


def _format_runtime_resources(summary: dict[str, Any]) -> list[str]:
    raw_resources = summary.get("runtime_resources")
    if not isinstance(raw_resources, list) or not raw_resources:
        return []
    lines = [
        "[RUNTIME RESOURCE MAP]",
        "- These are observed runtime resources and executions, not business conclusions.",
        "- Use commands, ports, HTTP observations, logs, and project config together before forming a hypothesis.",
    ]
    for raw_resource in raw_resources[-20:]:
        if not isinstance(raw_resource, dict):
            continue
        resource_id = raw_resource.get("id") or "resource"
        kind = raw_resource.get("kind") or "resource"
        command = raw_resource.get("command") or ""
        cwd = raw_resource.get("cwd") or ""
        url = raw_resource.get("url") or ""
        method = raw_resource.get("method") or ""
        port = raw_resource.get("port")
        pid = raw_resource.get("pid")
        ready = raw_resource.get("ready")
        exit_code = raw_resource.get("exit_code")
        status_code = raw_resource.get("status_code")
        status = raw_resource.get("status") or ""
        summary_text = raw_resource.get("summary") or ""
        log_path = raw_resource.get("log_path") or ""
        line = f"- {resource_id}: {kind}"
        if summary_text:
            line += f"; summary={_compact_text(str(summary_text), 160)}"
        if command:
            line += f"; command={_compact_text(str(command), 120)}"
        if cwd:
            line += f"; cwd={_compact_text(str(cwd), 80)}"
        if url:
            line += f"; url={_compact_text(str(url), 160)}"
        if method:
            line += f"; method={method}"
        if port:
            line += f"; port={port}"
        if pid:
            line += f"; pid={pid}"
        if ready is not None:
            line += f"; ready={ready}"
        if exit_code is not None:
            line += f"; exit_code={exit_code}"
        if status_code is not None:
            line += f"; status_code={status_code}"
        if status:
            line += f"; status={status}"
        if log_path:
            line += f"; log={_compact_text(str(log_path), 120)}"
        lines.append(line)
    return lines


def _is_runtime_summary_message(content: str) -> bool:
    return content.startswith("Runtime summary:") or content.startswith(
        (
            "[RUNTIME PROTOCOL]",
            "[PROJECT GUIDANCE]",
            "[PROJECT MEMORY INDEX]",
            "[PROJECT FACT MEMORY]",
            "[WORKFLOW MEMORY]",
            "[WORK MEMORY]",
            "[EVIDENCE TAIL]",
            "[WORKSPACE FACTS]",
            "[ACTIONABLE EVIDENCE TAIL]",
            "[ENGINEERING FACTS]",
            "[RUNTIME RESOURCE MAP]",
            "[EXECUTION STATE]",
        )
    )


def _drop_leading_tool_messages(messages: list[ChatMessage]) -> list[ChatMessage]:
    trimmed = list(messages)
    while trimmed and trimmed[0].role == "tool":
        trimmed.pop(0)
    return trimmed


def _clear_old_tool_results(
    messages: list[ChatMessage],
    *,
    keep_recent: int,
    summary_chars: int,
) -> list[ChatMessage]:
    """Clear old raw tool payloads from active context while preserving sequence.

    The audit log still contains the compacted original tool result in
    ChatHistory. This function only transforms the active model context. It
    mirrors the same idea as tool-result clearing in mature agent runtimes:
    once a tool result has already been consumed by later model turns, keep the
    tool_call_id/name and operational facts, but remove bulky raw stdout/html or
    file contents that make every later call more expensive.
    """

    tool_indexes = [index for index, message in enumerate(messages) if message.role == "tool"]
    if not tool_indexes:
        return messages
    keep_recent = max(0, keep_recent)
    keep_indexes = set(tool_indexes[-keep_recent:]) if keep_recent else set()
    summary_chars = max(160, summary_chars)
    cleared: list[ChatMessage] = []
    for index, message in enumerate(messages):
        if message.role != "tool" or index in keep_indexes:
            cleared.append(message)
            continue
        if message.metadata.get("tool_result_cleared") is True:
            cleared.append(message)
            continue
        content = _summarize_tool_result_for_active_context(
            message,
            limit=summary_chars,
        )
        metadata = dict(message.metadata)
        metadata["tool_result_cleared"] = True
        metadata["original_tool_result_chars"] = len(message.content)
        cleared.append(
            ChatMessage(
                role=message.role,
                content=content,
                name=message.name,
                tool_call_id=message.tool_call_id,
                metadata=metadata,
            )
        )
    return cleared


def _summarize_tool_result_for_active_context(message: ChatMessage, *, limit: int) -> str:
    changed_files: list[str] = []
    commands: list[str] = []
    failures: list[str] = []
    _collect_tool_summary(message.content, changed_files, commands, failures)
    lines = [
        "[tool result cleared after model consumption]",
        f"- tool: {message.name or 'unknown'}",
        f"- tool_call_id: {message.tool_call_id or 'unknown'}",
        f"- original_chars: {len(message.content)}",
    ]
    if commands:
        lines.append("- commands:")
        for command in _dedupe_keep_order(commands)[-4:]:
            lines.append(f"  - {_compact_text(command, 180)}")
    if changed_files:
        lines.append("- changed_files: " + _compact_text(", ".join(_dedupe_keep_order(changed_files)[-6:]), 220))
    if failures:
        lines.append("- failures:")
        for failure in _dedupe_keep_order(failures)[-5:]:
            lines.append(f"  - {_compact_text(failure, 220)}")
    excerpt = ""
    if not (commands or changed_files or failures):
        excerpt = _compact_text(message.content, max(120, limit // 2))
    if excerpt:
        lines.append("- excerpt: " + excerpt)
    return _compact_lines(lines, limit)


def _count_cleared_tool_results(messages: list[ChatMessage]) -> int:
    return sum(
        1
        for message in messages
        if message.role == "tool" and message.metadata.get("tool_result_cleared") is True
    )


def _summarize_omitted_messages(messages: list[ChatMessage], *, limit: int = 5000) -> str:
    if not messages:
        return "Context checkpoint handoff: no earlier messages omitted."
    role_counts: dict[str, int] = {}
    changed_files: list[str] = []
    commands: list[str] = []
    failures: list[str] = []
    evidence: list[str] = []
    tool_facts: list[str] = []
    recent_user_messages: list[str] = []
    assistant_summaries: list[str] = []
    for message in messages:
        role_counts[message.role] = role_counts.get(message.role, 0) + 1
        if message.role == "tool":
            _collect_tool_summary(message.content, changed_files, commands, failures)
            continue
        content = _compact_text(message.content, 600)
        if message.role == "user" and content:
            recent_user_messages.append(content)
        elif message.role == "assistant" and content:
            assistant_summaries.append(content)
        elif message.role == "runtime":
            lower = content.lower()
            if "tool result facts" in lower:
                tool_facts.append(content)
            if "evidence" in lower:
                evidence.append(content)
            if "failed" in lower or "blocked" in lower or "failure" in lower:
                failures.append(content)
    lines = [
        "Context checkpoint handoff:",
        f"- omitted_messages: {len(messages)}",
        "- role_counts: "
        + ", ".join(f"{role}={count}" for role, count in sorted(role_counts.items())),
    ]
    if commands:
        lines.append("- commands_run:")
        for item in _dedupe_keep_order(commands)[-8:]:
            lines.append(f"  - {_compact_text(item, 180)}")
    if changed_files:
        changed_text = ", ".join(_dedupe_keep_order(changed_files)[-6:])
        lines.append("- files_changed: " + _compact_text(changed_text, 140))
    if failures:
        deduped_failures = _dedupe_keep_order(failures)
        selected_failures = deduped_failures[-5:]
        traceback_failure = next(
            (item for item in deduped_failures if "traceback" in item.lower()),
            None,
        )
        if traceback_failure and traceback_failure not in selected_failures:
            selected_failures = [traceback_failure, *selected_failures]
        failure_text = "; ".join(selected_failures)
        lines.append("- important_failures: " + _compact_text(failure_text, 180))
    if recent_user_messages:
        lines.append("- recent_user_requests:")
        for item in recent_user_messages[-4:]:
            lines.append(f"  - {_compact_text(item, 220)}")
    if assistant_summaries:
        lines.append("- earlier_assistant_progress:")
        for item in assistant_summaries[-4:]:
            lines.append(f"  - {_compact_text(item, 220)}")
    if tool_facts:
        lines.append("- recent_tool_result_facts:")
        for item in tool_facts[-4:]:
            lines.append(f"  - {_compact_text(item, 260)}")
    if evidence:
        lines.append("- evidence_context:")
        for item in evidence[-4:]:
            lines.append(f"  - {_compact_text(item, 220)}")
    lines.append(
        "- remaining_next_step_rule: continue from this checkpoint using current workspace state; inspect files or rerun commands if exact old details are needed."
    )
    return _compact_lines(lines, limit)


def _summarize_with_optional_callback(
    messages: list[ChatMessage],
    *,
    limit: int,
    compaction_callback: CompactionCallback | None,
) -> str:
    if compaction_callback is not None:
        try:
            summary = compaction_callback(messages, limit)
        except Exception:
            summary = None
        if summary:
            return _compact_lines(summary.splitlines(), limit)
    return _summarize_omitted_messages(messages, limit=limit)


def _collect_tool_summary(
    content: str,
    changed_files: list[str],
    commands: list[str],
    failures: list[str],
) -> None:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith(("wrote ", "edited ", "deleted ")):
            parts = stripped.split(maxsplit=1)
            if len(parts) == 2:
                changed_files.append(parts[1])
        elif stripped.startswith("command:"):
            commands.append(stripped.removeprefix("command:").strip())
        elif (
            "traceback" in stripped.lower()
            or "error:" in stripped.lower()
            or "failed" in stripped.lower()
            or "semantic_failure:" in stripped.lower()
            or "no tests ran" in stripped.lower()
        ):
            failures.append(stripped)


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def _compact_text(text: str, limit: int) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def _compact_lines(lines: list[str], limit: int) -> str:
    text = "\n".join(lines)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _limit_context_images(messages: list[ChatMessage], *, max_images: int) -> list[ChatMessage]:
    if max_images < 0:
        max_images = 0
    image_indexes = [
        index
        for index, message in enumerate(messages)
        if isinstance(message.metadata.get("image"), dict)
    ]
    keep_indexes = set(image_indexes[-max_images:]) if max_images else set()
    limited: list[ChatMessage] = []
    for index, message in enumerate(messages):
        if index in keep_indexes or not isinstance(message.metadata.get("image"), dict):
            limited.append(message)
            continue
        image = message.metadata.get("image") or {}
        metadata = dict(message.metadata)
        metadata.pop("image", None)
        path = image.get("path") or "image"
        limited.append(
            ChatMessage(
                role=message.role,
                content=f"{message.content}\n[older image omitted from model context: {path}]",
                name=message.name,
                tool_call_id=message.tool_call_id,
                metadata=metadata,
            )
        )
    return limited
