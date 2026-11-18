"""Автообновление логики приложения без перекачки клиента.

Обновляется бандл (Python-код + web-ассеты, десятки-сотни КБ), а не exe.
Перекачка exe нужна только при смене ядра — манифест объявляет это полем
min_core, и тогда пользователю показывается ссылка на новый инсталлятор.

Манифест на сервере:
{
  "version":  "1.0.1",
  "url":      "https://.../bundles/irc-1.0.1.zip",
  "sha256":   "…",
  "min_core": "1.0.0",
  "notes":    "что нового"
}

Безопасность: zip проверяется по sha256 из манифеста, распаковка защищена от
path traversal, переключение версии атомарное, при неудачных стартах —
автоматический откат на last_good.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from . import paths
from .version import CORE_VERSION

USER_AGENT = f"ichamps-suite/{CORE_VERSION}"
MAX_BUNDLE_BYTES = 96 * 1024 * 1024        # установщик ~24 МБ, бандл — сотни КБ
FAIL_LIMIT = 2               # столько неудачных стартов подряд -> откат


def _vt(v: str) -> tuple:
    try:
        return tuple(int(x) for x in v.split("."))
    except Exception:
        return (0,)


def _fetch(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(MAX_BUNDLE_BYTES + 1)


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    dest = dest.resolve()
    for member in zf.namelist():
        target = (dest / member).resolve()
        if not str(target).startswith(str(dest)):
            raise RuntimeError(f"небезопасный путь в архиве: {member}")
    zf.extractall(dest)


class Updater:
    def __init__(self, manifest_url: str = ""):
        self.manifest_url = manifest_url

    # ---------- запуск ----------

    def active_version(self) -> str | None:
        ptr = paths.read_pointer()
        v = ptr.get("current")
        if v and (paths.version_dir(v) / "suite" / "core" / "app.py").exists():
            return v
        return None

    @staticmethod
    def seed_version() -> str:
        """Версия бандла, вшитого в exe. Читается из его version.py, а не из
        версии ядра: это разные вещи и расходятся при обновлениях."""
        vp = paths.seed_dir() / "suite" / "core" / "version.py"
        try:
            for line in vp.read_text("utf-8").splitlines():
                if line.strip().startswith("VERSION"):
                    return line.split("=", 1)[1].strip().strip('"\'')
        except Exception:
            pass
        return "0.0.0"

    def ensure_seeded(self, fallback_version: str = "0.0.0") -> str:
        """Разложить вшитый бандл, если он нужен.

        Разворачиваем не только на первом запуске: после переустановки клиента
        свежий seed внутри exe должен вытеснить старый развёрнутый бандл, иначе
        пользователь поставит новую версию и продолжит работать на старой логике.
        Обновления, скачанные по сети, при этом не затираются — они всегда
        новее seed по версии.
        """
        seed_v = self.seed_version()
        if seed_v == "0.0.0":
            seed_v = fallback_version
        active = self.active_version()

        if active and _vt(active) >= _vt(seed_v):
            return active

        target = paths.version_dir(seed_v)
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True, exist_ok=True)
        shutil.copytree(paths.seed_dir(), target, dirs_exist_ok=True)
        paths.write_pointer({"current": seed_v, "last_good": seed_v, "fails": 0})
        return seed_v

    def note_start(self) -> None:
        """Отметить попытку старта. Если версия падает — откатимся."""
        ptr = paths.read_pointer()
        ptr["fails"] = int(ptr.get("fails", 0)) + 1
        paths.write_pointer(ptr)

    def note_ok(self) -> None:
        """Приложение успешно поднялось — версия признаётся рабочей."""
        ptr = paths.read_pointer()
        ptr["fails"] = 0
        ptr["last_good"] = ptr.get("current")
        paths.write_pointer(ptr)

    def rollback_if_needed(self) -> str | None:
        """Вызывается ДО старта. Вернёт версию, на которую откатились."""
        ptr = paths.read_pointer()
        if int(ptr.get("fails", 0)) < FAIL_LIMIT:
            return None
        good = ptr.get("last_good")
        cur = ptr.get("current")
        if not good or good == cur:
            return None
        ptr["current"] = good
        ptr["fails"] = 0
        paths.write_pointer(ptr)
        return good

    # ---------- обновление ----------

    def check(self) -> dict:
        if not self.manifest_url:
            return {"ok": False, "message": "URL обновлений не настроен"}
        try:
            man = json.loads(_fetch(self.manifest_url).decode("utf-8"))
        except Exception as e:
            return {"ok": False, "message": f"Не удалось получить манифест: {e}"}

        remote = str(man.get("version") or "")
        current = self.active_version() or "0.0.0"
        if _vt(remote) <= _vt(current):
            return {"ok": True, "update": False, "message": f"Установлена последняя версия ({current})"}

        min_core = str(man.get("min_core") or "0.0.0")
        if _vt(CORE_VERSION) < _vt(min_core):
            return {"ok": False, "update": False, "core_update_required": True,
                    "manifest": man, "version": remote,
                    "message": f"Версия {remote} требует нового клиента "
                               f"(ядро {min_core}, установлено {CORE_VERSION}). "
                               f"Нужен новый инсталлятор."}
        return {"ok": True, "update": True, "manifest": man,
                "message": f"Доступна версия {remote}"}

    def apply(self, man: dict) -> dict:
        version = str(man["version"])
        url = str(man["url"])
        want = str(man.get("sha256") or "").lower()

        try:
            blob = _fetch(url, timeout=60)
        except Exception as e:
            return {"ok": False, "message": f"Скачивание не удалось: {e}"}
        if len(blob) > MAX_BUNDLE_BYTES:
            return {"ok": False, "message": "Бандл слишком большой"}

        got = hashlib.sha256(blob).hexdigest()
        if want and got != want:
            return {"ok": False, "message": "Контрольная сумма не сошлась — обновление отклонено"}

        target = paths.version_dir(version)
        staging = Path(tempfile.mkdtemp(prefix="rc-update-"))
        try:
            zpath = staging / "bundle.zip"
            zpath.write_bytes(blob)
            with zipfile.ZipFile(zpath) as zf:
                _safe_extract(zf, staging / "unpacked")
            unpacked = staging / "unpacked"
            if not (unpacked / "suite" / "core" / "app.py").exists():
                return {"ok": False, "message": "В бандле нет suite/core/app.py"}

            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(unpacked, target)
        except Exception as e:
            return {"ok": False, "message": f"Распаковка не удалась: {e}"}
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        ptr = paths.read_pointer()
        ptr["last_good"] = ptr.get("current")     # текущая рабочая — на откат
        ptr["current"] = version
        ptr["fails"] = 0
        paths.write_pointer(ptr)
        self._prune(keep={version, str(ptr.get("last_good") or "")})
        return {"ok": True, "updated": True, "version": version,
                "message": f"Обновлено до {version}. Перезапустите приложение."}

    def fetch_installer(self, man: dict) -> dict:
        """Скачать установщик новой версии — когда обновления бандла мало.

        Бандл обновляется сам, но при смене ядра (зависимости, точка входа)
        нужен новый exe. Его тоже качаем и проверяем по sha256; запускает
        установщик пользователь из панели, молча подменять exe нельзя.
        """
        core = man.get("core") or {}
        url = str(core.get("url") or "")
        want = str(core.get("sha256") or "").lower()
        if not url:
            return {"ok": False, "message": "В манифесте нет установщика"}
        dest = paths.data_dir() / "updates"
        dest.mkdir(parents=True, exist_ok=True)
        name = url.rsplit("/", 1)[-1] or f"iRC-setup-{man.get('version')}.exe"
        target = dest / name
        if target.exists() and want and hashlib.sha256(target.read_bytes()).hexdigest() == want:
            return {"ok": True, "path": str(target), "cached": True}
        try:
            blob = _fetch(url, timeout=180)
        except Exception as e:
            return {"ok": False, "message": f"Скачивание установщика не удалось: {e}"}
        if want and hashlib.sha256(blob).hexdigest() != want:
            return {"ok": False, "message": "Контрольная сумма установщика не сошлась"}
        tmp = target.with_suffix(".part")
        tmp.write_bytes(blob)
        tmp.replace(target)
        for old in dest.glob("*.exe"):          # держим только свежий
            if old != target:
                old.unlink(missing_ok=True)
        return {"ok": True, "path": str(target)}

    def check_and_apply(self) -> dict:
        res = self.check()
        if not res.get("update"):
            return res
        return self.apply(res["manifest"])

    def _prune(self, keep: set[str], keep_last: int = 3) -> None:
        try:
            dirs = sorted(paths.versions_dir().iterdir(), key=lambda p: _vt(p.name))
            for d in dirs[:-keep_last]:
                if d.name not in keep:
                    shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass
