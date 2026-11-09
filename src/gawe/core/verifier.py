"""Post-execution verification.

A tool returning without raising is not proof that it did the right thing. Before a step may
reach `succeeded`, its output must match the tool's declared output schema and pass the named
post-condition (if the manifest declares one). Failures are reported as short codes only, never
as echoed output, so a hostile tool response cannot smuggle text into logs.

Fail closed: a manifest that names a post-condition nobody registered fails verification.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator

from gawe.core.models import ToolManifest

PostCondition = Callable[[Mapping[str, Any], Mapping[str, Any]], bool]  # (args, output) -> ok


@dataclass(frozen=True)
class VerificationResult:
    ok: bool
    failures: tuple[str, ...] = ()


class Verifier:
    def __init__(self, postconditions: Mapping[str, PostCondition] | None = None) -> None:
        self._postconditions = dict(postconditions or {})

    def verify(
        self, manifest: ToolManifest, args: Mapping[str, Any], output: Mapping[str, Any]
    ) -> VerificationResult:
        failures: list[str] = []
        validator = Draft202012Validator(manifest.output_schema)
        if any(True for _ in validator.iter_errors(dict(output))):
            failures.append("output_schema")

        name = manifest.postcondition
        if name is not None:
            check = self._postconditions.get(name)
            if check is None:
                failures.append("postcondition_unregistered")
            else:
                try:
                    passed = bool(check(args, output))
                except Exception:  # noqa: BLE001 - a crashing check must not pass
                    passed = False
                if not passed:
                    failures.append("postcondition_failed")
        return VerificationResult(ok=not failures, failures=tuple(failures))
