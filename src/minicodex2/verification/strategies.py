from __future__ import annotations

import os
import json
from dataclasses import dataclass
from pathlib import Path

from minicodex2.project.profile import ProjectProfile
from minicodex2.tools.command_runner import find_free_port
from minicodex2.verification.compilers import default_c_compiler, find_c_compiler
from minicodex2.verification.plan import VerificationPlan, VerificationStep


@dataclass(slots=True)
class MatchResult:
    matched: bool
    confidence: float
    reason: str


class VerificationStrategy:
    name = "base"

    def matches(self, root: Path, profile: ProjectProfile, changed_files: list[str]) -> MatchResult:
        raise NotImplementedError

    def build_plan(self, root: Path, profile: ProjectProfile, changed_files: list[str]) -> VerificationPlan:
        raise NotImplementedError


class ConfiguredCommandsStrategy(VerificationStrategy):
    name = "configured"

    def __init__(self, commands: list[str]) -> None:
        self.commands = commands

    def matches(self, root: Path, profile: ProjectProfile, changed_files: list[str]) -> MatchResult:
        return MatchResult(bool(self.commands), 1.0, "configured verification commands")

    def build_plan(self, root: Path, profile: ProjectProfile, changed_files: list[str]) -> VerificationPlan:
        return VerificationPlan(
            steps=[
                VerificationStep(
                    name=f"configured:{idx}",
                    step_type="command",
                    command=command,
                    timeout_seconds=60,
                )
                for idx, command in enumerate(self.commands, start=1)
            ],
            reason="configured verification commands",
            confidence=1.0,
        )


class ScriptStaticVerificationStrategy(VerificationStrategy):
    name = "script_static"
    script_suffixes = {".bat", ".cmd", ".ps1", ".sh"}

    def matches(self, root: Path, profile: ProjectProfile, changed_files: list[str]) -> MatchResult:
        script_changes = self._script_changes(root, changed_files)
        if not script_changes:
            return MatchResult(False, 0.0, "no changed script files")
        return MatchResult(True, 0.35, "changed script files need static verification")

    def build_plan(self, root: Path, profile: ProjectProfile, changed_files: list[str]) -> VerificationPlan:
        script_changes = self._script_changes(root, changed_files)
        return VerificationPlan(
            steps=[
                VerificationStep(
                    name=f"static script check:{path}",
                    step_type="static_file_check",
                    command=path,
                    timeout_seconds=5,
                )
                for path in script_changes
            ],
            reason="changed script files need static verification",
            confidence=0.35,
        )

    def _script_changes(self, root: Path, changed_files: list[str]) -> list[str]:
        paths: list[str] = []
        for path in changed_files:
            candidate = Path(path)
            if candidate.suffix.lower() not in self.script_suffixes:
                continue
            relative = self._relative_to_root(root, candidate)
            paths.append(relative.as_posix())
        return paths

    def _relative_to_root(self, root: Path, path: Path) -> Path:
        parts = path.parts
        if parts and parts[0] == root.name:
            stripped = Path(*parts[1:])
            if (root / stripped).exists():
                return stripped
        return path


