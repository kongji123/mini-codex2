from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Literal

from minicodex2.agent_os.state import EvidenceRecord, SessionState, WorkPlanItem
from minicodex2.tools.results import ToolResult


@dataclass(slots=True)
class GoalTurnState:
    requested: bool
    objective: str | None = None
    no_tool_retry_used: bool = False
    completion_review_retries: int = 0
    verified_steps: int = 0
    max_steps: int = 4


@dataclass(slots=True)
class GoalControlDecision:
    should_continue: bool
    expected_action: str
    runtime_message: str


class GoalController:
    def __init__(self, state: SessionState, *, max_steps_per_turn: int = 4) -> None:
        self.state = state
        self.max_steps_per_turn = max_steps_per_turn

    def start_turn(self, *, engaged: bool = False) -> GoalTurnState:
        requested = engaged and self.state.goal_status == "active"
        return GoalTurnState(
            requested=requested,
            objective=self.state.active_goal,
            max_steps=self.max_steps_per_turn,
        )

    def get_goal(self) -> ToolResult:
        return ToolResult(ok=True, content=self._goal_json())

    def create_goal(self, objective: str, token_budget: int | None = None) -> ToolResult:
        normalized = " ".join(objective.strip().split())
        if not normalized:
            return ToolResult(
                ok=False,
                content="objective is required",
                blocked=True,
                block_reason="objective is required",
            )
        if token_budget is not None and token_budget <= 0:
            return ToolResult(
                ok=False,
                content="token_budget must be positive when supplied",
                blocked=True,
                block_reason="token_budget must be positive when supplied",
            )
        if self.state.goal_status == "active" and self.state.active_goal:
            used_ids = {item.id for item in self.state.work_plan}
            extension_id = _unique_work_item_id(
                f"goal-extension-{len(self.state.work_plan) + 1}",
                used_ids,
            )
            extension = WorkPlanItem(
                id=extension_id,
                title=f"New requirement: {normalized[:240]}",
                status="pending",
                acceptance="Implement and verify this newly requested requirement with concrete evidence.",
                verification_hint=(
                    "Read any relevant design document or project files, merge concrete substeps with "
                    "set_work_plan/sync_work_plan_from_document if needed, then verify the changed behavior."
                ),
                required=True,
            )
            self.state.work_plan.append(extension)
            self.state.set_work_plan(self.state.work_plan)
            return ToolResult(
                ok=True,
                content=(
                    "active goal already exists, so the requested objective was appended as a new "
                    "pending WorkPlan item instead of replacing the current goal. Continue by reading "
                    "or syncing the relevant design document, decomposing this new requirement into "
                    "concrete steps if needed, and then implementing/verifying it.\n\n"
                    f"appended_step_id={extension.id}\n\n{self._goal_json()}"
                ),
                metadata={
                    "goal_event": "extended",
                    "goal": self.state.goal_snapshot(),
                    "appended_step": extension.to_dict(),
                },
            )
        if self.state.active_goal and self.state.goal_status in {"complete", "blocked"}:
            self.state.archive_current_goal("replaced_by_new_goal")
        self.state.active_goal = normalized[:500]
        self.state.goal_status = "active"
        self.state.goal_token_budget = token_budget
        self.state.work_plan = []
        self.state.current_step_id = None
        return ToolResult(
            ok=True,
            content=self._goal_json(),
            metadata={"goal_event": "created", "goal": self.state.goal_snapshot()},
        )

    def set_work_plan(
        self,
        steps: list[dict[str, object]],
        replace_document_plan: bool = False,
    ) -> ToolResult:
        if not self.state.active_goal:
            return ToolResult(
                ok=False,
                content="cannot set work plan because this session has no active goal",
                blocked=True,
                block_reason="no active goal",
            )
        if not isinstance(steps, list) or not steps:
            return ToolResult(
                ok=False,
                content="steps must be a non-empty list",
                blocked=True,
                block_reason="work plan is empty",
            )
        has_document_plan = any(item.source_document for item in self.state.work_plan)
        incoming_has_document_source = any(
            isinstance(step, dict) and bool(step.get("source_document"))
            for step in steps
        )
        if has_document_plan and not incoming_has_document_source and not replace_document_plan:
            return ToolResult(
                ok=False,
                content=(
                    "cannot replace a document-sourced WorkPlan with unsourced steps. "
                    "The existing plan came from a planning/design document, so transient diagnostic "
                    "or verification substeps must not be promoted to durable project plan items. "
                    "Use update_work_step/set_current_step for existing steps, "
                    "sync_work_plan_from_document for document changes, or pass "
                    "replace_document_plan=true only when the user explicitly wants to replace the "
                    "document plan."
                ),
                blocked=True,
                block_reason="document-sourced work plan replacement requires explicit override",
                metadata={"goal_event": "plan_replace_blocked"},
            )
        items: list[WorkPlanItem] = []
        used_ids: set[str] = set()
        for index, raw_step in enumerate(steps, start=1):
            if not isinstance(raw_step, dict):
                return ToolResult(
                    ok=False,
                    content=f"step {index} must be an object",
                    blocked=True,
                    block_reason="invalid work plan step",
                )
            item = self._build_work_item(raw_step, index=index, used_ids=used_ids)
            if item is None:
                return ToolResult(
                    ok=False,
                    content=f"step {index} requires a title",
                    blocked=True,
                    block_reason="invalid work plan step",
                )
            items.append(item)
        self.state.set_work_plan(items)
        return ToolResult(
            ok=True,
            content=self._goal_json(),
            metadata={"goal_event": "plan_updated", "goal": self.state.goal_snapshot()},
        )

    def merge_work_plan(self, steps: list[dict[str, object]], *, source_document: str = "") -> ToolResult:
        if not self.state.active_goal:
            return ToolResult(
                ok=False,
                content="cannot merge work plan because this session has no active goal",
                blocked=True,
                block_reason="no active goal",
            )
        if not isinstance(steps, list) or not steps:
            return ToolResult(
                ok=False,
                content="steps must be a non-empty list",
                blocked=True,
                block_reason="work plan is empty",
            )
        used_ids = {item.id for item in self.state.work_plan}
        merged_count = 0
        added_count = 0
        document_done_count = 0
        reconciled_count = 0
        duplicate_guidance_count = 0
        for index, raw_step in enumerate(steps, start=1):
            if not isinstance(raw_step, dict):
                return ToolResult(
                    ok=False,
                    content=f"step {index} must be an object",
                    blocked=True,
                    block_reason="invalid work plan step",
                )
            incoming = self._build_work_item(raw_step, index=index, used_ids=set())
            if incoming is None:
                return ToolResult(
                    ok=False,
                    content=f"step {index} requires a title",
                    blocked=True,
                    block_reason="invalid work plan step",
                )
            if source_document and not incoming.source_document:
                incoming.source_document = source_document[:500]
            existing = self._match_existing_work_item(incoming)
            existing_was_done = existing is not None and existing.status == "done"
            if existing is None:
                incoming.status = "pending" if incoming.status == "done" else incoming.status
                incoming.id = _unique_work_item_id(incoming.id, used_ids)
                used_ids.add(incoming.id)
                self.state.work_plan.append(incoming)
                existing = incoming
                added_count += 1
            else:
                merged_count += 1
                existing.title = incoming.title
                existing.required = incoming.required
                if incoming.acceptance:
                    existing.acceptance = incoming.acceptance
                if incoming.verification_hint:
                    existing.verification_hint = incoming.verification_hint
                if incoming.source_document:
                    existing.source_document = incoming.source_document
                if incoming.status == "in_progress" and existing.status == "pending":
                    existing.status = "in_progress"
            if raw_step.get("status") == "done" and not existing_was_done:
                evidence = self._record_document_acceptance(existing, source_document=source_document)
                existing.status = "done"
                _append_once(existing.evidence_ids, evidence.id)
                _append_once(existing.evidence, evidence.summary)
                document_done_count += 1
            elif incoming.status == "blocked" and existing.status != "done":
                existing.status = "blocked"
                if not existing.blocker:
                    existing.blocker = f"Document marks this step blocked: {source_document}".strip()
            elif existing.status != "done":
                reconciled = self._reconcile_existing_evidence(existing)
                if reconciled:
                    reconciled_count += 1
                if _looks_like_repeated_verification_step(existing.title):
                    duplicate_guidance_count += 1
        self.state.set_work_plan(self.state.work_plan)
        summary = (
            f"merged work plan: {merged_count} existing, {added_count} added, "
            f"{document_done_count} document-accepted done, "
            f"{reconciled_count} evidence-reconciled"
        )
        if duplicate_guidance_count:
            summary += (
                f", {duplicate_guidance_count} verification/meta steps should reuse existing evidence "
                "before running new checks"
            )
        return ToolResult(
            ok=True,
            content=f"{summary}\n\n{self._goal_json()}",
            metadata={"goal_event": "plan_merged", "goal": self.state.goal_snapshot()},
        )

    def update_work_step(
        self,
        step_id: str,
        status: Literal["pending", "in_progress", "done", "blocked"],
        evidence: str | None = None,
        evidence_ids: list[str] | None = None,
        blocker: str | None = None,
        acceptance: str | None = None,
        verification_hint: str | None = None,
        required: bool | None = None,
    ) -> ToolResult:
        item = self.state.find_work_step(step_id)
        if item is None:
            return ToolResult(
                ok=False,
                content=f"unknown work step: {step_id}",
                blocked=True,
                block_reason="unknown work step",
            )
        if status not in {"pending", "in_progress", "done", "blocked"}:
            return ToolResult(
                ok=False,
                content="status must be pending, in_progress, done, or blocked",
                blocked=True,
                block_reason="invalid work step status",
            )
        normalized_evidence_ids = _normalize_evidence_ids(evidence_ids)
        missing_evidence_ids = [
            evidence_id
            for evidence_id in normalized_evidence_ids
            if self.state.find_evidence_record(evidence_id) is None
        ]
        if missing_evidence_ids:
            recent_records = self._recent_evidence_records()
            suggested_ids = [record.id for record in recent_records[:3]]
            retry_guidance = self._evidence_retry_guidance(item.id, status, suggested_ids)
            return ToolResult(
                ok=False,
                content=(
                    f"unknown evidence_ids: {', '.join(missing_evidence_ids)}; "
                    f"{self._recent_evidence_guidance()} "
                    f"{retry_guidance}"
                ),
                blocked=True,
                block_reason="unknown evidence ids",
                metadata={
                    "failure_kind": "unknown_evidence_ids",
                    "missing_evidence_ids": missing_evidence_ids,
                    "suggested_evidence_ids": suggested_ids,
                    "recent_evidence_records": [record.to_dict() for record in recent_records],
                    "retry_arguments": {
                        "step_id": item.id,
                        "status": status,
                        "evidence_ids": suggested_ids[:1],
                    },
                },
            )
        normalized_evidence = " ".join(evidence.strip().split())[:500] if evidence else ""
        has_existing_evidence = bool(item.evidence or item.evidence_ids)
        evidence_text_is_known = bool(
            normalized_evidence and normalized_evidence in self.state.acceptance_evidence
        )
        if status == "done" and not (
            evidence_text_is_known or normalized_evidence_ids or has_existing_evidence
        ):
            recent_records = self._recent_evidence_records()
            suggested_ids = [record.id for record in recent_records[:3]]
            return ToolResult(
                ok=False,
                content=(
                    "cannot mark work step done without known evidence. "
                    "Plan step ids are created before verification; evidence_ids are created only after "
                    "verification, smoke checks, HTTP/browser checks, or other tool observations. "
                    f"{self._recent_evidence_guidance()} "
                    f"{self._evidence_retry_guidance(item.id, status, suggested_ids)} "
                    "If none of the recent evidence matches this step acceptance, run the narrowest "
                    "verification/smoke check first, then retry update_work_step with the new evidence_ids."
                ),
                blocked=True,
                block_reason="missing work step evidence",
                metadata={
                    "failure_kind": "missing_work_step_evidence",
                    "suggested_evidence_ids": suggested_ids,
                    "recent_evidence_records": [record.to_dict() for record in recent_records],
                    "retry_arguments": {
                        "step_id": item.id,
                        "status": status,
                        "evidence_ids": suggested_ids[:1],
                    },
                },
            )
        if acceptance is not None:
            item.acceptance = " ".join(acceptance.strip().split())[:500]
        if verification_hint is not None:
            item.verification_hint = " ".join(verification_hint.strip().split())[:500]
        if required is not None:
            item.required = bool(required)
        for evidence_id in normalized_evidence_ids:
            if evidence_id not in item.evidence_ids:
                item.evidence_ids.append(evidence_id)
        if normalized_evidence and normalized_evidence not in item.evidence:
            item.evidence.append(normalized_evidence)
        if blocker is not None:
            item.blocker = " ".join(blocker.strip().split())[:500]
        item.status = status
        if status == "in_progress":
            self.state.current_step_id = item.id
        elif status == "done" and self.state.current_step_id == item.id:
            self.state.current_step_id = next(
                (candidate.id for candidate in self.state.work_plan if candidate.status == "pending"),
                None,
            )
        return ToolResult(
            ok=True,
            content=self._goal_json(),
            metadata={
                "goal_event": "step_updated",
                "goal": self.state.goal_snapshot(),
                "step": item.to_dict(),
                "evidence_records": [
                    record.to_dict()
                    for evidence_id in item.evidence_ids
                    if (record := self.state.find_evidence_record(evidence_id)) is not None
                ],
            },
        )

    def _recent_evidence_guidance(self) -> str:
        recent = self._recent_evidence_records()
        if not recent:
            return "available recent evidence_ids: <none>."
        entries = []
        for record in recent:
            summary = " ".join(record.summary.strip().split())[:160]
            entries.append(f"{record.id} ({record.kind} via {record.source_tool}: {summary})")
        return "available recent evidence_ids: " + "; ".join(entries) + "."

    def _recent_evidence_records(self) -> list[EvidenceRecord]:
        return list(reversed(self.state.evidence_records[-8:]))

    def reconcile_work_plan_evidence(self, status: str = "open") -> ToolResult:
        allowed_statuses = {"open", "pending", "in_progress", "blocked", "all"}
        status = status if status in allowed_statuses else "open"
        updated: list[dict[str, object]] = []
        for item in self.state.work_plan:
            if item.status == "done":
                continue
            if status != "all":
                if status == "open" and item.status not in {"pending", "in_progress", "blocked"}:
                    continue
                if status != "open" and item.status != status:
                    continue
            before_status = item.status
            before_ids = set(item.evidence_ids)
            if not self._reconcile_existing_evidence(item):
                continue
            new_ids = [evidence_id for evidence_id in item.evidence_ids if evidence_id not in before_ids]
            updated.append(
                {
                    "id": item.id,
                    "title": item.title,
                    "from_status": before_status,
                    "to_status": item.status,
                    "attached_evidence_ids": new_ids,
                }
            )
        if updated:
            self.state.set_work_plan(self.state.work_plan)
        payload = {
            "reconciled_count": len(updated),
            "reconciled_steps": updated,
            "note": (
                "Existing evidence was matched against WorkPlan quality gates. "
                "Steps are marked done only when matching evidence categories satisfy the step."
            ),
        }
        return ToolResult(
            ok=True,
            content=json.dumps(payload, ensure_ascii=False, indent=2),
            metadata={"goal_event": "plan_evidence_reconciled", "reconciled_steps": updated},
        )

    @staticmethod
    def _evidence_retry_guidance(step_id: str, status: str, suggested_ids: list[str]) -> str:
        if not suggested_ids:
            return "No known evidence_ids are available yet."
        return (
            "Retry using an exact known evidence id, for example: "
            f"update_work_step(step_id={step_id!r}, status={status!r}, "
            f"evidence_ids={[suggested_ids[0]]!r}). Do not reuse unknown evidence ids."
        )

    def set_current_step(self, step_id: str) -> ToolResult:
        normalized = " ".join(step_id.strip().split())
        if not self.state.set_current_step(normalized):
            return ToolResult(
                ok=False,
                content=f"unknown work step: {step_id}",
                blocked=True,
                block_reason="unknown work step",
            )
        return ToolResult(
            ok=True,
            content=self._goal_json(),
            metadata={"goal_event": "current_step_updated", "goal": self.state.goal_snapshot()},
        )

    def accept_work_step(
        self,
        step_id: str,
        reason: str,
        source: Literal["user_confirmed", "document_marked_done", "model_reviewed_existing_evidence"] = "model_reviewed_existing_evidence",
    ) -> ToolResult:
        item = self.state.find_work_step(step_id)
        if item is None:
            return ToolResult(
                ok=False,
                content=f"unknown work step: {step_id}",
                blocked=True,
                block_reason="unknown work step",
            )
        normalized_reason = _normalize_string(reason)
        if not normalized_reason:
            return ToolResult(
                ok=False,
                content="reason is required for manual acceptance",
                blocked=True,
                block_reason="manual acceptance reason is required",
            )
        if source not in {"user_confirmed", "document_marked_done", "model_reviewed_existing_evidence"}:
            return ToolResult(
                ok=False,
                content="source must be user_confirmed, document_marked_done, or model_reviewed_existing_evidence",
                blocked=True,
                block_reason="invalid acceptance source",
            )
        source_labels = {
            "user_confirmed": "user acceptance",
            "document_marked_done": "document acceptance",
            "model_reviewed_existing_evidence": "model review acceptance",
        }
        evidence_kinds = {
            "user_confirmed": "user_acceptance",
            "document_marked_done": "document_acceptance",
            "model_reviewed_existing_evidence": "model_review_acceptance",
        }
        label = source_labels[source]
        evidence = self.state.add_evidence_record(
            kind=evidence_kinds[source],
            source_tool="accept_work_step",
            summary=f"{label}: {item.title} — {normalized_reason}",
            detail=normalized_reason,
            status=source,
        )
        item.status = "done"
        _append_once(item.evidence_ids, evidence.id)
        _append_once(item.evidence, evidence.summary)
        if self.state.current_step_id == item.id:
            self.state.current_step_id = next(
                (candidate.id for candidate in self.state.work_plan if candidate.status == "pending"),
                None,
            )
        return ToolResult(
            ok=True,
            content=self._goal_json(),
            metadata={
                "goal_event": "step_manually_accepted",
                "acceptance_source": source,
                "acceptance_kind": evidence.kind,
                "goal": self.state.goal_snapshot(),
                "step": item.to_dict(),
                "evidence_records": [evidence.to_dict()],
            },
        )

    def update_goal(self, status: Literal["complete", "blocked"]) -> ToolResult:
        if status not in {"complete", "blocked"}:
            return ToolResult(
                ok=False,
                content="status must be 'complete' or 'blocked'",
                blocked=True,
                block_reason="invalid goal status",
            )
        if not self.state.active_goal:
            return ToolResult(
                ok=False,
                content="cannot update goal because this session has no goal",
                blocked=True,
                block_reason="no active goal",
            )
        if status == "complete" and not self.state.acceptance_evidence:
            return ToolResult(
                ok=False,
                content=(
                    "goal remains active: cannot mark the whole goal complete without acceptance "
                    "evidence. Run project-specific verification, smoke checks, or other concrete "
                    "validation first, then retry update_goal only when the whole goal is complete."
                ),
                blocked=False,
                block_reason="missing acceptance evidence",
                metadata={
                    "failure_kind": "goal_not_complete",
                    "recoverable": True,
                    "suggested_action": "collect_acceptance_evidence_or_continue_work_plan",
                },
            )
        if status == "complete":
            incomplete_items = [
                item
                for item in self.state.work_plan
                if item.required and item.status != "done"
            ]
            incomplete = [item.id for item in incomplete_items]
            if incomplete:
                return ToolResult(
                    ok=False,
                    content=(
                        "goal remains active: cannot mark the whole goal complete because required "
                        "WorkPlan steps are not done. If the current requested scope is finished, "
                        "mark only the relevant work steps done or set the next current step; do not "
                        "complete the whole goal until every required step is done. "
                        f"unfinished_required_steps={', '.join(incomplete)}"
                    ),
                    blocked=False,
                    block_reason="required work steps not done",
                    metadata={
                        "failure_kind": "goal_not_complete",
                        "recoverable": True,
                        "suggested_action": "continue_work_plan_or_mark_specific_steps",
                        "incomplete_steps": [
                            {
                                "id": item.id,
                                "title": item.title,
                                "status": item.status,
                                "required": item.required,
                            }
                            for item in incomplete_items
                        ],
                    },
                )
            without_evidence = [
                item.id
                for item in self.state.work_plan
                if item.required and item.status == "done" and not (item.evidence or item.evidence_ids)
            ]
            if without_evidence:
                return ToolResult(
                    ok=False,
                    content=(
                        "goal remains active: cannot mark the whole goal complete because required "
                        "WorkPlan steps lack evidence. Attach evidence to those steps or run fresh "
                        "verification before completing the whole goal. "
                        f"steps_without_evidence={', '.join(without_evidence)}"
                    ),
                    blocked=False,
                    block_reason="required work steps lack evidence",
                    metadata={
                        "failure_kind": "goal_not_complete",
                        "recoverable": True,
                        "suggested_action": "attach_step_evidence_or_verify",
                        "steps_without_evidence": without_evidence,
                    },
                )
        self.state.goal_status = status
        self.state.archive_current_goal(f"marked_{status}")
        return ToolResult(
            ok=True,
            content=self._goal_json(),
            metadata={"goal_event": "updated", "goal": self.state.goal_snapshot()},
        )

    def no_write_decision(self, turn: GoalTurnState) -> GoalControlDecision | None:
        if not turn.requested:
            return None
        if turn.no_tool_retry_used:
            return None
        turn.no_tool_retry_used = True
        return GoalControlDecision(
            should_continue=True,
            expected_action="continue_project_build",
            runtime_message=(
                "The active goal requires concrete project progress, not only discussion. "
                "Inspect the workspace and project documents, then make implementation or test "
                "changes with tools. Do not stop with a conversational status update unless the "
                "whole requested project milestone is genuinely complete. If the goal is complete "
                "or genuinely blocked, call update_goal with the appropriate status."
            ),
        )

    def plan_completion_review_decision(self, turn: GoalTurnState) -> GoalControlDecision | None:
        if not turn.requested:
            return None
        if self.state.goal_status != "active":
            return None
        if turn.completion_review_retries >= 4:
            return None
        pending_required = [
            item for item in self.state.work_plan if item.required and item.status in {"pending", "in_progress"}
        ]
        blocked_required = [
            item for item in self.state.work_plan if item.required and item.status == "blocked"
        ]
        done_required = [
            item for item in self.state.work_plan if item.required and item.status == "done"
        ]
        if not self.state.work_plan and not self.state.acceptance_evidence:
            return None
        turn.completion_review_retries += 1
        if pending_required:
            expected_action = "continue_open_work_plan"
            runtime_message = (
                "The active goal is still open and the WorkPlan has required pending or in-progress "
                "steps. Do not stop after the current local plan. Review RootObjective and WorkPlan, "
                "select the next required step with set_current_step if useful, then continue with "
                "concrete tool calls. If the next safe action is clear from the WorkPlan and evidence, "
                "do not ask the user to choose between required steps; choose the next coherent step "
                "yourself. If the plan is outdated, update it with set_work_plan or update_work_step "
                "before continuing."
            )
        elif blocked_required:
            expected_action = "review_blocked_work_plan"
            runtime_message = (
                "The active goal is still open and required WorkPlan steps are blocked. Review the "
                "blocker evidence. If no independent work remains, call update_goal(status='blocked'); "
                "otherwise continue with another concrete step and keep the blocked item recorded."
            )
        elif done_required:
            expected_action = "review_goal_after_plan_done"
            runtime_message = (
                "The current WorkPlan has no required pending, in-progress, or blocked steps. The "
                "RootObjective is still active, so reconcile it now instead of starting broad "
                "rediscovery. If the completed WorkPlan covers the objective, call "
                "update_goal(status='complete') using the existing evidence. Only read more files or "
                "extend the WorkPlan if you can name a concrete missing acceptance boundary; do not "
                "loop over source files just to re-check already completed plan items. If a concrete "
                "unresolvable blocker remains, call update_goal(status='blocked') with that blocker."
            )
        else:
            expected_action = "review_active_goal"
            runtime_message = (
                "The active goal is still open. Do not treat the current response as terminal until "
                "you either continue concrete work, call update_goal(status='complete'), or call "
                "update_goal(status='blocked') with evidence."
            )
        return GoalControlDecision(True, expected_action, runtime_message)

    def verification_passed_decision(
        self, turn: GoalTurnState, *, plan_reason: str
    ) -> GoalControlDecision | None:
        if not turn.requested:
            return None
        if self.state.goal_status != "active":
            return None
        turn.verified_steps += 1
        if turn.verified_steps >= turn.max_steps:
            return None
        if plan_reason == "documentation-only changes":
            expected_action = "continue_project_build_after_docs"
            runtime_message = (
                "The documentation-only change passed verification, but the active goal requires "
                "project implementation. Continue with concrete source code, tests, or runnable "
                "project files. Inspect existing project documents/files first if needed."
            )
        else:
            expected_action = "continue_project_build_after_verified_step"
            runtime_message = (
                "The current implementation step passed verification. Continue the active goal by "
                "doing the next concrete task if required. If multiple required tasks remain, choose "
                "the next coherent one from the WorkPlan instead of asking the user which required "
                "step to do. If the whole goal is complete, respond by calling update_goal with "
                "status='complete', then give a concise final summary."
            )
        return GoalControlDecision(True, expected_action, runtime_message)

    def _goal_json(self) -> str:
        return json.dumps(self.state.goal_snapshot(), ensure_ascii=False, indent=2)

    def _match_existing_work_item(self, incoming: WorkPlanItem) -> WorkPlanItem | None:
        exact = self.state.find_work_step(incoming.id)
        if exact is not None:
            return exact
        incoming_title = _canonical_title(incoming.title)
        for item in self.state.work_plan:
            if _canonical_title(item.title) == incoming_title:
                return item
        return None

    def _record_document_acceptance(self, item: WorkPlanItem, *, source_document: str):
        source = source_document or item.source_document
        return self.state.add_evidence_record(
            kind="document_acceptance",
            source_tool="sync_work_plan_from_document",
            summary=f"document marks work step done: {item.title}",
            detail=f"Source document marks this step as complete: {source}".strip(),
            path=source,
            status="manual",
        )

    def _reconcile_existing_evidence(self, item: WorkPlanItem) -> bool:
        """Attach already-collected evidence to newly synced document steps.

        The runtime should not understand product semantics, but it can recognize
        reusable engineering evidence categories. This prevents a document sync from
        turning "we have been doing integration tests" into a fresh pending feature
        that causes another expensive browser/API loop.
        """
        matches = self._matching_existing_evidence(item)
        if not matches:
            return False
        for record in matches[:4]:
            _append_once(item.evidence_ids, record.id)
            _append_once(item.evidence, record.summary)
        if _evidence_satisfies_meta_step(item.title, matches):
            item.status = "done"
            if self.state.current_step_id == item.id:
                self.state.current_step_id = next(
                    (candidate.id for candidate in self.state.work_plan if candidate.status == "pending"),
                    None,
                )
        return True

    def _matching_existing_evidence(self, item: WorkPlanItem) -> list[EvidenceRecord]:
        title = item.title
        title_tokens = _semantic_tokens(title)
        matches: list[EvidenceRecord] = []
        for record in reversed(self.state.evidence_records[-80:]):
            if record.status.startswith("failed") or record.status in {"blocked", "timeout"}:
                continue
            evidence_text = " ".join(
                part
                for part in (
                    record.kind,
                    record.source_tool,
                    record.summary,
                    record.detail,
                    record.command,
                    record.url,
                    record.path,
                )
                if part
            )
            if _evidence_category_matches(title, record):
                matches.append(record)
                continue
            evidence_tokens = _semantic_tokens(evidence_text)
            if title_tokens and len(title_tokens & evidence_tokens) >= min(2, len(title_tokens)):
                matches.append(record)
        return matches

    @staticmethod
    def _build_work_item(
        raw_step: dict[str, object], *, index: int, used_ids: set[str]
    ) -> WorkPlanItem | None:
        title = _normalize_string(raw_step.get("title"))
        if not title:
            return None
        item_id = _normalize_string(raw_step.get("id")) or f"step-{index}"
        base_id = item_id[:80]
        item_id = base_id
        suffix = 2
        while item_id in used_ids:
            item_id = f"{base_id}-{suffix}"[:80]
            suffix += 1
        used_ids.add(item_id)
        status = _normalize_string(raw_step.get("status")) or "pending"
        if status not in {"pending", "in_progress", "done", "blocked"}:
            status = "pending"
        evidence = raw_step.get("evidence")
        evidence_ids = raw_step.get("evidence_ids")
        required = raw_step.get("required")
        return WorkPlanItem(
            id=item_id,
            title=title[:300],
            status=status,
            acceptance=(_normalize_string(raw_step.get("acceptance")) or "")[:500],
            evidence=[item[:500] for item in evidence if isinstance(item, str)][:10]
            if isinstance(evidence, list)
            else [],
            evidence_ids=[item[:80] for item in evidence_ids if isinstance(item, str)][:20]
            if isinstance(evidence_ids, list)
            else [],
            blocker=(_normalize_string(raw_step.get("blocker")) or "")[:500],
            verification_hint=(_normalize_string(raw_step.get("verification_hint")) or "")[:500],
            required=required if isinstance(required, bool) else True,
            source_document=(_normalize_string(raw_step.get("source_document")) or "")[:500],
        )


