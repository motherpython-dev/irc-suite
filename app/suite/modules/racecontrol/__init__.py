"""Модуль A: рейс-контрол.

Оборачивает уже работавшую логику (детект инцидентов, флаги, оверлей) в
контракт модуля Suite. Сама логика не переписана — изменился только способ
получения ресурсов: клиент сима, хранилище и сервер теперь общие.
"""
from __future__ import annotations

from ...core.modules import Context
from .detector import IncidentDetector
from .flags import FlagControl


class RaceControlModule:
    id = "racecontrol"
    title = "Рейс-контрол"

    def __init__(self):
        self.ctx: Context | None = None
        self.detector = None
        self.flags = None
        self.overlay = None
        self.pace_laps = 2
        self._overlay_thread = None

    # ---------- жизненный цикл ----------

    def start(self, ctx: Context) -> None:
        self.ctx = ctx
        cfg = ctx.config
        self.pace_laps = int(cfg.get("pace_laps_default", 2))

        self.flags = FlagControl(ctx.sim.ir, ctx.storage, cfg.get("chat_commands"))
        self.detector = IncidentDetector(
            ctx.sim, ctx.storage,
            poll_period=float(cfg.get("detector_period", 1.0)),
            offtrack_enabled=bool(cfg.get("offtrack_enabled", True)),
            min_offtrack_seconds=float(cfg.get("min_offtrack_seconds", 1.0)),
            offtrack_cooldown=float(cfg.get("offtrack_cooldown", 8.0)),
            merge_window=float(cfg.get("merge_window", 12.0)))
        self.detector.on_incident = self._incident_pushed
        self.detector.start()

        # Оверлей живёт в главном потоке: Tk из фонового потока работать
        # не обязан. Ядро отдаёт ему главный поток после старта модулей.
        ctx.log.info("рейс-контрол запущен")

    def stop(self) -> None:
        if self.detector:
            self.detector.stop()
            self.detector = None
        if self.overlay:
            try:
                self.overlay.stop()
            except Exception:
                pass
            self.overlay = None

    def status(self) -> dict:
        return {
            "overlay_visible": self.overlay_visible(),
            "overlay_available": self.overlay is not None,
            "incidents": len(self.incidents()) if self.ctx else 0,
            "pace_laps": self.pace_laps,
            "missed_in_replay": self.detector.missed_in_replay if self.detector else 0,
        }

    def wants_main_thread(self) -> bool:
        """Оверлею нужен главный поток (Tk). Ядро спрашивает об этом при старте."""
        return bool(self.ctx and self.ctx.config.get("overlay_enabled", True))

    def build_overlay(self, host_root=None):
        from .overlay import Overlay
        self.overlay = Overlay(self, host_root)
        return self.overlay

    # ---------- окно оверлея ----------

    def overlay_visible(self) -> bool:
        return bool(self.overlay and self.overlay.visible)

    def set_overlay(self, visible: bool) -> dict:
        """Показать или убрать окно поверх сима.

        Tk не потокобезопасен, а команда приходит из HTTP-потока или из трея,
        поэтому переключение уходит в очередь главного потока.
        """
        if not self.overlay:
            return {"ok": False, "error": "no_overlay",
                    "message": "Оверлей выключен в настройках модуля"}
        self.overlay.request_visible(bool(visible))
        return {"ok": True, "visible": bool(visible)}

    # ---------- данные ----------

    @property
    def cfg(self) -> dict:
        return self.ctx.config if self.ctx else {}

    def state(self) -> dict:
        """Состояние ядра плюс поля рейс-контрола — оверлей читает отсюда."""
        st = self.ctx.app.state()
        st["pace_laps"] = self.pace_laps
        st["missed_in_replay"] = self.detector.missed_in_replay if self.detector else 0
        return st

    def incidents(self) -> list[dict]:
        """Лента ТОЛЬКО текущей сессии.

        Без фильтра при заходе в новую сессию оставались карточки прошлой, а
        клик по такой карточке мотал бы повтор по чужому session_time — то есть
        в никуда. Когда сим не запущен, показываем последнюю сессию из базы,
        чтобы можно было доразобрать эпизоды после гонки.
        """
        st = self.ctx.storage
        s = self.ctx.sim.snapshot()
        if s.connected:
            return [i.to_dict() for i in st.list_incidents(
                limit=200, subsession_id=s.subsession_id, track=s.track,
                session_num=s.session_num)]
        key = st.last_session_key() or (None, None)
        return [i.to_dict() for i in st.list_incidents(
            limit=200, subsession_id=key[0], track=key[1])]

    def find_drivers(self, query: str, limit: int = 8) -> list[dict]:
        return self.ctx.app.find_drivers(query, limit)

    # ---------- действия ----------

    def show_incident(self, incident_id: int) -> dict:
        inc = self.ctx.storage.get_incident(incident_id)
        if not inc:
            return {"ok": False, "error": "not_found"}
        if (err := self.ctx.app.sim_ready()):
            return err
        pre = (self.cfg.get("pre_roll_estimated", 20.0) if inc.estimated
               else self.cfg.get("pre_roll_live", 10.0))
        group = self.ctx.sim.resolve_camera_group(self.cfg.get("incident_camera", ""))
        return {"ok": self.ctx.rc.show_incident(inc, pre_roll=float(pre),
                                                camera_group=group)}

    def resume_live(self) -> dict:
        if (err := self.ctx.app.sim_ready()):
            return err
        ok = self.ctx.rc.resume_live()
        if self.detector:
            self.detector.reset_missed()
        return {"ok": ok}

    def command(self, name: str, params: dict) -> dict:
        if (err := self.ctx.app.sim_ready()):
            return err
        rc = self.ctx.rc
        fn = {
            "play": lambda: rc.play(int(params.get("speed", 1))),
            "pause": rc.pause,
            "step": lambda: rc.step(bool(params.get("forward", True))),
            "jump_sim_incident": lambda: rc.jump_sim_incident(
                bool(params.get("forward", True))),
            "cam_at_incident": rc.cam_at_incident,
            "cam_car": lambda: rc.cam_car(str(params.get("car", ""))),
        }.get(name)
        if not fn:
            return {"ok": False, "error": f"unknown command {name}"}
        return {"ok": bool(fn())}

    def cameras(self) -> dict:
        groups = self.ctx.sim.camera_groups()
        return {"current": self.cfg.get("incident_camera", ""),
                "groups": [{"name": n, "num": v}
                           for n, v in sorted(groups.items(), key=lambda kv: kv[1])]}

    def set_camera(self, name: str) -> dict:
        groups = self.ctx.sim.camera_groups()
        if name and name not in groups:
            return {"ok": False, "error": "unknown_camera",
                    "message": f"Камера «{name}» не найдена в этой сессии"}
        self.cfg["incident_camera"] = name
        self.ctx.save_config()
        self.ctx.storage.log_event("camera_changed", {"camera": name or "(текущая)"})
        return {"ok": True, "current": name}

    def set_reviewed(self, incident_id: int, reviewed: bool, note: str = "") -> dict:
        self.ctx.storage.set_reviewed(incident_id, reviewed, note)
        self._push_incidents()
        return {"ok": True}

    def flag_action(self, action: str, params: dict) -> dict:
        """Админ-команды в чат сима. Требуют прав админа сессии."""
        if (err := self.ctx.app.sim_ready()):
            return err
        car = str(params.get("car", "")).strip().lstrip("#")
        if action in ("drive_through", "time_penalty", "clear", "dq", "wave_by") \
                and not car:
            return {"ok": False, "error": "no_car", "message": "Укажите номер машины"}

        f = self.flags
        if action == "yellow":
            ok = f.yellow()
        elif action == "pace_laps":
            # дельта, а не абсолютное значение: сим текущее число не отдаёт
            self.pace_laps = max(0, self.pace_laps + int(params.get("delta", 0)))
            ok = f.pace_laps(self.pace_laps)
        elif action == "clear":
            ok = f.clear(car)
        elif action == "drive_through":
            ok = f.drive_through(car)
        elif action == "time_penalty":
            ok = f.time_penalty(car, int(params.get("seconds", 5)))
        elif action == "dq":
            ok = f.disqualify(car)
        elif action == "wave_by":
            ok = f.wave_by(car)
        else:
            return {"ok": False, "error": f"unknown action {action}"}

        if not ok:
            return {"ok": False, "error": "chat_failed",
                    "message": f.last_error or "Команда не отправлена"}
        return {"ok": True, "pace_laps": self.pace_laps}

    # ---------- события ----------

    def _incident_pushed(self, inc) -> None:
        self._push_incidents()
        app = self.ctx.app
        if getattr(app, "on_incident_notify", None):
            try:
                app.on_incident_notify(inc)
            except Exception:
                pass

    def _push_incidents(self) -> None:
        self.ctx.publish("incidents", self.incidents())

    def on_mode_change(self, old: str, new: str) -> None:
        if self.detector and new == "LIVE" and old == "REPLAY":
            self.detector.reset_missed()

    # ---------- HTTP ----------

    def routes(self) -> dict:
        import json as _json

        def body(req) -> dict:
            n = int(req.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return _json.loads(req.rfile.read(n).decode("utf-8"))
            except Exception:
                return {}

        return {
            "GET /api/racecontrol/incidents": lambda r: self.incidents(),
            "GET /api/racecontrol/cameras": lambda r: self.cameras(),
            "GET /api/racecontrol/overlay":
                lambda r: {"visible": self.overlay_visible(),
                           "available": self.overlay is not None},
            "POST /api/racecontrol/overlay":
                lambda r: self.set_overlay(bool(body(r).get("visible"))),
            "POST /api/racecontrol/show":
                lambda r: self.show_incident(int(body(r).get("id", 0))),
            "POST /api/racecontrol/live": lambda r: self.resume_live(),
            "POST /api/racecontrol/camera":
                lambda r: self.set_camera(str(body(r).get("name", ""))),
            "POST /api/racecontrol/command":
                lambda r: (lambda b: self.command(str(b.get("name", "")), b))(body(r)),
            "POST /api/racecontrol/flag":
                lambda r: (lambda b: self.flag_action(str(b.get("action", "")), b))(body(r)),
            "POST /api/racecontrol/reviewed":
                lambda r: (lambda b: self.set_reviewed(
                    int(b.get("id", 0)), bool(b.get("reviewed")),
                    str(b.get("note", ""))))(body(r)),
        }
