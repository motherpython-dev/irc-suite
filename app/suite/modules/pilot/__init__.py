"""Модуль P: оверлеи пилота — поверх сима на компьютере самого пилота.

Пять окон: Standings (таблица позиций), Relative (кто рядом на трассе),
радар «слева и справа», ветер относительно машины и двигатель (вода, масло).
Окна держит тот же процесс, что и эфирные оверлеи (`ui/overlay_host.py`),
картинку рисует `ui/pilot_paint.py`. Модуль отдаёт готовые к отрисовке данные:
`GET /api/pilot/data?id=<оверлей>`.

Каналы своей машины и массивы CarIdx читает отвод сима на 60 Гц: радару
нужна каждая смена `CarLeftRight`. Таблицы считаются по запросу из
`sim.timing()` не чаще пяти раз в секунду.

Чего iRacing не отдаёт и что из этого следует:
  * поперечного расстояния до соседа нет — радар знает только сторону
    (`CarLeftRight`) и продольное положение (`CarIdxLapDistPct`);
  * рабочих и предельных температур двигателя машины нет, своих порогов
    программа не задаёт: цвет и тревога — только по индикации самого сима
    (`EngineWarnings`: перегрев воды, масла, низкое давление). Температура, при
    которой лампа загорелась, запоминается для машины — только как отметка
    на шкале.
"""
from __future__ import annotations

import json
import math
import threading
import time
from urllib.parse import parse_qs, urlparse

from ...core import config as core_config
from ...core.modules import Context
from ...core.livery_colors import LiveryColors
from ...core.replay_demo import ModuleDemo
from ...core.radar_script import radar_script
from ..broadcast import CLASS_COLOR, FlagWatch, GapClock, PitTracker, RaceOrder, course_flag, track_short
from ..broadcast.sectors import SectorTimer
from ..telemetry.radar_probe import RadarProbe
from .setups import SetupLog, demo_summary, setup_hash
from ..telemetry.history import SetupHistory

OVERLAYS = (
    ("standings", "Standings"),
    ("relative", "Relative"),
    ("radar", "Радар"),
    ("wind", "Ветер"),
    ("engine", "Двигатель"),
    ("setups", "Сетапы"),
)

# Колонки таблиц: ключ, подпись в настройках
COLUMNS = (("chg", "▲▼ к старту"), ("ld", "Δ круга ко мне"), ("ir", "iRating"), ("dir", "Прогноз iR"), ("lic", "Лицензия"),
           ("flags", "Флаги"), ("pits", "Пит-стопы"), ("pitlast", "Последний пит"), ("sec", "Сектора"), ("last", "Последний"),
           ("best", "Лучший"), ("gap", "Отрыв"))

DEFAULTS = {
    # replay — показывать и в повторе (места и отрывы на момент кадра повтора)
    "standings": {"lock": 0, "anchor": "tl", "scale": 100, "top": 3, "around": 4, "by_class": False, "replay": 1, "inc": 1, "head": "top",
                  "flag_wave": 0, "bg": 92,
                  "font_pos": "exo", "font_name": "exo", "font_num": "mono",
                  "cols": {"chg": 1, "ld": 0, "ir": 1, "dir": 0, "lic": 1, "flags": 1, "pits": 0, "pitlast": 0, "sec": 0,
                           "last": 1, "best": 0, "gap": 1},
                  # практика и квалификация: свои колонки, по умолчанию лучший круг вместо последнего
                  "cols_q": {"ld": 0, "ir": 1, "dir": 0, "lic": 1, "flags": 1, "pits": 0, "pitlast": 0, "sec": 0,
                             "last": 0, "best": 1, "gap": 1}},
    "relative": {"lock": 0, "anchor": "tl", "scale": 100, "around": 3, "replay": 1, "head": "top", "flag_wave": 0, "bg": 92,
                 "font_pos": "exo", "font_name": "exo", "font_num": "mono",
                 "cols": {"ld": 0, "ir": 1, "dir": 0, "lic": 1, "flags": 1, "pits": 0, "sec": 0, "last": 0, "gap": 1}},
    # радар: расстояние между полосами, длина, толщина, изгиб (+ наружу «( )»,
    # − внутрь «) (»), цвет и прозрачность индикатора; test — индикатор всегда
    # car_cm — длина вашей машины: плашка радара целиком равна ей
    "radar": {"lock": 0, "anchor": "tl", "spread": 420, "len": 110, "width": 26, "bend": 0, "color": "#FF5A1F", "alpha": 100,
              "test": 0, "car_cm": 500, "bg": 75},
    "wind": {"lock": 0, "anchor": "tl", "scale": 70, "style": "ring", "bg": 92},
    # кто говорит в рацию: где показывать (relative / standings / both / off),
    # строка «Говорит» под таблицей, если пилота нет в окне, и частота
    "voice": {"where": "relative", "strip": 1, "freq": 1},
    # цвет и тревога — по индикации сима; своих порогов нет
    "engine": {"lock": 0, "anchor": "tl", "scale": 100, "units": "C", "press": 1, "rpm": 1, "bars": 1, "flash": 1,
               "style": "rings", "bg": 92},
    # сравнение сетапов: столбцов максимум (0 — все), лучшее или среднее по чистым,
    # закреплённый сетап (отпечаток содержимого), строки отставания, теор. круга и кругов
    # tyres — блок «Резина»; hist — всегда 0: окно сравнивает сетапы внутри одной сессии
    # (история прошлых заездов была в 2.66.0–2.66.1, убрана по решению 2026-11-08)
    "setups": {"lock": 0, "anchor": "tl", "scale": 100, "max": 3, "mode": "best", "pin": "",
               "delta": 1, "theo": 1, "laps": 1, "hist": 0, "tyres": 1, "bg": 92},
}

# Куда ставить окно, пока его ни разу не двигали (экран 1920x1080)
DEFAULT_POS = {"standings": {"x": 40, "y": 120}, "relative": {"x": 1400, "y": 600},
               "radar": {"x": 750, "y": 560}, "wind": {"x": 900, "y": 30},
               "engine": {"x": 1530, "y": 120}, "setups": {"x": 40, "y": 640}}

ACC_DEFAULT = "#FF5A1F"
SESSION = {"race": ("Race", "#FF5A1F"), "practice": ("Practice", "#3B82F6"),
           "qualify": ("Qualifying", "#A855F7"), "warmup": ("Warmup", "#3B82F6"),
           "testing": ("Test", "#3B82F6")}

# Пороги цвета температур, как у цифр в чёрной плашке iRacing (сим их не отдаёт):
# CarPath -> {"oil"/"water": (жёлтый с, красный с), °C}. Сняты с экрана: цвет цифры
# и температура из телеметрии в момент смены, 20 замеров в секунду.
#   Chevrolet Silverado (2026-11-06): масло жёлтое между 134,98 и 135,22, красное между
#   139,92 и 140,16 — дважды одинаково. Флаг перегрева масла (EngineWarnings 0x40) сим
#   ставит раньше, около 133 °C при зелёной цифре, — по нему цвет не считаем.
#   Вода на коротком овале до жёлтого не дошла (максимум 103 °C) — нужен суперспидвей.
#   Траки этого поколения отличаются только кузовом — пороги общие (Silverado 2019,
#   Tundra 2022, F-150, RAM 2026); старый Silverado («trucks silverado») — другое поколение.
# траки: вода жёлтая со 126, красная со 130 °C (со слов пилота, 2026-11-11)
_TRUCKS = {"oil": (135.0, 140.0), "water": (126.0, 130.0)}
# вода — жёлтая со 146 °C (со слов пилота, 2026-11-09); красный не известен — None:
# красного по воде нет (флаг сима 0x01 загорается около 113,5, раньше жёлтого)
_NEXTGEN = {"oil": (135.0, 145.0), "water": (146.0, None)}
TEMP_LIMITS = {
    "trucks silverado2019": _TRUCKS,
    "trucks toyotatundra2022": _TRUCKS,
    "trucks fordf150": _TRUCKS,
    "trucks ram2026": _TRUCKS,
    # NASCAR Next Gen (Camaro ZL1 сняли на Daytona 2026-11-09): масло жёлтое между 134,92 и 135,01 и
    # обратно в зелёный на том же месте; красный 145 — со слов пилота (в гонке масло дошло до 137,3).
    # Вода до 115,9 °C оставалась зелёной (флаг сима 0x01 — уже на 113,5) — порог выше, пока не снят.
    "stockcars chevycamarozl12022": _NEXTGEN,
    "stockcars fordmustang2022": _NEXTGEN,
    "stockcars toyotacamry2022": _NEXTGEN,
}

# EngineWarnings (irsdk_EngineWarnings)
W_WATER, W_OIL_PRESS, W_OIL_TEMP = 0x01, 0x04, 0x40
# CarIdxSessionFlags
F_BLACK, F_DQ, F_FURLED, F_REPAIR = 0x00010000, 0x00020000, 0x00080000, 0x00100000
ON_TRACK = 3                       # irsdk_TrkLoc.on_track
PROBE_KEEP = 20                    # сколько файлов записи радара хранить

