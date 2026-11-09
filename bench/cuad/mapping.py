"""Which Reader clause kinds CUAD can grade, and against which of its categories.

Checked against CUAD's category_descriptions.csv (design Appendix B). Every Reader kind appears
exactly once, so a kind added to the app later fails a test until it is placed here.
"""

from dataclasses import dataclass
from typing import Literal

Presence = Literal["answer_yes", "clause_present"]


@dataclass(frozen=True)
class Mapped:
    kind: str
    category: str  # CUAD category name, as in its category_descriptions.csv
    presence: Presence  # Yes/No answer column, or (for value categories) a non-empty clause column
    why: str
    column: str = ""  # its master_clauses.csv column when that is spelled differently

    @property
    def csv_column(self) -> str:
        return self.column or self.category


# Scored: the CUAD category means the same thing as the Reader kind.
SCORED: tuple[Mapped, ...] = (
    Mapped(
        "unlimited_liability",
        "Uncapped Liability",
        "answer_yes",
        "liability left uncapped for some breach or claim",
    ),
    Mapped(
        "non_compete",
        "Non-Compete",
        "answer_yes",
        "a restriction on competing or on operating in a field or area",
    ),
    Mapped(
        "ip_assignment",
        "IP Ownership Assignment",
        "answer_yes",
        "one party's intellectual property passes to the other",
        column="Ip Ownership Assignment",  # master_clauses.csv capitalizes it this way
    ),
)

# Partial: related, but CUAD marks a wider set. Reported in their own table, never in the headline.
PARTIAL: tuple[Mapped, ...] = (
    Mapped(
        "unilateral_termination",
        "Termination for Convenience",
        "answer_yes",
        "CUAD does not say which party may terminate",
        column="Termination For Convenience",  # master_clauses.csv capitalizes it this way
    ),
    Mapped(
        "auto_renewal",
        "Renewal Term",
        "clause_present",
        "CUAD marks any renewal term, including ones that need both parties to agree",
    ),
)

# Not scored, with the reason.
NOT_SCORED: dict[str, str] = {
    "unilateral_fee_change": "Price Restrictions limits price changes; it is not a one-sided right",
    "one_sided_liability": "Cap on Liability marks a cap, not whether it is one-sided",
    "broad_data_sharing": "no CUAD category covers data sharing",
    "other": "a catch-all, including the Reader's flag for text aimed at AI reviewers",
}


def scored_kinds() -> tuple[str, ...]:
    return tuple(m.kind for m in SCORED)


def partial_kinds() -> tuple[str, ...]:
    return tuple(m.kind for m in PARTIAL)
