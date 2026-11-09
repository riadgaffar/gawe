import io
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from bench.budget import BudgetExceededError
from bench.cli import EXIT_REFUSED, main
from bench.results import STATUS_BUDGET_STOPPED, ResultsWriter, run_trials
from gawe.llm.anthropic_provider import AnthropicProvider
from tests.bench.conftest import RealModelUsedError

# --------------------------------------------------------------------- results


def test_trials_are_on_disk_as_soon_as_they_finish(tmp_path: Path) -> None:
    writer = ResultsWriter(tmp_path / "run1")
    writer.write_manifest({"model": "m", "cap": Decimal("2.5")})
    writer.append("cuad", {"case_id": "a", "status": "ok", "cost_usd": Decimal("0.01")})
    lines = (tmp_path / "run1" / "trials.jsonl").read_text().splitlines()
    assert json.loads(lines[0]) == {
        "kind": "cuad",
        "case_id": "a",
        "status": "ok",
        "cost_usd": "0.01",
    }
    assert json.loads((tmp_path / "run1" / "manifest.json").read_text())["cap"] == "2.5"


def test_a_manifest_is_never_overwritten(tmp_path: Path) -> None:
    writer = ResultsWriter(tmp_path)
    writer.write_manifest({"a": 1})
    with pytest.raises(FileExistsError):
        writer.write_manifest({"a": 2})


def test_unknown_types_are_refused_rather_than_written(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        ResultsWriter(tmp_path).append("cuad", {"x": object()})


def stopped(case: str) -> dict[str, Any]:
    return {"case_id": case, "status": STATUS_BUDGET_STOPPED}


def test_all_cases_run_when_the_budget_holds(tmp_path: Path) -> None:
    writer = ResultsWriter(tmp_path)
    done = run_trials(
        ["a", "b"],
        lambda c: {"case_id": c, "status": "ok"},
        writer=writer,
        kind="cuad",
        stopped=stopped,
    )
    assert done and [t["status"] for t in writer.trials()] == ["ok", "ok"]


def test_a_budget_stop_keeps_finished_trials_and_marks_the_rest(tmp_path: Path) -> None:
    def run_one(case: str) -> dict[str, Any]:
        if case == "c":
            raise BudgetExceededError("no money left")
        return {"case_id": case, "status": "ok"}

    writer = ResultsWriter(tmp_path)
    done = run_trials(["a", "b", "c", "d"], run_one, writer=writer, kind="cuad", stopped=stopped)
    writer.finish(complete=done, reason="budget" if not done else None, spent_usd=Decimal("1.5"))
    assert not done
    assert [(t["case_id"], t["status"]) for t in writer.trials("cuad")] == [
        ("a", "ok"),
        ("b", "ok"),
        ("c", "budget_stopped"),
        ("d", "budget_stopped"),
    ]
    status = json.loads((tmp_path / "status.json").read_text())
    assert status == {"complete": False, "reason": "budget", "spent_usd": "1.5"}


def test_other_errors_are_not_swallowed(tmp_path: Path) -> None:
    def run_one(case: str) -> dict[str, Any]:
        raise RuntimeError("harness bug")

    with pytest.raises(RuntimeError):
        run_trials(["a"], run_one, writer=ResultsWriter(tmp_path), kind="cuad", stopped=stopped)


def test_reading_trials_from_an_empty_run(tmp_path: Path) -> None:
    assert ResultsWriter(tmp_path / "nothing").trials() == []


# ------------------------------------------------------------------------- cli


def run_cli(*argv: str, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), env=env or {}, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


@pytest.mark.parametrize("command", ["run", "estimate"])
def test_commands_refuse_without_a_cap(command: str, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, _ = run_cli(command, env={"GAWE_LLM_MODEL": "claude-sonnet-5-5"})
    assert code == EXIT_REFUSED
    assert "--max-usd" in capsys.readouterr().err


@pytest.mark.parametrize("cap", ["0", "-1", "lots"])
def test_commands_refuse_a_bad_cap(cap: str) -> None:
    code, _, err = run_cli("run", "--max-usd", cap, "--model", "claude-sonnet-5-5")
    assert code == EXIT_REFUSED and "refused" in err


def test_commands_refuse_an_unknown_model() -> None:
    code, _, err = run_cli("run", "--max-usd", "5", "--model", "mystery-model")
    assert code == EXIT_REFUSED and "no price for model 'mystery-model'" in err


def test_commands_refuse_without_any_model() -> None:
    code, _, err = run_cli("estimate", "--max-usd", "5")
    assert code == EXIT_REFUSED and "GAWE_LLM_MODEL" in err


def test_bad_profiles_are_refused() -> None:
    code, _, err = run_cli(
        "run", "--max-usd", "5", "--model", "claude-sonnet-5-5", "--profiles", "presence,vibes"
    )
    assert code == EXIT_REFUSED and "--profiles" in err


def test_help_exits_cleanly(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli("--help")[0] == 0
    assert "estimate" in capsys.readouterr().out


# ----------------------------------------------------------------------- guard


def test_the_guard_stops_any_real_model_client() -> None:
    with pytest.raises(RealModelUsedError):
        AnthropicProvider(model="claude-sonnet-5-5")
    anthropic = pytest.importorskip("anthropic")
    with pytest.raises(RealModelUsedError):
        anthropic.Anthropic(api_key="not-a-key")
