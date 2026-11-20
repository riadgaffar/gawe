"""M17 D2: the deterministic liability-language scan, on the scripted corpus and edge cases."""

from pathlib import Path

import pytest

from gawe.apps.contract_review.liability_scan import VERSION, normalize, scan

CORPUS = Path(__file__).parents[2] / "bench" / "injection" / "corpus" / "base"


def doc(name: str) -> str:
    return (CORPUS / name).read_text(encoding="utf-8")


def test_umbrella_c1004_is_express_across_its_line_break() -> None:
    text = doc("umbrella_data.md")
    assert "is not\nlimited" in text  # the phrase really does break across lines
    result = scan(text)
    assert result.express and "liability_not_limited" in result.patterns


def test_northwind_c1001_is_a_carve_out_only() -> None:
    result = scan(doc("northwind_msa.md"))
    assert result.carve_out and not result.express
    assert result.patterns == ("nothing_limits_liability",)


def test_globex_c1002_is_express_by_word_order() -> None:
    result = scan(doc("globex_licence.md"))
    assert result.express and "liability_is_unlimited" in result.patterns


def test_initech_c1003_has_no_hit() -> None:
    result = scan(doc("initech_sow.md"))
    assert not result.express and not result.carve_out and result.patterns == ()


@pytest.mark.parametrize("mark", ["​", "‌", "‍", "⁠", "﻿", "­"])
def test_format_characters_inside_a_phrase_do_not_hide_it(mark: str) -> None:
    text = f"Customer's liability is not lim{mark}ited in any way."
    assert scan(text).express


@pytest.mark.parametrize(
    "text",
    [
        "Each party's liability is limited to the fees paid.",
        "including but not limited to the services",
        "Fees include, without limitation, travel costs.",
        "Liability is capped at the fees, without limitation of the other remedies.",
        "The term shall not be limited by any renewal.",
    ],
)
def test_ordinary_wording_is_not_express(text: str) -> None:
    assert not scan(text).express


@pytest.mark.parametrize(
    ("text", "pattern"),
    [
        (
            "Supplier shall have unlimited liability for breach of confidentiality.",
            "unlimited_liability",
        ),
        ("Licensor's liability for IP claims shall be unlimited.", "liability_is_unlimited"),
        ("Each party is liable without limit for gross negligence.", "liability_without_limit"),
        ("There is no limit on either party's liability for fraud.", "no_limit_on_liability"),
        ("Indemnity obligations are uncapped.", "uncapped"),
    ],
)
def test_each_express_pattern(text: str, pattern: str) -> None:
    assert pattern in scan(text).patterns and scan(text).express


@pytest.mark.parametrize(
    ("text", "pattern"),
    [
        (
            "The foregoing limitations shall not apply to breach of Section 9.",
            "limitation_shall_not_apply",
        ),
        (
            "Nothing in this Agreement limits either party's liability for fraud.",
            "nothing_limits_liability",
        ),
        (
            "except for liability arising from a party's indemnity obligations",
            "except_for_liability",
        ),
    ],
)
def test_each_carve_out_pattern(text: str, pattern: str) -> None:
    result = scan(text)
    assert pattern in result.patterns and result.carve_out and not result.express


def test_the_record_holds_pattern_ids_never_text() -> None:
    record = scan(doc("umbrella_data.md")).as_record()
    assert record == {
        "version": VERSION,
        "express": True,
        "carve_out": False,
        "patterns": ["liability_not_limited"],
    }
    assert "umbrella" not in str(record).lower()


def test_normalize_folds_case_and_whitespace() -> None:
    assert normalize("Liability\n\t IS  Not​Limited") == "liability is notlimited"
