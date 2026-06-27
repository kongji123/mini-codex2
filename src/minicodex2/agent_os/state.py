from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime


@dataclass(slots=True)
class FailureReproductionTarget:
    kind: str
    entrypoint: str
    source: str
    method: str | None = None
    status: str = "reported"
    evidence: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(slots=True)
class WorkPlanItem:
    id: str
    title: str
    status: str = "pending"
    acceptance: str = ""
    evidence: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    blocker: str = ""
    verification_hint: str = ""
    required: bool = True
    source_document: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class EvidenceRecord:
    id: str
    kind: str
    source_tool: str
    summary: str
    detail: str = ""
    command: str = ""
    url: str = ""
    path: str = ""
    status: str = "passed"
    exit_code: int | None = None
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class WorkPlanSource:
    path: str
    content_hash: str
    mtime: float | None = None
    synced_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class RuntimeResource:
    id: str
    kind: str
    source_tool: str
    command: str = ""
    cwd: str = ""
    url: str = ""
    method: str = ""
    port: int | None = None
    pid: int | None = None
    ready: bool | None = None
    exit_code: int | None = None
    status_code: int | None = None
    log_path: str = ""
    status: str = ""
    summary: str = ""
    data: dict[str, object] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class EngineeringFact:
    id: str
    type: str
    source: str
    summary: str
    data: dict[str, object] = field(default_factory=dict)
    confidence: str = "medium"
    stale: bool = False
    observed_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class LessonRecord:
    id: str
    summary: str
    trigger: str = ""
    scope: str = "general"
    evidence: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    use_count: int = 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class WorkspaceEntry:
    path: str
    kind: str
    source_tool: str
    size: int | None = None
    observed_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class ReadRecord:
    path: str
    source_tool: str
    size: int | None = None
    excerpt: str = ""
    content_hash: str = ""
    mtime: float | None = None
    read_count: int = 1
    truncated: bool = False
    line_count: int | None = None
    line_ranges: list[list[int]] = field(default_factory=list)
    full_read_count: int = 0
    last_start_line: int | None = None
    last_end_line: int | None = None
    observed_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class ChangeRecord:
    path: str
    source_tool: str
    action: str
    summary: str = ""
    observed_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class SessionState:
    changed_files: list[str] = field(default_factory=list)
    verified_files: list[str] = field(default_factory=list)
    previous_failure_signature: str | None = None
    repair_round: int = 0
    failure_repair_counts: dict[str, int] = field(default_factory=dict)
    did_write: bool = False
    result_state: str = "ready"
    collaboration_advised: bool = False
    design_docs_exist: bool = False
    active_goal: str | None = None
    goal_status: str = "none"
    goal_token_budget: int | None = None
    acceptance_evidence: list[str] = field(default_factory=list)
    evidence_records: list[EvidenceRecord] = field(default_factory=list)
    _next_evidence_index: int = 1
    work_plan: list[WorkPlanItem] = field(default_factory=list)
    work_plan_sources: list[WorkPlanSource] = field(default_factory=list)
    current_step_id: str | None = None
    archived_goals: list[dict[str, object]] = field(default_factory=list)
    failure_targets: list[FailureReproductionTarget] = field(default_factory=list)
    runtime_resources: list[RuntimeResource] = field(default_factory=list)
    _next_runtime_resource_index: int = 1
    engineering_facts: list[EngineeringFact] = field(default_factory=list)
    _next_engineering_fact_index: int = 1
    lessons: list[LessonRecord] = field(default_factory=list)
    _next_lesson_index: int = 1
    workspace_entries: list[WorkspaceEntry] = field(default_factory=list)
    read_records: list[ReadRecord] = field(default_factory=list)
    change_records: list[ChangeRecord] = field(default_factory=list)

    def all_changed_files(self) -> list[str]:
        files: list[str] = []
        seen: set[str] = set()
        for path in [*self.verified_files, *self.changed_files]:
            if path not in seen:
                files.append(path)
                seen.add(path)
        return files

    def repair_count(self, signature: str) -> int:
        return self.failure_repair_counts.get(signature, 0)

    def record_repair_attempt(self, signature: str) -> int:
        count = self.failure_repair_counts.get(signature, 0) + 1
        self.failure_repair_counts[signature] = count
        self.repair_round = count
        self.previous_failure_signature = signature
        return count

    def begin_next_goal_step(self) -> None:
        for path in self.changed_files:
            if path not in self.verified_files:
                self.verified_files.append(path)
        self.changed_files = []
        self.did_write = False

    def goal_snapshot(self, *, include_archived: bool = True) -> dict[str, object]:
        snapshot: dict[str, object] = {
            "objective": self.active_goal,
            "status": self.goal_status,
            "token_budget": self.goal_token_budget,
            "acceptance_evidence": self.acceptance_evidence,
            "evidence_records": [item.to_dict() for item in self.evidence_records],
            "work_plan": [item.to_dict() for item in self.work_plan],
            "work_plan_sources": [item.to_dict() for item in self.work_plan_sources],
            "current_step_id": self.current_step_id,
            "runtime_resources": [item.to_dict() for item in self.runtime_resources],
            "engineering_facts": [item.to_dict() for item in self.engineering_facts],
            "lessons": [item.to_dict() for item in self.lessons],
            "workspace_entries": [item.to_dict() for item in self.workspace_entries],
            "read_records": [item.to_dict() for item in self.read_records],
            "change_records": [item.to_dict() for item in self.change_records],
        }
        if include_archived:
            snapshot["archived_goals"] = self.archived_goals
        return snapshot

    def archive_current_goal(self, reason: str) -> None:
        if not self.active_goal:
            return
        archived = self.goal_snapshot(include_archived=False)
        archived["archived_at"] = datetime.now(UTC).isoformat()
        archived["archive_reason"] = reason[:120]
        key = (archived.get("objective"), archived.get("status"))
        self.archived_goals = [
            item
            for item in self.archived_goals
            if (item.get("objective"), item.get("status")) != key
        ]
        self.archived_goals.append(archived)
        self.archived_goals = self.archived_goals[-20:]

    def load_goal_snapshot(self, data: dict[str, object]) -> None:
        self.active_goal = _optional_string(data.get("objective") or data.get("active_goal"))
        self.goal_status = _optional_string(data.get("status") or data.get("goal_status")) or "none"
        token_budget = data.get("token_budget") or data.get("goal_token_budget")
        self.goal_token_budget = token_budget if isinstance(token_budget, int) else None
        self.acceptance_evidence = [
            item for item in data.get("acceptance_evidence", []) if isinstance(item, str)
        ] if isinstance(data.get("acceptance_evidence"), list) else []
        self.acceptance_evidence = _drop_runtime_resource_acceptance(self.acceptance_evidence)
        self.evidence_records = []
        dropped_evidence_ids: set[str] = set()
        raw_evidence_records = data.get("evidence_records")
        if isinstance(raw_evidence_records, list):
            for raw_record in raw_evidence_records:
                if isinstance(raw_record, dict):
                    record = _evidence_record_from_dict(raw_record)
                    if record is not None:
                        if _is_runtime_resource_acceptance_record(record):
                            dropped_evidence_ids.add(record.id)
                        else:
                            self.evidence_records.append(record)
        self._next_evidence_index = _next_evidence_index(self.evidence_records)
        self.runtime_resources = []
        raw_runtime_resources = data.get("runtime_resources")
        if isinstance(raw_runtime_resources, list):
            for raw_resource in raw_runtime_resources:
                if isinstance(raw_resource, dict):
                    resource = _runtime_resource_from_dict(raw_resource)
                    if resource is not None:
                        self.runtime_resources.append(resource)
        self._next_runtime_resource_index = _next_runtime_resource_index(self.runtime_resources)
        self.engineering_facts = []
        raw_engineering_facts = data.get("engineering_facts")
        if isinstance(raw_engineering_facts, list):
            for raw_fact in raw_engineering_facts:
                if isinstance(raw_fact, dict):
                    fact = _engineering_fact_from_dict(raw_fact)
                    if fact is not None:
                        self.engineering_facts.append(fact)
        self._next_engineering_fact_index = _next_engineering_fact_index(self.engineering_facts)
        self.lessons = []
        raw_lessons = data.get("lessons")
        if isinstance(raw_lessons, list):
            for raw_lesson in raw_lessons:
                if isinstance(raw_lesson, dict):
                    lesson = _lesson_record_from_dict(raw_lesson)
                    if lesson is not None:
                        self.lessons.append(lesson)
        self._next_lesson_index = _next_lesson_index(self.lessons)
        self.workspace_entries = []
        raw_workspace_entries = data.get("workspace_entries")
        if isinstance(raw_workspace_entries, list):
            for raw_entry in raw_workspace_entries:
                if isinstance(raw_entry, dict):
                    entry = _workspace_entry_from_dict(raw_entry)
                    if entry is not None:
                        self.workspace_entries.append(entry)
        self.read_records = []
        raw_read_records = data.get("read_records")
        if isinstance(raw_read_records, list):
            for raw_record in raw_read_records:
                if isinstance(raw_record, dict):
                    record = _read_record_from_dict(raw_record)
                    if record is not None:
                        self.read_records.append(record)
        self.change_records = []
        raw_change_records = data.get("change_records")
        if isinstance(raw_change_records, list):
            for raw_record in raw_change_records:
                if isinstance(raw_record, dict):
                    record = _change_record_from_dict(raw_record)
                    if record is not None:
                        self.change_records.append(record)
        self.work_plan = []
        raw_plan = data.get("work_plan")
        if isinstance(raw_plan, list):
            for raw_item in raw_plan:
                if isinstance(raw_item, dict):
                    item = _work_plan_item_from_dict(raw_item)
                    if item is not None:
                        if dropped_evidence_ids:
                            item.evidence_ids = [
                                evidence_id
                                for evidence_id in item.evidence_ids
                                if evidence_id not in dropped_evidence_ids
                            ]
                        item.evidence = _drop_runtime_resource_acceptance(item.evidence)
                        self.work_plan.append(item)
        self.work_plan_sources = []
        raw_plan_sources = data.get("work_plan_sources")
        if isinstance(raw_plan_sources, list):
            for raw_source in raw_plan_sources:
                if isinstance(raw_source, dict):
                    source = _work_plan_source_from_dict(raw_source)
                    if source is not None:
                        self.work_plan_sources.append(source)
        self.archived_goals = []
        raw_archived_goals = data.get("archived_goals")
        if isinstance(raw_archived_goals, list):
            for raw_goal in raw_archived_goals[-20:]:
                if isinstance(raw_goal, dict):
                    self.archived_goals.append(dict(raw_goal))
        current_step_id = _optional_string(data.get("current_step_id"))
        self.current_step_id = current_step_id if self.find_work_step(current_step_id) else None

    def add_acceptance_evidence(
        self,
        evidence: str,
        *,
        evidence_id: str | None = None,
    ) -> None:
        normalized = " ".join(evidence.strip().split())
        if normalized and normalized not in self.acceptance_evidence:
            self.acceptance_evidence.append(normalized)
        current = self.current_step()
        if current and normalized and normalized not in current.evidence:
            current.evidence.append(normalized)
        if current and evidence_id and evidence_id not in current.evidence_ids:
            current.evidence_ids.append(evidence_id)

    def add_evidence_record(
        self,
        *,
        kind: str,
        source_tool: str,
        summary: str,
        detail: str = "",
        command: str = "",
        url: str = "",
        path: str = "",
        status: str = "passed",
        exit_code: int | None = None,
    ) -> EvidenceRecord:
        normalized_summary = " ".join(summary.strip().split())
        for record in self.evidence_records:
            if record.summary == normalized_summary and record.source_tool == source_tool:
                return record
        record = EvidenceRecord(
            id=f"ev-{self._next_evidence_index}",
            kind=kind[:80],
            source_tool=source_tool[:80],
            summary=normalized_summary[:500],
            detail=detail[:2000],
            command=command[:500],
            url=url[:500],
            path=path[:500],
            status=status[:80],
            exit_code=exit_code,
        )
        self._next_evidence_index += 1
        self.evidence_records.append(record)
        return record

    def remember_work_plan_source(
        self,
        *,
        path: str,
        content: str,
        mtime: float | None = None,
    ) -> WorkPlanSource:
        normalized_path = _optional_string(path) or ""
        content_hash = hashlib.sha1(content.encode("utf-8", errors="replace")).hexdigest()
        for source in self.work_plan_sources:
            if source.path == normalized_path:
                source.content_hash = content_hash
                source.mtime = mtime
                source.synced_at = datetime.now(UTC).isoformat()
                return source
        source = WorkPlanSource(
            path=normalized_path[:500],
            content_hash=content_hash,
            mtime=mtime,
        )
        self.work_plan_sources.append(source)
        return source

    def add_runtime_resource(
        self,
        *,
        kind: str,
        source_tool: str,
        command: str = "",
        cwd: str = "",
        url: str = "",
        method: str = "",
        port: int | None = None,
        pid: int | None = None,
        ready: bool | None = None,
        exit_code: int | None = None,
        status_code: int | None = None,
        log_path: str = "",
        status: str = "",
        summary: str = "",
        data: dict[str, object] | None = None,
    ) -> RuntimeResource:
        normalized_kind = _optional_string(kind) or "runtime_observation"
        normalized_source = _optional_string(source_tool) or "runtime"
        normalized_command = _optional_string(command) or ""
        normalized_cwd = _optional_string(cwd) or ""
        normalized_url = _optional_string(url) or ""
        normalized_method = _optional_string(method) or ""
        for resource in self.runtime_resources:
            if (
                resource.kind == normalized_kind
                and resource.source_tool == normalized_source
                and resource.command == normalized_command
                and resource.cwd == normalized_cwd
                and resource.url == normalized_url
                and resource.port == port
            ):
                resource.method = normalized_method[:40]
                resource.pid = pid
                resource.ready = ready
                resource.exit_code = exit_code
                resource.status_code = status_code
                resource.log_path = log_path[:500]
                resource.status = status[:80]
                resource.summary = (" ".join(summary.strip().split()) or resource.summary)[:500]
                resource.data = dict(data or resource.data)
                resource.created_at = datetime.now(UTC).isoformat()
                return resource
        resource = RuntimeResource(
            id=f"rr-{self._next_runtime_resource_index}",
            kind=normalized_kind[:80],
            source_tool=normalized_source[:80],
            command=normalized_command[:500],
            cwd=normalized_cwd[:500],
            url=normalized_url[:500],
            method=normalized_method[:40],
            port=port,
            pid=pid,
            ready=ready,
            exit_code=exit_code,
            status_code=status_code,
            log_path=log_path,
            status=status,
            summary=(" ".join(summary.strip().split()))[:500],
            data=dict(data or {}),
        )
        self._next_runtime_resource_index += 1
        self.runtime_resources.append(resource)
        self.runtime_resources = self.runtime_resources[-50:]
        return resource

    def add_engineering_fact(
        self,
        *,
        type: str,
        source: str,
        summary: str,
        data: dict[str, object] | None = None,
        confidence: str = "medium",
        stale: bool = False,
    ) -> EngineeringFact:
        normalized_type = " ".join(type.strip().split())
        normalized_source = " ".join(source.strip().split())
        normalized_summary = " ".join(summary.strip().split())
        if not normalized_type or not normalized_summary:
            normalized_type = normalized_type or "fact"
            normalized_summary = normalized_summary or normalized_source or normalized_type
        for fact in self.engineering_facts:
            if (
                fact.type == normalized_type
                and fact.source == normalized_source
                and fact.summary == normalized_summary
            ):
                fact.data = dict(data or fact.data)
                fact.confidence = confidence[:40]
                fact.stale = stale
                fact.observed_at = datetime.now(UTC).isoformat()
                return fact
        fact = EngineeringFact(
            id=f"fact-{self._next_engineering_fact_index}",
            type=normalized_type[:80],
            source=normalized_source[:500],
            summary=normalized_summary[:500],
            data=dict(data or {}),
            confidence=confidence[:40],
            stale=stale,
        )
        self._next_engineering_fact_index += 1
        self.engineering_facts.append(fact)
        self.engineering_facts = self.engineering_facts[-100:]
        return fact

    def add_lesson(
        self,
        *,
        summary: str,
        trigger: str = "",
        scope: str = "general",
        evidence: str = "",
    ) -> LessonRecord:
        normalized_summary = " ".join(summary.strip().split())
        if not normalized_summary:
            normalized_summary = "Keep future reasoning grounded in evidence."
        normalized_trigger = " ".join(trigger.strip().split())
        normalized_scope = " ".join(scope.strip().split()) or "general"
        normalized_evidence = " ".join(evidence.strip().split())
        for lesson in self.lessons:
            if lesson.summary == normalized_summary and lesson.scope == normalized_scope:
                lesson.trigger = normalized_trigger[:300] or lesson.trigger
                lesson.evidence = normalized_evidence[:500] or lesson.evidence
                lesson.use_count += 1
                return lesson
        lesson = LessonRecord(
            id=f"lesson-{self._next_lesson_index}",
            summary=normalized_summary[:500],
            trigger=normalized_trigger[:300],
            scope=normalized_scope[:80],
            evidence=normalized_evidence[:500],
        )
        self._next_lesson_index += 1
        self.lessons.append(lesson)
        self.lessons = self.lessons[-80:]
        return lesson

    def relevant_lessons(self, query: str = "", limit: int = 5) -> list[LessonRecord]:
        limit = max(1, min(int(limit or 5), 20))
        query_terms = {
            term.lower()
            for term in re_split_words(query)
            if len(term) >= 3
        }
        if not query_terms:
            return self.lessons[-limit:]

        def score(lesson: LessonRecord) -> tuple[int, str]:
            text_terms = {
                term.lower()
                for term in re_split_words(
                    f"{lesson.summary} {lesson.trigger} {lesson.scope} {lesson.evidence}"
                )
                if len(term) >= 3
            }
            return (len(query_terms & text_terms), lesson.created_at)

        ranked = sorted(self.lessons, key=score, reverse=True)
        return [lesson for lesson in ranked if score(lesson)[0] > 0][:limit] or self.lessons[-limit:]

    def observe_workspace_entry(
        self,
        *,
        path: str,
        kind: str,
        source_tool: str,
        size: int | None = None,
    ) -> WorkspaceEntry:
        normalized_path = _normalize_path(path)
        normalized_kind = _optional_string(kind) or "unknown"
        for entry in self.workspace_entries:
            if entry.path == normalized_path:
                entry.kind = normalized_kind[:40]
                entry.source_tool = source_tool[:80]
                entry.size = size
                entry.observed_at = datetime.now(UTC).isoformat()
                return entry
        entry = WorkspaceEntry(
            path=normalized_path[:500],
            kind=normalized_kind[:40],
            source_tool=source_tool[:80],
            size=size,
        )
        self.workspace_entries.append(entry)
        self.workspace_entries = self.workspace_entries[-300:]
        return entry

    def record_read(
        self,
        *,
        path: str,
        source_tool: str,
        size: int | None = None,
        excerpt: str = "",
        content_hash: str = "",
        mtime: float | None = None,
        truncated: bool = False,
        line_count: int | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> ReadRecord:
        normalized_path = _normalize_path(path)
        compact_excerpt = " ".join(excerpt.strip().split())[:500]
        normalized_ranges = _merge_line_ranges([], start_line, end_line)
        full_read = _is_full_line_read(normalized_ranges, line_count, truncated)
        for record in self.read_records:
            if record.path == normalized_path:
                normalized_hash = content_hash[:80]
                hash_changed = bool(
                    normalized_hash
                    and record.content_hash
                    and normalized_hash != record.content_hash
                )
                if hash_changed:
                    record.line_ranges = []
                    record.line_count = None
                    record.full_read_count = 0
                    record.last_start_line = None
                    record.last_end_line = None
                had_full_coverage = _has_full_line_coverage(record.line_ranges, record.line_count)
                record.source_tool = source_tool[:80]
                record.size = size
                record.excerpt = compact_excerpt
                record.content_hash = normalized_hash
                record.mtime = mtime
                record.read_count += 1
                record.truncated = truncated
                if line_count is not None:
                    record.line_count = line_count
                if start_line is not None and end_line is not None:
                    record.last_start_line = start_line
                    record.last_end_line = end_line
                record.line_ranges = _merge_line_ranges(record.line_ranges, start_line, end_line)
                has_full_coverage = _has_full_line_coverage(record.line_ranges, record.line_count)
                if full_read or (has_full_coverage and not had_full_coverage):
                    record.full_read_count += 1
                record.observed_at = datetime.now(UTC).isoformat()
                return record
        record = ReadRecord(
            path=normalized_path[:500],
            source_tool=source_tool[:80],
            size=size,
            excerpt=compact_excerpt,
            content_hash=content_hash[:80],
            mtime=mtime,
            truncated=truncated,
            line_count=line_count,
            line_ranges=normalized_ranges,
            full_read_count=1 if full_read else 0,
            last_start_line=start_line,
            last_end_line=end_line,
        )
        self.read_records.append(record)
        self.read_records = self.read_records[-100:]
        return record

    def record_change(
        self,
        *,
        path: str,
        source_tool: str,
        action: str,
        summary: str = "",
    ) -> ChangeRecord:
        record = ChangeRecord(
            path=_normalize_path(path)[:500],
            source_tool=source_tool[:80],
            action=(_optional_string(action) or "changed")[:80],
            summary=" ".join(summary.strip().split())[:500],
        )
        self.change_records.append(record)
        self.change_records = self.change_records[-100:]
        return record

    def find_evidence_record(self, evidence_id: str | None) -> EvidenceRecord | None:
        if not evidence_id:
            return None
        for record in self.evidence_records:
            if record.id == evidence_id:
                return record
        return None

    def current_step(self) -> WorkPlanItem | None:
        return self.find_work_step(self.current_step_id)

    def find_work_step(self, item_id: str | None) -> WorkPlanItem | None:
        if not item_id:
            return None
        for item in self.work_plan:
            if item.id == item_id:
                return item
        return None

    def set_work_plan(self, items: list[WorkPlanItem]) -> None:
        self.work_plan = items
        if not self.find_work_step(self.current_step_id):
            self.current_step_id = next((item.id for item in items if item.status == "in_progress"), None)
        if not self.current_step_id:
            self.current_step_id = next((item.id for item in items if item.status == "pending"), None)

    def set_current_step(self, item_id: str | None) -> bool:
        if item_id is None:
            self.current_step_id = None
            return True
        item = self.find_work_step(item_id)
        if item is None:
            return False
        self.current_step_id = item.id
        if item.status == "pending":
            item.status = "in_progress"
        return True

    def add_failure_target(
        self,
        *,
        kind: str,
        entrypoint: str,
        source: str,
        method: str | None = None,
    ) -> None:
        normalized = " ".join(entrypoint.strip().split())
        if not normalized:
            return
        for target in self.failure_targets:
            if target.kind == kind and target.entrypoint == normalized:
                return
        self.failure_targets.append(
            FailureReproductionTarget(
                kind=kind,
                entrypoint=normalized,
                source=source,
                method=method.upper() if method else None,
            )
        )

    def mark_failure_target_verified(self, entrypoint: str, evidence: str) -> None:
        normalized = " ".join(entrypoint.strip().split())
        if not normalized:
            return
        for target in self.failure_targets:
            if target.entrypoint == normalized:
                target.status = "verified"
                target.evidence = evidence

    def mark_failure_target_reproduced(self, entrypoint: str, evidence: str) -> None:
        normalized = " ".join(entrypoint.strip().split())
        if not normalized:
            return
        for target in self.failure_targets:
            if target.entrypoint == normalized and target.status != "verified":
                target.status = "reproduced"
                target.evidence = evidence

    def unresolved_failure_targets(self) -> list[FailureReproductionTarget]:
        return [target for target in self.failure_targets if target.status != "verified"]


def _optional_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.strip().split())
    return normalized or None


