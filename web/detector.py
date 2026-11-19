"""Phát hiện trực tiếp: 1 luồng đọc khung + 1 luồng suy luận. Không chặn luồng web."""
import os
import sys
import threading
import time
from collections import deque

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core  # noqa: E402

from . import overlay  # noqa: E402

SAMPLE_MIN_GAP = 0.05      # chỉ tiền xử lý khung cách nhau >= 50 ms (đủ mịn để chọn 16 khung cách 187.5 ms)
BUFFER_SEC = 3.5           # giữ hơi hơn 3 giây
WINDOW_SPAN = core.SAMPLE_DT * (core.NUM_FRAMES - 1)   # 2.8125 s từ khung đầu đến khung cuối
SNAP_MAX_SIDE = 960        # ảnh chụp lưu kèm mỗi khung trong bộ đệm (để chụp khung tin cậy cao nhất)
STREAM_MAX_SIDE = 960
URL_RETRIES = 5


URL_TIMEOUT_MS = 5000


def describe_source(source):
    s = str(source)
    return f"camera số {s}" if s.isdigit() else s


def tcp_check(url, timeout=2.5):
    """Thử mở kết nối TCP tới host:port của URL -> 'open' | 'refused' | 'timeout' | 'unreachable'."""
    import socket
    from urllib.parse import urlparse
    try:
        u = urlparse(url)
        host, port = u.hostname, u.port or (443 if u.scheme == "https" else 554 if u.scheme == "rtsp" else 80)
    except ValueError:
        return "unreachable"
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return "open"
    except ConnectionRefusedError:
        return "refused"
    except socket.timeout:
        return "timeout"
    except OSError:
        return "unreachable"


def open_error_message(source, tried):
    """Thông báo lỗi dễ hiểu khi không mở được nguồn (URL: tự chẩn đoán cổng để báo đúng nguyên nhân)."""
    s = str(source)
    if s.isdigit():
        return (f"Không mở được camera số {s}. Có thể số camera không tồn tại, hoặc camera đang bị ứng dụng khác "
                "dùng (Zoom, Teams, Camera, DroidCam Client...). Hãy thử số khác hoặc đóng ứng dụng đó.")
    from urllib.parse import urlparse
    u = urlparse(s)
    where = f"{u.hostname}:{u.port or 80}"
    state = tcp_check(s)
    if state == "refused":
        return (f"Điện thoại ở {u.hostname} có trong mạng nhưng KHÔNG có DroidCam nhận kết nối ở cổng {u.port or 80}. "
                "Mở ứng dụng DroidCam trên điện thoại và để nó Ở TRÊN MÀN HÌNH (màn hình sáng, không chuyển sang app khác) "
                "cho tới khi hiện 'WiFi IP' và 'Port'; cấp quyền Camera cho DroidCam; kiểm tra số cổng DroidCam hiển thị "
                "(mặc định 4747) khớp với ô Cổng.")
    if state in ("timeout", "unreachable"):
        return (f"Không có thiết bị nào trả lời ở {where}. Kiểm tra: màn hình điện thoại đang SÁNG và DroidCam đang mở "
                "(khi khóa màn hình/tiết kiệm pin, Wi-Fi của điện thoại có thể ngủ); IP đúng như DroidCam đang hiển thị "
                "(IP có thể đổi sau khi điện thoại kết nối lại Wi-Fi); điện thoại và máy tính cùng một mạng Wi-Fi "
                "(không phải Wi-Fi khách, không dùng 4G); VPN trên máy tính hoặc điện thoại (ví dụ Cloudflare WARP) đang tắt.")
    return ("Kết nối được tới " + where + " nhưng không nhận được hình từ " + " hoặc ".join(tried) + ". "
            "Có thể DroidCam đang bận vì một thiết bị/trình duyệt/DroidCam Client khác đang xem (DroidCam chỉ cho "
            "1 kết nối): hãy đóng chúng rồi thử lại. Nếu bạn dùng ứng dụng khác DroidCam, đường dẫn video có thể khác /video.")


