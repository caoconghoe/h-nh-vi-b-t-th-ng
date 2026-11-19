"""Video tải lên: lưu file trong data/uploads (để phát lại), đọc thông tin (fps, thời lượng, cờ xoay)
và đăng ký vào CSDL. KHÔNG phân tích trước: video được phân tích khi phát (chế độ thời gian thực, xem rtvideo.py)."""
import os
import sys
import time
import uuid

import cv2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import open_video_file  # noqa: E402  (cùng cách đọc cờ xoay như app.py)

from . import db  # noqa: E402

ALLOWED_EXT = {".mp4", ".mov", ".avi", ".mkv", ".m4v"}
MAX_UPLOAD_MB = 1024


def new_id_path(filename):
    vid = uuid.uuid4().hex[:12]
    return vid, os.path.join(db.UPLOAD_DIR, vid + os.path.splitext(filename)[1].lower())


def save_upload(src_file, dst_path, max_bytes):
    """Chép file tải lên vào thư mục uploads, dừng nếu vượt dung lượng."""
    n = 0
    with open(dst_path, "wb") as out:
        while True:
            chunk = src_file.read(1 << 20)
            if not chunk:
                break
            n += len(chunk)
            if n > max_bytes:
                raise ValueError("too_large")
            out.write(chunk)
    return n


def probe(path):
    """Thông tin video (đã tính cờ xoay). RuntimeError nếu không đọc được."""
    cap, rot, how = open_video_file(path)
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        if not (1 <= fps <= 240):
            fps = 30.0
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        ok, _ = cap.read()
        if not ok:
            raise RuntimeError("không có khung hình")
        w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    finally:
        cap.release()
    if rot in (90, 270):
        w, h = h, w
    return {"fps": round(fps, 3), "frames": n, "duration": round(n / fps, 2) if n else None,
            "width": w, "height": h, "rotation": rot, "orientation": how}


def register(vid, filename, path, size):
    info = probe(path)
    db.insert_video(vid, filename, os.path.basename(path), size, time.time(), status="ready", info=info)
    return db.get_video(vid, full=False)


def clean_orphans():
    """Xóa file trong uploads không có trong CSDL (tải dở, v.v.)."""
    known = {os.path.basename(v["path"]) for v in db.list_videos(limit=100000)}
    for f in os.listdir(db.UPLOAD_DIR):
        if f not in known:
            db._remove(os.path.join(db.UPLOAD_DIR, f))