def _normalize_path(value: str) -> str:
    return value.strip().replace("\\", "/").strip("/")


def _drop_runtime_resource_acceptance(items: list[str]) -> list[str]:
    return [
        item
        for item in items
        if not _is_observation_only_acceptance_text(item)
    ]


def _is_runtime_resource_acceptance_record(record: EvidenceRecord) -> bool:
    return (
        record.source_tool == "start_background_command"
        or record.summary.strip().lower().startswith("service ready:")
    )


def _is_observation_only_acceptance_text(item: str) -> bool:
    normalized = item.strip().lower()
    return normalized.startswith("service ready:") or normalized.startswith("http_request passed:")


def _work_plan_item_from_dict(data: dict[str, object]) -> WorkPlanItem | None:
    item_id = _optional_string(data.get("id"))
    title = _optional_string(data.get("title"))
    if not item_id or not title:
        return None
    status = _optional_string(data.get("status")) or "pending"
    if status not in {"pending", "in_progress", "done", "blocked"}:
        status = "pending"
    evidence = data.get("evidence")
    evidence_ids = data.get("evidence_ids")
    required = data.get("required")
    return WorkPlanItem(
        id=item_id[:80],
        title=title[:300],
        status=status,
        acceptance=(_optional_string(data.get("acceptance")) or "")[:500],
        evidence=[item[:500] for item in evidence if isinstance(item, str)][:10]
        if isinstance(evidence, list)
        else [],
        evidence_ids=[item[:80] for item in evidence_ids if isinstance(item, str)][:20]
        if isinstance(evidence_ids, list)
        else [],
        blocker=(_optional_string(data.get("blocker")) or "")[:500],
        verification_hint=(_optional_string(data.get("verification_hint")) or "")[:500],
        required=required if isinstance(required, bool) else True,
        source_document=(_optional_string(data.get("source_document")) or "")[:500],
    )


