from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import threading
from typing import Any
from uuid import uuid4


IGNORED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    ".minicodex2",
    ".pytest_cache",
    ".ruff_cache",
    "build",
    "dist",
    "target",
    ".next",
    ".cache",
    ".turbo",
    "__pycache__",
}


@dataclass(slots=True)
class ProjectGuidanceFile:
    path: str
    content: str
    truncated: bool = False

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class ProjectMemoryDocument:
    path: str
    title: str
    headings: list[str]
    excerpt: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class ProjectMemoryFact:
    id: str
    title: str
    content: str
    tags: list[str] = field(default_factory=list)
    scope: str = "project"
    confidence: str = "medium"
    source: str = "model"
    stale: bool = False
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    last_used_at: str = ""
    use_count: int = 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def index_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "tags": self.tags,
            "scope": self.scope,
            "confidence": self.confidence,
            "stale": self.stale,
            "updated_at": self.updated_at,
            "summary": _compact_text(self.content, 180),
        }


class ProjectMemoryStore:
    """Persistent model-writable project memory.

    The store intentionally preserves facts without interpreting project semantics.
    Runtime provides persistence, search, dedupe, and compact context views; the
    model decides what facts matter and when to use them.
    """

    def __init__(self, artifact_root: Path, *, max_facts: int = 500) -> None:
        self.path = artifact_root / "memory" / "project_facts.json"
        self.max_facts = max_facts
        self._facts: list[ProjectMemoryFact] | None = None

    def all(self) -> list[ProjectMemoryFact]:
        self._ensure_loaded()
        return list(self._facts or [])

    def remember(
        self,
        *,
        title: str,
        content: str,
        tags: list[str] | None = None,
        scope: str = "project",
        confidence: str = "medium",
        source: str = "model",
    ) -> ProjectMemoryFact:
        self._ensure_loaded()
        normalized_title = _compact_text(" ".join(title.strip().split()), 160)
        normalized_content = _compact_text(" ".join(content.strip().split()), 4000)
        normalized_tags = _normalize_tags(tags or [])
        normalized_scope = _compact_text(" ".join(scope.strip().split()) or "project", 80)
        normalized_confidence = _normalize_confidence(confidence)
        normalized_source = _compact_text(" ".join(source.strip().split()) or "model", 160)
        if not normalized_title:
            normalized_title = _compact_text(normalized_content, 80) or "project fact"
        if not normalized_content:
            normalized_content = normalized_title

        existing = self._find_equivalent(
            title=normalized_title,
            scope=normalized_scope,
            tags=normalized_tags,
        )
        now = datetime.now(UTC).isoformat()
        if existing is not None:
            existing.content = normalized_content
            existing.tags = normalized_tags
            existing.scope = normalized_scope
            existing.confidence = normalized_confidence
            existing.source = normalized_source
            existing.stale = False
            existing.updated_at = now
            self._save()
            return existing

        fact = ProjectMemoryFact(
            id=f"mem-{uuid4().hex[:12]}",
            title=normalized_title,
            content=normalized_content,
            tags=normalized_tags,
            scope=normalized_scope,
            confidence=normalized_confidence,
            source=normalized_source,
        )
        assert self._facts is not None
        self._facts.append(fact)
        self._facts = self._facts[-self.max_facts :]
        self._save()
        return fact

    def update(
        self,
        memory_id: str,
        *,
        title: str | None = None,
        content: str | None = None,
        tags: list[str] | None = None,
        scope: str | None = None,
        confidence: str | None = None,
        stale: bool | None = None,
    ) -> ProjectMemoryFact | None:
        self._ensure_loaded()
        fact = self.get(memory_id, mark_used=False)
        if fact is None:
            return None
        if title is not None:
            fact.title = _compact_text(" ".join(title.strip().split()), 160) or fact.title
        if content is not None:
            fact.content = _compact_text(" ".join(content.strip().split()), 4000) or fact.content
        if tags is not None:
            fact.tags = _normalize_tags(tags)
        if scope is not None:
            fact.scope = _compact_text(" ".join(scope.strip().split()) or "project", 80)
        if confidence is not None:
            fact.confidence = _normalize_confidence(confidence)
        if stale is not None:
            fact.stale = bool(stale)
        fact.updated_at = datetime.now(UTC).isoformat()
        self._save()
        return fact

    def get(self, memory_id: str, *, mark_used: bool = True) -> ProjectMemoryFact | None:
        self._ensure_loaded()
        memory_id = memory_id.strip()
        for fact in self._facts or []:
            if fact.id == memory_id:
                if mark_used:
                    fact.use_count += 1
                    fact.last_used_at = datetime.now(UTC).isoformat()
                    self._save()
                return fact
        return None

    def forget(self, memory_id: str) -> bool:
        self._ensure_loaded()
        before = len(self._facts or [])
        self._facts = [fact for fact in self._facts or [] if fact.id != memory_id.strip()]
        changed = len(self._facts) != before
        if changed:
            self._save()
        return changed

    def search(
        self,
        *,
        query: str = "",
        tags: list[str] | None = None,
        scope: str = "",
        include_stale: bool = False,
        limit: int = 8,
    ) -> list[ProjectMemoryFact]:
        self._ensure_loaded()
        limit = max(1, min(int(limit or 8), 50))
        query_terms = {
            term.lower()
            for term in re_split_words(query)
            if len(term) >= 2
        }
        tag_terms = {tag.lower() for tag in _normalize_tags(tags or [])}
        normalized_scope = scope.strip().lower()
        candidates: list[ProjectMemoryFact] = []
        for fact in self._facts or []:
            if fact.stale and not include_stale:
                continue
            if normalized_scope and fact.scope.lower() != normalized_scope:
                continue
            fact_tags = {tag.lower() for tag in fact.tags}
            if tag_terms and not tag_terms.issubset(fact_tags):
                continue
            candidates.append(fact)

        if not query_terms:
            return sorted(candidates, key=_memory_recency_key, reverse=True)[:limit]

        scored = [
            (self._score_fact(fact, query_terms), fact)
            for fact in candidates
        ]
        ranked = [
            fact
            for score, fact in sorted(scored, key=lambda item: (item[0], _memory_recency_key(item[1])), reverse=True)
            if score > 0
        ]
        return ranked[:limit]

    def hot(self, *, limit: int = 5) -> list[ProjectMemoryFact]:
        self._ensure_loaded()
        active = [fact for fact in self._facts or [] if not fact.stale]
        return sorted(active, key=_memory_hot_key, reverse=True)[: max(0, min(limit, 20))]

    def index(self, *, limit: int = 20) -> list[dict[str, object]]:
        self._ensure_loaded()
        active = [fact for fact in self._facts or [] if not fact.stale]
        ranked = sorted(active, key=_memory_hot_key, reverse=True)
        return [fact.index_dict() for fact in ranked[: max(0, min(limit, 100))]]

    def _score_fact(self, fact: ProjectMemoryFact, query_terms: set[str]) -> int:
        text_terms = {
            term.lower()
            for term in re_split_words(
                f"{fact.id} {fact.title} {fact.content} {' '.join(fact.tags)} {fact.scope}"
            )
            if len(term) >= 2
        }
        score = len(query_terms & text_terms) * 10
        if fact.confidence == "high":
            score += 3
        if fact.stale:
            score -= 5
        score += min(fact.use_count, 5)
        return score

    def _find_equivalent(
        self,
        *,
        title: str,
        scope: str,
        tags: list[str],
    ) -> ProjectMemoryFact | None:
        normalized = title.casefold()
        tag_set = {tag.casefold() for tag in tags}
        for fact in self._facts or []:
            if fact.scope.casefold() != scope.casefold():
                continue
            if fact.title.casefold() == normalized:
                return fact
            if tag_set and fact.title.casefold() == normalized and tag_set == {tag.casefold() for tag in fact.tags}:
                return fact
        return None

    def _ensure_loaded(self) -> None:
        if self._facts is not None:
            return
        self._facts = []
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        raw_facts = data.get("facts") if isinstance(data, dict) else data
        if not isinstance(raw_facts, list):
            return
        for raw in raw_facts:
            if not isinstance(raw, dict):
                continue
            fact = _memory_fact_from_dict(raw)
            if fact is not None:
                self._facts.append(fact)

    def _save(self) -> None:
        self._ensure_loaded()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": datetime.now(UTC).isoformat(),
            "facts": [fact.to_dict() for fact in self._facts or []],
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


