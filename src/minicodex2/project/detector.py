from __future__ import annotations

from pathlib import Path

from minicodex2.project.profile import ProjectProfile


class ProjectDetector:
    def detect(self, workspace_root: str | Path) -> ProjectProfile:
        root = Path(workspace_root)
        profile = ProjectProfile()
        self._detect_files(root, profile)
        self._detect_python(root, profile)
        self._detect_node(root, profile)
        self._detect_c(root, profile)
        self._detect_other(root, profile)
        return profile

    def _add_type(self, profile: ProjectProfile, value: str) -> None:
        if value not in profile.detected_types:
            profile.detected_types.append(value)

    def _add_key(self, profile: ProjectProfile, value: str) -> None:
        if value not in profile.key_files:
            profile.key_files.append(value)

    def _detect_files(self, root: Path, profile: ProjectProfile) -> None:
        for name in (
            "pyproject.toml",
            "requirements.txt",
            "pytest.ini",
            "package.json",
            "Makefile",
            "main.c",
            "CMakeLists.txt",
            "Cargo.toml",
            "go.mod",
            "app.py",
        ):
            if (root / name).exists():
                self._add_key(profile, name)

    def _detect_python(self, root: Path, profile: ProjectProfile) -> None:
        if any((root / name).exists() for name in ("pyproject.toml", "requirements.txt", "pytest.ini")):
            self._add_type(profile, "python")
        has_python_tests = any((root / "tests").glob("test_*.py")) or any(
            (root / "tests").glob("*_test.py")
        )
        if has_python_tests or (root / "pytest.ini").exists():
            profile.test_signals.append("pytest")
            self._add_type(profile, "python")
        if (root / "app.py").exists():
            profile.likely_entrypoints.append("app.py")
            text = (root / "app.py").read_text(encoding="utf-8", errors="ignore")[:10000]
            if "flask" in text.lower():
                self._add_type(profile, "python_web")
                profile.web_signals.append("flask")
            if "fastapi" in text.lower():
                self._add_type(profile, "python_web")
                profile.web_signals.append("fastapi")

    def _detect_node(self, root: Path, profile: ProjectProfile) -> None:
        if (root / "package.json").exists():
            self._add_type(profile, "node")
        if any(root.glob("vite.config.*")):
            self._add_type(profile, "node_web")
            profile.web_signals.append("vite")

    def _detect_c(self, root: Path, profile: ProjectProfile) -> None:
        if (root / "Makefile").exists():
            self._add_type(profile, "c")
            profile.test_signals.append("make")
        if (root / "main.c").exists():
            self._add_type(profile, "c")
            profile.likely_entrypoints.append("main.c")
        if (root / "CMakeLists.txt").exists():
            self._add_type(profile, "cmake")

    def _detect_other(self, root: Path, profile: ProjectProfile) -> None:
        if (root / "Cargo.toml").exists():
            self._add_type(profile, "rust")
            profile.test_signals.append("cargo test")
        if (root / "go.mod").exists():
            self._add_type(profile, "go")
            profile.test_signals.append("go test")
