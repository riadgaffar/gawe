"""Fail if docs/threat-model.md names a test that does not exist.

Looks for `tests/...py::test_name` (a function) and bare `tests/...` or `ui/tests/...` paths
(a file). Run by `make check-docs` and in CI so the threat model cannot drift from the tests.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "threat-model.md"
REF = re.compile(r"`((?:ui/)?tests/[\w/.\-]+?)(?:::(\w+))?`")


def main() -> int:
    problems: list[str] = []
    refs = REF.findall(DOC.read_text(encoding="utf-8"))
    for path, function in refs:
        file = ROOT / path
        if not file.is_file():
            problems.append(f"missing file: {path}")
        elif function and not re.search(rf"^(?:async )?def {function}\b", file.read_text(), re.M):
            problems.append(f"missing test: {path}::{function}")
    for problem in sorted(set(problems)):
        print(problem, file=sys.stderr)
    print(f"checked {len(refs)} references, {len(set(problems))} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