CHANNELS = ["CarLeftRight", "CarIdxLapDistPct", "CarIdxTrackSurface", "CarIdxEstTime",
            "CarIdxSessionFlags", "PlayerCarIdx", "IsOnTrack", "IsReplayPlaying",
            "WaterTemp", "OilTemp", "OilPress", "EngineWarnings", "RPM",
            "WindVel", "WindDir", "YawNorth", "SessionTimeRemain", "SessionLapsRemainEx",
            "SessionTime", "RadioTransmitCarIdx", "RadioTransmitRadioIdx", "RadioTransmitFrequencyIdx",
            "PlayerCarMyIncidentCount", "LapDistPct", "OnPitRoad",
            "PlayerCarSLFirstRPM", "PlayerCarSLShiftRPM", "PlayerCarSLLastRPM", "PlayerCarSLBlinkRPM"]
VOICE_KEEP_S = 1.2                 # после конца реплики — окну хватит доиграть уход


def _wrap(x: float) -> float:
    """Разница по кругу в [-0.5, 0.5)."""
    return ((x % 1.0) + 1.5) % 1.0 - 0.5


class RadarSides:
    """С какой стороны каждая машина рядом и на какой дорожке.

    iRacing называет только сторону и число машин (CarLeftRight), но не какие
    это машины; положение по длине даёт CarIdxLapDistPct. Связываем так:
      * сторона одна — все перекрывающие машины с этой стороны;
      * «слева и справа» — машина получает ту сторону, где сим прибавил машину,
        когда она вошла в перекрытие, и держит её, пока не выйдет. Вошли разом
        с двух сторон — разложим наугад: это единственный случай, где стороны
        могут оказаться перепутаны.
    Две машины с одной стороны, перекрывающие друг друга по длине, едут бок о бок
    (тривайд, дорожки); не перекрывающие — друг за другом (поезд, одна дорожка).
    """
    N_LEFT = {2: 1, 4: 1, 5: 2}
    N_RIGHT = {3: 1, 4: 1, 6: 2}

    def __init__(self):
        self.side: dict[int, str] = {}
        self.lane: dict[int, int] = {}
        self.seq: dict[int, int] = {}
        self.n = 0

    def reset(self) -> None:
        self.side.clear()
        self.lane.clear()
        self.seq.clear()

    def update(self, clr: int, near: list[tuple[int, float]], car_m: float) -> list[dict]:
        nl, nr = self.N_LEFT.get(clr, 0), self.N_RIGHT.get(clr, 0)
        if not nl and not nr:                      # сим говорит «чисто» — рядом никого
            self.reset()
            return []
        ids = {i for i, _ in near}
        for d in (self.side, self.lane, self.seq):
            for i in [x for x in d if x not in ids]:
                d.pop(i)
        for i, _ in near:
            if i not in self.seq:
                self.n += 1
                self.seq[i] = self.n
        if nl and not nr:
            self.side = {i: "l" for i, _ in near}
        elif nr and not nl:
            self.side = {i: "r" for i, _ in near}
        else:
            # обе стороны: новые машины — туда, где не хватает
            for i, _ in sorted(near, key=lambda x: self.seq[x[0]]):
                if i in self.side:
                    continue
                cl = sum(1 for v in self.side.values() if v == "l")
                cr = sum(1 for v in self.side.values() if v == "r")
                self.side[i] = "l" if nl - cl >= nr - cr else "r"
            # сим говорит «с обеих», а у нас все с одной — младшая переходит
            for a, b in (("l", "r"), ("r", "l")):
                mine = [i for i, v in self.side.items() if v == a]
                if len(mine) > 1 and not any(v == b for v in self.side.values()):
                    self.side[max(mine, key=lambda i: self.seq[i])] = b
        out = []
        for s in ("l", "r"):
            cars = sorted(((i, d) for i, d in near if self.side.get(i) == s), key=lambda x: -x[1])
            wide = any(abs(cars[a][1] - cars[b][1]) < car_m
                       for a in range(len(cars)) for b in range(a + 1, len(cars)))
            for i, d in cars:
                c = {"s": s, "dm": round(d, 2)}
                if wide:
                    # дорожка держится за машиной: иначе они менялись бы местами при обгоне
                    if i not in self.lane:
                        used = {self.lane[x] for x, _ in cars if x in self.lane}
                        self.lane[i] = 0 if 0 not in used else 1
                    c["lane"] = self.lane[i]
                else:
                    self.lane.pop(i, None)
                out.append(c)
        return out


def ir_delta(irs: list[int]) -> list[int]:
    """Прогноз изменения iRating по месту — оценка калькуляторов, не официальная.

    irs — рейтинги в порядке мест. Ожидаемое число «побеждённых» по
    попарным шансам и поправка за размер поля."""
    n = len(irs)
    if n < 2:
        return [0] * n
    br = 1600 / math.log(2)

    def chance(a, b):
        ea, eb = math.exp(-a / br), math.exp(-b / br)
        return (1 - ea) * eb / ((1 - eb) * ea + (1 - ea) * eb)

    out = []
    for k, a in enumerate(irs):
        a = a or 1350
        exp = sum(chance(a, b or 1350) for b in irs) - 0.5
        fudge = (n / 2 - (k + 1)) / 100
        out.append(round((n - (k + 1) - exp - fudge) * 200 / n))
    return out


def _session_kind(stype: str) -> str:
    s = (stype or "").lower()
    for k in ("race", "qualify", "practice", "warmup"):
        if k in s:
            return k
    return "testing" if "test" in s else ("race" if not s else "practice")


