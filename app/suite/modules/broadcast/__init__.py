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
        self.pits_tracker = PitTracker()
        self._delta_prev = None
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
                "overlays": ["tower", "grid", "commentators", "delta", "pits"]}

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
            self.pits_tracker.reset()

        rows = sim.timing()
        # В повторе CarIdx* показывают просматриваемый кадр — питы по ним
        # считать нельзя, иначе прошлые заезды запишутся как новые
        if snap.mode == "LIVE":
            self.pits_tracker.update(rows, snap.session_time)
        for r in rows:
            r["pit_stops"] = self.pits_tracker.count(r["car_idx"])
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

    # ---------- остальные оверлеи ----------

    def grid(self) -> dict:
        """Стартовая решётка."""
        rows = self.ctx.sim.qualifying_grid()
        source = "qualifying"
        if not rows:
            # квалификации не было или результаты ещё не пришли — показываем
            # текущий порядок, честно помечая источник
            rows = [{"position": r["position"], "car_idx": r["car_idx"],
                     "car_number": r["car_number"], "name": r["name"],
                     "team": r["team"], "class_name": r["class_name"],
                     "time": r["best_lap"] if r["best_lap"] > 0 else 0.0}
                    for r in self._rows]
            source = "current"
        pole = rows[0]["time"] if rows and rows[0]["time"] > 0 else 0.0
        for r in rows:
            r["delta"] = round(r["time"] - pole, 3) if (r["time"] and pole) else 0.0
        c = self.ctx.config
        return {"source": source,
                "track": self.ctx.sim.snapshot().track,
                "title": c.get("grid_title", "стартовая решётка"),
                "rows": rows[:int(c.get("grid_rows", 24))]}

    def commentators(self) -> dict:
        c = self.ctx.config
        return {"visible": bool(c.get("comm_visible", False)),
                "people": c.get("commentators", [])[:2],
                "caption": c.get("comm_caption", "")}

    def set_commentators(self, data: dict) -> dict:
        c = self.ctx.config
        if "people" in data:
            people = []
            for item in list(data["people"])[:2]:   # больше двух в плашку не влезает
                people.append({"name": str(item.get("name", "")).strip(),
                               "role": str(item.get("role", "")).strip()})
            c["commentators"] = [x for x in people if x["name"]]
        for key, cfg_key in (("visible", "comm_visible"),
                             ("caption", "comm_caption")):
            if key in data:
                c[cfg_key] = data[key]
        self.ctx.save_config()
        self.ctx.publish("commentators", self.commentators())
        return {"ok": True, **self.commentators()}

    def delta(self) -> dict:
        """Отставание между выбранными пилотами.

        Считаем по F2Time — это оценка сима, на разорванном поле она грубее
        секунды. Для пары, идущей не подряд, берём разницу их значений.
        """
        pair = [str(x).lstrip("#") for x in self.ctx.config.get("delta_pair", [])][:2]
        by_num = {r["car_number"]: r for r in self._rows}
        rows = [by_num.get(n) for n in pair]
        if len(rows) < 2 or not all(rows):
            return {"ready": False, "pair": pair}
        a, b = rows
        lead, chase = (a, b) if a["position"] < b["position"] else (b, a)
        gap = round(abs(chase["gap"] - lead["gap"]), 2)

        prev = self._delta_prev
        trend = 0
        if prev is not None:
            diff = gap - prev
            trend = -1 if diff < -0.05 else (1 if diff > 0.05 else 0)
        self._delta_prev = gap
        return {"ready": True, "gap": gap, "trend": trend,
                "lead": {"car_number": lead["car_number"], "name": lead["name"],
                         "position": lead["position"]},
                "chase": {"car_number": chase["car_number"], "name": chase["name"],
                          "position": chase["position"]},
                "laps_apart": lead["laps"] - chase["laps"]}

    def pits(self) -> dict:
        """Пит-стопы по полю: сколько раз стоял и как долго."""
        c = self.ctx.config
        rows = []
        for r in sorted(self._rows, key=lambda x: x["position"]):
            stops = self.pits_tracker.stops(r["car_idx"])
            if not stops and not c.get("pits_show_all", False):
                continue
            rows.append({
                "position": r["position"],
                "car_number": r["car_number"],
                "name": r["name"],
                "count": len(stops),
                "last": stops[-1] if stops else None,
                "total": round(sum(x["duration"] for x in stops), 1),
                "in_pit": r["on_pit"] or r["in_pit_stall"],
                # None = неизвестно: компаунд не менялся, но это не доказывает,
                # что резину не меняли (см. ТЗ, B5)
                "tyre_changed": stops[-1]["tyre_changed"] if stops else None,
            })
        return {"title": c.get("pits_title", "пит-стопы"),
                "rows": rows[:int(c.get("pits_rows", 12))]}

    def settings(self) -> dict:
        c = self.ctx.config
        return {"tower_rows": c.get("tower_rows", 12),
                "gap_mode": c.get("gap_mode", "leader"),
                "show_pits": c.get("show_pits", True),
                "tower_title": c.get("tower_title", ""),
                "focus_car": c.get("focus_car", ""),
                "refresh_hz": c.get("refresh_hz", 3),
                "grid_title": c.get("grid_title", "стартовая решётка"),
                "grid_rows": c.get("grid_rows", 24),
                "delta_pair": c.get("delta_pair", []),
                "pits_title": c.get("pits_title", "пит-стопы"),
                "pits_rows": c.get("pits_rows", 12),
                "pits_show_all": c.get("pits_show_all", False),
                "comm_visible": c.get("comm_visible", False),
                "comm_caption": c.get("comm_caption", ""),
                "commentators": c.get("commentators", [])}

    def set_settings(self, data: dict) -> dict:
        c = self.ctx.config
        for key in ("tower_rows", "gap_mode", "show_pits", "tower_title",
                    "focus_car", "refresh_hz", "grid_title", "grid_rows",
                    "delta_pair", "pits_title", "pits_rows", "pits_show_all",
                    "comm_visible", "comm_caption"):
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
            "GET /ov/grid": page("ov_grid.html"),
            "GET /ov/commentators": page("ov_commentators.html"),
            "GET /ov/delta": page("ov_delta.html"),
            "GET /ov/pits": page("ov_pits.html"),
            "GET /api/broadcast/tower": lambda r: self.tower(),
            "GET /api/broadcast/grid": lambda r: self.grid(),
            "GET /api/broadcast/commentators": lambda r: self.commentators(),
            "POST /api/broadcast/commentators": lambda r: self.set_commentators(body(r)),
            "GET /api/broadcast/delta": lambda r: self.delta(),
            "GET /api/broadcast/pits": lambda r: self.pits(),
            "GET /api/broadcast/settings": lambda r: self.settings(),
            "POST /api/broadcast/settings": lambda r: self.set_settings(body(r)),
        }
