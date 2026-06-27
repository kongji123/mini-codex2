from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class SafePath:
    path: Path
    relative: str


class PathSafety:
    def __init__(self, workspace_root: str | Path, projects_root: str | Path | None = None) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.projects_root = Path(projects_root).resolve() if projects_root else None

    def resolve_workspace_path(self, value: str | Path) -> SafePath:
        raw = Path(value)
        path = raw.resolve() if raw.is_absolute() else (self.workspace_root / raw).resolve()
        if not self._inside(path, self.workspace_root):
            raise ValueError(f"path is outside workspace: {path}")
        return SafePath(path=path, relative=path.relative_to(self.workspace_root).as_posix())

    def resolve_new_project_path(self, value: str | Path) -> Path:
        raw = Path(value)
        path = raw.resolve() if raw.is_absolute() else (self.workspace_root / raw).resolve()
        if self._inside(path, self.workspace_root):
            return path
        if self.projects_root and self._inside(path, self.projects_root):
            return path
        raise ValueError(f"new project path is outside allowed roots: {path}")

    @staticmethod
    def _inside(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