def _work_plan_source_from_dict(data: dict[str, object]) -> WorkPlanSource | None:
    path = _optional_string(data.get("path"))
    content_hash = _optional_string(data.get("content_hash"))
    if not path or not content_hash:
        return None
    mtime = data.get("mtime")
    synced_at = _optional_string(data.get("synced_at")) or datetime.now(UTC).isoformat()
    return WorkPlanSource(
        path=path[:500],
        content_hash=content_hash[:80],
        mtime=mtime if isinstance(mtime, (int, float)) else None,
        synced_at=synced_at,
    )


def _evidence_record_from_dict(data: dict[str, object]) -> EvidenceRecord | None:
    record_id = _optional_string(data.get("id"))
    kind = _optional_string(data.get("kind"))
    source_tool = _optional_string(data.get("source_tool"))
    summary = _optional_string(data.get("summary"))
    if not record_id or not kind or not source_tool or not summary:
        return None
    exit_code = data.get("exit_code")
    return EvidenceRecord(
        id=record_id[:80],
        kind=kind[:80],
        source_tool=source_tool[:80],
        summary=summary[:500],
        detail=(_optional_string(data.get("detail")) or "")[:2000],
        command=(_optional_string(data.get("command")) or "")[:500],
        url=(_optional_string(data.get("url")) or "")[:500],
        path=(_optional_string(data.get("path")) or "")[:500],
        status=(_optional_string(data.get("status")) or "passed")[:80],
        exit_code=exit_code if isinstance(exit_code, int) else None,
        created_at=(_optional_string(data.get("created_at")) or datetime.now(UTC).isoformat()),
    )


