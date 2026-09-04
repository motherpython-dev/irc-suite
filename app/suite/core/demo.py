"""Синтетическая гонка для проверки оверлеев без запущенного iRacing.

Зачем: вёрстку башни, решётки, дельт и пит-стопов невозможно проверить на
пустых данных, а поднимать сим ради каждой правки шрифта — долго. Здесь
живёт модель заезда, которая выдаёт те же структуры, что и `SimClient`, и
меняется во времени: поле едет, круги идут, кто-то заезжает на пит.

Это НЕ симулятор физики. Задача — правдоподобные числа нужной формы:
разрывы растут и сокращаются, позиции иногда меняются, времена кругов
скачут в разумных пределах.

Демо кормит ТОЛЬКО оверлеи трансляции. Рейс-контролу синтетика не идёт
намеренно: выдуманные инциденты осели бы в базе вперемешку с настоящими.

Время сжато (см. SPEED) и заезд начинается не с нуля, иначе первого
пит-стопа пришлось бы ждать минут двадцать — проверять оверлей нечем.
"""
from __future__ import annotations

import json
import math
import os
import pathlib
import random
import time
from dataclasses import dataclass, field

# Запасной состав, если своего нет. Имена выдуманные намеренно: настоящие
# фамилии в тестовых данных легко принять за реальные результаты, если
# снимок попадёт в чат лиги.
FALLBACK_ROSTER = [
    ("14", "Артём Ковалёв", "Nord Racing", "GT3"),
    ("7", "Максим Лавров", "Nord Racing", "GT3"),
    ("23", "Илья Соколов", "Vertex Motorsport", "GT3"),
    ("3", "Даниил Мороз", "Vertex Motorsport", "GT3"),
    ("88", "Егор Пантелеев", "Redline Crew", "GT3"),
    ("41", "Кирилл Ушаков", "Redline Crew", "GT3"),
    ("56", "Никита Верещагин", "Apex Union", "GT3"),
    ("9", "Роман Гайдуков", "Apex Union", "GT3"),
    ("77", "Тимур Астахов", "Silver Arrow", "GT3"),
    ("31", "Владислав Чуев", "Silver Arrow", "GT3"),
    ("12", "Пётр Заславский", "Ostwind", "GT4"),
    ("64", "Глеб Мартынов", "Ostwind", "GT4"),
    ("5", "Антон Рыбаков", "Kupava Sport", "GT4"),
    ("18", "Сергей Мещеряков", "Kupava Sport", "GT4"),
    ("47", "Лев Дорошенко", "Bereg Racing", "GT4"),
    ("2", "Марк Половцев", "Bereg Racing", "GT4"),
    ("29", "Юрий Балашов", "Steppe Motors", "GT4"),
    ("95", "Денис Кравчук", "Steppe Motors", "GT4"),
    ("60", "Олег Тимошенко", "Zarya Team", "GT4"),
    ("8", "Артур Мамедов", "Zarya Team", "GT4"),
]

def load_roster(path: "pathlib.Path | None" = None) -> list[tuple[str, str, str, str]]:
    """Состав из выгрузки чемпионата iRChamps, иначе запасной.

    Файл — `demo-roster.json` в папке данных: список объектов с полями
    `car_number`, `driver_name`, `team_name`, `division_name` (последние два
    необязательны). Ровно та форма, в которой чемпионат хранит регистрации,
    так что выгрузка с сайта кладётся как есть.
    """
    if path is None:
        base = os.environ.get("IRC_DATA_DIR") or os.environ.get("LOCALAPPDATA") or "."
        path = pathlib.Path(base)
        if not os.environ.get("IRC_DATA_DIR"):
            path = path / "iRC"
        path = path / "demo-roster.json"
    try:
        raw = json.loads(pathlib.Path(path).read_text("utf-8"))
    except Exception:
        return list(FALLBACK_ROSTER)

    rows = raw.get("drivers") if isinstance(raw, dict) else raw
    out = []
    for i, item in enumerate(rows or []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("driver_name") or item.get("name") or "").strip()
        if not name:
            continue
        num = str(item.get("car_number") or "").strip() or str(i + 1)
        team = str(item.get("team_name") or item.get("team") or "").strip()
        # дивизион чемпионата играет роль класса: по нему красится башня
        cls = str(item.get("division_name") or item.get("division_code")
                  or item.get("class_name") or "RACE").strip().upper()
        out.append((num, name, team, cls))
    return out or list(FALLBACK_ROSTER)


CLASS_ID = {"GT3": 4011, "GT4": 4012}
BASE_LAP = {"GT3": 92.4, "GT4": 99.8}          # секунды на круг
DEFAULT_LAP = 94.0                             # для классов вне списка
TRACK = "Sebring International Raceway"
PIT_LOSS = 52.0                                # потеря на заезд, секунды
SPEED = 8.0                                    # во столько раз быстрее реального
START_LAP = 9.0                                # заезд начинается по ходу гонки


def _lap(cls: str) -> float:
    return BASE_LAP.get(cls, DEFAULT_LAP)


def _class_id(cls: str) -> int:
    """Числовой id класса: башня красит строки по нему, а не по названию."""
    if cls in CLASS_ID:
        return CLASS_ID[cls]
    return 4000 + (abs(hash(cls)) % 900)


