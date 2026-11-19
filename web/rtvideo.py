"""Video tải lên ở chế độ THỜI GIAN THỰC: phát file đúng tốc độ gốc (1x) như một camera, phân tích đồng thời
bằng chính pipeline trực tiếp (bộ đệm 3 giây, 16 khung đều, cùng chu kỳ, THR, K, chống lặp).
- Đồng hồ nguồn = vị trí phát trong video (giây) -> cửa sổ, đoạn báo động, sự kiện đều tính theo giây video.
- CPU chậm hơn chu kỳ: luồng suy luận luôn lấy cửa sổ mới nhất, cửa sổ cũ bị bỏ; video vẫn chạy đều.
- Đoạn báo động bắt đầu từ ĐẦU CỬA SỔ ĐẦU TIÊN của chuỗi K cửa sổ (lúc hành động bắt đầu), không phải lúc banner hiện."""
import os
import sys
import threading
import time

import cv2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core  # noqa: E402
from app import open_video_file  # noqa: E402  (cùng cách đọc cờ xoay như app.py)

from . import db  # noqa: E402
from .detector import BUFFER_SEC, SAMPLE_MIN_GAP, SNAP_MAX_SIDE, LiveDetector, _downscale  # noqa: E402
from .recorder import save_snapshot  # noqa: E402

TICK_SEC = 0.25          # chu kỳ gửi vị trí phát cho trình duyệt


class VideoDetector(LiveDetector):
    """LiveDetector với nguồn là file video, đọc theo nhịp thời gian thực; có play/pause/seek."""

    def __init__(self, hub, settings, on_tick=None, on_end=None, on_seek=None, **kw):
        super().__init__(hub, settings, **kw)
        self.on_tick = on_tick or (lambda s: None)
        self.on_end = on_end or (lambda s: None)
        self.on_seek = on_seek or (lambda t: None)
        self._pos = 0.0
        self._playing = False
        self._ended = False
        self._seek_req = None
        self._start_t = 0.0
        self.video = None
        self.state.update(pos=0.0, duration=0.0, playing=False, ended=False, video_id=None, filename=None)

    def update_settings(self, s):
        """THR, K, chu kỳ, chống lặp dùng chung với camera; góc xoay thì KHÔNG (video tự xoay theo cờ trong file)."""
        super().update_settings({**s, "rotate": 0})

    # --- móc của LiveDetector
    def _src_now(self):
        return self._pos

    def _hold(self):
        return not self._playing or self._ended

    # --- điều khiển
    def open(self, video, t=0.0):
        self.video = video
        self._start_t, self._seek_req = max(0.0, float(t)), None
        self._playing, self._ended, self._pos = True, False, self._start_t
        self.state.update(video_id=video["id"], filename=video["filename"], pos=self._pos,
                          duration=(video.get("info") or {}).get("duration") or 0.0, playing=True, ended=False)
        path = os.path.join(db.UPLOAD_DIR, os.path.basename(video["path"]))
        self.start(path, kind="video", message=f"Đang mở {video['filename']}...")

    def play(self):
        if self._ended:
            self.seek(0.0)
            return
        self._playing = True
        self._set_state(playing=True, status="running", message=f"Đang phát: {self.state.get('filename')}")

    def pause(self):
        self._playing = False
        self._set_state(playing=False, status="paused", message="Tạm dừng (dừng cả phân tích)")

    def seek(self, t):
        dur = self.state.get("duration") or 0
        t = max(0.0, min(float(t), max(0.0, dur - 0.05) if dur else float(t)))
        self._seek_req = t
        self._playing, self._ended = True, False

    def close(self):
        # xóa kết quả của phiên cũ TRƯỚC khi báo trạng thái, để không lẫn sang video sau
        self.state.update(label=None, conf=0.0, probs={}, alarm=False, windows=0, dropped=0, ratio=None,
                          lagging=False, infer_ms=None, period=None, win_src_end=None)
        self.stop("Đã đóng video")
        self.video = None
        self.state.update(video_id=None, filename=None, playing=False, ended=False, pos=0.0)

    # --- luồng đọc khung theo nhịp thời gian thực
    def _reader(self, path, stop):
        try:
            cap, rot, _ = open_video_file(path)
        except RuntimeError:
            self._set_state(running=False, status="error", message="Không đọc được file video (hỏng hoặc codec không hỗ trợ)")
            stop.set()
            return
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        if not (1 <= fps <= 240):
            fps = 30.0
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        dur = n / fps if n else (self.state.get("duration") or 0.0)
        self.state["duration"] = round(dur, 3)

        def goto(t):
            i = max(0, int(round(t * fps)))
            cap.set(cv2.CAP_PROP_POS_FRAMES, i)
            return i

        idx = goto(self._start_t) if self._start_t > 0 else 0
        clock0, wall0 = idx / fps, time.monotonic()
        last_sample, last_tick, nshown, t_fps = -1e9, 0.0, 0, time.monotonic()
        self._set_state(status="running", message=f"Đang phát: {self.state.get('filename')}", playing=True)
        try:
            while not stop.is_set():
                req = self._seek_req
                if req is not None:                       # tua: nhảy tới giây đó, đổ lại bộ đệm 3 giây
                    self._seek_req = None
                    idx = goto(req)
                    clock0, wall0, last_sample = idx / fps, time.monotonic(), -1e9
                    self._pos = clock0
                    self._reset_analysis()
                    self.on_seek(clock0)
                    self._set_state(status="running", message=f"Đang phát từ {clock0:.1f}s (đang đổ lại bộ đệm 3 giây)",
                                    playing=True, ended=False, pos=round(clock0, 3))
                if not self._playing or self._ended:      # tạm dừng / hết video: đứng yên ở khung hiện tại
                    clock0, wall0 = idx / fps, time.monotonic()
                    time.sleep(0.03)
                    continue
                target = clock0 + (time.monotonic() - wall0)   # vị trí lẽ ra phải tới (1x)
                ft = idx / fps
                if ft > target:                           # sớm hơn: chờ tới đúng giờ của khung
                    time.sleep(min(ft - target, 0.01))
                    continue
                if target - ft > 3 / fps:                 # đọc/giải mã chậm hơn 1x: bỏ khung để không bị chậm dần
                    if not cap.grab():
                        self._finish(ft)
                        continue
                    idx += 1
                    continue
                ok, frame = cap.read()
                if not ok or frame is None:
                    self._finish(ft)
                    continue
                idx += 1
                frame = core.rotate(frame, rot)
                with self._lock:
                    self._latest = frame
                    self._latest_id += 1
                self._pos = ft
                if ft - last_sample >= SAMPLE_MIN_GAP:     # tiền xử lý 1 lần khi khung đến (như camera)
                    last_sample = ft
                    arr = self.hub.preprocess(frame)
                    snap = _downscale(frame, SNAP_MAX_SIDE)
                    with self._lock:
                        self._buf.append((ft, arr, snap))
                        while self._buf and self._buf[0][0] < ft - BUFFER_SEC:
                            self._buf.popleft()
                nshown += 1
                now = time.monotonic()
                if now - t_fps >= 2:
                    self.state["fps"] = round(nshown / (now - t_fps), 1)
                    nshown, t_fps = 0, now
                if now - last_tick >= TICK_SEC:
                    last_tick = now
                    self.state["pos"] = round(ft, 3)
                    self.on_tick(dict(self.state))
        finally:
            cap.release()

    def _finish(self, ft):
        """Hết video: dừng ở khung cuối, giữ nguyên kết quả hiển thị."""
        self._ended, self._playing = True, False
        self._pos = max(self._pos, ft)
        self._set_state(status="ended", message="Hết video — bấm Phát lại để xem lại", playing=False, ended=True,
                        pos=round(self.state.get("duration") or self._pos, 3))
        self.on_end(dict(self.state))


