"""Downloads CUAD v1 from Zenodo, checks it against a pinned hash, and extracts it safely.

    python -m bench data           (or: make bench-data)

The archive is untrusted input: it is checked against `PINNED_SHA256` before anything is
extracted, every member path must stay inside the target directory, and nothing in it is ever
run. Until the hash is pinned (after the first, approved download), the download stops after
printing the hash so it can be reviewed and pinned here.
"""

import hashlib
import shutil
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path

URL = "https://zenodo.org/records/4595826/files/CUAD_v1.zip?download=1"
# CUAD v1, Zenodo record 4595826. Pinned 2026-11-08 after its MD5 matched the one Zenodo
# publishes (c38f490a984420b8a62600db401fafd5).
PINNED_SHA256: str | None = "88b694d99007d39777fa44cd72daf8297773d285dc3eab0091ba32078888d18e"
DATA_DIR = Path(".bench-data")
ARCHIVE_NAME = "CUAD_v1.zip"
_MAX_BYTES = 300 * 1024 * 1024  # the archive is about 106 MB
_MAX_EXTRACTED = 1024 * 1024 * 1024

Download = Callable[[str, Path], None]


class FetchError(Exception):
    pass


class UnpinnedArchiveError(FetchError):
    def __init__(self, sha256: str) -> None:
        super().__init__(
            f"downloaded archive has SHA-256 {sha256}; review it, then set PINNED_SHA256 in "
            "bench/cuad/fetch.py to extract it"
        )
        self.sha256 = sha256


def http_download(url: str, dest: Path) -> None:
    with urllib.request.urlopen(url, timeout=60) as response, dest.open("wb") as out:  # noqa: S310
        copied = 0
        while chunk := response.read(1024 * 1024):
            copied += len(chunk)
            if copied > _MAX_BYTES:
                raise FetchError("download is larger than CUAD should be")
            out.write(chunk)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def fetch(
    data_dir: Path = DATA_DIR,
    *,
    url: str = URL,
    pinned: str | None = PINNED_SHA256,
    download: Download = http_download,
) -> Path:
    """Make sure the pinned archive is in `data_dir` and extracted; return the extracted root."""
    data_dir.mkdir(parents=True, exist_ok=True)
    archive = data_dir / ARCHIVE_NAME
    if not archive.exists():
        partial = archive.with_suffix(".part")
        partial.unlink(missing_ok=True)
        download(url, partial)
        partial.replace(archive)
    actual = sha256_of(archive)
    if pinned is None:
        raise UnpinnedArchiveError(actual)
    if actual != pinned:
        raise FetchError("archive does not match the pinned SHA-256; delete it and download again")
    target = data_dir / "CUAD_v1"
    if not (target / ".extracted").exists():
        if target.exists():
            shutil.rmtree(target)
        safe_extract(archive, target)
        (target / ".extracted").write_text(actual + "\n")
    return find_root(target)


def safe_extract(archive: Path, target: Path) -> None:
    """Extract only regular files, only inside `target`, within a size limit."""
    target.mkdir(parents=True, exist_ok=True)
    base = target.resolve()
    with zipfile.ZipFile(archive) as zf:
        members = zf.infolist()
        if sum(m.file_size for m in members) > _MAX_EXTRACTED:
            raise FetchError("archive expands to more than CUAD should")
        for member in members:
            mode = (member.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise FetchError("archive contains a symbolic link")
            destination = (base / member.filename).resolve()
            if not destination.is_relative_to(base):
                raise FetchError("archive has a path that leaves the target directory")
            if member.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as source, destination.open("wb") as out:
                shutil.copyfileobj(source, out)


def find_root(target: Path) -> Path:
    """The directory holding master_clauses.csv (the archive may nest it one level down)."""
    for candidate in (target, *sorted(p for p in target.iterdir() if p.is_dir())):
        if (candidate / "master_clauses.csv").is_file():
            return candidate
    raise FetchError("extracted archive has no master_clauses.csv")
