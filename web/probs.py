"""Số liệu phiên "Xác suất theo thời gian": xác suất cả 4 nhãn của từng cửa sổ + xác suất cao nhất của
từng nhãn trong phiên. Giữ trong bộ nhớ (không ghi CSDL); xóa bằng nút "Xóa số liệu phiên"."""
import threading
import time
from collections import deque

MAX_WINDOWS = 1500          # ~50 phút camera với chu kỳ 2 giây


class ProbSession:
    def __init__(self, time_kind):
        self.time_kind = time_kind           # "clock" (camera: giờ thật) | "video" (giây trong video)
        self._lock = threading.Lock()
        self.video_id = None
        self.reset()

    def reset(self, video_id=None):
        with self._lock:
            self.windows = deque(maxlen=MAX_WINDOWS)
            self.max = {}
            self.n = 0
            self.started = time.time()
            self.video_id = video_id

    def add(self, w):
        """w: cửa sổ từ LiveDetector.on_window (có probs, label, conf, alarm, win_end, src_end)."""
        t = w["src_end"] if self.time_kind == "video" else w["win_end"]
        with self._lock:
            self.n += 1
            e = {"i": self.n, "t": round(t, 3), "label": w["label"], "conf": round(w["conf"], 4),
                 "alarm": bool(w["alarm"]), "probs": {k: round(v, 4) for k, v in w["probs"].items()}}
            self.windows.append(e)
            for lab, p in e["probs"].items():
                if p > self.max.get(lab, {}).get("p", -1):
                    self.max[lab] = {"p": p, "t": e["t"], "i": e["i"]}
            return e, {k: dict(v) for k, v in self.max.items()}

    def snapshot(self):
        with self._lock:
            return {"time_kind": self.time_kind, "video_id": self.video_id, "started": self.started, "n": self.n,
                    "windows": list(self.windows), "max": {k: dict(v) for k, v in self.max.items()}}
