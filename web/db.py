"""SQLite (thư viện chuẩn): cài đặt, sự kiện, video tải lên. Mỗi lần gọi mở kết nối riêng để an toàn đa luồng."""
import json
import os
import sqlite3
import threading

# ANOMALY_DATA_DIR: dùng thư mục dữ liệu khác (ví dụ để chạy thử mà không đụng dữ liệu thật)
DATA_DIR = os.environ.get("ANOMALY_DATA_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SNAP_DIR = os.path.join(DATA_DIR, "snapshots")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
DB_PATH = os.path.join(DATA_DIR, "app.db")

DEFAULT_SETTINGS = {
    # nguồn trực tiếp đang dùng
    "source": "0",          # "0","1",... = số camera; "http://IP:4747/video" = DroidCam; "none" = dừng
    "source_kind": "laptop",  # laptop | phone | none
    "rotate": 0,            # góc xoay đang áp dụng (0/90/180/270, chiều kim đồng hồ)
    # giá trị nhớ riêng cho từng tab
    "laptop_index": "0",
    "phone_mode": "url",    # url (DroidCam qua Wi-Fi) | virtual (camera ảo DroidCam Client)
    "phone_ip": "",
    "phone_port": 4747,
    "phone_index": "1",
    "phone_rotate": 0,
    # phát hiện
    "thr": 0.7,             # ngưỡng độ tin cậy CHUNG (mặc định cho nhãn chưa đặt riêng)
    "k": 2,                 # số cửa sổ liên tiếp cùng nhãn CHUNG
    "thr_by_label": {},     # ngưỡng riêng theo nhãn, ví dụ {"fall": 0.6}; nhãn không có ở đây dùng "thr"
    "k_by_label": {},       # K riêng theo nhãn, ví dụ {"fight": 3}; nhãn không có ở đây dùng "k"
    "step": 2.0,            # chu kỳ suy luận trực tiếp (giây); thực tế >= thời gian suy luận
    "cooldown": 10.0,       # thời gian chờ chống lặp cảnh báo cùng nhãn (giây)
    "video_step": 1.0,      # bước giữa các cửa sổ khi phân tích video tải lên (giây)
}
# tên cũ (giai đoạn trước) -> tên mới
_KIND_ALIASES = {"droidcam_url": "phone", "droidcam_virtual": "phone"}

_write_lock = threading.Lock()


def connect():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db():
    os.makedirs(SNAP_DIR, exist_ok=True)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    with _write_lock, connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            label          TEXT NOT NULL,
            start_ts       REAL NOT NULL,      -- epoch giây
            end_ts         REAL NOT NULL,
            max_conf       REAL NOT NULL,
            source         TEXT NOT NULL,      -- số camera / URL / tên file video
            snapshot       TEXT,               -- tên file trong data/snapshots
            false_positive INTEGER NOT NULL DEFAULT 0,
            created_at     REAL NOT NULL DEFAULT (strftime('%s','now'))
        );
        CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_ts);
        CREATE INDEX IF NOT EXISTS idx_events_label ON events(label);
        CREATE TABLE IF NOT EXISTS videos (
            id        TEXT PRIMARY KEY,
            filename  TEXT NOT NULL,           -- tên gốc người dùng tải lên
            path      TEXT NOT NULL,           -- tên file trong data/uploads
            size      INTEGER NOT NULL,
            created   REAL NOT NULL,
            status    TEXT NOT NULL,           -- queued | running | done | error | cancelled
            message   TEXT,
            mode      TEXT,
            thr REAL, k INTEGER, step REAL,
            info      TEXT,                    -- JSON: fps, khung, thời lượng, kích thước, xoay
            windows   TEXT,                    -- JSON: kết quả từng cửa sổ
            timeline  TEXT,                    -- JSON: nhãn theo từng giây
            segments  TEXT,                    -- JSON: các đoạn báo động
            elapsed   REAL
        );
        """)
        # nâng cấp CSDL cũ (các giai đoạn trước chưa có các cột này)
        cols = {r["name"] for r in con.execute("PRAGMA table_info(events)")}
        for name, decl in (("probs", "TEXT"), ("best_ts", "REAL"), ("thumb", "TEXT"),
                           ("video_start", "REAL"), ("video_end", "REAL"),   # vị trí (giây) trong video tải lên
                           ("source_type", "TEXT"), ("video_id", "TEXT")):  # laptop | phone | video
            if name not in cols:
                con.execute(f"ALTER TABLE events ADD COLUMN {name} {decl}")
        con.execute("UPDATE events SET source_type = CASE "
                    "WHEN video_start IS NOT NULL THEN 'video' "
                    "WHEN source GLOB '[0-9]*' THEN 'laptop' ELSE 'phone' END WHERE source_type IS NULL")
        con.execute("CREATE INDEX IF NOT EXISTS idx_events_video ON events(video_id)")
        # video đang dở khi server tắt: đánh dấu lỗi
        con.execute("UPDATE videos SET status='error', message='Bị gián đoạn do server khởi động lại' "
                    "WHERE status IN ('queued','running')")


# ------------------------------------------------------------------ cài đặt
def get_settings():
    s = dict(DEFAULT_SETTINGS)
    with connect() as con:
        for row in con.execute("SELECT key, value FROM settings"):
            if row["key"] in s:
                s[row["key"]] = json.loads(row["value"])
    s["source_kind"] = _KIND_ALIASES.get(s["source_kind"], s["source_kind"])
    return s


def save_settings(values):
    with _write_lock, connect() as con:
        con.executemany(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [(k, json.dumps(v)) for k, v in values.items() if k in DEFAULT_SETTINGS],
        )
    return get_settings()


# ------------------------------------------------------------------ sự kiện
EVENT_COLS = ("id", "label", "start_ts", "end_ts", "max_conf", "source", "snapshot", "thumb",
              "probs", "best_ts", "false_positive", "created_at", "video_start", "video_end",
              "source_type", "video_id")


def _row(r):
    d = {k: r[k] for k in EVENT_COLS}
    d["probs"] = json.loads(d["probs"]) if d["probs"] else {}
    d["false_positive"] = bool(d["false_positive"])
    return d


def insert_event(label, start_ts, end_ts, max_conf, source, probs, best_ts, snapshot=None, thumb=None,
                 video_start=None, video_end=None, source_type=None, video_id=None):
    with _write_lock, connect() as con:
        cur = con.execute(
            "INSERT INTO events(label, start_ts, end_ts, max_conf, source, probs, best_ts, snapshot, thumb, "
            "video_start, video_end, source_type, video_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (label, start_ts, end_ts, max_conf, source, json.dumps(probs), best_ts, snapshot, thumb,
             video_start, video_end, source_type, video_id))
        return cur.lastrowid


def update_event(event_id, **fields):
    fields = {k: (json.dumps(v) if k == "probs" else v) for k, v in fields.items()
              if k in ("start_ts", "end_ts", "max_conf", "probs", "best_ts", "snapshot", "thumb", "false_positive",
                       "video_start", "video_end")}
    if not fields:
        return
    with _write_lock, connect() as con:
        con.execute(f"UPDATE events SET {', '.join(k + '=?' for k in fields)} WHERE id=?",
                    (*fields.values(), event_id))


def _where(label=None, from_ts=None, to_ts=None, fp=None, source_type=None):
    sql, args = [], []
    if label:
        sql.append("label = ?"); args.append(label)
    if source_type:
        sql.append("source_type = ?"); args.append(source_type)
    if from_ts is not None:
        sql.append("start_ts >= ?"); args.append(from_ts)
    if to_ts is not None:
        sql.append("start_ts < ?"); args.append(to_ts)
    if fp is not None:
        sql.append("false_positive = ?"); args.append(1 if fp else 0)
    return (" WHERE " + " AND ".join(sql)) if sql else "", args


def list_events(label=None, from_ts=None, to_ts=None, fp=None, limit=50, offset=0, source_type=None):
    where, args = _where(label, from_ts, to_ts, fp, source_type)
    with connect() as con:
        total = con.execute(f"SELECT COUNT(*) FROM events{where}", args).fetchone()[0]
        rows = con.execute(f"SELECT {', '.join(EVENT_COLS)} FROM events{where} ORDER BY start_ts DESC, id DESC "
                           "LIMIT ? OFFSET ?", (*args, int(limit), int(offset))).fetchall()
    return total, [_row(r) for r in rows]


def get_event(event_id):
    with connect() as con:
        r = con.execute(f"SELECT {', '.join(EVENT_COLS)} FROM events WHERE id=?", (event_id,)).fetchone()
    return _row(r) if r else None


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _remove_files(rows):
    for r in rows:
        for name in (r["snapshot"], r["thumb"]):
            if name:
                _remove(os.path.join(SNAP_DIR, os.path.basename(name)))


def delete_event(event_id):
    with _write_lock, connect() as con:
        rows = con.execute("SELECT snapshot, thumb FROM events WHERE id=?", (event_id,)).fetchall()
        con.execute("DELETE FROM events WHERE id=?", (event_id,))
    _remove_files(rows)
    return len(rows)


def delete_all_events():
    with _write_lock, connect() as con:
        rows = con.execute("SELECT snapshot, thumb FROM events").fetchall()
        con.execute("DELETE FROM events")
    _remove_files(rows)
    return len(rows)


# ------------------------------------------------------------------ video tải lên
_VIDEO_JSON = ("info", "windows", "timeline", "segments")


def _video_row(r, full=True):
    d = dict(r)
    for k in _VIDEO_JSON:
        if k in d:
            d[k] = json.loads(d[k]) if d[k] else ([] if k != "info" else {})
    if not full:
        for k in ("windows", "timeline"):
            d.pop(k, None)
    return d


def insert_video(vid, filename, path, size, created, status="ready", info=None, mode=None, thr=None, k=None, step=None):
    with _write_lock, connect() as con:
        con.execute("INSERT INTO videos(id, filename, path, size, created, status, message, mode, thr, k, step, info) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (vid, filename, path, size, created, status, "", mode, thr, k, step,
                     json.dumps(info or {}, ensure_ascii=False)))


def update_video(vid, **fields):
    fields = {k: (json.dumps(v, ensure_ascii=False) if k in _VIDEO_JSON else v) for k, v in fields.items()}
    if not fields:
        return
    with _write_lock, connect() as con:
        con.execute(f"UPDATE videos SET {', '.join(k + '=?' for k in fields)} WHERE id=?", (*fields.values(), vid))


def get_video(vid, full=True):
    with connect() as con:
        r = con.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone()
    return _video_row(r, full) if r else None


def list_videos(limit=50):
    with connect() as con:
        rows = con.execute("SELECT id, filename, path, size, created, status, message, mode, thr, k, step, info, "
                           "segments, elapsed FROM videos ORDER BY created DESC LIMIT ?", (int(limit),)).fetchall()
    return [_video_row(r, full=False) for r in rows]


def delete_video(vid):
    """Xóa video + file + mọi sự kiện (và ảnh) của video. Trả về số sự kiện đã xóa hoặc None nếu không có."""
    with _write_lock, connect() as con:
        v = con.execute("SELECT path FROM videos WHERE id=?", (vid,)).fetchone()
        rows = con.execute("SELECT snapshot, thumb FROM events WHERE video_id=?", (vid,)).fetchall()
        con.execute("DELETE FROM events WHERE video_id=?", (vid,))
        con.execute("DELETE FROM videos WHERE id=?", (vid,))
    _remove_files(rows)
    if v is None:
        return None if not rows else len(rows)
    _remove(os.path.join(UPLOAD_DIR, os.path.basename(v["path"])))
    return len(rows)
