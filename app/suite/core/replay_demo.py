"""Демо по прошлой гонке: настоящий заезд из iRacing вместо выдуманного.

Сайт отдаёт ход гонки по кругам (`/api/desktop/race_replay`): у каждого
пилота — время пересечения линии на каждом круге, место, пит, инцидент.
Отсюда восстанавливается движение: внутри круга машина едет равномерно, на
круге с питом — обычным темпом до въезда, потом стоит в боксе. Получаются
настоящие отрывы, обгоны и пит-стопы, а не синтетика.

`ReplayRace` выдаёт то же, что `DemoRace`, и подставляется в отдельный
`SimClient` — у каждого модуля (трансляция, пилот) своё демо. Общий сим и
рейс-контрол демо не видят.

Гонка одна на всех и едет вместе с программой (`demo_race.json`); если
файла нет — запасной вариант, синтетическая `DemoRace`.
"""
from __future__ import annotations

import bisect
import json
import logging
import math
import pathlib
import threading
import time

from .demo import DEMO_CAMERAS, DEMO_COLORS, SF_CAUTION, SF_GREEN, SF_YELLOW, _class_id

log = logging.getLogger("irc.replay_demo")

LEAD_IN = 20.0          # секунд до первого пересечения линии — демо стартует с хода
LOOP_PAUSE = 20.0       # после финиша лидера — пауза и заново
PIT_AT = 0.94           # где на круге въезд на пит-лейн, доля круга
PIT_OUT = 0.08          # где выезд с пит-лейна на трассу, доля круга
FAC_STEP = 5.0          # шаг, с, с которым считается темп поля (жёлтые)


# цвета лицензий iRacing по букве
LIC_COLOR = {"R": "#fc0706", "D": "#ff8c00", "C": "#feec04", "B": "#00c702", "A": "#0153db", "P": "#828282"}


# папка машины в paint по названию — в данных прошлой гонки CarPath нет;
# нужна, чтобы демо брало цвета ливрей Trading Paints, которые уже есть на диске
CAR_PATHS = (("Next Gen Chevrolet", "stockcars chevycamarozl12022"),
             ("Next Gen Ford", "stockcars fordmustang2022"),
             ("Next Gen Toyota", "stockcars toyotacamry2022"))


def _car_path(car: str) -> str:
    return next((p for k, p in CAR_PATHS if k.lower() in str(car).lower()), "")


def _lic(name) -> str:
    """«Class A» / «Rookie» / «Pro» от сайта → буква, как в iRacing."""
    s = str(name or "").strip()
    if not s:
        return ""
    if s.lower().startswith("class "):
        return s[6:7].upper()
    return {"rookie": "R", "pro": "P", "pro/wc": "P"}.get(s.lower(), s[:1].upper())