def _runtime_resource_from_dict(data: dict[str, object]) -> RuntimeResource | None:
    resource_id = _optional_string(data.get("id"))
    kind = _optional_string(data.get("kind"))
    source_tool = _optional_string(data.get("source_tool"))
    if not resource_id or not kind or not source_tool:
        return None
    port = data.get("port")
    pid = data.get("pid")
    ready = data.get("ready")
    exit_code = data.get("exit_code")
    status_code = data.get("status_code")
    raw_data = data.get("data")
    return RuntimeResource(
        id=resource_id[:80],
        kind=kind[:80],
        source_tool=source_tool[:80],
        command=(_optional_string(data.get("command")) or "")[:500],
        cwd=(_optional_string(data.get("cwd")) or "")[:500],
        url=(_optional_string(data.get("url")) or "")[:500],
        method=(_optional_string(data.get("method")) or "")[:40],
        port=port if isinstance(port, int) else None,
        pid=pid if isinstance(pid, int) else None,
        ready=ready if isinstance(ready, bool) else None,
        exit_code=exit_code if isinstance(exit_code, int) else None,
        status_code=status_code if isinstance(status_code, int) else None,
        log_path=(_optional_string(data.get("log_path")) or "")[:500],
        status=(_optional_string(data.get("status")) or "")[:80],
        summary=(_optional_string(data.get("summary")) or "")[:500],
        data=dict(raw_data) if isinstance(raw_data, dict) else {},
        created_at=(_optional_string(data.get("created_at")) or datetime.now(UTC).isoformat()),
    )


