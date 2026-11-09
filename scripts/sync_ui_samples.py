"""Copy the synthetic sample documents into the UI's static files.

The UI offers them as one-click demo inputs. The objectives come from scripts/live_smoke.py, so
there is one list of demo cases. Run after changing samples:  python scripts/sync_ui_samples.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from live_smoke import CASES  # noqa: E402

OUT = ROOT / "ui" / "public" / "samples"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    index = []
    for profile, cases in CASES.items():
        folder = ROOT / "src" / "gawe" / "apps" / profile / "samples"
        for filename, objective in cases:
            (OUT / filename).write_text((folder / filename).read_text(encoding="utf-8"))
            index.append(
                {"file": filename, "profile": profile, "objective": objective, "title": filename}
            )
    (OUT / "index.json").write_text(json.dumps(index, indent=2) + "\n")
    print(f"wrote {len(index)} samples to {OUT}")


if __name__ == "__main__":
    main()
