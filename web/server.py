"""Web cục bộ phát hiện hành vi bất thường: trang Giám sát (camera laptop / điện thoại / video tải lên),
Lịch sử, Cài đặt.
Chạy:  python -m uvicorn web.server:app            (chỉ máy này: http://127.0.0.1:8000)
       python -m web.server --host 0.0.0.0         (cho điện thoại cùng Wi-Fi xem -> AI CÙNG MẠNG CŨNG XEM ĐƯỢC)
"""
import asyncio
import base64
import json
import os
import re
import socket
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Dict, Optional

import cv2
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db
from . import uploads as up
from . import replay
from .detector import LiveDetector, ModelHub, describe_source, open_error_message, open_source
from .overlay import label_info
from .recorder import EventRecorder
from .rtvideo import Player
from .probs import ProbSession
import core  # noqa: E402  (detector đã thêm thư mục gốc vào sys.path)

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")

# ------------------------------------------------------------------ nhật ký lỗi (để biết vì sao server dừng)
import atexit  # noqa: E402
import faulthandler  # noqa: E402
import traceback  # noqa: E402

os.makedirs(db.DATA_DIR, exist_ok=True)
ERROR_LOG = os.path.join(db.DATA_DIR, "server_errors.log")


def _log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, file=sys.stderr, flush=True)
    try:
        with open(ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


_crash_file = open(ERROR_LOG, "a", encoding="utf-8")
faulthandler.enable(file=_crash_file, all_threads=True)      # Python chết ở tầng C (OpenCV/FFmpeg): ghi vết vào file


def _thread_excepthook(a):
    _log(f"LỖI trong luồng nền '{a.thread.name if a.thread else '?'}':\n"
         + "".join(traceback.format_exception(a.exc_type, a.exc_value, a.exc_traceback)))


threading.excepthook = _thread_excepthook
_STOP_LOGGED = threading.Event()


def _log_stop(reason):
    if not _STOP_LOGGED.is_set():
        _STOP_LOGGED.set()
        _log(f"Server dừng {reason}")


# thoát ở tầng Python mà không qua bước tắt gọn của app (ví dụ lỗi khi khởi động)
_STARTED = threading.Event()
atexit.register(lambda: _STARTED.is_set() and _log_stop("(thoát bất thường, không qua bước tắt gọn)"))


# ------------------------------------------------------------------ SSE broker
class Broker:
    """Phát tin nhắn từ luồng nền tới mọi client SSE (thread-safe)."""

    def __init__(self):
        self.loop = None
        self.queues = set()

    def publish(self, event, data):
        if self.loop is None:
            return
        msg = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        self.loop.call_soon_threadsafe(self._fanout, msg)

    def _fanout(self, msg):
        for q in list(self.queues):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass


broker = Broker()
PROBS = {"live": ProbSession("clock"), "video": ProbSession("video")}
SHUTDOWN = threading.Event()
hub: ModelHub = None
live: LiveDetector = None
recorder: EventRecorder = None
player: Player = None


def _hook_signals():
    """Ctrl+C: báo các luồng /stream, /events, /player/stream, /replay kết thúc để uvicorn không treo ở
    'Waiting for connections to close'. Uvicorn đã gắn hàm xử lý tín hiệu TRƯỚC khi nạp app, nên phải bọc
    chính hàm đang được gắn (gọi trong lifespan, ở luồng chính)."""
    import signal
    sigs = [signal.SIGINT, signal.SIGTERM] + ([signal.SIGBREAK] if hasattr(signal, "SIGBREAK") else [])
    for sig in sigs:
        try:
            prev = signal.getsignal(sig)
            if getattr(prev, "_anomaly_hook", False):
                continue

            def handler(s, f, prev=prev):
                SHUTDOWN.set()
                if callable(prev):
                    prev(s, f)
                elif prev == signal.SIG_DFL and s == signal.SIGINT:
                    raise KeyboardInterrupt
            handler._anomaly_hook = True
            signal.signal(sig, handler)
        except (ValueError, OSError):      # không phải luồng chính / tín hiệu không hỗ trợ
            pass


def _lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))   # không gửi gói nào, chỉ để lấy IP card mạng
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "<IP-máy-tính>"


