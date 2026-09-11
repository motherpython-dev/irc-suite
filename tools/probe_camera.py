"""Что iRacing отдаёт про камеру: запустить при открытой сессии (спектейт).

    python tools/probe_camera.py            # разовый снимок
    python tools/probe_camera.py --watch    # раз в полсекунды, пока крутишь камеру

Зачем: проверить, есть ли в телеметрии положение камеры (X/Y/Z) и зум (FOV).
Известные каналы — CamCarIdx, CamGroupNumber, CamCameraNumber, CamCameraState;
всё, что найдётся сверх них по маскам ниже, печатается с текущим значением.
В режиме --watch крути камеру в инструменте камер (Ctrl+F12) и смотри, какие
числа меняются — те и можно будет выставлять или хотя бы показывать.
"""
from __future__ import annotations

import re
import sys
import time

import irsdk

MASK = re.compile(r"cam|fov|zoom|pitch|yaw|roll|focus|posx|posy|posz", re.I)


def main() -> int:
    ir = irsdk.IRSDK()
    if not ir.startup():
        print("iRacing не найден — открой сессию и запусти снова")
        return 1

    names = sorted(n for n in ir.var_headers_names if MASK.search(n))
    print("каналов по маске:", len(names))
    for n in names:
        print(f"  {n:28} = {ir[n]}")

    info = ir["CameraInfo"] or {}
    groups = info.get("Groups") or []
    print(f"\nгрупп камер в CameraInfo: {len(groups)}")
    for g in groups:
        cams = ", ".join(str(c.get("CameraName")) for c in g.get("Cameras") or [])
        print(f"  {g.get('GroupNum'):>3} {g.get('GroupName')}: {cams}")

    if "--watch" not in sys.argv:
        return 0

    print("\nслежу за изменениями (Ctrl+C — выход)...")
    last = {}
    try:
        while True:
            ir.freeze_var_buffer_latest()
            for n in names:
                v = ir[n]
                if last.get(n) != v:
                    print(f"{time.strftime('%H:%M:%S')}  {n} = {v}")
                    last[n] = v
            ir.unfreeze_var_buffer_latest()
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
