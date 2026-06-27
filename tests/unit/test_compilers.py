from __future__ import annotations

from minicodex2.verification.compilers import CCompiler, find_c_compiler


def test_find_c_compiler_supports_msvc(monkeypatch) -> None:
    def fake_which(executable: str) -> str | None:
        return "C:/BuildTools/cl.exe" if executable == "cl" else None

    monkeypatch.setattr("shutil.which", fake_which)

    compiler = find_c_compiler()

    assert compiler == CCompiler(executable="cl", family="msvc")
    assert compiler.build_command("main.c", ".minicodex2/build/main.exe") == (
        'cl /nologo main.c /Fe:".minicodex2/build/main.exe"'
    )


def test_find_c_compiler_supports_cpp_fallback(monkeypatch) -> None:
    def fake_which(executable: str) -> str | None:
        return "/usr/bin/g++" if executable == "g++" else None

    monkeypatch.setattr("shutil.which", fake_which)

    compiler = find_c_compiler()

    assert compiler == CCompiler(executable="g++", family="gcc")
    assert compiler.build_command("main.c", ".minicodex2/build/main") == (
        'g++ main.c -o ".minicodex2/build/main"'
    )
