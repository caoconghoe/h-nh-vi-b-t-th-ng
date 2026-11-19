"""Điểm vào duy nhất.
  python app.py                         -> menu
  python app.py --source 0              -> camera laptop
  python app.py --source http://IP:4747/video  -> DroidCam
  python app.py --source video.mov      -> phân tích cả file video
"""
import argparse
import os
import sys
import time

import cv2
import numpy as np

import core

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
ALARM_TEXT = "BÁO ĐỘNG"


# ---------------------------------------------------------------- vẽ chữ
def _load_font(size):
    try:
        from PIL import ImageFont
        for name in ("arialbd.ttf", "arial.ttf", "segoeui.ttf"):
            p = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", name)
            if os.path.exists(p):
                return ImageFont.truetype(p, size)
    except Exception:
        pass
    return None


_FONTS = {}


def put_text(img, text, org, size=28, color=(255, 255, 255), bg=(0, 0, 0)):
    """Vẽ chữ (hỗ trợ tiếng Việt nếu có Pillow, nếu không thì bỏ dấu)."""
    font = _FONTS.setdefault(size, _load_font(size))
    x, y = org
    if font is not None:
        from PIL import Image, ImageDraw
        l, t, r, b = font.getbbox(text)
        w, h = r - l + 12, b - t + 12
        x2, y2 = min(img.shape[1], x + w), min(img.shape[0], y + h)
        if x2 <= x or y2 <= y:
            return
        roi = img[y:y2, x:x2]
        pil = Image.new("RGB", (x2 - x, y2 - y), bg[::-1])
        ImageDraw.Draw(pil).text((6 - l, 6 - t), text, font=font, fill=color[::-1])
        roi[:] = cv2.cvtColor(np.asarray(pil), cv2.COLOR_RGB2BGR)
        return
    import unicodedata
    ascii_text = unicodedata.normalize("NFKD", text.replace("Đ", "D").replace("đ", "d"))
    ascii_text = "".join(c for c in ascii_text if ord(c) < 128)
    scale = size / 32
    (w, h), base = cv2.getTextSize(ascii_text, cv2.FONT_HERSHEY_SIMPLEX, scale, 2)
    cv2.rectangle(img, (x, y), (x + w + 12, y + h + base + 12), bg, -1)
    cv2.putText(img, ascii_text, (x + 6, y + h + 6), cv2.FONT_HERSHEY_SIMPLEX, scale, color, 2, cv2.LINE_AA)


