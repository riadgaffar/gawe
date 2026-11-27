"""The packaged demo's command line (M29): `make demo` runs `python -m gawe.demo run`.

    python -m gawe.demo run [--store memory|postgres]   the API and the UI; Ctrl-C stops both
    python -m gawe.demo prepare DIR                      fresh tokens for the demo's people
    python -m gawe.demo seed-sql                         the companies' tenants and users (Postgres)

`run` uses the scripted demo model, so it needs no API key. Both servers listen on 127.0.0.1
only. With memory stores (the default) every start is a fresh demo: runs, and the mock systems
they act on, are forgotten when it stops. With Postgres, runs persist; `make demo
DEMO_STORE=postgres` starts Postgres and runs scripts/demo_postgres.sh, which uses its own
database, gawe_demo, never the development one.
"""

import argparse
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TextIO

from gawe.demo.companies import COMPANIES, SETTINGS_FILE, write_tokens

ROOT = Path(__file__).resolve().parents[3]
HOST = "127.0.0.1"
API_PORT, UI_PORT = 8000, 3000
DEMO_DIR = ROOT / ".demo-data" / "demo"


def api_env(
    base: Mapping[str, str], *, store: str, tokens_dir: Path, systems_dir: Path
) -> dict[str, str]:
    """The API's environment: the contract review app, the scripted model, the companies'
    settings and the demo's tokens. Signing and review-sample keys are made for the run unless
    set."""
    return {
        **base,
        "GAWE_APP": "gawe.apps.contract_review:bundle",
        "GAWE_LLM_PROVIDER": "demo",
        "GAWE_STORE": store,
        "GAWE_TENANT_SETTINGS": str(SETTINGS_FILE),
        "GAWE_API_TOKENS_FILE": str(tokens_dir / "api-tokens.json"),
        "GAWE_DEMO_DIR": str(systems_dir),
        "GAWE_POLICY_SIGNING_KEY": base.get("GAWE_POLICY_SIGNING_KEY") or secrets.token_hex(32),
        "GAWE_REVIEW_SAMPLE_KEY": base.get("GAWE_REVIEW_SAMPLE_KEY") or secrets.token_hex(32),
    }


def ui_env(base: Mapping[str, str], *, tokens_dir: Path) -> dict[str, str]:
    return {
        **base,
        "GAWE_UI_DEMO": "1",
        "GAWE_UI_DEMO_SIGNIN": str(tokens_dir / "sign-in.json"),
        "GAWE_API_URL": f"http://{HOST}:{API_PORT}",
    }


def seed_sql() -> str:
    """The companies' tenants and users, for a Postgres demo (runs refer to both)."""
    lines = ["BEGIN;"]
    for company in COMPANIES:
        lines.append(
            "INSERT INTO tenants (id, name) VALUES "
            f"('{company.tenant_id}', '{company.name}') ON CONFLICT DO NOTHING;"
        )
        for person in company.people:
            lines.append(
                "INSERT INTO users (id, tenant_id, email, display_name) VALUES "
                f"('{person.user_id}', '{company.tenant_id}', '{person.key}@demo.example', "
                f"'{person.name} ({company.name}, {person.title})') ON CONFLICT DO NOTHING;"
            )
    lines.append("COMMIT;")
    return "\n".join(lines) + "\n"


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex((HOST, port)) != 0


def _wait_for_api(timeout: float, out: TextIO) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://{HOST}:{API_PORT}/healthz", timeout=2):
                return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    print("the API did not start; see its output above", file=out)
    return False


def problems(store: str, env: Mapping[str, str]) -> list[str]:
    """What stops `run` before it starts anything."""
    found = []
    if shutil.which("node") is None or shutil.which("npm") is None:
        found.append("Node.js 22.6 or newer is needed for the UI (node and npm on the PATH)")
    if not (ROOT / "ui" / "node_modules").is_dir():
        found.append("the UI's packages aren't installed: run `make ui-install` once")
    for port, what in ((API_PORT, "the API"), (UI_PORT, "the UI")):
        if not _port_free(port):
            found.append(f"port {port} is in use; stop whatever holds it ({what} needs it)")
    if store == "postgres" and not env.get("GAWE_POLICY_SIGNING_KEY"):
        found.append(
            "a Postgres demo keeps its runs, so it needs .env's signing key: run it as "
            "`make demo DEMO_STORE=postgres`"
        )
    return found


def run(store: str, out: TextIO = sys.stdout) -> int:
    base = dict(os.environ)
    found = problems(store, base)
    if found:
        for problem in found:
            print(f"cannot start the demo: {problem}", file=out)
        return 2
    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    tokens_dir = DEMO_DIR / "tokens"
    write_tokens(tokens_dir)
    fresh = store == "memory"
    systems_dir = (
        Path(tempfile.mkdtemp(prefix="systems-", dir=DEMO_DIR)) if fresh else DEMO_DIR / "systems"
    )
    systems_dir.mkdir(parents=True, exist_ok=True)
    # `next dev` rewrites this tracked file; put it back on the way out so a demo leaves git clean.
    next_env = ROOT / "ui" / "next-env.d.ts"
    next_env_text = next_env.read_bytes() if next_env.exists() else None
    processes: list[subprocess.Popen[bytes]] = []
    try:
        processes.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "--factory",
                    "gawe.api.main:create_app_from_env",
                    "--host",
                    HOST,
                    "--port",
                    str(API_PORT),
                ],
                cwd=ROOT,
                env=api_env(base, store=store, tokens_dir=tokens_dir, systems_dir=systems_dir),
            )
        )
        if not _wait_for_api(60, out):
            return 1
        processes.append(
            subprocess.Popen(
                ["npm", "run", "dev", "--", "-H", HOST],
                cwd=ROOT / "ui",
                env=ui_env(base, tokens_dir=tokens_dir),
            )
        )
        print(
            f"\nThe demo is starting. Open http://{HOST}:{UI_PORT}/demo "
            "(the first page takes a few seconds). Ctrl-C stops it.\n",
            file=out,
            flush=True,
        )
        while all(p.poll() is None for p in processes):
            time.sleep(0.5)
        return 1  # one of them stopped on its own
    except KeyboardInterrupt:
        return 0
    finally:
        for process in processes:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        if next_env_text is not None:
            next_env.write_bytes(next_env_text)
        if fresh:
            shutil.rmtree(systems_dir, ignore_errors=True)
        print("demo stopped", file=out)


def main(argv: Sequence[str] | None = None, out: TextIO = sys.stdout) -> int:
    parser = argparse.ArgumentParser(prog="python -m gawe.demo", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("run", help="start the API and the UI with the demo")
    start.add_argument("--store", choices=("memory", "postgres"), default="memory")
    prepare = commands.add_parser("prepare", help="write fresh tokens for the demo's people")
    prepare.add_argument("directory", type=Path)
    commands.add_parser("seed-sql", help="print the companies' tenants and users for Postgres")
    args = parser.parse_args(argv)
    if args.command == "run":
        return run(args.store, out)
    if args.command == "prepare":
        write_tokens(args.directory)
        print(f"wrote api-tokens.json and sign-in.json in {args.directory}", file=out)
        return 0
    print(seed_sql(), end="", file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
