"""Выпуск новой версии: бандл, манифест и выкладка в релизы; установщик — по надобности.

    python build/publish.py 2.18.0 --notes "что нового"
    python build/publish.py 2.18.0 --installer     # ядро поменялось: пересобрать установщик
    python build/publish.py 2.18.0 --dry-run       # собрать, но не выкладывать

Что получают пользователи:
  * бандл (irc-<версия>.zip) — логика приложения, сотни КБ. Программа качает
    и ставит его сама, изменения видны после перезапуска;
  * установщик (iRC-setup.exe) — для новых пользователей и когда меняется ядро
    (bootstrap, зависимости, точка входа). Лежит по ПОСТОЯННОМУ адресу —
    релиз «installer» — и пересобирается только с --installer. Один и тот же
    файл месяцами: SmartScreen копит репутацию по отпечатку файла, и
    предупреждение «неизвестное приложение» со временем уходит; на проверку в
    Microsoft его тоже достаточно отправить один раз. Если ядро поменялось, а
    --installer не указан, выпуск остановится;
  * manifest.json — что сейчас свежее. Лежит по постоянному адресу
    .../releases/latest/download/manifest.json, он же прописан в конфиге.

Права: нужен gh CLI со входом в аккаунт, у которого есть доступ к репозиторию.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "dist" / "bundles"
INSTALLERS = ROOT / "dist" / "installer"
REPO = "motherpython-dev/irc-releases"
BASE = f"https://github.com/{REPO}/releases/download"
# релиз с установщиком: не «последний», чтобы не перехватить манифест у latest
INSTALLER_TAG = "installer"
INSTALLER_NAME = "iRC-setup.exe"
INSTALLER_URL = f"{BASE}/{INSTALLER_TAG}/{INSTALLER_NAME}"
ISCC = Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe")


def run(args, **kw) -> subprocess.CompletedProcess:
    print("$", " ".join(str(a) for a in args))
    r = subprocess.run(args, text=True, **kw)
    if r.returncode:
        sys.exit(f"команда завершилась с кодом {r.returncode}")
    return r


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def core_version() -> str:
    t = (ROOT / "bootstrap" / "version.py").read_text("utf-8")
    return t.split("=", 1)[1].strip().strip('"\'')


def set_installer_version(version: str) -> None:
    p = ROOT / "build" / "installer.iss"
    raw = p.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    txt = re.sub(r'#define AppVersion     "[^"]+"',
                 f'#define AppVersion     "{version}"', raw.decode("utf-8-sig"))
    p.write_bytes((b"\xef\xbb\xbf" if bom else b"") + txt.encode("utf-8"))


def installer_state() -> dict | None:
    """Что сейчас лежит по постоянному адресу: {core, version, sha256} или None."""
    tmp = ROOT / "build" / "_installer_state"
    shutil.rmtree(tmp, ignore_errors=True)
    r = subprocess.run(["gh", "release", "download", INSTALLER_TAG, "-R", REPO,
                        "-p", "installer.json", "-D", str(tmp)], capture_output=True, text=True)
    if r.returncode:
        return None
    try:
        return json.loads((tmp / "installer.json").read_text("utf-8"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def publish_installer(installer: Path, ver: str, core: str) -> dict:
    """Положить установщик по постоянному адресу (с заменой файла)."""
    state = {"core": core, "version": ver, "sha256": sha256(installer)}
    spath = INSTALLERS / "installer.json"
    spath.write_text(json.dumps(state, indent=2), "utf-8")
    have = subprocess.run(["gh", "release", "view", INSTALLER_TAG, "-R", REPO],
                          capture_output=True, text=True).returncode == 0
    if not have:
        run(["gh", "release", "create", INSTALLER_TAG, "-R", REPO, "--latest=false",
             "-t", "Установщик iRC", "-n",
             "Постоянный адрес установщика. Обновления приходят в программу сами."])
    run(["gh", "release", "upload", INSTALLER_TAG, str(installer), str(spath),
         "-R", REPO, "--clobber"])
    return state


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("version")
    ap.add_argument("--notes", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-build", action="store_true",
                    help="взять уже собранные бандл и установщик")
    ap.add_argument("--installer", action="store_true",
                    help="пересобрать установщик и заменить его по постоянному адресу "
                         "(нужно, когда поменялось ядро)")
    ap.add_argument("--replace", action="store_true",
                    help="перезаписать файлы уже выпущенной версии (осознанно)")
    a = ap.parse_args()
    ver = a.version

    # Номер версии — единственное, по чему клиенты решают, обновляться ли.
    # Повторный выпуск под старым номером молча перезаписал бы чужой релиз,
    # а клиенты с более новой версией его даже не увидели бы.
    exists = subprocess.run(["gh", "release", "view", f"v{ver}", "-R", REPO],
                            capture_output=True, text=True).returncode == 0
    if exists and not a.replace and not a.dry_run:
        latest = subprocess.run(["gh", "release", "list", "-R", REPO, "--limit", "1",
                                 "--json", "tagName", "--jq", ".[0].tagName"],
                                capture_output=True, text=True).stdout.strip()
        sys.exit(f"версия {ver} уже выпущена (последняя: {latest}). "
                 f"Возьмите номер новее или добавьте --replace.")
    bundle = OUT / f"irc-{ver}.zip"
    installer = INSTALLERS / INSTALLER_NAME
    core = core_version()

    # Установщик пересобираем только с --installer. Без него — проверяем, что
    # по постоянному адресу лежит установщик с тем же ядром: иначе новые
    # пользователи получили бы ядро, с которым свежий бандл не запустится.
    state = None if a.installer else installer_state()
    if not a.installer and (not state or state.get("core") != core):
        have = state.get("core") if state else "нет"
        sys.exit(f"ядро {core}, а у установщика по постоянному адресу — {have}. "
                 f"Добавьте --installer: установщик пересоберётся и заменится.")

    if not a.skip_build:
        run([sys.executable, str(ROOT / "build" / "make_bundle.py"), ver], cwd=ROOT)
        if a.installer:
            shutil.rmtree(ROOT / "build" / "_work", ignore_errors=True)
            shutil.rmtree(ROOT / "dist" / "iRC", ignore_errors=True)
            run([sys.executable, "-m", "PyInstaller", str(ROOT / "build" / "irc.spec"),
                 "--noconfirm", "--clean", "--distpath", str(ROOT / "dist"),
                 "--workpath", str(ROOT / "build" / "_work")], cwd=ROOT)
            set_installer_version(ver)
            run([str(ISCC), str(ROOT / "build" / "installer.iss")], cwd=ROOT)

    for f in (bundle, installer) if a.installer else (bundle,):
        if not f.is_file():
            sys.exit(f"нет файла: {f}")

    if a.installer and not a.dry_run:
        state = publish_installer(installer, ver, core)
    elif a.installer:
        state = {"core": core, "version": ver, "sha256": sha256(installer)}

    manifest = {
        "version": ver,
        "url": f"{BASE}/v{ver}/{bundle.name}",
        "sha256": sha256(bundle),
        # обновление бандла хватает, пока у пользователя ядро не старее этого
        "min_core": core,
        "notes": a.notes,
        "core": {"version": state["core"], "url": INSTALLER_URL, "sha256": state["sha256"]},
    }
    mpath = OUT / "manifest.json"
    mpath.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), "utf-8")
    print(json.dumps(manifest, indent=2, ensure_ascii=False))

    if a.dry_run:
        print("\n--dry-run: файлы собраны, в релизы ничего не выложено")
        return

    title = f"iRC {ver}"
    notes = a.notes or f"Версия {ver}"
    have = subprocess.run(["gh", "release", "view", f"v{ver}", "-R", REPO],
                          capture_output=True, text=True).returncode == 0
    if have:
        run(["gh", "release", "upload", f"v{ver}", str(bundle),
             str(mpath), "-R", REPO, "--clobber"])
    else:
        run(["gh", "release", "create", f"v{ver}", str(bundle),
             str(mpath), "-R", REPO, "-t", title, "-n", notes, "--latest"])
    print(f"\nвыложено: https://github.com/{REPO}/releases/tag/v{ver}")
    print("клиенты увидят обновление в течение часа")


if __name__ == "__main__":
    main()
