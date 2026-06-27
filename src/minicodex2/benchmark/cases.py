from __future__ import annotations

import subprocess
from pathlib import Path

from minicodex2.benchmark.types import BenchmarkCase
from minicodex2.model.fake_adapter import FakeStep
from minicodex2.model.messages import ToolCall

C_COMPILER_CANDIDATES = ["gcc", "clang", "cc", "cl", "g++", "clang++", "c++"]


def built_in_suite(name: str = "smoke") -> list[BenchmarkCase]:
    if name == "smoke":
        return [python_cli_repair_case(), c_compile_repair_case()]
    if name == "extended":
        return curated_python_repair_cases() + [
            python_cli_persistence_case(),
            python_web_persistence_case(),
            c_compile_repair_case(),
            node_test_repair_case(),
        ]
    raise ValueError(f"unknown benchmark suite: {name}")


def curated_python_repair_cases() -> list[BenchmarkCase]:
    return [
        python_cli_repair_case(),
        _python_repair_case(
            name="python_reverse_string",
            module_name="text_tools",
            prompt="Fix reverse_text so the tests pass.",
            tests=(
                "from text_tools import reverse_text\n\n"
                "def test_reverse_text():\n"
                "    assert reverse_text('MiniCodex') == 'xedoCiniM'\n"
            ),
            bad_source="def reverse_text(text):\n    return text\n",
            old_text="return text",
            new_text="return text[::-1]",
            expected_snippet="return text[::-1]",
        ),
        _python_repair_case(
            name="python_slugify",
            module_name="slug",
            prompt="Fix slugify so it produces lowercase hyphenated slugs.",
            tests=(
                "from slug import slugify\n\n"
                "def test_slugify():\n"
                "    assert slugify('Hello, Mini Codex 2!') == 'hello-mini-codex-2'\n"
            ),
            bad_source=(
                "def slugify(value):\n"
                "    return value.lower().replace(' ', '_')\n"
            ),
            old_text="return value.lower().replace(' ', '_')",
            new_text=(
                "import re\n"
                "    cleaned = re.sub(r'[^a-z0-9]+', '-', value.lower()).strip('-')\n"
                "    return cleaned"
            ),
            expected_snippet="re.sub",
        ),
        _python_repair_case(
            name="python_filter_even",
            module_name="num_tools",
            prompt="Fix filter_even so it returns only even numbers.",
            tests=(
                "from num_tools import filter_even\n\n"
                "def test_filter_even():\n"
                "    assert filter_even([1, 2, 3, 4, 5, 6]) == [2, 4, 6]\n"
            ),
            bad_source="def filter_even(values):\n    return values\n",
            old_text="return values",
            new_text="return [value for value in values if value % 2 == 0]",
            expected_snippet="value % 2 == 0",
        ),
        _python_repair_case(
            name="python_json_total",
            module_name="orders",
            prompt="Fix total_prices so it sums item prices from JSON.",
            tests=(
                "from orders import total_prices\n\n"
                "def test_total_prices():\n"
                "    data = '[{\"price\": 2.5}, {\"price\": 4.0}]'\n"
                "    assert total_prices(data) == 6.5\n"
            ),
            bad_source="import json\n\n\ndef total_prices(raw):\n    return 0\n",
            old_text="return 0",
            new_text="return sum(item['price'] for item in json.loads(raw))",
            expected_snippet="json.loads",
        ),
        _python_repair_case(
            name="python_fibonacci",
            module_name="fib",
            prompt="Fix fibonacci so it returns the nth Fibonacci number.",
            tests=(
                "from fib import fibonacci\n\n"
                "def test_fibonacci():\n"
                "    assert fibonacci(0) == 0\n"
                "    assert fibonacci(1) == 1\n"
                "    assert fibonacci(7) == 13\n"
            ),
            bad_source="def fibonacci(n):\n    return n\n",
            old_text="return n",
            new_text=(
                "a, b = 0, 1\n"
                "    for _ in range(n):\n"
                "        a, b = b, a + b\n"
                "    return a"
            ),
            expected_snippet="a, b = b, a + b",
        ),
        _python_repair_case(
            name="python_file_line_count",
            module_name="files",
            prompt="Fix count_lines so it handles trailing newlines correctly.",
            tests=(
                "from pathlib import Path\n"
                "from files import count_lines\n\n"
                "def test_count_lines(tmp_path: Path):\n"
                "    path = tmp_path / 'sample.txt'\n"
                "    path.write_text('a\\nb\\n', encoding='utf-8')\n"
                "    assert count_lines(path) == 2\n"
            ),
            bad_source="def count_lines(path):\n    return len(path.read_text(encoding='utf-8').split('\\n'))\n",
            old_text="return len(path.read_text(encoding='utf-8').split('\\n'))",
            new_text="return len(path.read_text(encoding='utf-8').splitlines())",
            expected_snippet="splitlines",
        ),
        _python_repair_case(
            name="python_csv_total",
            module_name="csv_tools",
            prompt="Fix total_amount so it sums the amount column.",
            tests=(
                "from csv_tools import total_amount\n\n"
                "def test_total_amount():\n"
                "    raw = 'name,amount\\na,3\\nb,4\\n'\n"
                "    assert total_amount(raw) == 7\n"
            ),
            bad_source="import csv\nfrom io import StringIO\n\n\ndef total_amount(raw):\n    return len(list(csv.DictReader(StringIO(raw))))\n",
            old_text="return len(list(csv.DictReader(StringIO(raw))))",
            new_text="return sum(int(row['amount']) for row in csv.DictReader(StringIO(raw)))",
            expected_snippet="sum(int(row['amount'])",
        ),
        _python_repair_case(
            name="python_dedupe_preserve_order",
            module_name="collections_tools",
            prompt="Fix dedupe so it preserves first occurrence order.",
            tests=(
                "from collections_tools import dedupe\n\n"
                "def test_dedupe():\n"
                "    assert dedupe(['b', 'a', 'b', 'c', 'a']) == ['b', 'a', 'c']\n"
            ),
            bad_source="def dedupe(values):\n    return list(set(values))\n",
            old_text="return list(set(values))",
            new_text=(
                "seen = set()\n"
                "    result = []\n"
                "    for value in values:\n"
                "        if value not in seen:\n"
                "            seen.add(value)\n"
                "            result.append(value)\n"
                "    return result"
            ),
            expected_snippet="seen.add",
        ),
        _python_repair_case(
            name="python_word_count",
            module_name="words",
            prompt="Fix word_count so it counts words, not characters.",
            tests=(
                "from words import word_count\n\n"
                "def test_word_count():\n"
                "    assert word_count('one two\\nthree') == 3\n"
            ),
            bad_source="def word_count(text):\n    return len(text)\n",
            old_text="return len(text)",
            new_text="return len(text.split())",
            expected_snippet="text.split",
        ),
        _python_repair_case(
            name="python_safe_divide",
            module_name="math_tools",
            prompt="Fix safe_divide so division by zero returns None.",
            tests=(
                "from math_tools import safe_divide\n\n"
                "def test_safe_divide():\n"
                "    assert safe_divide(8, 2) == 4\n"
                "    assert safe_divide(8, 0) is None\n"
            ),
            bad_source="def safe_divide(a, b):\n    return a / b\n",
            old_text="return a / b",
            new_text="return None if b == 0 else a / b",
            expected_snippet="b == 0",
        ),
        _python_repair_case(
            name="python_flatten_once",
            module_name="flatten",
            prompt="Fix flatten_once so it flattens exactly one list level.",
            tests=(
                "from flatten import flatten_once\n\n"
                "def test_flatten_once():\n"
                "    assert flatten_once([[1, 2], [3], []]) == [1, 2, 3]\n"
            ),
            bad_source="def flatten_once(groups):\n    return groups\n",
            old_text="return groups",
            new_text="return [item for group in groups for item in group]",
            expected_snippet="for group in groups",
        ),
        _python_repair_case(
            name="python_clamp_number",
            module_name="clamp",
            prompt="Fix clamp so it constrains a number to a min/max range.",
            tests=(
                "from clamp import clamp\n\n"
                "def test_clamp():\n"
                "    assert clamp(5, 1, 10) == 5\n"
                "    assert clamp(-3, 1, 10) == 1\n"
                "    assert clamp(99, 1, 10) == 10\n"
            ),
            bad_source="def clamp(value, low, high):\n    return value\n",
            old_text="return value",
            new_text="return max(low, min(high, value))",
            expected_snippet="max(low",
        ),
        _python_repair_case(
            name="python_palindrome",
            module_name="palindrome",
            prompt="Fix is_palindrome so it ignores case and punctuation.",
            tests=(
                "from palindrome import is_palindrome\n\n"
                "def test_is_palindrome():\n"
                "    assert is_palindrome('A man, a plan, a canal: Panama') is True\n"
                "    assert is_palindrome('MiniCodex') is False\n"
            ),
            bad_source="def is_palindrome(text):\n    return text == text[::-1]\n",
            old_text="return text == text[::-1]",
            new_text=(
                "cleaned = ''.join(ch.lower() for ch in text if ch.isalnum())\n"
                "    return cleaned == cleaned[::-1]"
            ),
            expected_snippet="isalnum",
        ),
        _python_repair_case(
            name="python_parse_bool",
            module_name="bools",
            prompt="Fix parse_bool so it parses common true/false strings.",
            tests=(
                "from bools import parse_bool\n\n"
                "def test_parse_bool():\n"
                "    assert parse_bool('YES') is True\n"
                "    assert parse_bool('off') is False\n"
            ),
            bad_source="def parse_bool(text):\n    return bool(text)\n",
            old_text="return bool(text)",
            new_text=(
                "value = text.strip().lower()\n"
                "    if value in {'1', 'true', 'yes', 'on'}:\n"
                "        return True\n"
                "    if value in {'0', 'false', 'no', 'off'}:\n"
                "        return False\n"
                "    raise ValueError(f'unknown boolean: {text}')"
            ),
            expected_snippet="'off'",
        ),
        _python_repair_case(
            name="python_merge_dicts",
            module_name="dict_tools",
            prompt="Fix merge so right-hand values override left-hand values.",
            tests=(
                "from dict_tools import merge\n\n"
                "def test_merge():\n"
                "    assert merge({'a': 1, 'b': 2}, {'b': 9, 'c': 3}) == {'a': 1, 'b': 9, 'c': 3}\n"
            ),
            bad_source="def merge(left, right):\n    return left\n",
            old_text="return left",
            new_text="return {**left, **right}",
            expected_snippet="{**left, **right}",
        ),
        _python_repair_case(
            name="python_chunk_list",
            module_name="chunks",
            prompt="Fix chunks so it splits a list into fixed-size chunks.",
            tests=(
                "from chunks import chunks\n\n"
                "def test_chunks():\n"
                "    assert chunks([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]\n"
            ),
            bad_source="def chunks(values, size):\n    return [values]\n",
            old_text="return [values]",
            new_text="return [values[index:index + size] for index in range(0, len(values), size)]",
            expected_snippet="range(0, len(values), size)",
        ),
        _python_repair_case(
            name="python_temperature_conversion",
            module_name="temperature",
            prompt="Fix celsius_to_fahrenheit so it uses the correct formula.",
            tests=(
                "from temperature import celsius_to_fahrenheit\n\n"
                "def test_celsius_to_fahrenheit():\n"
                "    assert celsius_to_fahrenheit(0) == 32\n"
                "    assert celsius_to_fahrenheit(100) == 212\n"
            ),
            bad_source="def celsius_to_fahrenheit(value):\n    return value * 2\n",
            old_text="return value * 2",
            new_text="return value * 9 / 5 + 32",
            expected_snippet="9 / 5 + 32",
        ),
    ]


