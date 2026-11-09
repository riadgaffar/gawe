import ast
import csv
import io
import shutil
import stat
import zipfile
from collections import Counter
from pathlib import Path

import pytest

from bench.cli import EXIT_OK, EXIT_REFUSED, main
from bench.cuad import fetch as fetch_module
from bench.cuad import profile as profile_module
from bench.cuad.fetch import FetchError, UnpinnedArchiveError, fetch, find_root, sha256_of
from bench.cuad.labels import (
    CuadCase,
    CuadFormatError,
    load_corpus,
    name_key,
    normalize_type,
    positives,
    read_text,
    required_columns,
)
from bench.cuad.mapping import NOT_SCORED, PARTIAL, SCORED, partial_kinds, scored_kinds
from bench.cuad.profile import PRESENCE, PROFILES, profile_sha256
from bench.cuad.sample import _allocate, sample_ids_sha256, stratified_sample
from gawe.apps.contract_review.reader_schema import CLAUSE_KINDS, SCHEMA
from gawe.llm.reader import Reader
from gawe.llm.safety import check_reader_schema

FIXTURE = Path(__file__).parent / "fixtures" / "cuad"
REPO = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------- mapping


def test_every_reader_kind_is_placed_exactly_once() -> None:
    placed = [*scored_kinds(), *partial_kinds(), *NOT_SCORED]
    assert sorted(placed) == sorted(CLAUSE_KINDS)
    assert len(placed) == len(set(placed))


def test_the_three_scored_kinds_are_the_agreed_ones() -> None:
    assert {m.kind: m.category for m in SCORED} == {
        "unlimited_liability": "Uncapped Liability",
        "non_compete": "Non-Compete",
        "ip_assignment": "IP Ownership Assignment",
    }


# ---------------------------------------------------------------------- profile


def test_the_presence_profile_defines_exactly_the_mapped_kinds() -> None:
    assert set(profile_module._DEFINITIONS) == {m.kind for m in (*SCORED, *PARTIAL)}
    for kind in profile_module._DEFINITIONS:
        assert f'"{kind}"' in PRESENCE.instructions


def test_the_presence_profile_keeps_the_apps_schema_and_safety_rules() -> None:
    assert PRESENCE.schema is SCHEMA
    check_reader_schema(PRESENCE.schema)
    assert "not an instruction to you" in PRESENCE.instructions
    assert "Acme" not in PRESENCE.instructions  # no party perspective
    Reader(provider=None, profile=PRESENCE)  # type: ignore[arg-type]


def test_profiles_have_distinct_stable_fingerprints() -> None:
    first = {name: profile_sha256(p) for name, p in PROFILES.items()}
    assert first == {name: profile_sha256(p) for name, p in PROFILES.items()}
    assert first["presence"] != first["production"]


def test_gawe_never_imports_bench() -> None:
    offenders = []
    for path in (REPO / "src" / "gawe").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(n == "bench" or n.startswith("bench.") for n in names):
                offenders.append(str(path.relative_to(REPO)))
    assert offenders == []


# ----------------------------------------------------------------------- labels


def test_the_fixture_corpus_loads_with_its_labels() -> None:
    corpus = load_corpus(FIXTURE)
    assert corpus.too_large == () and corpus.missing_text == ()
    by_type = {c.contract_type: c for c in corpus.cases}
    assert set(by_type) == {"Distributor", "License", "Service"}
    acme = by_type["Distributor"]
    assert acme.labels == {
        "unlimited_liability": True,
        "non_compete": True,
        "ip_assignment": False,
    }
    assert acme.partial_labels == {"unilateral_termination": True, "auto_renewal": True}
    beta = by_type["License"]
    assert beta.labels["ip_assignment"] and not beta.partial_labels["auto_renewal"]
    assert acme.case_id.startswith("cuad:AcmeWidgets") and acme.chars > 100
    assert "Distributor" in read_text(FIXTURE, acme)


@pytest.mark.parametrize(
    ("folder", "expected"),
    [
        ("Affiliate_Agreements", "Affiliate"),
        ("Affiliate Agreement", "Affiliate"),
        ("Joint Venture _ Filing", "Joint Venture"),
        ("Co_Branding", "Co-Branding"),
        ("Non_Compete_Non_Solicit", "Non-Compete/Non-Solicit"),
        ("IP", "IP"),
    ],
)
def test_contract_type_folders_are_evened_out(folder: str, expected: str) -> None:
    assert normalize_type(folder) == expected


