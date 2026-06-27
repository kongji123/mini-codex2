from __future__ import annotations

import os
import socket
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable

from minicodex2.tools.results import CommandResult


LONG_RUNNING_HINTS = (
    "npm run dev",
    "vite",
    "flask run",
    "uvicorn",
    "streamlit run",
    "python -m http.server",
    "runserver",
)

INTERACTIVE_HINTS = (" pause", "read -p", "input(", "python -i")


def looks_long_running(command: str) -> bool:
    lower = command.lower()
    return any(hint in lower for hint in LONG_RUNNING_HINTS)


def looks_interactive(command: str) -> bool:
    lower = command.lower()
    return any(hint in lower for hint in INTERACTIVE_HINTS)


def truncate_output(text: str, limit: int = 12000) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + "\n... output truncated ...\n" + text[-half:]


class CommandRunner:
    def run(
        self,
        command: str,
        cwd: Path,
        timeout_seconds: int = 30,
        progress_callback: Callable[[dict[str, object]], None] | None = None,
    ) -> CommandResult:
        if looks_long_running(command):
            return CommandResult(
                command=command,
                cwd=str(cwd),
                exit_code=None,
                stdout="",
                stderr="",
                blocked=True,
                block_reason="command looks long-running; use start_background_command",
            )
        if looks_interactive(command):
            return CommandResult(
                command=command,
                cwd=str(cwd),
                exit_code=None,
                stdout="",
                stderr="",
                blocked=True,
                block_reason="command looks interactive",
            )
        start = time.monotonic()
        process: subprocess.Popen[str] | None = None
        try:
            process = subprocess.Popen(
                command,
                cwd=str(cwd),
                shell=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=_creation_flags(),
                start_new_session=(os.name != "nt"),
            )
            _emit_progress(
                progress_callback,
                command=command,
                cwd=str(cwd),
                timeout_seconds=timeout_seconds,
                elapsed_seconds=0.0,
                pid=process.pid,
                stage="started",
            )
            deadline = start + timeout_seconds
            next_progress_at = start + 5
            while True:
                now = time.monotonic()
                remaining = max(0.0, deadline - now)
                try:
                    stdout, stderr = process.communicate(timeout=min(1.0, remaining or 0.001))
                    break
                except subprocess.TimeoutExpired:
                    now = time.monotonic()
                    if now >= deadline:
                        _kill_process_tree(process.pid)
                        try:
                            stdout, stderr = process.communicate(timeout=5)
                        except subprocess.TimeoutExpired:
                            stdout, stderr = "", "process did not exit after timeout kill"
                        duration = time.monotonic() - start
                        _emit_progress(
                            progress_callback,
                            command=command,
                            cwd=str(cwd),
                            timeout_seconds=timeout_seconds,
                            elapsed_seconds=duration,
                            pid=process.pid,
                            stage="timed_out",
                        )
                        stderr_text = _coerce_text(stderr)
                        if stderr_text:
                            stderr_text += "\n"
                        stderr_text += f"command timed out after {timeout_seconds}s; process tree was killed"
                        return CommandResult(
                            command=command,
                            cwd=str(cwd),
                            exit_code=None,
                            stdout=truncate_output(_coerce_text(stdout)),
                            stderr=truncate_output(stderr_text),
                            timed_out=True,
                            duration_seconds=duration,
                        )
                    if now >= next_progress_at:
                        _emit_progress(
                            progress_callback,
                            command=command,
                            cwd=str(cwd),
                            timeout_seconds=timeout_seconds,
                            elapsed_seconds=now - start,
                            pid=process.pid,
                            stage="running",
                        )
                        next_progress_at = now + 5
            duration = time.monotonic() - start
            _emit_progress(
                progress_callback,
                command=command,
                cwd=str(cwd),
                timeout_seconds=timeout_seconds,
                elapsed_seconds=duration,
                pid=process.pid,
                stage="finished",
            )
            return CommandResult(
                command=command,
                cwd=str(cwd),
                exit_code=process.returncode,
                stdout=truncate_output(_coerce_text(stdout)),
                stderr=truncate_output(_coerce_text(stderr)),
                duration_seconds=duration,
            )
        except Exception:
            if process is not None and process.poll() is None:
                _kill_process_tree(process.pid)
            raise


def _creation_flags() -> int:
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def _kill_process_tree(pid: int) -> None:
    if os.name == "nt":
        completed = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            lowered = detail.lower()
            if (
                "no running instance" in lowered
                or "not found" in lowered
                or "could not be found" in lowered
                or "找不到" in detail
                or "没有运行" in detail
            ):
                return
            raise RuntimeError(f"taskkill failed for PID {pid}: {detail}")
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return


def _emit_progress(
    callback: Callable[[dict[str, object]], None] | None,
    *,
    command: str,
    cwd: str,
    timeout_seconds: int,
    elapsed_seconds: float,
    pid: int,
    stage: str,
) -> None:
    if callback is None:
        return
    callback(
        {
            "command": command,
            "cwd": cwd,
            "timeout_seconds": timeout_seconds,
            "elapsed_seconds": round(elapsed_seconds, 1),
            "pid": pid,
            "stage": stage,
        }
    )