class DocumentationVerificationStrategy(VerificationStrategy):
    name = "documentation"
    document_suffixes = {".md", ".markdown", ".txt", ".rst", ".adoc"}
    document_names = {"readme", "license", "changelog", "authors", "contributors"}

    def matches(self, root: Path, profile: ProjectProfile, changed_files: list[str]) -> MatchResult:
        document_changes = self._document_changes(root, changed_files)
        if not document_changes:
            return MatchResult(False, 0.0, "no changed documentation files")
        if len(document_changes) != len(_relevant_changed_files(changed_files)):
            return MatchResult(False, 0.0, "mixed documentation and project changes")
        return MatchResult(True, 0.3, "documentation-only changes")

    def build_plan(self, root: Path, profile: ProjectProfile, changed_files: list[str]) -> VerificationPlan:
        return VerificationPlan(
            steps=[
                VerificationStep(
                    name=f"document check:{path}",
                    step_type="document_check",
                    command=path,
                    timeout_seconds=5,
                )
                for path in self._document_changes(root, changed_files)
            ],
            reason="documentation-only changes",
            confidence=0.3,
        )

    def _document_changes(self, root: Path, changed_files: list[str]) -> list[str]:
        paths: list[str] = []
        for path in _relevant_changed_files(changed_files):
            candidate = Path(path)
            if not self._is_document_path(candidate):
                continue
            relative = _relative_to_root(root, candidate)
            paths.append(relative.as_posix())
        return paths

    def _is_document_path(self, path: Path) -> bool:
        suffix = path.suffix.lower()
        if suffix in self.document_suffixes:
            return True
        return path.name.lower() in self.document_names


class PythonVerificationStrategy(VerificationStrategy):
    name = "python"

    def matches(self, root: Path, profile: ProjectProfile, changed_files: list[str]) -> MatchResult:
        if "pytest" in profile.test_signals:
            return MatchResult(True, 0.95, "pytest signal found")
        if profile.has_type("python"):
            return MatchResult(True, 0.5, "python project without explicit tests")
        return MatchResult(False, 0.0, "no python signal")

    def build_plan(self, root: Path, profile: ProjectProfile, changed_files: list[str]) -> VerificationPlan:
        if "pytest" in profile.test_signals:
            pytest_command = "python -m pytest"
            test_target = _local_pytest_target(root)
            if test_target and _python_tests_appear_service_dependent(root / test_target):
                return VerificationPlan(
                    steps=[],
                    reason=(
                        "local pytest tests appear service-dependent or integration-like; "
                        "model should decide verification order"
                    ),
                    confidence=0.45,
                    requires_model_decision=True,
                )
            if test_target:
                pytest_command = f"python -m pytest {test_target}"
            return VerificationPlan(
                steps=[
                    VerificationStep(
                        name="pytest",
                        step_type="test",
                        command=pytest_command,
                        timeout_seconds=60,
                    )
                ],
                reason="pytest signal found",
                confidence=0.95,
            )
        entry = profile.likely_entrypoints[0] if profile.likely_entrypoints else None
        if entry and entry.endswith(".py"):
            return VerificationPlan(
                steps=[
                    VerificationStep(
                        name=f"run:{entry}",
                        step_type="run",
                        command=f"python {entry}",
                        timeout_seconds=30,
                    )
                ],
                reason="python entrypoint smoke",
                confidence=0.5,
            )
        return VerificationPlan(
            steps=[
                VerificationStep(
                    name="compileall",
                    step_type="test",
                    command="python -m compileall .",
                    timeout_seconds=60,
                )
            ],
            reason="python syntax smoke",
            confidence=0.4,
        )


class CVerificationStrategy(VerificationStrategy):
    name = "c"

    def matches(self, root: Path, profile: ProjectProfile, changed_files: list[str]) -> MatchResult:
        if profile.has_type("c"):
            return MatchResult(True, 0.9 if "main.c" in profile.key_files else 0.75, "C project signal")
        return MatchResult(False, 0.0, "no C signal")

    def build_plan(self, root: Path, profile: ProjectProfile, changed_files: list[str]) -> VerificationPlan:
        if "Makefile" in profile.key_files:
            return VerificationPlan(
                steps=[
                    VerificationStep(
                        name="make",
                        step_type="test",
                        command="make test",
                        timeout_seconds=60,
                    )
                ],
                reason="Makefile found",
                confidence=0.75,
            )
        exe_name = "main.exe" if os.name == "nt" else "main"
        out = f".minicodex2/build/{exe_name}"
        run_command = out if os.name == "nt" else f"./{out}"
        compiler = find_c_compiler() or default_c_compiler()
        return VerificationPlan(
            steps=[
                VerificationStep(
                    name=f"{compiler.executable} main.c",
                    step_type="compile",
                    command=compiler.build_command("main.c", out),
                    timeout_seconds=30,
                ),
                VerificationStep(
                    name="run main",
                    step_type="run",
                    command=run_command,
                    timeout_seconds=10,
                ),
            ],
            reason="main.c found",
            confidence=0.9,
        )


