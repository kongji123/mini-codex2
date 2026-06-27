from __future__ import annotations

import json
import shutil
from pathlib import Path

from minicodex2.benchmark.cases import (
    C_COMPILER_CANDIDATES,
    built_in_suite,
    c_compile_repair_case,
    python_cli_persistence_case,
    python_cli_repair_case,
    python_web_persistence_case,
)
from minicodex2.benchmark.runner import BenchmarkRunner
from minicodex2.model.adapter import ModelAdapter
from minicodex2.model.messages import ModelRequest, ModelResponse


def test_python_benchmark_measures_one_turn_repair(tmp_path: Path) -> None:
    runner = BenchmarkRunner(tmp_path)
    result = runner.run_case(python_cli_repair_case())

    assert result.status == "passed"
    assert result.one_turn_success is True
    assert result.agent_status == "passed"
    assert result.verification_started is True
    assert result.verification_passed is True
    assert result.failure_pack_created is True
    assert result.repair_rounds >= 1
    assert result.tool_calls == 2
    assert result.false_completed is False
    assert result.verification_skipped is False


def test_benchmark_suite_writes_json_and_markdown_reports(tmp_path: Path) -> None:
    runner = BenchmarkRunner(tmp_path)
    suite = runner.run_suite("smoke", [python_cli_repair_case()])

    json_path = tmp_path / "benchmark-report.json"
    md_path = tmp_path / "benchmark-report.md"
    assert json_path.exists()
    assert md_path.exists()
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["suite"] == "smoke"
    assert data["model_mode"] == "fake"
    assert data["one_turn_success_rate"] == 1.0
    assert "python_cli_repair" in md_path.read_text(encoding="utf-8")
    assert suite.success_count == 1


def test_c_benchmark_skips_when_gcc_is_missing(tmp_path: Path) -> None:
    case = c_compile_repair_case()
    if any(shutil.which(executable) for executable in C_COMPILER_CANDIDATES):
        return

    runner = BenchmarkRunner(tmp_path)
    result = runner.run_case(case)
    assert result.status == "skipped"
    assert "missing one of executables" in result.summary


def test_builtin_smoke_suite_includes_interpreted_and_compiled_shapes() -> None:
    names = {case.name for case in built_in_suite("smoke")}
    assert names == {"python_cli_repair", "c_compile_repair"}


def test_builtin_extended_suite_has_representative_oracle_cases() -> None:
    cases = built_in_suite("extended")
    names = {case.name for case in cases}
    assert len(cases) >= 20
    assert "python_slugify" in names
    assert "python_json_total" in names
    assert "python_file_line_count" in names
    assert "python_cli_persistence" in names
    assert "python_web_persistence" in names
    assert "c_compile_repair" in names
    assert "node_test_repair" in names


def test_requirement_level_persistence_cases_run(tmp_path: Path) -> None:
    runner = BenchmarkRunner(tmp_path)
    suite = runner.run_suite(
        "requirement-smoke",
        [python_cli_persistence_case(), python_web_persistence_case()],
    )

    assert suite.runnable_count == 2
    assert suite.success_count == 2
    assert all(result.failure_pack_created for result in suite.results)
    assert all(result.verification_passed for result in suite.results)


def test_extended_python_subset_runs_many_oracle_cases(tmp_path: Path) -> None:
    cases = [
        case
        for case in built_in_suite("extended")
        if case.name.startswith("python_")
    ][:5]
    runner = BenchmarkRunner(tmp_path)
    suite = runner.run_suite("extended-subset", cases)

    assert suite.runnable_count == len(cases)
    assert suite.success_count == len(cases)
    assert suite.one_turn_success_rate == 1.0
    assert all(result.failure_pack_created for result in suite.results)


def test_real_benchmark_skips_without_api_key(tmp_path: Path) -> None:
    runner = BenchmarkRunner(tmp_path, model_mode="real")
    result = runner.run_case(python_cli_repair_case())

    assert result.status == "skipped"
    assert result.model_mode == "real"
    assert "requires api_key" in result.summary


def test_benchmark_case_model_error_does_not_abort_suite(tmp_path: Path, monkeypatch) -> None:
    class BrokenModel(ModelAdapter):
        def complete(self, request: ModelRequest) -> ModelResponse:
            raise RuntimeError("network broke")

    runner = BenchmarkRunner(tmp_path)
    monkeypatch.setattr(runner, "_build_model", lambda settings, case: BrokenModel())

    suite = runner.run_suite("smoke", [python_cli_repair_case()])

    assert suite.results[0].status == "failed"
    assert suite.results[0].agent_status == "error"
    assert "network broke" in suite.results[0].summary
    assert (tmp_path / "benchmark-report.json").exists()
