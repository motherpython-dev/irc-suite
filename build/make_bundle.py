"""Собирает обновляемый бандл + манифест для автообновления.

  python build/make_bundle.py 1.0.1

На выходе в dist/bundles/:
    racecontrol-1.0.1.zip     — то, что качает клиент (десятки КБ)
    manifest.json             — то, что кладётся на сервер обновлений

Клиент качает ТОЛЬКО zip. Инсталлятор перевыпускается лишь тогда, когда
меняется ядро (bootstrap/зависимости) — тогда подними min_core.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
OUT = ROOT / "dist" / "bundles"

BASE_URL = "https://updates.ichamps.example/suite"   # заменить на реальный
MIN_CORE = "2.0.0"

EXCLUDE = {"__pycache__", ".pytest_cache", ".mypy_cache"}


def bump_version(version: str) -> None:
    (APP / "suite" / "core" / "version.py").write_text(
        f'VERSION = "{version}"\n', "utf-8")


def build(version: str) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    zpath = OUT / f"suite-{version}.zip"
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

    manifest = {
        "version": version,
        "url": f"{BASE_URL}/racecontrol-{version}.zip",
        "sha256": digest,
        "min_core": MIN_CORE,
        "notes": "",
    }
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), "utf-8")

    size_kb = zpath.stat().st_size / 1024
    print(f"бандл:    {zpath}  ({size_kb:.1f} КБ)")
    print(f"sha256:   {digest}")
    print(f"манифест: {OUT / 'manifest.json'}")
    print("\nВыложи оба файла на сервер обновлений; клиенты подхватят сами.")


if __name__ == "__main__":
    main()