def open_source(source):
    """Mở camera số hoặc URL. Trả về (cap | None, url/số thực dùng, danh sách đã thử)."""
    source = str(source).strip()
    if source.isdigit():
        idx = int(source)
        cap = cv2.VideoCapture(idx)
        if not cap.isOpened() and os.name == "nt":
            cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
        if not cap.isOpened():
            return None, source, [source]
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap, source, [source]
    urls = [source]
    if source.rstrip("/").endswith("/video"):       # DroidCam: thử thêm /mjpegfeed
        urls.append(source.rstrip("/")[: -len("/video")] + "/mjpegfeed")
    params = [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, URL_TIMEOUT_MS, cv2.CAP_PROP_READ_TIMEOUT_MSEC, URL_TIMEOUT_MS]
    tried = []
    for u in urls:
        tried.append(u)
        cap = cv2.VideoCapture(u, cv2.CAP_FFMPEG, params) if "://" in u else cv2.VideoCapture(u)
        if cap.isOpened():
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            return cap, u, tried
        cap.release()
    return None, source, tried


def _downscale(frame, max_side):
    h, w = frame.shape[:2]
    s = max_side / max(h, w)
    if s >= 1:
        return frame
    return cv2.resize(frame, (round(w * s), round(h * s)), interpolation=cv2.INTER_AREA)


class LabelAlertState(core.AlertState):
    """Như core.AlertState nhưng THR và K có thể đặt RIÊNG cho từng nhãn.
    Báo động khi K_nhãn cửa sổ liên tiếp cùng một nhãn != normal, mỗi cửa sổ có độ tin cậy >= THR_nhãn.
    Nhãn chưa đặt riêng dùng THR/K chung."""

    def __init__(self, thr, k, normal_label="normal", thr_by=None, k_by=None):
        super().__init__(thr, k, normal_label)
        self.thr_by, self.k_by = dict(thr_by or {}), dict(k_by or {})

    def thr_for(self, label):
        v = self.thr_by.get(label)
        return float(v) if v is not None else float(self.thr)

    def k_for(self, label):
        v = self.k_by.get(label)
        return int(v) if v is not None else int(self.k)

    def update(self, label, conf):
        if label != self.normal and conf >= self.thr_for(label):
            if label == self.run_label:
                self.run_len += 1
            else:
                self.run_label, self.run_len = label, 1
        else:
            self.run_label, self.run_len = None, 0
        return self.alarm

    @property
    def alarm(self):
        return self.run_label is not None and self.run_len >= self.k_for(self.run_label)


def make_alert(settings, normal_label):
    return LabelAlertState(float(settings["thr"]), int(settings["k"]), normal_label,
                           settings.get("thr_by_label"), settings.get("k_by_label"))


class ModelHub:
    """Một model dùng chung; khóa để luồng trực tiếp và xử lý video tải lên không chạy đồng thời."""

    def __init__(self, model_dir=core.DEFAULT_MODEL_DIR):
        self.clf = core.Classifier(model_dir)
        self.lock = threading.Lock()
        self.labels = self.clf.labels
        self.normal_label = self.clf.normal_label

    def preprocess(self, frame):
        return self.clf.preprocess_frame(frame)

    def predict(self, frames):
        with self.lock:
            return self.clf.predict(frames)