def _engineering_fact_from_dict(data: dict[str, object]) -> EngineeringFact | None:
    fact_id = _optional_string(data.get("id"))
    fact_type = _optional_string(data.get("type"))
    summary = _optional_string(data.get("summary"))
    if not fact_id or not fact_type or not summary:
        return None
    raw_data = data.get("data")
    return EngineeringFact(
        id=fact_id[:80],
        type=fact_type[:80],
        source=(_optional_string(data.get("source")) or "")[:500],
        summary=summary[:500],
        data=dict(raw_data) if isinstance(raw_data, dict) else {},
        confidence=(_optional_string(data.get("confidence")) or "medium")[:40],
        stale=bool(data.get("stale")),
        observed_at=(_optional_string(data.get("observed_at")) or datetime.now(UTC).isoformat()),
    )


def _lesson_record_from_dict(data: dict[str, object]) -> LessonRecord | None:
    lesson_id = _optional_string(data.get("id"))
    summary = _optional_string(data.get("summary"))
    if not lesson_id or not summary:
        return None
    use_count = data.get("use_count")
    return LessonRecord(
        id=lesson_id[:80],
        summary=summary[:500],
        trigger=(_optional_string(data.get("trigger")) or "")[:300],
        scope=(_optional_string(data.get("scope")) or "general")[:80],
        evidence=(_optional_string(data.get("evidence")) or "")[:500],
        created_at=(_optional_string(data.get("created_at")) or datetime.now(UTC).isoformat()),
        use_count=use_count if isinstance(use_count, int) else 0,
    )


