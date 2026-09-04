"""Ядро Suite: общие ресурсы и жизненный цикл модулей.

Здесь живёт всё, что делится между модулями — клиент сима, broadcast-команды,
хранилище, HTTP-сервер, шина SSE. Логика конкретных функций (инциденты, башня
позиций, ливреи) вынесена в модули.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from . import config
from .commands import RaceControl
from .modules import Context, Registry
from .server import Hub, PanelServer
from .simclient import MODE_LIVE, MODE_REPLAY, SimClient
from .storage import SQLiteStorage
from .version import VERSION

log = logging.getLogger("irc")


class Application:
    def __init__(self):
        self.cfg = config.load()
        self.storage = SQLiteStorage(str(config.db_path()))
        self.storage.open()

        self.sim = SimClient(poll_hz=float(self.cfg.get("poll_hz", 5.0)))
        self.rc = RaceControl(self.sim.ir, self.storage)

        self.hub = Hub()
        self.registry = Registry(log)
        self.server = PanelServer(self, self.hub, int(self.cfg.get("http_port", 8777)),
                                  dynamic=self.registry.routes,
                                  bind_all=bool(self.cfg.get("allow_lan", False)))

        self._stop = threading.Event()
        self._panel = None               # процесс окна панели, см. open_panel
        self.on_module_change = None     # трей перерисовывает меню
        self.on_mode_change = None       # трей меняет цвет значка

        self.sim.on_mode_change = self._mode_changed

    # ---------- модули ----------

    def register(self, module) -> None:
        mid = module.id
        enabled = bool(config.module_config(self.cfg, mid).get("enabled", False))
        self.registry.add(module, enabled=enabled)

    def _make_ctx(self, module_id: str) -> Context:
        return Context(
            sim=self.sim,
            rc=self.rc,
            storage=self.storage,
            publish=self.hub.publish,
            config=config.module_config(self.cfg, module_id),
            save_config=lambda: config.save(self.cfg),
            log=logging.getLogger(f"module.{module_id}"),
            app=self,
        )

    def set_lan(self, allow: bool) -> dict:
        """Доступ с телефона. Требует перезапуска: порт занимается при старте."""
        self.cfg["allow_lan"] = bool(allow)
        config.save(self.cfg)
        return {"ok": True, "allow_lan": bool(allow), "restart_required": True}

    def set_module(self, module_id: str, enabled: bool) -> dict:
        """Включить или выключить модуль на лету."""
        res = (self.registry.start(module_id, self._make_ctx) if enabled
               else self.registry.stop(module_id))
        if res.get("ok"):
            config.module_config(self.cfg, module_id)["enabled"] = enabled
            config.save(self.cfg)
            self.storage.log_event("module_toggled",
                                   {"module": module_id, "enabled": enabled})
            if self.on_module_change:
                try:
                    self.on_module_change()
                except Exception:
                    pass
            self._push_state()
        return res

    def module(self, module_id: str):
        return self.registry.get(module_id)

    # ---------- API ядра ----------

    def state(self) -> dict:
        s = self.sim.snapshot()
        return {
            "version": VERSION,
            "mode": s.mode,
            "connected": s.connected,
            "session_num": s.session_num,
            "session_time": s.session_time,
            "session_time_estimated": s.session_time_estimated,
            "replay_frames_to_end": s.replay_frames_to_end,
            "session_tick": s.session_tick,
            "player_car_idx": s.player_car_idx,
            "player_incidents": s.player_incidents,
            "results_available": s.results_available,
            "spectator": s.spectator,
            "session_flags": s.session_flags,
            "session_state": s.session_state,
            "track": s.track,
            "subsession_id": s.subsession_id,
            "drivers": len(s.drivers),
            "modules": self.registry.statuses(),
            "lan_url": self.server.lan_url,
            "allow_lan": bool(self.cfg.get("allow_lan", False)),
            "demo": bool(self.sim.demo),
        }

    def drivers(self) -> list[dict]:
        s = self.sim.snapshot()
        return [{"car_idx": d.car_idx, "car_number": d.car_number,
                 "name": d.name, "team": d.team}
                for d in sorted(s.drivers, key=lambda d: d.name.lower())]

    def find_drivers(self, query: str, limit: int = 8) -> list[dict]:
        """Поиск по номеру и по имени/фамилии.

        Номер ищем с начала строки (ввод «2» должен давать #2 раньше #23),
        имя — по любому слову, чтобы фамилию можно было набирать без имени.
        """
        q = query.strip().lstrip("#").lower()
        if not q:
            return self.drivers()[:limit]
        out = []
        for d in self.drivers():
            num, name = d["car_number"].lower(), d["name"].lower()
            if num == q:
                rank = 0
            elif num.startswith(q):
                rank = 1
            elif any(w.startswith(q) for w in name.split()):
                rank = 2
            elif q in name:
                rank = 3
            else:
                continue
            out.append((rank, d))
        out.sort(key=lambda r: (r[0], r[1]["name"]))
        return [d for _, d in out[:limit]]

    def sim_ready(self) -> dict | None:
        """broadcast через HWND_BROADCAST успешен всегда, даже когда сима нет.
        Поэтому связь проверяем явно, иначе панель врёт оператору."""
        if not self.sim.snapshot().connected:
            return {"ok": False, "error": "sim_offline",
                    "message": "iRacing не запущен или сессия не активна"}
        return None

    # ---------- события ----------

    def _mode_changed(self, old: str, new: str) -> None:
        self.storage.log_event("mode_change", {"from": old, "to": new})
        if self.on_mode_change:
            try:
                self.on_mode_change(old, new)
            except Exception:
                pass
        rc = self.registry.get("racecontrol")
        if rc and hasattr(rc, "on_mode_change"):
            try:
                rc.on_mode_change(old, new)
            except Exception:
                log.exception("racecontrol не обработал смену режима")
        self._push_state()

    def _push_state(self) -> None:
        self.hub.publish("state", self.state())

    # ---------- жизненный цикл ----------

    def set_demo(self, on: bool) -> dict:
        """Включить синтетическую гонку для проверки оверлеев.

        В конфиг НЕ сохраняем: демо, забытое включённым, во время реальной
        гонки увело бы рейс-контрол на выдуманные данные. После перезапуска
        программа всегда читает сим.
        """
        self.sim.set_demo(bool(on))
        self.storage.log_event("demo", {"on": bool(on)})
        self._push_state()
        return {"ok": True, "demo": bool(self.sim.demo)}

    def open_panel(self, path: str = "") -> None:
        """Показать панель родным окном на WebView2.

        Окно поднимает отдельный процесс (`--panel`): главный поток занят
        циклом Tk, на котором держатся оверлеи. Если панель уже открыта —
        не плодим вторую, а поднимаем существующую.
        """
        url = self.server.url.rstrip("/") + (path or "/")
        from .ui import panel_native

        if self._panel and self._panel.poll() is None:
            if panel_native.focus(self._panel.pid):
                return
            # окно закрыли, а процесс ещё жив — не ждём, откроем заново

        try:
            self._panel = self._spawn_panel(url)
            code = self._panel.poll()
            if code is None:
                time.sleep(2.0)          # успел бы упасть — упал бы сразу
                code = self._panel.poll()
            if code is None:
                return
            log.error("процесс окна панели умер сразу, код %s — открываю браузером",
                      code)
            self._trace(f"панель умерла, код {code}")
        except Exception:
            log.exception("родное окно панели не запустилось — открываю браузером")
            self._trace("панель не запустилась: исключение")

        from .ui.panel_window import open_panel as open_window
        try:
            open_window(url, profile_dir=config.data_dir() / "panel-profile")
        except Exception:
            # без панели программа остаётся живой в трее, но пользователь
            # видит только иконку и не понимает, почему — поэтому в лог
            log.exception("панель не открылась: %s", url)

    @staticmethod
    def _trace(event: str) -> None:
        """След в startup.log — тот же файл, что ведёт точка входа."""
        try:
            import os
            with open(config.data_dir() / "startup.log", "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {os.getpid()} {event}\n")
        except Exception:
            pass

    @staticmethod
    def _close_panel_tree(pid: int) -> None:
        """Закрыть окно панели вместе с процессами движка.

        WebView2 поднимает свои `msedgewebview2.exe`, и просто убить окно мало:
        они остаются сиротами, держат папку данных движка, и следующий запуск
        показывает пустоту вместо панели. Поэтому гасим дерево целиком.
        """
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def _spawn_panel(self, url: str) -> subprocess.Popen:
        """Процесс окна панели: сам exe с флагом, а из исходников — run.py."""
        if getattr(sys, "frozen", False):
            args = [sys.executable, "--panel", url]
        else:
            # из исходников точка входа — тот же run.py, которым запустили
            # приложение; вычислять её от __file__ нельзя: модуль лежит в
            # распакованном бандле, а не в репозитории
            args = [sys.executable, str(Path(sys.argv[0]).resolve()), "--panel", url]
        log.info("окно панели: %s", " ".join(args[-2:]))
        # Вывод процесса уводим в файл: у отсоединённого окна нет консоли, и
        # без этого падение движка не оставляет никаких следов.
        out = open(config.data_dir() / "panel-window.log", "a", encoding="utf-8")
        proc = subprocess.Popen(args, close_fds=True, stdout=out, stderr=out,
                                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        # Разрешаем окну выйти вперёд: без этого Windows не даёт фоновому
        # процессу передний план, и окно поднимается за чужими окнами.
        try:
            import ctypes
            ctypes.windll.user32.AllowSetForegroundWindow(proc.pid)
        except Exception:
            pass
        return proc

    def start(self) -> None:
        self.server.start()
        self.sim.start()
        self.registry.start_enabled(self._make_ctx)
        threading.Thread(target=self._broadcast_loop, daemon=True, name="push").start()
        self.storage.log_event("app_start", {"version": VERSION})
        self._trace(f"старт ядра, окно панели при запуске: "
                    f"{bool(self.cfg.get('open_panel_on_start'))}")
        if self.cfg.get("open_panel_on_start"):
            self.open_panel()

    def _broadcast_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._push_state()
            except Exception:
                pass
            time.sleep(1.0)

    def stop(self) -> None:
        self._stop.set()
        # Окно панели — отдельный процесс, и само оно о выходе не узнает:
        # закрываем вместе с приложением, иначе останется висеть без сервера.
        if self._panel and self._panel.poll() is None:
            try:
                self._close_panel_tree(self._panel.pid)
            except Exception:
                log.debug("окно панели не закрылось", exc_info=True)
        self.storage.log_event("app_stop", {"version": VERSION})
        self.registry.stop_all()
        self.sim.stop()
        self.server.stop()
        self.storage.close()