class NodeVerificationStrategy(VerificationStrategy):
    name = "node"

    def matches(self, root: Path, profile: ProjectProfile, changed_files: list[str]) -> MatchResult:
        if profile.has_type("node"):
            return MatchResult(True, 0.85, "package.json found")
        return MatchResult(False, 0.0, "no node signal")

    def build_plan(self, root: Path, profile: ProjectProfile, changed_files: list[str]) -> VerificationPlan:
        package = json.loads((root / "package.json").read_text(encoding="utf-8"))
        scripts = package.get("scripts") or {}
        test_script = str(scripts.get("test") or "")
        if test_script and _node_test_script_is_auto_runnable(test_script):
            command = "npm test"
            reason = "package.json test script found"
            timeout_seconds = 60
        elif "build" in scripts:
            command = "npm run build"
            reason = "package.json build script found"
            timeout_seconds = 180
        elif test_script:
            return VerificationPlan(
                steps=[],
                reason=(
                    "package.json test script may be interactive or long-running; "
                    "model should inspect scripts and choose verification"
                ),
                confidence=0.45,
                requires_model_decision=True,
            )
        else:
            command = "npm install --package-lock-only --ignore-scripts"
            reason = "package.json found, no test/build script"
            timeout_seconds = 60
        return VerificationPlan(
            steps=[
                VerificationStep(
                    name="node verification",
                    step_type="test",
                    command=command,
                    timeout_seconds=timeout_seconds,
                )
            ],
            reason=reason,
            confidence=0.85,
        )


class CMakeVerificationStrategy(VerificationStrategy):
    name = "cmake"

    def matches(self, root: Path, profile: ProjectProfile, changed_files: list[str]) -> MatchResult:
        if profile.has_type("cmake"):
            return MatchResult(True, 0.9, "CMakeLists.txt found")
        return MatchResult(False, 0.0, "no cmake signal")

    def build_plan(self, root: Path, profile: ProjectProfile, changed_files: list[str]) -> VerificationPlan:
        return VerificationPlan(
            steps=[
                VerificationStep("cmake configure", "command", "cmake -S . -B .minicodex2/build/cmake", 60),
                VerificationStep("cmake build", "command", "cmake --build .minicodex2/build/cmake", 120),
                VerificationStep("ctest", "test", "ctest --test-dir .minicodex2/build/cmake", 60),
            ],
            reason="CMakeLists.txt found",
            confidence=0.9,
        )


class RustVerificationStrategy(VerificationStrategy):
    name = "rust"

    def matches(self, root: Path, profile: ProjectProfile, changed_files: list[str]) -> MatchResult:
        if profile.has_type("rust"):
            return MatchResult(True, 0.95, "Cargo.toml found")
        return MatchResult(False, 0.0, "no rust signal")

    def build_plan(self, root: Path, profile: ProjectProfile, changed_files: list[str]) -> VerificationPlan:
        return VerificationPlan(
            steps=[VerificationStep("cargo test", "test", "cargo test", 120)],
            reason="Cargo.toml found",
            confidence=0.95,
        )


