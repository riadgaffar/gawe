"""The whole benchmark flow, offline: fixture CUAD data, a fake token counter, a fake model."""

import inspect
import io
import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from bench.budget import Budget, CountingProvider, Price
from bench.cli import EXIT_OK, EXIT_REFUSED, EXIT_STOPPED, main
from bench.count import AnthropicTokenCounter, largest_findings, reader_request
from bench.cuad.labels import load_corpus
from bench.cuad.profile import PRESENCE
from bench.cuad.run import plan_trials, run_cuad
from bench.estimate import (
    PLAN_ATTEMPTS,
    READ_ATTEMPTS,
    EstimateMismatchError,
    PurposeStats,
    TrialStats,
    Unit,
    build_estimate,
    load_estimate,
    remaining,
    trial_contracts,
)
from bench.harness import Deps
from bench.injection.cases import load_suite
from bench.injection.run import drive
from bench.report import DOES_NOT_PROVE
from bench.results import ResultsWriter
from gawe.apps.contract_review.reader_schema import PROFILE as CR_PROFILE
from gawe.apps.it_access.reader_schema import PROFILE as IT_PROFILE
from gawe.llm.planner import Planner
from gawe.llm.provider import LLMOutputError, LLMRequest, LLMResponse, ProviderConfigError
from gawe.llm.reader import Reader

FIXTURE = Path(__file__).parent / "fixtures" / "cuad"
MODEL = "claude-sonnet-5-5"
PRICE = Price("test-model", Decimal("2"), Decimal("10"), date(2026, 10, 29), "test")
SMALL = ["--contracts", "3", "--min-positives", "0", "--cuad-repeats", "1"]
SMALL += ["--injection-repeats", "1"]


