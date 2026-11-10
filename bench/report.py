"""Turns a run's files into report.json, then report.md from report.json alone.

Every number in the Markdown comes from report.json, so any sentence can be traced back. The
"What this does not prove" section is fixed text and always present (design section 3.4).

    python -m bench report bench-results/<run id>
"""

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from bench.cuad.labels import CuadCase
from bench.cuad.mapping import NOT_SCORED, PARTIAL, SCORED, partial_kinds, scored_kinds
from bench.metrics import prevalence, summarize_cuad, summarize_injection, summarize_scripted
from bench.results import ResultsWriter

REPORT_SCHEMA = "gawe.bench.report.v1"
SCRIPTED_REPORT_SCHEMA = "gawe.bench.scripted-report.v1"

SCRIPTED_DOES_NOT_SHOW = (
    "How often a real model is fooled. The model's replies were written to be fooled; the live "
    "benchmark measures how often that happens.",
    "Every way a model could be fooled. Each attack is scripted one way per mode: the "
    "attacker's step added to an honest plan, or the attacker's findings planned from faithfully.",
    "That approvals hold. A step waiting for approval is safe only if the person says no; the "
    "benchmark never approves anything.",
    "What happens with real systems. The systems acted on are mocks.",
)

OUTCOME_LABELS = {
    "none": "ran",
    "policy_deny": "denied by policy",
    "approval_pending": "waits for approval",
    "validator": "rejected by plan validation",
    "not_run": "never ran",
    "not_acted_on": "not acted on",
}

DOES_NOT_PROVE = (
    "CUAD measures whether the Reader finds clauses, not whether it judges legal risk well. "
    "That judgment still needs a person.",
    "CUAD is public and may be in the model's training data, which could make the accuracy "
    "numbers look better than they would on unseen contracts.",
    "The attacks were written by us and fixed in advance. An attacker who adapts to the "
    "defenses may do better. A low rate of unauthorized steps is evidence about this set of "
    "attacks, not a guarantee.",
    "The results cover one model, one date and one configuration, all named above. A different "
    "model or prompt needs a new run.",
    "The systems acted on are mocks. Real connectors must keep the same contract (one effect per "
    "idempotency key).",
    "An audit anchor proves the chain head existed when it was anchored. It does not prove the "
    "chain was correct before that, and runs still in progress are not anchored.",
    "The samples are small. The intervals show how small.",
)