def _workspace_entry_from_dict(data: dict[str, object]) -> WorkspaceEntry | None:
    path = _optional_string(data.get("path"))
    kind = _optional_string(data.get("kind"))
    source_tool = _optional_string(data.get("source_tool"))
    if not path or not kind or not source_tool:
        return None
    size = data.get("size")
    return WorkspaceEntry(
        path=_normalize_path(path)[:500],
        kind=kind[:40],
        source_tool=source_tool[:80],
        size=size if isinstance(size, int) else None,
        observed_at=(_optional_string(data.get("observed_at")) or datetime.now(UTC).isoformat()),
    )


def _read_record_from_dict(data: dict[str, object]) -> ReadRecord | None:
    path = _optional_string(data.get("path"))
    source_tool = _optional_string(data.get("source_tool"))
    if not path or not source_tool:
        return None
    size = data.get("size")
    raw_hash = data.get("content_hash") or data.get("hash")
    line_count = data.get("line_count")
    full_read_count = data.get("full_read_count")
    last_start_line = data.get("last_start_line")
    last_end_line = data.get("last_end_line")
    return ReadRecord(
        path=_normalize_path(path)[:500],
        source_tool=source_tool[:80],
        size=size if isinstance(size, int) else None,
        excerpt=(_optional_string(data.get("excerpt")) or "")[:500],
        content_hash=raw_hash[:80] if isinstance(raw_hash, str) else "",
        mtime=data.get("mtime") if isinstance(data.get("mtime"), (int, float)) else None,
        read_count=data.get("read_count") if isinstance(data.get("read_count"), int) else 1,
        truncated=bool(data.get("truncated")),
        line_count=line_count if isinstance(line_count, int) else None,
        line_ranges=_read_ranges_from_dict(data.get("line_ranges")),
        full_read_count=full_read_count if isinstance(full_read_count, int) else 0,
        last_start_line=last_start_line if isinstance(last_start_line, int) else None,
        last_end_line=last_end_line if isinstance(last_end_line, int) else None,
        observed_at=(_optional_string(data.get("observed_at")) or datetime.now(UTC).isoformat()),
    )


