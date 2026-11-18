# ТЗ: модуль «Телеметрия» — фаза 0 (данные для модели машины)

Зачем: строим модель машины iRacing (аэро, шины «на ось», тепловая модель) по своей телеметрии и
считаем по ней сетапы под любую трассу — план в `irc-setups/docs/MODEL_PLAN.md`. Модулю нужно
добавить данные, которых сейчас нет: **драфт и трафик**, **условия и версия сима**, **метка
эксперимента**, **выбеги** и **колёса** из `.ibt`. Анализ и модели — не в iRC (их делает Claude
по файлам); iRC только собирает и раскладывает данные.

Исходные ТЗ: `TELEMETRY_SPEC.md`, `TELEMETRY_FIXES.md`. Код: `app/suite/modules/telemetry/`
(`__init__.py`, `laps.py`, `ibt.py`). Не ломать: формат `laps.jsonl`/`laps_ibt.jsonl`
(только добавлять поля), раскладку папок, маршруты API, отправку на сервер.

Проверено на данных (дамп live-каналов `stockcarschevycamarozl12022_live.json`, `.ibt` от 25.10):
- live: `CarIdxLap`, `CarIdxLapCompleted`, `CarIdxLapDistPct`, `CarIdxTrackSurface`,
  `CarIdxTrackSurfaceMaterial`, `CarIdxOnPitRoad`, `CarIdxEstTime`, `CarIdxClass`, `CarIdxSteer`;
  погода: `TrackTemp`, `TrackTempCrew`, `AirTemp`, `AirDensity`, `AirPressure`, `RelativeHumidity`,
  `WindVel`, `WindDir`, `TrackWetness`, `Skies`, `Precipitation`;
- в `.ibt` (не в live): `LFspeed`, `RFspeed`, `LRspeed`, `RRspeed` (скорость колеса), высоты, давления;
  `CarIdx*` в `.ibt` **нет** — драфт пишет только iRC;
- session info: `WeekendInfo.BuildVersion` (напр. `2026.10.01.01`), `BuildType`, `TrackVersion`,
  `TrackType`, `TrackAltitude`, `TrackDynamicTrack`, `WeekendOptions` (`WeatherType`, `TimeOfDay`,
  `WeatherTemp`), `SessionInfo.Sessions[].SessionTrackRubberState` (напр. `carry over`).

---

## П1. Драфт и трафик (live, только iRC)

**Чтение:** в отводе 60 Гц добавить `CarIdxLapDistPct`, `CarIdxLapCompleted`, `CarIdxOnPitRoad`,
`CarIdxTrackSurface`, `CarIdxEstTime`, `CarIdxClass`, `PlayerCarIdx`. Считать раз в 6 кадров (10 Гц).

**Расчёт на кадре** (`L` — длина трассы в м из `WeekendInfo.TrackLength`):
- учитывать машины: не своя, `CarIdxTrackSurface` ≥ 0 (на трассе/в мире), не на пит-лейне;
- дистанция вперёд до машины `k`: `((pct[k] − pct[me]) mod 1) × L`; назад: `((pct[me] − pct[k]) mod 1) × L`;
- ближайшая впереди и сзади: дистанция (м) и разрыв по времени (с) из `CarIdxEstTime`
  (с учётом перехода через линию);
- скорость сближения: изменение дистанции до той же машины между кадрами (м/с).

**Сводка круга** — заполнить поле `draft` (сейчас `null`) в `laps.jsonl`:
```json
"draft": {
  "ahead_m": {"median": 12.4, "p10": 6.1}, "behind_m": {"median": 30.2, "p10": 9.8},
  "ahead_s": {"median": 0.15}, "behind_s": {"median": 0.37},
  "pct_ahead_lt": {"10": 22.0, "25": 61.0, "50": 85.0},
  "pct_behind_lt": {"10": 5.0, "25": 30.0, "50": 70.0},
  "cars_within_100m": 3.4,
  "solo": false
}
```
`solo = true`, если ≥90% круга нет машины впереди ближе 100 м и сзади ближе 50 м (пороги — в конфиге).

**Сырые позиции** — `field/lap_NNN.csv.gz`, 10 Гц: `session_time, my_pct, ahead_idx, ahead_m,
ahead_s, behind_idx, behind_m, behind_s, closing_ahead_ms, closing_behind_ms`.
Плюс один файл на сессию `field/all.csv.gz`, 10 Гц: `session_time` и `pct` всех 64 машин
(для разборов, кто где ехал). Связь с `.ibt` — по `SessionTime`. На сервер `field/` не отправлять.

**API:** `GET /api/telemetry/field?seconds=30` — последние N секунд: ближайшие впереди/сзади,
дистанции, разрывы.

## П2. Условия и версия сима

**Каждая строка круга** (`laps.jsonl`, `laps_ibt.jsonl`) — блок `cond`, средние по кругу:
```json
"cond": {"track_temp": 37.6, "track_temp_crew": 30.6, "air_temp": 22.6, "air_density": 1.18,
         "air_pressure": 100340, "humidity": 0.52, "wind_vel": 2.8, "wind_dir": 4.95,
         "wetness": 0, "skies": 1, "precip": 0.0,
         "surface_material": {"asphalt": 0.71, "concrete": 0.29}}
```
`surface_material` — доля кадров круга по `CarIdxTrackSurfaceMaterial[me]` (Martinsville: бетон в поворотах).