class GoVerificationStrategy(VerificationStrategy):
    name = "go"

    def matches(self, root: Path, profile: ProjectProfile, changed_files: list[str]) -> MatchResult:
        if profile.has_type("go"):
            return MatchResult(True, 0.95, "go.mod found")
        if self._go_source_changes(changed_files):
            return MatchResult(True, 0.55, "changed Go source files without go.mod")
        return MatchResult(False, 0.0, "no go signal")

    def build_plan(self, root: Path, profile: ProjectProfile, changed_files: list[str]) -> VerificationPlan:
        if not profile.has_type("go"):
            sources = self._go_source_changes(changed_files)
            return VerificationPlan(
                steps=[
                    VerificationStep(
                        "go run changed sources",
                        "run",
                        f"go run {' '.join(sources)}",
                        60,
                    )
                ],
                reason="changed Go source files without go.mod",
                confidence=0.55,
            )
        return VerificationPlan(
            steps=[VerificationStep("go test", "test", "go test ./...", 120)],
            reason="go.mod found",
            confidence=0.95,
        )

    def _go_source_changes(self, changed_files: list[str]) -> list[str]:
        return [
            Path(path).as_posix()
            for path in changed_files
            if path.endswith(".go") and not path.endswith("_test.go")
        ]


class PythonWebVerificationStrategy(VerificationStrategy):
    name = "python_web"

    def matches(self, root: Path, profile: ProjectProfile, changed_files: list[str]) -> MatchResult:
        if profile.has_type("python_web"):
            return MatchResult(True, 0.88, f"python web signal: {profile.web_signals}")
        return MatchResult(False, 0.0, "no python web signal")

    def build_plan(self, root: Path, profile: ProjectProfile, changed_files: list[str]) -> VerificationPlan:
        port = find_free_port()
        if "fastapi" in profile.web_signals:
            command = f"uvicorn app:app --host 127.0.0.1 --port {port}"
            reason = "FastAPI app signal"
            smoke = f"http://127.0.0.1:{port}/docs"
        else:
            command = f"python -m flask --app app run --host 127.0.0.1 --port {port}"
            reason = "Flask app signal"
            smoke = f"http://127.0.0.1:{port}/"
        return VerificationPlan(
            steps=[
                VerificationStep(
                    name="start web server",
                    step_type="background_server",
                    command=command,
                    timeout_seconds=15,
                    port=port,
                ),
                VerificationStep(
                    name="http smoke",
                    step_type="http_smoke",
                    timeout_seconds=10,
                    url=smoke,
                ),
            ],
            reason=reason,
            confidence=0.88,
        )


def _relevant_changed_files(changed_files: list[str]) -> list[str]:
    return [
        path
        for path in changed_files
        if Path(path).parts and Path(path).parts[0] not in {".minicodex2"}
    ]


def _relative_to_root(root: Path, path: Path) -> Path:
    parts = path.parts
    if parts and parts[0] == root.name:
        stripped = Path(*parts[1:])
        if (root / stripped).exists():
            return stripped
    return path


def _local_pytest_target(root: Path) -> str | None:
    tests_dir = root / "tests"
    if not tests_dir.is_dir():
        return None
    patterns = ("test_*.py", "*_test.py")
    for pattern in patterns:
        if any(tests_dir.rglob(pattern)):
            return "tests"
    return None


def _python_tests_appear_service_dependent(path: Path) -> bool:
    if not path.exists():
        return False
    patterns = ("test_*.py", "*_test.py")
    markers = (
        "requests",
        "http://",
        "https://",
        "localhost",
        "127.0.0.1",
        "base_url",
        "api/",
    )
    files: list[Path] = []
    if path.is_file():
        files = [path]
    else:
        for pattern in patterns:
            files.extend(path.rglob(pattern))
    for file_path in files[:40]:
        text = file_path.read_text(encoding="utf-8", errors="ignore").lower()
        if any(marker in text for marker in markers):
            return True
    return False


def _node_test_script_is_auto_runnable(script: str) -> bool:
    normalized = " ".join(script.lower().split())
    if not normalized:
        return False
    if any(flag in normalized for flag in ("--watch", "--watchall", " --ui", " --open")):
        return False
    if "react-scripts test" in normalized:
        return False
    if "vitest" in normalized and "--run" not in normalized:
        return False
    if "jest" in normalized and "--watch" not in normalized and "--ci" not in normalized:
        return False
    return True
