from __future__ import annotations

import os
import shutil
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CCompiler:
    executable: str
    family: str

    def build_command(self, source: str, output: str) -> str:
        if self.family == "msvc":
            return f'cl /nologo {source} /Fe:"{output}"'
        return f'{self.executable} {source} -o "{output}"'


def find_c_compiler() -> CCompiler | None:
    candidates = (
        ("gcc", "gcc"),
        ("clang", "clang"),
        ("cc", "cc"),
        ("cl", "msvc"),
        ("g++", "gcc"),
        ("clang++", "clang"),
        ("c++", "cc"),
    )
    for executable, family in candidates:
        if shutil.which(executable):
            return CCompiler(executable=executable, family=family)
    return None


def default_c_compiler() -> CCompiler:
    if os.name == "nt":
        return CCompiler(executable="cl", family="msvc")
    return CCompiler(executable="gcc", family="gcc")
