"""Модуль RUTP: ливреи Trading Paints прямо из iRC.

Раньше ливреи тянул отдельный агент RUTP со своим значком в трее, а iRC
только запускал его. Теперь тот же цикл работает внутри программы
(`client.py` — сеть и файлы, здесь — цикл и управление):

    каждые 5 с: участники сессии из общего клиента сима -> новые -> сервер
    RUTP -> файлы в папку paint; конец сессии -> убрать чужие ливреи.

Участников модуль берёт у SimClient (`livery_context`): читать iRacing в
программе может только одно место.

Отдельный агент, если он ещё стоит у человека, мешает: два писателя в одной
папке paint стирают файлы друг друга. Пока он запущен, модуль ничего не
пишет и предлагает закрыть агента и убрать его из автозапуска — по кнопке,
сам не трогает.
"""
from __future__ import annotations

import csv
import io
import json
import os
import subprocess
import threading
import time
from pathlib import Path

from ...core import config as core_config
from ...core.modules import Context
from .client import (BACKEND_URL, LEGACY_DIR, Backend, KeyRejected, Written, legacy_config,
                     new_device_id, save_file, suggest_paint_folder)

POLL_S = 5.0
LEGACY_CHECK_S = 10.0
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _legacy_pids() -> list[int]:
    """Процессы отдельного агента RUTP."""
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq RUTP.exe", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=10,
                             creationflags=NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    pids = []
    for row in csv.reader(io.StringIO(out)):
        if len(row) > 1 and row[0].lower() == "rutp.exe" and row[1].isdigit():
            pids.append(int(row[1]))
    return pids


def _legacy_autostart() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, "RUTP")
        return True
    except OSError:
        return False


SELF_RETRY_S = 30.0                  # своя ливрея в начале сессии: сколько пробовать, если файл занят

