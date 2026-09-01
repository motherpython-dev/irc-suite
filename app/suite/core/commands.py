"""Broadcast-команды в сим.

Идут через HWND_BROADCAST (irsdk.py: SendNotifyMessageW(0xFFFF, ...)), поэтому
фокус окна iRacing НЕ нужен — панель может жить на втором мониторе и не воровать
фокус у сима.

Флагов здесь нет намеренно: в BroadcastMsg их не существует, они делаются
админ-чат-командами. Появятся отдельным модулем, когда дойдём до них.
"""
from __future__ import annotations

import time

try:
    import irsdk
except ImportError:
    irsdk = None

from .storage import Incident, Storage


class RaceControl:
    def __init__(self, ir, storage: Storage | None = None):
        self.ir = ir
        self.storage = storage

    def _log(self, kind: str, **payload) -> None:
        if self.storage:
            try:
                self.storage.log_event(kind, payload)
            except Exception:
                pass

    def show_incident(self, inc: Incident, pre_roll: float | None = None,
                      camera_group: int | None = None) -> bool:
        """Перемотать реплей к инциденту, навести камеру, поставить паузу.

        pre_roll по умолчанию больше для оценочных якорей: они пойманы в режиме
        REPLAY и имеют погрешность порядка 1-2 секунд.

        camera_group=None — сохраняем ТЕКУЩУЮ группу камеры оператора
        (`CamGroupNumber`), меняя только машину. Важная деталь: у
        `cam_switch_num` группа по умолчанию 1 (Nose), поэтому "не указать
        группу" не означает "не трогать вид" — на каждый клик оператора
        выдёргивало в Nose. Группу надо передавать явно.
        """
        if not self.ir:
            return False
        if pre_roll is None:
            pre_roll = 15.0 if inc.estimated else 5.0

        ms = int(max(0.0, inc.session_time - pre_roll) * 1000)

        # Команду шлём ДВАЖДЫ. Замер на живой сессии: первый вызов из live-режима
        # только переводит сим в повтор и почти не сдвигает позицию
        # (frame 60157 -> 60158), а точное позиционирование делает второй
        # (frame -> 45816). Одиночная команда выглядит как "видео просто
        # встало на паузу и никуда не перемоталось".
        self.ir.replay_search_session_time(inc.session_num, ms)
        time.sleep(0.25)
        self.ir.replay_search_session_time(inc.session_num, ms)
        if inc.car_number and inc.car_number != "?":
            group = camera_group
            if group is None:
                try:
                    group = int(self.ir["CamGroupNumber"] or 1)
                except Exception:
                    group = 1
            self.ir.cam_switch_num(str(inc.car_number), group=group)
        self.ir.replay_set_play_speed(0)
        self._log("cmd_show_incident", incident_id=inc.id, session_num=inc.session_num,
                  session_time=inc.session_time, pre_roll=pre_roll, car=inc.car_number)
        return True

    def resume_live(self) -> bool:
        if not self.ir:
            return False
        self.ir.replay_search(irsdk.RpySrchMode.to_end)
        self.ir.replay_set_play_speed(1)
        self._log("cmd_resume_live")
        return True

    def play(self, speed: int = 1, slow_motion: bool = False) -> bool:
        if not self.ir:
            return False
        self.ir.replay_set_play_speed(speed, slow_motion)
        self._log("cmd_play", speed=speed, slow=slow_motion)
        return True

    def pause(self) -> bool:
        return self.play(0)

    def step(self, forward: bool = True) -> bool:
        if not self.ir:
            return False
        mode = irsdk.RpySrchMode.next_frame if forward else irsdk.RpySrchMode.prev_frame
        self.ir.replay_search(mode)
        self._log("cmd_step", forward=forward)
        return True

    def jump_sim_incident(self, forward: bool = True) -> bool:
        """Прыжок по СОБСТВЕННЫМ маркерам инцидентов iRacing.
        Страховка: они ставятся симом независимо от нашего детекта."""
        if not self.ir:
            return False
        mode = irsdk.RpySrchMode.next_incident if forward else irsdk.RpySrchMode.prev_incident
        self.ir.replay_search(mode)
        self._log("cmd_jump_sim_incident", forward=forward)
        return True

    def cam_at_incident(self) -> bool:
        """Камера на инцидент по мнению самого сима (csMode.at_incident = -3)."""
        if not self.ir:
            return False
        self.ir.cam_switch_pos(irsdk.csMode.at_incident)
        self._log("cmd_cam_at_incident")
        return True

    def cam_car(self, car_number: str) -> bool:
        if not self.ir:
            return False
        self.ir.cam_switch_num(str(car_number))
        self._log("cmd_cam_car", car=car_number)
        return True