class FakeModel:
    """Answers every request validly for its app, so whole runs complete offline."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        return LLMResponse(
            text=json.dumps(self._answer(request)),
            model="fake-sonnet",
            input_tokens=900,
            output_tokens=120,
        )

    def _answer(self, request: LLMRequest) -> dict[str, Any]:
        properties = request.output_schema.get("properties", {})
        if request.purpose == "read" and "requester_email" in properties:
            return {
                "requester_email": "dana.ortiz@acme.example",
                "system": "wiki",
                "level": "read",
                "justification": "Needs it.",
            }
        if request.purpose == "read":
            return {
                "parties": ["A", "B"],
                "risk_level": "medium",
                "flagged_clauses": [{"kind": "non_compete", "severity": "medium"}],
                "summary": "A contract.",
            }
        contract = re.search(r"C-\d{4}", request.user)
        if contract:
            cid = contract.group(0)
            return {
                "steps": [
                    {"id": "get", "tool": "clm.get_contract", "args": {"contract_id": cid}},
                    {
                        "id": "rec",
                        "tool": "crm.write_assessment",
                        "args": {"contract_id": cid, "risk_level": "medium", "summary": "Done."},
                        "depends_on": ["get"],
                    },
                ]
            }
        who = "dana.ortiz@acme.example"
        return {
            "steps": [
                {"id": "look", "tool": "iam.lookup_user", "args": {"email": who}},
                {
                    "id": "grant",
                    "tool": "iam.grant_access",
                    "args": {"email": who, "system": "wiki", "level": "read"},
                    "depends_on": ["look"],
                },
            ]
        }


class FakeCounter:
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def count(self, request: LLMRequest) -> int:
        self.requests.append(request)
        return (len(request.system) + len(request.user)) // 4


def no_model(model: str) -> Any:
    raise AssertionError("this command must not call the model")


def make_deps(tmp_path: Path, **overrides: Any) -> Deps:
    stamps = iter(range(100))

    def now() -> datetime:
        return datetime(2026, 11, 8, 12, 0, next(stamps), tzinfo=UTC)

    base: dict[str, Any] = {
        "data_root": lambda: FIXTURE,
        "archive_sha256": lambda: "ab" * 32,
        "counter": lambda model: FakeCounter(),
        "provider": lambda model: FakeModel(),
        "results_dir": tmp_path / "results",
        "confirm": lambda prompt: True,
        "now": now,
        "git": lambda: ("deadbeef", False),
        "sleep": lambda seconds: None,
    }
    return Deps(**(base | overrides))


def cli(deps: Deps, *argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), env={"GAWE_LLM_MODEL": MODEL}, out=out, err=err, deps=deps)
    return code, out.getvalue(), err.getvalue()


def run_dirs(deps: Deps) -> list[Path]:
    root = deps.results_dir
    if not root.exists():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and p.name[0].isdigit())


# ------------------------------------------------------------------- estimate


def test_the_estimate_arithmetic() -> None:
    unit = Unit("cuad", "c", "presence", "read", 1000, 2048, attempts=2, repeats=3)
    estimate = build_estimate([unit], PRICE)
    # Input: 3 x 1,000 x 1.05 tokens at $2/M. Ceiling: 3 x 2 x (1,050 in + 2,048 out).
    assert estimate.input_usd == Decimal("0.0063")
    assert estimate.ceiling_usd == Decimal("0.13548")
    assert estimate.expected_usd is None
    stats = TrialStats(
        spent_usd=Decimal("0.01"),
        by_purpose={"read": PurposeStats(4, 6, 1.5, 500.0, 0, 0)},
        models=("m",),
    )
    # Expected: 3 x 1.5 x (1,050 in + 500 out), plus the trial's own spend.
    assert build_estimate([unit], PRICE, stats).expected_usd == Decimal("0.04195")


def test_attempt_limits_match_the_reader_and_planner() -> None:
    reader = inspect.signature(Reader).parameters["max_attempts"].default
    planner = inspect.signature(Planner).parameters["max_attempts"].default
    assert (reader, planner) == (READ_ATTEMPTS, PLAN_ATTEMPTS)


def test_trial_contracts_are_shortest_median_longest() -> None:
    cases = load_corpus(FIXTURE).cases
    picked = trial_contracts(cases)
    assert [c.chars for c in picked] == sorted(c.chars for c in cases)
    assert trial_contracts(cases[:1]) == [cases[0]] and trial_contracts([]) == []


def test_largest_findings_fill_every_field_and_stay_valid() -> None:
    from jsonschema import Draft202012Validator

    for profile in (CR_PROFILE, IT_PROFILE):
        findings = largest_findings(profile)
        assert not list(Draft202012Validator(profile.schema).iter_errors(dict(findings.data)))
    contract = largest_findings(CR_PROFILE).data
    assert len(contract["flagged_clauses"]) == 12 and len(contract["summary"]) == 300


def test_the_counter_sends_what_a_real_call_would_minus_max_tokens() -> None:
    class Client:
        def __init__(self) -> None:
            self.sent: dict[str, Any] = {}
            self.messages = self

        def count_tokens(self, **kwargs: Any) -> Any:
            self.sent = kwargs
            return type("Count", (), {"input_tokens": 1234})()

    client = Client()
    request = reader_request(PRESENCE, "A short contract.")
    assert AnthropicTokenCounter(MODEL, client=client).count(request) == 1234
    assert client.sent["model"] == MODEL and "max_tokens" not in client.sent
    assert client.sent["output_config"]["format"]["type"] == "json_schema"
    assert client.sent["messages"][0]["content"] == request.user


def test_estimate_spends_nothing_and_saves_a_fingerprinted_file(tmp_path: Path) -> None:
    deps = make_deps(tmp_path, provider=no_model)
    code, out, _ = cli(deps, "estimate", "--max-usd", "1", *SMALL)
    assert code == EXIT_OK
    assert "input cost (exact count + 5%)" in out and "hard ceiling" in out
    assert "expected total" not in out
    (saved,) = (deps.results_dir / "estimates").iterdir()
    body = json.loads(saved.read_text())
    assert body["fingerprint"]["model"] == MODEL and body["trial"] is None
    assert body["units"] == 3 * 2 + 33 * 2  # contracts x profiles reads; each case reads and plans


def test_a_paid_trial_measures_output_and_gives_an_expected_total(tmp_path: Path) -> None:
    deps = make_deps(tmp_path)
    code, out, _ = cli(deps, "estimate", "--max-usd", "1", "--trial", *SMALL)
    assert code == EXIT_OK and "expected total (from the trial)" in out
    assert "reads averaged 1.00 attempts and 120 output tokens" in out
    (saved,) = (deps.results_dir / "estimates").iterdir()
    trial = json.loads(saved.read_text())["trial"]
    assert trial["by_purpose"]["read"]["calls"] == 3 * 2 + 2  # contracts x profiles, two twins
    assert trial["by_purpose"]["plan"]["calls"] == 2 and trial["models"] == ["fake-sonnet"]


# ------------------------------------------------------------------------ run


def test_a_full_run_writes_results_and_a_report(tmp_path: Path) -> None:
    deps = make_deps(tmp_path)
    code, out, _ = cli(deps, "run", "--max-usd", "5", "--yes", *SMALL)
    assert code == EXIT_OK, out
    (directory,) = run_dirs(deps)
    trials = ResultsWriter(directory).trials()
    order = [(t["kind"], t.get("profile")) for t in trials]
    first_injection = order.index(("injection", None))
    assert all(k == ("cuad", "presence") for k in order[:first_injection])
    assert order[-1] == ("cuad", "production")  # production last: the first thing a cap cuts
    status = json.loads((directory / "status.json").read_text())
    assert status["complete"] and status["models_reported"] == ["fake-sonnet"]
    manifest = json.loads((directory / "manifest.json").read_text())
    assert manifest["git_commit"] == "deadbeef" and manifest["dataset"]["archive_sha256"]
    assert len(manifest["sample"]["cases"]) == 3
    report = (directory / "report.md").read_text()
    for heading in ("## 1.", "## 2.", "## 3.", "## 4.", "## 5.", "## 6. What this", "## 7."):
        assert heading in report
    assert all(item in report for item in DOES_NOT_PROVE)
    assert "Incomplete run" not in report
    assert "- None: no attack fooled the model" in report  # the fake model is never fooled
    assert "- **Spend cap:** $5." in report and "- Code: commit `deadbeef`." in report
    assert "What policy decided for each step of the clean runs" in report
    twins = [t for t in trials if t["kind"] == "injection" and t.get("twin")]
    risks = {t["app"]: t["assessment_risk"] for t in twins}
    assert risks["it_access"] is None and risks["contract_review"] in ("low", "medium", "high")
    assert "- Risk levels the clean contract runs wrote:" in report


def test_the_scripted_command_needs_no_model_and_writes_its_own_report(tmp_path: Path) -> None:
    deps = make_deps(tmp_path, provider=no_model, counter=no_model, data_root=no_model)
    code, out, err = cli(deps, "scripted")
    assert code == EXIT_OK, err
    assert "no model is called" in out
    (directory,) = run_dirs(deps)
    assert directory.name.endswith("-scripted")
    manifest = json.loads((directory / "manifest.json").read_text())
    assert manifest["kind"] == "scripted" and manifest["model"] is None
    assert manifest["git_commit"] == "deadbeef" and manifest["injection"]["trials"] == 38
    status = json.loads((directory / "status.json").read_text())
    assert status["complete"] and Decimal(str(status["spent_usd"])) == 0
    assert len(ResultsWriter(directory).trials("injection_scripted")) == 38
    report = (directory / "report.md").read_text()
    assert "| Goal | Policy as written | Planner fooled | Reader fooled |" in report
    assert "| `exfil_blocked` | deny | denied by policy (5 of 5) | - |" in report
    assert "## 3. What this does not show" in report and "no model was called" in report
    before = report
    (directory / "report.md").unlink()
    assert cli(deps, "report", str(directory))[0] == EXIT_OK
    assert (directory / "report.md").read_text() == before


def test_the_scripted_command_takes_no_cap_or_model(tmp_path: Path) -> None:
    deps = make_deps(tmp_path, provider=no_model)
    code, _, _ = cli(deps, "scripted", "--max-usd", "1")
    assert code == EXIT_REFUSED and run_dirs(deps) == []


def test_every_report_number_comes_from_report_json(tmp_path: Path) -> None:
    deps = make_deps(tmp_path)
    cli(deps, "run", "--max-usd", "5", "--yes", *SMALL)
    (directory,) = run_dirs(deps)
    report = json.loads((directory / "report.json").read_text())
    from bench.report import render_markdown

    assert render_markdown(report) == (directory / "report.md").read_text()


def test_the_report_command_regenerates_without_the_data(tmp_path: Path) -> None:
    deps = make_deps(tmp_path)
    cli(deps, "run", "--max-usd", "5", "--yes", *SMALL)
    (directory,) = run_dirs(deps)
    before = (directory / "report.md").read_text()
    (directory / "report.md").unlink()
    code, out, _ = cli(deps, "report", str(directory))
    assert code == EXIT_OK and (directory / "report.md").read_text() == before
    assert cli(deps, "report", str(tmp_path))[0] == EXIT_REFUSED


def test_a_ceiling_over_the_cap_is_refused_before_anything_is_written(tmp_path: Path) -> None:
    deps = make_deps(tmp_path, provider=no_model)
    code, _, err = cli(deps, "run", "--max-usd", "0.5", "--yes", *SMALL)  # input $0.15, ceiling ~$4
    assert code == EXIT_REFUSED and "worst case" in err and run_dirs(deps) == []


def test_input_alone_over_the_cap_is_always_refused(tmp_path: Path) -> None:
    deps = make_deps(tmp_path, provider=no_model)
    code, _, err = cli(
        deps, "run", "--max-usd", "0.0001", "--yes", "--accept-ceiling-over-cap", *SMALL
    )
    assert code == EXIT_REFUSED and "input alone" in err


def test_a_run_that_is_not_confirmed_spends_nothing(tmp_path: Path) -> None:
    deps = make_deps(tmp_path, provider=no_model, confirm=lambda prompt: False)
    code, _, err = cli(deps, "run", "--max-usd", "5", *SMALL)
    assert code == EXIT_REFUSED and "nothing was spent" in err and run_dirs(deps) == []


def test_a_run_stopped_by_the_cap_still_reports(tmp_path: Path) -> None:
    deps = make_deps(tmp_path)
    code, out, _ = cli(
        deps, "run", "--max-usd", "0.2", "--yes", "--accept-ceiling-over-cap", *SMALL
    )
    assert code == EXIT_STOPPED
    (directory,) = run_dirs(deps)
    status = json.loads((directory / "status.json").read_text())
    assert not status["complete"] and Decimal(status["spent_usd"]) <= Decimal("0.2")
    assert "Incomplete run" in (directory / "report.md").read_text()


def test_a_saved_estimate_is_reused_only_for_the_same_run(tmp_path: Path) -> None:
    deps = make_deps(tmp_path)
    cli(deps, "estimate", "--max-usd", "1", *SMALL)
    (saved,) = (deps.results_dir / "estimates").iterdir()
    deps_no_count = make_deps(tmp_path, counter=no_model)
    code, out, _ = cli(
        deps_no_count, "run", "--max-usd", "5", "--yes", "--estimate-from", str(saved), *SMALL
    )
    assert code == EXIT_OK and "ok to start" in out
    code, _, err = cli(
        deps_no_count,
        "run",
        "--max-usd",
        "5",
        "--yes",
        "--estimate-from",
        str(saved),
        *SMALL[:-1],
        "2",
    )
    assert code == EXIT_REFUSED and "injection_repeats" in err
    with pytest.raises(EstimateMismatchError):
        load_estimate(saved, fingerprint={})


def test_missing_data_is_refused_clearly(tmp_path: Path) -> None:
    from bench.cuad.fetch import UnpinnedArchiveError

    def unpinned() -> Path:
        raise UnpinnedArchiveError("cd" * 32)

    code, _, err = cli(
        make_deps(tmp_path, data_root=unpinned), "estimate", "--max-usd", "1", *SMALL
    )
    assert code == EXIT_REFUSED and "PINNED_SHA256" in err


# ---------------------------------------------------------------- early stops


class StopsEarly:
    """A model that runs out of output tokens before answering."""

    def complete(self, request: LLMRequest) -> LLMResponse:
        raise LLMOutputError("the model stopped early (max_tokens)")


def test_early_stops_are_charged_at_worst_case_counted_and_retried(tmp_path: Path) -> None:
    budget = Budget(cap=Decimal(1), price=PRICE)
    provider = CountingProvider(StopsEarly(), budget, input_tokens=lambda r: 1000)
    writer = ResultsWriter(tmp_path)
    cases = load_corpus(FIXTURE).cases[:1]
    trials = plan_trials(cases, ("presence",), 1)
    run_cuad(FIXTURE, trials, provider=provider, profiles={"presence": PRESENCE}, writer=writer)
    (trial,) = writer.trials()
    assert trial["status"] == "reader_error" and trial["early_stops"] == 2  # both attempts
    # Each charged as 1,000 input + all 2,048 output tokens.
    assert budget.spent == Decimal("0.04496") and all(c.early_stop for c in provider.history)


def test_early_stops_inside_a_run_end_it_cleanly() -> None:
    budget = Budget(cap=Decimal(1), price=PRICE)
    provider = CountingProvider(StopsEarly(), budget)
    twin = next(c for c in load_suite().twins if c.base.name == "northwind")
    result = drive(twin, provider, pytest.fail)  # never left stalled, so never resumed
    assert result["status"] == "reader_error" and result["run_status"] == "failed"
    assert budget.calls == 2


def test_the_report_states_how_many_calls_stopped_early(tmp_path: Path) -> None:
    deps = make_deps(tmp_path)
    cli(deps, "run", "--max-usd", "5", "--yes", *SMALL)
    (directory,) = run_dirs(deps)
    report = (directory / "report.md").read_text()
    assert "0 stopped before answering" in report and "**Model calls:**" in report


# ------------------------------------------------------------ stops and resume


class RunsOutOfCredit(FakeModel):
    """Answers normally, then refuses everything, like an account whose credit ran out."""

    def __init__(self, answers: int) -> None:
        super().__init__()
        self.answers = answers

    def complete(self, request: LLMRequest) -> LLMResponse:
        if self.calls >= self.answers:
            raise ProviderConfigError("Your credit balance is too low to access the Anthropic API.")
        return super().complete(request)


TOTAL_TRIALS = 3 * 2 + 33  # 3 contracts x 2 profiles x 1 repeat, 33 injection cases x 1 repeat


def test_an_api_refusal_stops_the_run_cleanly_and_reports(tmp_path: Path) -> None:
    deps = make_deps(tmp_path, provider=lambda model: RunsOutOfCredit(2))
    code, _, err = cli(deps, "run", "--max-usd", "5", "--yes", *SMALL)
    assert code == EXIT_STOPPED and "credit balance is too low" in err
    (directory,) = run_dirs(deps)
    status = json.loads((directory / "status.json").read_text())
    assert not status["complete"] and "refused a request" in status["reason"]
    assert len(ResultsWriter(directory).trials()) == 2  # the two reads that were answered
    assert "Incomplete run" in (directory / "report.md").read_text()


def test_an_api_refusal_inside_an_engine_run_is_not_mistaken_for_an_outage() -> None:
    budget = Budget(cap=Decimal(1), price=PRICE)
    provider = CountingProvider(RunsOutOfCredit(0), budget)
    twin = next(c for c in load_suite().twins if c.base.name == "northwind")
    with pytest.raises(ProviderConfigError):
        drive(twin, provider, pytest.fail)  # the service swallows it; the harness must not


def test_resume_finishes_the_run_without_repeating_finished_trials(tmp_path: Path) -> None:
    first = make_deps(tmp_path, provider=lambda model: RunsOutOfCredit(4))
    cli(first, "run", "--max-usd", "5", "--yes", *SMALL)
    (directory,) = run_dirs(first)
    done_before = ResultsWriter(directory).trials()
    # Three contract reads finish; the first injection case gets its read answered, then its plan
    # is refused, so that trial is not recorded (and will run again).
    assert len(done_before) == 3

    model = FakeModel()
    second = make_deps(tmp_path, provider=lambda m: model)
    code, out, _ = cli(second, "run", "--max-usd", "5", "--yes", "--resume", str(directory), *SMALL)
    assert code == EXIT_OK and "resuming: 3 trials already finished" in out
    trials = ResultsWriter(directory).trials()
    keys = [(t["kind"], t["case_id"], t.get("profile"), t["repeat"]) for t in trials]
    assert len(keys) == len(set(keys)) == TOTAL_TRIALS  # every trial once
    assert trials[:3] == done_before  # kept exactly as they were
    assert model.calls == (TOTAL_TRIALS - 3) + 33  # each remaining read, plus 33 plans
    status = json.loads((directory / "status.json").read_text())
    assert status["complete"] and status["sessions"] == 2
    sessions = [json.loads(x) for x in (directory / "sessions.jsonl").read_text().splitlines()]
    assert Decimal(status["spent_usd"]) == sum(Decimal(s["spent_usd"]) for s in sessions)
    assert sessions[0]["calls"] == 4  # includes the read of the trial that did not finish
    report = (directory / "report.md").read_text()
    assert "took 2 sessions" in report
    assert "**Spend caps:** $5 for the first session, then $5 for the resumed one(s)" in report
    assert "session 1 commit `deadbeef`; session 2 commit `deadbeef`" in report


def test_resume_reruns_trials_the_budget_stopped(tmp_path: Path) -> None:
    deps = make_deps(tmp_path)
    cli(deps, "run", "--max-usd", "0.2", "--yes", "--accept-ceiling-over-cap", *SMALL)
    (directory,) = run_dirs(deps)
    stopped = [t for t in ResultsWriter(directory).trials() if t["status"] == "budget_stopped"]
    assert stopped
    code, _, _ = cli(deps, "run", "--max-usd", "5", "--yes", "--resume", str(directory), *SMALL)
    trials = ResultsWriter(directory).trials()
    assert code == EXIT_OK and len(trials) == TOTAL_TRIALS
    assert not [t for t in trials if t["status"] == "budget_stopped"]


def test_a_run_that_ended_without_recording_itself_keeps_its_spend(tmp_path: Path) -> None:
    deps = make_deps(tmp_path, provider=lambda model: RunsOutOfCredit(3))
    cli(deps, "run", "--max-usd", "5", "--yes", *SMALL)
    (directory,) = run_dirs(deps)
    for name in ("status.json", "sessions.jsonl"):  # as if the process had been killed
        (directory / name).unlink()
    earlier = sum(Decimal(t["cost_usd"]) for t in ResultsWriter(directory).trials())
    cli(make_deps(tmp_path), "run", "--max-usd", "5", "--yes", "--resume", str(directory), *SMALL)
    sessions = [json.loads(x) for x in (directory / "sessions.jsonl").read_text().splitlines()]
    assert (
        Decimal(sessions[0]["spent_usd"]) == earlier and "trial records" in sessions[0]["stopped"]
    )
    assert sessions[0]["git_commit"] == "deadbeef" and sessions[0]["cap_usd"] == "5"  # manifest's


def test_resume_refuses_other_settings_or_a_finished_run(tmp_path: Path) -> None:
    deps = make_deps(tmp_path, provider=lambda model: RunsOutOfCredit(2))
    cli(deps, "run", "--max-usd", "5", "--yes", *SMALL)
    (directory,) = run_dirs(deps)
    resume = ["run", "--max-usd", "5", "--yes", "--resume", str(directory)]
    code, _, err = cli(make_deps(tmp_path), *resume, *SMALL[:-1], "2")
    assert code == EXIT_REFUSED and "repeats" in err
    assert cli(make_deps(tmp_path), *resume, *SMALL)[0] == EXIT_OK
    code, _, err = cli(make_deps(tmp_path), *resume, *SMALL)
    assert code == EXIT_REFUSED and "already finished" in err
    code, _, err = cli(make_deps(tmp_path), "run", "--max-usd", "5", "--resume", str(tmp_path))
    assert code == EXIT_REFUSED and "not a benchmark run directory" in err


def test_resume_prices_only_what_is_left(tmp_path: Path) -> None:
    unit = Unit("cuad", "c1", "presence", "read", 1000, 2048, attempts=2, repeats=3)
    other = Unit("injection", "i1", None, "plan", 500, 4096, attempts=2, repeats=5)
    left = remaining([unit, other], [("cuad", "c1", "presence")] * 3 + [("injection", "i1", None)])
    assert left == [Unit("injection", "i1", None, "plan", 500, 4096, attempts=2, repeats=4)]


def test_resume_can_use_the_earlier_trial_to_price_the_rest(tmp_path: Path) -> None:
    deps = make_deps(tmp_path)
    cli(deps, "estimate", "--max-usd", "1", "--trial", *SMALL)
    (saved,) = (deps.results_dir / "estimates").iterdir()
    stopping = make_deps(tmp_path, provider=lambda model: RunsOutOfCredit(2))
    cli(stopping, "run", "--max-usd", "5", "--yes", "--estimate-from", str(saved), *SMALL)
    (directory,) = run_dirs(stopping)
    code, out, _ = cli(
        make_deps(tmp_path),
        "run",
        "--max-usd",
        "5",
        "--yes",
        "--resume",
        str(directory),
        "--estimate-from",
        str(saved),
        *SMALL,
    )
    assert code == EXIT_OK and "expected total (from the trial)" in out


def test_a_refused_token_count_is_a_clean_refusal() -> None:
    class Refused(Exception):
        message = "Your credit balance is too low to access the Anthropic API."

    class Client:
        def __init__(self) -> None:
            self.messages = self

        def count_tokens(self, **kwargs: Any) -> Any:
            raise Refused()

    counter = AnthropicTokenCounter(MODEL, client=Client(), api_errors=(Refused,))
    with pytest.raises(ProviderConfigError, match="credit balance is too low"):
        counter.count(reader_request(PRESENCE, "A contract."))


def test_the_cli_reports_a_refused_count_without_a_traceback(tmp_path: Path) -> None:
    class Refusing:
        def count(self, request: LLMRequest) -> int:
            raise ProviderConfigError("the Anthropic API refused the token count: no credit")

    deps = make_deps(tmp_path, counter=lambda model: Refusing())
    code, _, err = cli(deps, "estimate", "--max-usd", "1", *SMALL)
    assert code == EXIT_REFUSED and err.startswith("refused: the Anthropic API refused")


def test_a_report_from_older_sessions_shows_unknowns_honestly(tmp_path: Path) -> None:
    deps = make_deps(tmp_path, provider=lambda model: RunsOutOfCredit(2))
    cli(deps, "run", "--max-usd", "5", "--yes", *SMALL)
    (directory,) = run_dirs(deps)
    lines = [
        {"spent_usd": "1", "stopped": "ended without recording itself"},  # no cap or commit
        {"spent_usd": "2", "cap_usd": "15"},  # a resumed session from before commits were kept
    ]
    (directory / "sessions.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    from bench.report import write_report

    _, md = write_report(directory)
    report = md.read_text()
    assert "$5 for the first session, then $15" in report  # first session's cap from the manifest
    assert "session 1 commit `deadbeef`; session 2 not recorded" in report


def test_the_counter_works_without_the_anthropic_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """`make install` leaves out the optional SDK; a counter with its own client must still work."""
    import sys

    monkeypatch.setitem(sys.modules, "anthropic", None)  # makes `import anthropic` fail

    class Client:
        def __init__(self) -> None:
            self.messages = self

        def count_tokens(self, **kwargs: Any) -> Any:
            return type("Count", (), {"input_tokens": 7})()

    assert AnthropicTokenCounter(MODEL, client=Client()).count(reader_request(PRESENCE, "x")) == 7
