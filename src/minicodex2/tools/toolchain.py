from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ProgressCallback = Callable[[str, dict[str, object]], None]


@dataclass(frozen=True, slots=True)
class ToolchainSpec:
    name: str
    command: str
    winget_id: str | None = None
    official_downloads_url: str | None = None


TOOLCHAINS: dict[str, ToolchainSpec] = {
    "go": ToolchainSpec("go", "go", "GoLang.Go", "https://go.dev/dl/?mode=json"),
}


class ToolchainManager:
    def inspect(self, name: str) -> dict[str, object]:
        spec = self._spec(name)
        executable = shutil.which(spec.command)
        version = None
        if executable:
            version = self._version(spec.command)
        return {
            "name": spec.name,
            "command": spec.command,
            "available": bool(executable),
            "path": executable,
            "version": version,
            "installers": self._available_installers(spec),
        }

    def install(
        self,
        name: str,
        *,
        installer: str = "auto",
        artifact_root: Path | None = None,
        progress: ProgressCallback | None = None,
    ) -> dict[str, object]:
        spec = self._spec(name)
        _emit_progress(progress, "inspect_before", name=spec.name)
        before = self.inspect(spec.name)
        if before["available"]:
            _emit_progress(progress, "already_available", name=spec.name, path=before.get("path"))
            return {
                "name": spec.name,
                "installed": False,
                "already_available": True,
                "before": before,
                "after": before,
                "message": f"{spec.command} is already available.",
            }

        selected = self._select_installer(spec, installer)
        _emit_progress(
            progress,
            "select_installer",
            name=spec.name,
            requested_installer=installer,
            selected_installer=selected,
            available_installers=before["installers"],
        )
        if selected is None:
            _emit_progress(progress, "no_installer", name=spec.name)
            return {
                "name": spec.name,
                "installed": False,
                "already_available": False,
                "before": before,
                "after": before,
                "error": "No supported installer is available. Install the toolchain manually and restart the terminal.",
            }

        if selected == "winget":
            result = self._install_with_winget(spec, progress)
        elif selected == "official_msi":
            result = self._install_go_with_official_msi(spec, artifact_root, progress)
        else:  # pragma: no cover - guarded by _select_installer
            result = {
                "ok": False,
                "command": "",
                "exit_code": None,
                "stdout": "",
                "stderr": f"Unsupported installer: {selected}",
            }
        _emit_progress(progress, "inspect_after", name=spec.name)
        after = self.inspect(spec.name)
        _emit_progress(
            progress,
            "finished",
            name=spec.name,
            installed=bool(result["ok"]),
            available=bool(after["available"]),
            message=self._install_message(spec, bool(result["ok"]), after),
        )
        return {
            "name": spec.name,
            "installer": selected,
            "installed": bool(result["ok"]),
            "already_available": False,
            "before": before,
            "after": after,
            "command": result["command"],
            "exit_code": result["exit_code"],
            "stdout": result["stdout"],
            "stderr": result["stderr"],
            "message": self._install_message(spec, bool(result["ok"]), after),
        }

    def _spec(self, name: str) -> ToolchainSpec:
        normalized = name.strip().lower()
        if normalized not in TOOLCHAINS:
            supported = ", ".join(sorted(TOOLCHAINS))
            raise ValueError(f"unsupported toolchain: {name}. Supported: {supported}")
        return TOOLCHAINS[normalized]

    def _available_installers(self, spec: ToolchainSpec) -> list[str]:
        installers: list[str] = []
        if spec.winget_id and os.name == "nt" and shutil.which("winget"):
            installers.append("winget")
        if spec.name == "go" and os.name == "nt" and spec.official_downloads_url:
            installers.append("official_msi")
        return installers

    def _select_installer(self, spec: ToolchainSpec, installer: str) -> str | None:
        available = self._available_installers(spec)
        requested = installer.strip().lower()
        if requested == "auto":
            return available[0] if available else None
        if requested in available:
            return requested
        return None

    @staticmethod
    def _version(command: str) -> str | None:
        completed = subprocess.run(
            [command, "version"],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        output = (completed.stdout or completed.stderr).strip()
        return output or None

    @staticmethod
    def _install_with_winget(
        spec: ToolchainSpec, progress: ProgressCallback | None = None
    ) -> dict[str, object]:
        if not spec.winget_id:
            return {
                "ok": False,
                "command": "",
                "exit_code": None,
                "stdout": "",
                "stderr": f"{spec.name} has no winget package configured.",
            }
        command = [
            "winget",
            "install",
            "--id",
            spec.winget_id,
            "--source",
            "winget",
            "--accept-package-agreements",
            "--accept-source-agreements",
        ]
        _emit_progress(progress, "run_installer", name=spec.name, command=" ".join(command))
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=900,
            check=False,
        )
        return {
            "ok": completed.returncode == 0,
            "command": " ".join(command),
            "exit_code": completed.returncode,
            "stdout": completed.stdout[-4000:],
            "stderr": completed.stderr[-4000:],
        }

    @staticmethod
    def _install_go_with_official_msi(
        spec: ToolchainSpec,
        artifact_root: Path | None,
        progress: ProgressCallback | None = None,
    ) -> dict[str, object]:
        if not spec.official_downloads_url:
            return {
                "ok": False,
                "command": "",
                "exit_code": None,
                "stdout": "",
                "stderr": f"{spec.name} has no official download URL configured.",
            }
        download_root = artifact_root or Path.cwd()
        download_dir = download_root / "downloads" / "toolchains"
        download_dir.mkdir(parents=True, exist_ok=True)
        try:
            _emit_progress(
                progress,
                "download_metadata",
                name=spec.name,
                url=spec.official_downloads_url,
            )
            with urllib.request.urlopen(spec.official_downloads_url, timeout=30) as response:
                releases = json.loads(response.read().decode("utf-8"))
            msi_url = _select_go_windows_amd64_msi(releases)
            msi_path = download_dir / msi_url.rsplit("/", 1)[-1]
            _emit_progress(progress, "download_installer", name=spec.name, url=msi_url, path=str(msi_path))
            _download_file(msi_url, msi_path, progress, spec.name)
        except Exception as exc:
            _emit_progress(progress, "download_failed", name=spec.name, error=str(exc))
            return {
                "ok": False,
                "command": f"download {spec.official_downloads_url}",
                "exit_code": None,
                "stdout": "",
                "stderr": f"official MSI download failed: {exc}",
            }

        command = ["msiexec", "/i", str(msi_path), "/passive", "/norestart"]
        _emit_progress(progress, "run_installer", name=spec.name, command=" ".join(command))
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=900,
            check=False,
        )
        return {
            "ok": completed.returncode == 0,
            "command": " ".join(command),
            "exit_code": completed.returncode,
            "stdout": completed.stdout[-4000:],
            "stderr": completed.stderr[-4000:],
            "downloaded": str(msi_path),
        }

    @staticmethod
    def _install_message(spec: ToolchainSpec, ok: bool, after: dict[str, object]) -> str:
        if not ok:
            return f"{spec.name} installation failed. Review stdout/stderr and install manually if needed."
        if after["available"]:
            return f"{spec.name} installed and {spec.command} is available."
        return (
            f"{spec.name} installer completed, but {spec.command} is not visible in this process PATH yet. "
            "Restart the terminal or TUI, then rerun verification."
        )