@dataclass(slots=True)
class WorkflowMemory:
    id: str
    title: str
    cwd: str = "."
    command: str = ""
    purpose: str = "workflow"
    ready_check: str = ""
    steps: list[str] = field(default_factory=list)
    ports: list[int] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    scope: str = "project"
    confidence: str = "medium"
    source: str = "model"
    notes: str = ""
    stale: bool = False
    success_count: int = 0
    failure_count: int = 0
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    last_used_at: str = ""
    last_verified_at: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def index_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "cwd": self.cwd,
            "command": self.command,
            "purpose": self.purpose,
            "ready_check": self.ready_check,
            "steps": self.steps,
            "ports": self.ports,
            "tags": self.tags,
            "scope": self.scope,
            "confidence": self.confidence,
            "stale": self.stale,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "updated_at": self.updated_at,
            "summary": _compact_text(
                self.notes or " | ".join(self.steps[:3]) or self.command or self.ready_check or self.title,
                180,
            ),
        }


class WorkflowMemoryStore:
    """Persistent model-writable operating procedures.

    Workflow memory is intentionally procedural and narrow: where to run a stable
    command, what it is for, and how the model can verify it next time. Runtime
    persists and ranks these records; the model decides when a workflow matters.
    """

    def __init__(self, artifact_root: Path, *, max_workflows: int = 300) -> None:
        self.path = artifact_root / "memory" / "workflows.json"
        self.max_workflows = max_workflows
        self._workflows: list[WorkflowMemory] | None = None

    def all(self) -> list[WorkflowMemory]:
        self._ensure_loaded()
        return list(self._workflows or [])

    def remember(
        self,
        *,
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
    ) -> WorkflowMemory:
        self._ensure_loaded()
        normalized = _normalize_workflow_fields(
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
        existing = self._find_equivalent(
            title=normalized["title"],
            cwd=normalized["cwd"],
            command=normalized["command"],
            purpose=normalized["purpose"],
            scope=normalized["scope"],
        )
        now = datetime.now(UTC).isoformat()
        if existing is not None:
            existing.title = normalized["title"]
            existing.cwd = normalized["cwd"]
            existing.command = normalized["command"]
            existing.purpose = normalized["purpose"]
            existing.ready_check = normalized["ready_check"]
            existing.steps = normalized["steps"] or existing.steps
            existing.ports = normalized["ports"]
            existing.tags = normalized["tags"]
            existing.scope = normalized["scope"]
            existing.confidence = normalized["confidence"]
            existing.source = normalized["source"]
            existing.notes = normalized["notes"]
            existing.stale = False
            existing.updated_at = now
            self._save()
            return existing

        workflow = WorkflowMemory(
            id=f"wf-{uuid4().hex[:12]}",
            title=normalized["title"],
            cwd=normalized["cwd"],
            command=normalized["command"],
            purpose=normalized["purpose"],
            ready_check=normalized["ready_check"],
            steps=normalized["steps"],
            ports=normalized["ports"],
            tags=normalized["tags"],
            scope=normalized["scope"],
            confidence=normalized["confidence"],
            source=normalized["source"],
            notes=normalized["notes"],
        )
        assert self._workflows is not None
        self._workflows.append(workflow)
        self._workflows = self._workflows[-self.max_workflows :]
        self._save()
        return workflow

    def get(self, workflow_id: str, *, mark_used: bool = True) -> WorkflowMemory | None:
        self._ensure_loaded()
        workflow_id = workflow_id.strip()
        for workflow in self._workflows or []:
            if workflow.id == workflow_id:
                if mark_used:
                    workflow.last_used_at = datetime.now(UTC).isoformat()
                    self._save()
                return workflow
        return None

    def update(
        self,
        workflow_id: str,
        *,
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
    ) -> WorkflowMemory | None:
        self._ensure_loaded()
        workflow = self.get(workflow_id, mark_used=False)
        if workflow is None:
            return None
        if title is not None:
            workflow.title = _compact_text(" ".join(title.strip().split()), 160) or workflow.title
        if cwd is not None:
            workflow.cwd = _normalize_relative_text(cwd, 180) or "."
        if command is not None:
            workflow.command = _compact_text(" ".join(command.strip().split()), 600)
        if purpose is not None:
            workflow.purpose = _compact_text(" ".join(purpose.strip().split()) or "workflow", 80)
        if ready_check is not None:
            workflow.ready_check = _compact_text(" ".join(ready_check.strip().split()), 300)
        if steps is not None:
            workflow.steps = _normalize_steps(steps)
        if ports is not None:
            workflow.ports = _normalize_ports(ports)
        if tags is not None:
            workflow.tags = _normalize_tags(tags)
        if scope is not None:
            workflow.scope = _compact_text(" ".join(scope.strip().split()) or "project", 80)
        if confidence is not None:
            workflow.confidence = _normalize_confidence(confidence)
        if notes is not None:
            workflow.notes = _compact_text(" ".join(notes.strip().split()), 1200)
        if stale is not None:
            workflow.stale = bool(stale)
        workflow.success_count = max(0, workflow.success_count + int(success_delta or 0))
        workflow.failure_count = max(0, workflow.failure_count + int(failure_delta or 0))
        if success_delta > 0:
            workflow.last_verified_at = datetime.now(UTC).isoformat()
            if workflow.confidence == "low":
                workflow.confidence = "medium"
            elif workflow.success_count >= 2 and workflow.confidence == "medium":
                workflow.confidence = "high"
        if failure_delta > 0 and workflow.failure_count >= max(2, workflow.success_count + 2):
            workflow.stale = True
        workflow.updated_at = datetime.now(UTC).isoformat()
        self._save()
        return workflow

    def forget(self, workflow_id: str) -> bool:
        self._ensure_loaded()
        before = len(self._workflows or [])
        self._workflows = [
            workflow
            for workflow in self._workflows or []
            if workflow.id != workflow_id.strip()
        ]
        changed = len(self._workflows) != before
        if changed:
            self._save()
        return changed

    def search(
        self,
        *,
        query: str = "",
        tags: list[str] | None = None,
        scope: str = "",
        purpose: str = "",
        include_stale: bool = False,
        limit: int = 8,
    ) -> list[WorkflowMemory]:
        self._ensure_loaded()
        limit = max(1, min(int(limit or 8), 50))
        query_terms = {
            term.lower()
            for term in re_split_words(query)
            if len(term) >= 2
        }
        tag_terms = {tag.lower() for tag in _normalize_tags(tags or [])}
        normalized_scope = scope.strip().lower()
        normalized_purpose = purpose.strip().lower()
        candidates: list[WorkflowMemory] = []
        for workflow in self._workflows or []:
            if workflow.stale and not include_stale:
                continue
            if normalized_scope and workflow.scope.lower() != normalized_scope:
                continue
            if normalized_purpose and workflow.purpose.lower() != normalized_purpose:
                continue
            workflow_tags = {tag.lower() for tag in workflow.tags}
            if tag_terms and not tag_terms.issubset(workflow_tags):
                continue
            candidates.append(workflow)
        if not query_terms:
            return sorted(candidates, key=_workflow_hot_key, reverse=True)[:limit]
        scored = [
            (self._score_workflow(workflow, query_terms), workflow)
            for workflow in candidates
        ]
        ranked = [
            workflow
            for score, workflow in sorted(scored, key=lambda item: (item[0], _workflow_hot_key(item[1])), reverse=True)
            if score > 0
        ]
        return ranked[:limit]

    def hot(self, *, limit: int = 5) -> list[WorkflowMemory]:
        self._ensure_loaded()
        active = [workflow for workflow in self._workflows or [] if not workflow.stale]
        return sorted(active, key=_workflow_hot_key, reverse=True)[: max(0, min(limit, 20))]

    def index(self, *, limit: int = 20) -> list[dict[str, object]]:
        self._ensure_loaded()
        active = [workflow for workflow in self._workflows or [] if not workflow.stale]
        ranked = sorted(active, key=_workflow_hot_key, reverse=True)
        return [workflow.index_dict() for workflow in ranked[: max(0, min(limit, 100))]]

    def _score_workflow(self, workflow: WorkflowMemory, query_terms: set[str]) -> int:
        text_terms = {
            term.lower()
            for term in re_split_words(
                " ".join(
                    [
                        workflow.id,
                        workflow.title,
                        workflow.cwd,
                        workflow.command,
                        workflow.purpose,
                        workflow.ready_check,
                        " ".join(workflow.steps),
                        " ".join(workflow.tags),
                        workflow.scope,
                        workflow.notes,
                    ]
                )
            )
            if len(term) >= 2
        }
        score = len(query_terms & text_terms) * 10
        if workflow.confidence == "high":
            score += 4
        score += min(workflow.success_count, 6) * 2
        score -= min(workflow.failure_count, 6) * 2
        return score

    def _find_equivalent(
        self,
        *,
        title: str,
        cwd: str,
        command: str,
        purpose: str,
        scope: str,
    ) -> WorkflowMemory | None:
        title_key = title.casefold()
        cwd_key = cwd.casefold()
        command_key = command.casefold()
        purpose_key = purpose.casefold()
        scope_key = scope.casefold()
        for workflow in self._workflows or []:
            if workflow.scope.casefold() != scope_key:
                continue
            if command_key and workflow.command.casefold() == command_key and workflow.cwd.casefold() == cwd_key:
                return workflow
            if (
                workflow.title.casefold() == title_key
                and workflow.cwd.casefold() == cwd_key
                and workflow.purpose.casefold() == purpose_key
            ):
                return workflow
        return None

    def _ensure_loaded(self) -> None:
        if self._workflows is not None:
            return
        self._workflows = []
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        raw_workflows = data.get("workflows") if isinstance(data, dict) else data
        if not isinstance(raw_workflows, list):
            return
        for raw in raw_workflows:
            if not isinstance(raw, dict):
                continue
            workflow = _workflow_from_dict(raw)
            if workflow is not None:
                self._workflows.append(workflow)

    def _save(self) -> None:
        self._ensure_loaded()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": datetime.now(UTC).isoformat(),
            "workflows": [workflow.to_dict() for workflow in self._workflows or []],
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


@dataclass(slots=True)
class TurnMemoryRecord:
    id: str
    kind: str
    title: str
    content: str
    scope: str = "project"
    tags: list[str] = field(default_factory=list)
    source_turn_id: str = ""
    source_user_input: str = ""
    confidence: str = "medium"
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    occurrences: int = 1

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class TurnMemoryStore:
    """Codex-style lightweight memory consolidation for completed turns.

    This store is deliberately evidence-driven: it extracts from structured runtime
    events and turn metrics, then writes file-backed memory artifacts. It does not
    guess project semantics or decide user preferences from raw natural language.
    Model-authored durable facts still belong in ProjectMemoryStore,
    WorkflowMemoryStore, or SessionState lessons.
    """

    def __init__(self, artifact_root: Path, *, max_records: int = 500) -> None:
        self.root = artifact_root / "memory"
        self.records_path = self.root / "turn_memories.json"
        self.raw_path = self.root / "raw_turn_memories.jsonl"
        self.memory_md_path = self.root / "MEMORY.md"
        self.summary_path = self.root / "memory_summary.md"
        self.max_records = max_records
        self._records: list[TurnMemoryRecord] | None = None
        self._lock = threading.RLock()

    def all(self) -> list[TurnMemoryRecord]:
        with self._lock:
            self._ensure_loaded()
            return list(self._records or [])

    def important(self, *, kinds: set[str], limit: int = 12) -> list[TurnMemoryRecord]:
        with self._lock:
            self._ensure_loaded()
            wanted = {kind.strip() for kind in kinds if kind.strip()}
            records = [
                record
                for record in self._records or []
                if record.kind in wanted
            ]
            return sorted(
                records,
                key=lambda item: (_turn_memory_importance_rank(item), item.occurrences, item.updated_at),
                reverse=True,
            )[: max(0, min(limit, 50))]

    def summary(self, *, max_chars: int = 2400) -> str:
        with self._lock:
            if not self.summary_path.exists():
                return ""
            text = _read_text(self.summary_path, max_bytes=max(max_chars * 4, 4096))
            return _compact_text(text, max_chars)

    def record_turn(
        self,
        *,
        turn_id: str,
        user_input: str,
        result_status: str,
        metrics: dict[str, object],
        events: list[Any],
    ) -> list[TurnMemoryRecord]:
        if turn_id.startswith("idle_"):
            return []
        with self._lock:
            candidates = self._extract_candidates(
                turn_id=turn_id,
                user_input=user_input,
                result_status=result_status,
                metrics=metrics,
                events=events,
            )
            if not candidates:
                return []

            self._ensure_loaded()
            changed: list[TurnMemoryRecord] = []
            for candidate in candidates:
                record = self._upsert(candidate)
                changed.append(record)
                self._append_raw(record)
            self._records = sorted(
                self._records or [],
                key=lambda item: (item.occurrences, item.updated_at),
                reverse=True,
            )[: self.max_records]
            self._save()
            self._write_memory_artifacts()
            return changed

    def record_model_memories(
        self,
        *,
        turn_id: str,
        user_input: str,
        memories: list[dict[str, object]],
        rollout_summary: str = "",
    ) -> list[TurnMemoryRecord]:
        if not memories and not rollout_summary:
            return []
        with self._lock:
            self._ensure_loaded()
            changed: list[TurnMemoryRecord] = []
            if rollout_summary.strip():
                summary_record = self._candidate(
                    kind="rollout_summary",
                    title=f"turn {turn_id} summary",
                    content=rollout_summary,
                    tags=["rollout-summary"],
                    turn_id=turn_id,
                    user_input=user_input,
                    confidence="medium",
                )
                changed.append(self._upsert(summary_record))
                self._append_raw(summary_record)
            for raw in memories:
                title = str(raw.get("title") or "").strip()
                content = str(raw.get("content") or "").strip()
                if not title or not content:
                    continue
                tags = raw.get("tags")
                if not isinstance(tags, list):
                    tags = []
                kind = str(raw.get("kind") or "lesson")
                reuse_rule = str(raw.get("reuse_rule") or "").strip()
                evidence = str(raw.get("evidence") or "").strip()
                if reuse_rule:
                    content += f"\nReuse rule: {reuse_rule}"
                if evidence:
                    content += f"\nEvidence: {evidence}"
                record = TurnMemoryRecord(
                    id=_turn_memory_id(kind, title, content),
                    kind=_compact_text(kind, 80),
                    title=_compact_text(title, 160),
                    content=_compact_text(content, 2000),
                    scope=_compact_text(str(raw.get("scope") or "project"), 80),
                    tags=_normalize_tags([str(item) for item in tags]),
                    source_turn_id=turn_id,
                    source_user_input=_compact_text(user_input, 500),
                    confidence=_normalize_confidence(str(raw.get("confidence") or "medium")),
                )
                changed.append(self._upsert(record))
                self._append_raw(record)
            if not changed:
                return []
            self._records = sorted(
                self._records or [],
                key=lambda item: (item.occurrences, item.updated_at),
                reverse=True,
            )[: self.max_records]
            self._save()
            self._write_memory_artifacts()
            return changed

    def _extract_candidates(
        self,
        *,
        turn_id: str,
        user_input: str,
        result_status: str,
        metrics: dict[str, object],
        events: list[Any],
    ) -> list[TurnMemoryRecord]:
        candidates: list[TurnMemoryRecord] = []
        tool_starts: list[tuple[str, str]] = []
        tool_failures: list[dict[str, str]] = []
        successful_reusable_tools: list[tuple[str, str]] = []
        for event in events:
            payload = getattr(event, "payload", {})
            if not isinstance(payload, dict):
                continue
            if getattr(event, "type", "") == "tool_call_started":
                name = str(payload.get("name") or "")
                summary = _compact_text(str(payload.get("summary") or ""), 500)
                if name:
                    tool_starts.append((name, summary))
            elif getattr(event, "type", "") == "tool_call_finished":
                name = str(payload.get("name") or "")
                if payload.get("ok"):
                    if name in {"run_command", "run_python", "start_background_command", "http_request", "browser_test"}:
                        summary = _compact_text(str(payload.get("summary") or ""), 500)
                        if summary:
                            successful_reusable_tools.append((name, summary))
                    continue
                failure_kind = _compact_text(str(payload.get("failure_kind") or ""), 80)
                if failure_kind == "unknown_tool":
                    # Unknown tools are usually model/tool-schema drift from a single turn.
                    # They should be fed back immediately, but not promoted into durable
                    # project memory where they can bias future turns toward the fake tool.
                    continue
                tool_failures.append(
                    {
                        "name": name or "tool",
                        "kind": failure_kind,
                        "stage": _compact_text(str(payload.get("failure_stage") or ""), 80),
                        "summary": _compact_text(
                            str(payload.get("failure_summary") or payload.get("content_excerpt") or ""),
                            500,
                        ),
                    }
                )

        grouped_failures: dict[tuple[str, str, str], dict[str, object]] = {}
        for failure in tool_failures:
            key = (failure["name"], failure["kind"], failure["summary"][:160])
            entry = grouped_failures.setdefault(key, {"count": 0, "failure": failure})
            entry["count"] = int(entry["count"]) + 1
        for entry in grouped_failures.values():
            count = int(entry["count"])
            failure = entry["failure"]
            if not isinstance(failure, dict):
                continue
            title_parts = [str(failure.get("name") or "tool"), "failure"]
            if failure.get("kind"):
                title_parts.append(str(failure["kind"]))
            title = " ".join(title_parts)
            content = (
                f"Turn {turn_id} hit {count} {failure.get('name')} failure(s). "
                f"Kind={failure.get('kind') or 'unknown'}; stage={failure.get('stage') or 'unknown'}. "
                f"Observed summary: {failure.get('summary') or 'no summary'}."
            )
            candidates.append(
                self._candidate(
                    kind="failure_shield",
                    title=title,
                    content=content,
                    tags=["tool-failure", str(failure.get("name") or "tool")],
                    turn_id=turn_id,
                    user_input=user_input,
                    confidence="high" if failure.get("kind") else "medium",
                )
            )

        elapsed = _safe_float(metrics.get("elapsed_seconds"))
        tool_failure_count = _safe_int(metrics.get("tool_failures"))
        model_calls = _safe_int(metrics.get("model_calls"))
        if elapsed >= 300 or tool_failure_count >= 4 or model_calls >= 12:
            content = (
                f"Turn {turn_id} was expensive: elapsed={elapsed:.1f}s, "
                f"model_calls={model_calls}, tool_failures={tool_failure_count}, "
                f"status={result_status}. Future agents should inspect existing memory and recent evidence "
                "before repeating broad exploration."
            )
            candidates.append(
                self._candidate(
                    kind="cost_hotspot",
                    title="expensive turn hotspot",
                    content=content,
                    tags=["cost", "workflow"],
                    turn_id=turn_id,
                    user_input=user_input,
                    confidence="medium",
                )
            )

        for name, summary in successful_reusable_tools[:4]:
            if len(summary) < 8:
                continue
            candidates.append(
                self._candidate(
                    kind="successful_tool_flow",
                    title=f"successful {name}",
                    content=f"Turn {turn_id} successfully used {name}: {summary}",
                    tags=["tool-success", name],
                    turn_id=turn_id,
                    user_input=user_input,
                    confidence="medium",
                )
            )
        return candidates

    def _candidate(
        self,
        *,
        kind: str,
        title: str,
        content: str,
        tags: list[str],
        turn_id: str,
        user_input: str,
        confidence: str,
    ) -> TurnMemoryRecord:
        return TurnMemoryRecord(
            id=_turn_memory_id(kind, title, content),
            kind=kind,
            title=_compact_text(title, 160),
            content=_compact_text(content, 1600),
            tags=_normalize_tags(tags),
            source_turn_id=turn_id,
            source_user_input=_compact_text(user_input, 500),
            confidence=_normalize_confidence(confidence),
        )

    def _upsert(self, candidate: TurnMemoryRecord) -> TurnMemoryRecord:
        assert self._records is not None
        now = datetime.now(UTC).isoformat()
        for record in self._records:
            if record.id == candidate.id:
                record.content = candidate.content
                record.tags = candidate.tags
                record.source_turn_id = candidate.source_turn_id
                record.source_user_input = candidate.source_user_input
                record.confidence = candidate.confidence
                record.updated_at = now
                record.occurrences += 1
                return record
        self._records.append(candidate)
        return candidate

    def _append_raw(self, record: TurnMemoryRecord) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.raw_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")

    def _write_memory_artifacts(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        records = self._records or []
        by_kind: dict[str, list[TurnMemoryRecord]] = {}
        for record in records:
            by_kind.setdefault(record.kind, []).append(record)

        memory_lines = [
            "# MiniCodex2 Memory",
            "",
            "Evidence-backed memory consolidated from completed turns. Treat entries as hints, not commands.",
            "",
        ]
        for kind in sorted(by_kind):
            memory_lines.append(f"## {kind}")
            memory_lines.append("")
            for record in sorted(by_kind[kind], key=lambda item: (item.occurrences, item.updated_at), reverse=True)[:40]:
                tags = ", ".join(record.tags)
                memory_lines.append(f"### {record.title}")
                memory_lines.append(f"- id: `{record.id}`")
                memory_lines.append(f"- confidence: {record.confidence}; occurrences: {record.occurrences}; updated_at: {record.updated_at}")
                if tags:
                    memory_lines.append(f"- tags: {tags}")
                memory_lines.append(f"- source_turn: {record.source_turn_id}")
                memory_lines.append(f"- memory: {record.content}")
                memory_lines.append("")
        _atomic_write_text(self.memory_md_path, "\n".join(memory_lines).rstrip() + "\n")

        summary_lines = [
            "v1",
            "# Memory Summary",
            "",
            "Load this as navigation for durable memory. Search/read MEMORY.md or specific memory tools for details.",
        ]
        for record in sorted(records, key=lambda item: (item.occurrences, item.updated_at), reverse=True)[:16]:
            summary_lines.append(
                f"- {record.id}: kind={record.kind}; confidence={record.confidence}; "
                f"uses={record.occurrences}; {record.title} - {_compact_text(record.content, 220)}"
            )
        _atomic_write_text(self.summary_path, "\n".join(summary_lines).rstrip() + "\n")

    def _ensure_loaded(self) -> None:
        if self._records is not None:
            return
        self._records = []
        if not self.records_path.exists():
            return
        try:
            data = json.loads(self.records_path.read_text(encoding="utf-8"))
        except Exception:
            self._records = self._load_records_from_raw_log()
            return
        raw_records = data.get("records") if isinstance(data, dict) else data
        if not isinstance(raw_records, list):
            return
        for raw in raw_records:
            if isinstance(raw, dict):
                record = _turn_memory_from_dict(raw)
                if record is not None:
                    self._records.append(record)

    def _save(self) -> None:
        self._ensure_loaded()
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": datetime.now(UTC).isoformat(),
            "records": [record.to_dict() for record in self._records or []],
        }
        _atomic_write_text(self.records_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    def _load_records_from_raw_log(self) -> list[TurnMemoryRecord]:
        """Best-effort recovery when the compact JSON index was interrupted.

        `record_turn` runs on the main turn path while model memory extraction runs
        on a background thread. The raw jsonl log is append-only, so it gives us a
        safer recovery source if a previous process died midway through rewriting
        `turn_memories.json`.
        """

        if not self.raw_path.exists():
            return []
        by_id: dict[str, TurnMemoryRecord] = {}
        try:
            lines = self.raw_path.read_text(encoding="utf-8").splitlines()
        except Exception:
            return []
        for line in lines:
            try:
                raw = json.loads(line)
            except Exception:
                continue
            if not isinstance(raw, dict):
                continue
            record = _turn_memory_from_dict(raw)
            if record is None:
                continue
            existing = by_id.get(record.id)
            if existing is None or record.updated_at >= existing.updated_at:
                by_id[record.id] = record
        return sorted(
            by_id.values(),
            key=lambda item: (item.occurrences, item.updated_at),
            reverse=True,
        )[: self.max_records]


class ProjectGuidanceLoader:
    def __init__(self, *, max_files: int = 4, max_chars_per_file: int = 6000) -> None:
        self.max_files = max_files
        self.max_chars_per_file = max_chars_per_file

    def load(self, workspace_root: Path) -> list[ProjectGuidanceFile]:
        files = _find_named_files(workspace_root, {"AGENTS.md"}, max_files=self.max_files)
        loaded: list[ProjectGuidanceFile] = []
        for path in files:
            text = _read_text(path)
            truncated = len(text) > self.max_chars_per_file
            loaded.append(
                ProjectGuidanceFile(
                    path=path.relative_to(workspace_root).as_posix(),
                    content=text[: self.max_chars_per_file],
                    truncated=truncated,
                )
            )
        return loaded


class ProjectMemoryIndex:
    def __init__(self, *, max_documents: int = 20, max_excerpt_chars: int = 700) -> None:
        self.max_documents = max_documents
        self.max_excerpt_chars = max_excerpt_chars

    def build(self, workspace_root: Path) -> list[ProjectMemoryDocument]:
        documents: list[Path] = []
        for path in _iter_files(workspace_root):
            if path.name in {"AGENTS.md", "README.md"}:
                documents.append(path)
                continue
            if path.suffix.lower() in {".md", ".txt"} and "docs" in {
                part.lower() for part in path.relative_to(workspace_root).parts[:-1]
            }:
                documents.append(path)
        documents = sorted(documents, key=lambda item: _document_sort_key(workspace_root, item))
        indexed: list[ProjectMemoryDocument] = []
        for path in documents[: self.max_documents]:
            text = _read_text(path)
            headings = _extract_markdown_headings(text, limit=8)
            indexed.append(
                ProjectMemoryDocument(
                    path=path.relative_to(workspace_root).as_posix(),
                    title=_document_title(path, text, headings),
                    headings=headings,
                    excerpt=_compact_text(_strip_markdown_noise(text), self.max_excerpt_chars),
                )
            )
        return indexed


class WorkPlanDocumentParser:
    """Extract explicit checklist/bullet items into provisional WorkPlan steps.

    This parser intentionally avoids semantic keyword classification. If a document is a
    PRD rather than an explicit task list, the model should read it and call
    set_work_plan with domain-aware steps.
    """

    def parse(self, text: str, *, max_steps: int = 20) -> list[dict[str, object]]:
        steps: list[dict[str, object]] = []
        seen_titles: set[str] = set()
        lines = text.splitlines()
        for index, line in enumerate(lines):
            candidate = _plan_candidate_from_line(line, next_line=_next_nonempty_line(lines, index + 1))
            if candidate is None:
                continue
            title = candidate["title"]
            if title in seen_titles:
                continue
            seen_titles.add(title)
            steps.append(candidate)
            if len(steps) >= max_steps:
                break
        return steps


def load_project_context(workspace_root: Path) -> dict[str, object]:
    return {
        "project_guidance": [item.to_dict() for item in ProjectGuidanceLoader().load(workspace_root)],
        "project_memory_index": [item.to_dict() for item in ProjectMemoryIndex().build(workspace_root)],
    }


def _find_named_files(workspace_root: Path, names: set[str], *, max_files: int) -> list[Path]:
    found = [path for path in _iter_files(workspace_root) if path.name in names]
    return sorted(found, key=lambda item: _document_sort_key(workspace_root, item))[:max_files]


def _iter_files(workspace_root: Path) -> list[Path]:
    files: list[Path] = []
    if not workspace_root.exists():
        return files
    for current_root, dirnames, filenames in os.walk(workspace_root):
        current = Path(current_root)
        try:
            relative_parts = current.relative_to(workspace_root).parts
        except ValueError:
            continue
        if any(part in IGNORED_DIRS for part in relative_parts):
            dirnames[:] = []
            continue
        dirnames[:] = [name for name in dirnames if name not in IGNORED_DIRS]
        for filename in filenames:
            files.append(current / filename)
    return files


def _document_sort_key(workspace_root: Path, path: Path) -> tuple[int, int, str]:
    relative = path.relative_to(workspace_root).as_posix()
    depth = len(path.relative_to(workspace_root).parts)
    priority = 0 if path.name == "AGENTS.md" else 1 if path.name == "README.md" else 2
    return priority, depth, relative.lower()


def _read_text(path: Path, *, max_bytes: int = 200_000) -> str:
    data = path.read_bytes()
    if len(data) > max_bytes:
        data = data[:max_bytes]
    return data.decode("utf-8", errors="replace")


def _extract_markdown_headings(text: str, *, limit: int) -> list[str]:
    headings: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$", line)
        if not match:
            continue
        title = _compact_text(match.group(2).strip(" #"), 120)
        if title:
            headings.append(title)
        if len(headings) >= limit:
            break
    return headings


def _document_title(path: Path, text: str, headings: list[str]) -> str:
    if headings:
        return headings[0]
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return _compact_text(stripped.strip("# "), 120)
    return path.name


def _strip_markdown_noise(text: str) -> str:
    lines: list[str] = []
    in_fence = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if not stripped:
            continue
        lines.append(stripped)
        if len(lines) >= 20:
            break
    return " ".join(lines)


def _plan_candidate_from_line(line: str, *, next_line: str = "") -> dict[str, object] | None:
    stripped = line.strip()
    if not stripped:
        return None
    bullet_match = re.match(r"^(?:[-*+]|\d+[.)]|\[[ xX]\])\s+(.+)$", stripped)
    if not bullet_match:
        return None
    title = _compact_text(_clean_step_title(bullet_match.group(1)), 180)
    if not title or _looks_like_non_step(title, next_line=next_line):
        return None
    acceptance, verification_hint = _default_step_acceptance()
    return {
        "id": _slugify(title),
        "title": title,
        "status": _status_from_line(stripped),
        "acceptance": acceptance,
        "verification_hint": verification_hint,
    }


def _clean_step_title(title: str) -> str:
    title = re.sub(r"^\[[ xX]\]\s*", "", title)
    title = re.sub(r"^\*\*(.+?)\*\*$", r"\1", title)
    title = title.strip(" -:")
    return " ".join(title.split())


def _status_from_line(line: str) -> str:
    lowered = line.lower()
    is_chinese_done = "已完成" in line or (
        "完成" in line and "未完成" not in line and "待完成" not in line and "还没完成" not in line
    )
    if "[x]" in lowered or "done" in lowered or "completed" in lowered or is_chinese_done:
        return "done"
    if "blocked" in lowered or "阻塞" in line:
        return "blocked"
    if "in_progress" in lowered or "in progress" in lowered or "进行中" in line:
        return "in_progress"
    return "pending"


def _slugify(text: str) -> str:
    ascii_text = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    if ascii_text:
        return ascii_text[:60]
    encoded = "-".join(f"{ord(char):x}" for char in text[:12])
    return f"step-{encoded}"[:60]


def _compact_text(text: str, limit: int) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def _looks_like_non_step(title: str, *, next_line: str = "") -> bool:
    if len(title) < 3:
        return True
    if re.fullmatch(r"[-:：| ]+", title):
        return True
    return _looks_like_definition_line(title, next_line)


def _looks_like_definition_line(title: str, next_line: str) -> bool:
    delimiter_positions = [pos for pos in (title.find(":"), title.find("：")) if pos >= 0]
    if not delimiter_positions:
        return False
    delimiter = min(delimiter_positions)
    prefix = title[:delimiter].strip()
    suffix = title[delimiter + 1 :].strip()
    if 0 < len(prefix) <= 18 and len(suffix) >= 8:
        return True
    compact_next = " ".join(next_line.strip().split())
    return 0 < len(prefix) <= 18 and len(compact_next) >= 8


def _default_step_acceptance() -> tuple[str, str]:
    return (
        "Relevant implementation is complete and verified with concrete evidence matching this step.",
        "Run the narrowest project-specific test, build, command, API check, UI smoke, or integration check that proves this step; choose the method from project evidence.",
    )


def _next_nonempty_line(lines: list[str], start: int) -> str:
    for line in lines[start:]:
        if line.strip():
            return line
    return ""


def re_split_words(text: str) -> list[str]:
    return [item for item in re.split(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", text) if item]


def _normalize_tags(tags: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        if not isinstance(tag, str):
            continue
        compact = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "-", tag.strip().lower()).strip("-")
        if not compact or compact in seen:
            continue
        normalized.append(compact[:40])
        seen.add(compact)
        if len(normalized) >= 12:
            break
    return normalized


def _normalize_confidence(confidence: str) -> str:
    value = (confidence or "medium").strip().lower()
    if value not in {"low", "medium", "high"}:
        return "medium"
    return value


def _normalize_relative_text(value: str, limit: int) -> str:
    compact = " ".join(str(value or "").strip().split())
    compact = compact.replace("\\", "/")
    return _compact_text(compact, limit)


def _normalize_ports(ports: list[int]) -> list[int]:
    normalized: list[int] = []
    seen: set[int] = set()
    for raw in ports:
        try:
            port = int(raw)
        except (TypeError, ValueError):
            continue
        if port < 1 or port > 65535 or port in seen:
            continue
        normalized.append(port)
        seen.add(port)
        if len(normalized) >= 8:
            break
    return normalized


def _normalize_steps(steps: list[str]) -> list[str]:
    normalized: list[str] = []
    for step in steps:
        if not isinstance(step, str):
            continue
        compact = _compact_text(" ".join(step.strip().split()), 260)
        if not compact or compact in normalized:
            continue
        normalized.append(compact)
        if len(normalized) >= 20:
            break
    return normalized


def _normalize_workflow_fields(
    *,
    title: str,
    cwd: str,
    command: str,
    purpose: str,
    ready_check: str,
    steps: list[str],
    ports: list[int],
    tags: list[str],
    scope: str,
    confidence: str,
    source: str,
    notes: str,
) -> dict[str, object]:
    normalized_title = _compact_text(" ".join(title.strip().split()), 160)
    normalized_cwd = _normalize_relative_text(cwd or ".", 180) or "."
    normalized_command = _compact_text(" ".join(command.strip().split()), 600)
    normalized_purpose = _compact_text(" ".join(purpose.strip().split()) or "workflow", 80)
    normalized_ready_check = _compact_text(" ".join(ready_check.strip().split()), 300)
    normalized_scope = _compact_text(" ".join(scope.strip().split()) or "project", 80)
    normalized_source = _compact_text(" ".join(source.strip().split()) or "model", 160)
    normalized_notes = _compact_text(" ".join(notes.strip().split()), 1200)
    if not normalized_title:
        normalized_title = _compact_text(normalized_command or normalized_ready_check or normalized_purpose, 80)
    if not normalized_title:
        normalized_title = "workflow"
    return {
        "title": normalized_title,
        "cwd": normalized_cwd,
        "command": normalized_command,
        "purpose": normalized_purpose,
        "ready_check": normalized_ready_check,
        "steps": _normalize_steps(steps),
        "ports": _normalize_ports(ports),
        "tags": _normalize_tags(tags),
        "scope": normalized_scope,
        "confidence": _normalize_confidence(confidence),
        "source": normalized_source,
        "notes": normalized_notes,
    }


def _memory_fact_from_dict(data: dict[str, object]) -> ProjectMemoryFact | None:
    memory_id = data.get("id")
    title = data.get("title")
    content = data.get("content")
    if not isinstance(memory_id, str) or not isinstance(title, str) or not isinstance(content, str):
        return None
    tags = data.get("tags")
    return ProjectMemoryFact(
        id=memory_id[:80],
        title=_compact_text(title, 160),
        content=_compact_text(content, 4000),
        tags=_normalize_tags(tags if isinstance(tags, list) else []),
        scope=_compact_text(str(data.get("scope") or "project"), 80),
        confidence=_normalize_confidence(str(data.get("confidence") or "medium")),
        source=_compact_text(str(data.get("source") or "model"), 160),
        stale=bool(data.get("stale")),
        created_at=str(data.get("created_at") or datetime.now(UTC).isoformat()),
        updated_at=str(data.get("updated_at") or datetime.now(UTC).isoformat()),
        last_used_at=str(data.get("last_used_at") or ""),
        use_count=int(data.get("use_count") or 0) if isinstance(data.get("use_count") or 0, int) else 0,
    )


def _workflow_from_dict(data: dict[str, object]) -> WorkflowMemory | None:
    workflow_id = data.get("id")
    title = data.get("title")
    if not isinstance(workflow_id, str) or not isinstance(title, str):
        return None
    ports = data.get("ports")
    steps = data.get("steps")
    tags = data.get("tags")
    success_count = data.get("success_count")
    failure_count = data.get("failure_count")
    return WorkflowMemory(
        id=workflow_id[:80],
        title=_compact_text(title, 160) or "workflow",
        cwd=_normalize_relative_text(str(data.get("cwd") or "."), 180) or ".",
        command=_compact_text(str(data.get("command") or ""), 600),
        purpose=_compact_text(str(data.get("purpose") or "workflow"), 80),
        ready_check=_compact_text(str(data.get("ready_check") or ""), 300),
        steps=_normalize_steps(steps if isinstance(steps, list) else []),
        ports=_normalize_ports(ports if isinstance(ports, list) else []),
        tags=_normalize_tags(tags if isinstance(tags, list) else []),
        scope=_compact_text(str(data.get("scope") or "project"), 80),
        confidence=_normalize_confidence(str(data.get("confidence") or "medium")),
        source=_compact_text(str(data.get("source") or "model"), 160),
        notes=_compact_text(str(data.get("notes") or ""), 1200),
        stale=bool(data.get("stale")),
        success_count=success_count if isinstance(success_count, int) else 0,
        failure_count=failure_count if isinstance(failure_count, int) else 0,
        created_at=str(data.get("created_at") or datetime.now(UTC).isoformat()),
        updated_at=str(data.get("updated_at") or datetime.now(UTC).isoformat()),
        last_used_at=str(data.get("last_used_at") or ""),
        last_verified_at=str(data.get("last_verified_at") or ""),
    )


def _turn_memory_from_dict(data: dict[str, object]) -> TurnMemoryRecord | None:
    memory_id = data.get("id")
    kind = data.get("kind")
    title = data.get("title")
    content = data.get("content")
    if not all(isinstance(value, str) and value for value in (memory_id, kind, title, content)):
        return None
    tags = data.get("tags")
    occurrences = data.get("occurrences")
    return TurnMemoryRecord(
        id=str(memory_id)[:80],
        kind=_compact_text(str(kind), 80),
        title=_compact_text(str(title), 160),
        content=_compact_text(str(content), 1600),
        scope=_compact_text(str(data.get("scope") or "project"), 80),
        tags=_normalize_tags(tags if isinstance(tags, list) else []),
        source_turn_id=_compact_text(str(data.get("source_turn_id") or ""), 80),
        source_user_input=_compact_text(str(data.get("source_user_input") or ""), 500),
        confidence=_normalize_confidence(str(data.get("confidence") or "medium")),
        created_at=str(data.get("created_at") or datetime.now(UTC).isoformat()),
        updated_at=str(data.get("updated_at") or datetime.now(UTC).isoformat()),
        occurrences=occurrences if isinstance(occurrences, int) and occurrences > 0 else 1,
    )


def _memory_recency_key(fact: ProjectMemoryFact) -> str:
    return fact.updated_at or fact.created_at


def _memory_hot_key(fact: ProjectMemoryFact) -> tuple[int, int, str]:
    confidence_rank = {"high": 3, "medium": 2, "low": 1}.get(fact.confidence, 0)
    return (confidence_rank, min(fact.use_count, 20), fact.updated_at or fact.created_at)


def _workflow_hot_key(workflow: WorkflowMemory) -> tuple[int, int, int, str]:
    confidence_rank = {"high": 3, "medium": 2, "low": 1}.get(workflow.confidence, 0)
    return (
        confidence_rank,
        min(workflow.success_count, 20),
        -min(workflow.failure_count, 20),
        workflow.updated_at or workflow.created_at,
    )


def _turn_memory_id(kind: str, title: str, content: str) -> str:
    key = f"{kind}\n{title.casefold()}\n{content[:260].casefold()}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:36] or "memory"
    return f"tm-{digest}-{slug}"[:80]


def _turn_memory_importance_rank(record: TurnMemoryRecord) -> int:
    kind_rank = {
        "pending_requirement": 50,
        "product_decision": 45,
        "preference": 40,
        "workflow": 35,
        "tooling_quirk": 30,
        "failure_shield": 25,
        "lesson": 20,
        "repo_fact": 15,
    }.get(record.kind, 0)
    confidence_rank = {"high": 3, "medium": 2, "low": 1}.get(record.confidence, 0)
    return kind_rank + confidence_rank


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _safe_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _safe_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