class PilotModule:
    id = "pilot"
    title = "Оверлей"

    def __init__(self):
        self.ctx: Context | None = None
        self._tap = None
        self._lock = threading.Lock()
        self._v: dict = {}                       # последний кадр отвода
        self._v_at = 0.0
        self._rows: list[dict] = []
        self._rows_at = 0.0
        self._field_lock = threading.Lock()
        self._answers: dict = {}             # oid -> (время, ответ) — см. TABLE_TTL
        self.gaps = GapClock()
        self.order = RaceOrder()
        self.pits = PitTracker()
        self.sectors = SectorTimer()
        self.flags = FlagWatch()             # выброс жёлтого и зелёного — волна по таблице, как в башне
        self._start_pos: dict[int, int] = {}
        self._lead_laps: dict = {}           # последние круги лидера — для оценки остатка гонки
        self._session_key = None
        # двигатель: зона, номер мигания, история воды для скорости нагрева
        self._eng = {"zone": 0, "flash": 0, "tone": "", "learned_now": None}
        self._demo_eng = {"water": 92.0, "oil": 104.0, "t": 0.0}
        # рация: текущая (или только что закончившаяся) реплика
        self.sides = RadarSides()
        self.probe = None                    # запись данных радара, пока окно включено
        self._voice = None                   # {id, car, radio, freq, start, end}
        self._voice_id = 0
        self._demo_voice_next = 0.0
        self.demo = None                     # своё демо: прошлая гонка (ModuleDemo)
        self.livery = LiveryColors()         # два цвета ливреи Trading Paints — для плашки номера
        self.setup_log = SetupLog()          # свои сектора и круги по сетапам
        self.history = None                  # история сетапов из записанной телеметрии (SetupHistory)
        self._cs_seen = (None, None)         # (объект CarSetup, его отпечаток)

    @property
    def sim(self):
        """Сим, который читает модуль: демо этого модуля, пока оно включено, иначе общий."""
        d = getattr(self, "demo", None)
        return (d.sim if d is not None and d.sim is not None else None) or self.ctx.sim

    # ---------- жизненный цикл ----------

    def start(self, ctx: Context) -> None:
        self.ctx = ctx
        self.demo = ModuleDemo(ctx, "pilot")
        for k, v in DEFAULTS.items():
            cur = ctx.config.setdefault(k, json.loads(json.dumps(v)))
            for kk, vv in v.items():
                if kk.startswith("cols"):
                    cur.setdefault(kk, {})
                    for c, on in vv.items():
                        cur[kk].setdefault(c, on)
                else:
                    cur.setdefault(kk, vv)
            for kk in [x for x in cur if x not in v]:
                cur.pop(kk)                  # настройки прошлых версий (пороги двигателя)
        ctx.config.setdefault("visible", {})
        ctx.config.setdefault("learned", {})
        # «Сетапы» — только эта сессия (история прошлых заездов убрана)
        ctx.config["setups"]["hist"] = 0
        ctx.config.pop("setups_hist_v2", None)
        self._setups_path = core_config.data_dir() / "pilot_setups.json"
        self._setups_saved = 0
        try:
            if self.setup_log.load(json.loads(self._setups_path.read_text("utf-8"))):
                self._setups_saved = self.setup_log.version
        except (OSError, ValueError):
            pass
        self._tap = ctx.sim.add_tap(CHANNELS, 60.0, self._frame)
        # поле считаем непрерывно: отрывы и питы копят историю, даже пока окна скрыты
        self._stop = threading.Event()
        threading.Thread(target=self._loop, daemon=True, name="pilot").start()
        if any(self._vis().values()) and ctx.app is not None:
            ctx.app.ensure_overlays()
        self._probe_sync()

    def _loop(self) -> None:
        while not self._stop.wait(0.25):
            try:
                if self.sim.snapshot().connected or getattr(self.sim, "demo", False):
                    self._field()
                if self.ctx.sim.snapshot().connected:
                    self._setup_check()
                if self.setup_log.version != self._setups_saved:
                    self._setups_saved = self.setup_log.version
                    tmp = self._setups_path.with_suffix(".tmp")
                    tmp.write_text(json.dumps(self.setup_log.dump(), ensure_ascii=False), "utf-8")
                    tmp.replace(self._setups_path)
                if getattr(self.sim, "demo", False):
                    self._demo_voice()
            except Exception:
                self.ctx.log.exception("оверлеи пилота: сбор поля")

    def _probe_sync(self) -> None:
        """Пока окно радара включено, пишем данные: на каком расстоянии сим говорит
        «рядом», реальная длина машины, запаздывание. Пишется только когда кто-то
        ближе 15 м; хранятся последние PROBE_KEEP файлов."""
        on = self.is_on("radar")
        try:
            if on and self.probe is None:
                root = core_config.data_dir() / "telemetry"
                self.probe = RadarProbe(self.ctx.sim, root, self.ctx.log)
                self.probe.start()
                files = sorted((root / "radar-probe").glob("*.jsonl"))
                for f in files[:-PROBE_KEEP]:
                    f.unlink(missing_ok=True)
            elif not on and self.probe is not None:
                self.probe.stop()
                self.probe = None
        except OSError:
            self.ctx.log.exception("оверлеи пилота: запись данных радара")

    def stop(self) -> None:
        if self.probe is not None:
            self.probe.stop()
            self.probe = None
        if getattr(self, "_stop", None):
            self._stop.set()
        if self.demo is not None:
            self.demo.stop()
        if self._tap is not None and self.ctx:
            self.ctx.sim.remove_tap(self._tap)
        self._tap = None

    def status(self) -> dict:
        return {"overlays": [{"id": i, "title": t, "visible": self.is_on(i)} for i, t in OVERLAYS],
                "demo": self.demo.status() if self.demo else {}}

    # ---------- отвод 60 Гц (поток опроса сима) ----------

    def _frame(self, v: dict) -> None:
        now = time.monotonic()
        with self._lock:
            self._v, self._v_at = v, now
        self._engine_watch(v)
        if not v.get("IsReplayPlaying"):
            self.setup_log.tick(v.get("LapDistPct"), v.get("SessionTime"), bool(v.get("IsOnTrack")),
                                bool(v.get("OnPitRoad")), int(v.get("PlayerCarMyIncidentCount") or 0))
        self._voice_watch(v.get("RadioTransmitCarIdx"), v.get("RadioTransmitRadioIdx"),
                          v.get("RadioTransmitFrequencyIdx"), now)

    # ---------- рация ----------

    def _voice_watch(self, car, radio, freq, now: float) -> None:
        """iRacing отдаёт одного говорящего: RadioTransmitCarIdx (-1 — тишина)."""
        car = int(car) if car is not None else -1
        cur = self._voice
        if cur and cur["end"] is None and cur["car"] == car:
            return
        if cur and cur["end"] is None:
            cur["end"] = now                       # замолчал или перебили
        if car >= 0:
            self._voice_id += 1
            self._voice = {"id": self._voice_id, "car": car, "start": now, "end": None,
                           "radio": int(radio or 0), "freq": int(freq or 0)}

    def _demo_voice(self) -> None:
        """Демо: пауза 1–3 с, потом говорит кто-то рядом или из лидеров 2–4,5 с."""
        import random
        now = time.monotonic()
        cur = self._voice
        if cur and cur["end"] is None and now >= cur.get("until", 0):
            self._voice_watch(-1, 0, 0, now)
            self._demo_voice_next = now + random.uniform(1, 3)
            return
        if (cur and cur["end"] is None) or now < self._demo_voice_next or not self._rows:
            return
        rows = self._rows
        pick = random.choice(rows[:3] + rows[3:9])
        self._voice_watch(pick["car_idx"], 0, random.choice([0, 0, 1]), now)
        self._voice["until"] = now + random.uniform(2, 4.5)

    def _voice_for(self, oid: str, rows: list[dict]) -> dict | None:
        """Реплика для окна: отмечает строку говорящего и решает про строку «Говорит»."""
        vc = self.ctx.config.get("voice") or {}
        if vc.get("where") not in (oid, "both"):
            return None
        cur = self._voice
        now = time.monotonic()
        if not cur or (cur["end"] is not None and now - cur["end"] > VOICE_KEEP_S):
            return None
        in_rows = False
        for c in rows:
            if c.get("idx") == cur["car"]:
                c["talk"] = True
                in_rows = True
        if not in_rows and not vc.get("strip"):
            return None
        d = getattr(self.sim, "_drivers", {}).get(cur["car"])
        if getattr(self.sim, "demo", False):
            freq = ("@DRIVERS", "@ALLTEAMS")[cur["freq"] % 2]
        else:
            freq = self.sim.radio_frequency(cur["radio"], cur["freq"])
        return {"id": cur["id"], "ended": cur["end"] is not None, "in_rows": in_rows,
                "num": getattr(d, "car_number", "") or "", "name": getattr(d, "name", "") or "",
                "freq": freq if vc.get("freq") else ""}

    def _vals(self) -> dict:
        with self._lock:
            return self._v if time.monotonic() - self._v_at < 2.0 else {}

    # ---------- что показывать ----------

    def _vis(self) -> dict:
        return self.ctx.config.setdefault("visible", {})

    def is_on(self, oid: str) -> bool:
        return bool(self._vis().get(oid, False))

    def _driving(self) -> bool:
        sim = self.sim
        if getattr(sim, "demo", False):
            return True
        snap = sim.snapshot()
        v = self._vals()
        return bool(snap.connected and v.get("IsOnTrack") and not v.get("IsReplayPlaying"))

    def _replay(self) -> bool:
        """Сим проигрывает повтор."""
        sim = self.sim
        return bool(sim.snapshot().connected and self._vals().get("IsReplayPlaying"))

    def _shown(self, oid: str) -> bool:
        """Окно включено, и вы в машине — или идёт повтор и окну разрешено в повторе."""
        if not self.is_on(oid):
            return False
        return self._driving() or (bool(self.ctx.config[oid].get("replay")) and self._replay())

    def set_visible(self, oid: str, on: bool) -> dict:
        if oid not in dict(OVERLAYS):
            return {"ok": False, "error": "unknown_overlay"}
        self._vis()[oid] = bool(on)
        self.ctx.save_config()
        self._probe_sync()
        if on and self.ctx.app is not None:
            self.ctx.app.ensure_overlays()
        return {"ok": True, **self.state()}

    def set_settings(self, data: dict) -> dict:
        oid = str(data.get("id") or "")
        if oid not in DEFAULTS:
            return {"ok": False, "error": "unknown_overlay"}
        cur = self.ctx.config[oid]
        if data.get("reset"):
            self.ctx.config[oid] = json.loads(json.dumps(DEFAULTS[oid]))
        for k, v in (data.get("set") or {}).items():
            if k.startswith("cols") and k in DEFAULTS[oid] and isinstance(v, dict):
                for c, on in v.items():
                    if c in DEFAULTS[oid][k]:
                        cur[k][c] = 1 if on else 0
            elif k in DEFAULTS[oid]:
                d = DEFAULTS[oid][k]
                try:
                    cur[k] = (str(v) if isinstance(d, str) else bool(v) if isinstance(d, bool)
                              else int(round(float(v))))
                except (TypeError, ValueError):
                    pass
        if data.get("forget_learned"):
            self.ctx.config["learned"] = {}
        self.ctx.save_config()
        return {"ok": True, **self.state()}

    def state(self) -> dict:
        return {"overlays": [{"id": i, "title": t, "visible": self.is_on(i)} for i, t in OVERLAYS],
                "settings": {k: self.ctx.config.get(k) for k in DEFAULTS},
                "columns": [list(c) for c in COLUMNS],
                "learned": self.ctx.config.get("learned") or {},
                "setups": self._setups_all()[0],
                "car": self._my_car_name()}

    def positions(self) -> dict:
        pos = self.ctx.config.get("positions") or {}
        return {"overlays": [i for i, _ in OVERLAYS],
                "positions": {i: pos.get(i) or DEFAULT_POS[i] for i, _ in OVERLAYS}}

    def set_position(self, data: dict) -> dict:
        oid = str(data.get("id", ""))
        if oid not in dict(OVERLAYS):
            return {"ok": False, "error": "unknown_overlay"}
        anchor = str(data.get("anchor") or "tl")
        self.ctx.config.setdefault("positions", {})[oid] = {"x": int(data.get("x", 0)),
                                                            "y": int(data.get("y", 0)), "anchor": anchor}
        self.ctx.config[oid]["anchor"] = anchor
        self.ctx.save_config()
        return {"ok": True}

    # ---------- поле: строки, отрывы, питы ----------

    def _me(self) -> int:
        sim = self.sim
        if getattr(sim, "demo", False):
            rows = self._rows
            me = sim.demo_me()
            if me >= 0:
                return me
            return rows[min(4, len(rows) - 1)]["car_idx"] if rows else -1
        v = self._vals()
        idx = v.get("PlayerCarIdx")
        return int(idx) if idx is not None else sim.snapshot().player_car_idx

    def _field(self) -> list[dict]:
        """Строки всего поля, не чаще пяти раз в секунду."""
        with self._field_lock:
            return self._field_locked()

    def _field_locked(self) -> list[dict]:
        now = time.monotonic()
        if now - self._rows_at < 0.2:
            return self._rows
        sim = self.sim
        snap = sim.snapshot()
        key = (snap.subsession_id, snap.session_num)
        live = sim.live_frame()
        # в повторе номер сессии — просматриваемой: перемотка не стирает накопленное
        if key != self._session_key and (live or self._session_key is None):
            self._session_key = key
            self.order.reset()
            self.gaps.reset()
            self.pits.reset()
            self.sectors.reset()
            self.flags.reset()
            self._start_pos.clear()
            self._lead_laps.clear()
        rows = sim.timing()
        drv = getattr(sim, "_drivers", {})
        paint = self._paint_folder()
        v = self._vals()
        flags = v.get("CarIdxSessionFlags") or []
        for r in rows:
            d = drv.get(r["car_idx"])
            r["irating"] = getattr(d, "irating", 0) or 0
            r["lic"] = getattr(d, "lic", "") or ""
            r["lic_color"] = getattr(d, "lic_color", "") or ""
            r["est_lap"] = getattr(d, "est_lap", 0.0) or 0.0
            r["liv"] = self.livery.get(paint, getattr(d, "car_path", ""), getattr(d, "user_id", 0) or 0,
                                       getattr(d, "team_id", 0) or 0) if paint else None
            f = int(flags[r["car_idx"]]) if r["car_idx"] < len(flags) and flags[r["car_idx"]] is not None else 0
            r["flag"] = ("black" if f & (F_BLACK | F_DQ) else "meat" if f & F_REPAIR
                         else "warn" if f & F_FURLED else "")
        if getattr(sim, "demo", False):
            self._demo_fill(rows)
        if self._kind() != "race":
            # практика и квалификация: место и лучший круг — из официальных
            # результатов (как на экране Results iRacing); нет их — по телеметрии
            res = sim.session_results() if hasattr(sim, "session_results") else {}
            for r in rows:
                x = res.get(r["car_idx"])
                if x and x["fastest"] > 0:
                    r["best_lap"] = round(x["fastest"], 3)
                    r["res_pos"] = x["pos"]
        if live:
            self.flags.update(course_flag(snap.session_flags), now)
            self.pits.update(rows, snap.session_time)
            try:
                self.sectors.update(sim.sector_starts(), rows, snap.session_time)
            except Exception:
                pass
            self.gaps.update(rows, now)
        if self._kind() == "race":
            # после клетчатого (и в повторе этой части) — официальный итог iRacing:
            # положение на трассе после финиша порядок не задаёт, а по нему считается и
            # прогноз iRating. Повтор посреди гонки по-прежнему идёт по кадру
            res = sim.session_results() if hasattr(sim, "session_results") else {}
            if res and int(snap.session_state or 0) >= 5:
                # сошедших и отключившихся сим в CarIdx* уже не отдаёт — добавляем их из
                # результатов: без них поле короче настоящего и прогноз iRating врёт
                have = {r["car_idx"] for r in rows}
                for idx in res:
                    d = drv.get(idx)
                    if idx in have or d is None or getattr(d, "spectator", False):
                        continue
                    rows.append({"car_idx": idx, "position": 0, "class_position": 0, "car_number": d.car_number,
                                 "name": d.name, "team": d.team, "car_class": d.car_class, "class_name": d.class_name,
                                 "car": d.car_name, "color": d.color, "cust_id": d.user_id, "gap": 0, "laps": 0, "lap": 0,
                                 "on_pit": False, "in_pit_stall": False, "not_in_world": True, "last_lap": 0, "best_lap": 0,
                                 "lap_dist": 0, "tyre": -1, "irating": getattr(d, "irating", 0) or 0,
                                 "lic": getattr(d, "lic", "") or "", "lic_color": getattr(d, "lic_color", "") or "",
                                 "est_lap": 0, "flag": "", "liv": None})
                for r in rows:
                    x = res.get(r["car_idx"])
                    r["position"] = x["pos"] if x and x["pos"] > 0 else 0
                rows = sorted(rows, key=lambda r: (r["position"] <= 0, r["position"]))
            else:
                rows = self.order.apply(rows, live, snap.session_state, self.gaps, now)
        for r in rows:
            r["pit_stops"] = self.pits.count(r["car_idx"])
        self._rows, self._rows_at = rows, now
        return rows

    def _paint_folder(self) -> str:
        """Папка paint из настроек RUTP — там ливреи Trading Paints."""
        try:
            ru = self.ctx.app.registry.get("rutp") if self.ctx.app is not None else None
            folder = str(((getattr(ru, "cfg", None) or {}).get("paint_folder")) or "")
            if not folder:
                from ..rutp.client import suggest_paint_folder
                folder = suggest_paint_folder() or ""   # та, что RUTP предлагает «Найдена — использовать»
            return folder
        except Exception:                    # noqa: BLE001
            return ""

    def _demo_fill(self, rows: list[dict]) -> None:
        """Демо: у машин нет рейтингов и лицензий — придумываем ровные."""
        for r in rows:
            i = r["car_idx"]
            if r.get("irating"):
                continue                     # прошлая гонка: рейтинг и лицензия настоящие
            r["irating"] = 1400 + (i * 397) % 3200
            r["lic"] = ("A", "B", "C")[i % 3] + f" {1.5 + (i * 0.37) % 3:.2f}"
            r["lic_color"] = ("#0153db", "#00c702", "#feec04")[i % 3]

    def _kind(self) -> str:
        try:
            return _session_kind(self.sim.session_type())
        except Exception:
            return "race"

    @staticmethod
    def _color(r: dict) -> str:
        return CLASS_COLOR.get(str(r.get("class_name", "")).upper()) or r.get("color") or "#5A6B85"

    @staticmethod
    def _fmt(t) -> str:
        if not t or t <= 0:
            return "—"
        m = int(t // 60)
        return f"{m}:{t - m * 60:06.3f}" if m else f"{t:.3f}"

    @staticmethod
    def _fmt_gap(g: float) -> str:
        if g >= 60:
            return f"+{int(g // 60)}:{g % 60:04.1f}"
        return f"+{g:.1f}"

    def _pit_last(self, idx: int) -> str:
        """Последний пит-стоп: круг заезда и секунды стоянки — «L23 14.2s»."""
        stops = self.pits.stops(idx)
        if not stops:
            return "—"
        s = stops[-1]
        return f"L{s.get('lap_in', s['lap'])} {s['duration']:.1f}s"

    def _sector_cells(self, r: dict, n: int, best: list) -> list:
        s = self.sectors.get(r["car_idx"], n)
        out = []
        for j in range(n):
            t = s["current"][j] if s["current"][j] is not None else s["last"][j]
            if t is None:
                out.append({"t": "—", "c": ""})
                continue
            c = ("purple" if best[j] is not None and t <= best[j] + 0.0005 else
                 "green" if s["best"][j] is not None and t <= s["best"][j] + 0.0005 else "")
            out.append({"t": f"{t:.1f}", "c": c})
        return out

    def _cells(self, r: dict, ctx: dict) -> dict:
        """Значения всех колонок строки — какие показывать, решает отрисовка."""
        bo = ctx["best_overall"]
        last_c = ("purple" if r["last_lap"] > 0 and bo and r["last_lap"] <= bo + 0.0005 else
                  "green" if r["last_lap"] > 0 and r["last_lap"] <= r["best_lap"] + 0.0005 else "")
        return {
            "pos": r["_pos"], "idx": r["car_idx"], "num": r["car_number"], "name": r["name"],
            # плашка номера — два цвета ливреи Trading Paints (градиент), иначе цвет класса или конструктора
            "color": r["liv"][0] if r.get("liv") else self._color(r),
            "color2": r["liv"][1] if r.get("liv") else "",
            "me": r["car_idx"] == ctx["me"], "pit": bool(r.get("on_pit") or r.get("in_pit_stall")),
            "flag": r.get("flag", ""), "ir": r.get("irating", 0),
            "dir": ctx["dir"].get(r["car_idx"], 0), "lic": r.get("lic", ""),
            "lic_color": r.get("lic_color", "") or "#8E8E98", "pits": r.get("pit_stops", 0),
            "pitlast": self._pit_last(r["car_idx"]),
            "sec": self._sector_cells(r, ctx["nsec"], ctx["sec_best"]) if ctx["nsec"] else [],
            "last": self._fmt(r["last_lap"]), "last_c": last_c,
            "best": self._fmt(r["best_lap"]),
            "ld": self._lap_delta(r, ctx),
            "best_c": "purple" if r["best_lap"] > 0 and bo and r["best_lap"] <= bo + 0.0005 else "",
        }

    @staticmethod
    def _lap_delta(r: dict, ctx: dict) -> float | None:
        """Круг пилота минус мой: плюс — я быстрее (зелёное), минус — медленнее (красное)."""
        mine, his = ctx.get("my_lap") or 0, r.get(ctx.get("lap_key") or "last_lap") or 0
        if r["car_idx"] == ctx["me"] or mine <= 0 or his <= 0:
            return None
        return round(his - mine, 3)

    def _ctx(self, rows: list[dict], me: int, by_class: bool) -> dict:
        try:
            nsec = len(self.sim.sector_starts())
        except Exception:
            nsec = 0
        # прогноз iRating — как у iRacing (сверено с гонкой 89874964: 24 из 24 в пределах
        # ±1 очка): всегда внутри своего класса, даже в общем зачёте; в поле — все, кто был
        # в гонке, включая сошедших и отключившихся (сим их в CarIdx* уже не отдаёт —
        # считаем их позади всех, кто в таблице)
        dir_ = {}
        groups = {}
        for r in rows:
            groups.setdefault(r.get("car_class"), []).append(r)
        present = {r["car_idx"] for r in rows}
        gone = {}
        if self._kind() == "race":
            for idx, d in (getattr(self.sim, "_drivers", {}) or {}).items():
                if idx not in present and not getattr(d, "spectator", False):
                    gone.setdefault(getattr(d, "car_class", None), []).append(getattr(d, "irating", 0) or 0)
        for cls, g in groups.items():
            for r, d in zip(g, ir_delta([x.get("irating", 0) for x in g] + gone.get(cls, []))):
                dir_[r["car_idx"]] = d
        bests = [r["best_lap"] for r in rows if r["best_lap"] > 0]
        mine = next((r for r in rows if r["car_idx"] == me), None)
        # Δ круга ко мне: в гонке — последний круг, в практике и квале — лучший
        lap_key = "last_lap" if self._kind() == "race" else "best_lap"
        return {"me": me, "dir": dir_, "best_overall": min(bests) if bests else 0,
                "lap_key": lap_key, "my_lap": (mine or {}).get(lap_key) or 0,
                "nsec": nsec, "sec_best": self.sectors.best_overall(nsec) if nsec else []}

    # ---------- Standings ----------

    def _ordered(self, rows: list[dict], kind: str) -> list[dict]:
        rows = [dict(r) for r in rows]
        if kind == "race":
            rows = [r for r in rows if r["position"] > 0] + [r for r in rows if r["position"] <= 0]
        else:
            # официальное место, если есть; затем по лучшему кругу; без времени — в конец
            timed = sorted((r for r in rows if r["best_lap"] > 0),
                           key=lambda r: (r.get("res_pos") or 999, r["best_lap"]))
            rows = timed + [r for r in rows if not r["best_lap"] > 0]
        return rows

    def standings(self) -> dict:
        cfg = self.ctx.config["standings"]
        rows = self._field()
        sim = self.sim
        snap = sim.snapshot()
        kind = self._kind()
        me = self._me()
        live = sim.live_frame()
        by_class = bool(cfg.get("by_class"))
        rows = self._ordered(rows, kind)
        # старт — для ▲▼: решётка квалификации, иначе первое увиденное место
        if kind == "race":
            if not self._start_pos:
                grid = {g["car_idx"]: g["position"] for g in (sim.qualifying_grid() or [])}
                self._start_pos = grid or {r["car_idx"]: k for k, r in enumerate(rows, start=1)}
        ctx = self._ctx(rows, me, by_class)
        now = time.monotonic()
        groups: list[tuple[str, list[dict]]] = []
        if by_class:
            seen = {}
            for r in rows:
                seen.setdefault(r["class_name"], []).append(r)
            groups = sorted(seen.items(), key=lambda kv: min(r["position"] or 999 for r in kv[1]))
        else:
            groups = [("", rows)]
        blocks = []
        for cls, lst in groups:
            lead = lst[0] if lst else None
            pole = lead["best_lap"] if lead and lead["best_lap"] > 0 else 0
            mine = next((k for k, r in enumerate(lst) if r["car_idx"] == me), None)
            top, around = int(cfg.get("top", 3)), int(cfg.get("around", 4))
            k0 = mine if mine is not None else top
            show = set(range(min(top, len(lst))))
            show |= set(range(max(0, k0 - around), min(len(lst), k0 + around + 1)))
            out, prev = [], -1
            for k, r in enumerate(lst):
                r["_pos"] = k + 1
                if k not in show:
                    continue
                c = self._cells(r, ctx)
                c["sep"] = prev >= 0 and k > prev + 1
                prev = k
                if kind == "race":
                    c["chg"] = (self._start_pos.get(r["car_idx"], k + 1) - (r["position"] or k + 1)) \
                        if r["position"] > 0 else 0
                    if r is lead:
                        c["gap"] = "LEADER" if not by_class or lst is rows else "CL LEADER"
                    elif not live:
                        # повтор: своей истории по ленте нет — круги и отрыв
                        # из кадра повтора (CarIdxF2Time сима)
                        down = int((lead["lap"] + lead["lap_dist"]) - (r["lap"] + r["lap_dist"]))
                        g = max(0.0, (r.get("gap") or 0) - (lead.get("gap") or 0))
                        c["gap"] = f"+{down} L" if down >= 1 else self._fmt_gap(g) if g > 0 else "—"
                    else:
                        pd, d = self.gaps.pos.get(lead["car_idx"]), self.gaps.pos.get(r["car_idx"])
                        down = int(pd - d) if pd is not None and d is not None else 0
                        if down >= 1:
                            c["gap"] = f"+{down} L"
                        else:
                            g = self.gaps.behind(lead["car_idx"], r["car_idx"], now)
                            c["gap"] = self._fmt_gap(g) if g is not None else "—"
                else:
                    c["chg"] = 0
                    c["gap"] = (("—" if r["best_lap"] <= 0 else self._fmt(r["best_lap"])) if r is lead
                                else "—" if r["best_lap"] <= 0 or not pole else f"+{r['best_lap'] - pole:.3f}")
                out.append(c)
            blocks.append({"title": f"{cls} · {len(lst)}" if by_class else "",
                           "color": self._color(lst[0]) if by_class and lst else "", "rows": out})
        head = self._head(rows, inc_on=bool(cfg.get("inc", 1)))
        if kind != "race":
            cfg = {**cfg, "cols": cfg.get("cols_q") or cfg["cols"]}   # практика и квала — свои колонки
        return {"visible": self._shown("standings"), "cfg": cfg, **head,
                "gap_head": "Gap",
                "blocks": blocks,
                "flag_event": self._flag_event(cfg), "flag_tone": self._flag_tone(),
                "voice": self._voice_for("standings", [c for b in blocks for c in b["rows"]])}

    def _head(self, rows: list[dict], inc_on: bool = True) -> dict:
        """Шапка таблиц: сессия, трасса, SOF, температуры, инциденты, круг, остаток.
        Одна для Standings и Relative — цифры в обоих окнах одинаковые."""
        sim = self.sim
        snap = sim.snapshot()
        kind = self._kind()
        me = self._me()
        live = sim.live_frame()
        name, col = SESSION.get(kind, SESSION["race"])
        v = self._vals()
        remain = v.get("SessionTimeRemain")
        lead = rows[0] if rows else None
        laps_total = 0
        try:
            for s in (sim.session_yaml()["SessionInfo"].get("Sessions") or []):
                if int(s.get("SessionNum", -1)) == snap.session_num:
                    laps_total = int(str(s.get("SessionLaps") or "0").split()[0])
        except (ValueError, TypeError, KeyError, IndexError):
            laps_total = 0
        if not laps_total and getattr(sim, "demo", False):
            laps_total = int(getattr(getattr(sim, "_demo", None), "laps_total", 0) or 0)
        irs = [r.get("irating", 0) for r in rows if r.get("irating")]
        inc = self._inc(me) if inc_on else None
        grid = self._grid_count(snap) if kind == "race" else None
        remain = remain if isinstance(remain, (int, float)) and 0 <= remain < 360000 else None
        # после клетчатого (SessionState 5–6) остаток не считаем; в повторе после финиша
        # iRacing отдаёт круг −1 — его не показываем
        finished = int(snap.session_state or 0) >= 5
        est = self._estimate(lead, laps_total, remain) if kind == "race" and not finished else None
        lap_now = lead["lap"] if lead and (lead.get("lap") or 0) >= 1 else 0
        if kind == "race" and finished:
            # после клетчатого — итог: кругов у победителя по официальным результатам
            res = sim.session_results() if hasattr(sim, "session_results") else {}
            won = max((x.get("laps", 0) for x in res.values()), default=0)
            lap_now = won or laps_total or 0
        w = sim.weather() or {}
        return {"inc": inc, "grid": grid, "replay": not live,
                "track": track_short(snap.track), "sof": _sof(irs),
                "session": name if live else f"Replay · {name}", "session_color": col, "race": kind == "race",
                "lap": lap_now, "laps_total": laps_total, "finished": kind == "race" and finished,
                "remain": remain, "est": est, "track_temp": w.get("track_temp"), "air_temp": w.get("air")}

    def _estimate(self, lead: dict | None, laps_total: int, remain) -> dict | None:
        """Сколько примерно осталось: гонка на круги — время по темпу лидера,
        гонка на время — круги. Темп — медиана последних 5 кругов лидера."""
        if not lead or (lead.get("laps") or 0) < 0:
            return None
        h = self._lead_laps
        if lead["laps"] != h.get("n"):
            h["n"] = lead["laps"]
            if lead.get("last_lap", 0) > 0:
                h["t"] = (h.get("t") or [])[-4:] + [lead["last_lap"]]
        ts = sorted(h.get("t") or [])
        pace = ts[len(ts) // 2] if ts else lead.get("best_lap", 0)
        if not pace or pace <= 0:
            return None
        if laps_total and remain is None:
            left = max(0.0, laps_total - lead["laps"] - lead["lap_dist"])
            return {"time": left * pace}
        if remain is not None and not laps_total:
            return {"laps": remain / pace}
        return None

    def _grid_count(self, snap) -> dict | None:
        """До старта гонки: сколько пилотов уже в машине (на гриде или в боксах)
        из всех заявленных. SessionState 1–3 — «садитесь в машину», прогрев,
        прогревочные круги; с зелёного флага (4) не показываем."""
        if not 1 <= int(snap.session_state or 0) <= 3:
            return None
        sim = self.sim
        drv = [i for i, d in (getattr(sim, "_drivers", {}) or {}).items() if not getattr(d, "spectator", False)]
        if not drv:
            return None
        surf = self._vals().get("CarIdxTrackSurface") or []
        n = sum(1 for i in drv if i < len(surf) and surf[i] is not None and surf[i] >= 0)
        return {"n": n, "total": len(drv)}

    def _inc(self, me: int) -> dict | None:
        """Свои инциденты и лимит дисквалификации сессии (WeekendOptions.IncidentLimit).
        Порог штрафа iRacing в данных сессии не публикует; если появятся поля с
        «Incident» в названии — пишем их в лог один раз, чтобы добавить."""
        sim = self.sim
        if getattr(sim, "demo", False):
            n = (sim.driver_incidents() or {}).get(me)
        else:
            n = self._vals().get("PlayerCarMyIncidentCount")
        if n is None:
            return None
        opts = ((sim.session_yaml().get("WeekendInfo") or {}).get("WeekendOptions") or {})
        other = {k: v for k, v in opts.items() if "incident" in k.lower() and k != "IncidentLimit"}
        if other and not getattr(self, "_inc_logged", False):
            self._inc_logged = True
            self.ctx.log.info("оверлеи пилота: поля инцидентов сессии %s", other)
        try:
            limit = int(str(opts.get("IncidentLimit") or "").split()[0])
        except (ValueError, IndexError):
            limit = 0                        # unlimited
        return {"n": int(n), "limit": limit if limit > 0 else None}

    # ---------- Relative ----------

    def relative(self) -> dict:
        cfg = self.ctx.config["relative"]
        rows = self._field()
        me = self._me()
        kind = self._kind()
        ordered = self._ordered(rows, kind)
        for k, r in enumerate(ordered, start=1):
            r["_pos"] = k
        ctx = self._ctx(ordered, me, False)
        mine = next((r for r in ordered if r["car_idx"] == me), None)
        out = []
        if mine is not None:
            v = self._vals()
            est = v.get("CarIdxEstTime") or []
            lap_t = mine["best_lap"] if mine["best_lap"] > 0 else (mine.get("est_lap") or 0)
            my_d = mine["lap"] + mine["lap_dist"]

            def rel_s(r):
                """Секунды по трассе: «−» — впереди, «+» — сзади."""
                i, j = r["car_idx"], me
                if lap_t and i < len(est) and j < len(est) and est[i] is not None and est[j] is not None:
                    dt = float(est[i]) - float(est[j])
                    dt = (dt + lap_t / 2) % lap_t - lap_t / 2
                    return -dt
                return -_wrap(r["lap_dist"] - mine["lap_dist"]) * (lap_t or 100)

            near = []
            for r in ordered:
                if r is mine or r.get("not_in_world") or r.get("in_pit_stall"):
                    continue
                near.append((rel_s(r), r))
            pc = self._pace_row(v)               # пейс-кар на трассе — всегда
            if pc is not None:
                # оценку времени (CarIdxEstTime) сим для пейс-кара может не вести —
                # секунды по доле круга и времени своего круга
                near.append((-_wrap(pc["lap_dist"] - mine["lap_dist"]) * (lap_t or 100), pc))
            ahead = sorted((x for x in near if x[0] < 0), key=lambda x: -x[0])[:int(cfg.get("around", 3))]
            behind = sorted((x for x in near if x[0] >= 0), key=lambda x: x[0])[:int(cfg.get("around", 3))]
            for s, r in list(reversed(ahead)) + [(0.0, mine)] + behind:
                if r.get("pace"):
                    out.append({"pace": True, "pos": "", "idx": r["car_idx"], "num": "PC", "name": "Pace Car",
                                "color": "#E0A800", "color2": "", "me": False, "pit": r.get("on_pit", False),
                                "flag": "", "tone": "pace", "gap": f"{'-' if s < 0 else '+'}{abs(s):.1f}"})
                    continue
                c = self._cells(r, ctx)
                d = r["lap"] + r["lap_dist"]
                lap_diff = round((d - my_d) - _wrap(r["lap_dist"] - mine["lap_dist"]))
                c["tone"] = "" if r is mine else "ahead" if lap_diff > 0 else "behind" if lap_diff < 0 else ""
                c["gap"] = "" if r is mine else f"{'-' if s < 0 else '+'}{abs(s):.1f}"
                out.append(c)
        # шапка — та же, что у Standings: сессия, трасса, SOF, температуры, инциденты, круг, остаток
        head = self._head(self._ordered(self._field(), kind), inc_on=True)
        return {"visible": self._shown("relative"), "cfg": cfg, "rows": out, "head": head,
                "last": self._fmt(mine["last_lap"]) if mine else "—",
                "flag_event": self._flag_event(cfg), "flag_tone": self._flag_tone(),
                "voice": self._voice_for("relative", out)}

    def _flag_tone(self) -> str:
        """«yellow», пока действует полный жёлтый, — кромка таблиц жёлтая."""
        return "yellow" if self.flags.state(time.monotonic()).get("flag_tone") == "yellow" else ""

    def _flag_event(self, cfg: dict) -> dict | None:
        """Выброс флага для волны по таблице: {id, tone, age} — если волна включена."""
        if not cfg.get("flag_wave"):
            return None
        return self.flags.state(time.monotonic()).get("flag_event")

    def _pace_row(self, v: dict) -> dict | None:
        """Пейс-кар как строка Relative: только пока он на трассе или въезжает
        на пит-лейн (CarIdxTrackSurface 2–3); в боксах и вне мира — нет."""
        sim = self.sim
        idx = sim.pace_car_idx() if hasattr(sim, "pace_car_idx") else -1
        if idx < 0:
            return None
        pct, surf = v.get("CarIdxLapDistPct") or [], v.get("CarIdxTrackSurface") or []
        if idx >= len(pct) or idx >= len(surf) or pct[idx] is None or surf[idx] is None:
            return None
        p, sf = float(pct[idx]), int(surf[idx])
        if p < 0 or sf not in (2, ON_TRACK):
            return None
        return {"car_idx": idx, "lap": 0, "lap_dist": p, "pace": True, "on_pit": sf == 2}

    # ---------- Радар ----------

    def radar(self) -> dict:
        """Кто рядом: машины, чей корпус перекрывает ваш по длине, со стороной и
        дорожкой. Плашка в окне — длина вашей машины, оранжевое — перекрытие."""
        cfg = self.ctx.config["radar"]
        sim = self.sim
        Lc = max(1.0, float(cfg.get("car_cm", 500)) / 100)
        out = {"visible": self.is_on("radar") and self._driving(), "cfg": cfg, "car_m": Lc, "cars": []}
        L = sim.track_length_m() or ((sim.weather() or {}).get("length_km") or 0) * 1000
        if getattr(sim, "demo", False):
            # в прошлой гонке нет места машины поперёк трассы — радар в демо
            # показывает сценарий мокапа: заход, тривайды, соседний поезд
            out["cars"] = radar_script(time.monotonic(), Lc)
            out["demo"] = True
        else:
            v = self._vals()
            clr = v.get("CarLeftRight")
            pct, surf, me = v.get("CarIdxLapDistPct") or [], v.get("CarIdxTrackSurface") or [], v.get("PlayerCarIdx")
            if clr is not None and me is not None and L and me < len(pct):
                near = []
                for i, x in enumerate(pct):
                    if i == me or x is None or x < 0 or (i < len(surf) and surf[i] != ON_TRACK):
                        continue
                    dm = _wrap(x - pct[me]) * L
                    if abs(dm) < Lc:
                        near.append((i, dm))
                out["cars"] = self.sides.update(int(clr), near, Lc)
        # в демо окно не прячется между вариантами — гаснут только полосы
        out["visible"] = out["visible"] and (bool(out["cars"]) or bool(cfg.get("test")) or bool(out.get("demo")))
        return out

    # ---------- Ветер ----------

    def wind(self) -> dict:
        cfg = self.ctx.config["wind"]
        sim = self.sim
        v = self._vals()
        if getattr(sim, "demo", False):
            w = sim.weather() or {}
            rows = sim.timing()                  # на этот кадр, как у радара
            mine = next((r for r in rows if r["car_idx"] == self._me()), None)
            f = mine["lap_dist"] if mine else 0
            yaw = (f * 2 * math.pi + math.sin(f * 31) * 0.7) % (2 * math.pi)
            speed, frm = w.get("wind_speed") or 0, w.get("wind_dir") or 0
        else:
            speed, frm, yaw = v.get("WindVel"), v.get("WindDir"), v.get("YawNorth")
        out = {"visible": self.is_on("wind") and self._driving(), "cfg": cfg, "ok": False}
        if speed is None or frm is None or yaw is None:
            return out
        # угол «откуда дует» относительно носа машины: 0 — встречный, 180 — попутный
        rel = math.degrees(float(frm) - float(yaw)) % 360
        kmh = float(speed) * 3.6
        head, cross = math.cos(math.radians(rel)) * kmh, math.sin(math.radians(rel)) * kmh
        kind = (("headwind" if head > 0 else "tailwind") if abs(head) > abs(cross)
                else ("from right" if cross > 0 else "from left"))
        out.update(ok=True, rel=round(rel, 1), kmh=round(kmh, 1), kind=kind,
                   head=round(head, 1), cross=round(abs(cross), 1))
        return out

    # ---------- Двигатель ----------

    def _my_car_name(self) -> str:
        sim = self.sim
        me = self._me()
        d = getattr(sim, "_drivers", {}).get(me)
        return getattr(d, "car_name", "") or ""

    def _marks(self) -> dict:
        """При какой температуре у этой машины уже загоралась лампа сима — отметка на шкале."""
        got = (self.ctx.config.get("learned") or {}).get(self._my_car_name()) or {}
        return {"lamp_water": got.get("water"), "lamp_oil": got.get("oil")}

    def _engine_values(self) -> dict:
        if getattr(self.sim, "demo", False):
            return self._demo_engine()
        v = self._vals()
        return {"water": v.get("WaterTemp"), "oil": v.get("OilTemp"), "press": v.get("OilPress"),
                "rpm": v.get("RPM"), "warn": int(v.get("EngineWarnings") or 0)}

    def _demo_engine(self) -> dict:
        """Демо: вода греется в мешке за машиной ближе 20 м, на чистом воздухе остывает."""
        e = self._demo_eng
        now = time.monotonic()
        dt = min(1.0, now - e["t"]) if e["t"] else 0.0
        e["t"] = now
        rows = self._rows or []
        me = self._me()
        mine = next((r for r in rows if r["car_idx"] == me), None)
        draft = 0.0
        L = ((self.sim.weather() or {}).get("length_km") or 6) * 1000
        if mine:
            ahead = [_wrap(r["lap_dist"] - mine["lap_dist"]) * L for r in rows if r is not mine]
            ahead = [m for m in ahead if 0 < m < 20]
            draft = max(0.0, 1 - min(ahead) / 20) if ahead else 0.0
        e["water"] += ((92 + draft * 38) - e["water"]) * dt / (6 if draft else 14)
        e["oil"] += ((104 + draft * 22) - e["oil"]) * dt / 25
        return {"water": e["water"], "oil": e["oil"], "press": 4.6 - max(0, e["oil"] - 115) * .06,
                "rpm": 5200 + 1800 * math.sin(now * 1.3) * math.sin(now * .37),
                "warn": W_WATER if e["water"] >= 118 else 0}

    def _engine_watch(self, v: dict) -> None:
        """Из отвода: запомнить температуру, при которой загорелась лампа сима."""
        warn = int(v.get("EngineWarnings") or 0)
        if not warn & (W_WATER | W_OIL_TEMP) or not v.get("IsOnTrack"):
            self._eng["learned_now"] = None
            return
        if self._eng["learned_now"]:
            return
        car = self._my_car_name()
        if not car:
            return
        got = self.ctx.config.setdefault("learned", {}).setdefault(car, {})
        changed = False
        for bit, key, chan in ((W_WATER, "water", "WaterTemp"), (W_OIL_TEMP, "oil", "OilTemp")):
            t = v.get(chan)
            if warn & bit and t and (not got.get(key) or t < got[key] - 0.5):
                got[key] = round(float(t), 1)
                changed = True
        self._eng["learned_now"] = True
        if changed:
            self.ctx.log.info("двигатель: лампа сима у %s — %s", car, got)
            threading.Thread(target=self.ctx.save_config, daemon=True).start()

    def engine(self) -> dict:
        cfg = self.ctx.config["engine"]
        e = self._engine_values()
        water, oil = e.get("water"), e.get("oil")
        out = {"visible": self.is_on("engine") and self._driving(), "cfg": cfg, "ok": water is not None,
               **self._marks()}
        if water is None:
            return out
        warn = e.get("warn") or 0
        ww, ow, pw = bool(warn & W_WATER), bool(warn & W_OIL_TEMP), bool(warn & W_OIL_PRESS)
        water, oil = float(water), float(oil or 0)
        # пороги машины известны — красное по ним (как цифра в плашке iRacing), а не по
        # флагу сима: тот у масла загорается раньше жёлтого
        lim = self._temp_limits()
        if "water" in lim:
            ww = lim["water"][1] is not None and water >= lim["water"][1]
        if "oil" in lim:
            ow = lim["oil"][1] is not None and oil >= lim["oil"][1]
        out["limits"] = {k: list(v) for k, v in lim.items()}
        # тревога — красная зона; мигаем один раз на вход в неё
        zone = 2 if (ww or ow or pw) else 0
        st = self._eng
        if zone and not st["zone"]:
            st["flash"] += 1
            st["tone"] = "red"
        st["zone"] = zone
        msg = ("Water and oil overheat" if ww and ow else "Water overheat" if ww else
               "Oil overheat" if ow else "Low oil pressure" if pw else "")
        out.update(water=round(water, 1), oil=round(oil, 1),
                   press=round(float(e["press"]), 2) if e.get("press") is not None else None,
                   warn_water=ww, warn_oil=ow, warn_press=pw, zone=zone,
                   rpm=round(float(e["rpm"]), -1) if e.get("rpm") is not None else None,
                   flash=st["flash"] if cfg.get("flash") else 0, tone=st["tone"], msg=msg,
                   sl=self._shift_lights())
        return out

    def _temp_limits(self) -> dict:
        """Пороги жёлтого и красного для своей машины (TEMP_LIMITS), если сняты."""
        d = getattr(self.sim, "_drivers", {}).get(self._me())
        return TEMP_LIMITS.get(str(getattr(d, "car_path", "") or "").lower(), {})

    def _shift_lights(self) -> dict:
        """Лампочки переключения этой машины, как на руле: первая, переключение,
        последняя, мигание, отсечка. Телеметрия (у некоторых машин — по передаче),
        иначе данные сессии (DriverInfo). Нет — пусто, окно считает от отсечки."""
        sim = self.sim
        if getattr(sim, "demo", False):
            return {"first": 5800, "shift": 6600, "last": 6850, "blink": 6950, "red": 7200}
        v = self._vals()
        di = (sim.session_yaml().get("DriverInfo") or {})

        def num(x):
            try:
                x = float(str(x).split()[0])
            except (ValueError, IndexError, TypeError):
                return 0.0
            return x if x > 0 else 0.0
        out = {}
        for k, tel, yml in (("first", "PlayerCarSLFirstRPM", "DriverCarSLFirstRPM"),
                            ("shift", "PlayerCarSLShiftRPM", "DriverCarSLShiftRPM"),
                            ("last", "PlayerCarSLLastRPM", "DriverCarSLLastRPM"),
                            ("blink", "PlayerCarSLBlinkRPM", "DriverCarSLBlinkRPM")):
            out[k] = num(v.get(tel)) or num(di.get(yml))
        out["red"] = num(di.get("DriverCarRedLine"))
        return {k: x for k, x in out.items() if x}

    # ---------- Сетапы ----------

    def _setup_check(self) -> None:
        """Какой сетап сейчас в машине — по содержимому CarSetup (сим, не демо)."""
        sim = self.ctx.sim
        cs = sim.car_setup()
        if cs is not self._cs_seen[0]:
            self._cs_seen = (cs, setup_hash(cs))
        di = sim.session_yaml().get("DriverInfo") or {}
        snap = sim.snapshot()
        d = getattr(sim, "_drivers", {}).get(snap.player_car_idx)
        key = (snap.track, getattr(d, "car_path", "") or "")
        try:
            self.setup_log.set_starts(sim.sector_starts())
        except Exception:                    # noqa: BLE001
            pass
        self.setup_log.check(key, self._cs_seen[1], str(di.get("DriverSetupName") or ""),
                             bool(int(di.get("DriverSetupIsModified") or 0)))

    def _setups_all(self) -> tuple[list[dict], int]:
        cfg = self.ctx.config["setups"]
        mode = "avg" if cfg.get("mode") == "avg" else "best"
        if getattr(self.sim, "demo", False):
            return demo_summary(mode)
        items, n = self.setup_log.summary(mode)
        hist = self._history(only_session=True)
        # эта сессия: сектора и круги — свои живые, остальное — из записи этой сессии
        for e in items:
            a = hist.get(e["id"]) or {}
            e.update(trk=a.get("trk"), fuel=a.get("fuel"), tyres=a.get("tyres"))
            if e.get("avg") is None:
                e["avg"] = a.get("avg")
            if not e.get("laps") and a.get("laps"):
                # своих кругов нет (программу перезапустили) — круги из записи этой сессии
                e.update(laps=a["laps"], lap=a.get("best") if mode != "avg" else a.get("avg"))
            if e["cur"] and e["tyres"]:
                self._live_pressures(e["tyres"])
        # промежуточные состояния гаража (щёлкали настройки, не выезжая) — не сетапы для
        # сравнения: в окне только проеханные (хоть один сектор или круг) и текущий
        items = [e for e in items if e["cur"] or e.get("laps") or any(v is not None for v in (e.get("s") or []))]
        # «изм. N» — по порядку среди оставшихся, без дыр
        seq: dict = {}
        for e in items:
            if e.get("edit"):
                seq[e["name"]] = seq.get(e["name"], 0) + 1
                e["edit"] = seq[e["name"]]
        return items, n

    def _history(self, only_session: bool) -> dict:
        """История сетапов этой машины на этой трассе из папки телеметрии."""
        try:
            if self.history is None:
                self.history = SetupHistory(core_config.data_dir() / "telemetry")
            sim = self.ctx.sim
            d = getattr(sim, "_drivers", {}).get(sim.snapshot().player_car_idx)
            car = getattr(d, "car_path", "") or ""
            track = str((sim.session_yaml().get("WeekendInfo") or {}).get("TrackName") or "")
            only = None
            if only_session:
                tm = self.ctx.app.module("telemetry") if self.ctx.app is not None else None
                sess = getattr(tm, "sess", None) or {}
                only = sess.get("dir")
                if not only:
                    return {}
            return self.history.get(car, track, only)
        except Exception:                        # noqa: BLE001
            self.ctx.log.exception("сетапы: история из телеметрии")
            return {}

    def _live_pressures(self, tyres: dict) -> None:
        """Горячее давление с последнего пит-стопа — из CarSetup сима (LastHotPressure),
        если в записи его ещё нет (оно там только после разбора .ibt)."""
        cs = self.ctx.sim.car_setup() or {}
        names = {"LF": "LeftFront", "RF": "RightFront", "LR": "LeftRear", "RR": "RightRear"}
        for c, t in tyres.items():
            if t.get("pres") is not None:
                continue
            v = ((cs.get("Tires") or {}).get(names[c]) or {}).get("LastHotPressure")
            try:
                t["pres"] = round(float(str(v).split()[0]))
            except (TypeError, ValueError, IndexError):
                pass

    def setups(self) -> dict:
        cfg = self.ctx.config["setups"]
        sim = self.sim
        snap = sim.snapshot()
        allx, n = self._setups_all()
        mx = int(cfg.get("max") or 0)
        pin = str(cfg.get("pin") or "")
        shown = allx
        if mx and len(allx) > mx:
            # самые свежие по времени (текущий — всегда среди них), закреплённый — всегда
            order = sorted(allx, key=lambda e: e["used"])
            pinned = next((e for e in allx if e["id"] == pin), None)
            pick = order[-mx:]
            if pinned is not None and pinned not in pick:
                pick = [pinned] + [e for e in order if e is not pinned][-(mx - 1):]
            keep = {e["id"] for e in pick}
            shown = [e for e in allx if e["id"] in keep]

        def best(vals):
            vals = [v for v in vals if v is not None]
            return min(vals) if len(vals) >= 1 else None
        many = len(allx) >= 2
        rows = []
        for k in range(n):
            ab = best([e["s"][k] for e in allx]) if many else None
            rb = best([e["s"][k] for e in shown]) if len(shown) >= 2 else None
            rows.append((ab, rb))
        lab = best([e["lap"] for e in allx])
        lrb = best([e["lap"] for e in shown]) if len(shown) >= 2 else None

        def cls(v, ab, rb):
            return "" if v is None else "p" if many and v == ab else "b" if v == rb else ""
        cols = []
        for e in shown:
            cols.append({**e, "pin": e["id"] == pin,
                         "cls": [cls(e["s"][k], *rows[k]) for k in range(n)],
                         "lap_cls": cls(e["lap"], lab if many else None, lrb),
                         "delta": (None if e["lap"] is None or lab is None
                                   else round(e["lap"] - lab, 3))})
        kind = self._kind()
        name, col = SESSION.get(kind, ("Session", ACC_DEFAULT))
        return {"visible": self._shown("setups"), "cfg": cfg, "ok": True,
                "session": name, "session_color": col, "track": track_short(snap.track),
                "car": self._my_car_name(), "count": len(allx), "best": lab, "n": n, "cols": cols}

    # ---------- HTTP ----------

    # готовый ответ таблиц живёт столько: окна спрашивают 10 раз в секунду
    TABLE_TTL = 0.1

    def data(self, oid: str) -> dict:
        fn = {"standings": self.standings, "relative": self.relative, "radar": self.radar,
              "wind": self.wind, "engine": self.engine, "setups": self.setups}.get(oid)
        if not fn:
            return {"ok": False, "error": "unknown_overlay"}
        if not self.sim.snapshot().connected and not getattr(self.sim, "demo", False):
            return {"visible": False, "cfg": self.ctx.config.get(oid, {})}
        if oid in ("standings", "relative"):
            now = time.monotonic()
            hit = self._answers.get(oid)
            if hit and now - hit[0] < self.TABLE_TTL:
                return hit[1]
            out = fn()
            self._answers[oid] = (now, out)
            return out
        return fn()

    def _set_demo(self, b: dict) -> dict:
        # поле демо копило историю не той гонки — начать заново
        self._session_key = None
        self._rows_at = 0.0
        return self.demo.set(bool(b.get("on")))

    def routes(self) -> dict:
        def local(fn):
            # окна пилота — только этому компьютеру
            def h(r):
                if r.client_address[0] != "127.0.0.1":
                    return {"ok": False, "error": "forbidden"}
                return fn(r)
            return h

        def q(r) -> dict:
            return {k: v[0] for k, v in parse_qs(urlparse(r.path).query).items()}

        def body(r) -> dict:
            n = int(r.headers.get("Content-Length") or 0)
            try:
                return json.loads(r.rfile.read(n).decode("utf-8")) if n else {}
            except Exception:                    # noqa: BLE001
                return {}

        def vis(r):
            b = body(r)
            return self.set_visible(str(b.get("id", "")), bool(b.get("visible")))

        return {
            "GET /api/pilot/state": local(lambda r: self.state()),
            "GET /api/pilot/data": local(lambda r: self.data(q(r).get("id", ""))),
            "GET /api/pilot/positions": local(lambda r: self.positions()),
            "POST /api/pilot/position": local(lambda r: self.set_position(body(r))),
            "POST /api/pilot/visibility": local(vis),
            "POST /api/pilot/settings": local(lambda r: self.set_settings(body(r))),
            "POST /api/pilot/demo": local(lambda r: self._set_demo(body(r))),
        }


def _sof(ratings: list[int]) -> int | None:
    """Сила поля — как у iRacing: логарифмическое среднее iRating."""
    rs = [r for r in ratings if r and r > 0]
    if not rs:
        return None
    br = 1600 / math.log(2)
    s = sum(math.exp(-r / br) for r in rs) / len(rs)
    return int(round(-br * math.log(s)))