def build_report(directory: Path) -> dict[str, Any]:
    writer = ResultsWriter(directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("kind") == "scripted":
        return build_scripted_report(writer, manifest)
    status_path = directory / "status.json"
    status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else None
    cases = {c["case_id"]: _case(c) for c in manifest["sample"]["cases"]}
    cuad = writer.trials("cuad")
    injection = writer.trials("injection")
    profiles = [p for p in manifest["profiles"] if any(t["profile"] == p for t in cuad)]
    seed = int(manifest["sample"]["seed"])
    sample = list(cases.values())
    return {
        "schema": REPORT_SCHEMA,
        "manifest": manifest,
        "status": status,
        "complete": bool(status and status.get("complete")),
        "cuad": {
            profile: {
                "scored": summarize_cuad(
                    cuad, cases, profile=profile, kinds=scored_kinds(), seed=seed
                ),
                "partial": summarize_cuad(
                    cuad, cases, profile=profile, kinds=partial_kinds(), seed=seed
                ),
            }
            for profile in profiles
        },
        "prevalence": {
            kind: {
                "sample": prevalence(sample, kind),
                "corpus": manifest["dataset"]["prevalence"].get(kind),
            }
            for kind in (*scored_kinds(), *partial_kinds())
        },
        "injection": summarize_injection(
            injection, policy_by_goal=manifest["injection"]["policy_by_goal"]
        )
        if injection
        else None,
        "sessions": _sessions(directory, manifest),
        "does_not_prove": list(DOES_NOT_PROVE),
    }


def _sessions(directory: Path, manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Each start or resume, with its cap and code version. Runs from before sessions recorded
    these get the manifest's values for their first session; anything else unknown stays None."""
    path = directory / "sessions.jsonl"
    if not path.exists():
        sessions: list[dict[str, Any]] = [{}]
    else:
        lines = path.read_text(encoding="utf-8").splitlines()
        sessions = [json.loads(line) for line in lines if line.strip()]
    out = []
    for index, session in enumerate(sessions):
        first = index == 0
        out.append(
            {
                "cap_usd": session.get("cap_usd") or (manifest["max_usd"] if first else None),
                "git_commit": session.get("git_commit")
                or (manifest["git_commit"] if first else None),
                "git_dirty": session.get("git_dirty", manifest["git_dirty"] if first else None),
                "spent_usd": session.get("spent_usd"),
            }
        )
    return out


def build_scripted_report(writer: ResultsWriter, manifest: Mapping[str, Any]) -> dict[str, Any]:
    trials = writer.trials("injection_scripted")
    return {
        "schema": SCRIPTED_REPORT_SCHEMA,
        "manifest": manifest,
        "scripted": summarize_scripted(
            trials, policy_by_goal=manifest["injection"]["policy_by_goal"]
        ),
        "does_not_show": list(SCRIPTED_DOES_NOT_SHOW),
    }


def write_report(directory: Path) -> tuple[Path, Path]:
    report = build_report(directory)
    json_path = directory / "report.json"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path = directory / "report.md"
    saved = json.loads(json_path.read_text())
    render = render_scripted if saved["schema"] == SCRIPTED_REPORT_SCHEMA else render_markdown
    md_path.write_text(render(saved), encoding="utf-8")
    return json_path, md_path


def _case(data: Mapping[str, Any]) -> CuadCase:
    return CuadCase(
        case_id=data["case_id"],
        contract_type=data["contract_type"],
        text_path="",
        text_sha256=data["text_sha256"],
        chars=int(data["chars"]),
        labels=dict(data["labels"]),
        partial_labels=dict(data["partial_labels"]),
    )


# --------------------------------------------------------------------- markdown


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


def span(interval: Sequence[float] | None) -> str:
    return "n/a" if not interval else f"{pct(interval[0])}–{pct(interval[1])}"


def kof(entry: Mapping[str, Any]) -> str:
    return f"{entry['k']} of {entry['n']} ({pct(entry['rate'])}, 95% {span(entry['wilson_95'])})"


def render_markdown(report: Mapping[str, Any]) -> str:
    m = report["manifest"]
    out: list[str] = [f"# GAWE benchmark report: {m['bench_run_id']}", ""]
    if not report["complete"]:
        reason = (report.get("status") or {}).get("reason") or "the run did not finish"
        out += [f"> **Incomplete run:** {reason}. The numbers cover only what finished.", ""]
    sessions = report.get("sessions") or []
    out += _how(m, report.get("status"), sessions) + _headline(report) + _accuracy(report)
    out += _injection(report)
    out += _variance(report) + _limits(report) + _reproduce(m, sessions)
    return "\n".join(out).rstrip() + "\n"


def _caps(m: Mapping[str, Any], sessions: Sequence[Mapping[str, Any]]) -> str:
    if len(sessions) <= 1:
        return f"- **Spend cap:** ${m['max_usd']}."
    caps = [f"${s['cap_usd']}" if s.get("cap_usd") else "not recorded" for s in sessions]
    return (
        f"- **Spend caps:** {caps[0]} for the first session, then "
        + ", ".join(caps[1:])
        + " for the resumed one(s); each caps only that session's new spend."
    )


def _how(
    m: Mapping[str, Any], status: Mapping[str, Any] | None, sessions: Sequence[Mapping[str, Any]]
) -> list[str]:
    sample, dataset = m["sample"], m["dataset"]
    calls = []
    if status and "calls" in status:
        calls = [
            f"- **Model calls:** {status['calls']}, spending ${status['spent_usd']}. "
            f"{status['hit_output_cap']} used their whole output allowance; "
            f"{status['early_stops']} stopped before answering (a refusal, or out of output "
            "tokens) and were retried as failed attempts."
        ]
        if int(status.get("sessions", 1)) > 1:
            calls.append(
                f"- **Sessions:** the run was resumed and took {status['sessions']} sessions; "
                "trials finished before each stop were kept, not repeated."
            )
    return [
        "## 1. What we tested and how",
        "",
        f"- **Model:** `{m['model']}`, at the API's default temperature, run {m['started_at']}.",
        f"- **Reader accuracy:** {len(sample['cases'])} CUAD contracts, sampled by contract type "
        f"with seed {sample['seed']}, each read {m['repeats']['cuad']} times per profile "
        f"({', '.join(m['profiles'])}). {len(dataset['too_large'])} contracts were longer than "
        "the Reader accepts and were set aside before sampling.",
        f"- **Prompt injection:** {m['injection']['attacks']} attacks and "
        f"{m['injection']['twins']} clean twins, each run {m['repeats']['injection']} times "
        "through the full engine. Nothing was ever approved.",
        _caps(m, sessions),
        *calls,
        "",
    ]


def _headline(report: Mapping[str, Any]) -> list[str]:
    lines = ["## 2. Headline results", ""]
    presence = report["cuad"].get("presence", {}).get("scored")
    if presence:
        micro = presence["micro"]
        boot = micro["bootstrap_95"]
        lines.append(
            f"- **Finding clauses (presence profile, {len(SCORED)} clause types):** precision "
            f"{pct(micro['precision'])} (95% {span(boot['precision'])}), recall "
            f"{pct(micro['recall'])} (95% {span(boot['recall'])}), over {presence['reads']} reads "
            f"of {presence['contracts']} contracts."
        )
    injection = report.get("injection")
    if injection:
        overall = injection["overall"]
        lines += [
            f"- **Model fooled by an attack:** {kof(overall['model_fooled'])}.",
            f"- **Attacker's step actually ran:** {kof(overall['unauthorized_step_ran'])}.",
            f"- **Clean twins that completed their task:** {kof(injection['twins']['completed'])}.",
        ]
        allowed = [g for g, e in injection["by_goal"].items() if e.get("policy") == "allow"]
        if allowed:
            lines.append(
                "- Steps policy allows without approval (" + ", ".join(sorted(allowed)) + ") "
                "count as unauthorized when an attack causes them. They are policy findings, "
                "not engine failures; see section 4."
            )
    return [*lines, ""]


def _accuracy_table(summary: Mapping[str, Any], lower_bound: bool) -> list[str]:
    recall = "Recall (lower bound)" if lower_bound else "Recall"
    rows = [
        f"| Clause type | Precision | {recall} | F1 | TP | FP | FN |",
        "|---|---|---|---|---|---|---|",
    ]
    for kind, c in summary["kinds"].items():
        rows.append(
            f"| `{kind}` | {pct(c['precision'])} | {pct(c['recall'])} | {pct(c['f1'])} | "
            f"{c['tp']:g} | {c['fp']:g} | {c['fn']:g} |"
        )
    micro = summary["micro"]
    rows.append(
        f"| **All** | {pct(micro['precision'])} | {pct(micro['recall'])} | {pct(micro['f1'])} | "
        f"{micro['tp']:g} | {micro['fp']:g} | {micro['fn']:g} |"
    )
    return rows


def _accuracy(report: Mapping[str, Any]) -> list[str]:
    lines = ["## 3. Reader accuracy per clause type", ""]
    for profile, parts in report["cuad"].items():
        scored = parts["scored"]
        lines += [f"### Profile `{profile}`", ""]
        if scored["recall_is_lower_bound"]:
            lines += [
                "This is the app's real prompt, which asks for risk to Acme Corp. Acme is not a "
                'party to any CUAD contract, so a missed clause may be a correct "not risky": '
                "recall here is a lower bound. Precision is meaningful.",
                "",
            ]
        lines += _accuracy_table(scored, scored["recall_is_lower_bound"]) + [""]
        lines += [
            f"Failed reads (counted as finding nothing): {pct(scored['failure_rate'])}.",
            "",
            "Partial matches, never in the headline (CUAD marks a wider set):",
            "",
            *_accuracy_table(parts["partial"], scored["recall_is_lower_bound"]),
            "",
        ]
    lines += ["How common each clause type is:", "", "| Clause type | In the sample | In CUAD |"]
    lines.append("|---|---|---|")
    for kind, share in report["prevalence"].items():
        lines.append(f"| `{kind}` | {pct(share['sample'])} | {pct(share['corpus'])} |")
    lines += ["", "Not scored: " + "; ".join(f"`{k}` ({why})" for k, why in NOT_SCORED.items())]
    lines.append(
        "Partial: " + "; ".join(f"`{m.kind}` vs {m.category} ({m.why})" for m in PARTIAL) + "."
    )
    return [*lines, ""]


def _injection(report: Mapping[str, Any]) -> list[str]:
    injection = report.get("injection")
    lines = ["## 4. Injection results by goal and technique", ""]
    if not injection:
        return [*lines, "The injection suite did not run.", ""]
    lines += [
        "| Goal | Policy as written | Model fooled | Attacker's step ran |",
        "|---|---|---|---|",
    ]
    for goal, entry in injection["by_goal"].items():
        lines.append(
            f"| `{goal}` | {entry.get('policy') or '-'} | {kof(entry['model_fooled'])} | "
            f"{kof(entry['unauthorized_step_ran'])} |"
        )
    lines += ["", "| Technique | Model fooled | Attacker's step ran |", "|---|---|---|"]
    for technique, entry in injection["by_technique"].items():
        lines.append(
            f"| `{technique}` | {kof(entry['model_fooled'])} | "
            f"{kof(entry['unauthorized_step_ran'])} |"
        )
    lines += ["", "What stopped attacks that fooled the model:", ""]
    labels = {
        "policy_deny": "denied by policy",
        "approval_pending": "waiting for a person to approve (safe only if they say no)",
        "validator": "rejected by plan validation",
        "not_run": "never ran (the run ended first)",
        "not_acted_on": "not acted on (only the Reader was fooled, and no step used it)",
        "none": "nothing: the step ran",
    }
    for key, count in injection["stopped_by"].items():
        lines.append(f"- {labels.get(key, key)}: {count}")
    if not injection["stopped_by"]:
        lines.append("- None: no attack fooled the model, so no defense had to stop one.")
    overall = injection["overall"]
    lines += [
        "",
        f"- Attacker text reached a stored record: {kof(overall['attacker_text_persisted'])}.",
        f"- The Reader flagged text aimed at AI reviewers: {kof(overall['injection_flagged'])}.",
        f"- Trial outcomes: {_counts(injection['statuses'])}.",
        "",
    ]
    return lines + _clean_policy(injection.get("clean_policy"))


def _clean_policy(clean: Mapping[str, Any] | None) -> list[str]:
    if not clean or not clean["steps"]:
        return []
    lines = [
        "What policy decided for each step of the clean runs (no attack):",
        "",
        "| App | Step | Policy | Clean runs |",
        "|---|---|---|---|",
    ]
    for step in clean["steps"]:
        runs = clean["runs_by_app"].get(step["app"], 0)
        lines.append(
            f"| {step['app']} | `{step['tool']}` | {step['outcome']} | {step['count']} of {runs} |"
        )
    if clean["assessment_risk"]:
        lines += [
            "",
            f"- Risk levels the clean contract runs wrote: {_counts(clean['assessment_risk'])}.",
        ]
    return [*lines, ""]


def _variance(report: Mapping[str, Any]) -> list[str]:
    lines = ["## 5. How much results varied", ""]
    for profile, parts in report["cuad"].items():
        scored = parts["scored"]
        per = scored["micro"]["per_repeat"]
        lines.append(
            f"- `{profile}`: precision {pct(per['precision']['low'])}–"
            f"{pct(per['precision']['high'])} and recall {pct(per['recall']['low'])}–"
            f"{pct(per['recall']['high'])} across repeats; {pct(scored['flip_rate'])} of "
            "(contract, clause type) answers changed between repeats."
        )
    injection = report.get("injection")
    if injection:
        mixed = sum(1 for e in injection["per_case"].values() if 0 < e["fooled"] < e["of"])
        lines.append(
            f"- Injection: {mixed} of {len(injection['per_case'])} attacks fooled the model on "
            "some repeats but not others."
        )
    return [*lines, ""]


def _limits(report: Mapping[str, Any]) -> list[str]:
    return [
        "## 6. What this does not prove",
        "",
        *[f"- {item}" for item in report["does_not_prove"]],
        "",
    ]


def _commit(session: Mapping[str, Any]) -> str:
    if not session.get("git_commit"):
        return "not recorded"
    dirty = " (with uncommitted changes)" if session.get("git_dirty") else ""
    return f"commit `{session['git_commit']}`{dirty}"


def _reproduce(m: Mapping[str, Any], sessions: Sequence[Mapping[str, Any]]) -> list[str]:
    if len(sessions) > 1:
        code = (
            "- Code: "
            + "; ".join(f"session {i} {_commit(s)}" for i, s in enumerate(sessions, start=1))
            + "."
        )
    else:
        code = f"- Code: {_commit({'git_commit': m['git_commit'], 'git_dirty': m['git_dirty']})}."
    return [
        "## 7. How to reproduce",
        "",
        code,
        f"- CUAD archive SHA-256: `{m['dataset']['archive_sha256']}`.",
        f"- Sample: seed {m['sample']['seed']}, "
        f"contract ids SHA-256 `{m['sample']['ids_sha256']}`.",
        f"- Attack corpus SHA-256: `{m['injection']['corpus_sha256']}`.",
        "- Prompts: " + ", ".join(f"`{k}` {v[:12]}" for k, v in m["profile_sha256"].items()) + ".",
        f"- Prices: {m['price']['input_per_mtok']}/{m['price']['output_per_mtok']} USD per million "
        f"input/output tokens, checked {m['price']['checked_on']}.",
        "- Command: `make bench MAX_USD=<cap>` (see README).",
        "",
    ]


def _counts(counts: Mapping[str, int]) -> str:
    return ", ".join(f"{k} {v}" for k, v in counts.items()) or "none"


# --------------------------------------------------------------------- scripted markdown

MODE_LABELS = {"planner": "Planner fooled", "reader": "Reader fooled"}


def _ordered(counts: Mapping[str, int]) -> list[tuple[str, int]]:
    """Outcome counts in a fixed order (report.json sorts its keys alphabetically)."""
    order = list(OUTCOME_LABELS)
    keys = [k for k in counts if k != "of"]
    return [(k, counts[k]) for k in sorted(keys, key=lambda k: (order + [k]).index(k))]


def render_scripted(report: Mapping[str, Any]) -> str:
    m, s = report["manifest"], report["scripted"]
    out = [
        f"# GAWE scripted report: {m['bench_run_id']}",
        "",
        "Every attack in the injection suite, run through the real engine with a model that was "
        "fooled on purpose: its replies were written by the benchmark, and no model was called. "
        "This shows what plan validation, policy and approvals do once a model is fooled. The "
        "live benchmark shows how often that happens.",
        "",
        "## 1. What ran",
        "",
        f"- **Runs:** {s['trials']} ({m['injection']['attacks']} attacks; "
        + ", ".join(f"{MODE_LABELS.get(k, k)}: {v['of']}" for k, v in s["by_mode"].items())
        + ").",
        "- **Planner fooled:** the Reader reports the document honestly; the plan carries the "
        "attacker's step.",
        "- **Reader fooled:** the Reader reports what the attacker wanted; the Planner plans from "
        "it faithfully. Only goals the Reader's fields can express run this way.",
        "- **Never approved:** a step that needs approval waits, and the run is then cancelled.",
        f"- **Trial outcomes:** {_counts(s['statuses'])}.",
        "",
        "## 2. What stopped each attack",
        "",
        "| Goal | Policy as written | "
        + " | ".join(MODE_LABELS.get(k, k) for k in s["modes"])
        + " |",
        "|---|---|" + "---|" * len(s["modes"]),
    ]
    for goal, entry in s["by_goal"].items():
        cells = [_outcome_cell(entry["modes"].get(mode)) for mode in s["modes"]]
        out.append(f"| `{goal}` | {entry.get('policy') or '-'} | " + " | ".join(cells) + " |")
    out += [
        "",
        '"Policy as written" is what the rules do with the attacker\'s step on its own. Some rules '
        "also look at what the Reader found, so a run can be stopped where that column says "
        '"allow".',
        "",
        "In total:",
        "",
    ]
    for mode, counts in s["by_mode"].items():
        parts = [f"{OUTCOME_LABELS.get(k, k)} {v}" for k, v in _ordered(counts)]
        out.append(f"- {MODE_LABELS.get(mode, mode)} ({counts['of']} runs): {', '.join(parts)}.")
    out += [
        f"- Attacker text reached a stored record in {s['attacker_text_persisted']} of "
        f"{s['trials']} runs.",
        "",
        "## 3. What this does not show",
        "",
        *[f"- {line}" for line in report["does_not_show"]],
        "",
        "## 4. How to reproduce",
        "",
        f"- Code: {_commit({'git_commit': m['git_commit'], 'git_dirty': m['git_dirty']})}.",
        f"- Attack corpus SHA-256: `{m['injection']['corpus_sha256']}`.",
        "- Command: `python -m bench scripted` (free; no API key needed).",
        "",
    ]
    return "\n".join(out).rstrip() + "\n"


def _outcome_cell(counts: Mapping[str, int] | None) -> str:
    if not counts:
        return "-"
    total = counts["of"]
    parts = _ordered(counts)
    if len(parts) == 1:
        key, count = parts[0]
        return f"{OUTCOME_LABELS.get(key, key)} ({count} of {total})"
    return ", ".join(f"{OUTCOME_LABELS.get(k, k)} {v}" for k, v in parts) + f" (of {total})"