def _warn_host():
    argv = sys.argv
    host = argv[argv.index("--host") + 1] if "--host" in argv and argv.index("--host") + 1 < len(argv) else "127.0.0.1"
    if host not in ("127.0.0.1", "localhost", "::1"):
        port = argv[argv.index("--port") + 1] if "--port" in argv else "8000"
        print("\n" + "!" * 72)
        print(f"  CẢNH BÁO: server đang lắng nghe trên {host} (không chỉ máy này).")
        print("  BẤT KỲ AI CÙNG MẠNG Wi-Fi đều xem được camera, video, lịch sử, ảnh chụp và đổi cài đặt.")
        print("  Không có mật khẩu. Chỉ dùng trong mạng tin cậy, tắt khi không cần.")
        print(f"  Từ điện thoại mở: http://{_lan_ip()}:{port}")
        print("!" * 72 + "\n", flush=True)


def _probs_add(source, w):
    e, mx = PROBS[source].add(w)
    broker.publish("probs", {"source": source, "w": e, "max": mx})


@asynccontextmanager
async def lifespan(app):
    global hub, live, recorder, player
    _hook_signals()
    _log(f"Server khởi động (PID {os.getpid()}). Nhật ký lỗi: {ERROR_LOG}")
    _STARTED.set()
    _warn_host()
    broker.loop = asyncio.get_running_loop()
    db.init_db()
    recorder = EventRecorder(on_change=lambda kind, ev: broker.publish(
        "event", {"kind": kind, "event": {**ev, **label_info(ev["label"])}}))
    settings = db.get_settings()
    print("Đang nạp model...", flush=True)
    hub = await asyncio.to_thread(ModelHub)
    print(f"Đã nạp model. Nhãn: {hub.labels}", flush=True)

    def on_alert(a):
        broker.publish("alert", {**a, **label_info(a["label"])})

    def on_state(s):
        if s.get("status") in ("error", "reconnecting"):
            _log(f"Nguồn trực tiếp [{s.get('status')}]: {s.get('message')}")
        if not s["running"]:
            recorder.close()            # dừng / lỗi nguồn -> đóng sự kiện đang mở
        broker.publish("state", s)

    live = LiveDetector(hub, settings,
                        on_status=lambda s: broker.publish("status", s),
                        on_alert=on_alert, on_state=on_state,
                        on_window=lambda w: (recorder.window(w), _probs_add("live", w)))
    up.clean_orphans()
    player = Player(hub, live, settings, broker.publish, db.get_settings, label_info)
    _rec_window = player.det.on_window
    player.det.on_window = lambda w: (_rec_window(w), _probs_add("video", w))
    if settings["source"] != "none":
        await asyncio.to_thread(live.start, settings["source"])
    yield
    SHUTDOWN.set()
    await asyncio.to_thread(player.close)
    await asyncio.to_thread(live.stop)
    recorder.close()
    _log_stop("(tắt gọn: Ctrl+C / tín hiệu dừng)")


