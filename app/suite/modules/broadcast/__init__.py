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
from .macros import MacroRunner, normalize_macros
from .sectors import SectorTimer

WEB = Path(__file__).resolve().parents[2] / "web"


# Биты SessionFlags из irSDK. Нужны только те, что меняют цвет полосы.
FLAG_CHECKERED = 0x00000001
FLAG_WHITE = 0x00000002
FLAG_GREEN = 0x00000004
FLAG_YELLOW = 0x00000008
FLAG_RED = 0x00000010
FLAG_CAUTION = 0x00004000
FLAG_CAUTION_WAVING = 0x00008000


def _flag_name(bits: int) -> str:
    """Флаг сессии одним словом — для цвета полосы вдоль пилона."""
    bits = int(bits or 0)
    if bits & FLAG_CHECKERED:
        return "checkered"
    if bits & FLAG_RED:
        return "red"
    if bits & (FLAG_YELLOW | FLAG_CAUTION | FLAG_CAUTION_WAVING):
        return "yellow"
    if bits & FLAG_WHITE:
        return "white"
    if bits & FLAG_GREEN:
        return "green"
    return "none"


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


# порядок как в пульте
OVERLAYS = (
    ("tower", "Башня позиций"),
    ("grid", "Стартовая решётка"),
    ("commentators", "Комментаторы"),
    ("delta", "Дельта пилотов"),
    ("pits", "Пит-стопы"),
)


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
        self.deck = None
        self.windows: dict = {}
        self.sectors = SectorTimer()
        self.macro_runner = MacroRunner(self)

    # ---------- жизненный цикл ----------

    def start(self, ctx: Context) -> None:
        self.ctx = ctx
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="broadcast")
        self._thread.start()

        # Окна НЕ создаём: при старте приложения на экране должно быть только
        # главное окно. Оверлеи и пульт появляются, когда их включают.
        # эфир после запуска пуст: графику выводит режиссёр. Иначе флаги
        # «в эфире» из прошлого запуска расходились с тем, что на экране
        ctx.config["visible"] = {}
        ctx.log.info("оверлеи трансляции запущены (окна по требованию)")

    def _ensure_window(self, overlay_id: str):
        """Создать окно оверлея при первом показе.

        Tk живёт в главном потоке, а команда приходит из HTTP или трея,
        поэтому создание уходит в очередь хоста и выполняется там же.
        """
        from ...core.ui import host
        from .windows import WINDOWS
        if overlay_id in self.windows or overlay_id not in WINDOWS:
            return
        if not host.is_running():
            return

        def make():
            if self.ctx is None or overlay_id in self.windows:
                return
            try:
                w = WINDOWS[overlay_id](self, host.root(), config_key=overlay_id)
                self.windows[overlay_id] = w
                w.set_visible(self.is_visible(overlay_id))
            except Exception:
                self.ctx.log.exception("окно оверлея %s не создалось", overlay_id)

        host.call(make)

    def show_deck(self, visible: bool) -> dict:
        """Пульт эфира — родное окно поверх iRacing.

        Это та же страница, что `/deck` на телефоне: одна вёрстка на оба
        случая. Окно держит ядро отдельным процессом, как и главную панель —
        WebView2 не уживается в одном потоке с Tk.
        """
        app = self.ctx.app if self.ctx else None
        if app is None:
            return {"ok": False, "error": "no_app"}
        res = app.open_deck() if visible else app.close_deck()
        self.ctx.publish("overlays", self.visibility())
        return res

    def deck_visible(self) -> bool:
        app = self.ctx.app if self.ctx else None
        return bool(app and app.deck_open())

    def stop(self) -> None:
        self._stop.set()
        self.macro_runner.stop()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        for w in self.windows.values():
            try:
                w.stop()
            except Exception:
                pass
        self.windows.clear()
        if self.deck:
            try:
                self.deck.stop()
            except Exception:
                pass
            self.deck = None
        self.windows: dict = {}



    def build_deck(self, host_root=None):
        from .deck import BroadcastDeck
        self.deck = BroadcastDeck(self, host_root)
        return self.deck

    def build_windows(self, host_root=None) -> dict:
        """Оверлеи окнами поверх сима.

        При одном мониторе OBS захватывает экран целиком, поэтому в эфир идёт
        то же, что видит оператор. Браузерные страницы остаются для схемы с
        двумя машинами, где OBS берёт их источником.
        """
        from .windows import WINDOWS
        for oid, cls in WINDOWS.items():
            try:
                w = cls(self, host_root, config_key=oid)
                w.set_visible(self.is_visible(oid))
                self.windows[oid] = w
            except Exception:
                self.ctx.log.exception("окно оверлея %s не создалось", oid)
        return self.windows

    def status(self) -> dict:
        return {"cars": len(self._rows),
                "deck_visible": self.deck_visible(),
                "overlays": [{"id": i, "title": t, "visible": self.is_visible(i)}
                             for i, t in OVERLAYS]}

    # ---------- что сейчас в эфире ----------

    def _vis(self) -> dict:
        return self.ctx.config.setdefault("visible", {})

    def is_visible(self, overlay_id: str) -> bool:
        """По умолчанию скрыто: после запуска экран чистый, графику выводит
        режиссёр."""
        return bool(self._vis().get(overlay_id, False))

    def set_visible(self, overlay_id: str, visible: bool) -> dict:
        if overlay_id not in dict(OVERLAYS):
            return {"ok": False, "error": "unknown_overlay"}
        self._vis()[overlay_id] = bool(visible)
        # у плашки комментаторов свой флаг — держим их в согласии
        if overlay_id == "commentators":
            self.ctx.config["comm_visible"] = bool(visible)
        self.ctx.save_config()
        if visible and self.ctx.app is not None:
            # Окна оверлеев держит отдельный процесс на WebView2. Страница сама
            # увидит в данных, что её вывели в эфир, и покажет окно с анимацией.
            self.ctx.app.ensure_overlays()
        self.ctx.publish("overlays", self.visibility())
        return {"ok": True, **self.visibility()}

    def visibility(self) -> dict:
        return {"overlays": [{"id": i, "title": t, "visible": self.is_visible(i)}
                             for i, t in OVERLAYS]}

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
            self.sectors.reset()

        rows = sim.timing()
        # В повторе CarIdx* показывают просматриваемый кадр — питы по ним
        # считать нельзя, иначе прошлые заезды запишутся как новые
        if snap.mode == "LIVE":
            self.pits_tracker.update(rows, snap.session_time)
            self.sectors.update(sim.sector_starts(), rows, snap.session_time)
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
            "visible": self.is_visible("tower"),
            "mode": snap.mode,
            "track": snap.track,
            "session_time": snap.session_time,
            "gap_mode": cfg.get("gap_mode", "leader"),
            "show_pits": bool(cfg.get("show_pits", True)),
            "title": cfg.get("tower_title", ""),
            "rows": shown,
            "total": len(rows),
            "flag": _flag_name(snap.session_flags),
            "lap": max((r["lap"] for r in rows), default=0),
            "laps_total": int(cfg.get("laps_total", 0)),
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
        return {"visible": self.is_visible("grid"),
                "source": source,
                "track": self.ctx.sim.snapshot().track,
                "title": c.get("grid_title", "стартовая решётка"),
                # ограничение щедрое: решётка листается страницами по десять,
                # и обрезать состав больше незачем
                "rows": rows[:int(c.get("grid_rows", 60))]}

    def commentators(self) -> dict:
        c = self.ctx.config
        people = c.get("commentators", [])[:2]
        if not people:
            # в демо плашка пустой не нужна: её показывают ради вёрстки
            people = self.ctx.sim.demo_hints().get("commentators", [])
        return {"visible": self.is_visible("commentators"),
                "people": people,
                "caption": c.get("comm_caption", "")}

    def set_commentators(self, data: dict) -> dict:
        c = self.ctx.config
        if "visible" in data:
            self._vis()["commentators"] = bool(data["visible"])
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
        if len(pair) < 2:
            # в демо берём борьбу за лидерство, иначе оверлей нечем проверить
            pair = self.ctx.sim.demo_hints().get("delta_pair", pair)
        by_num = {r["car_number"]: r for r in self._rows}
        rows = [by_num.get(n) for n in pair]
        if len(rows) < 2 or not all(rows):
            return {"ready": False, "visible": self.is_visible("delta"), "pair": pair}
        a, b = rows
        lead, chase = (a, b) if a["position"] < b["position"] else (b, a)
        gap = round(abs(chase["gap"] - lead["gap"]), 2)

        prev = self._delta_prev
        trend = 0
        if prev is not None:
            diff = gap - prev
            trend = -1 if diff < -0.05 else (1 if diff > 0.05 else 0)
        self._delta_prev = gap
        return {"ready": True, "visible": self.is_visible("delta"),
                "gap": gap, "trend": trend,
                # класс нужен чипу номера: он красится под цвет класса
                "lead": {"car_number": lead["car_number"], "name": lead["name"],
                         "position": lead["position"],
                         "class_name": lead.get("class_name", "")},
                "chase": {"car_number": chase["car_number"], "name": chase["name"],
                          "position": chase["position"],
                          "class_name": chase.get("class_name", "")},
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
                "class_name": r.get("class_name", ""),
                "count": len(stops),
                "last": stops[-1] if stops else None,
                "total": round(sum(x["duration"] for x in stops), 1),
                "in_pit": r["on_pit"] or r["in_pit_stall"],
                # None = неизвестно: компаунд не менялся, но это не доказывает,
                # что резину не меняли (см. ТЗ, B5)
                "tyre_changed": stops[-1]["tyre_changed"] if stops else None,
            })
        return {"visible": self.is_visible("pits"),
                "title": c.get("pits_title", "пит-стопы"),
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
                "grid_rows": c.get("grid_rows", 60),
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

    # ---------- пульт: карточки, камеры, макросы ----------

    def cards(self) -> dict:
        """Карточки пилотов для пульта.

        Разрыв считаем двумя способами сразу: до лидера и до идущего впереди.
        Сектора — текущего круга, прошлого и лучшие, плюс лучшие по полю,
        чтобы пульт подсветил фиолетовым, как в таймингах.
        """
        sim = self.ctx.sim
        snap = sim.snapshot()
        starts = sim.sector_starts()
        n = len(starts)
        inc = sim.driver_incidents()
        cam = sim.camera_state()
        rows = sorted(self._rows, key=lambda r: r["position"])
        out, prev = [], None
        for r in rows:
            out.append({
                "car_idx": r["car_idx"],
                "position": r["position"],
                "class_position": r.get("class_position", 0),
                "car_number": r["car_number"],
                "name": r["name"],
                "team": r.get("team", ""),
                "class_name": r.get("class_name", ""),
                "gap": r["gap"],
                "interval": (None if prev is None
                             else round(max(0.0, r["gap"] - prev["gap"]), 3)),
                "lap": r.get("lap", 0),
                "last_lap": r.get("last_lap", 0.0),
                "best_lap": r.get("best_lap", 0.0),
                "in_pit": bool(r.get("on_pit") or r.get("in_pit_stall")),
                "pit_stops": r.get("pit_stops", 0),
                "incidents": inc.get(r["car_idx"]),
                "sectors": self.sectors.get(r["car_idx"], n),
            })
            prev = r
        return {"mode": snap.mode, "live": snap.mode == "LIVE",
                "demo": bool(getattr(sim, "demo", False)),
                "sectors_n": n, "best": self.sectors.best_overall(n),
                "focus_car_idx": cam.get("car_idx", -1), "rows": out}

    def _camera_now(self) -> dict:
        sim = self.ctx.sim
        cur = sim.camera_state()
        names = {v: k for k, v in sim.camera_groups().items()}
        row = next((r for r in self._rows if r["car_idx"] == cur.get("car_idx")), None)
        return {**cur, "group_name": names.get(cur.get("group"), ""),
                "car_number": row["car_number"] if row else ""}

    def cameras(self) -> dict:
        groups = sorted(({"name": k, "num": v}
                         for k, v in self.ctx.sim.camera_groups().items()),
                        key=lambda g: g["num"])
        return {"groups": groups, "current": self._camera_now()}

    def camera(self, data: dict) -> dict:
        """Камера на машину и/или группу.

        car: номер, «leader» или пусто/«focus» — машина, что уже в кадре.
        group: номер группы или её имя из CameraInfo; пусто — текущая.
        """
        sim = self.ctx.sim
        car = str(data.get("car") or "").strip().lstrip("#")
        if car in ("", "focus"):
            car = None
        elif car == "leader":
            lead = next((r for r in self._rows if r["position"] == 1), None)
            if not lead:
                return {"ok": False, "error": "no_leader"}
            car = lead["car_number"]

        group = data.get("group")
        gnum = None
        if group not in (None, ""):
            if isinstance(group, int) or str(group).isdigit():
                gnum = int(group)
            else:
                gmap = {k.lower(): v for k, v in sim.camera_groups().items()}
                gnum = gmap.get(str(group).strip().lower())
                if gnum is None:
                    return {"ok": False, "error": "unknown_camera_group",
                            "group": group}
        ok = sim.switch_camera(car, gnum)
        return {"ok": bool(ok), "current": self._camera_now()}

    def apply_overlay(self, overlay_id: str, action: str) -> dict:
        """Шаг макроса: показать, убрать, переключить, оставить только этот."""
        ids = [i for i, _ in OVERLAYS]
        if overlay_id == "*":
            for i in ids:
                if self.is_visible(i) != (action == "show"):
                    self.set_visible(i, action == "show")
            return {"ok": True, **self.visibility()}
        if overlay_id not in ids:
            return {"ok": False, "error": "unknown_overlay"}
        if action == "only":
            for i in ids:
                if i != overlay_id and self.is_visible(i):
                    self.set_visible(i, False)
            return self.set_visible(overlay_id, True)
        if action == "toggle":
            return self.set_visible(overlay_id, not self.is_visible(overlay_id))
        return self.set_visible(overlay_id, action != "hide")

    def macros_get(self) -> dict:
        return {"macros": self.ctx.config.get("macros") or [],
                "running": self.macro_runner.running}

    def macros_save(self, data: dict) -> dict:
        clean = normalize_macros(data.get("macros"), [i for i, _ in OVERLAYS])
        self.ctx.config["macros"] = clean
        self.ctx.save_config()
        return {"ok": True, "macros": clean}

    # ---------- положение окон оверлеев ----------

    # Куда ставить окно, пока его ни разу не двигали (экран 1920x1080).
    DEFAULT_POS = {
        "tower": {"x": 16, "y": 110}, "grid": {"x": 506, "y": 690},
        "commentators": {"x": 40, "y": 930}, "delta": {"x": 1580, "y": 110},
        "pits": {"x": 1540, "y": 420},
    }

    def positions(self) -> dict:
        """Хосту оверлеев: какие окна поднимать и где они стояли."""
        pos = self.ctx.config.get("positions") or {}
        return {"overlays": [i for i, _ in OVERLAYS],
                "positions": {i: pos.get(i) or self.DEFAULT_POS.get(i, {"x": 40, "y": 120})
                              for i, _ in OVERLAYS}}

    def set_position(self, data: dict) -> dict:
        oid = str(data.get("id", ""))
        if oid not in dict(OVERLAYS):
            return {"ok": False, "error": "unknown_overlay"}
        self.ctx.config.setdefault("positions", {})[oid] = {
            "x": int(data.get("x", 0)), "y": int(data.get("y", 0))}
        self.ctx.save_config()
        return {"ok": True}

    def routes(self) -> dict:
        def body(req) -> dict:
            n = int(req.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(req.rfile.read(n).decode("utf-8"))
            except Exception:
                return {}

        def overlay(oid: str):
            # один отрисовщик на все оверлеи: страница общая, меняется только id
            return lambda r: ((WEB / "ov" / "overlay.html").read_bytes()
                              .replace(b"__OV__", oid.encode()), "text/html; charset=utf-8")

        def spec_get(req):
            from urllib.parse import parse_qs, urlparse
            from . import specs
            oid = parse_qs(urlparse(req.path).query).get("id", [""])[0]
            return specs.load(oid) or {"ok": False, "error": "unknown_overlay"}

        def spec_save(req):
            from . import specs
            b = body(req)
            return specs.save(str(b.get("id", "")), b.get("spec") or {})

        def query(req, key):
            from urllib.parse import parse_qs, urlparse
            return parse_qs(urlparse(req.path).query).get(key, [""])[0]

        def asset_get(req):
            from . import specs
            got = specs.read_asset(query(req, "name"))
            return got if got else ({"ok": False, "error": "not_found"})

        def asset_save(req):
            from . import specs
            b = body(req)
            return specs.save_asset(str(b.get("name", "")), str(b.get("data", "")))

        def spec_reset(req):
            from . import specs
            return specs.reset(str(body(req).get("id", "")))

        def assets_list(req):
            from . import specs
            return {"assets": specs.list_assets()}

        return {
            **{f"GET /ov/{oid}": overlay(oid) for oid, _ in OVERLAYS},
            "GET /ov/render.js": lambda r: ((WEB / "ov" / "render.js").read_bytes(),
                                            "application/javascript; charset=utf-8"),
            "GET /api/broadcast/spec": spec_get,
            "POST /api/broadcast/spec": spec_save,
            "POST /api/broadcast/spec/reset": spec_reset,
            "GET /editor": lambda r: ((WEB / "editor.html").read_bytes(),
                                      "text/html; charset=utf-8"),
            "GET /ov/asset": asset_get,
            "GET /api/broadcast/assets": assets_list,
            "POST /api/broadcast/asset": asset_save,
            "GET /api/broadcast/positions": lambda r: self.positions(),
            "POST /api/broadcast/position": lambda r: self.set_position(body(r)),
            "GET /api/broadcast/tower": lambda r: self.tower(),
            "GET /api/broadcast/grid": lambda r: self.grid(),
            "GET /api/broadcast/commentators": lambda r: self.commentators(),
            "POST /api/broadcast/commentators": lambda r: self.set_commentators(body(r)),
            "GET /api/broadcast/delta": lambda r: self.delta(),
            "GET /api/broadcast/pits": lambda r: self.pits(),
            "GET /api/broadcast/visibility": lambda r: self.visibility(),
            "POST /api/broadcast/deck":
                lambda r: self.show_deck(bool(body(r).get("visible"))),
            "POST /api/broadcast/visibility":
                lambda r: (lambda b: self.set_visible(str(b.get("id", "")),
                                                      bool(b.get("visible"))))(body(r)),
            "GET /api/broadcast/settings": lambda r: self.settings(),
            "POST /api/broadcast/settings": lambda r: self.set_settings(body(r)),
            "GET /api/broadcast/cards": lambda r: self.cards(),
            "GET /api/broadcast/cameras": lambda r: self.cameras(),
            "POST /api/broadcast/camera": lambda r: self.camera(body(r)),
            "GET /api/broadcast/macros": lambda r: self.macros_get(),
            "POST /api/broadcast/macros": lambda r: self.macros_save(body(r)),
            "POST /api/broadcast/macro/run":
                lambda r: self.macro_runner.run(str(body(r).get("id", ""))),
            "POST /api/broadcast/macro/stop":
                lambda r: (self.macro_runner.stop(), {"ok": True})[1],
        }