class RutpModule:
    id = "rutp"
    title = "RUTP"

    def __init__(self) -> None:
        self.ctx: Context | None = None
        self.backend = Backend()
        self.written: Written | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.state = "выключен"
        self.cur_session = None
        self.processed: set[tuple[int, str]] = set()
        self.drivers: dict[int, dict] = {}       # custId -> строка таблицы
        self.events: list[dict] = []
        self._self_drivers: list[dict] = []
        self._self_dirty = False
        self._self_until = 0.0                    # до какого момента пробовать записать свою ливрею
        self._session_ctx: dict = {}
        self.legacy = {"pids": [], "autostart": False}
        self._legacy_ts = 0.0

    # ---------- настройки ----------

    @property
    def cfg(self) -> dict:
        return self.ctx.config if self.ctx else {}

    def _migrate(self) -> None:
        """Один раз забрать у отдельного агента ключ, номер установки и папку
        paint: не активироваться заново и не выбирать папку второй раз."""
        c = self.cfg
        if c.get("migrated"):
            return
        old = legacy_config()
        for src, dst in (("api_key", "api_key"), ("device_id", "device_id"),
                         ("paint_folder", "paint_folder")):
            if old.get(src) and not c.get(dst):
                c[dst] = old[src]
        c["migrated"] = True
        for k in ("agent_path", "start_with_module"):      # от управления агентом
            c.pop(k, None)
        self.ctx.save_config()
        if old:
            self._event("настройки перенесены из отдельного RUTP")

    # ---------- жизненный цикл ----------

    def start(self, ctx: Context) -> None:
        self.ctx = ctx
        self._migrate()
        if not self.cfg.get("device_id"):
            self.cfg["device_id"] = new_device_id()
            ctx.save_config()
        self.backend.key = str(self.cfg.get("api_key") or "")
        self.written = Written(core_config.data_dir() / "rutp-written.json")
        left = self.written.cleanup()
        if left:
            self._event(f"убраны ливреи прошлого запуска: {left}")
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="rutp", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        if self.written:
            n = self.written.cleanup()          # чужие убираем, своя остаётся
            if n and self.ctx:
                self.ctx.log.info("RUTP: убрано ливрей при остановке: %s", n)
        self.state = "выключен"
        self.cur_session = None
        self.processed.clear()
        self.drivers.clear()
        self.ctx = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except KeyRejected:
                self._invalidate_key()
            except OSError as exc:                       # сеть: URLError, таймаут
                self._set_state(f"сервер RUTP недоступен ({exc.__class__.__name__})")
            except Exception as exc:                     # noqa: BLE001 — цикл не падает
                if self.ctx:
                    self.ctx.log.exception("RUTP: ошибка цикла")
                self._set_state(f"ошибка: {exc}")
            self._stop.wait(POLL_S)

    # ---------- шаг ----------

    def _tick(self) -> None:
        now = time.monotonic()
        if now - self._legacy_ts > LEGACY_CHECK_S:
            self._legacy_ts = now
            self.legacy = {"pids": _legacy_pids(), "autostart": _legacy_autostart()}
        if self.legacy["pids"]:
            self._set_state("работает отдельный RUTP — встроенный ждёт")
            return
        if not self.backend.key:
            self._enroll()
            if not self.backend.key:
                return

        lc = self.ctx.sim.livery_context() if self.ctx else None
        if lc is None:
            if self.cur_session is not None:
                self._end_session()
            # вне сессии файл своей машины свободен — дописываем свою ливрею
            if self._self_dirty and self._write_self():
                self._self_dirty = False
            self._set_state("ждёт сессию iRacing")
            return

        folder = str(self.cfg.get("paint_folder") or "")
        if not folder:
            self._set_state("укажите папку paint")
            return

        if lc["session"] != self.cur_session:
            self._end_session()
            self.cur_session = lc["session"]
            self._event("новая сессия · пилотов: %d" % len(lc["drivers"]))
            # своя ливрея — в начале сессии, свежая с Trading Paints (раньше — после сессии)
            self._self_until = time.monotonic() + SELF_RETRY_S
        self._session_ctx = lc["ctx"]

        for d in lc["drivers"]:
            row = self.drivers.setdefault(d["custId"], {"number": d["number"], "name": d["name"],
                                                         "state": "self" if d["isSelf"] else "wait"})
            row.update(number=d["number"], name=d["name"])
        selfs = [d for d in lc["drivers"] if d["isSelf"]]
        if selfs:
            self._self_drivers = selfs
        if selfs and self._self_until and time.monotonic() < self._self_until:
            # файл может быть занят, пока iRacing грузит машину, — повторяем SELF_RETRY_S
            if self._write_self():
                self._self_until = 0.0
                self._reload_own()
        elif self._self_until and time.monotonic() >= self._self_until:
            self._self_until = 0.0
            self._event("своя ливрея: файл занят — не обновлена")
        new = [d for d in lc["drivers"]
               if (d["custId"], d["carPath"]) not in self.processed and not d["isSelf"]]
        if new:
            self._set_state(f"загружаю ливреи: +{len(new)}")
            self._fetch(new, Path(folder))
        self._set_state(f"в сессии · ливрей: {self._loaded()}")

    def _fetch(self, drivers: list[dict], folder: Path) -> None:
        got = self.backend.resolve(drivers, self._session_ctx)
        found = set()
        for drv in got:
            files = drv.get("files") or []
            for f in files:
                data = self.backend.download(f["url"])
                dest = folder / f["directory"] / f["filename"]
                if save_file(dest, data):
                    self.written.add(str(dest))
            cid = drv.get("custId")
            found.add(cid)
            self.processed.add((cid, drv.get("carPath")))
            if cid in self.drivers:
                self.drivers[cid]["state"] = "ok" if files else "none"
        for d in drivers:                       # сервер ничего не нашёл — на TP нет
            if d["custId"] not in found:
                self.processed.add((d["custId"], d["carPath"]))
                self.drivers[d["custId"]]["state"] = "none"
        n = sum(1 for d in drivers if self.drivers[d["custId"]]["state"] == "ok")
        self._event(f"загружено ливрей: {n} из {len(drivers)}")

    def _write_self(self) -> bool:
        """Своя ливрея — в начале сессии, свежая с Trading Paints; после записи iRacing
        перечитывает текстуру своей машины (reload_texture). Не удаляется.
        False — файл ещё занят, повторим на следующем шаге."""
        if not self._self_drivers:
            return True
        folder = str(self.cfg.get("paint_folder") or "")
        if not (folder and self.backend.key):
            return False
        ok = True
        for drv in self.backend.resolve(self._self_drivers, self._session_ctx):
            for f in drv.get("files") or []:
                if not save_file(Path(folder) / f["directory"] / f["filename"],
                                 self.backend.download(f["url"])):
                    ok = False
        if ok:
            self._event("своя ливрея обновлена")
        return ok

    def _reload_own(self) -> None:
        """Новая своя ливрея — сразу на машине: iRacing перечитывает текстуру по команде."""
        try:
            sim = self.ctx.sim
            idx = sim.snapshot().player_car_idx
            ir = getattr(sim, "ir", None)
            if ir is not None and idx is not None and idx >= 0:
                ir.reload_texture(int(idx))
        except Exception:                        # noqa: BLE001 — без перезагрузки подхватится при выезде
            pass

    def _end_session(self) -> None:
        removed = self.written.cleanup() if self.written else 0
        if self.cur_session is not None:
            self._event(f"сессия закончилась · убрано ливрей: {removed}")
        self._self_until = 0.0
        self.processed.clear()
        self.drivers.clear()
        self.cur_session = None

    # ---------- ключ ----------

    def _enroll(self) -> None:
        self._set_state("активация…")
        key = self.backend.enroll(str(self.cfg.get("device_id") or ""))
        if not key:
            self._set_state("активация не удалась")
            return
        self.backend.key = key
        self.cfg["api_key"] = key
        self.ctx.save_config()
        self._event("установка активирована на сервере RUTP")

    def _invalidate_key(self) -> None:
        self.backend.key = ""
        if self.ctx:
            self.cfg["api_key"] = ""
            self.ctx.save_config()
        self.processed.clear()
        self._set_state("ключ устарел — активируюсь заново")

    # ---------- состояние ----------

    def _loaded(self) -> int:
        return sum(1 for r in self.drivers.values() if r["state"] == "ok")

    def _set_state(self, text: str) -> None:
        self.state = text

    def _event(self, text: str) -> None:
        with self._lock:
            self.events.insert(0, {"at": time.time(), "text": text})
            del self.events[40:]
        if self.ctx:
            self.ctx.log.info("RUTP: %s", text)
            try:
                self.ctx.storage.log_event("rutp", {"text": text})
            except Exception:                            # noqa: BLE001
                pass

    def status(self) -> dict:
        with self._lock:
            events = list(self.events[:20])
        try:                                    # таблицу правит рабочий поток
            rows = [dict(r) for r in list(self.drivers.values())]
        except RuntimeError:
            rows = []
        return {
            "state": self.state,
            "in_session": self.cur_session is not None,
            "liveries": self._loaded(),
            "drivers": sorted(rows, key=lambda r: (len(r["number"]), r["number"])),
            "events": events,
            "paint_folder": str(self.cfg.get("paint_folder") or ""),
            "paint_suggest": "" if self.cfg.get("paint_folder") else suggest_paint_folder(),
            "activated": bool(self.backend.key),
            "backend": BACKEND_URL,
            "legacy_running": bool(self.legacy["pids"]),
            "legacy_autostart": bool(self.legacy["autostart"]),
        }

    # ---------- действия ----------

    def set_folder(self, path: str) -> dict:
        raw = str(path or "").strip().strip(chr(34)).strip()
        p = Path(raw)
        # пустая строка — это Path("."), текущая папка: такую не принимаем
        if not raw or not p.is_absolute() or not p.is_dir():
            return {"ok": False, "error": "not_found", "message": "Такой папки нет"}
        if self.ctx:
            if self.cur_session is not None:
                self._end_session()                 # старую папку чистим, новую наполним
            self.cfg["paint_folder"] = str(p)
            self.ctx.save_config()
            self._event(f"папка paint: {p}")
        return {"ok": True, **self.status()}

    def pick_folder(self) -> dict:
        """Окно выбора папки. Tk живёт в главном потоке — ждём ответа оттуда."""
        from ...core.ui import host
        if not host.is_running():
            return {"ok": False, "error": "no_ui", "message": "Интерфейс не запущен"}
        done, res = threading.Event(), {}

        def ask():
            import tkinter as tk
            from tkinter import filedialog
            # окно выбора открывалось позади программы: его родитель — невидимое
            # окно поверх всех, тогда и диалог встаёт поверх
            top = None
            try:
                top = tk.Toplevel()
                top.overrideredirect(True)
                top.attributes("-alpha", 0.0)
                top.attributes("-topmost", True)
                top.geometry("1x1+200+200")
                top.lift()
                top.focus_force()
                res["path"] = filedialog.askdirectory(
                    parent=top, title="Папка paint iRacing",
                    initialdir=self.cfg.get("paint_folder") or suggest_paint_folder() or str(Path.home()))
            finally:
                if top is not None:
                    top.destroy()
                done.set()
        host.call(ask)
        done.wait(300)
        if not res.get("path"):
            return {"ok": False, "error": "cancelled", "message": "Папка не выбрана"}
        return self.set_folder(res["path"])

    def open_paint(self) -> dict:
        folder = str(self.cfg.get("paint_folder") or "")
        if not folder or not Path(folder).is_dir():
            return {"ok": False, "error": "no_folder", "message": "Папка paint не выбрана"}
        os.startfile(folder)                       # noqa: S606
        return {"ok": True}

    def reload(self) -> dict:
        """Убрать скачанное в этой сессии и загрузить заново."""
        self._end_session()
        self._event("перезагрузка ливрей по кнопке")
        return {"ok": True, **self.status()}

    def stop_legacy(self) -> dict:
        """Закрыть отдельного агента и убрать его из автозапуска — по кнопке.

        Агент 1.2 не умеет закрываться по просьбе, поэтому закрываем процесс,
        а чужие ливреи, которые он успел положить, убираем по его же списку."""
        for pid in _legacy_pids():
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                           timeout=10, creationflags=NO_WINDOW)
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, "RUTP")
        except OSError:
            pass
        time.sleep(0.5)
        left = Written(LEGACY_DIR / "written.json").cleanup()
        self.legacy = {"pids": _legacy_pids(), "autostart": _legacy_autostart()}
        self._legacy_ts = time.monotonic()
        if self.legacy["pids"]:
            return {"ok": False, "error": "still_running", **self.status(),
                    "message": "Отдельный RUTP не закрылся — закройте его из его трея"}
        self._event("отдельный RUTP закрыт и убран из автозапуска"
                    + (f" · убраны его ливреи: {left}" if left else ""))
        return {"ok": True, **self.status()}

    # ---------- роуты ----------

    def routes(self) -> dict:
        def body(req) -> dict:
            n = int(req.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(req.rfile.read(n).decode("utf-8"))
            except Exception:                      # noqa: BLE001
                return {}

        return {
            "GET /api/rutp/status": lambda r: self.status(),
            "POST /api/rutp/folder": lambda r: self.set_folder(str(body(r).get("path", ""))),
            "POST /api/rutp/pick": lambda r: self.pick_folder(),
            "POST /api/rutp/paint": lambda r: self.open_paint(),
            "POST /api/rutp/reload": lambda r: self.reload(),
            "POST /api/rutp/legacy": lambda r: self.stop_legacy(),
        }
