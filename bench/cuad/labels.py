"""Reads CUAD's labels and contract texts into benchmark cases.

master_clauses.csv has a `Filename` column, then two columns per category: the clause text (a
list written as text, "[]" when there is none) and `<category>-Answer` (Yes/No for most
categories, a value such as a date for the rest). Only the columns the mapping needs are read,
and a missing one stops the load with its name, so a different CUAD release fails loudly
instead of scoring against the wrong column.

File names are matched loosely: the text files in CUAD v1 replace characters such as `'` and `&`
with `_` and drop a stray space before the extension, so names are compared with punctuation
normalized (and two contracts that would then collide stop the load). A contract's type comes
from the folder its PDF is filed under (`full_contract_pdf/Part_*/<type>/`), with the folder
names' inconsistent spellings evened out.

Contracts longer than the Reader accepts are set aside before sampling and listed, so the
results never quietly cover only the easier contracts.
"""

import csv
import hashlib
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from bench.cuad.mapping import PARTIAL, SCORED, Mapped
from gawe.llm.reader import DEFAULT_MAX_DOCUMENT_CHARS
from gawe.llm.safety import sanitize_text

FILENAME_COLUMN = "Filename"
TEXT_DIR = "full_contract_txt"
PDF_DIR = "full_contract_pdf"
_EMPTY_CLAUSES = ("", "[]")


class CuadFormatError(ValueError):
    """The CUAD files do not have the shape this loader expects."""


@dataclass(frozen=True)
class CuadCase:
    case_id: str  # "cuad:" + file stem
    contract_type: str
    text_path: str  # relative to the CUAD root
    text_sha256: str
    chars: int  # after the Reader's own sanitizing, which is what its limit applies to
    labels: Mapping[str, bool]  # scored kind -> CUAD says present
    partial_labels: Mapping[str, bool]


@dataclass(frozen=True)
class CuadCorpus:
    cases: tuple[CuadCase, ...]
    too_large: tuple[tuple[str, int], ...] = ()  # (case_id, chars), set aside before sampling
    missing_text: tuple[str, ...] = ()  # labelled contracts with no text file
    max_chars: int = DEFAULT_MAX_DOCUMENT_CHARS


def required_columns() -> list[str]:
    columns = [FILENAME_COLUMN]
    for mapped in (*SCORED, *PARTIAL):
        columns.append(_column(mapped))
    return columns


def _column(mapped: Mapped) -> str:
    column = mapped.csv_column
    return f"{column}-Answer" if mapped.presence == "answer_yes" else column


def present(mapped: Mapped, row: Mapping[str, str]) -> bool:
    value = (row.get(_column(mapped)) or "").strip()
    if mapped.presence == "clause_present":
        return value not in _EMPTY_CLAUSES
    answer = value.lower()
    if answer not in ("yes", "no"):
        raise CuadFormatError(f"{_column(mapped)} should be Yes or No")
    return answer == "yes"


def name_key(name: str) -> str:
    """A file name without extension, accents, case or punctuation differences, for matching.

    Accents are dropped because CUAD spells some names with a precomposed letter in one place and
    a letter plus a combining accent in another.
    """
    stem = unicodedata.normalize("NFKD", Path(name.strip()).stem)
    plain = "".join(ch for ch in stem if not unicodedata.combining(ch))
    return re.sub(r"[^0-9a-z]+", "_", plain.lower()).strip("_")


_TYPE_NAMES = {"Co Branding": "Co-Branding", "Non Compete Non Solicit": "Non-Compete/Non-Solicit"}


def normalize_type(folder: str) -> str:
    """`Affiliate_Agreements`, `Affiliate Agreement` -> `Affiliate`; `Joint Venture _ Filing` ->
    `Joint Venture`."""
    words = [w for w in folder.replace("_", " ").split() if w.lower() not in _TYPE_NOISE]
    name = " ".join(words) or "Unknown"
    return _TYPE_NAMES.get(name, name)


_TYPE_NOISE = frozenset({"agreement", "agreements", "filing"})


def _index(files: Iterable[Path], what: str) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for path in files:
        key = name_key(path.name)
        if key in found:
            raise CuadFormatError(f"two {what} files have the same normalized name: {key}")
        found[key] = path
    return found


def read_rows(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = [c for c in required_columns() if c not in (reader.fieldnames or [])]
        if missing:
            raise CuadFormatError(f"master_clauses.csv is missing columns: {', '.join(missing)}")
        return list(reader)


def load_corpus(
    root: Path,
    *,
    csv_name: str = "master_clauses.csv",
    max_chars: int = DEFAULT_MAX_DOCUMENT_CHARS,
) -> CuadCorpus:
    """Build cases from an extracted CUAD release at `root`."""
    cases: list[CuadCase] = []
    too_large: list[tuple[str, int]] = []
    missing: list[str] = []
    seen: set[str] = set()
    texts = _index((root / TEXT_DIR).glob("*.txt"), "text")
    types = {
        key: normalize_type(path.parent.name)
        for key, path in _index((root / PDF_DIR).glob("*/*/*"), "PDF").items()
    }
    for row in read_rows(root / csv_name):
        filename = row[FILENAME_COLUMN].strip()
        key = name_key(filename)
        case_id = f"cuad:{Path(filename).stem.strip()}"
        if key in seen:
            raise CuadFormatError(f"master_clauses.csv lists {filename} twice")
        seen.add(key)
        path = texts.get(key)
        if path is None:
            missing.append(case_id)
            continue
        relative = path.relative_to(root).as_posix()
        raw = path.read_bytes()
        chars = len(sanitize_text(raw.decode("utf-8", errors="replace")))
        if chars > max_chars:
            too_large.append((case_id, chars))
            continue
        cases.append(
            CuadCase(
                case_id=case_id,
                contract_type=types.get(key, "Unknown"),
                text_path=relative,
                text_sha256=hashlib.sha256(raw).hexdigest(),
                chars=chars,
                labels={m.kind: present(m, row) for m in SCORED},
                partial_labels={m.kind: present(m, row) for m in PARTIAL},
            )
        )
    return CuadCorpus(
        cases=tuple(sorted(cases, key=lambda c: c.case_id)),
        too_large=tuple(sorted(too_large)),
        missing_text=tuple(sorted(missing)),
        max_chars=max_chars,
    )


def read_text(root: Path, case: CuadCase) -> str:
    """The contract text, refusing a file that changed since the corpus was loaded."""
    raw = (root / case.text_path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != case.text_sha256:
        raise CuadFormatError(f"{case.case_id} changed since it was loaded")
    return raw.decode("utf-8", errors="replace")


def positives(cases: Iterable[CuadCase], kind: str) -> int:
    return sum(1 for c in cases if c.labels.get(kind, c.partial_labels.get(kind, False)))
