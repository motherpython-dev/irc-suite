"""Собирает обновляемый бандл приложения.

  python build/make_bundle.py 2.18.0

На выходе dist/bundles/irc-2.18.0.zip — то, что качает клиент (сотни КБ), и
обновлённый app/suite/core/version.py.

Выпуск версии целиком (бандл + установщик + манифест + выкладка в релизы) —
build/publish.py; манифест пишется там, где известны адреса релиза.
"""
from __future__ import annotations

import hashlib
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
OUT = ROOT / "dist" / "bundles"

EXCLUDE = {"__pycache__", ".pytest_cache", ".mypy_cache"}


def bump_version(version: str) -> None:
    (APP / "suite" / "core" / "version.py").write_text(
        f'VERSION = "{version}"\n', "utf-8")


def build(version: str) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    zpath = OUT / f"irc-{version}.zip"
    if zpath.exists():
        zpath.unlink()

    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(APP.rglob("*")):
            if any(part in EXCLUDE for part in p.parts):
                continue
            if p.is_file():
                zf.write(p, p.relative_to(APP).as_posix())
    return zpath


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    version = sys.argv[1]
    bump_version(version)
    zpath = build(version)
    digest = hashlib.sha256(zpath.read_bytes()).hexdigest()
    size_kb = zpath.stat().st_size / 1024
    print(f"бандл:  {zpath}  ({size_kb:.1f} КБ)")
    print(f"sha256: {digest}")


if __name__ == "__main__":
    main()