def _normalize_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.strip().split())
    return normalized or None


def _normalize_evidence_ids(value: list[str] | None) -> list[str]:
    if not isinstance(value, list):
        return []
    ids: list[str] = []
    for item in value:
        normalized = _normalize_string(item)
        if normalized and normalized not in ids:
            ids.append(normalized[:80])
    return ids


def _canonical_title(value: str) -> str:
    return "".join(char.lower() for char in value if char.isalnum())


def _semantic_tokens(value: str) -> set[str]:
    normalized = value.lower()
    tokens = {
        item
        for item in re_split_words(normalized)
        if len(item) >= 2 and item not in _STOP_WORDS
    }
    for char in normalized:
        if "\u4e00" <= char <= "\u9fff":
            tokens.add(char)
    return tokens


def _looks_like_repeated_verification_step(title: str) -> bool:
    return bool(_step_verification_categories(title))


def _evidence_category_matches(title: str, record: EvidenceRecord) -> bool:
    categories = _step_verification_categories(title)
    if not categories:
        return False
    evidence_categories = _evidence_categories(record)
    return bool(categories & evidence_categories)


def _evidence_satisfies_meta_step(title: str, matches: list[EvidenceRecord]) -> bool:
    categories = _step_verification_categories(title)
    if not categories:
        return False
    matched_categories: set[str] = set()
    for record in matches:
        matched_categories.update(_evidence_categories(record))
    if "integration" in categories and {"api", "ui"} <= matched_categories:
        return True
    if "e2e" in categories and "ui" in matched_categories:
        return True
    if "api" in categories and "api" in matched_categories:
        return True
    if "ui" in categories and "ui" in matched_categories:
        return True
    if "automation" in categories and ({"api", "ui"} <= matched_categories or "command" in matched_categories):
        return True
    if "unit" in categories and "unit" in matched_categories:
        return True
    return len(categories & matched_categories) >= 2


