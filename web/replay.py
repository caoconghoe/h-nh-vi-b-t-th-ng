"""Phát lại video đã phân tích qua MJPEG phía server (không dùng thẻ <video>: iPhone .MOV có thể là HEVC).
Đọc bằng OpenCV, xoay đúng chiều (cùng cách app.py), đúng tốc độ fps gốc; mỗi khung được vẽ lớp phủ:
trong đoạn báo động -> viền đỏ + nhãn; ngoài -> huy hiệu 'Bình thường'."""
import os
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core  # noqa: E402
from app import open_video_file  # noqa: E402

from . import overlay  # noqa: E402

MAX_SIDE = 960
MAX_OUT_FPS = 30          # video 60 fps: gửi 30 khung/giây (bỏ bớt khung, vẫn đúng tốc độ thời gian thực)
JPEG_QUALITY = 78


def make_labeler(video):
    """Trả về hàm at(t) -> (label, conf, alarm) cho thời điểm t (giây) trong video."""
    windows = video.get("windows") or []
    segs = video.get("segments") or []
    if not windows:
        return lambda t: (None, 0.0, False)
    centers = np.array([w["s"] for w in windows]) + core.WINDOW_SEC / 2

    def at(t):
        for g in segs:
            if g["start"] <= t <= g["end"]:
                # độ tin cậy: cửa sổ cùng nhãn thuộc đoạn có tâm gần t nhất
                idx = [i for i in g.get("windows", []) if windows[i]["label"] == g["label"]]
                if idx:
                    i = min(idx, key=lambda i: abs(centers[i] - t))
                    return g["label"], windows[i]["conf"], True
                return g["label"], g["max_conf"], True
        w = windows[int(np.abs(centers - t).argmin())]
        return w["label"], w["conf"], False

    return at


def _prepare(frame, rot):
    frame = core.rotate(frame, rot)
    h, w = frame.shape[:2]
    s = MAX_SIDE / max(h, w)
    if s < 1:
        frame = cv2.resize(frame, (round(w * s), round(h * s)), interpolation=cv2.INTER_AREA)
    return frame


def _open_at(path, t):
    cap, rot, _ = open_video_file(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    if not (1 <= fps <= 240):
        fps = 30.0
    start = max(0, int(round(t * fps)))
    if start:
        cap.set(cv2.CAP_PROP_POS_FRAMES, start)   # tua 1 lần khi bắt đầu phát
    return cap, rot, fps, start


def still_jpeg(path, video, t, with_overlay=True):
    """Một khung tĩnh tại giây t (có lớp phủ nếu video đã phân tích xong)."""
    try:
        cap, rot, fps, start = _open_at(path, t)
    except RuntimeError:
        return None
    try:
        ok, frame = cap.read()
        if not ok and start:                       # quá cuối video: lấy khung cuối (nhãn theo thời điểm khung cuối)
            n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, n - 1))
            ok, frame = cap.read()
            t = max(0, n - 1) / fps
    finally:
        cap.release()
    if not ok or frame is None:
        return None
    frame = _prepare(frame, rot)
    if with_overlay and video.get("windows"):
        label, conf, alarm = make_labeler(video)(t)
        overlay.draw(frame, label, conf, alarm, t)
    return cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])[1].tobytes()


def iter_mjpeg(path, video, t0, shutdown_evt, part):
    """Generator đồng bộ (Starlette chạy ở threadpool): từng phần MJPEG, giữ nhịp theo fps gốc."""
    at = make_labeler(video)
    cap, rot, fps, start = _open_at(path, t0)
    every = max(1, round(fps / MAX_OUT_FPS))
    wall0 = time.monotonic()
    idx = start
    try:
        while not shutdown_evt.is_set():
            if not cap.grab():
                break
            rel = (idx - start) / fps                  # giây kể từ lúc bắt đầu phát
            due = wall0 + rel
            now = time.monotonic()
            if (idx - start) % every == 0 and now - due < 0.25:   # trễ quá thì bỏ khung để bắt kịp thời gian thực
                ok, frame = cap.retrieve()
                if ok and frame is not None:
                    t = idx / fps
                    frame = _prepare(frame, rot)
                    label, conf, alarm = at(t)
                    overlay.draw(frame, label, conf, alarm, t)
                    jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])[1].tobytes()
                    wait = due - time.monotonic()
                    if wait > 0:
                        time.sleep(wait)
                    yield part(jpg)
            idx += 1
    finally:
        cap.release()
