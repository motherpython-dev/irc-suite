# ТЗ: модуль «Телеметрия» iRC — данные для разбора сетапа

> **Состояние (iRC 2.34.0):** этап 1 сделан целиком (п. 2.1–2.6) плюс маршруты
> `status, vars, sessions, laps, setup` из п. 4.4. Не сделано: профили, сырые
> 20 Гц, гистограммы, `/api/telemetry/live` и `/profile` (этап 2), драфт (этап 3).
> Отличия от текста: `ibt.json` — последний файл + список `files`; в сводке
> круга есть `samples` (число кадров) и давления `pressure`/`cold_pressure`;
> модуль регистрирует сам бандл (точка входа в exe его не знает).

Цель: iRC пишет телеметрию **своей машины** так, чтобы Claude (скилл `iracing-oval-setup`)
мог разбирать сетап как гоночный инженер: высоты и платформа в повороте и на прямой, работа
амортизаторов, руль против рыскания, драфт, шины — и связывать это с конкретной версией сетапа.
Данные доступны двумя путями: **файлы** в папке данных iRC (постфактум) и **HTTP API** на
`127.0.0.1:8777` (почти реальное время, пока идёт сессия). Плюс `.ibt` от самого iRacing
(360 Гц, полный набор каналов) — iRC только включает запись и регистрирует файл.

Только своя машина и локально: в SDK подвески/шин чужих машин нет.

## 0. Место в архитектуре

- Модуль `app/suite/modules/telemetry`, `id = "telemetry"`, интерфейс `Module`
  (`start/stop/status/routes`). Включается в конфиге, по умолчанию выключен.
- Свой экземпляр `irsdk` модуль не создаёт (правило `Context`). Нужен **высокочастотный отвод**
  в `SimClient`: он сейчас опрашивает сим на 5 Гц, телеметрии нужно 60 Гц для заданного
  списка каналов. Предложение: `SimClient.add_tap(names: list[str], hz: int, callback)` —
  в том же фоновом потоке, с `freeze_var_buffer_latest()`, callback получает dict значений
  и `SessionTime`/`SessionTick`. Только в режиме `LIVE` (в реплее не писать — см. docstring
  `simclient.py`). Чтение ~50 каналов на 60 Гц в Python — дёшево.