def _change_record_from_dict(data: dict[str, object]) -> ChangeRecord | None:
    path = _optional_string(data.get("path"))
    source_tool = _optional_string(data.get("source_tool"))
    action = _optional_string(data.get("action"))
    if not path or not source_tool or not action:
        return None
    return ChangeRecord(
        path=_normalize_path(path)[:500],
        source_tool=source_tool[:80],
        action=action[:80],
        summary=(_optional_string(data.get("summary")) or "")[:500],
        observed_at=(_optional_string(data.get("observed_at")) or datetime.now(UTC).isoformat()),
    )


def _next_evidence_index(records: list[EvidenceRecord]) -> int:
    highest = 0
    for record in records:
        if not record.id.startswith("ev-"):
            continue
        try:
            highest = max(highest, int(record.id.removeprefix("ev-")))
        except ValueError:
            continue
    return highest + 1


def _next_runtime_resource_index(resources: list[RuntimeResource]) -> int:
    highest = 0
    for resource in resources:
        if not resource.id.startswith("rr-"):
            continue
        try:
            highest = max(highest, int(resource.id.removeprefix("rr-")))
        except ValueError:
            continue
    return highest + 1


def _next_engineering_fact_index(facts: list[EngineeringFact]) -> int:
    highest = 0
    for fact in facts:
        if not fact.id.startswith("fact-"):
            continue
        try:
            highest = max(highest, int(fact.id.removeprefix("fact-")))
        except ValueError:
            continue
    return highest + 1