os.makedirs(db.SNAP_DIR, exist_ok=True)
app = FastAPI(title="Giám sát hành vi bất thường", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.mount("/snapshots", StaticFiles(directory=db.SNAP_DIR), name="snapshots")   # ảnh chỉ phục vụ cục bộ

FIELD_VI = {"thr": "Ngưỡng THR", "k": "K", "step": "Chu kỳ suy luận", "cooldown": "Thời gian chờ chống lặp",
            "video_step": "Bước phân tích video", "rotate": "Góc xoay", "phone_rotate": "Góc xoay",
            "source": "Nguồn", "kind": "Loại nguồn", "file": "File video", "mode": "Chế độ",
            "laptop_index": "Số camera laptop", "phone_index": "Số camera ảo", "phone_ip": "IP điện thoại",
            "phone_port": "Cổng", "phone_mode": "Cách kết nối điện thoại", "t": "Thời điểm",
            "thr_by_label": "Ngưỡng riêng", "k_by_label": "K riêng"}


@app.exception_handler(RequestValidationError)
async def _validation_vi(request, exc):
    """Thông báo lỗi dữ liệu nhập bằng tiếng Việt."""
    msgs = []
    for e in exc.errors():
        name = FIELD_VI.get(str(e["loc"][-1]), str(e["loc"][-1]))
        ctx = e.get("ctx") or {}
        if "le" in ctx or "ge" in ctx:
            msgs.append(f"{name} phải trong khoảng cho phép"
                        + (f" (≥ {ctx['ge']})" if "ge" in ctx else "") + (f" (≤ {ctx['le']})" if "le" in ctx else ""))
        elif e["type"] == "missing":
            msgs.append(f"Thiếu {name}")
        else:
            msgs.append(f"{name} không hợp lệ")
    return JSONResponse({"detail": "; ".join(msgs)}, status_code=422)


# ------------------------------------------------------------------ trang
@app.get("/")
def page_monitor():
    return FileResponse(os.path.join(STATIC, "monitor.html"))


@app.get("/history")
def page_history():
    return FileResponse(os.path.join(STATIC, "history.html"))


@app.get("/settings")
def page_settings():
    return FileResponse(os.path.join(STATIC, "settings.html"))


@app.get("/upload")
def page_upload_old():
    return RedirectResponse("/?tab=video")


# ------------------------------------------------------------------ nhãn, trạng thái, cài đặt
@app.get("/api/labels")
def api_labels():
    return [{"name": n, **label_info(n), "alert": n != hub.normal_label} for n in hub.labels]


@app.get("/api/state")
def api_state():
    return live.state


@app.get("/api/settings")
def api_settings():
    return db.get_settings()


_URL_RE = re.compile(r"^(https?|rtsp)://[\w.\-\[\]:]+(:\d+)?(/\S*)?$", re.I)
_HOST_RE = re.compile(r"^[\w.\-]+$")


def _check_source(src):
    src = (src or "").strip()
    if src == "none" or src.isdigit() or _URL_RE.match(src):
        return src
    raise HTTPException(400, "Nguồn không hợp lệ: nhập số camera (0, 1, ...) hoặc URL dạng http://IP:4747/video")


class SettingsBody(BaseModel):
    thr: Optional[float] = Field(None, ge=0.05, le=0.99)
    k: Optional[int] = Field(None, ge=1, le=10)
    step: Optional[float] = Field(None, ge=0.5, le=10)
    cooldown: Optional[float] = Field(None, ge=0, le=3600)
    video_step: Optional[float] = Field(None, ge=0.25, le=5)
    thr_by_label: Optional[Dict[str, Optional[float]]] = None   # {nhãn: ngưỡng | null = dùng THR chung}
    k_by_label: Optional[Dict[str, Optional[int]]] = None       # {nhãn: K | null = dùng K chung}
    laptop_index: Optional[str] = Field(None, pattern=r"^\d{1,2}$")
    phone_mode: Optional[str] = Field(None, pattern="^(url|virtual)$")
    phone_ip: Optional[str] = Field(None, max_length=200)
    phone_port: Optional[int] = Field(None, ge=1, le=65535)
    phone_index: Optional[str] = Field(None, pattern=r"^\d{1,2}$")
    phone_rotate: Optional[int] = None


def _apply_settings(new):
    if "phone_rotate" in new and new["phone_rotate"] not in (0, 90, 180, 270):
        raise HTTPException(400, "Góc xoay chỉ được 0, 90, 180 hoặc 270")
    if new.get("phone_ip"):
        ip = new["phone_ip"].strip()
        if not (_URL_RE.match(ip) or _HOST_RE.match(ip.split(":")[0])):
            raise HTTPException(400, "IP điện thoại không hợp lệ (ví dụ 192.168.1.5 hoặc http://192.168.1.5:4747/video)")
        new["phone_ip"] = ip
    alert_labels = [l for l in hub.labels if l != hub.normal_label]
    for key, lo, hi, cast in (("thr_by_label", 0.05, 0.99, float), ("k_by_label", 1, 10, int)):
        if key in new:
            clean = {}
            for lab, v in (new[key] or {}).items():
                if lab not in alert_labels:
                    raise HTTPException(400, f"Nhãn không hợp lệ: {lab} (chỉ: {', '.join(alert_labels)})")
                if v is None:
                    continue                               # để trống = dùng giá trị chung
                if not (lo <= v <= hi):
                    name = 'Ngưỡng' if key == 'thr_by_label' else 'K'
                    raise HTTPException(400, f"{name} của {label_info(lab)['display']} phải từ {lo} đến {hi}")
                clean[lab] = cast(v)
            new[key] = clean
    cur = db.get_settings()
    if "phone_rotate" in new and cur["source_kind"] == "phone":
        new["rotate"] = new["phone_rotate"]            # đang chạy điện thoại: xoay áp dụng ngay
    saved = db.save_settings(new)
    live.update_settings(saved)                        # THR/K/chu kỳ/chống lặp/xoay: áp dụng ngay
    player.det.update_settings(saved)                  # video thời gian thực dùng cùng THR/K/chu kỳ
    return saved


@app.put("/api/settings")
def api_settings_put(body: SettingsBody):
    saved = _apply_settings(body.model_dump(exclude_none=True))
    broker.publish("settings", {"thr": saved["thr"], "k": saved["k"]})
    return {"settings": saved, "state": live.state}


def _phone_source(s):
    if s["phone_mode"] == "virtual":
        return str(s["phone_index"])
    ip = (s["phone_ip"] or "").strip()
    if not ip:
        raise HTTPException(400, "Chưa nhập IP điện thoại (xem IP trên màn hình ứng dụng DroidCam).")
    if "://" in ip:
        return _check_source(ip)
    host = ip if ":" in ip else f"{ip}:{s['phone_port']}"
    return _check_source(f"http://{host}/video")


class StartBody(SettingsBody):
    kind: str = Field("laptop", pattern="^(laptop|phone)$")


@app.post("/api/live/start")
def api_live_start(body: StartBody):
    """Bật camera laptop hoặc điện thoại (lưu các ô nhập của tab trước)."""
    fields = body.model_dump(exclude_none=True)
    kind = fields.pop("kind")
    s = _apply_settings(fields) if fields else db.get_settings()
    if kind == "laptop":
        src, rot = str(s["laptop_index"]), 0
    else:
        src, rot = _phone_source(s), int(s["phone_rotate"])
    s = db.save_settings({"source": src, "source_kind": kind, "rotate": rot})
    live.update_settings(s)
    live.start(src)
    return live.state


@app.post("/api/live/stop")
def api_live_stop():
    db.save_settings({"source": "none", "source_kind": "none"})   # khởi động lại server sẽ không tự bật camera
    live.stop()
    return live.state


class TestBody(BaseModel):
    source: str
    rotate: int = 0


@app.post("/api/source/test")
def api_source_test(body: TestBody):
    """Mở thử nguồn, đọc 1 khung, trả ảnh xem trước (đã xoay) để kiểm tra chiều."""
    src = _check_source(body.source)
    if src == "none":
        raise HTTPException(400, "Chọn một nguồn để kiểm tra")
    rot = body.rotate if body.rotate in (0, 90, 180, 270) else 0
    t0 = time.perf_counter()
    if live.state["running"] and live.state["source"] == src:
        # nguồn đang chạy (camera/DroidCam không cho mở 2 lần): dùng khung hiện tại
        frame = live.latest_frame()
        used = src
        if frame is None:
            return {"ok": False, "message": "Nguồn đang mở nhưng chưa có khung hình, thử lại sau vài giây."}
        frame = core.rotate(frame, (rot - int(live.settings.get("rotate", 0))) % 360)
        note = "Nguồn này đang chạy trực tiếp; ảnh lấy từ luồng hiện tại."
    else:
        cap, used, tried = open_source(src)
        if cap is None:
            return {"ok": False, "message": open_error_message(src, tried)}
        ok, frame = False, None
        try:
            for _ in range(15):                 # vài khung đầu của webcam có thể rỗng/tối
                ok, frame = cap.read()
                if ok and frame is not None:
                    break
        finally:
            cap.release()
        if not ok or frame is None:
            return {"ok": False, "message": f"Mở được {describe_source(used)} nhưng không đọc được khung hình nào."}
        frame = core.rotate(frame, rot)
        note = ""
    h, w = frame.shape[:2]
    small = cv2.resize(frame, (round(w * 480 / max(h, w)), round(h * 480 / max(h, w)))) if max(h, w) > 480 else frame
    jpg = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 80])[1].tobytes()
    msg = (f"Kết nối được {describe_source(used)} · khung {w}x{h} "
           f"({'dọc' if h > w else 'ngang'}) · {time.perf_counter() - t0:.1f}s")
    if used != src:
        msg += f". Lưu ý: {src} không được, đã dùng {used}."
    return {"ok": True, "message": msg, "note": note, "used": used, "width": w, "height": h,
            "preview": "data:image/jpeg;base64," + base64.b64encode(jpg).decode()}