def _coerce_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def is_port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _read_tail(path: Path, limit: int = 4000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(text) <= limit:
        return text
    return text[-limit:]


class PortManager:
    def inspect(self, port: int) -> dict[str, object]:
        pids = self.pids_using_port(port)
        return {"port": port, "in_use": bool(pids), "pids": pids}

    def release(self, port: int, *, timeout_seconds: float = 5) -> dict[str, object]:
        before = self.pids_using_port(port)
        candidates = self._release_candidate_pids(before)
        killed: list[int] = []
        errors: list[str] = []
        for pid in candidates:
            try:
                self.kill_process_tree(pid)
                killed.append(pid)
            except Exception as exc:  # pragma: no cover - platform-specific edge
                errors.append(f"{pid}: {exc}")
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline and self.pids_using_port(port):
            time.sleep(0.1)
        after = self.pids_using_port(port)
        return {
            "port": port,
            "pids_before": before,
            "killed_pids": killed,
            "pids_after": after,
            "released": not after,
            "errors": errors,
        }

    def _release_candidate_pids(self, pids: list[int]) -> list[int]:
        candidates = list(pids)
        if os.name == "nt":
            candidates.extend(self._windows_descendant_pids(pids))
        return sorted(set(candidates))

    def pids_using_port(self, port: int) -> list[int]:
        if os.name == "nt":
            return self._windows_pids_using_port(port)
        return self._posix_pids_using_port(port)

    @staticmethod
    def kill_process_tree(pid: int) -> None:
        if os.name == "nt":
            completed = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "").strip()
                raise RuntimeError(f"taskkill failed for PID {pid}: {detail}")
            return
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except ProcessLookupError:
            return
        except OSError:
            os.kill(pid, signal.SIGTERM)

    @staticmethod
    def _windows_pids_using_port(port: int) -> list[int]:
        completed = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        pids: set[int] = set()
        suffix = f":{port}"
        for line in completed.stdout.splitlines():
            parts = line.split()
            if len(parts) < 5:
                continue
            local_address = parts[1]
            state = parts[3]
            pid_text = parts[4]
            if not local_address.endswith(suffix):
                continue
            if state.upper() != "LISTENING":
                continue
            if pid_text.isdigit() and int(pid_text) > 0:
                pids.add(int(pid_text))
        return sorted(pids)

    def _windows_descendant_pids(self, parent_pids: list[int]) -> list[int]:
        descendants: set[int] = set()
        queue = list(parent_pids)
        while queue:
            parent = queue.pop(0)
            for child in self._windows_child_pids(parent):
                if child in descendants or child in parent_pids:
                    continue
                descendants.add(child)
                queue.append(child)
        return sorted(descendants)

    @staticmethod
    def _windows_child_pids(parent_pid: int) -> list[int]:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    "Get-CimInstance Win32_Process "
                    f"| Where-Object {{ $_.ParentProcessId -eq {parent_pid} }} "
                    "| Select-Object -ExpandProperty ProcessId"
                ),
            ],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        pids: list[int] = []
        for line in completed.stdout.splitlines():
            stripped = line.strip()
            if stripped.isdigit():
                pids.append(int(stripped))
        return sorted(set(pids))

    @staticmethod
    def _posix_pids_using_port(port: int) -> list[int]:
        completed = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}"],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        pids = []
        for line in completed.stdout.splitlines():
            stripped = line.strip()
            if stripped.isdigit():
                pids.append(int(stripped))
        return sorted(set(pids))


class BackgroundCommandManager:
    def __init__(self) -> None:
        self.processes: dict[int, subprocess.Popen[str]] = {}
        self.ports = PortManager()

    def start(
        self,
        command: str,
        cwd: Path,
        log_path: Path,
        *,
        port: int | None = None,
        ready_timeout_seconds: int = 15,
        restart_port: bool = False,
    ) -> dict[str, object]:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if port and is_port_open(port):
            if restart_port:
                release = self.ports.release(port)
                if not release["released"]:
                    return {
                        "pid": None,
                        "command": command,
                        "port": port,
                        "ready": False,
                        "log_path": str(log_path),
                        "error": f"port {port} could not be released before starting command",
                        "release": release,
                    }
            else:
                occupants = self.ports.inspect(port)
                return {
                    "pid": None,
                    "command": command,
                    "port": port,
                    "ready": False,
                    "log_path": str(log_path),
                    "error": f"port {port} is already in use before starting command",
                    "occupants": occupants,
                }
        log_file = log_path.open("w", encoding="utf-8")
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            shell=True,
            text=True,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=os.environ.copy(),
        )
        self.processes[process.pid] = process
        ready = False
        if port:
            deadline = time.monotonic() + ready_timeout_seconds
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                if is_port_open(port):
                    ready = True
                    break
                time.sleep(0.2)
        return {
            "pid": process.pid,
            "command": command,
            "port": port,
            "ready": ready if port else process.poll() is None,
            "log_path": str(log_path),
            "exit_code": process.poll(),
            "log_tail": _read_tail(log_path),
        }

    def terminate_all(self) -> list[dict[str, object]]:
        terminated: list[dict[str, object]] = []
        for process in list(self.processes.values()):
            entry: dict[str, object] = {"pid": process.pid, "was_running": process.poll() is None}
            if process.poll() is None:
                if os.name == "nt":
                    completed = subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        text=True,
                        capture_output=True,
                        timeout=5,
                    )
                    entry.update(
                        {
                            "terminated": completed.returncode == 0,
                            "exit_code": completed.returncode,
                            "stdout": completed.stdout[-500:],
                            "stderr": completed.stderr[-500:],
                        }
                    )
                else:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                        entry["terminated"] = True
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
                        entry["terminated"] = True
            else:
                entry["terminated"] = False
                entry["exit_code"] = process.poll()
            terminated.append(entry)
        self.processes.clear()
        return terminated
