from __future__ import annotations

import json
import shutil
import tempfile
import time
from collections import Counter
from pathlib import Path

from minicodex2.agent.chat_history import ChatHistory
from minicodex2.agent.unified_session import UnifiedAgentSession
from minicodex2.benchmark.types import (
    BenchmarkCase,
    BenchmarkCaseResult,
    BenchmarkModelMode,
    BenchmarkSuiteResult,
)
from minicodex2.config.settings import ConfigLoader
from minicodex2.model.fake_adapter import FakeModelAdapter
from minicodex2.model.openai_compatible import OpenAICompatibleModelAdapter
from minicodex2.tools.registry import build_runtime_tool_registry
from minicodex2.tools.runtime_tools import RuntimeTools


class BenchmarkRunner:
    def __init__(
        self,
        output_root: str | Path | None = None,
        *,
        model_mode: BenchmarkModelMode = "fake",
        api_key: str | None = None,
        model_profile: str | None = None,
        base_url: str | None = None,
        model_name: str | None = None,
        wire_api: str | None = None,
    ) -> None:
        self._tempdir: tempfile.TemporaryDirectory[str] | None = None
        self.model_mode = model_mode
        self.api_key = api_key
        self.model_profile = model_profile
        self.base_url = base_url
        self.model_name = model_name
        self.wire_api = wire_api
        if output_root:
            self.output_root = Path(output_root).resolve()
            self.output_root.mkdir(parents=True, exist_ok=True)
        else:
            self._tempdir = tempfile.TemporaryDirectory(prefix="minicodex2-benchmark-")
            self.output_root = Path(self._tempdir.name).resolve()

    def close(self) -> None:
        if self._tempdir:
            self._tempdir.cleanup()
            self._tempdir = None

    def run_suite(self, suite_name: str, cases: list[BenchmarkCase]) -> BenchmarkSuiteResult:
        results = [self.run_case(case) for case in cases]
        suite_result = BenchmarkSuiteResult(
            suite=suite_name,
            model_mode=self.model_mode,
            results=results,
        )
        self.write_reports(suite_result)
        return suite_result

    def run_case(self, case: BenchmarkCase) -> BenchmarkCaseResult:
        workspace = self.output_root / "workspaces" / case.name
        log_path: str | None = None
        if case.requires_executable and not shutil.which(case.requires_executable):
            return BenchmarkCaseResult(
                name=case.name,
                status="skipped",
                one_turn_success=False,
                agent_status="skipped",
                model_mode=self.model_mode,
                workspace=str(workspace),
                summary=f"missing executable: {case.requires_executable}",
            )
        if case.requires_any_executable and not any(
            shutil.which(executable) for executable in case.requires_any_executable
        ):
            return BenchmarkCaseResult(
                name=case.name,
                status="skipped",
                one_turn_success=False,
                agent_status="skipped",
                model_mode=self.model_mode,
                workspace=str(workspace),
                summary="missing one of executables: "
                + ", ".join(case.requires_any_executable),
            )
        if self.model_mode == "real" and not self.api_key:
            return BenchmarkCaseResult(
                name=case.name,
                status="skipped",
                one_turn_success=False,
                agent_status="skipped",
                model_mode=self.model_mode,
                workspace=str(workspace),
                summary="real benchmark requires api_key",
            )
        self._reset_workspace(workspace)
        case.setup(workspace)
        if self.model_mode == "real" and case.seed_broken:
            case.seed_broken(workspace)

        settings = ConfigLoader().load(
            workspace,
            api_key=self.api_key,
            model_profile=self.model_profile,
            base_url=self.base_url,
            model=self.model_name,
            wire_api=self.wire_api,
        )
        settings.max_repair_rounds = case.max_repair_rounds
        model = self._build_model(settings, case)
        runtime = RuntimeTools(settings)
        session = UnifiedAgentSession(
            settings=settings,
            model=model,
            tools=build_runtime_tool_registry(runtime),
            history=ChatHistory(),
        )
        try:
            turn_result = session.run_turn(
                case.prompt,
                allow_advice=False,
                expected_action="modify_and_verify",
            )
        except Exception as exc:
            events = session.event_bus.events()
            event_counts = Counter(event.type for event in events)
            return BenchmarkCaseResult(
                name=case.name,
                status="failed",
                one_turn_success=False,
                agent_status="error",
                model_mode=self.model_mode,
                workspace=str(workspace),
                summary=f"benchmark case raised: {exc!r}",
                repair_rounds=event_counts["repair_round_started"],
                tool_calls=event_counts["tool_call_started"],
                model_calls=getattr(model, "call_count", 0),
                total_tokens=session.token_usage.total().total_tokens,
                verification_started=event_counts["verification_started"] > 0,
                verification_passed=event_counts["verification_passed"] > 0,
                failure_pack_created=event_counts["failure_pack_created"] > 0,
                blocked_without_evidence=True,
                hidden_assertion="case crashed before hidden assertion",
                log_path=str(session.logger.path),
                event_counts=dict(event_counts),
            )
        log_path = str(session.logger.path)
        events = session.event_bus.events()
        event_counts = Counter(event.type for event in events)

        try:
            hidden_ok, hidden_summary = case.hidden_assert(workspace)
        except Exception as exc:
            hidden_ok = False
            hidden_summary = f"hidden assertion raised: {exc!r}"
        verification_started = event_counts["verification_started"] > 0
        verification_passed = event_counts["verification_passed"] > 0
        false_completed = (
            turn_result.status == "completed"
            and case.expected_status == "passed"
            and not verification_passed
        )
        verification_skipped = bool(turn_result.changed_files) and not verification_started
        blocked_without_evidence = (
            turn_result.status == "blocked" and event_counts["failure_pack_created"] == 0
        )
        one_turn_success = (
            turn_result.status == case.expected_status
            and hidden_ok
            and verification_passed
            and not false_completed
            and not verification_skipped
            and not blocked_without_evidence
        )
        status = "passed" if one_turn_success else "failed"
        if self.model_mode == "real" and turn_result.status == "completed" and not turn_result.changed_files:
            false_completed = True
            one_turn_success = False
            status = "failed"
        return BenchmarkCaseResult(
            name=case.name,
            status=status,
            one_turn_success=one_turn_success,
            agent_status=turn_result.status,
            model_mode=self.model_mode,
            workspace=str(workspace),
            summary=turn_result.response,
            repair_rounds=event_counts["repair_round_started"],
            tool_calls=event_counts["tool_call_started"],
            model_calls=getattr(model, "call_count", 0),
            total_tokens=turn_result.token_usage.total_tokens,
            verification_started=verification_started,
            verification_passed=verification_passed,
            failure_pack_created=event_counts["failure_pack_created"] > 0,
            false_completed=false_completed,
            verification_skipped=verification_skipped,
            blocked_without_evidence=blocked_without_evidence,
            hidden_assertion=hidden_summary,
            log_path=log_path,
            event_counts=dict(event_counts),
        )

    def _build_model(self, settings, case: BenchmarkCase):
        if self.model_mode == "fake":
            return FakeModelAdapter(case.steps)
        return CountingModelAdapter(
            OpenAICompatibleModelAdapter(
                base_url=settings.model.base_url,
                api_key=settings.model.api_key or "",
                model=settings.model.model,
                wire_api=settings.model.wire_api,
                timeout_seconds=settings.model.timeout_seconds,
            )
        )

    def write_reports(self, result: BenchmarkSuiteResult) -> tuple[Path, Path]:
        self.output_root.mkdir(parents=True, exist_ok=True)
        json_path = self.output_root / "benchmark-report.json"
        md_path = self.output_root / "benchmark-report.md"
        json_path.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        md_path.write_text(result.to_markdown(), encoding="utf-8")
        return json_path, md_path

    def _reset_workspace(self, workspace: Path) -> None:
        workspace = workspace.resolve()
        root = (self.output_root / "workspaces").resolve()
        if workspace.exists():
            if root not in workspace.parents:
                raise ValueError(f"refusing to delete benchmark workspace outside {root}: {workspace}")
            shutil.rmtree(workspace)
        workspace.mkdir(parents=True, exist_ok=True)


class CountingModelAdapter:
    def __init__(self, inner) -> None:
        self.inner = inner
        self.call_count = 0

    def complete(self, request):
        self.call_count += 1
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                return self.inner.complete(request)
            except Exception as exc:
                last_error = exc
                if attempt == 3:
                    break
                time.sleep(1.5 * attempt)
        raise last_error or RuntimeError("model request failed")