class LiveDetector:
    """on_status(dict), on_alert(dict), on_state(dict), on_window(dict) được gọi từ luồng nền."""

    def __init__(self, hub, settings, on_status=None, on_alert=None, on_state=None, on_window=None):
        self.hub = hub
        self.settings = dict(settings)
        self.on_status = on_status or (lambda m: None)
        self.on_alert = on_alert or (lambda m: None)
        self.on_state = on_state or (lambda m: None)
        self.on_window = on_window or (lambda m: None)
        self._lock = threading.Lock()
        self._ctl = threading.Lock()          # start/stop tuần tự
        self._stop_evt = threading.Event()
        self._threads = []
        self._latest = None
        self._latest_id = 0
        self._jpeg = (-1, None)
        self._buf = deque()
        self._alert = make_alert(self.settings, hub.normal_label)
        self._last_alert = {}
        self._gen = 0                    # tăng mỗi khi bộ đệm bị đổ lại (tua/xoay) -> bỏ kết quả cũ
        self._last_end = None            # thời điểm cuối của cửa sổ phân tích gần nhất (đồng hồ nguồn)
        self._dropped = 0                # số cửa sổ phải bỏ vì CPU không theo kịp chu kỳ
        self._dropped_f = 0.0
        self._n_windows = 0
        self.state = {"running": False, "status": "stopped", "message": "Chưa bật nguồn", "source": None, "kind": None,
                      "fps": 0.0, "label": None, "conf": 0.0, "probs": {}, "alarm": False,
                      "infer_ms": None, "period": None, "ts": None}

    # ------------------------------------------------------------ điều khiển
    def update_settings(self, s):
        """Áp dụng ngay (không khởi động lại): thr, k, step, cooldown, rotate."""
        old_rot = int(self.settings.get("rotate", 0))
        self.settings.update(s)
        self._alert.thr, self._alert.k = float(self.settings["thr"]), int(self.settings["k"])
        self._alert.thr_by = dict(self.settings.get("thr_by_label") or {})     # ngưỡng/K riêng: áp dụng ngay
        self._alert.k_by = dict(self.settings.get("k_by_label") or {})
        if int(self.settings.get("rotate", 0)) != old_rot:      # đổi chiều xoay: bỏ khung cũ, bắt đầu lại chuỗi
            self._reset_analysis()

    def latest_frame(self):
        with self._lock:
            return None if self._latest is None else self._latest.copy()

    # --- móc cho lớp con (nguồn video file): đồng hồ nguồn + tạm dừng
    def _src_now(self):
        """Thời điểm hiện tại theo đồng hồ của nguồn (camera: đồng hồ máy)."""
        return time.monotonic()

    def _hold(self):
        return False

    def _reset_analysis(self):
        """Bỏ bộ đệm + chuỗi báo động (khi đổi xoay / tua video). Kết quả đang suy luận dở sẽ bị bỏ."""
        with self._lock:
            self._buf.clear()
            self._gen += 1
        self._alert.run_label, self._alert.run_len = None, 0
        self._last_end = None
        self.state.update(label=None, conf=0.0, probs={}, alarm=False)

    def start(self, source, kind=None, message=None):
        with self._ctl:
            self._stop_locked()
            source = str(source).strip()
            if not source or source == "none":
                self._set_state(status="stopped", message="Đã dừng", source=None)
                return
            self._stop_evt = stop = threading.Event()
            with self._lock:
                self._buf.clear()
                self._gen += 1
            self._alert = make_alert(self.settings, self.hub.normal_label)
            self._last_end, self._dropped, self._dropped_f, self._n_windows = None, 0, 0.0, 0
            self._set_state(running=True, status="opening", message=message or f"Đang mở {describe_source(source)}...",
                            source=source, kind=kind or self.settings.get("source_kind", "laptop"),
                            label=None, conf=0.0, probs={}, alarm=False, fps=0.0,
                            ratio=None, lagging=False, dropped=0, windows=0)
            self._threads = [threading.Thread(target=self._reader, args=(source, stop), daemon=True, name="reader"),
                             threading.Thread(target=self._infer_loop, args=(stop,), daemon=True, name="infer")]
            for t in self._threads:
                t.start()

    def stop(self, message="Đã dừng"):
        with self._ctl:
            self._stop_locked(message)

    def _stop_locked(self, message="Đã dừng"):
        self._stop_evt.set()
        for t in self._threads:
            t.join(timeout=5)
        self._threads = []
        if self.state["running"]:
            self._set_state(running=False, status="stopped", message=message, alarm=False, fps=0.0)

    def _set_state(self, **kw):
        self.state.update(kw)
        self.on_state(dict(self.state))

    # ------------------------------------------------------------ khung cho /stream
    def latest_id(self):
        return self._latest_id

    def jpeg(self, quality=75):
        """JPEG của khung mới nhất kèm lớp phủ (viền đỏ + huy hiệu nhãn) vẽ ở server;
        mã hóa 1 lần, dùng chung cho mọi người xem."""
        with self._lock:
            fid, frame = self._latest_id, self._latest
        if frame is None:
            return fid, None
        if self._jpeg[0] != fid:
            out = _downscale(frame, STREAM_MAX_SIDE)
            out = out.copy() if out is frame else out          # không vẽ đè lên khung gốc
            st = self.state
            overlay.draw(out, st["label"], st["conf"], st["alarm"], time.monotonic())
            ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, quality])
            if ok:
                self._jpeg = (fid, buf.tobytes())
        return self._jpeg

    # ------------------------------------------------------------ luồng đọc khung
    def _reader(self, source, stop):
        cap, used, tried = open_source(source)
        if stop.is_set():
            if cap is not None:
                cap.release()
            return
        if cap is None:
            self._set_state(running=False, status="error", message=open_error_message(source, tried))
            stop.set()
            return
        self._set_state(status="running", message=f"Đang chạy: {describe_source(used)}")
        source = used        # nếu /video không được mà /mjpegfeed được thì dùng /mjpegfeed khi mở lại
        is_url = not source.isdigit()
        last_sample, fails, retries = 0.0, 0, 0
        last_ok = time.monotonic()
        n, t_fps = 0, time.monotonic()
        try:
            while not stop.is_set():
                ok, frame = cap.read()
                if not ok or frame is None:
                    fails += 1
                    if fails < 30 and time.monotonic() - last_ok < 3.0:   # trục trặc ngắn: đọc tiếp
                        time.sleep(0.03)
                        continue
                    # mất tín hiệu: thử mở lại (URL) hoặc báo lỗi (camera)
                    cap.release()
                    if not is_url or retries >= URL_RETRIES:
                        msg = (f"Mất kết nối với {describe_source(source)} sau {retries} lần thử mở lại. "
                               "Kiểm tra điện thoại còn bật DroidCam, còn cùng Wi-Fi và không bị khóa màn hình/tiết kiệm pin."
                               if is_url else
                               f"Camera số {source} ngừng gửi hình (có thể bị rút ra hoặc ứng dụng khác chiếm).")
                        self._set_state(running=False, status="error", message=msg)
                        stop.set()
                        return
                    retries += 1
                    self._set_state(status="reconnecting",
                                    message=f"Mất kết nối, đang thử kết nối lại ({retries}/{URL_RETRIES})...")
                    if stop.wait(min(2 * retries, 8)):
                        return
                    cap, _, _ = open_source(source)
                    if cap is None:
                        cap = cv2.VideoCapture()   # rỗng -> read() trả False -> vòng thử tiếp theo
                    fails, last_ok = 0, time.monotonic()
                    continue
                last_ok = time.monotonic()
                if fails or retries:
                    if retries:
                        self._set_state(status="running", message=f"Đã kết nối lại: {describe_source(source)}")
                    fails, retries = 0, 0
                frame = core.rotate(frame, int(self.settings.get("rotate", 0)))
                t = time.monotonic()
                with self._lock:
                    self._latest = frame
                    self._latest_id += 1
                if t - last_sample >= SAMPLE_MIN_GAP:
                    last_sample = t
                    arr = self.hub.preprocess(frame)            # tiền xử lý 1 lần khi khung đến
                    snap = _downscale(frame, SNAP_MAX_SIDE)
                    with self._lock:
                        if self._buf and self._buf[-1][2].shape != snap.shape:
                            self._buf.clear()                   # đổi chiều xoay -> bỏ bộ đệm cũ
                        self._buf.append((t, arr, snap))
                        while self._buf and self._buf[0][0] < t - BUFFER_SEC:
                            self._buf.popleft()
                n += 1
                if t - t_fps >= 2:
                    self.state["fps"] = round(n / (t - t_fps), 1)
                    n, t_fps = 0, t
        finally:
            cap.release()

    # ------------------------------------------------------------ luồng suy luận
    def _infer_loop(self, stop):
        next_t = time.monotonic()
        while not stop.wait(max(0.0, next_t - time.monotonic())):
            t_begin = time.monotonic()
            if self._hold():                     # tạm dừng / hết video: không phân tích
                next_t = t_begin + 0.1
                continue
            with self._lock:
                items, gen = list(self._buf), self._gen
            src_now = self._src_now()
            if not items or items[-1][0] - items[0][0] < WINDOW_SPAN - 0.05 or src_now - items[-1][0] > 1.5:
                next_t = t_begin + 0.1           # chưa đủ 3 giây hoặc nguồn đang đứng
                continue
            times = np.array([it[0] for it in items])
            end = times[-1]
            if self._last_end is not None and end <= self._last_end:
                next_t = t_begin + 0.05          # không có khung mới từ lần trước
                continue
            # luôn lấy cửa sổ MỚI NHẤT (3 giây vừa qua); cửa sổ cũ chưa kịp phân tích thì bỏ qua
            targets = end - core.SAMPLE_DT * np.arange(core.NUM_FRAMES - 1, -1, -1)
            idxs = [int(np.abs(times - x).argmin()) for x in targets]
            t0 = time.perf_counter()
            label, conf, probs = self.hub.predict([items[i][1] for i in idxs])
            dt = time.perf_counter() - t0
            if stop.is_set():
                break
            if gen != self._gen:                 # đã tua trong lúc suy luận: bỏ kết quả cũ
                next_t = time.monotonic()
                continue
            step = float(self.settings.get("step", 2.0))
            period = max(step, dt * 1.15)       # không cho suy luận chiếm 100% CPU
            if self._last_end is not None:
                # số cửa sổ lẽ ra phải phân tích theo chu kỳ nhưng bị bỏ (khoảng cách > 1 chu kỳ), cộng dồn
                self._dropped_f += max(0.0, (end - self._last_end) / step - 1.0)
                self._dropped = int(self._dropped_f + 0.5)
            self._last_end = end
            self._n_windows += 1
            alarm = self._alert.update(label, conf)
            now = time.time()
            self.state.update(label=label, conf=conf, probs=probs, alarm=alarm, infer_ms=round(dt * 1000),
                              period=round(period, 2), ts=now, ratio=round(dt / step, 2),
                              lagging=dt * 1.15 > step, dropped=self._dropped, windows=self._n_windows,
                              win_src_end=round(end, 3))
            self.on_status(dict(self.state))
            # thời điểm thực (epoch) của cửa sổ; ảnh đại diện = khung giữa cửa sổ
            win_end = now - (src_now - end)
            self.on_window({"label": label, "conf": conf, "probs": probs, "alarm": alarm,
                            "run_len": self._alert.run_len, "win_start": win_end - core.WINDOW_SEC,
                            "win_end": win_end, "src_start": end - core.WINDOW_SEC, "src_end": end,
                            "frame": items[idxs[len(idxs) // 2]][2],
                            "source": self.state["source"],
                            "source_type": "phone" if self.settings.get("source_kind") == "phone" else "laptop"})
            if alarm:
                cd = float(self.settings.get("cooldown", 10))
                if now - self._last_alert.get(label, 0) >= cd:
                    self._last_alert[label] = now
                    self.on_alert({"label": label, "conf": conf, "ts": now, "source": self.state["source"]})
            next_t = t_begin + period