def _step_verification_categories(title: str) -> set[str]:
    lowered = title.lower()
    categories: set[str] = set()
    if _contains_any(lowered, ("test", "tests", "testing", "verification", "verify", "smoke", "测试", "验证", "校验")):
        categories.add("verification")
    if _contains_any(lowered, ("unit", "pytest", "unittest", "单元")):
        categories.add("unit")
    if _contains_any(lowered, ("integration", "integrated", "联调", "集成", "接口联调")):
        categories.add("integration")
    if _contains_any(lowered, ("e2e", "end-to-end", "end to end", "browser", "浏览器", "端到端", "流程自动化")):
        categories.add("e2e")
    if _contains_any(lowered, ("api", "http", "endpoint", "接口", "后端")):
        categories.add("api")
    if _contains_any(lowered, ("ui", "frontend", "browser", "page", "界面", "页面", "前端")):
        categories.add("ui")
    if _contains_any(lowered, ("automation", "automated", "自动化")):
        categories.add("automation")
    if not categories - {"verification"}:
        return set()
    return categories


def _evidence_categories(record: EvidenceRecord) -> set[str]:
    text = " ".join(
        part.lower()
        for part in (
            record.kind,
            record.source_tool,
            record.summary,
            record.detail,
            record.command,
            record.url,
            record.path,
        )
        if part
    )
    categories: set[str] = set()
    if record.source_tool in {"browser_test", "view_image"} or _contains_any(text, ("browser", "playwright", "screenshot")):
        categories.update({"verification", "ui", "e2e", "automation"})
    if record.source_tool == "http_request" or _contains_any(text, ("http", "api/", " status=200", " status_code=200")):
        categories.update({"verification", "api", "integration", "automation"})
    if record.source_tool in {"run_command", "run_python"}:
        categories.update({"verification", "command", "automation"})
        if _contains_any(text, ("pytest", "unittest", " python -m pytest", "go test", "cargo test")):
            categories.add("unit")
        if _contains_any(text, ("npm test", "npm run test", "vitest", "jest")):
            categories.update({"unit", "ui"})
        if _contains_any(text, ("npm run build", "compileall", "cargo build", "go build")):
            categories.add("build")
    if record.source_tool == "start_background_command" or _contains_any(text, ("server", "port", "ready")):
        categories.update({"service", "integration"})
    return categories


def _contains_any(value: str, needles: tuple[str, ...]) -> bool:
    return any(needle in value for needle in needles)


def _unique_work_item_id(item_id: str, used_ids: set[str]) -> str:
    base_id = item_id[:80]
    candidate = base_id
    suffix = 2
    while candidate in used_ids:
        candidate = f"{base_id}-{suffix}"[:80]
        suffix += 1
    return candidate


def _append_once(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def re_split_words(text: str) -> list[str]:
    return [item for item in re.split(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", text) if item]


_STOP_WORDS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "this",
    "that",
    "step",
    "work",
    "plan",
    "status",
    "ok",
    "true",
    "false",
}