- Хранение — через `ctx.storage`, корень `%LOCALAPPDATA%\iRC\telemetry\` (или `IRC_DATA_DIR`).
- События в SSE: `telemetry.lap`, `telemetry.setup`, `telemetry.session`.

## 1. Этапы

| Этап | Что | Зачем |
|---|---|---|
| **1 — минимум** | дамп каналов, снимки сетапа, покруговые сводки, регистрация `.ibt` | уже можно сравнивать сетапы по кругам и разбирать `.ibt` |
| **2** | профиль круга по дистанции, сырые данные круга 20 Гц, гистограммы амортизаторов, HTTP live | разбор «где на круге» и почти реальное время |
| **3** | драфт/трафик, сравнение кругов в UI | разбор суперспидвеев и гонки в паке |

## 2. Этап 1

### 2.1 Дамп доступных каналов (обязательно, первым)
Набор каналов зависит от машины и от того, live это или `.ibt` (часть каналов iRacing пишет
только на диск). Поэтому при подключении к симу и смене машины:
- сохранить `vars/<car_path>_live.json`: для каждого `var_header` — `name, unit, desc, type, count`;
- отдать `GET /api/telemetry/vars`.
По этому дампу Claude уточнит список каналов в п. 3. Каналы, которых нет, — писать `null`,
не падать.

### 2.2 Снимки сетапа из session YAML
В session info есть раздел `CarSetup` — текущий сетап, как в гараже, со счётчиком
`CarSetup.UpdateCount`. HTML-экспорт тогда не нужен.
- При старте сессии и при каждом изменении `UpdateCount` сохранить `setups/<NNN>.yaml`
  (раздел `CarSetup` целиком, как есть) + `setups/<NNN>.meta.json`:
  `{setup_id, update_count, saved_at, session_time, sha1}`. Одинаковый sha1 — не дублировать.
- `setup_id` текущего сетапа проставлять в каждый круг.
- Также сохранить имя загруженного сетапа, если оно доступно (`DriverInfo.DriverSetupName`,
  `DriverSetupLoadTypeName`, `DriverSetupIsModified`).

### 2.3 Сессия
`session.json` при старте и обновлять при изменениях:
`subsession_id, session_num, session_type (Practice/Qualify/Race/Offline Testing),
track (TrackName, TrackDisplayName, TrackConfigName, TrackLength), car (CarPath, CarScreenName),
driver (UserID, UserName), weather (TrackSurfaceTemp, AirTemp, TrackWindVel, TrackWindDir,
Skies, RelativeHumidity), started_at, iRC version`. Источник — `WeekendInfo`, `DriverInfo`,
`SessionInfo`.

### 2.4 Покруговые сводки — `laps.jsonl`
Одна строка JSON на завершённый круг (смена `Lap`/`LapCompleted`), дописывать по мере езды.
Все величины — в единицах SDK (м, м/с, рад, рад/с, м/с², °C, kPa, л), без конвертации.

```json
{
  "lap": 12, "setup_id": 3, "session_num": 2, "t_start": 1234.5, "t_end": 1283.1,
  "lap_time": 48.62, "valid": true, "flags": ["pit_in", "pit_out", "off_track", "incident", "yellow"],
  "fuel_start": 61.2, "fuel_end": 60.1, "fuel_used": 1.1,
  "track_temp": 38.4, "air_temp": 26.0, "wind_vel": 2.1, "wind_dir": 1.57,
  "speed": {"max": 87.1, "min": 79.3, "mean": 83.0},
  "throttle_full_pct": 98.7, "brake_used_pct": 0.0,
  "steer": {"corner_mean_abs": 0.061, "straight_mean_abs": 0.004},
  "lat_accel": {"corner_mean": 9.1, "max": 11.2},
  "vert_accel_max": 21.5,
  "ride_height": {
    "LF": {"corner_mean": 0.0121, "corner_min": 0.0098, "straight_mean": 0.0185},
    "RF": {...}, "LR": {...}, "RR": {...}, "CFSR": {...}
  },
  "shock_defl": {"LF": {"min": 0.01, "max": 0.06}, "RF": {...}, "LR": {...}, "RR": {...}},
  "tires": {"LF": {"temp": [L, M, R], "wear": [L, M, R], "pressure": 172.0}, ...},
  "draft": null
}
```
- «Поворот» = `|LatAccel| > 4 м/с²`, «прямая» = `|LatAccel| < 1.5 м/с²` и `|SteeringWheelAngle|`
  мала. Пороги — в конфиге модуля.
- `valid = false`, если круг пит-лейна, был сход с трассы (`PlayerTrackSurface` ≠ on track),
  инцидент или жёлтый флаг; причины — в `flags`.
- `tires`: что SDK отдаёт live. Если live-температуры обновляются только в боксе — писать
  последнее значение и флаг `tires_stale: true`; при стоянке в боксе (`PlayerCarInPitStall`)
  писать отдельную строку `{"event": "pit_stall", "tires": {...}}`.

### 2.5 `.ibt`
- Опция «включать запись телеметрии iRacing автоматически»: broadcast `TelemCommand` start
  при выезде из бокса, stop при выходе из машины (через `ctx.rc`). По умолчанию — выключено,
  включает пользователь.
- В конце сессии найти новый `.ibt` в `Documents\iRacing\telemetry` (учесть OneDrive-путь
  `OneDrive\Documents`) по времени и машине/трассе в имени, записать в `ibt.json`:
  `{path, size, mtime, car, track}`. Файл не копировать и не двигать.

### 2.6 Раскладка папки
```
telemetry/
  vars/<car_path>_live.json
  2026-10-21_1830_talladega_chevycamarozl12022_<subsession>/
    session.json
    setups/001.yaml, 001.meta.json, ...
    laps.jsonl
    ibt.json
    profiles/lap_012.json        (этап 2)
    raw/lap_012.csv.gz           (этап 2)
