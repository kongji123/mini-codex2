from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class ProjectProfile:
    detected_types: list[str] = field(default_factory=list)
    key_files: list[str] = field(default_factory=list)
    likely_entrypoints: list[str] = field(default_factory=list)
    test_signals: list[str] = field(default_factory=list)
    web_signals: list[str] = field(default_factory=list)

    def has_type(self, project_type: str) -> bool:
        return project_type in self.detected_types

    def summary(self) -> dict[str, object]:
        return {
            "detected_types": self.detected_types,
            "key_files": self.key_files,
            "likely_entrypoints": self.likely_entrypoints,
            "test_signals": self.test_signals,
            "web_signals": self.web_signals,
        }