**`session.json`** — добавить: `build_version`, `build_type`, `track_version`, `track_type`,
`track_altitude`, `dynamic_track`, `weather_type`, `time_of_day`, `weather_temp`, и список
`sessions: [{session_num, session_type, rubber_state, first_seen}]` (накат — свой у каждой сессии).

## П3. Метка эксперимента

Пилот сохраняет сетап под именем `EXP-<ID>-<описание>`, например `EXP-A05-F0-R0`, `EXP-S03-CROSS57`
(`<ID>` = буква фазы + две цифры). Имя есть в `DriverInfo.DriverSetupName`.

- Если имя совпадает с `^EXP-([A-Z]\d{2})(?:-(.*))?(\.sto)?$` — в каждую строку круга
  `"exp": {"id": "A05", "label": "F0-R0"}`; иначе `"exp": null`.
- Привязка — к сетапу, на котором проехан круг (как `setup_id` сейчас), а не к последнему загруженному имени
  (известная проблема: имя в снимке отстаёт — брать имя из того же снимка, что и `setup_id`).
- `experiments.json` в корне телеметрии: `{ "A05": [{"session": dir, "ibt": path, "laps": [..], "setup_id": n}], ... }`.
- **API:** `GET /api/telemetry/experiments`; в `status()` — текущий `exp`.
- В UI модуля — строка «Эксперимент: A05 · F0-R0» или «Эксперимент: нет», чтобы пилот видел, что метка распознана.

## П4. Колёса и тормоза (пересчёт из `.ibt`)

В `laps_ibt.jsonl` добавить блок `wheels`:
- **блокировка:** кадры, где `Brake > 0.1` и `{c}speed / Speed < 0.9` (колесо медленнее машины на 10%+),
  по колёсам: `lock_frames`, `lock_events` (слитные отрезки), макс. глубина;
- **пробуксовка:** `Throttle > 0.5` и `{c}speed / Speed > 1.05` для задних — `spin_frames`, `spin_events`;
- **тормоза:** пик `{c}brakeLinePress` за круг, доля круга на тормозе;
- **на что делить:** `Speed > 15 м/с`, чтобы не ловить шум на малой скорости.

Пороги — в конфиге. Каналы `{c}speed` — проверить единицы (м/с) на первом `.ibt`.

## П5. Выбеги (пересчёт из `.ibt`)

Найти в `.ibt` отрезки выбега и записать `coasts.jsonl` (по одной строке на отрезок):
- условия отрезка: `Throttle < 0.02`, `Brake < 0.02`, `|LatAccel| < 1.0 м/с²`, `Speed > 28 м/с`,
  длительность ≥ 3 с;
- поля: `t_start, t_end, v_start, v_end, decel_mean` (из `LongAccel` и из производной `Speed`),
  `clutch_mean`, `gear`, `lap`, `lap_dist_start`, `setup_id`, `exp`, `cond` (как в П2),
  `draft` за отрезок (если есть live-данные П1 за то же время — по `SessionTime`),
  средние высоты LF/RF/LR/RR/CFSR за отрезок;
- плюс сам ряд `[(v, decel)]` с шагом 0.5 с — для подгонки сопротивления.

## П6. Хранение и отправка

- `field/` — только локально; `coasts.jsonl` и новые поля круга — отправлять на сервер, как сейчас.
- Хранение `field/` — те же правила, что для сырых данных (`raw_keep_sessions`).

---

## Приёмка

**Офлайн (без iRacing):** `tools/telemetry_replay.py` на `.ibt` с выбегами:
1. `coasts.jsonl` — по строке на каждый выбег (проверочный `.ibt`: пилот делает 3 выбега на прямой
   с выжатым сцеплением → 3 строки, `v_start > v_end`, `decel_mean < 0`).
2. `laps_ibt.jsonl` — есть `wheels` (на Talladega `lock_events = 0`); есть `cond`.
3. `session.json` — есть `build_version`, `rubber_state` в `sessions`.

**Живая:**
1. **Test Drive** соло, сетап `EXP-A05-TEST`: в кругах `exp.id = "A05"`, `draft.solo = true`,
   `ahead_m` пусто/`null`; в UI «Эксперимент: A05 · TEST».
2. **AI-гонка** (любой овал): разрыв до машины впереди в `GET /api/telemetry/field` совпадает с
   блоком Relative iRacing (F3) в пределах 0.1 с; на прямой за машиной `ahead_m` 5–20 м;
   `pct_ahead_lt` растёт, когда едешь в паке.
3. **Martinsville:** `surface_material` показывает и асфальт, и бетон; на тормозах в повороте
   `wheels.LF.lock_events` > 0, если пилот специально перетормозил.

## Не менять
- Существующие поля и маршруты; логику кругов и пересчёта из `.ibt` (только добавить блоки).
- Логику LIVE/REPLAY рейс-контроля.
