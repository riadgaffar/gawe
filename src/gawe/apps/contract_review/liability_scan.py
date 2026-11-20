"""A deterministic scan of a contract for uncapped-liability language (M17, design D2).

Someone planting instructions in a contract can add text but usually cannot remove the real
liability clause, so this scan still finds it whatever the Reader reports. It is a signal, not a
judge: paraphrase defeats it, and a counterparty who wrote the whole contract controls the
wording. It cannot tell whose liability is unlimited, so it can only ever add a review.

Two levels:

- **express**: the contract says liability is unlimited or not limited, in either word order.
  Feeds a rule for "low" ratings, if its gate passes.
- **carve_out**: a cap's exceptions ("the foregoing limitation shall not apply", "nothing limits
  liability for"). Standard in honest contracts, so it feeds next-day review, never legal.

The text is normalized first: Unicode format characters (zero-width and the like) removed, every
run of whitespace, line breaks included, made one space, and case folded. The result holds
pattern ids only, never matched text, so no contract wording reaches the run record.

Patterns were written and frozen before the gate was measured (docs/m17-plan.md, S4). Changing
one changes `VERSION`.
"""

import re
import unicodedata
from dataclasses import dataclass

VERSION = 1

# "liab" covers liability, liabilities, liable. A window stays inside one sentence or clause.
_LIAB = r"\bliab(?:ility|ilities|le)\b"
_WINDOW = r"[^.;]{0,80}?"

EXPRESS: dict[str, re.Pattern[str]] = {
    "unlimited_liability": re.compile(r"\bunlimited liabilit(?:y|ies)\b"),
    "liability_is_unlimited": re.compile(
        _LIAB + _WINDOW + r"\b(?:is|are|shall be|will be|be) unlimited\b"
    ),
    "liability_not_limited": re.compile(
        _LIAB + _WINDOW + r"\b(?:is|are|shall|will) not (?:be )?(?:limited|capped)\b"
    ),
    "liability_without_limit": re.compile(
        _LIAB + _WINDOW + r"\bwithout (?:any )?(?:limit|cap)\b(?!ation)"
    ),
    "no_limit_on_liability": re.compile(
        r"\bno (?:limit|limitation|cap|ceiling) (?:on|of|to|upon) "
        r"(?:[\w'’]+ ){0,3}liabilit(?:y|ies)\b"
    ),
    "uncapped": re.compile(r"\buncapped\b"),
}

CARVE_OUT: dict[str, re.Pattern[str]] = {
    "limitation_shall_not_apply": re.compile(
        r"\b(?:limitations?|exclusions?|caps?)\b"
        + _WINDOW
        + r"\b(?:shall|will|does|do) not (?:apply|be applicable)\b"
    ),
    "nothing_limits_liability": re.compile(
        r"\bnothing\b[^.;]{0,60}?\b(?:limits?|excludes?|restricts?|shall limit|shall exclude|"
        r"will limit|will exclude)\b[^.;]{0,40}?" + _LIAB
    ),
    "except_for_liability": re.compile(
        r"\bexcept (?:for |with respect to )?(?:liabilit(?:y|ies)|damages|claims) "
        r"(?:arising|resulting|for|under|in connection)\b"
    ),
}


@dataclass(frozen=True)
class LiabilityScan:
    version: int
    express: bool
    carve_out: bool
    patterns: tuple[str, ...]  # ids of the patterns that hit, sorted; never the matched text

    def as_record(self) -> dict[str, object]:
        return {
            "version": self.version,
            "express": self.express,
            "carve_out": self.carve_out,
            "patterns": list(self.patterns),
        }


def normalize(text: str) -> str:
    """Format characters removed, whitespace runs to one space, case folded."""
    visible = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    return re.sub(r"\s+", " ", visible).casefold()


def scan(text: str) -> LiabilityScan:
    flat = normalize(text)
    express = sorted(name for name, p in EXPRESS.items() if p.search(flat))
    carve = sorted(name for name, p in CARVE_OUT.items() if p.search(flat))
    return LiabilityScan(
        version=VERSION,
        express=bool(express),
        carve_out=bool(carve),
        patterns=tuple(sorted(express + carve)),
    )
