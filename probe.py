"""Диагностика допущений SDK. Запускать при живой сессии iRacing (спектейт).

Проверяет ровно то, что помечено в скилле как НЕ ПРОВЕРЕНО:
  1. есть ли канал ReplayFrameNumEnd и какой порог считать "мы на конце ленты";
  2. обновляется ли session YAML (Incidents) во время просмотра реплея;
  3. существует ли телеметрический канал с IncidentFlags и есть ли он на всё поле;
  4. насколько точен якорь по настенным часам.

Запуск:   python probe.py            — интерактивный прогон
          python probe.py --vars     — только список каналов с "incident" в имени
"""
from __future__ import annotations

import sys
import time

import irsdk


def banner(t: str) -> None:
    print("\n" + "=" * 64)
    print(t)
    print("=" * 64)


def connect() -> irsdk.IRSDK:
    ir = irsdk.IRSDK()
    print("подключаюсь к симу...")
    for _ in range(30):
        if ir.startup() and ir.is_initialized and ir.is_connected:
            print("подключено")
            return ir
        time.sleep(1)
    print("сим не найден. Запусти iRacing и зайди в сессию.")
    sys.exit(1)


def probe_vars(ir: irsdk.IRSDK) -> None:
    banner("1. Каналы, связанные с инцидентами и реплеем")
    names = ir.var_headers_names or []
    inc = [n for n in names if "incident" in n.lower()]
    rpy = [n for n in names if n.lower().startswith("replay")]
    print(f"всего каналов: {len(names)}")
    print("\nincident-каналы:")
    for n in inc:
        v = ir[n]
        kind = "МАССИВ (всё поле!)" if isinstance(v, list) else "скаляр"
        print(f"  {n:35s} = {str(v)[:40]:40s} {kind}")
    if not inc:
        print("  (нет — значит IncidentFlags недоступен как канал,")
        print("   детект только через session YAML ResultsPositions[].Incidents)")
    print("\nreplay-каналы:")
    for n in rpy:
        print(f"  {n:35s} = {ir[n]}")


def probe_live_threshold(ir: irsdk.IRSDK, seconds: int = 10) -> None:
    banner("2. ReplayFrameNumEnd в LIVE (не трогай реплей эти 10 секунд)")
    vals = []
    for _ in range(seconds * 2):
        ir.freeze_var_buffer_latest()
        v = ir["ReplayFrameNumEnd"]
        st = ir["SessionTime"]
        ir.unfreeze_var_buffer_latest()
        if v is not None:
            vals.append(int(v))
        print(f"  ReplayFrameNumEnd={v}  SessionTime={st:.2f}" if st else f"  {v}")
        time.sleep(0.5)
    if vals:
        print(f"\nв LIVE: min={min(vals)} max={max(vals)} среднее={sum(vals)/len(vals):.1f}")
        print(f"РЕКОМЕНДУЕМЫЙ порог live_frame_threshold: {max(vals) + 30}")
    else:
        print("\nканал недоступен — определять режим придётся иначе "
              "(IsReplayPlaying + сравнение SessionTime с монотонным ростом)")


def probe_replay_yaml(ir: irsdk.IRSDK, seconds: int = 60) -> None:
    banner("3. ГЛАВНОЕ: обновляется ли YAML в реплее")
    print("Сейчас: уйди в повтор (F3 / кнопка Replay) и НЕ возвращайся к live.")
    input("Как ушёл в реплей — нажми Enter... ")

    def counts():
        info = ir["SessionInfo"] or {}
        num = ir["SessionNum"]
        for s in (info.get("Sessions") or []):
            if s.get("SessionNum") == num:
                return {p.get("CarIdx"): p.get("Incidents")
                        for p in (s.get("ResultsPositions") or [])}
        return {}

    start = counts()
    total0 = sum(v or 0 for v in start.values())
    t0 = time.monotonic()
    print(f"стартовая сумма инцидентов по полю: {total0} (машин: {len(start)})")
    print(f"наблюдаю {seconds} секунд...\n")

    while time.monotonic() - t0 < seconds:
        time.sleep(5)
        now = counts()
        total = sum(v or 0 for v in now.values())
        frames = ir["ReplayFrameNumEnd"]
        changed = [f"CarIdx {k}: {start.get(k)}->{v}" for k, v in now.items()
                   if start.get(k) != v]
        print(f"  t+{time.monotonic()-t0:5.1f}s  сумма={total}  "
              f"ReplayFrameNumEnd={frames}  изменений={len(changed)}")
        for c in changed[:5]:
            print(f"      {c}")

    final = counts()
    total1 = sum(v or 0 for v in final.values())
    print(f"\nитог: было {total0}, стало {total1}")
    if total1 != total0:
        print("ВЫВОД: YAML в реплее ОБНОВЛЯЕТСЯ. Детект в режиме REPLAY работает,")
        print("       якорь по настенным часам применим. Одноклиентная схема годится.")
    else:
        print("ВЫВОД: изменений не было. Это НЕ доказательство — возможно, за эту")
        print("       минуту в гонке просто никто не собрал инцидент. Повтори тест")
        print("       в плотном трафике. Если подтвердится, что не обновляется —")
        print("       нужен второй спектейт-клиент.")


def probe_anchor(ir: irsdk.IRSDK, seconds: int = 30) -> None:
    banner("4. Точность якоря по настенным часам")
    print("Вернись к LIVE и нажми Enter.")
    input()
    ir.freeze_var_buffer_latest()
    t0, w0 = ir["SessionTime"], time.monotonic()
    ir.unfreeze_var_buffer_latest()
    print(f"якорь снят: SessionTime={t0:.2f}")
    print(f"жду {seconds}с, потом сверю оценку с фактом...")
    time.sleep(seconds)
    ir.freeze_var_buffer_latest()
    real = ir["SessionTime"]
    ir.unfreeze_var_buffer_latest()
    est = t0 + (time.monotonic() - w0)
    print(f"\nфакт  = {real:.2f}")
    print(f"оценка= {est:.2f}")
    print(f"ошибка= {abs(real - est):.3f} с   "
          f"({'годится' if abs(real-est) < 2 else 'ВНИМАНИЕ: расхождение'})")


def main() -> None:
    ir = connect()
    if "--vars" in sys.argv:
        probe_vars(ir)
        return
    probe_vars(ir)
    probe_live_threshold(ir)
    probe_replay_yaml(ir)
    probe_anchor(ir)
    banner("готово")
    print("Впиши подобранный live_frame_threshold в config.json "
          "(%LOCALAPPDATA%\\ichamps\\racecontrol\\config.json)")


if __name__ == "__main__":
    main()