def _pit_kinds(pit: list[int]) -> list[str]:
    """Отметки пита по кругам → что за круг. iRacing отмечает и въезд, и выезд:
    два круга подряд — один пит-стоп (стоянка в начале второго)."""
    out, i, n = [""] * len(pit), 0, len(pit)
    while i < n:
        if not pit[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and pit[j + 1]:
            j += 1
        if i == j:
            out[i] = "one"
        else:
            out[i], out[j] = "in", "out"
            for m in range(i + 1, j):
                out[m] = "mid"
        i = j + 1
    return out


class ReplayRace:
    """Проигрыватель гонки по кругам. Время идёт от часов, 1:1 с реальным."""

    def __init__(self, data: dict, me_cust: int | None = None) -> None:
        self.data = data
        self.speed = 1.0
        self.track = ((data.get("track") or {}).get("name") or "").strip()
        self.sub = int(data.get("subsession_id") or 0)
        self.cam = {"car_idx": 0, "group": DEMO_CAMERAS["TV1"], "camera": 1}
        self.cars: list[dict] = []
        laps = data.get("laps") or {}
        for d in data.get("drivers") or []:
            rows = sorted(laps.get(str(d.get("id"))) or [])
            if len(rows) < 2:
                continue
            idx = len(self.cars)
            times = [r[1] for r in rows]
            clean = sorted(r[5] for r in rows if r[5] > 0 and not r[3])
            pace = clean[len(clean) // 2] if clean else (times[-1] - times[0]) / max(1, len(rows) - 1)
            # инциденты у iRacing по кругам — только отметка; итог раскладываем по отмеченным кругам
            marked = [r[0] for r in rows if r[4]]
            total = int(d.get("inc") or 0)
            inc_at = {}
            for k, lap in enumerate(marked):
                inc_at[lap] = total // len(marked) + (1 if k < total % len(marked) else 0)
            self.cars.append({
                "idx": idx, "id": d.get("id"), "number": str(d.get("number") or idx + 1),
                "name": d.get("name") or "", "cls": d.get("cls") or "", "car": d.get("car") or "",
                "ir": int(d.get("ir") or 0), "lic": _lic(d.get("lic")),
                "color": DEMO_COLORS[idx % len(DEMO_COLORS)],
                "laps": [r[0] for r in rows], "times": times, "pos": [r[2] for r in rows],
                "pit": [r[3] for r in rows], "lap_time": [r[5] for r in rows],
                "inc_at": inc_at, "pace": pace, "kind": _pit_kinds([r[3] for r in rows]),
            })
        self.me = next((c["idx"] for c in self.cars if me_cust and c["id"] == me_cust), -1)
        if self.me >= 0:
            self.cam["car_idx"] = self.me
        t0 = min((c["times"][0] for c in self.cars), default=0.0)
        self.t_from = t0 - LEAD_IN
        lead = min(self.cars, key=lambda c: (-c["laps"][-1], c["times"][-1]), default=None)
        self.t_to = (lead["times"][-1] if lead else t0) + LOOP_PAUSE
        self._yellow = self._cautions()
        self.laps_total = max((c["laps"][-1] for c in self.cars), default=0)
        self.started = time.monotonic()

    def _cautions(self) -> list[tuple[float, float]]:
        """Темп поля по времени и жёлтые. Флагов в данных нет, но под пейс-каром
        медленно едет всё поле: медиана «круг / свой темп» больше 1.25 — жёлтый.
        Все разом на пите (так бывает под жёлтым) — тоже жёлтый."""
        out, cur, t = [], None, self.t_from
        self._fac_t, self._fac = [], []
        while t < self.t_to:
            ratios, pits = [], 0
            for c in self.cars:
                T = c["times"]
                k = bisect.bisect_right(T, t) - 1
                if k < 0 or k >= len(T) - 1:
                    continue
                if c["pit"][k + 1]:
                    pits += 1
                    continue
                ratios.append((T[k + 1] - T[k]) / c["pace"])
            ratios.sort()
            fac = ratios[len(ratios) // 2] if ratios else 1.0
            y = (len(ratios) >= 3 and fac > 1.25) or (pits >= 3 and pits > 0.6 * (pits + len(ratios)))
            if y and fac < 1.25:
                fac = 1.6
            self._fac_t.append(t)
            self._fac.append(max(1.0, min(2.5, fac)))
            if y and cur is None:
                cur = t
            elif not y and cur is not None:
                out.append((cur, t))
                cur = None
            t += FAC_STEP
        if cur is not None:
            out.append((cur, self.t_to))
        return out

    def _pace_at(self, c: dict, t: float) -> float:
        """Темп машины в этот момент гонки: под жёлтым — медленнее."""
        k = bisect.bisect_right(self._fac_t, t) - 1
        return c["pace"] * (self._fac[k] if 0 <= k < len(self._fac) else 1.0)

    # ---------- время ----------

    def _span(self) -> float:
        return max(60.0, self.t_to - self.t_from)

    def now(self) -> float:
        """Текущее время гонки, с; по концу — заново."""
        return self.t_from + ((time.monotonic() - self.started) * self.speed) % self._span()

    def loop(self) -> int:
        """Который раз гонка проигрывается с начала."""
        return int((time.monotonic() - self.started) * self.speed // self._span())

    def session_time(self) -> float:
        return self.now()

    def tick(self) -> None:
        pass

    # ---------- где машина ----------

    def _state(self, c: dict, t: float) -> dict:
        """Дистанция (кругов, дробная) и пит в момент t."""
        T, L = c["times"], c["laps"]
        k = bisect.bisect_right(T, t) - 1
        if k < 0:                                    # до первого пересечения линии: подъезжает
            return {"dist": L[0] - (T[0] - t) / c["pace"], "pit": False, "stall": False, "gone": False}
        if k >= len(T) - 1:                          # последний круг закрыт
            gone = t - T[-1] > c["pace"] * 0.5       # финишировал или сошёл — со трассы убираем
            return {"dist": float(L[-1]) + min(0.5, (t - T[-1]) / c["pace"]) * (0 if gone else 1),
                    "pit": False, "stall": False, "gone": gone}
        a, b = T[k], T[k + 1]
        lap, dur, el = L[k], b - a, t - a
        kind = c["kind"][k + 1]
        # темп этого места гонки (под жёлтым медленнее): прошлый круг без пита
        P = self._pace_at(c, a)
        if kind == "in":
            # круг въезда: до въезда — по трассе, дальше пит-лейн до линии
            run = min(dur, PIT_AT * P)
            if el < run:
                return {"dist": lap + PIT_AT * el / run, "pit": False, "stall": False, "gone": False}
            f = PIT_AT + (1 - PIT_AT) * (el - run) / max(0.1, dur - run)
            return {"dist": lap + min(0.9999, f), "pit": True, "stall": False, "gone": False}
        if kind == "out":
            # круг выезда: сначала стоянка в боксе, потом пит-лейн и трасса
            lane = PIT_OUT * P * 1.6
            stop = max(0.0, dur - lane - (1 - PIT_OUT) * P)
            if el < stop:
                return {"dist": lap + 0.005, "pit": True, "stall": el > 2.0, "gone": False}
            if el < stop + lane:
                return {"dist": lap + 0.005 + (PIT_OUT - 0.005) * (el - stop) / lane, "pit": True,
                        "stall": False, "gone": False}
            rest = max(0.1, dur - stop - lane)
            return {"dist": lap + PIT_OUT + (1 - PIT_OUT) * min(1.0, (el - stop - lane) / rest),
                    "pit": False, "stall": False, "gone": False}
        if kind == "mid":
            return {"dist": lap + el / dur, "pit": True, "stall": False, "gone": False}
        if kind == "one" and dur > c["pace"] * 1.15:
            # отмечен один круг: проезд по пит-лейну со стоянкой в его конце
            run = PIT_AT * P
            if el < run:
                return {"dist": lap + el / P, "pit": False, "stall": False, "gone": False}
            rest = dur - run
            tail = (1 - PIT_AT) * P * 1.6
            stop = max(0.0, rest - tail)
            if el - run < stop:
                f = PIT_AT + 0.02 * min(1.0, (el - run) / 4.0)
                return {"dist": lap + f, "pit": True, "stall": el - run > 4.0, "gone": False}
            f = PIT_AT + 0.02 + (1 - PIT_AT - 0.02) * (el - run - stop) / max(0.1, tail)
            return {"dist": lap + min(0.9999, f), "pit": True, "stall": False, "gone": False}
        return {"dist": lap + el / dur, "pit": False, "stall": False, "gone": False}

    def _when(self, c: dict, dist: float) -> float | None:
        """Когда машина c была на дистанции dist (для отрыва от лидера)."""
        T, L = c["times"], c["laps"]
        lap = int(math.floor(dist))
        k = bisect.bisect_left(L, lap)
        if k >= len(L) or L[k] != lap or k + 1 >= len(T):
            return None
        return T[k] + (T[k + 1] - T[k]) * (dist - lap)

    # ---------- то, что читают модули ----------

    def timing(self) -> list[dict]:
        t = self.now()
        st = [(c, self._state(c, t)) for c in self.cars]
        st.sort(key=lambda x: (x[1]["gone"], -x[1]["dist"]))
        leader = st[0][1]["dist"] if st else 0
        lead_car = st[0][0] if st else None
        rows, seen_class = [], {}
        for pos, (c, s) in enumerate(st, start=1):
            seen_class[c["cls"]] = seen_class.get(c["cls"], 0) + 1
            done = bisect.bisect_right(c["times"], t)          # сколько пересечений линии уже было
            laps_done = c["laps"][done - 1] if done else 0
            last = c["lap_time"][done - 1] if done else 0.0
            best = min((x for x in c["lap_time"][:done] if x > 0), default=0.0)
            when = self._when(lead_car, s["dist"]) if lead_car is not None else None
            gap = (t - when) if when is not None else (leader - s["dist"]) * c["pace"]
            rows.append({
                "car_idx": c["idx"], "position": pos, "class_position": seen_class[c["cls"]],
                "car_number": c["number"], "name": c["name"], "team": "",
                "car_class": _class_id(c["cls"]), "class_name": c["cls"], "car": c["car"],
                "color": c["color"], "cust_id": c["id"] or 0,
                "gap": round(max(0.0, gap), 3), "laps": max(0, laps_done),
                "lap": max(1, int(s["dist"]) + 1), "on_pit": s["pit"], "in_pit_stall": s["stall"],
                "not_in_world": s["gone"], "last_lap": last, "best_lap": best,
                "lap_dist": round(s["dist"] % 1.0, 4), "tyre": 0,
            })
        return rows

    def qualifying_grid(self) -> list[dict]:
        order = sorted(self.cars, key=lambda c: (c["pos"][0] or 999, c["idx"]))
        return [{"position": p, "car_idx": c["idx"], "car_number": c["number"], "name": c["name"],
                 "team": "", "class_name": c["cls"], "car": c["car"], "color": c["color"], "time": 0.0}
                for p, c in enumerate(order, start=1)]

    def commentators(self) -> list[dict]:
        return [{"name": "Комментатор один", "role": "ведущий"},
                {"name": "Комментатор два", "role": "эксперт"}]

    def delta_pair(self) -> list[str]:
        return [r["car_number"] for r in self.timing()[:2]]

    def sector_starts(self) -> list[float]:
        return [0.0, 0.34, 0.68]

    def camera_groups(self) -> dict[str, int]:
        return dict(DEMO_CAMERAS)

    def set_camera(self, car_number: str | None, group: int | None) -> bool:
        if car_number is not None:
            car = next((c for c in self.cars if c["number"] == str(car_number)), None)
            if car is None:
                return False
            self.cam["car_idx"] = car["idx"]
        if group is not None:
            self.cam["group"] = int(group)
            self.cam["camera"] = 1
        return True

    def weather(self) -> dict:
        tr = self.data.get("track") or {}
        t = time.monotonic() - self.started
        return {"track": self.track, "config": tr.get("config") or "", "length_km": tr.get("length_km") or 4.0,
                "turns": 0, "air": 22.0 + math.sin(t / 90) * 0.5, "track_temp": 34.0 + math.sin(t / 70),
                "wind_speed": 2.8, "wind_dir": math.radians(230), "humidity": 55.0,
                "skies": 1, "skies_text": "Partly Cloudy", "time_of_day": (14 * 3600 + self.now()) % 86400}

    def caution(self) -> bool:
        t = self.now()
        return any(a <= t < b for a, b in self._yellow)

    def session_flags(self) -> int:
        return (SF_YELLOW | SF_CAUTION) if self.caution() else SF_GREEN

    def incidents(self) -> dict[int, int]:
        t = self.now()
        out = {}
        for c in self.cars:
            done = bisect.bisect_right(c["times"], t)
            out[c["idx"]] = sum(n for lap, n in c["inc_at"].items() if lap in c["laps"][:done])
        return out

    def drivers(self) -> list[tuple[int, str, str, str, int, str]]:
        return [(c["idx"], c["number"], c["name"], "", _class_id(c["cls"]), c["cls"]) for c in self.cars]

    def driver_extra(self) -> dict[int, dict]:
        """То, чего нет в кортеже drivers(): рейтинг, лицензия, машина."""
        return {c["idx"]: {"irating": c["ir"], "lic": c["lic"], "car_name": c["car"],
                           "lic_color": LIC_COLOR.get(c["lic"][:1], ""), "user_id": int(c["id"] or 0),
                           "car_path": _car_path(c["car"])} for c in self.cars}


# Гонка демо — одна на всех, лежит рядом с кодом (своя гонка владельца,
# снятая с сайта: /api/desktop/race_replay). Нет файла — синтетика.
BUILTIN = pathlib.Path(__file__).with_name("demo_race.json")


def builtin() -> dict | None:
    try:
        return json.loads(BUILTIN.read_text("utf-8"))
    except (OSError, ValueError):
        return None


class ModuleDemo:
    """Демо одного модуля: свой SimClient с прошлой гонкой (или синтетикой).

    Модуль читает `self.sim` — пока демо включено, это сим демо; общий сим
    программы и другие модули его не видят.
    """

    def __init__(self, ctx, name: str) -> None:
        self.ctx, self.name = ctx, name
        self.sim = None
        self.state = {"on": False, "loading": False, "title": "", "synthetic": False}
        self._lock = threading.Lock()

    def status(self) -> dict:
        return dict(self.state)

    def set(self, on: bool) -> dict:
        from .demo import DemoRace
        from .simclient import SimClient
        if not on:
            self._swap(None, {"on": False, "loading": False, "title": "", "synthetic": False})
            return {"ok": True, **self.status()}
        if self.state["on"]:
            return {"ok": True, **self.status()}
        data, race = builtin(), None
        info = {"on": True, "loading": False, "title": "", "synthetic": False}
        if data:
            try:
                race = ReplayRace(data, data.get("me"))
                tr = data.get("track") or {}
                info["title"] = " · ".join(x for x in (tr.get("name"), (data.get("start") or "")[:10]) if x)
            except Exception:                          # noqa: BLE001
                log.exception("демо: встроенная гонка не читается")
                race = None
        if race is None or not race.cars:
            race = DemoRace()
            info.update(synthetic=True, title="синтетическая гонка")
        sim = SimClient()
        sim.set_demo(True, race)
        sim.start()
        self._swap(sim, info)
        return {"ok": True, **self.status()}

    def _swap(self, sim, info: dict) -> None:
        with self._lock:
            old, self.sim = self.sim, sim
            self.state = info
        if old is not None:
            old.stop()
        try:
            self.ctx.app._push_state()
        except Exception:                              # noqa: BLE001
            pass

    def stop(self) -> None:
        self._swap(None, {**self.state, "on": False, "loading": False})