# ------------------------------------------------------------------ luồng trực tiếp + SSE
BOUNDARY = "frame"


def _part(jpg):
    return f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpg)}\r\n\r\n".encode() + jpg + b"\r\n"


@app.get("/stream")
async def stream():
    """MJPEG trực tiếp (đã vẽ viền + nhãn ở server): tối đa ~20 khung/giây, mỗi khung mã hóa 1 lần dùng chung."""
    async def gen():
        last = -1
        while not SHUTDOWN.is_set():
            fid = live.latest_id()
            if fid != last and live.state["running"]:
                fid, jpg = await asyncio.to_thread(live.jpeg)
                if jpg is not None:
                    last = fid
                    yield _part(jpg)
            await asyncio.sleep(0.05)

    return StreamingResponse(gen(), media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
                             headers={"Cache-Control": "no-store"})


@app.get("/events")
async def events():
    q = asyncio.Queue(maxsize=200)
    broker.queues.add(q)

    async def gen():
        try:
            yield "retry: 2000\n\n"
            yield f"event: state\ndata: {json.dumps(live.state, ensure_ascii=False)}\n\n"
            while not SHUTDOWN.is_set():
                try:
                    yield await asyncio.wait_for(q.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            broker.queues.discard(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


# ------------------------------------------------------------------ video tải lên
MAX_UPLOAD_BYTES = up.MAX_UPLOAD_MB * 1024 * 1024


@app.middleware("http")
async def _limit_upload_size(request: Request, call_next):
    """Chặn sớm file quá lớn dựa trên Content-Length (trước khi nhận hết dữ liệu)."""
    if request.method == "POST" and request.url.path == "/api/videos":
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > MAX_UPLOAD_BYTES + 1024 * 1024:
            return JSONResponse({"detail": f"File quá lớn (giới hạn {up.MAX_UPLOAD_MB} MB)."}, status_code=413)
    return await call_next(request)


@app.post("/api/videos", status_code=201)
async def api_video_upload(file: UploadFile = File(...)):
    """Lưu file + đọc thông tin. Không phân tích trước: trình duyệt mở /api/player/open để phát + phân tích ngay."""
    name = os.path.basename(file.filename or "video")
    ext = os.path.splitext(name)[1].lower()
    if ext not in up.ALLOWED_EXT:
        raise HTTPException(400, f"Chỉ nhận video {', '.join(sorted(up.ALLOWED_EXT))} "
                                 f"(file gửi lên: {ext or 'không có đuôi'})")
    vid, path = up.new_id_path(name)
    try:
        size = await asyncio.to_thread(up.save_upload, file.file, path, MAX_UPLOAD_BYTES)
    except ValueError:
        db._remove(path)
        raise HTTPException(413, f"File quá lớn (giới hạn {up.MAX_UPLOAD_MB} MB).")
    except Exception as e:
        db._remove(path)
        raise HTTPException(500, f"Không lưu được file: {e}")
    finally:
        await file.close()
    if size == 0:
        db._remove(path)
        raise HTTPException(400, "File rỗng")
    try:
        return await asyncio.to_thread(up.register, vid, name, path, size)
    except RuntimeError:
        db._remove(path)
        raise HTTPException(422, "Không đọc được video (file hỏng hoặc định dạng/codec không hỗ trợ)")


@app.get("/api/videos")
def api_videos():
    return db.list_videos()


def _video_or_404(vid, full=True):
    v = db.get_video(vid, full)
    if not v:
        raise HTTPException(404, "Không tìm thấy video (có thể đã bị xóa)")
    return v


def _video_path(v):
    path = os.path.join(db.UPLOAD_DIR, os.path.basename(v["path"]))
    if not os.path.exists(path):
        raise HTTPException(410, "File video không còn trên máy")
    return path


@app.get("/api/videos/{vid}")
def api_video(vid: str):
    return _video_or_404(vid)


@app.delete("/api/videos/{vid}")
def api_video_delete(vid: str):
    if player.is_open(vid):
        player.close()
    n = db.delete_video(vid)
    if n is None:
        raise HTTPException(404, "Không tìm thấy video")
    broker.publish("event", {"kind": "deleted", "event": {"video_id": vid}})
    return {"deleted_events": n}


@app.get("/api/videos/{vid}/frame")
def api_video_frame(vid: str, t: float = 0.0, overlay: int = 0):
    """Một khung tĩnh tại giây t (khung đầu tiên khi vừa tải lên; hoặc khi tạm dừng ở chế độ phát lại đã phân tích)."""
    v = _video_or_404(vid)
    jpg = replay.still_jpeg(_video_path(v), v, max(0.0, t), bool(overlay))
    if jpg is None:
        raise HTTPException(422, "Không đọc được khung hình tại thời điểm này")
    return Response(jpg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/replay/{vid}")
def replay_stream(vid: str, t: float = 0.0):
    """Phát lại ĐÃ PHÂN TÍCH (dùng kết quả cửa sổ đã lưu): MJPEG đúng fps gốc, nhãn khớp từng khung."""
    v = _video_or_404(vid)
    if not v.get("windows"):
        raise HTTPException(409, "Video chưa có kết quả đã lưu (hãy phát hết một lần ở chế độ thời gian thực)")
    gen = replay.iter_mjpeg(_video_path(v), v, max(0.0, t), SHUTDOWN, _part)
    return StreamingResponse(gen, media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
                             headers={"Cache-Control": "no-store"})


# ------------------------------------------------------------------ video thời gian thực (player)
class OpenBody(BaseModel):
    video_id: str
    t: float = Field(0.0, ge=0)
    mode: str = Field("share", pattern="^(pause|share)$")


class SeekBody(BaseModel):
    t: float = Field(..., ge=0)


@app.post("/api/player/open")
def api_player_open(body: OpenBody):
    """Phát video ĐÚNG TỐC ĐỘ GỐC và phân tích đồng thời như một camera trực tiếp."""
    v = _video_or_404(body.video_id, full=False)
    _video_path(v)
    if PROBS["video"].video_id != body.video_id:
        PROBS["video"].reset(body.video_id)
        broker.publish("probs_reset", {"source": "video"})
    return player.open(v, body.t, body.mode)


# ------------------------------------------------------------------ xác suất theo thời gian (số liệu phiên)
@app.get("/api/probs/{source}")
def api_probs(source: str):
    if source not in PROBS:
        raise HTTPException(404, "Nguồn phải là live hoặc video")
    s = db.get_settings()
    labels = [l for l in hub.labels if l != hub.normal_label]
    return {**PROBS[source].snapshot(),
            "thr": {l: (s["thr_by_label"] or {}).get(l, s["thr"]) for l in labels},
            "k": {l: (s["k_by_label"] or {}).get(l, s["k"]) for l in labels}}


@app.post("/api/probs/{source}/reset")
def api_probs_reset(source: str):
    if source not in PROBS:
        raise HTTPException(404, "Nguồn phải là live hoặc video")
    PROBS[source].reset(PROBS[source].video_id)
    broker.publish("probs_reset", {"source": source})
    return api_probs(source)


@app.post("/api/player/play")
def api_player_play():
    if not player.det.state.get("video_id"):
        raise HTTPException(409, "Chưa mở video nào")
    return player.play()


@app.post("/api/player/pause")
def api_player_pause():
    return player.pause()


@app.post("/api/player/seek")
def api_player_seek(body: SeekBody):
    if not player.det.state.get("video_id"):
        raise HTTPException(409, "Chưa mở video nào")
    return player.seek(body.t)


@app.post("/api/player/close")
def api_player_close():
    return player.close()


@app.get("/api/player/state")
def api_player_state():
    return player.state()


@app.get("/player/stream")
async def player_stream():
    """MJPEG của video đang phát thời gian thực (viền + nhãn vẽ ở server), tối đa ~30 khung/giây."""
    det = player.det

    async def gen():
        last, last_sent = -1, 0.0
        while not SHUTDOWN.is_set():
            fid = det.latest_id()
            now = time.monotonic()
            if fid != last and now - last_sent >= 1 / 31:
                fid, jpg = await asyncio.to_thread(det.jpeg)
                if jpg is not None:
                    last, last_sent = fid, now
                    yield _part(jpg)
            await asyncio.sleep(0.01)

    return StreamingResponse(gen(), media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
                             headers={"Cache-Control": "no-store"})


# ------------------------------------------------------------------ lịch sử sự kiện
def _decorate(ev):
    return {**ev, **label_info(ev["label"]), "ongoing": ev["id"] == recorder.current_id}


def _parse_day(s, end=False):
    """'YYYY-MM-DD' (giờ máy) -> epoch; end=True -> đầu ngày hôm sau."""
    if not s:
        return None
    try:
        d = datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, f"Ngày không hợp lệ: {s}")
    return d.timestamp() + (86400 if end else 0)


@app.get("/api/events")
def api_events(label: Optional[str] = None, source_type: Optional[str] = None, date_from: Optional[str] = None,
               date_to: Optional[str] = None, fp: Optional[str] = None, limit: int = 50, offset: int = 0):
    fp_val = {"1": True, "0": False}.get(fp or "")
    if source_type and source_type not in ("laptop", "phone", "video"):
        raise HTTPException(400, "Nguồn lọc không hợp lệ")
    total, items = db.list_events(label or None, _parse_day(date_from), _parse_day(date_to, end=True),
                                  fp_val, max(1, min(limit, 500)), max(0, offset), source_type or None)
    return {"total": total, "items": [_decorate(e) for e in items]}


@app.get("/api/events/{event_id}")
def api_event(event_id: int):
    ev = db.get_event(event_id)
    if not ev:
        raise HTTPException(404, "Không tìm thấy sự kiện")
    return _decorate(ev)


class FPBody(BaseModel):
    value: bool = True


@app.post("/api/events/{event_id}/false_positive")
def api_event_fp(event_id: int, body: FPBody):
    if not db.get_event(event_id):
        raise HTTPException(404, "Không tìm thấy sự kiện")
    db.update_event(event_id, false_positive=1 if body.value else 0)
    return _decorate(db.get_event(event_id))


@app.delete("/api/events/{event_id}")
def api_event_delete(event_id: int, with_video: int = 0):
    """with_video=1: xóa luôn video tải lên của sự kiện này (kèm mọi sự kiện khác của video đó)."""
    ev = db.get_event(event_id)
    if not ev:
        raise HTTPException(404, "Không tìm thấy sự kiện")
    if event_id == recorder.current_id:
        recorder.close()
    if with_video and ev.get("video_id"):
        if player.is_open(ev["video_id"]):
            player.close()
        n = db.delete_video(ev["video_id"]) or 0
        return {"deleted": max(1, n), "video_deleted": True}
    db.delete_event(event_id)
    return {"deleted": 1, "video_deleted": False}


@app.delete("/api/events")
def api_events_delete_all(confirm: str = "", with_videos: int = 0):
    if confirm != "yes":
        raise HTTPException(400, "Thiếu xác nhận (?confirm=yes)")
    recorder.close()
    n = db.delete_all_events()
    nv = 0
    if with_videos:
        for v in db.list_videos(limit=100000):
            if player.is_open(v["id"]):
                player.close()
            db.delete_video(v["id"])
            nv += 1
    return {"deleted": n, "videos_deleted": nv}


# ------------------------------------------------------------------ chạy trực tiếp
if __name__ == "__main__":
    import argparse
    import uvicorn
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1", help="mặc định 127.0.0.1; 0.0.0.0 để điện thoại cùng Wi-Fi xem")
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    uvicorn.run(app, host=a.host, port=a.port, timeout_graceful_shutdown=5)   # an toàn: không chờ quá 5s khi tắt