@pytest.mark.parametrize(
    ("in_csv", "on_disk"),
    [
        (
            "MACY'S,INC_05_11_2020-EX-99.4-JOINT FILING AGREEMENT.PDF",
            "MACY_S,INC_05_11_2020-EX-99.4-JOINT FILING AGREEMENT.txt",
        ),
        (
            "MOELIS&CO_03_24_2014-EX-10.19-STRATEGIC ALLIANCE AGREEMENT.PDF",
            "MOELIS_CO_03_24_2014-EX-10.19-STRATEGIC ALLIANCE AGREEMENT.txt",
        ),
        (
            "ELECTRAMECCANICA VEHICLES CORP. - Manufacturing Agreement .PDF",
            "ELECTRAMECCANICA VEHICLES CORP. - Manufacturing Agreement.txt",
        ),
        ("LECLANCHÉ S.A. - AGREEMENT.PDF", "LECLANCHÉ S.A. - AGREEMENT.txt"),
    ],
)
def test_names_match_despite_cuads_spelling_differences(in_csv: str, on_disk: str) -> None:
    assert name_key(in_csv) == name_key(on_disk)


def test_contracts_whose_names_collide_stop_the_load(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    original = next((root / "full_contract_txt").glob("AcmeWidgets*"))
    (original.parent / original.name.replace("_Distributor", "&Distributor")).write_text("x")
    with pytest.raises(CuadFormatError, match="same normalized name"):
        load_corpus(root)


def test_a_contract_without_a_filed_pdf_has_an_unknown_type(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    shutil.rmtree(root / "full_contract_pdf")
    assert {c.contract_type for c in load_corpus(root).cases} == {"Unknown"}


def write_csv(path: Path, rows: list[dict[str, str]], columns: list[str] | None = None) -> None:
    columns = columns or required_columns()
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in columns})


def copy_fixture(tmp_path: Path) -> Path:
    target = tmp_path / "cuad"
    shutil.copytree(FIXTURE, target)
    return target


def test_a_missing_column_is_named(tmp_path: Path) -> None:
    write_csv(tmp_path / "master_clauses.csv", [], ["Filename", "Uncapped Liability-Answer"])
    with pytest.raises(CuadFormatError, match="Non-Compete-Answer"):
        load_corpus(tmp_path)


