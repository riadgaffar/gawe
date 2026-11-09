"""A seeded, stratified sample of CUAD contracts.

1. Draw `size` contracts, spread over contract types in proportion to how common each type is
   (largest-remainder rounding, at least one from each type while the size allows).
2. Top up: for each scored kind, add contracts CUAD marks as having it until the sample holds
   `min_positives` of them, if the corpus has that many.

The same seed and corpus always give the same sample. Topping up makes each kind more common in
the sample than in CUAD; the report shows both shares, because that changes what precision means.
"""

import hashlib
import random
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from bench.cuad.labels import CuadCase
from bench.cuad.mapping import scored_kinds

DEFAULT_SIZE = 50
DEFAULT_MIN_POSITIVES = 10


@dataclass(frozen=True)
class Sample:
    cases: tuple[CuadCase, ...]
    seed: int
    size_requested: int
    topped_up: dict[str, int]  # kind -> contracts added to reach the minimum
    shortfall: dict[str, int]  # kind -> positives still missing (the corpus has too few)

    @property
    def ids_sha256(self) -> str:
        return sample_ids_sha256(c.case_id for c in self.cases)


def sample_ids_sha256(case_ids: Iterable[str]) -> str:
    joined = "\n".join(sorted(case_ids))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def stratified_sample(
    cases: Sequence[CuadCase],
    *,
    seed: int,
    size: int = DEFAULT_SIZE,
    min_positives: int = DEFAULT_MIN_POSITIVES,
    kinds: Sequence[str] | None = None,
) -> Sample:
    if size < 1 or min_positives < 0:
        raise ValueError("size must be at least 1 and min_positives not negative")
    rng = random.Random(seed)
    pool = sorted(cases, key=lambda c: c.case_id)
    chosen = _by_type(pool, min(size, len(pool)), rng)

    topped_up: dict[str, int] = {}
    shortfall: dict[str, int] = {}
    for kind in sorted(kinds or scored_kinds()):
        have = sum(1 for c in chosen if c.labels.get(kind))
        if have >= min_positives:
            continue
        candidates = [c for c in pool if c.labels.get(kind) and c not in chosen]
        added = rng.sample(candidates, min(min_positives - have, len(candidates)))
        chosen.extend(added)
        if added:
            topped_up[kind] = len(added)
        if have + len(added) < min_positives:
            shortfall[kind] = min_positives - have - len(added)

    return Sample(
        cases=tuple(sorted(chosen, key=lambda c: c.case_id)),
        seed=seed,
        size_requested=size,
        topped_up=topped_up,
        shortfall=shortfall,
    )


def _by_type(pool: list[CuadCase], size: int, rng: random.Random) -> list[CuadCase]:
    groups: dict[str, list[CuadCase]] = defaultdict(list)
    for case in pool:
        groups[case.contract_type].append(case)
    quotas = _allocate({t: len(g) for t, g in groups.items()}, size)
    chosen: list[CuadCase] = []
    for contract_type in sorted(groups):
        chosen.extend(rng.sample(groups[contract_type], quotas[contract_type]))
    return chosen


def _allocate(counts: dict[str, int], size: int) -> dict[str, int]:
    """Split `size` over groups in proportion to `counts` (largest remainder, ties by name).

    When there are at least as many slots as groups, a group left with none takes one from the
    group with the most, so every contract type is represented.
    """
    names = sorted(counts)
    total = sum(counts.values())
    size = min(size, total)
    exact = {n: size * counts[n] / total for n in names}
    quotas = {n: int(exact[n]) for n in names}
    by_remainder = sorted(names, key=lambda n: (-(exact[n] - quotas[n]), n))
    for name in by_remainder[: size - sum(quotas.values())]:
        quotas[name] += 1  # never above counts[name]: exact[name] <= counts[name]
    if size >= len(names):
        for name in names:
            if quotas[name] == 0:
                donor = max(names, key=lambda d: quotas[d])  # first by name among equals
                quotas[donor] -= 1
                quotas[name] += 1
    return quotas
