"""Ядро Suite: общие ресурсы и жизненный цикл модулей.

Здесь живёт всё, что делится между модулями — клиент сима, broadcast-команды,
хранилище, HTTP-сервер, шина SSE. Логика конкретных функций (инциденты, башня
позиций, ливреи) вынесена в модули.
"""
from __future__ import annotations

import logging
import threading
import time
import webbrowser

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

    def open_panel(self, path: str = "") -> None:
        """Панель открывается окном приложения, а не вкладкой браузера."""
        from .ui.panel_window import open_panel as open_window
        url = self.server.url.rstrip("/") + (path or "/")
        try:
            open_window(url, profile_dir=config.data_dir() / "panel-profile")
        except Exception:
            # без панели программа остаётся живой в трее, но пользователь
            # видит только иконку и не понимает, почему — поэтому в лог
            log.exception("панель не открылась: %s", url)

    def start(self) -> None:
        self.server.start()
        self.sim.start()
        self.registry.start_enabled(self._make_ctx)
        threading.Thread(target=self._broadcast_loop, daemon=True, name="push").start()
        self.storage.log_event("app_start", {"version": VERSION})
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
        self.storage.log_event("app_stop", {"version": VERSION})
        self.registry.stop_all()
        self.sim.stop()
        self.server.stop()
        self.storage.close()