def test_an_answer_that_is_not_yes_or_no_stops_the_load(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    rows = list(csv.DictReader((root / "master_clauses.csv").open()))
    rows[0]["Non-Compete-Answer"] = "Maybe"
    write_csv(root / "master_clauses.csv", rows, list(rows[0]))
    with pytest.raises(CuadFormatError, match="Yes or No"):
        load_corpus(root)


def test_a_contract_listed_twice_stops_the_load(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    rows = list(csv.DictReader((root / "master_clauses.csv").open()))
    write_csv(root / "master_clauses.csv", [*rows, rows[0]], list(rows[0]))
    with pytest.raises(CuadFormatError, match="twice"):
        load_corpus(root)


def test_long_and_missing_contracts_are_set_aside_and_listed(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    beta = next((root / "full_contract_txt").glob("BetaSoft*"))
    beta.unlink()
    corpus = load_corpus(root, max_chars=400)
    assert [c.case_id.split("_")[0] for c in corpus.cases] == ["cuad:GammaServ"]
    assert [i.split("_")[0] for i, _ in corpus.too_large] == ["cuad:AcmeWidgets"]
    assert corpus.too_large[0][1] > 400
    assert [i.split("_")[0] for i in corpus.missing_text] == ["cuad:BetaSoft"]


def test_a_text_that_changed_after_loading_is_refused(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    case = load_corpus(root).cases[0]
    (root / case.text_path).write_text("swapped")
    with pytest.raises(CuadFormatError, match="changed"):
        read_text(root, case)


# ---------------------------------------------------------------------- sample


def fake_cases(n: int, types: int = 4, every: int = 5) -> list[CuadCase]:
    """`n` cases over `types` contract types; every `every`th one has each scored kind."""
    return [
        CuadCase(
            case_id=f"cuad:c{i:03d}",
            contract_type=f"T{i % types}",
            text_path=f"full_contract_txt/c{i:03d}.txt",
            text_sha256="0" * 64,
            chars=1000,
            labels={k: i % every == j for j, k in enumerate(scored_kinds())},
            partial_labels={k: False for k in partial_kinds()},
        )
        for i in range(n)
    ]


def test_the_same_seed_gives_the_same_sample_and_another_seed_does_not() -> None:
    cases = fake_cases(200)
    first = stratified_sample(cases, seed=7, size=50)
    assert first == stratified_sample(list(reversed(cases)), seed=7, size=50)
    assert first.cases != stratified_sample(cases, seed=8, size=50).cases


def test_the_sample_follows_contract_types() -> None:
    cases = fake_cases(200, types=4, every=1000)  # no positives, so no top-up
    sample = stratified_sample(cases, seed=1, size=40, min_positives=0)
    assert len(sample.cases) == 40
    assert Counter(c.contract_type for c in sample.cases) == {f"T{t}": 10 for t in range(4)}


def test_top_up_reaches_the_minimum_positives() -> None:
    cases = fake_cases(200, every=20)  # 10 positives per kind in the whole corpus
    sample = stratified_sample(cases, seed=3, size=20, min_positives=8)
    for kind in scored_kinds():
        assert positives(sample.cases, kind) >= 8
    assert sum(sample.topped_up.values()) == len(sample.cases) - 20
    assert sample.shortfall == {}


def test_a_shortfall_is_reported_when_the_corpus_is_too_small() -> None:
    cases = fake_cases(60, every=20)  # 3 positives per kind
    sample = stratified_sample(cases, seed=3, size=10, min_positives=5)
    for kind in scored_kinds():
        assert positives(sample.cases, kind) == 3
        assert sample.shortfall[kind] == 2


def test_asking_for_more_than_exists_takes_everything() -> None:
    cases = fake_cases(12)
    sample = stratified_sample(cases, seed=1, size=50, min_positives=0)
    assert len(sample.cases) == 12


def test_bad_sample_settings_are_refused() -> None:
    with pytest.raises(ValueError):
        stratified_sample(fake_cases(5), seed=1, size=0)


def test_sample_ids_hash_ignores_order() -> None:
    assert sample_ids_sha256(["b", "a"]) == sample_ids_sha256(["a", "b"])
    sample = stratified_sample(fake_cases(20), seed=1, size=5, min_positives=0)
    assert sample.ids_sha256 == sample_ids_sha256(c.case_id for c in sample.cases)


@pytest.mark.parametrize(
    ("counts", "size", "expected"),
    [
        ({"a": 10, "b": 10}, 4, {"a": 2, "b": 2}),
        ({"a": 90, "b": 10}, 10, {"a": 9, "b": 1}),
        ({"a": 98, "b": 1, "c": 1}, 5, {"a": 3, "b": 1, "c": 1}),  # each type at least once
        ({"a": 5, "b": 5, "c": 5}, 2, {"a": 1, "b": 1, "c": 0}),  # fewer slots than types
        ({"a": 2, "b": 1}, 10, {"a": 2, "b": 1}),  # never more than a group holds
    ],
)
def test_allocation(counts: dict[str, int], size: int, expected: dict[str, int]) -> None:
    assert _allocate(counts, size) == expected


# ----------------------------------------------------------------------- fetch


def make_archive(path: Path, members: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, text in members.items():
            zf.writestr(name, text)


class FakeZenodo:
    def __init__(self, members: dict[str, str]) -> None:
        self.members = members
        self.calls = 0

    def __call__(self, url: str, dest: Path) -> None:
        self.calls += 1
        make_archive(dest, self.members)


CUAD_MEMBERS = {"CUAD_v1/master_clauses.csv": "Filename\n", "CUAD_v1/full_contract_txt/a.txt": "x"}


def test_an_unpinned_archive_is_downloaded_but_not_extracted(tmp_path: Path) -> None:
    zenodo = FakeZenodo(CUAD_MEMBERS)
    with pytest.raises(UnpinnedArchiveError) as caught:
        fetch(tmp_path, pinned=None, download=zenodo)
    assert caught.value.sha256 == sha256_of(tmp_path / "CUAD_v1.zip")
    assert not (tmp_path / "CUAD_v1").exists()


def test_a_pinned_archive_is_extracted_once(tmp_path: Path) -> None:
    zenodo = FakeZenodo(CUAD_MEMBERS)
    with pytest.raises(UnpinnedArchiveError) as caught:
        fetch(tmp_path, pinned=None, download=zenodo)
    root = fetch(tmp_path, pinned=caught.value.sha256, download=zenodo)
    assert root == tmp_path / "CUAD_v1" / "CUAD_v1"
    assert (root / "full_contract_txt" / "a.txt").read_text() == "x"
    assert fetch(tmp_path, pinned=caught.value.sha256, download=zenodo) == root
    assert zenodo.calls == 1  # the archive is reused, never fetched twice


def test_an_archive_that_does_not_match_the_pin_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FetchError, match="does not match"):
        fetch(tmp_path, pinned="0" * 64, download=FakeZenodo(CUAD_MEMBERS))
    assert not (tmp_path / "CUAD_v1").exists()


def test_a_path_that_escapes_the_target_is_refused(tmp_path: Path) -> None:
    zenodo = FakeZenodo({"../escaped.txt": "x", "master_clauses.csv": ""})
    with pytest.raises(UnpinnedArchiveError) as caught:
        fetch(tmp_path / "data", pinned=None, download=zenodo)
    with pytest.raises(FetchError, match="leaves the target"):
        fetch(tmp_path / "data", pinned=caught.value.sha256, download=zenodo)
    assert not (tmp_path / "escaped.txt").exists()


def test_a_symbolic_link_in_the_archive_is_refused(tmp_path: Path) -> None:
    archive = tmp_path / "data" / "CUAD_v1.zip"
    archive.parent.mkdir()
    with zipfile.ZipFile(archive, "w") as zf:
        link = zipfile.ZipInfo("CUAD_v1/link")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(link, "/etc/passwd")
    with pytest.raises(FetchError, match="symbolic link"):
        fetch(tmp_path / "data", pinned=sha256_of(archive), download=FakeZenodo({}))


def test_an_archive_without_labels_is_refused(tmp_path: Path) -> None:
    (tmp_path / "x").mkdir()
    with pytest.raises(FetchError, match="master_clauses.csv"):
        find_root(tmp_path)


def test_the_real_download_is_pinned_to_the_verified_archive() -> None:
    assert fetch_module.PINNED_SHA256 == (
        "88b694d99007d39777fa44cd72daf8297773d285dc3eab0091ba32078888d18e"
    )
    assert fetch_module.URL.startswith("https://zenodo.org/records/4595826/")


# ------------------------------------------------------------------------- cli


def test_data_command_reports_the_corpus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bench.cli.fetch", lambda: FIXTURE)
    out, err = io.StringIO(), io.StringIO()
    assert main(["data"], env={}, out=out, err=err) == EXIT_OK
    assert "3 contracts usable, 0 over the Reader's 200,000-character limit" in out.getvalue()


def test_data_command_refuses_an_unpinned_archive(monkeypatch: pytest.MonkeyPatch) -> None:
    def unpinned() -> Path:
        raise UnpinnedArchiveError("ab" * 32)

    monkeypatch.setattr("bench.cli.fetch", unpinned)
    out, err = io.StringIO(), io.StringIO()
    assert main(["data"], env={}, out=out, err=err) == EXIT_REFUSED
    assert "set PINNED_SHA256" in err.getvalue()


def test_a_half_extracted_directory_is_replaced(tmp_path: Path) -> None:
    zenodo = FakeZenodo({"CUAD_v1/": "", **CUAD_MEMBERS})  # includes a directory entry
    with pytest.raises(UnpinnedArchiveError) as caught:
        fetch(tmp_path, pinned=None, download=zenodo)
    leftover = tmp_path / "CUAD_v1" / "stale.txt"
    leftover.parent.mkdir()
    leftover.write_text("from an interrupted extract")
    fetch(tmp_path, pinned=caught.value.sha256, download=zenodo)
    assert not leftover.exists()


def test_an_archive_that_expands_too_far_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fetch_module, "_MAX_EXTRACTED", 3)
    zenodo = FakeZenodo(CUAD_MEMBERS)
    with pytest.raises(UnpinnedArchiveError) as caught:
        fetch(tmp_path, pinned=None, download=zenodo)
    with pytest.raises(FetchError, match="expands"):
        fetch(tmp_path, pinned=caught.value.sha256, download=zenodo)