class VideoRecorder:
    """Gộp cửa sổ báo động liên tiếp cùng nhãn thành đoạn (giây video) + sự kiện trong Lịch sử.
    Bắt đầu đoạn = đầu cửa sổ ĐẦU TIÊN của chuỗi. Phân tích lại cùng chỗ (sau khi tua) thì nối vào đoạn cũ."""

    def __init__(self, on_change=None):
        self.on_change = on_change or (lambda kind, seg: None)
        self._lock = threading.Lock()
        self.video = None
        self.segments = []
        self._reset_run()

    def _reset_run(self):
        self.run_label, self.run_start, self.best, self.cur = None, None, None, None

    def begin(self, video):
        with self._lock:
            self.video = video
            self.segments = []
            self._reset_run()

    def close_run(self, end_at=None):
        """Đóng chuỗi đang mở. end_at: hết video khi báo động vẫn bật -> kéo đoạn tới cuối video."""
        with self._lock:
            if self.cur is not None:
                if end_at is not None and end_at > self.cur["end"]:
                    self.cur["end"] = round(end_at, 2)
                    db.update_event(self.cur["event_id"], end_ts=self._epoch(self.cur["end"]), video_end=self.cur["end"])
                self.on_change("closed", self.cur)
            self._reset_run()

    def _epoch(self, s):
        return self.video["created"] + s          # thời gian trong Lịch sử = lúc tải lên + vị trí trong video

    def _snapshot(self, seg, w):
        big, small = save_snapshot(seg["event_id"], seg["label"], self._epoch(seg["start"]), w["frame"])
        db.update_event(seg["event_id"], max_conf=w["conf"], probs=w["probs"], best_ts=self._epoch(w["src_end"]),
                        snapshot=big, thumb=small)
        seg["max_conf"], seg["best_s"] = round(w["conf"], 4), round(w["src_start"], 2)

    def window(self, w):
        if self.video is None:
            return
        with self._lock:
            if w["run_len"] <= 0:
                if self.cur is not None:
                    self.on_change("closed", self.cur)
                self._reset_run()
                return
            if w["label"] != self.run_label or w["run_len"] == 1:          # chuỗi mới
                if self.cur is not None:
                    self.on_change("closed", self.cur)
                self._reset_run()
                self.run_label, self.run_start = w["label"], max(0.0, w["src_start"])
            better = self.best is None or w["conf"] > self.best["conf"]
            if better:
                self.best = w
            if not w["alarm"]:
                return
            end = w["src_end"]
            if self.cur is None:
                seg = next((g for g in self.segments if g["label"] == w["label"]
                            and g["start"] <= end + 0.5 and g["end"] >= self.run_start - 0.5), None)
                if seg is None:                                             # đoạn mới -> sự kiện mới
                    seg = {"label": w["label"], "start": round(self.run_start, 2), "end": round(end, 2),
                           "max_conf": 0.0, "best_s": None, "event_id": None}
                    seg["event_id"] = db.insert_event(
                        w["label"], self._epoch(seg["start"]), self._epoch(end), self.best["conf"],
                        self.video["filename"], self.best["probs"], self._epoch(self.best["src_end"]),
                        video_start=seg["start"], video_end=seg["end"], source_type="video", video_id=self.video["id"])
                    self.segments.append(seg)
                    self._snapshot(seg, self.best)
                    kind = "started"
                else:                                                       # nối vào đoạn đã có (sau khi tua)
                    seg["start"], seg["end"] = round(min(seg["start"], self.run_start), 2), round(max(seg["end"], end), 2)
                    db.update_event(seg["event_id"], start_ts=self._epoch(seg["start"]), end_ts=self._epoch(seg["end"]),
                                    video_start=seg["start"], video_end=seg["end"])
                    if self.best["conf"] > seg["max_conf"]:
                        self._snapshot(seg, self.best)
                    kind = "updated"
                self.cur = seg
            else:                                                           # đoạn đang diễn ra: kéo dài
                seg = self.cur
                seg["end"] = round(max(seg["end"], end), 2)
                db.update_event(seg["event_id"], end_ts=self._epoch(seg["end"]), video_end=seg["end"])
                if better and w["conf"] > seg["max_conf"]:
                    self._snapshot(seg, w)
                kind = "updated"
            self.on_change(kind, seg)