def python_cli_repair_case() -> BenchmarkCase:
    return _python_repair_case(
        name="python_cli_repair",
        module_name="calc",
        prompt="Implement calc.add(a, b) so the Python tests pass.",
        tests=(
            "from calc import add\n\n"
            "def test_add():\n"
            "    assert add(2, 3) == 5\n"
        ),
        bad_source="def add(a, b):\n    return a - b\n",
        old_text="return a - b",
        new_text="return a + b",
        expected_snippet="return a + b",
        description="Write a Python function, observe pytest failure, repair, and pass.",
    )


def c_compile_repair_case() -> BenchmarkCase:
    def setup(workspace: Path) -> None:
        (workspace / "README.md").write_text("Small C CLI benchmark.\n", encoding="utf-8")

    def hidden_assert(workspace: Path) -> tuple[bool, str]:
        text = (workspace / "main.c").read_text(encoding="utf-8")
        if 'printf("MiniCodex2 C OK\\n")' not in text:
            return False, "main.c did not contain the expected output"
        return True, "hidden assertion passed"

    good_program = (
        "#include <stdio.h>\n\n"
        "int main(void) {\n"
        '    printf("MiniCodex2 C OK\\n");\n'
        "    return 0;\n"
        "}\n"
    )
    bad_program = (
        "#include <stdio.h>\n\n"
        "int main(void) {\n"
        '    printf("MiniCodex2 C OK\\n")\n'
        "    return 0;\n"
        "}\n"
    )
    return BenchmarkCase(
        name="c_compile_repair",
        description="Write a C program, observe compile failure, repair, and run.",
        prompt=(
            "Fix the existing C program so it prints exactly MiniCodex2 C OK "
            "and the project verifies successfully."
        ),
        setup=setup,
        steps=[
            FakeStep(
                "write broken C program",
                [ToolCall("call_c_1", "write_file", {"path": "main.c", "content": bad_program})],
            ),
            FakeStep(
                "repair C compile failure",
                [ToolCall("call_c_2", "write_file", {"path": "main.c", "content": good_program})],
            ),
            FakeStep("fixed"),
        ],
        hidden_assert=hidden_assert,
        seed_broken=lambda workspace: (workspace / "main.c").write_text(
            bad_program,
            encoding="utf-8",
        ),
        requires_any_executable=C_COMPILER_CANDIDATES,
    )