@dataclass
class _Car:
    idx: int
    number: str
    name: str
    team: str
    cls: str
    pace: float                 # личный темп: множитель к базовому кругу
    distance: float = 0.0       # пройдено кругов, дробное
    last_lap: float = 0.0
    best_lap: float = 0.0
    stops: int = 0
    tyre: int = 0
    on_pit: bool = False
    in_stall: bool = False
    pit_until: float = 0.0
    next_stop_lap: float = 0.0
    quali: float = 0.0
    laps_done: int = 0


class DemoRace:
    """Модель заезда. Тикает сама от часов, состояние читают геттеры."""

    def __init__(self, seed: int = 7) -> None:
        self.rnd = random.Random(seed)
        self.started = time.monotonic()
        self.cars: list[_Car] = []
        roster = load_roster()
        for i, (num, name, team, cls) in enumerate(roster):
            pace = 1.0 + self.rnd.uniform(-0.004, 0.016)
            car = _Car(idx=i, number=num, name=name, team=team, cls=cls,
                       pace=pace, tyre=0)
            car.quali = _lap(cls) * pace * self.rnd.uniform(0.985, 0.995)
            car.next_stop_lap = START_LAP + self.rnd.uniform(0.4, 6.0)
            self.cars.append(car)
        # стартовая расстановка — по времени квалификации внутри класса
        for cls in sorted({c.cls for c in self.cars}):
            group = sorted((c for c in self.cars if c.cls == cls),
                           key=lambda c: c.quali)
            for k, c in enumerate(group):
                # стартовые интервалы плюс наезженное к этому моменту гонки
                c.distance = START_LAP - 0.02 * k
        self._last = self.started

    # ---------- ход времени ----------

    def tick(self) -> None:
        now = time.monotonic()
        dt = min(2.0, max(0.0, now - self._last)) * SPEED
        self._last = now
        if dt <= 0:
            return

        for c in self.cars:
            lap_time = _lap(c.cls) * c.pace
            # лёгкое дыхание темпа, чтобы разрывы не были линейкой
            lap_time *= 1.0 + 0.004 * math.sin((now - self.started) / 23.0 + c.idx)
            if c.tyre == 1:                    # «медленный» компаунд
                lap_time *= 1.004

            if c.on_pit:
                if now >= c.pit_until:
                    c.on_pit = c.in_stall = False
                    c.stops += 1
                    c.tyre = 1 - c.tyre if self.rnd.random() < 0.7 else c.tyre
                    c.next_stop_lap = c.distance + self.rnd.uniform(13.0, 19.0)
                else:
                    c.in_stall = now < c.pit_until - 4.0
                    continue                   # на пите дистанция не растёт

            before = int(c.distance)
            c.distance += dt / lap_time
            if int(c.distance) > before:       # закрыт круг
                c.laps_done = int(c.distance)
                c.last_lap = round(lap_time * self.rnd.uniform(0.997, 1.006), 3)
                c.best_lap = c.last_lap if not c.best_lap else min(c.best_lap,
                                                                  c.last_lap)
            if not c.on_pit and c.distance >= c.next_stop_lap:
                c.on_pit = True
                # длительность считаем в реальных секундах: гонка ускорена,
                # и без деления на SPEED машина теряла бы восемь пит-стопов
                c.pit_until = now + PIT_LOSS / SPEED * self.rnd.uniform(0.9, 1.15)
                c.distance += 0.02             # въезд на пит-лейн

    # ---------- то, что читают модули ----------

    def _order(self) -> list[_Car]:
        return sorted(self.cars, key=lambda c: -c.distance)

    def timing(self) -> list[dict]:
        self.tick()
        order = self._order()
        leader = order[0]
        rows, seen_class = [], {}
        for pos, c in enumerate(order, start=1):
            seen_class[c.cls] = seen_class.get(c.cls, 0) + 1
            gap_laps = leader.distance - c.distance
            gap = gap_laps * _lap(c.cls) * c.pace
            rows.append({
                "car_idx": c.idx,
                "position": pos,
                "class_position": seen_class[c.cls],
                "car_number": c.number,
                "name": c.name,
                "team": c.team,
                "car_class": _class_id(c.cls),
                "class_name": c.cls,
                "gap": round(gap, 3),
                "laps": max(0, c.laps_done),
                "lap": max(1, int(c.distance) + 1),
                "on_pit": c.on_pit,
                "in_pit_stall": c.in_stall,
                "not_in_world": False,
                "last_lap": c.last_lap,
                "best_lap": c.best_lap,
                "lap_dist": round(c.distance % 1.0, 4),
                "tyre": c.tyre,
            })
        return rows

    def qualifying_grid(self) -> list[dict]:
        rows = []
        for pos, c in enumerate(sorted(self.cars, key=lambda c: c.quali), start=1):
            rows.append({
                "position": pos,
                "car_idx": c.idx,
                "car_number": c.number,
                "name": c.name,
                "team": c.team,
                "class_name": c.cls,
                "time": round(c.quali, 3),
            })
        return rows

    def commentators(self) -> list[dict]:
        """Кого показать в плашке, пока своих имён не задали."""
        return [{"name": "Комментатор один", "role": "ведущий"},
                {"name": "Комментатор два", "role": "эксперт"}]

    def delta_pair(self) -> list[str]:
        """Пара для оверлея дельты: два первых номера в текущем порядке."""
        order = self._order()
        return [c.number for c in order[:2]]

    def drivers(self) -> list[tuple[int, str, str, str, int, str]]:
        return [(c.idx, c.number, c.name, c.team, _class_id(c.cls), c.cls)
                for c in self.cars]
