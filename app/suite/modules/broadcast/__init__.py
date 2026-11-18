"""Модуль B: оверлеи трансляции.

Оверлеи отдаются как обычные страницы и подключаются в OBS через browser
source — режиссёр двигает и масштабирует их средствами OBS. Своих окон модуль
не создаёт.

Первым сделана башня позиций (B1); остальные оверлеи из ТЗ добавляются рядом
как новые страницы, данные у них общие.
"""
from __future__ import annotations

import json
import math
import threading
import time
from collections import deque
from pathlib import Path

from ...core.modules import Context
from ...core.replay_demo import ModuleDemo
from .director import Director
from .macros import MacroRunner, normalize_macros
from .sectors import SectorTimer

WEB = Path(__file__).resolve().parents[2] / "web"


# Цвет черты у строки, если у машины нет цвета ливреи (демо, старые сессии)
CLASS_COLOR = {"GT3": "#FF5A1F", "PRO": "#FF5A1F", "GT4": "#3B82F6", "AM": "#3B82F6",
               "SILVER": "#94A3B8", "GOLD": "#E3B341"}

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


def course_flag(bits: int) -> str:
    """Флаг всей гонки — для волны и плашки «пейс-кар на трассе».

    Не _flag_name: в нём жёлтый — и местный жёлтый участка (FLAG_YELLOW), а в
    гонке сим отдаёт флаги со стороны своей машины. Подъехал к месту аварии —
    загорелся местный жёлтый, проехал — снова зелёный: выходили выброс жёлтого
    и «рестарт», хотя пейс-кар не выезжал. Отставшему на круг и далёкому от
    пелотона это попадалось постоянно. Полный жёлтый — только FLAG_CAUTION и
    FLAG_CAUTION_WAVING.
    """
    bits = int(bits or 0)
    if bits & (FLAG_CAUTION | FLAG_CAUTION_WAVING):
        return "yellow"
    if bits & FLAG_CHECKERED:
        return "checkered"
    if bits & FLAG_GREEN:
        return "green"
    return "none"


# Слова, с которых начинаются названия трасс, но которые ничего не говорят
# зрителю: «Circuit de Spa-Francorchamps» -> SPA-FRANCORCHAMPS.
_TRACK_FILLER = {"circuit", "circuito", "circuits", "autodromo", "autódromo", "autodrom",
                 "nazionale", "de", "del", "di", "da", "do", "the", "international",
                 "internacional", "motor", "raceway", "speedway", "park", "[legacy]"}


def track_short(name: str) -> str:
    """Короткое имя трассы для подписи в шапке: DAYTONA, MONZA, SPA-FRANCORCHAMPS."""
    for w in str(name or "").split():
        if w.lower().strip(",") not in _TRACK_FILLER and w != "-":
            return w.strip(",").upper()
    return str(name or "").upper()


SESSION_TITLE = {"race": "Гонка", "practice": "Практика", "open qualify": "Квалификация",
                 "lone qualify": "Квалификация", "qualify": "Квалификация",
                 "warmup": "Разминка", "offline testing": "Тест"}