class Player:
    """Một phiên phát video thời gian thực (dùng chung model với camera qua khóa của ModelHub).
    mode='pause': tạm dừng camera trong lúc phát, bật lại khi hết video hoặc đóng video.
    mode='share': chạy xen kẽ với camera (hai bên lần lượt dùng model, cả hai chậm hơn)."""

    def __init__(self, hub, live, settings, publish, get_settings, label_info):
        self.live, self.publish, self.get_settings, self.label_info = live, publish, get_settings, label_info
        self.mode = "share"
        self._paused_live = False
        self.recorder = VideoRecorder(on_change=self._seg_changed)
        self.det = VideoDetector(
            hub, settings,
            on_status=lambda s: self.publish("player", self._view(s)),
            on_state=lambda s: self.publish("player", self._view(s)),
            on_tick=lambda s: self.publish("player", self._view(s)),
            on_alert=lambda a: self.publish("alert", {**a, **self.label_info(a["label"]),
                                                      "source": self.det.state.get("filename") or a["source"]}),
            on_window=self.recorder.window,
            on_end=self._ended,
            on_seek=lambda t: self.recorder.close_run())

    def _view(self, s=None):
        s = dict(s or self.det.state)
        s["segments"] = [dict(g) for g in self.recorder.segments]
        s["mode"] = self.mode
        s["camera_paused"] = self._paused_live
        return s

    def state(self):
        return self._view()

    def _seg_changed(self, kind, seg):
        self.publish("event", {"kind": kind, "event": {"id": seg["event_id"], "label": seg["label"],
                                                       "video_id": self.recorder.video and self.recorder.video["id"]}})
        self.publish("player", self._view())

    def _pause_camera_if_needed(self):
        if self.mode == "pause" and self.live.state["running"]:
            self.live.stop("Tạm dừng camera trong lúc phát video (sẽ tự bật lại)")
            self._paused_live = True

    def _resume_camera(self):
        if self._paused_live:
            self._paused_live = False
            src = self.get_settings()["source"]
            if src != "none" and not self.live.state["running"]:
                threading.Thread(target=self.live.start, args=(src,), daemon=True).start()

    def _ended(self, s):
        self.recorder.close_run(end_at=s.get("duration") if s.get("alarm") else None)
        self._resume_camera()                     # hết video: trả camera lại
        self.publish("player", self._view())

    # --- API
    def open(self, video, t=0.0, mode="share"):
        self.det.close()
        self.recorder.close_run()
        self.mode = mode if mode in ("pause", "share") else "share"
        self.det.update_settings(self.get_settings())
        self._pause_camera_if_needed()
        self.recorder.begin(video)
        self.det.open(video, t)
        return self._view()

    def play(self):
        self._pause_camera_if_needed()            # phát lại sau khi camera đã được trả lại
        self.det.play()
        return self._view()

    def pause(self):
        self.det.pause()
        return self._view()

    def seek(self, t):
        self._pause_camera_if_needed()
        self.det.seek(t)
        return self._view()

    def close(self):
        self.recorder.close_run()
        self.det.close()
        self._resume_camera()
        self.publish("player", self._view())
        return self._view()

    def is_open(self, video_id):
        return self.det.state.get("video_id") == video_id and self.det.state.get("running")
