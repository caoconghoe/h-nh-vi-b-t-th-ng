"""Phần dùng chung: nạp model, tiền xử lý khung, suy luận 16 khung, quy tắc báo động."""
import os
from dataclasses import dataclass, field

import cv2
import numpy as np
import torch
from transformers import VideoMAEForVideoClassification, VideoMAEImageProcessor

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MODEL_DIR = os.path.join(HERE, "best_model_v3")

NUM_FRAMES = 16          # số khung mỗi cửa sổ
WINDOW_SEC = 3.0         # độ dài cửa sổ (giây)
SAMPLE_DT = WINDOW_SEC / NUM_FRAMES   # 0.1875 s giữa 2 khung lấy mẫu
SHORT_EDGE = 224

_ROT = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}


def rotate(frame, deg):
    """Xoay khung theo chiều kim đồng hồ 0/90/180/270 độ."""
    deg = int(deg) % 360
    return cv2.rotate(frame, _ROT[deg]) if deg in _ROT else frame


def resize_short_edge(frame, short=SHORT_EDGE):
    h, w = frame.shape[:2]
    s = short / min(h, w)
    if s == 1.0:
        return frame
    interp = cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR
    return cv2.resize(frame, (max(1, round(w * s)), max(1, round(h * s))), interpolation=interp)


class Classifier:
    def __init__(self, model_dir=DEFAULT_MODEL_DIR, threads=None):
        torch.set_num_threads(threads or os.cpu_count() or 4)
        self.processor = VideoMAEImageProcessor.from_pretrained(model_dir)
        self.model = VideoMAEForVideoClassification.from_pretrained(model_dir).eval()
        # nhãn lấy từ config.json, không hardcode thứ tự
        self.id2label = {int(k): v for k, v in self.model.config.id2label.items()}
        self.labels = [self.id2label[i] for i in range(len(self.id2label))]
        self.normal_label = "normal"

    def preprocess_frame(self, bgr):
        """Khung BGR (đã xoay đúng chiều) -> mảng float32 (3, 224, 224) đã chuẩn hóa."""
        rgb = cv2.cvtColor(resize_short_edge(bgr), cv2.COLOR_BGR2RGB)
        out = self.processor([rgb], return_tensors="np")["pixel_values"]
        return np.asarray(out, dtype=np.float32).reshape(3, *out.shape[-2:])

    @torch.inference_mode()
    def predict(self, frames):
        """frames: list 16 mảng (3,224,224) đã tiền xử lý -> (nhãn, độ tin cậy, dict xác suất)."""
        x = torch.from_numpy(np.stack(frames)[None])          # (1, 16, 3, 224, 224)
        probs = torch.softmax(self.model(pixel_values=x).logits[0], dim=-1).numpy()
        i = int(probs.argmax())
        return self.id2label[i], float(probs[i]), {self.id2label[j]: float(p) for j, p in enumerate(probs)}


class AlertState:
    """Báo động khi K cửa sổ liên tiếp cùng một nhãn != normal với độ tin cậy >= thr."""

    def __init__(self, thr=0.7, k=2, normal_label="normal"):
        self.thr, self.k, self.normal = thr, k, normal_label
        self.run_label, self.run_len = None, 0

    def update(self, label, conf):
        if label != self.normal and conf >= self.thr:
            if label == self.run_label:
                self.run_len += 1
            else:
                self.run_label, self.run_len = label, 1
        else:
            self.run_label, self.run_len = None, 0
        return self.run_len >= self.k

    @property
    def alarm(self):
        return self.run_len >= self.k


@dataclass
class Segment:
    label: str
    start: float
    end: float
    max_conf: float = 0.0
    windows: list = field(default_factory=list)


def alerts(results, thr=0.7, k=2, normal_label="normal", window_sec=WINDOW_SEC):
    """results: list (giây_bắt_đầu, nhãn, độ_tin_cậy) theo thứ tự thời gian.
    Trả về (list cờ báo động từng cửa sổ, list Segment báo động).
    Một đoạn báo động bắt đầu từ cửa sổ đầu tiên của chuỗi K cửa sổ và kết thúc ở cuối cửa sổ cuối."""
    st = AlertState(thr, k, normal_label)
    flags, segs, cur = [], [], None
    for i, (s, label, conf) in enumerate(results):
        on = st.update(label, conf)
        flags.append(on)
        if on:
            if cur is None or cur.label != label:
                first = i - st.run_len + 1
                cur = Segment(label, results[first][0], s + window_sec)
                cur.windows = list(range(first, i + 1))
                cur.max_conf = max(results[j][2] for j in cur.windows)
                segs.append(cur)
            else:
                cur.end = s + window_sec
                cur.windows.append(i)
                cur.max_conf = max(cur.max_conf, conf)
        else:
            cur = None
    return flags, segs