def python_cli_persistence_case() -> BenchmarkCase:
    def setup(workspace: Path) -> None:
        (workspace / "minicodex2.toml").write_text(
            '[verification]\ncommands = ["python tests/smoke.py"]\n',
            encoding="utf-8",
        )
        (workspace / "tests").mkdir(parents=True, exist_ok=True)
        (workspace / "tests" / "smoke.py").write_text(
            "from pathlib import Path\n"
            "import subprocess\n"
            "import sys\n\n"
            "root = Path(__file__).resolve().parents[1]\n"
            "data = root / 'data.txt'\n"
            "if data.exists():\n"
            "    data.unlink()\n"
            "def run(*args):\n"
            "    return subprocess.run([sys.executable, str(root / 'notes.py'), *args], "
            "cwd=root, text=True, capture_output=True, check=True).stdout.strip()\n"
            "assert run('show') == 'hello'\n"
            "assert run('append') == 'hellohelloworld'\n"
            "assert data.read_text(encoding='utf-8') == 'hellohelloworld'\n"
            "assert run('delete') == 'hello'\n"
            "assert data.read_text(encoding='utf-8') == 'hello'\n"
            "assert run('show') == 'hello'\n"
            "print('ok')\n",
            encoding="utf-8",
        )

    def hidden_assert(workspace: Path) -> tuple[bool, str]:
        completed = subprocess.run(
            "python tests/smoke.py",
            cwd=workspace,
            shell=True,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        if completed.returncode != 0:
            return False, (completed.stdout + completed.stderr)[-1000:]
        return True, "hidden assertion passed"

    bad_source = (
        "from pathlib import Path\n"
        "import sys\n\n"
        "DATA = Path('data.txt')\n\n"
        "def load():\n"
        "    return 'hello'\n\n"
        "def save(value):\n"
        "    pass\n\n"
        "def main():\n"
        "    command = sys.argv[1] if len(sys.argv) > 1 else 'show'\n"
        "    value = load()\n"
        "    if command == 'append':\n"
        "        value = value + 'helloworld'\n"
        "    elif command == 'delete':\n"
        "        value = value.replace('helloworld', '')\n"
        "    save(value)\n"
        "    print(value)\n\n"
        "if __name__ == '__main__':\n"
        "    main()\n"
    )
    good_source = bad_source.replace(
        "def load():\n    return 'hello'\n\n"
        "def save(value):\n    pass\n",
        "def load():\n"
        "    return DATA.read_text(encoding='utf-8') if DATA.exists() else 'hello'\n\n"
        "def save(value):\n"
        "    DATA.write_text(value, encoding='utf-8')\n",
    )
    return BenchmarkCase(
        name="python_cli_persistence",
        description="Verify CLI behavior and file persistence against requirement-level smoke.",
        prompt=(
            "Fix notes.py so show starts at hello, append adds helloworld, delete removes "
            "helloworld, and all changes persist in data.txt."
        ),
        setup=setup,
        steps=[
            FakeStep(
                "write non-persistent CLI",
                [ToolCall("call_cli_persist_1", "write_file", {"path": "notes.py", "content": bad_source})],
            ),
            FakeStep(
                "repair persistence",
                [ToolCall("call_cli_persist_2", "write_file", {"path": "notes.py", "content": good_source})],
            ),
            FakeStep("fixed"),
        ],
        hidden_assert=hidden_assert,
        seed_broken=lambda workspace: (workspace / "notes.py").write_text(bad_source, encoding="utf-8"),
    )


def python_web_persistence_case() -> BenchmarkCase:
    def setup(workspace: Path) -> None:
        (workspace / "minicodex2.toml").write_text(
            '[verification]\ncommands = ["python tests/smoke.py"]\n',
            encoding="utf-8",
        )
        (workspace / "tests").mkdir(parents=True, exist_ok=True)
        (workspace / "tests" / "smoke.py").write_text(
            "from pathlib import Path\n"
            "import os\n"
            "import socket\n"
            "import subprocess\n"
            "import sys\n"
            "import time\n"
            "import urllib.parse\n"
            "import urllib.request\n\n"
            "root = Path(__file__).resolve().parents[1]\n"
            "data = root / 'data.txt'\n"
            "if data.exists():\n"
            "    data.unlink()\n"
            "sock = socket.socket()\n"
            "sock.bind(('127.0.0.1', 0))\n"
            "port = sock.getsockname()[1]\n"
            "sock.close()\n"
            "env = os.environ.copy()\n"
            "env['PORT'] = str(port)\n"
            "proc = subprocess.Popen([sys.executable, str(root / 'app.py')], cwd=root, env=env)\n"
            "base = f'http://127.0.0.1:{port}'\n"
            "try:\n"
            "    deadline = time.time() + 10\n"
            "    while True:\n"
            "        try:\n"
            "            urllib.request.urlopen(base + '/', timeout=1).read()\n"
            "            break\n"
            "        except Exception:\n"
            "            if time.time() > deadline:\n"
            "                raise\n"
            "            time.sleep(0.1)\n"
            "    def get():\n"
            "        return urllib.request.urlopen(base + '/', timeout=3).read().decode()\n"
            "    def post(path):\n"
            "        req = urllib.request.Request(base + path, data=b'', method='POST')\n"
            "        return urllib.request.urlopen(req, timeout=3).read().decode()\n"
            "    assert 'hello world' in get()\n"
            "    assert 'helloworld' in post('/append')\n"
            "    assert data.read_text(encoding='utf-8') == 'hello worldhelloworld'\n"
            "    assert 'hello world' == post('/delete')\n"
            "    assert data.read_text(encoding='utf-8') == 'hello world'\n"
            "finally:\n"
            "    proc.terminate()\n"
            "    try:\n"
            "        proc.wait(timeout=5)\n"
            "    except subprocess.TimeoutExpired:\n"
            "        proc.kill()\n"
            "        proc.wait(timeout=5)\n"
            "print('ok')\n",
            encoding="utf-8",
        )

    def hidden_assert(workspace: Path) -> tuple[bool, str]:
        completed = subprocess.run(
            "python tests/smoke.py",
            cwd=workspace,
            shell=True,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        if completed.returncode != 0:
            return False, (completed.stdout + completed.stderr)[-1000:]
        return True, "hidden assertion passed"

    bad_source = (
        "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer\n"
        "import os\n\n"
        "TEXT = 'hello world'\n\n"
        "class Handler(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self._send(TEXT)\n"
        "    def do_POST(self):\n"
        "        if self.path == '/append':\n"
        "            self._send(TEXT + 'helloworld')\n"
        "        elif self.path == '/delete':\n"
        "            self._send(TEXT)\n"
        "        else:\n"
        "            self.send_response(404); self.end_headers()\n"
        "    def _send(self, body):\n"
        "        self.send_response(200)\n"
        "        self.end_headers()\n"
        "        self.wfile.write(body.encode())\n\n"
        "if __name__ == '__main__':\n"
        "    ThreadingHTTPServer(('127.0.0.1', int(os.environ.get('PORT', '8000'))), Handler).serve_forever()\n"
    )
    good_source = (
        "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer\n"
        "from pathlib import Path\n"
        "import os\n\n"
        "DATA = Path('data.txt')\n"
        "DEFAULT = 'hello world'\n\n"
        "def load():\n"
        "    return DATA.read_text(encoding='utf-8') if DATA.exists() else DEFAULT\n\n"
        "def save(value):\n"
        "    DATA.write_text(value, encoding='utf-8')\n\n"
        "class Handler(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self._send(load())\n"
        "    def do_POST(self):\n"
        "        value = load()\n"
        "        if self.path == '/append':\n"
        "            value += 'helloworld'\n"
        "            save(value)\n"
        "            self._send(value)\n"
        "        elif self.path == '/delete':\n"
        "            value = value.replace('helloworld', '')\n"
        "            save(value)\n"
        "            self._send(value)\n"
        "        else:\n"
        "            self.send_response(404); self.end_headers()\n"
        "    def _send(self, body):\n"
        "        self.send_response(200)\n"
        "        self.end_headers()\n"
        "        self.wfile.write(body.encode())\n\n"
        "if __name__ == '__main__':\n"
        "    ThreadingHTTPServer(('127.0.0.1', int(os.environ.get('PORT', '8000'))), Handler).serve_forever()\n"
    )
    return BenchmarkCase(
        name="python_web_persistence",
        description="Verify web behavior, POST actions, and persisted state using a smoke script.",
        prompt=(
            "Fix app.py so GET / shows hello world, POST /append appends helloworld and "
            "persists it, and POST /delete removes helloworld and persists it."
        ),
        setup=setup,
        steps=[
            FakeStep(
                "write non-persistent web app",
                [ToolCall("call_web_persist_1", "write_file", {"path": "app.py", "content": bad_source})],
            ),
            FakeStep(
                "repair web persistence",
                [ToolCall("call_web_persist_2", "write_file", {"path": "app.py", "content": good_source})],
            ),
            FakeStep("fixed"),
        ],
        hidden_assert=hidden_assert,
        seed_broken=lambda workspace: (workspace / "app.py").write_text(bad_source, encoding="utf-8"),
    )


def node_test_repair_case() -> BenchmarkCase:
    def setup(workspace: Path) -> None:
        (workspace / "package.json").write_text(
            '{"scripts":{"test":"node tests/run.js"},"type":"commonjs"}\n',
            encoding="utf-8",
        )
        (workspace / "tests").mkdir(parents=True, exist_ok=True)
        (workspace / "tests" / "run.js").write_text(
            "const assert = require('assert');\n"
            "const { add } = require('../index.js');\n"
            "assert.strictEqual(add(2, 3), 5);\n"
            "console.log('ok');\n",
            encoding="utf-8",
        )

    def hidden_assert(workspace: Path) -> tuple[bool, str]:
        text = (workspace / "index.js").read_text(encoding="utf-8")
        if "return a + b" not in text:
            return False, "index.js did not contain the expected addition implementation"
        return True, "hidden assertion passed"

    return BenchmarkCase(
        name="node_test_repair",
        description="Write a Node module, observe npm test failure, repair, and pass.",
        prompt="Fix the existing index.add(a, b) implementation so npm test passes.",
        setup=setup,
        steps=[
            FakeStep(
                "write bad node implementation",
                [
                    ToolCall(
                        "call_node_1",
                        "write_file",
                        {
                            "path": "index.js",
                            "content": "function add(a, b) { return a - b; }\nmodule.exports = { add };\n",
                        },
                    )
                ],
            ),
            FakeStep(
                "repair node implementation",
                [
                    ToolCall(
                        "call_node_2",
                        "edit_file",
                        {"path": "index.js", "old_text": "return a - b", "new_text": "return a + b"},
                    )
                ],
            ),
            FakeStep("fixed"),
        ],
        hidden_assert=hidden_assert,
        seed_broken=lambda workspace: (workspace / "index.js").write_text(
            "function add(a, b) { return a - b; }\nmodule.exports = { add };\n",
            encoding="utf-8",
        ),
        requires_executable="npm",
    )


def _python_repair_case(
    *,
    name: str,
    module_name: str,
    prompt: str,
    tests: str,
    bad_source: str,
    old_text: str,
    new_text: str,
    expected_snippet: str,
    description: str | None = None,
) -> BenchmarkCase:
    module_path = f"{module_name}.py"

    def setup(workspace: Path) -> None:
        (workspace / "tests").mkdir(parents=True, exist_ok=True)
        (workspace / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        (workspace / "tests" / f"test_{module_name}.py").write_text(tests, encoding="utf-8")

    def seed_broken(workspace: Path) -> None:
        (workspace / module_path).write_text(bad_source, encoding="utf-8")

    def hidden_assert(workspace: Path) -> tuple[bool, str]:
        if not (workspace / module_path).exists():
            return False, f"{module_path} was not created"
        completed = subprocess.run(
            "python -m pytest -q",
            cwd=workspace,
            shell=True,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        if completed.returncode != 0:
            return False, (completed.stdout + completed.stderr)[-1000:]
        return True, "hidden assertion passed"

    return BenchmarkCase(
        name=name,
        description=description or f"Repair Python pytest case: {name}.",
        prompt=prompt,
        setup=setup,
        steps=[
            FakeStep(
                "write initial failing implementation",
                [ToolCall(f"{name}_write", "write_file", {"path": module_path, "content": bad_source})],
            ),
            FakeStep(
                "repair after test failure",
                [
                    ToolCall(
                        f"{name}_edit",
                        "edit_file",
                        {"path": module_path, "old_text": old_text, "new_text": new_text},
                    )
                ],
            ),
            FakeStep("fixed"),
        ],
        hidden_assert=hidden_assert,
        seed_broken=seed_broken,
    )