```
В Test Drive (`session_type = Offline Testing`) `SubSessionID = 0` → вместо него в имени
папки писать `test`; уникальность даёт дата-время. Тест-драйв — обычный режим записи,
не пропускать его.
`index.json` в корне — список сессий (папка, трасса, машина, дата, число кругов) для быстрого поиска.

## 3. Каналы (live), этап 1–2
Имена — по каталогу iRacing; **сверить с дампом 2.1**, отсутствующие → `null`.

| Группа | Каналы |
|---|---|
| Время/круг | `SessionTime`, `SessionTick`, `Lap`, `LapCompleted`, `LapDist`, `LapDistPct`, `LapCurrentLapTime`, `LapLastLapTime` |
| Движение | `Speed`, `VelocityX`, `VelocityY`, `VelocityZ`, `LatAccel`, `LongAccel`, `VertAccel`, `YawRate`, `Yaw`, `Pitch`, `Roll` |
| Пилот | `SteeringWheelAngle`, `Throttle`, `Brake`, `Clutch`, `Gear`, `RPM` |
| Платформа | `LFrideHeight`, `RFrideHeight`, `LRrideHeight`, `RRrideHeight`, `CFSRrideHeight` (сплиттер, если есть) |
| Подвеска | `LFshockDefl`, `RFshockDefl`, `LRshockDefl`, `RRshockDefl`, `LFshockVel`, `RFshockVel`, `LRshockVel`, `RRshockVel` |
| Шины | `LFtempCL/CM/CR` (и RF/LR/RR), `LFwearL/M/R` (и др.), `LFcoldPressure` (и др.), горячие давления если есть |
| Состояние | `FuelLevel`, `PlayerTrackSurface`, `OnPitRoad`, `PlayerCarInPitStall`, `SessionFlags`, `PlayerCarMyIncidentCount` |
| Погода live | `TrackTempCrew`, `AirTemp`, `WindVel`, `WindDir`, `AirDensity` |
| Поле (этап 3) | `CarIdxLapDistPct`, `CarIdxEstTime`, `CarIdxOnPitRoad`, `CarIdxTrackSurface`, `PlayerCarIdx` |

## 4. Этап 2

### 4.1 Профиль круга — `profiles/lap_NNN.json`
Круг делится на 200 участков по `LapDistPct`; для каждого — среднее по участку:
`speed, steer, lat_accel, long_accel, throttle, brake, rh_LF/RF/LR/RR/CFSR, shock_defl_*`.
Формат: `{"lap": 12, "bins": 200, "channels": {"speed": [...200], ...}}`.
Это главный инструмент сравнения кругов и сетапов «в одной точке трассы».

### 4.2 Сырые данные круга — `raw/lap_NNN.csv.gz`
20 Гц (прореживание из 60), все каналы из п. 3 кроме `CarIdx*`, первая строка — заголовок.
≈100 КБ на круг. Хранить N последних сессий (конфиг `raw_keep_sessions`, по умолчанию 20).

### 4.3 Гистограммы амортизаторов
В сводку круга: `shock_hist.<corner>` — доля времени (%) по корзинам скорости штока
(`shockVel`, м/с), края: `[-∞, -0.127, -0.076, -0.038, -0.013, 0.013, 0.038, 0.076, 0.127, +∞]`
(±0.5/1.5/3/5 in/s). Плюс `avg_bump`, `avg_rebound`, `ls_bump_pct`, `hs_bump_pct`,
`ls_reb_pct`, `hs_reb_pct` с порогом LS/HS 0.038 м/с (1.5 in/s) — как в Shock Tuning Guide.
Считать по 60 Гц (live), точный вариант Claude посчитает из `.ibt` 360 Гц.

### 4.4 HTTP API (почти реальное время)
| Маршрут | Ответ |
|---|---|
| `GET /api/telemetry/status` | включён ли модуль, сессия, текущий круг, setup_id, пишется ли `.ibt` |
| `GET /api/telemetry/vars` | дамп каналов |
| `GET /api/telemetry/sessions` | `index.json` |
| `GET /api/telemetry/laps?session=<dir>&since=<lap>` | строки `laps.jsonl` после `since` |
| `GET /api/telemetry/profile?session=&lap=` | профиль круга |
| `GET /api/telemetry/live?seconds=30&channels=a,b` | последние N секунд из кольцевого буфера, 20 Гц, JSON-колонки |
| `GET /api/telemetry/setup?session=&id=` | YAML сетапа |
Только `127.0.0.1`. Без авторизации (локально), как остальные GET-маршруты iRC.

## 5. Этап 3 — драфт и трафик
В сводку круга `draft`: по `CarIdxLapDistPct` и длине трассы — дистанция до ближайшей машины
впереди и сзади (м); `pct_in_draft` — доля круга с машиной впереди ближе `draft_m`
(конфиг, по умолчанию 25 м); `solo: true/false`. Без этого нельзя честно сравнивать скорость
на суперспидвее: круг в драфте и соло — разные круги.

## 6. Нагрузка и диск
- Отвод 60 Гц только когда модуль включён и режим `LIVE`; агрегирование на лету, без
  записи 60 Гц на диск (60 Гц/360 Гц есть в `.ibt`).
- Этап 1: килобайты на сессию. Этап 2: ~100 КБ/круг raw + ~20 КБ/круг профиль.
- Ошибки чтения отдельного канала не роняют модуль; в `status` — счётчик ошибок.

## 7. Приёмка
1. Test Drive, Next Gen, Talladega: 10 кругов, затем в бокс, сменить cross на 1 клик, ещё 5 кругов.
2. Есть `vars/..._live.json`; в `session.json` трасса, машина, погода.
3. `setups/` — два снимка, разный sha1; круги 1–10 с `setup_id` 1, 11–15 — с 2.
4. `laps.jsonl` — 15 строк, у кругов выезда/заезда `valid=false` с причиной; высоты не `null`
   (или `null` и канала нет в дампе — тогда это фиксируем, а не баг).
5. `ibt.json` указывает на существующий файл (при включённой опции).
6. Этап 2: `GET /api/telemetry/laps?since=0` во время езды возвращает новые круги через ≤2 с
   после пересечения линии; `profile` — 200 значений на канал.

## 8. Как это использует Claude
- Постфактум: читает `telemetry/<сессия>/` и `.ibt` (pyirsdk `IBT`), сравнивает круги и сетапы
  (`setup_id`), строит выводы: высоты в повороте/на прямой, сплиттер, руль, гистограммы.
- Во время сессии: в чате Claude Code по запросу опрашивает `127.0.0.1:8777/api/telemetry/*`
  (например «посмотри последние 5 кругов»). Сам в чат без запроса Claude не пишет.
