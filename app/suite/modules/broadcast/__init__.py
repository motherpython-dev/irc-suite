"""Модуль B: оверлеи трансляции.

Оверлеи отдаются как обычные страницы и подключаются в OBS через browser
source — режиссёр двигает и масштабирует их средствами OBS. Своих окон модуль
не создаёт.

Первым сделана башня позиций (B1); остальные оверлеи из ТЗ добавляются рядом
как новые страницы, данные у них общие.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from ...core.modules import Context

WEB = Path(__file__).resolve().parents[2] / "web"


class PitTracker:
    """Счёт пит-стопов по всему полю.

    Остановка засчитывается по въезду в бокс (`in_pit_stall`), а не по
    пит-лейну: проезд насквозь остановкой не является. Длительность считаем от
    въезда в бокс до выезда на трассу.

    Смену резины НЕ угадываем: `CarIdxTireCompound` меняется только при смене
    ТИПА компаунда, а в сериях с единственным компаундом остаётся прежним даже
    после смены четырёх колёс. Пока это не проверено на живой гонке, отдаём
    длительность стоянки и компаунд как есть — интерфейс покажет «резина: —».
    """

    def __init__(self):
        self._in_stall: dict[int, float] = {}
        self._stops: dict[int, list[dict]] = {}
        self._tyre: dict[int, int] = {}

    def reset(self) -> None:
        self._in_stall.clear()
        self._stops.clear()
        self._tyre.clear()

    def update(self, rows: list[dict], session_time: float) -> None:
        for r in rows:
            idx = r["car_idx"]
            if r["tyre"] >= 0:
                self._tyre.setdefault(idx, r["tyre"])
            if r["in_pit_stall"]:
                self._in_stall.setdefault(idx, session_time)
                continue
            started = self._in_stall.pop(idx, None)
            if started is None:
                continue
            duration = max(0.0, session_time - started)
            if duration < 2.0:          # заезд-выезд без обслуживания
                continue
            before = self._tyre.get(idx, -1)
            now = r["tyre"]
            self._stops.setdefault(idx, []).append({
                "lap": r["laps"],
                "duration": round(duration, 1),
                "tyre_changed": (None if now < 0 or before < 0 or now == before
                                 else True),
                "tyre": now,
            })
            self._tyre[idx] = now

    def stops(self, car_idx: int) -> list[dict]:
        return self._stops.get(car_idx, [])

    def count(self, car_idx: int) -> int:
        return len(self._stops.get(car_idx, []))


class BroadcastModule:
    id = "broadcast"
    title = "Оверлеи трансляции"

    def __init__(self):
        self.ctx: Context | None = None
        self.pits = PitTracker()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._subsession = None
        self._rows: list[dict] = []

    # ---------- жизненный цикл ----------

    def start(self, ctx: Context) -> None:
        self.ctx = ctx
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="broadcast")
        self._thread.start()
        ctx.log.info("оверлеи трансляции запущены")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    def status(self) -> dict:
        return {"cars": len(self._rows),
                "tower_url": "/ov/tower"}

    # ---------- сбор данных ----------

    def _loop(self) -> None:
        period = 1.0 / float(self.ctx.config.get("refresh_hz", 3))
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                self.ctx.log.exception("сбор данных трансляции упал")
            time.sleep(period)

    def _tick(self) -> None:
        sim = self.ctx.sim
        snap = sim.snapshot()
        if not snap.connected:
            return
        if snap.subsession_id != self._subsession:
            self._subsession = snap.subsession_id
            self.pits.reset()

        rows = sim.timing()
        # В повторе CarIdx* показывают просматриваемый кадр — питы по ним
        # считать нельзя, иначе прошлые заезды запишутся как новые
        if snap.mode == "LIVE":
            self.pits.update(rows, snap.session_time)
        for r in rows:
            r["pit_stops"] = self.pits.count(r["car_idx"])
        self._rows = rows
        self.ctx.publish("timing", {"mode": snap.mode, "rows": rows})

    # ---------- данные для оверлеев ----------

    def tower(self) -> dict:
        cfg = self.ctx.config
        snap = self.ctx.sim.snapshot()
        rows = list(self._rows)
        limit = int(cfg.get("tower_rows", 12))
        focus = cfg.get("focus_car", "")

        shown = rows[:limit]
        if focus:
            # держим выбранную машину в кадре, даже если она за пределами топа
            extra = [r for r in rows if r["car_number"] == focus
                     and r not in shown]
            shown = shown + extra
        return {
            "mode": snap.mode,
            "track": snap.track,
            "session_time": snap.session_time,
            "gap_mode": cfg.get("gap_mode", "leader"),
            "show_pits": bool(cfg.get("show_pits", True)),
            "title": cfg.get("tower_title", ""),
            "rows": shown,
            "total": len(rows),
        }

    def settings(self) -> dict:
        c = self.ctx.config
        return {"tower_rows": c.get("tower_rows", 12),
                "gap_mode": c.get("gap_mode", "leader"),
                "show_pits": c.get("show_pits", True),
                "tower_title": c.get("tower_title", ""),
                "focus_car": c.get("focus_car", ""),
                "refresh_hz": c.get("refresh_hz", 3)}

    def set_settings(self, data: dict) -> dict:
        c = self.ctx.config
        for key in ("tower_rows", "gap_mode", "show_pits", "tower_title",
                    "focus_car", "refresh_hz"):
            if key in data:
                c[key] = data[key]
        self.ctx.save_config()
        return {"ok": True, **self.settings()}

    # ---------- HTTP ----------

    def routes(self) -> dict:
        def body(req) -> dict:
            n = int(req.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(req.rfile.read(n).decode("utf-8"))
            except Exception:
                return {}

        def page(name: str):
            return lambda r: ((WEB / name).read_bytes(), "text/html; charset=utf-8")

        return {
            "GET /ov/tower": page("ov_tower.html"),
            "GET /api/broadcast/tower": lambda r: self.tower(),
            "GET /api/broadcast/settings": lambda r: self.settings(),
            "POST /api/broadcast/settings": lambda r: self.set_settings(body(r)),
        }