def draw_overlay(img, label, conf, alarm, extra=None):
    size = max(18, img.shape[0] // 24)
    if label is None:
        put_text(img, "đang chờ đủ 3 giây...", (10, 10), size)
    else:
        col = (0, 0, 255) if alarm else ((0, 200, 0) if label == "normal" else (0, 200, 255))
        put_text(img, f"{label}  {conf:.2f}", (10, 10), size, color=col)
    if extra:
        put_text(img, extra, (10, 10 + int(size * 1.6)), int(size * 0.7), color=(220, 220, 220))
    if alarm:
        t = max(6, img.shape[0] // 60)
        cv2.rectangle(img, (0, 0), (img.shape[1] - 1, img.shape[0] - 1), (0, 0, 255), t)
        put_text(img, ALARM_TEXT, (10, img.shape[0] - int(size * 2.2)), int(size * 1.3),
                 color=(255, 255, 255), bg=(0, 0, 200))
    return img


# ---------------------------------------------------------------- chế độ video file
def open_video_file(path):
    """Mở file và xác định góc xoay cần áp dụng thủ công.
    Tắt tự xoay của OpenCV rồi đọc cờ xoay (metadata) để tự xoay -> kết quả chắc chắn, không xoay 2 lần."""
    cap = cv2.VideoCapture(path, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError(f"Không mở được video: {path}")
    meta = 0
    if hasattr(cv2, "CAP_PROP_ORIENTATION_AUTO") and cap.set(cv2.CAP_PROP_ORIENTATION_AUTO, 0):
        meta = int(round(cap.get(cv2.CAP_PROP_ORIENTATION_META))) % 360
        how = f"metadata xoay = {meta}° (tự xoay thủ công)"
    else:
        how = "OpenCV tự xoay theo metadata"
    return cap, meta, how


def run_video(args, clf):
    path = args.source
    cap, meta_rot, how = open_video_file(path)
    rot = (meta_rot + args.rotate) % 360
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    n_total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    raw_w, raw_h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"Video: {path}\n  {raw_w}x{raw_h} (khung thô), {fps:.2f} fps, {n_total} khung "
          f"(~{n_total / fps:.1f}s)\n  Chiều: {how}; --rotate {args.rotate} -> xoay tổng {rot}°")
    print(f"  THR={args.thr}  K={args.k}  STEP={args.step}s  ({core.NUM_FRAMES} khung / {core.WINDOW_SEC:.0f}s)\n")

    def frame_indices(s):
        return [round((s + j * core.SAMPLE_DT) * fps) for j in range(core.NUM_FRAMES)]

    mid = n_total // 2 if n_total > 0 else int(fps * 1.5)
    check_path = os.path.join(HERE, "check_frame.jpg")
    windows = []        # mỗi phần tử: [s, list_idx]
    next_n = 0
    cache = {}          # idx khung -> mảng đã tiền xử lý
    results = []        # (s, label, conf)
    times = []
    idx = -1
    print(f"{'bắt đầu(s)':>10} | {'nhãn':<10} | {'tin cậy':>7} | {'xử lý(s)':>8} | {'TB(s)':>6}")
    print("-" * 56)

    def infer(s, idxs):
        t0 = time.perf_counter()
        label, conf, _ = clf.predict([cache[i] for i in idxs])
        dt = time.perf_counter() - t0
        times.append(dt)
        results.append((s, label, conf))
        print(f"{s:10.2f} | {label:<10} | {conf:7.3f} | {dt:8.2f} | {np.mean(times):6.2f}", flush=True)

    while True:
        if not cap.grab():
            break
        idx += 1
        # tạo các cửa sổ có khung đầu tiên = idx
        while round(next_n * args.step * fps) <= idx:
            s = next_n * args.step
            windows.append([s, frame_indices(s)])
            next_n += 1
        need = any(idx in w[1] for w in windows) or idx == mid
        if not need:
            continue
        ok, frame = cap.retrieve()
        if not ok:
            continue
        frame = core.rotate(frame, rot)
        if idx == mid:
            cv2.imwrite(check_path, frame)
            print(f"  [đã lưu khung giữa video #{idx} -> check_frame.jpg, kích thước {frame.shape[1]}x{frame.shape[0]}]")
        if any(idx in w[1] for w in windows):
            cache[idx] = clf.preprocess_frame(frame)
        # chạy các cửa sổ đã đủ khung
        done = [w for w in windows if w[1][-1] <= idx]
        for w in done:
            infer(w[0], w[1])
            windows.remove(w)
        low = min((min(w[1]) for w in windows), default=idx + 1)
        for k in [k for k in cache if k < low]:
            del cache[k]
    cap.release()

    if not results and cache:   # video ngắn hơn 3 giây: dùng khung có được (lặp khung cuối)
        last = max(cache)
        idxs = [min(i, last) for i in frame_indices(0)]
        idxs = [i if i in cache else max(k for k in cache if k <= i) for i in idxs]
        print("  (video < 3 giây: lặp lại khung cuối để đủ 16 khung)")
        infer(0.0, idxs)
    if not results:
        print("Không đọc được khung nào.")
        return

    flags, segs = core.alerts(results, args.thr, args.k, clf.normal_label)
    print("-" * 56)
    print(f"Số cửa sổ: {len(results)} | thời gian xử lý TB mỗi cửa sổ: {np.mean(times):.2f}s "
          f"(min {np.min(times):.2f}, max {np.max(times):.2f})")
    if segs:
        print(f"\nCÁC ĐOẠN BÁO ĐỘNG (THR={args.thr}, K={args.k}):")
        for g in segs:
            print(f"  * {g.label:<10} {g.start:7.2f}s -> {g.end:7.2f}s   tin cậy max {g.max_conf:.3f}")
    else:
        print("\nKhông có đoạn báo động nào.")

    if not args.no_save:
        out = write_result_video(path, rot, fps, results, flags, segs)
        print(f"\nĐã lưu video kết quả: {out}")


def write_result_video(path, rot, fps, results, flags, segs):
    """Đọc lại tuần tự, vẽ nhãn của cửa sổ có tâm gần nhất + viền đỏ trong đoạn báo động."""
    cap, _, _ = open_video_file(path)
    base = os.path.splitext(os.path.basename(path))[0]
    out_path = os.path.join(HERE, f"{base}_result.mp4")
    writer = None
    starts = np.array([r[0] for r in results])
    centers = starts + core.WINDOW_SEC / 2
    idx = -1
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        idx += 1
        t = idx / fps
        frame = core.rotate(frame, rot)
        h, w = frame.shape[:2]
        s = min(1.0, 960 / max(h, w))
        if s < 1:
            frame = cv2.resize(frame, (round(w * s) // 2 * 2, round(h * s) // 2 * 2), interpolation=cv2.INTER_AREA)
        if writer is None:
            writer = cv2.VideoWriter(out_path, cv2.VideoWriter_fourcc(*"mp4v"), fps,
                                     (frame.shape[1], frame.shape[0]))
        i = int(np.argmin(np.abs(centers - t)))
        alarm = any(g.start <= t <= g.end for g in segs)
        _, label, conf = results[i]
        draw_overlay(frame, label, conf, alarm, extra=f"t={t:5.1f}s  cửa sổ {starts[i]:.1f}-{starts[i] + 3:.1f}s")
        writer.write(frame)
    cap.release()
    if writer is not None:
        writer.release()
    return out_path


# ---------------------------------------------------------------- chế độ trực tiếp
def run_live(args, clf):
    print("Chế độ trực tiếp sẽ được làm ở Giai đoạn 2.")


# ---------------------------------------------------------------- nhận dạng nguồn + menu
def is_live_source(src):
    return src.isdigit() or "://" in src


def menu():
    print("=== Phát hiện hành vi bất thường ===")
    print("[1] Camera laptop\n[2] Camera điện thoại (DroidCam)\n[3] Test video file")
    c = input("Chọn (1/2/3): ").strip()
    if c == "1":
        cam = input("Số camera [0]: ").strip() or "0"
        return cam
    if c == "2":
        ip = input("Nhập IP DroidCam (vd 192.168.1.5) hoặc số camera ảo DroidCam Client: ").strip()
        if ip.isdigit() or "://" in ip:
            return ip
        if ":" not in ip:
            ip += ":4747"
        return f"http://{ip}/video"
    if c == "3":
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        p = filedialog.askopenfilename(title="Chọn video", initialdir=HERE,
                                       filetypes=[("Video", "*.mp4 *.mov *.avi *.mkv *.m4v"), ("Tất cả", "*.*")])
        root.destroy()
        if not p:
            sys.exit("Chưa chọn file.")
        return p
    sys.exit("Lựa chọn không hợp lệ.")


def main():
    ap = argparse.ArgumentParser(description="Phát hiện hành vi bất thường (VideoMAE, CPU)")
    ap.add_argument("--source", help="số camera | URL | đường dẫn video")
    ap.add_argument("--thr", type=float, default=0.7, help="ngưỡng độ tin cậy (mặc định 0.7)")
    ap.add_argument("--k", type=int, default=2, help="số cửa sổ liên tiếp cùng nhãn để báo động (mặc định 2)")
    ap.add_argument("--step", type=float, default=None, help="bước giữa 2 cửa sổ, giây (mặc định 1 cho file)")
    ap.add_argument("--rotate", type=int, default=0, choices=[0, 90, 180, 270], help="xoay thêm theo chiều kim đồng hồ")
    ap.add_argument("--model", default=core.DEFAULT_MODEL_DIR, help="thư mục model")
    ap.add_argument("--no-save", action="store_true", help="không lưu video kết quả (chế độ file)")
    args = ap.parse_args()

    if not args.source:
        args.source = menu()
    if not os.path.isdir(args.model) or not os.path.exists(os.path.join(args.model, "config.json")):
        sys.exit(f"Không thấy model tại {args.model}")

    live = is_live_source(args.source)
    if not live and not os.path.isfile(args.source):
        sys.exit(f"Không tìm thấy file: {args.source}")

    print("Đang nạp model...", flush=True)
    t0 = time.perf_counter()
    clf = core.Classifier(args.model)
    print(f"Đã nạp model trong {time.perf_counter() - t0:.1f}s. Nhãn: {clf.labels}")

    if live:
        run_live(args, clf)
    else:
        if args.step is None:
            args.step = 1.0
        run_video(args, clf)


if __name__ == "__main__":
    main()
