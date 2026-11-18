"""Сеть и файлы RUTP: сервер ливрей, ключ установки, запись в папку paint.

Перенесено из агента RUTP (`rutp/agent/worker.py`) почти без изменений,
только на стандартной библиотеке вместо httpx. Сервер стоит вне РФ и сам
ходит в Trading Paints:

    участники сессии -> POST /resolve -> GET файлов -> папка paint

Правила записи — те же, что у агента:
  * чужие ливреи пишутся в сессии и запоминаются, а после сессии удаляются
    ровно они; список переживает перезапуск (written.json);
  * свою ливрею iRacing держит занятой, пока идёт сессия, — её пишем вне
    сессии и не удаляем;
  * временный файл создаётся вне папки paint: иначе OneDrive перехватывает
    недописанный файл.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import urllib.error
import urllib.request
import uuid
from pathlib import Path

BACKEND_URL = "https://testedtest.ru/rutp"
# Секрет активации из сборки агента: даёт право получить персональный ключ,
# но сам доступа к ливреям не даёт.
ENROLL_SECRET = "5c583603430ff3a3ac2497d55753004ece427c0a0188582d"
# Где жил отдельный агент: оттуда один раз забираем ключ и папку paint.
LEGACY_DIR = Path(os.environ.get("APPDATA") or Path.home()) / "RUTP"


class KeyRejected(Exception):
    """Сервер отверг ключ (401): сбросить и активироваться заново."""


class Backend:
    def __init__(self, url: str = BACKEND_URL):
        self.base = url.rstrip("/")
        self.key = ""
        # сначала мимо системного прокси, как у агента (прокси в РФ часто режет
        # запросы), а не вышло — через него (core/net.py)
        from ...core import net
        self._open = lambda req, timeout=60: net.urlopen(req, timeout, prefer="direct")

    def _req(self, method: str, path: str, body: dict | None = None,
             headers: dict | None = None, timeout: float = 60) -> bytes:
        h = {"User-Agent": "iRC-RUTP"}
        if self.key:
            h["X-API-Key"] = self.key
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, headers=h, method=method)
        try:
            with self._open(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise KeyRejected() from e
            raise

    def enroll(self, device_id: str) -> str:
        """Персональный ключ по секрету активации."""
        raw = self._req("POST", "/enroll", {"deviceId": device_id},
                        headers={"X-Enroll-Secret": ENROLL_SECRET}, timeout=30)
        return str(json.loads(raw).get("apiKey") or "")

    def resolve(self, drivers: list[dict], ctx: dict) -> list[dict]:
        """Какие файлы есть у этих пилотов: [{custId, carPath, files: [...]}]."""
        payload = {"drivers": [{"custId": d["custId"], "carPath": d["carPath"],
                                "teamId": d.get("teamId", 0), "carNumber": d.get("carNumber", "")}
                               for d in drivers],
                   "session": ctx}
        return json.loads(self._req("POST", "/resolve", payload)).get("drivers", [])

    def download(self, url: str) -> bytes:
        return self._req("GET", url)


def save_file(dest: Path, data: bytes) -> bool:
    """Записать файл. False — файл занят iRacing или сбой диска (повторим позже)."""
    tmp = None
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="rutp_", suffix=".tga")
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        try:
            os.replace(tmp, dest)           # тот же диск — атомарно
        except OSError:
            shutil.move(tmp, str(dest))     # другой диск — с копированием
        return True
    except OSError:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return False


class Written:
    """Файлы чужих ливрей, которые мы положили, — чтобы убрать после сессии."""

    def __init__(self, path: Path):
        self.path = path
        self._paths: set[str] = set()
        try:
            self._paths = set(json.loads(path.read_text("utf-8")))
        except (OSError, ValueError):
            pass

    def _flush(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(sorted(self._paths)), encoding="utf-8")
        except OSError:
            pass

    def add(self, path: str) -> None:
        self._paths.add(path)
        self._flush()

    def cleanup(self) -> int:
        n = 0
        for p in list(self._paths):
            try:
                Path(p).unlink(missing_ok=True)
                n += 1
            except OSError:
                pass
        self._paths.clear()
        self._flush()
        return n


def legacy_config() -> dict:
    """Настройки отдельного агента: ключ, номер установки, папка paint."""
    try:
        data = json.loads((LEGACY_DIR / "config.json").read_text("utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def new_device_id() -> str:
    return uuid.uuid4().hex


def suggest_paint_folder() -> str:
    """Где обычно лежит папка paint — только подсказка, выбирает человек."""
    home = Path.home()
    for base in (home / "OneDrive" / "Documents", home / "OneDrive" / "Документы",
                 home / "Documents"):
        p = base / "iRacing" / "paint"
        if p.is_dir():
            return str(p)
    return ""