class FlagWatch:
    """Смены флага сессии — для волны по башне и плашки под шапкой.

    Флаг — course_flag (полный жёлтый, без местных жёлтых участков). Смена
    засчитывается, только если новый флаг продержался STABLE_S: на рестарте
    биты сессии несколько раз подряд переключаются, и каждое мигание давало
    новую волну. Жёлтый выброшен — событие; зелёный после полного жёлтого —
    рестарт; первый зелёный сессии — старт; прочие появления зелёного (сим
    показывает его и после «нет флага») — не события. Каждое событие получает
    номер: оверлей по нему понимает, что волну ещё не показывал. Под жёлтым —
    плашка «пейс-кар на трассе», после зелёного пять секунд держится зелёная.
    """

    GREEN_HOLD = 5.0
    STABLE_S = 0.5                     # было 1 с — волна заметно запаздывала

    def __init__(self):
        self.reset()
        self.seq = 0

    def reset(self) -> None:
        self.last = None
        self.event = None
        self.green_until = 0.0
        self.green_text = ""
        self._cand, self._cand_at = None, 0.0     # новый флаг, пока не продержался STABLE_S
        self._caution = False                      # был полный жёлтый — следующий зелёный это рестарт
        self._started = False                      # старт в этой сессии уже был

    def update(self, flag: str, now: float) -> None:
        if flag != self._cand:
            self._cand, self._cand_at = flag, now
            return
        if now - self._cand_at < self.STABLE_S or flag == self.last:
            return
        prev, self.last = self.last, flag
        if prev is None:                           # первый взгляд после запуска — не событие
            self._caution = flag == "yellow"
            self._started = flag != "none"
            return
        if flag == "yellow":
            if not self._caution:
                self._caution = True
                self._fire("yellow", now)
        elif flag == "green" and (self._caution or not self._started):
            self.green_text = "Рестарт · гонка продолжается" if self._caution else "Старт · зелёный флаг"
            self._caution, self._started = False, True
            self.green_until = now + self.GREEN_HOLD
            self._fire("green", now)

    def _fire(self, tone: str, now: float) -> None:
        self.seq += 1
        self.event = {"id": self.seq, "tone": tone, "at": now}

    def state(self, now: float) -> dict:
        tone = ("yellow" if self._caution
                else "green" if now < self.green_until else "")
        bar = ({"text": "Пейс-кар на трассе", "tone": "yellow"} if tone == "yellow"
               else {"text": self.green_text, "tone": "green"} if tone == "green" else None)
        ev = None
        if self.event:
            ev = {"id": self.event["id"], "tone": self.event["tone"],
                  "age": round(now - self.event["at"], 3)}
        return {"flag_tone": tone, "flag_bar": bar, "flag_event": ev}


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
        self._in_lap: dict[int, int] = {}          # круг, на котором въехал в бокс
        self._stops: dict[int, list[dict]] = {}
        self._tyre: dict[int, int] = {}

    def reset(self) -> None:
        self._in_stall.clear()
        self._in_lap.clear()
        self._stops.clear()
        self._tyre.clear()

    def update(self, rows: list[dict], session_time: float) -> None:
        for r in rows:
            idx = r["car_idx"]
            if r["tyre"] >= 0:
                self._tyre.setdefault(idx, r["tyre"])
            if r["in_pit_stall"]:
                self._in_stall.setdefault(idx, session_time)
                self._in_lap.setdefault(idx, r["lap"])
                continue
            started = self._in_stall.pop(idx, None)
            lap_in = self._in_lap.pop(idx, r["lap"])
            if started is None:
                continue
            duration = max(0.0, session_time - started)
            if duration < 2.0:          # заезд-выезд без обслуживания
                continue
            before = self._tyre.get(idx, -1)
            now = r["tyre"]
            self._stops.setdefault(idx, []).append({
                "lap": r["laps"],
                "lap_in": lap_in,
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
    ("weather", "Трасса и погода"),
    ("events", "События гонки"),
    ("bestlap", "Лучший круг"),
)

# Шторка повтора (на весь экран) и метка «ПОВТОР» — не графика эфира с кнопкой
# «В эфир»: их показывает режиссёр (director.py), окна и страницы для OBS — свои
FX_OVERLAYS = ("replayfx", "replaybug")

# Метки ленты событий: текст, заливка, цвет надписи
EVENT_TAGS = {
    "contact": ("контакт", "#FF5A1F", "#000000"),
    "lost": ("потеря контроля", "#FF4D4D", "#FFFFFF"),
    "off": ("вне трассы", "#FFD200", "#111111"),
    "yellow": ("жёлтый флаг", "#FFD200", "#111111"),
    "green": ("зелёный флаг", "#2FD16A", "#111111"),
}
SKIES = ["Ясно", "Переменно", "Облачно", "Пасмурно"]
COMPASS = ["С", "СВ", "В", "ЮВ", "Ю", "ЮЗ", "З", "СЗ"]
ARROWS = ["↑", "↗", "→", "↘", "↓", "↙", "←", "↖"]
INC_FLASH_S = 15.0                     # сколько строка на пульте подсвечена после инков
EVENT_KEEP_S = 40.0                    # сколько событие висит в ленте
EVENTS_SHOW_S = 10.0                   # лента в эфире после последнего события, как плашка лучшего круга
FLAG_KEEP_S = 120.0                    # жёлтый и рестарт — дольше
BESTLAP_SHOW_S = 8.0                   # плашка лучшего круга
STANDINGS_EVERY_S = 120.0              # как часто перечитывать таблицу с сайта


class GapClock:
    """Отрыв в реальном времени: сколько секунд назад машина впереди была в
    той точке трассы, где сейчас эта.

    CarIdxF2Time сим обновляет только на отсечках — в эфире отрывы стоят и
    прыгают. Здесь по каждой машине копится путь (круг + доля круга) по
    времени, отрыв — интерполяция по истории машины впереди.
    """
    KEEP_S = 600.0

    def __init__(self):
        self.hist: dict[int, list[tuple[float, float]]] = {}
        self.pos: dict[int, float] = {}

    def reset(self) -> None:
        self.hist.clear()
        self.pos.clear()

    def update(self, rows: list[dict], now: float) -> None:
        for r in rows:
            idx = r["car_idx"]
            if r.get("not_in_world"):
                self.hist.pop(idx, None)
                self.pos.pop(idx, None)
                continue
            d = r["lap"] + r.get("lap_dist", 0.0)
            h = self.hist.setdefault(idx, [])
            if h and now - h[-1][0] > 5.0:
                h.clear()                      # долго не смотрели — по доле круга не склеить
            if h:
                last = h[-1][1]
                # на линии доля круга и номер круга меняются не в один кадр
                while d - last > 0.5:
                    d -= 1.0
                while d - last < -0.5:
                    d += 1.0
                if d < last - 0.02:            # эвакуатор, сброс — история заново
                    h.clear()
                elif d < last:
                    d = last
            h.append((now, d))
            if now - h[0][0] > self.KEEP_S + 30:
                cut = next((i for i, x in enumerate(h) if now - x[0] <= self.KEEP_S), len(h))
                del h[:cut]
            self.pos[idx] = d

    def behind(self, ahead_idx: int, idx: int, now: float | None = None) -> float | None:
        """Секунды от машины ahead_idx до idx; None — истории не хватает.

        Считается на момент замера положения idx, а не на момент вопроса: окна
        спрашивают между замерами, и прибавка «сейчас − замер» (0…0,25 с) делала
        отрыв дрожащим на десятые. now — только запасной, если замера нет."""
        h, d = self.hist.get(ahead_idx), self.pos.get(idx)
        own = self.hist.get(idx)
        if own:
            now = own[-1][0]
        if not h or d is None or now is None or d < h[0][1]:
            return None
        if d >= h[-1][1]:
            return 0.0
        lo, hi = 0, len(h) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if h[mid][1] <= d:
                lo = mid
            else:
                hi = mid
        (t0, d0), (t1, d1) = h[lo], h[hi]
        t = t0 + (t1 - t0) * ((d - d0) / (d1 - d0) if d1 > d0 else 0.0)
        return max(0.0, now - t)


class RaceOrder:
    """Места в гонке по реальному положению на трассе.

    `CarIdxPosition` сим обновляет на линии старт-финиша: пилот обгонял на круге,
    отрывы (GapClock) это уже показывали, а место менялось только на линии — выше
    в таблице оказывался пилот с большим отставанием. Поэтому, пока идёт гонка,
    порядок — по пути (круг + доля круга); до старта и после финиша — официальный.
    В повторе своей истории нет: порядок и отрыв считаются по кадру.
    """
    EPS = 0.0005                # меньше этой доли круга — порядок не меняем, иначе
                                # машины бок о бок прыгали бы местами каждый кадр

    def __init__(self):
        self.rank: dict[int, int] = {}
        self.last: dict[int, float] = {}

    def reset(self) -> None:
        self.rank.clear()
        self.last.clear()

    def _frame_dist(self, r: dict) -> float:
        """Путь по кадру: на линии доля круга и номер круга меняются не в один кадр."""
        if r.get("lap_dist", -1) < 0:
            return -1e9
        d = r["lap"] + r["lap_dist"]
        prev = self.last.get(r["car_idx"])
        near_line = r["lap_dist"] < 0.02 or r["lap_dist"] > 0.98    # иначе это перемотка ровно на круг
        if prev is not None and near_line and abs(abs(d - prev) - 1) < 0.05:
            d -= round(d - prev)
        self.last[r["car_idx"]] = d
        return d

    def apply(self, rows: list[dict], live: bool, state: int, gaps: "GapClock", now: float) -> list[dict]:
        """Ставит position, class_position, dist и gap. Возвращает строки по порядку."""
        for r in rows:
            d = gaps.pos.get(r["car_idx"]) if live else self._frame_dist(r)
            r["dist"] = d if d is not None else r["lap"] + r.get("lap_dist", 0.0)
        official = any(r["position"] > 0 for r in rows)
        # 4 — гонка идёт; до неё (решётка, пейс-круг) и после финиша верен официальный порядок
        by_track = state == 4 or not official or (not live and state not in (5, 6))
        if by_track:
            rows.sort(key=lambda r: (-r["dist"], r["position"] if r["position"] > 0 else 999))
            for i in range(len(rows) - 1):
                a, b = rows[i], rows[i + 1]
                if a["dist"] - b["dist"] < self.EPS and self.rank.get(b["car_idx"], 1e9) < self.rank.get(a["car_idx"], 1e9):
                    rows[i], rows[i + 1] = b, a
        else:
            rows.sort(key=lambda r: (r["position"] <= 0, r["position"]))
        cls: dict = {}
        for k, r in enumerate(rows, start=1):
            r["position"] = k
            cls[r.get("class_name")] = cls.get(r.get("class_name"), 0) + 1
            r["class_position"] = cls[r.get("class_name")]
        self.rank = {r["car_idx"]: r["position"] for r in rows}
        if rows:
            lead = rows[0]
            lap_t = lead.get("best_lap") or lead.get("last_lap") or 90.0
            for r in rows:
                g = gaps.behind(lead["car_idx"], r["car_idx"], now) if live else None
                if g is None and not live:
                    g = max(0.0, lead["dist"] - r["dist"]) * lap_t if r["dist"] > -1e8 else 0.0
                if g is not None:
                    r["gap"] = round(g, 3)
        return rows


def _incident_kind(delta: int, kind: str = "incident") -> str | None:
    """Инцидент iRacing по прибавке очков: 1x — вне трассы, 2x — потеря
    контроля, 4x — контакт. 0x (лёгкий контакт) в эфир не выводим."""
    if kind == "offtrack":
        return "off"
    if delta >= 4:
        return "contact"
    if delta >= 2:
        return "lost"
    if delta >= 1:
        return "off"
    return None


def _sof(ratings: list[int]) -> int | None:
    """Сила поля по формуле iRacing."""
    ratings = [r for r in ratings if r and r > 0]
    if not ratings:
        return None
    s = sum(2 ** (-r / 1600) for r in ratings)
    return int(round(1600 / math.log(2) * math.log(len(ratings) / s)))


class BroadcastModule:
    id = "broadcast"
    title = "Оверлеи трансляции"

    demo = None                              # своё демо: прошлая гонка (ModuleDemo)

    @property
    def sim(self):
        """Сим, который читает модуль: демо этого модуля, пока оно включено, иначе общий."""
        d = self.demo
        return (d.sim if d is not None and d.sim is not None else None) or self.ctx.sim

    def __init__(self):
        self.ctx: Context | None = None
        self.pits_tracker = PitTracker()
        self._delta_prev = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._subsession = None
        self._rows: list[dict] = []
        self.flags = FlagWatch()
        self._flag_seen = 0                           # номер последнего флага, ушедшего в ленту
        self._events: deque = deque(maxlen=40)        # лента событий гонки
        self._rc_seen: set = set()                    # инциденты рейс-контроля, уже в ленте
        self._rc_next = 0.0
        self._inc_prev: dict[int, int] = {}           # демо: инциденты по машинам в прошлый раз
        self._lead = None                             # лидер — для счёта смен
        self._lead_changes = 0
        self._caution_laps: set[int] = set()
        self._best: dict | None = None                # лучший круг гонки
        self._best_init = False
        self._standings: dict = {"stage": None, "rows": {}, "at": 0.0, "sub": None}
        self._tower_pos: dict[str, int] = {}          # номер -> место в прошлый раз
        self._tower_moved: dict[str, tuple[str, float]] = {}   # номер -> (up/down, до какого времени)
        self.sectors = SectorTimer()
        self.gaps = GapClock()
        self.order = RaceOrder()
        self._session_key = None
        self._car_inc: dict[int, tuple[float, int]] = {}   # машина -> (когда, сколько прибавилось)
        self._inc_seen: dict[int, int] = {}                # счётчики инков в прошлый раз
        self.macro_runner = MacroRunner(self)
        self.director = Director(self)                  # повтор инцидента, камеры
        self._frozen_diag = 0.0                         # повтор: когда последний раз писали сводку кадра

    # ---------- жизненный цикл ----------

    def start(self, ctx: Context) -> None:
        self.ctx = ctx
        self.demo = ModuleDemo(ctx, "broadcast")
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="broadcast")
        self._thread.start()
        threading.Thread(target=self._standings_loop, daemon=True,
                         name="broadcast-standings").start()

        # Окна НЕ создаём: при старте приложения на экране должно быть только
        # главное окно. Оверлеи и пульт появляются, когда их включают.
        # эфир после запуска пуст: графику выводит режиссёр. Иначе флаги
        # «в эфире» из прошлого запуска расходились с тем, что на экране
        ctx.config["visible"] = {}
        ctx.config["comm_visible"] = False     # у плашки свой флаг — сбрасываем вместе
        ctx.log.info("оверлеи трансляции запущены (окна по требованию)")

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
        if self.demo is not None:
            self.demo.stop()
        self.macro_runner.stop()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    def status(self) -> dict:
        return {"cars": len(self._rows),
                "demo": self.demo.status() if self.demo else {},
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
            # Окна оверлеев держит отдельный процесс. Он сам увидит в данных,
            # что оверлей вывели в эфир, и покажет окно с анимацией.
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
            try:
                self.director.tick()
            except Exception:
                self.ctx.log.exception("режиссёр: автосмена камер")
            time.sleep(period)

    def _tick(self) -> None:
        sim = self.sim
        snap = sim.snapshot()
        if not snap.connected:
            return
        # практика → квалификация → гонка идут в одной подсессии: всё, что
        # копится за сессию, начинаем заново и при смене SessionNum
        key = (snap.subsession_id, snap.session_num)
        live = sim.live_frame()
        # в повторе SessionNum — номер просматриваемой сессии: перемотка не должна
        # стирать накопленное в живой гонке (питы, лучший круг, события)
        if key != self._session_key and (live or self._session_key is None):
            self._session_key = key
            self.order.reset()
            self._car_inc.clear()
            self._inc_seen.clear()
            self._subsession = snap.subsession_id
            self.gaps.reset()
            self._tower_pos.clear()
            self._tower_moved.clear()
            self.pits_tracker.reset()
            self.sectors.reset()
            self.flags.reset()
            self._events.clear()
            self._rc_seen.clear()
            self._inc_prev.clear()
            self._lead, self._lead_changes = None, 0
            self._caution_laps.clear()
            self._best, self._best_init = None, False
            self.director.incidents.clear()       # журнал повторов — по сессии
        if live:
            self.flags.update(course_flag(snap.session_flags), time.monotonic())

        rows = sim.timing()
        if not live:
            # повтор: CarIdx* — кадр повтора. Башня идёт по нему; если кадр пустой
            # (сим ещё ищет место на ленте, у машин нет положения) — держим
            # последнюю целую картину, а не гасим башню
            good = [r for r in rows if (r.get("lap_dist") if r.get("lap_dist") is not None else -1) >= 0]
            now_m = time.monotonic()
            if now_m - self._frozen_diag > 15:
                self._frozen_diag = now_m
                self.ctx.log.info("повтор: строк %d, с положением %d, с местом %d, кадров до конца %s",
                                  len(rows), len(good), sum(1 for r in rows if r.get("position", 0) > 0),
                                  snap.replay_frames_to_end)
            if self._rows and len(good) < max(2, len(rows) // 2):
                self.ctx.publish("timing", {"mode": snap.mode, "rows": self._rows})
                return
        if self._is_race():
            now = time.monotonic()
            if live:
                self.gaps.update(rows, now)
            rows = self.order.apply(rows, live, snap.session_state, self.gaps, now)
        if live:
            self._watch_incidents(sim)
        # В повторе CarIdx* показывают просматриваемый кадр — питы по ним
        # считать нельзя, иначе прошлые заезды запишутся как новые
        if live:
            self.pits_tracker.update(rows, snap.session_time)
            self.sectors.update(sim.sector_starts(), rows, snap.session_time)
        for r in rows:
            r["pit_stops"] = self.pits_tracker.count(r["car_idx"])
        self._rows = rows
        if live:
            self._race_facts(rows, snap)
        self.ctx.publish("timing", {"mode": snap.mode, "rows": rows})

    # ---------- данные для оверлеев ----------

    def _focus_car(self) -> str:
        """Чью строку держать в окне: выбранная в программе, иначе та, что в кадре."""
        focus = str(self.ctx.config.get("focus_car", "") or "").lstrip("#")
        if focus:
            return focus
        try:
            idx = self.sim.camera_state().get("car_idx", -1)
        except Exception:
            idx = -1
        row = next((r for r in self._rows if r["car_idx"] == idx), None)
        return row["car_number"] if row else ""

    def _status(self, snap, fl: dict) -> str:
        """Крупная строка шапки башни: что сейчас идёт."""
        flag = _flag_name(snap.session_flags)
        if fl["flag_tone"] == "yellow":
            return "Жёлтый флаг"
        if fl["flag_tone"] == "green":
            return "Зелёный флаг"
        if flag == "checkered":
            return "Финиш"
        title = str(self.ctx.config.get("tower_title", "") or "").strip()
        if title:
            return title
        try:
            stype = self.sim.session_type().lower()
        except Exception:
            stype = ""
        return SESSION_TITLE.get(stype, "Гонка")

    def _watch_incidents(self, sim) -> None:
        """Кто только что получил инки — подсветка строки на пульте."""
        now = time.monotonic()
        for idx, n in (sim.driver_incidents() or {}).items():
            prev = self._inc_seen.get(idx)
            self._inc_seen[idx] = n
            if prev is not None and n > prev:
                self._car_inc[idx] = (now, n - prev)
                self.director.on_incident(idx, n - prev, n)

    def _is_race(self) -> bool:
        try:
            stype = self.sim.session_type().lower()
        except Exception:
            return True
        return not stype or "race" in stype

    def _qual_order(self, rows: list[dict]) -> list[dict]:
        """Практика и квалификация: весь список сразу, по лучшему кругу;
        кто ещё без времени — внизу. Отрыв — от лучшего времени сессии.
        Место и лучший круг — из официальных результатов (экран Results iRacing):
        в повторе телеметрия даёт время на кадре, а сим считает и незасчитанные круги."""
        res = self.sim.session_results() if hasattr(self.sim, "session_results") else {}
        for r in rows:
            x = res.get(r["car_idx"])
            if x and x["fastest"] > 0:
                r["best_lap"] = round(x["fastest"], 3)
                r["res_pos"] = x["pos"]
        timed = sorted((r for r in rows if r.get("best_lap", 0) > 0),
                       key=lambda r: (r.get("res_pos") or 999, r["best_lap"]))
        rest = sorted((r for r in rows if not r.get("best_lap", 0) > 0),
                      key=lambda r: (r["position"] <= 0, r["position"], r["car_number"]))
        pole = timed[0]["best_lap"] if timed else 0
        out = timed + rest
        for i, r in enumerate(out, start=1):
            r["position"] = i
            r["qual"] = True
            r["gap"] = round(r["best_lap"] - pole, 3) if r.get("best_lap", 0) > 0 else 0.0
            r["laps_down"] = 0
        return out

    def tower(self) -> dict:
        """Башня: первая десятка и окно у машины в кадре («топ-10 + окно»).

        Когда поле не помещается (больше top + window машин), под первой
        десяткой — разделитель «позиции 25–32 · камера #95», ниже окно из
        window строк вокруг машины, на которую смотрит камера. Камера ушла
        к другой машине — окно едет за ней.
        """
        cfg = self.ctx.config
        snap = self.sim.snapshot()
        now = time.monotonic()
        rows = [dict(r) for r in self._rows]
        top = max(1, int(cfg.get("tower_top", 10)))
        size = max(1, int(cfg.get("tower_window", 8)))
        focus = self._focus_car()
        race = self._is_race()
        if not race:
            rows = self._qual_order(rows)

        # ▲▼ у строки три секунды после смены места; при перемотке повтора
        # места прыгают — не обгоны
        live = self.sim.live_frame()
        for r in rows:
            num = r["car_number"]
            prev = self._tower_pos.get(num)
            if live and prev is not None and prev != r["position"]:
                self._tower_moved[num] = ("up" if r["position"] < prev else "down", now + 3.0)
            self._tower_pos[num] = r["position"]
            mv = self._tower_moved.get(num)
            r["moved"] = mv[0] if mv and mv[1] > now else ""
        best = min((r["best_lap"] for r in rows if r.get("best_lap", 0) > 0), default=0)
        lead = rows[0] if rows else None
        dist = lambda r: r.get("dist", r["lap"] + r.get("lap_dist", 0))          # noqa: E731
        lead_dist = dist(lead) if lead else 0
        for r in rows:
            r["fastest"] = bool(best) and r.get("best_lap") == best
            r["in_pit"] = bool(r.get("on_pit") or r.get("in_pit_stall"))
            r["focus"] = bool(focus) and r["car_number"] == focus
            if race:
                r["laps_down"] = max(0, int(lead_dist - dist(r))) if dist(r) > -1e8 else 0
            if not r.get("color"):
                r["color"] = CLASS_COLOR.get(str(r.get("class_name", "")).upper(), "#5A6B85")
        if cfg.get("gap_mode") == "ahead":
            self._gaps_to_ahead(rows, race, now)

        sep_after = win_from = win_to = 0
        note = ""
        shown = rows
        if len(rows) > top + size:
            fpos = next((i for i, r in enumerate(rows) if r["focus"]), -1)
            start = top if fpos < top else min(max(fpos - 3, top), len(rows) - size)
            shown = rows[:top] + rows[start:start + size]
            sep_after, win_from, win_to = top, start + 1, start + size
            if fpos >= top:
                note = f" · камера #{focus}"

        fl = self.flags.state(now)
        return {
            "visible": self.is_visible("tower"),
            "mode": snap.mode,
            "track": snap.track,
            "track_short": track_short(snap.track),
            "status": self._status(snap, fl),
            "session_time": snap.session_time,
            "gap_mode": cfg.get("gap_mode", "leader"),
            "show_pits": bool(cfg.get("show_pits", True)),
            "title": cfg.get("tower_title", ""),
            "rows": shown,
            "total": len(rows),
            "focus_car": focus,
            "sep_after": sep_after, "win_from": win_from, "win_to": win_to, "win_note": note,
            "flag": _flag_name(snap.session_flags),
            **fl,
            "lap": max((r["lap"] for r in rows), default=0),
            "laps_total": int(cfg.get("laps_total", 0)),
        }

    def _gaps_to_ahead(self, rows: list[dict], race: bool, now: float) -> None:
        """Режим «до впереди»: отрыв и круги отставания до соседа сверху."""
        lead_gap = {r["car_idx"]: r["gap"] for r in rows}
        for prev, r in zip(rows, rows[1:]):
            if not race:
                r["gap"] = round(max(0.0, r["best_lap"] - prev["best_lap"]), 3) \
                    if r.get("best_lap", 0) > 0 and prev.get("best_lap", 0) > 0 else 0.0
                continue
            g = self.gaps.behind(prev["car_idx"], r["car_idx"], now)
            r["gap"] = round(g if g is not None
                             else max(0.0, lead_gap[r["car_idx"]] - lead_gap[prev["car_idx"]]), 3)
            pd, d = self.gaps.pos.get(prev["car_idx"]), self.gaps.pos.get(r["car_idx"])
            r["laps_down"] = max(0, int(pd - d)) if pd is not None and d is not None else 0

    # ---------- факты гонки: лидер, жёлтые круги, лучший круг, события ----------

    def _race_facts(self, rows: list[dict], snap) -> None:
        now = time.monotonic()
        lead = rows[0] if rows else None
        if lead:
            if self._lead is not None and lead["car_number"] != self._lead:
                self._lead_changes += 1
            self._lead = lead["car_number"]
            if self.flags.last == "yellow":
                self._caution_laps.add(lead["lap"])
        # лучший круг: при первом взгляде запоминаем без плашки — программу
        # могли открыть посреди гонки
        cand = min((r for r in rows if r.get("best_lap", 0) > 0),
                   key=lambda r: r["best_lap"], default=None)
        if cand:
            t_best = cand["best_lap"]
            if not self._best_init:
                self._best = {"row": dict(cand), "time": t_best, "lap": cand.get("laps", 0), "at": 0.0}
            elif not self._best or t_best < self._best["time"] - 0.0005:
                self._best = {"row": dict(cand), "time": t_best, "lap": cand.get("laps", 0), "at": now}
        if rows:
            self._best_init = True
        # флаги — в ленту
        ev = self.flags.event
        if ev and ev["id"] != self._flag_seen:
            self._flag_seen = ev["id"]
            tone = ev["tone"]
            self._events.append({"key": f"flag{ev['id']}", "at": now, "keep": FLAG_KEEP_S,
                                 "lap": lead["lap"] if lead else 0, "kind": tone,
                                 "why": ("Пейс-кар на трассе" if tone == "yellow"
                                         else self.flags.green_text)})
        # инциденты: в демо — по прибавке счётчика, вживую — из рейс-контроля
        by_idx = {r["car_idx"]: r for r in rows}
        if getattr(self.sim, "demo", False):
            inc = self.sim.driver_incidents()
            for idx, n in inc.items():
                prev = self._inc_prev.get(idx)
                self._inc_prev[idx] = n
                kind = _incident_kind(n - prev) if prev is not None else None
                r = by_idx.get(idx)
                if kind and r:
                    self._events.append({"key": f"d{idx}-{n}", "at": now, "keep": EVENT_KEEP_S,
                                         "lap": r["lap"], "kind": kind, "row": dict(r)})
        elif now >= self._rc_next:
            self._rc_next = now + 2.0
            self._pull_incidents(by_idx, snap, now)

    def _pull_incidents(self, by_idx: dict, snap, now: float) -> None:
        """Новые инциденты рейс-контроля текущей сессии — в ленту.
        Старше EVENT_KEEP_S по времени сессии не берём: при запуске посреди
        гонки лента не должна вывалить всё прошлое разом."""
        app = getattr(self.ctx, "app", None)
        rc = app.module("racecontrol") if app else None
        if rc is None:
            return
        try:
            items = rc.incidents()
        except Exception:
            return
        for it in items:
            iid = it.get("id")
            if iid is None or iid in self._rc_seen:
                continue
            self._rc_seen.add(iid)
            if snap.session_time - float(it.get("session_time") or 0) > EVENT_KEEP_S:
                continue
            kind = _incident_kind(int(it.get("inc_delta") or 0), it.get("kind") or "incident")
            if not kind:
                continue
            r = by_idx.get(it.get("car_idx")) or {"car_number": it.get("car_number", ""),
                                                   "name": it.get("driver_name", ""),
                                                   "class_name": ""}
            self._events.append({"key": f"rc{iid}", "at": now, "keep": EVENT_KEEP_S,
                                 "lap": int(it.get("lap") or 0), "kind": kind, "row": dict(r)})

    def events(self) -> dict:
        """Лента событий гонки: свежие сверху, не больше четырёх."""
        now = time.monotonic()
        rows = []
        for e in reversed(self._events):
            if now - e["at"] > e["keep"]:
                continue
            text, fill, fg = EVENT_TAGS[e["kind"]]
            r = e.get("row") or {}
            rows.append({"key": e["key"], "lap_text": f"К {e['lap']}" if e["lap"] else "",
                         "car_number": r.get("car_number", ""), "name": r.get("name", ""),
                         "color": self._color(r) if r else "", "why": e.get("why", ""),
                         "tag": text, "tag_color": fill, "tag_fg": fg,
                         "fresh": now - e["at"] < 2.0})
            if len(rows) >= 4:
                break
        # как плашка лучшего круга: в эфир — с новым событием, через
        # EVENTS_SHOW_S после последнего уходит сама
        fresh = bool(self._events) and now - self._events[-1]["at"] < EVENTS_SHOW_S
        return {"visible": self.is_visible("events") and fresh and bool(rows), "rows": rows}

    def bestlap(self) -> dict:
        """Новый лучший круг: плашка видна BESTLAP_SHOW_S после рекорда, если
        режиссёр включил её в эфир."""
        b = self._best
        fresh = bool(b and b["at"] and time.monotonic() - b["at"] < BESTLAP_SHOW_S)
        snap = self.sim.snapshot()
        out = {"visible": self.is_visible("bestlap") and fresh, "ready": bool(b),
               "title": self._status(snap, self.flags.state(time.monotonic()))}
        if b:
            r = b["row"]
            out.update(car_number=r["car_number"], name=r["name"], color=self._color(r),
                       time=b["time"], lap=b["lap"])
        return out

    @staticmethod
    def _fmt_c(v) -> str:
        return "—" if v is None else f"{v:.0f} °C"

    def weather(self) -> dict:
        """Трасса и погода: условия из сима, лучший круг и счётчики гонки."""
        sim = self.sim
        snap = sim.snapshot()
        try:
            w = sim.weather()
        except Exception:
            w = {}
        track = w.get("track") or snap.track
        line = [track]
        if w.get("config"):
            line.append(w["config"])
        if w.get("length_km"):
            line.append(f"{w['length_km']:.2f} км".replace(".", ","))
        if w.get("turns"):
            n = w["turns"]
            line.append(f"{n} " + ("поворот" if n % 10 == 1 and n % 100 != 11 else
                                   "поворота" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14
                                   else "поворотов"))
        wind_text, arrow, wind_from = "—", "", ""
        if w.get("wind_speed") is not None and w.get("wind_dir") is not None:
            deg = math.degrees(w["wind_dir"]) % 360          # откуда дует
            i = int((deg + 22.5) // 45) % 8
            arrow = ARROWS[(i + 4) % 8]                        # стрелка — куда дует
            wind_text = f"{w['wind_speed'] * 3.6:.0f} км/ч"
            wind_from = COMPASS[i]
        skies = w.get("skies")
        skies_text = (SKIES[skies] if isinstance(skies, int) and 0 <= skies < len(SKIES)
                      else w.get("skies_text") or "—")
        tod = w.get("time_of_day")
        tod_text = "—" if tod is None else f"{int(tod // 3600) % 24:02d}:{int(tod % 3600 // 60):02d}"
        hum = w.get("humidity")
        sof = _sof([d.irating for d in (snap.drivers or []) if getattr(d, "irating", 0)])
        b = self._best
        return {
            "visible": self.is_visible("weather"),
            "track": track, "track_short": track_short(track),
            "caption": track_short(track) + (f" · SOF {sof}" if sof else ""),
            "track_line": " · ".join(line),
            "air": self._fmt_c(w.get("air")), "track_temp": self._fmt_c(w.get("track_temp")),
            "wind": wind_text, "wind_arrow": arrow, "wind_from": wind_from,
            "humidity": "—" if hum is None else f"{hum:.0f} %",
            "skies": skies_text, "time_of_day": tod_text,
            "best_time": b["time"] if b else 0,
            "best_who": f"#{b['row']['car_number']} {b['row']['name']}" if b else "",
            "lead_changes": self._lead_changes,
            "caution_laps": len(self._caution_laps),
            "pit_stops": sum(self.pits_tracker.count(r["car_idx"]) for r in self._rows),
            "cars": len(self._rows), "sof": sof,
        }

    # ---------- таблица чемпионата с сайта ----------

    def _standings_loop(self) -> None:
        """Раз в пару минут — таблица чемпионата с irchamps.gg для решётки."""
        while not self._stop.wait(5.0):
            try:
                if time.monotonic() - self._standings["at"] >= STANDINGS_EVERY_S:
                    self._fetch_standings()
            except Exception:
                self.ctx.log.exception("таблица чемпионата: сбой")

    def _fetch_standings(self) -> None:
        sim = self.sim
        st = self._standings
        if getattr(sim, "demo", False):
            st.update(at=time.monotonic(), stage=None, rows={}, sub=None)
            return
        snap = sim.snapshot()
        app = getattr(self.ctx, "app", None)
        lic = getattr(app, "license", None) if app else None
        sub = int(snap.subsession_id or 0) if snap.connected else 0
        if not lic or not sub:
            return
        body = {"subsession_id": sub}
        rc = app.module("racecontrol")
        stage = getattr(getattr(rc, "live", None), "stage", None) if rc else None
        if stage and stage.get("id"):
            body["stage_plan_id"] = stage["id"]
        res = lic.api("/api/desktop/standings", body)
        st["at"] = time.monotonic()
        if not res.get("ok"):
            return                      # нет связи — покажем, что было
        rows = {int(r["customer_id"]): (int(r.get("position") or 0), int(r.get("points") or 0))
                for r in res.get("rows") or [] if r.get("customer_id")}
        st.update(stage=res.get("stage"), rows=rows, sub=sub)

    def _grid_points(self) -> tuple[dict, dict]:
        """(по номеру машины или cust_id -> (место, очки), подпись этапа).
        Очки показываем со второго этапа: на первом таблица пустая."""
        st = self._standings
        if getattr(self.sim, "demo", False):
            return {"demo": True}, {"caption": "демо-чемпионат · этап 3 из 6", "points": True}
        stage = st.get("stage")
        if not stage:
            return {}, {"caption": "", "points": False}
        cap = f"{stage.get('championship', '')} · этап {stage.get('number')} из {stage.get('of')}"
        return st["rows"], {"caption": cap.strip(" ·"),
                            "points": bool(st["rows"]) and int(stage.get("number") or 0) > 1}

    # ---------- остальные оверлеи ----------

    def grid(self) -> dict:
        """Стартовая решётка."""
        rows = self.sim.qualifying_grid()
        source = "qualifying"
        if not rows:
            # квалификации не было или результаты ещё не пришли — показываем
            # текущий порядок, честно помечая источник
            rows = [{"position": r["position"], "car_idx": r["car_idx"],
                     "car_number": r["car_number"], "name": r["name"],
                     "team": r["team"], "class_name": r["class_name"],
                     "car": r.get("car", ""), "color": r.get("color", ""),
                     "cust_id": r.get("cust_id", 0),
                     "time": r["best_lap"] if r["best_lap"] > 0 else 0.0}
                    for r in (self._rows if self._is_race()
                              else self._qual_order([dict(x) for x in self._rows]))]
            source = "current"
        pole = rows[0]["time"] if rows and rows[0]["time"] > 0 else 0.0
        for r in rows:
            r["delta"] = round(r["time"] - pole, 3) if (r["time"] and pole) else 0.0
            if not r.get("color"):
                r["color"] = CLASS_COLOR.get(str(r.get("class_name", "")).upper(), "#5A6B85")
        c = self.ctx.config
        track = self.sim.snapshot().track
        pts, champ = self._grid_points()
        if pts.get("demo"):
            # демо: выдуманные очки, чтобы было видно колонку
            order = sorted(rows, key=lambda r: (int(r["car_number"]) * 37) % 101)
            for i, r in enumerate(order):
                r["pts"], r["rank"] = max(0, 140 - i * 7), i + 1
        else:
            for r in rows:
                got = pts.get(int(r.get("cust_id") or 0))
                r["pts"], r["rank"] = (got[1], got[0]) if got else (0, 0)
        return {"visible": self.is_visible("grid"),
                "source": source,
                "track": track,
                "track_short": track_short(track),
                "caption": " · ".join(x for x in (track_short(track), champ["caption"].upper()) if x),
                "show_points": champ["points"],
                "title": c.get("grid_title", "стартовая решётка"),
                # ограничение щедрое: решётка листается страницами по десять,
                # и обрезать состав больше незачем
                "rows": rows[:int(c.get("grid_rows", 60))]}

    def commentators(self) -> dict:
        c = self.ctx.config
        people = c.get("commentators", [])[:2]
        if not people:
            # в демо плашка пустой не нужна: её показывают ради вёрстки
            people = self.sim.demo_hints().get("commentators", [])
        return {"visible": self.is_visible("commentators"),
                "people": people,
                "caption": c.get("comm_caption", "")}

    def set_commentators(self, data: dict) -> dict:
        """Имена, подпись и показ плашки из карточки в панели.

        Показ идёт через общий set_visible, как у остальных кнопок: он и
        поднимает окна оверлеев, и держит флаги в согласии. Раньше здесь
        ставился только флаг «в эфире» — процесс окон не запускался, и
        плашка не появлялась.
        """
        c = self.ctx.config
        if "people" in data:
            people = []
            for item in list(data["people"])[:2]:   # больше двух в плашку не влезает
                people.append({"name": str(item.get("name", "")).strip(),
                               "role": str(item.get("role", "")).strip()})
            c["commentators"] = [x for x in people if x["name"]]
        if "caption" in data:
            c["comm_caption"] = str(data["caption"] or "").strip()
        self.ctx.save_config()
        if "visible" in data:
            want = bool(data["visible"])
            current = self.commentators()
            if want and not current["people"] and not current["caption"]:
                return {"ok": False, "error": "no_people",
                        "message": "Впиши хотя бы одно имя — без имён плашке нечего показать."}
            self.set_visible("commentators", want)
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
            pair = self.sim.demo_hints().get("delta_pair", pair)
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
                         "class_name": lead.get("class_name", ""), "color": self._color(lead)},
                "chase": {"car_number": chase["car_number"], "name": chase["name"],
                          "position": chase["position"],
                          "class_name": chase.get("class_name", ""), "color": self._color(chase)},
                "laps_apart": lead["laps"] - chase["laps"]}

    @staticmethod
    def _color(r: dict) -> str:
        return r.get("color") or CLASS_COLOR.get(str(r.get("class_name", "")).upper(), "#5A6B85")

    def pits(self) -> dict:
        """Пит-стопы по полю: сколько раз стоял и как долго."""
        c = self.ctx.config
        rows = []
        for r in sorted(self._rows, key=lambda x: (x["position"] <= 0, x["position"])):
            stops = self.pits_tracker.stops(r["car_idx"])
            if not stops and not c.get("pits_show_all", False):
                continue
            rows.append({
                "position": r["position"],
                "car_number": r["car_number"],
                "name": r["name"],
                "class_name": r.get("class_name", ""),
                "color": self._color(r),
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
                "tower_top": c.get("tower_top", 10),
                "cam_favorites": c.get("cam_favorites", []),
                "tower_window": c.get("tower_window", 8),
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
                "replay_fx": self.director.variant(),
                "replay_bug": bool(c.get("replay_bug", True)),
                "commentators": c.get("commentators", [])}

    def set_settings(self, data: dict) -> dict:
        c = self.ctx.config
        for key in ("tower_rows", "tower_top", "tower_window", "cam_favorites", "gap_mode", "show_pits", "tower_title",
                    "focus_car", "refresh_hz", "grid_title", "grid_rows",
                    "delta_pair", "pits_title", "pits_rows", "pits_show_all",
                    "comm_visible", "comm_caption", "replay_fx", "replay_bug"):
            if key in data:
                c[key] = data[key]
        self.ctx.save_config()
        return {"ok": True, **self.settings()}

    # ---------- HTTP ----------

    # ---------- пульт: карточки, камеры, макросы ----------

    def _inc_flash(self, idx: int) -> dict:
        hit = self._car_inc.get(idx)
        if not hit:
            return {}
        age = time.monotonic() - hit[0]
        if age > INC_FLASH_S:
            self._car_inc.pop(idx, None)
            return {}
        return {"inc_new": hit[1], "inc_fade": round(1 - age / INC_FLASH_S, 3)}

    def cards(self) -> dict:
        """Карточки пилотов для пульта.

        Разрыв считаем двумя способами сразу: до лидера и до идущего впереди.
        Сектора — текущего круга, прошлого и лучшие, плюс лучшие по полю,
        чтобы пульт подсветил фиолетовым, как в таймингах.
        """
        sim = self.sim
        snap = sim.snapshot()
        starts = sim.sector_starts()
        n = len(starts)
        inc = sim.driver_incidents()
        cam = sim.camera_state()
        rows = sorted(self._rows, key=lambda r: (r["position"] <= 0, r["position"]))
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
                # инки только что: строка подсвечена и гаснет за INC_FLASH_S
                **self._inc_flash(r["car_idx"]),
                "sectors": self.sectors.get(r["car_idx"], n),
            })
            prev = r
        return {"mode": snap.mode, "live": snap.mode == "LIVE",
                "delta_pair": [str(x).lstrip("#") for x in self.ctx.config.get("delta_pair", [])][:2],
                "demo": bool(getattr(sim, "demo", False)),
                "sectors_n": n, "best": self.sectors.best_overall(n),
                "focus_car_idx": cam.get("car_idx", -1), "rows": out}

    def _camera_now(self) -> dict:
        sim = self.sim
        cur = sim.camera_state()
        names = {v: k for k, v in sim.camera_groups().items()}
        row = next((r for r in self._rows if r["car_idx"] == cur.get("car_idx")), None)
        return {**cur, "group_name": names.get(cur.get("group"), ""),
                "car_number": row["car_number"] if row else ""}

    def cameras(self) -> dict:
        groups = sorted(({"name": k, "num": v}
                         for k, v in self.sim.camera_groups().items()),
                        key=lambda g: g["num"])
        return {"groups": groups, "current": self._camera_now()}

    def camera(self, data: dict) -> dict:
        """Камера на машину и/или группу.

        car: номер, «leader» или пусто/«focus» — машина, что уже в кадре.
        group: номер группы или её имя из CameraInfo; пусто — текущая.
        """
        sim = self.sim
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
        "weather": {"x": 1420, "y": 780}, "events": {"x": 16, "y": 830},
        "bestlap": {"x": 16, "y": 740},
        "replayfx": {"x": 0, "y": 0}, "replaybug": {"x": 1640, "y": 36},
    }

    def positions(self) -> dict:
        """Хосту оверлеев: какие окна поднимать и где они стояли."""
        pos = self.ctx.config.get("positions") or {}
        ids = [i for i, _ in OVERLAYS] + list(FX_OVERLAYS)
        return {"overlays": ids,
                "positions": {i: pos.get(i) or self.DEFAULT_POS.get(i, {"x": 40, "y": 120}) for i in ids}}

    def set_position(self, data: dict) -> dict:
        oid = str(data.get("id", ""))
        if oid not in dict(OVERLAYS) and oid not in FX_OVERLAYS:
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
            "POST /api/broadcast/demo": lambda r: self.demo.set(bool(body(r).get("on"))),
            "GET /api/broadcast/grid": lambda r: self.grid(),
            "GET /api/broadcast/commentators": lambda r: self.commentators(),
            "POST /api/broadcast/commentators": lambda r: self.set_commentators(body(r)),
            "GET /api/broadcast/delta": lambda r: self.delta(),
            "GET /api/broadcast/pits": lambda r: self.pits(),
            "GET /api/broadcast/weather": lambda r: self.weather(),
            "GET /api/broadcast/events": lambda r: self.events(),
            "GET /api/broadcast/bestlap": lambda r: self.bestlap(),
            "GET /api/broadcast/visibility": lambda r: self.visibility(),
            "POST /api/broadcast/deck":
                lambda r: self.show_deck(bool(body(r).get("visible"))),
            "POST /api/broadcast/visibility":
                lambda r: (lambda b: self.set_visible(str(b.get("id", "")),
                                                      bool(b.get("visible"))))(body(r)),
            "GET /api/broadcast/settings": lambda r: self.settings(),
            "POST /api/broadcast/settings": lambda r: self.set_settings(body(r)),
            "GET /api/broadcast/cards": lambda r: self.cards(),
            "GET /api/broadcast/director": lambda r: self.director.state(),
            "POST /api/broadcast/director": lambda r: self.director.set(body(r)),
            "POST /api/broadcast/replay":
                lambda r: (lambda b: self.director.live() if b.get("live") else self.director.show(b.get("id", 0)))(body(r)),
            "GET /api/broadcast/fx": lambda r: self.director.fx_state(),
            **{f"GET /ov/{oid}": (lambda oid: lambda r: ((WEB / "ov_replay.html").read_bytes()
                                                         .replace(b"__PART__", oid.encode()), "text/html; charset=utf-8"))(oid)
               for oid in FX_OVERLAYS},
            "GET /api/broadcast/cameras": lambda r: self.cameras(),
            "POST /api/broadcast/camera": lambda r: self.camera(body(r)),
            "GET /api/broadcast/macros": lambda r: self.macros_get(),
            "POST /api/broadcast/macros": lambda r: self.macros_save(body(r)),
            "POST /api/broadcast/macro/run":
                lambda r: self.macro_runner.run(str(body(r).get("id", ""))),
            "POST /api/broadcast/macro/stop":
                lambda r: (self.macro_runner.stop(), {"ok": True})[1],
        }
