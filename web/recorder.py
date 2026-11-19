"""Gộp các cửa sổ báo động liên tiếp cùng nhãn thành MỘT sự kiện, lưu CSDL + ảnh chụp."""
import os
import threading
import time

import cv2

from . import db

THUMB_W = 320


def save_snapshot(event_id, label, start_ts, frame):
    """Lưu ảnh lớn + ảnh nhỏ; trả về (tên ảnh lớn, tên ảnh nhỏ).
    Tên file cố định theo sự kiện -> khi có cửa sổ tin cậy cao hơn thì ghi đè, không để lại file thừa."""
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(start_ts))
    base = f"ev{event_id:06d}_{label}_{stamp}"
    big, small = base + ".jpg", base + "_t.jpg"
    cv2.imwrite(os.path.join(db.SNAP_DIR, big), frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
    h, w = frame.shape[:2]
    s = THUMB_W / w
    thumb = cv2.resize(frame, (THUMB_W, max(1, round(h * s))), interpolation=cv2.INTER_AREA) if s < 1 else frame
    cv2.imwrite(os.path.join(db.SNAP_DIR, small), thumb, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return big, small


class EventRecorder:
    """Nhận kết quả từng cửa sổ (dict: label, conf, probs, alarm, run_len, win_start, win_end, frame, source).
    - Cửa sổ thuộc chuỗi ứng viên (run_len > 0): ghi nhớ cửa sổ đầu + cửa sổ tin cậy cao nhất.
    - Khi chuỗi đủ điều kiện báo động: tạo sự kiện (bắt đầu = đầu cửa sổ đầu tiên của chuỗi),
      chụp ảnh tại cửa sổ tin cậy cao nhất; các cửa sổ sau kéo dài kết thúc, thay ảnh nếu tin cậy cao hơn.
    - Chuỗi đứt (normal / dưới ngưỡng / đổi nhãn / dừng nguồn): đóng sự kiện."""

    def __init__(self, on_change=None):
        self.on_change = on_change or (lambda kind, ev: None)
        self._lock = threading.Lock()
        self._reset()

    def _reset(self):
        self.run_label = None
        self.run_start = None
        self.best = None          # cửa sổ tin cậy cao nhất của chuỗi
        self.event_id = None

    @property
    def current_id(self):
        return self.event_id

    def close(self):
        with self._lock:
            self._close_locked()

    def _close_locked(self):
        if self.event_id is not None:
            ev = db.get_event(self.event_id)
            if ev:
                self.on_change("closed", ev)
        self._reset()

    def window(self, w):
        with self._lock:
            if w["run_len"] <= 0:                       # không thuộc chuỗi ứng viên nào
                self._close_locked()
                return
            if w["label"] != self.run_label or w["run_len"] == 1:   # bắt đầu chuỗi mới
                self._close_locked()
                self.run_label, self.run_start = w["label"], w["win_start"]
            if self.best is None or w["conf"] > self.best["conf"]:
                self.best = w
                best_changed = True
            else:
                best_changed = False
            if not w["alarm"]:
                return
            b = self.best
            if self.event_id is None:                   # sự kiện bắt đầu
                self.event_id = db.insert_event(w["label"], self.run_start, w["win_end"], b["conf"], w["source"],
                                                b["probs"], b["win_end"],
                                                source_type=w.get("source_type") or "laptop")
                big, small = save_snapshot(self.event_id, w["label"], self.run_start, b["frame"])
                db.update_event(self.event_id, snapshot=big, thumb=small)
                self.on_change("started", db.get_event(self.event_id))
            else:                                       # sự kiện kéo dài
                fields = {"end_ts": w["win_end"]}
                if best_changed:
                    big, small = save_snapshot(self.event_id, w["label"], self.run_start, b["frame"])
                    fields.update(max_conf=b["conf"], probs=b["probs"], best_ts=b["win_end"], snapshot=big, thumb=small)
                db.update_event(self.event_id, **fields)
                self.on_change("updated", db.get_event(self.event_id))