def _emit_progress(progress: ProgressCallback | None, stage: str, **payload: object) -> None:
    if progress:
        progress(stage, payload)


def _download_file(
    url: str,
    path: Path,
    progress: ProgressCallback | None,
    name: str,
    *,
    timeout_seconds: int = 60,
) -> None:
    with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
        total_header = response.headers.get("Content-Length")
        total = int(total_header) if total_header and total_header.isdigit() else None
        written = 0
        with path.open("wb") as file:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                file.write(chunk)
                written += len(chunk)
                _emit_progress(
                    progress,
                    "download_progress",
                    name=name,
                    bytes_downloaded=written,
                    bytes_total=total,
                    path=str(path),
                )


def _select_go_windows_amd64_msi(releases: list[dict[str, object]]) -> str:
    for release in releases:
        if not release.get("stable", False):
            continue
        for file_info in release.get("files", []):
            if not isinstance(file_info, dict):
                continue
            if (
                file_info.get("os") == "windows"
                and file_info.get("arch") == "amd64"
                and file_info.get("kind") == "installer"
                and str(file_info.get("filename", "")).endswith(".msi")
            ):
                return "https://go.dev/dl/" + str(file_info["filename"])
    raise ValueError("no stable Windows amd64 MSI found in Go downloads feed")
