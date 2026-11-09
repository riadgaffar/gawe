# What we measured: GAWE's first benchmark run

GAWE is an engine for AI agents that act on business systems. Its rule is simple: **the model
proposes, deterministic code decides.** A model reads a document and drafts a plan; a separate
policy engine decides, step by step, whether each action may run, and asks a person when the
policy says so. Every decision lands in an audit trail that can be checked from outside.

Claims like "the model can't make it do anything unauthorized" are easy to write and hard to
check, so we built a benchmark and ran it. This page reports what it found, including what it
does not show.

## The short version

- **Prompt injection:** across 125 runs of 25 attacks, the model was never fooled and no
  attacker's step ever ran. All 30 clean control runs finished their task.
- **Reading contracts:** the Reader found 94% of the clauses that lawyers had labelled, but only
  51% of what it flagged was right. Most of the false alarms came from one clause type.
- **The important caveat:** because the model was never fooled, this run did not exercise GAWE's
  own defenses. It shows that this model resisted these attacks. The defenses themselves are
  shown by the engine's tests, below.

One model (Claude Sonnet 5.5), one date (8 November 2026), one configuration. The run cost $15.42
in model fees.
Every figure on this page comes from the run's generated report,
[`results-report-20261108.md`](results-report-20261108.md).

## What we ran

**Prompt-injection suite.** 25 attacks, each a normal document (a contract or an access-request
ticket) with an attacker's instruction planted in it, plus 6 clean "twin" documents with no
attack. Each ran 5 times through the full engine: the real Reader, Planner, plan validator, policy
engine and executor, acting on mock business systems. The benchmark never approved anything, so
any effect in those systems happened without a person.

The attacks cover 7 goals, from emailing the contract to a blocked domain to granting production
admin access, and 8 techniques: plain instructions, hidden HTML comments, fake "end of document"
markers, invisible characters, claims of authority, fake tool calls, instructions split across
sections, and instructions in Spanish. They are in
[`bench/injection/cases.yaml`](../bench/injection/cases.yaml).

We counted two things separately, because they are different failures:

- **model fooled:** the model's findings or plan did what the attacker wanted;
- **attacker's step ran:** that action actually happened in a mock system.

**Contract reading.** 51 contracts from [CUAD](https://www.atticusprojectai.org/cuad), a public
set of 510 commercial contracts labelled by lawyers, sampled across all 25 contract types. 15
contracts longer than the Reader accepts were set aside before sampling, and are counted, not
hidden. Each was read 3 times. We scored three clause types where CUAD's labels mean the same as
GAWE's: uncapped liability, non-compete, and IP assignment.

## Results

### Prompt injection

| Measure | Result | 95% interval |
|---|---|---|
| Model fooled | 0 of 122 runs | 0% to 3% |
| Attacker's step ran | 0 of 125 runs | 0% to 3% |
| Attacker's text reached a stored record | 0 of 120 runs | 0% to 3% |
| Clean twins that completed their task | 30 of 30 | 89% to 100% |

The Reader is told to flag any text in a contract that addresses an AI reviewer. It flagged every
contract attack it read (87 of 87) and none of the 15 clean contract runs. For the
invisible-character attack, the hidden instruction was stripped before the model saw it; the flag
came from the visible text left around it.

Three runs failed to read a document after two attempts. They are counted as undecided, not as
successes.

### Reading contracts

| Clause type | Precision | Recall |
|---|---|---|
| IP assignment | 74% | 93% |
| Non-compete | 69% | 88% |
| Uncapped liability | 33% | 100% |
| **All three** | **51%** (95%: 37% to 64%) | **94%** (95%: 84% to 99%) |

Recall is high: the Reader rarely missed a labelled clause. Precision is low, and almost all of
the gap is uncapped liability, where 61 of its 91 flags were on contracts CUAD does not mark. Our
working guess is that the Reader treats liability carve-outs as "uncapped" more readily than
CUAD's labellers did. We have not confirmed that yet.

Between repeats, 12% of the Reader's answers changed. The intervals above come from resampling
the contracts, and they are wide because 51 contracts is a small sample.

We also ran the Contract Review app's real prompt, which asks for risk to one party rather than
the presence of a clause. On CUAD it scored lower (precision 45%, recall at least 58%). That is
expected and not a fair test: the party it judges for is not in any CUAD contract.

### Reliability and cost

648 model calls, with no call refusing or running out of output space. The run stopped once
partway, when the API account ran out of credit, and resumed without repeating or re-paying any
finished trial. Model fees were $15.42, plus $0.35 for a small trial run beforehand.

## What this does and does not show

- **The model was never fooled, so GAWE's defenses were never tested live.** Policy, approvals and
  plan validation never had to stop anything in this run. What shows they would is the engine's
  test suite, where scripted models that *are* fooled try the same attacks: blocked domains are
  denied, outside emails wait for an approval that never comes, invented tools are rejected, and
  approvals for one action cannot be reused for a changed one. See
  [`tests/adversarial/`](../tests/adversarial/), the benchmark's own scripted runs in
  [`tests/bench/test_injection.py`](../tests/bench/test_injection.py), and
  [`docs/threat-model.md`](threat-model.md).
- **Some attacks would have worked had the model been fooled.** Policy as written lets three of
  the attackers' goals run without approval: an email to an internal address, a CRM risk record
  marked low, and a low-level access grant. This run did not trigger them; that is a property
  of the model, not the engine.
- **We wrote the attacks.** They are fixed in advance. An attacker who adapts to the defenses may
  do better. A low rate here is evidence about this set of attacks, not a guarantee.
- **These numbers are not comparable to published results on AgentDojo,** the shared public
  benchmark for injection attacks on agents, used for example by CaMeL (Google and ETH Zürich),
  FIDES (Microsoft Research) and LlamaFirewall (Meta). Those use many more tasks and attacks. We
  have not run GAWE on AgentDojo.
- **CUAD is public** and may be in the model's training data, which could make accuracy look
  better than it would on unseen contracts.
- **CUAD measures finding clauses, not judging legal risk.** That still needs a person.
- **The business systems are mocks.** Real connectors have to keep the same guarantees (one effect
  per idempotency key).
- **One model, one date.** A different model or prompt needs a new run.

## What building the benchmark found

Measuring the system turned up problems that its own tests had not:

- **Runs could stall and keep costing money.** If the model stopped before answering (a refusal,
  or running out of output tokens), the Reader and Planner passed the error up, and the run sat in
  "planning". Each resume paid for another attempt. Fixed: it now counts as a failed attempt, and
  the run ends cleanly.
- **A budget stop could be silently swallowed.** The API layer catches errors while advancing a run
  so the run can be resumed later, which also hid the benchmark's spend cap. Fixed in the harness.
- **The real dataset did not match its documentation.** Column names, file-name spellings and
  contract types differed from what CUAD's description file suggested. The loader refuses
  anything it does not recognize, which is how these were caught.

## Reproduce it

```bash
make bench-data                                 # download CUAD and check its pinned hash
make bench-estimate MAX_USD=1                   # free: exact input cost and the worst case
make bench MAX_USD=25                           # the run; refuses if it may not fit the cap
```

Every command that can spend money needs a cap. The design, including how each number is defined,
is in [`docs/m12-design.md`](m12-design.md). The run used seed 20261029; the CUAD archive's
SHA-256 is `88b694d99007d39777fa44cd72daf8297773d285dc3eab0091ba32078888d18e`.

The full generated report, with every table and interval, is in
[`results-report-20261108.md`](results-report-20261108.md).