def _next_lesson_index(lessons: list[LessonRecord]) -> int:
    highest = 0
    for lesson in lessons:
        if not lesson.id.startswith("lesson-"):
            continue
        try:
            highest = max(highest, int(lesson.id.removeprefix("lesson-")))
        except ValueError:
            continue
    return highest + 1


def re_split_words(text: str) -> list[str]:
    return re.findall(r"[\w.-]+", text)


def _read_ranges_from_dict(value: object) -> list[list[int]]:
    if not isinstance(value, list):
        return []
    ranges: list[list[int]] = []
    for raw in value:
        if (
            isinstance(raw, list)
            and len(raw) == 2
            and isinstance(raw[0], int)
            and isinstance(raw[1], int)
        ):
            ranges = _merge_line_ranges(ranges, raw[0], raw[1])
    return ranges


def _merge_line_ranges(
    ranges: list[list[int]],
    start_line: int | None,
    end_line: int | None,
) -> list[list[int]]:
    if start_line is None or end_line is None:
        return [list(item) for item in ranges]
    start = max(1, int(start_line))
    end = max(start, int(end_line))
    merged = [list(item) for item in ranges]
    merged.append([start, end])
    merged.sort(key=lambda item: (item[0], item[1]))
    compact: list[list[int]] = []
    for start, end in merged:
        if not compact or start > compact[-1][1] + 1:
            compact.append([start, end])
        else:
            compact[-1][1] = max(compact[-1][1], end)
    return compact[-12:]


def _is_full_line_read(
    ranges: list[list[int]],
    line_count: int | None,
    truncated: bool,
) -> bool:
    if truncated or not ranges or not isinstance(line_count, int) or line_count <= 0:
        return False
    return _has_full_line_coverage(ranges, line_count)


def _has_full_line_coverage(ranges: list[list[int]], line_count: int | None) -> bool:
    if not ranges or not isinstance(line_count, int) or line_count <= 0:
        return False
    return any(start <= 1 and end >= line_count for start, end in ranges)
