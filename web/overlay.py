"""Vẽ lớp phủ thống nhất lên khung hình (phía server) cho mọi nguồn: trực tiếp và phát lại video.
- Bình thường: huy hiệu nhỏ màu xanh "Bình thường" ở góc phải trên.
- Báo động: viền đỏ dày (nhấp nháy nhẹ) + huy hiệu "<NHÃN> 0.87" màu riêng của nhãn ở góc phải trên.
Chữ tiếng Việt có dấu vẽ bằng Pillow; huy hiệu được vẽ thành ảnh nhỏ (có cache) rồi dán vào khung -> nhanh."""
import math
import os
from functools import lru_cache

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

# Tên hiển thị + màu NHÃN (màu đi theo nhãn ở mọi nơi: huy hiệu, chip lịch sử, biểu đồ).
# Bộ màu đã kiểm tra bằng validate_palette (mọi cặp, nền sáng #fff và tối #181b22): phân biệt được với
# mắt thường (ΔE >= 18.8) và người mù màu đỏ-lục (ΔE >= 8.8).
LABEL_INFO = {
    "normal":    {"display": "Bình thường",     "color": "#0d9488", "tone": 0},
    "fight":     {"display": "ĐÁNH NHAU",       "color": "#dc2626", "tone": 880},
    "fall":      {"display": "TÉ NGÃ",          "color": "#c4880a", "tone": 660},
    "run_chase": {"display": "CHẠY/RƯỢT ĐUỔI",  "color": "#7c3aed", "tone": 1040},
}
NORMAL_LABEL = "normal"
ALARM_BORDER = (0, 0, 235)          # BGR đỏ
STATUS_GOOD = "#16a34a"             # màu TRẠNG THÁI "an toàn" (viền + huy hiệu Bình thường trên khung hình)
PENDING = {"display": "Đang phân tích...", "color": "#475569"}


def _hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _lum(hexcolor):
    def ch(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(v) for v in _hex_rgb(hexcolor))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ink(bg_hex, min_white=4.5):
    """Màu chữ trên nền màu nhãn: trắng nếu đủ tương phản, nếu không thì gần đen."""
    white = 1.05 / (_lum(bg_hex) + 0.05)
    return "#ffffff" if white >= min_white else "#111111"


def label_info(name):
    info = LABEL_INFO.get(name, {"display": str(name).upper(), "color": "#64748b", "tone": 760})
    return {**info, "ink": ink(info["color"])}


@lru_cache(maxsize=8)
def _font(size):
    windir = os.environ.get("WINDIR", r"C:\Windows")
    for name in ("segoeuib.ttf", "arialbd.ttf", "seguisb.ttf", "arial.ttf"):
        p = os.path.join(windir, "Fonts", name)
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/Library/Fonts/Arial Bold.ttf"):
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default(size)


@lru_cache(maxsize=256)
def _badge(text, color_hex, size):
    """Ảnh BGR của huy hiệu (nền màu nhãn, chữ trắng, viền mảnh)."""
    font = _font(size)
    l, t, r, b = font.getbbox(text)
    pad_x, pad_y = round(size * 0.55), round(size * 0.32)
    w, h = (r - l) + 2 * pad_x, (b - t) + 2 * pad_y
    img = Image.new("RGB", (w, h), _hex_rgb(color_hex))
    d = ImageDraw.Draw(img)
    fg = _hex_rgb(ink(color_hex, min_white=3.0))      # chữ đậm, cỡ lớn: chuẩn tương phản 3:1
    d.text((pad_x - l, pad_y - t), text, font=font, fill=fg)
    d.rectangle([0, 0, w - 1, h - 1], outline=fg, width=max(1, size // 14))
    return cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2BGR)


def badge_text(label, conf, alarm):
    """Nội dung huy hiệu. Ngoài báo động luôn là 'Bình thường' (kèm độ tin cậy khi model chọn normal)."""
    if label is None:
        return PENDING["display"], PENDING["color"]
    if alarm:
        info = label_info(label)
        return f"{info['display']} {conf:.2f}", info["color"]
    normal = LABEL_INFO[NORMAL_LABEL]
    if label == NORMAL_LABEL:
        return f"{normal['display']} {conf:.2f}", STATUS_GOOD
    return normal["display"], STATUS_GOOD


NORMAL_BORDER = (74, 163, 22)       # BGR xanh lá (#16a34a)
PENDING_BORDER = (139, 116, 100)    # BGR xám (#64748b)


def draw(frame, label, conf, alarm, t=0.0):
    """Vẽ lên frame (BGR, sửa tại chỗ) và trả về frame. t (giây) dùng cho hiệu ứng nhấp nháy viền.
    Viền: xám = đang phân tích (chưa có kết quả), xanh lá = bình thường, đỏ dày nhấp nháy = báo động."""
    h, w = frame.shape[:2]
    short = min(h, w)
    if alarm:
        th = max(6, round(short * 0.022))
        pulse = 0.5 + 0.5 * math.cos(2 * math.pi * 1.2 * t)       # 1.2 Hz, nhẹ: 65%..100% độ sáng
        k = 0.65 + 0.35 * pulse
        color = tuple(int(c * k) for c in ALARM_BORDER)
    else:
        th = max(3, round(short * 0.009))
        color = PENDING_BORDER if label is None else NORMAL_BORDER
    cv2.rectangle(frame, (th // 2, th // 2), (w - 1 - th // 2, h - 1 - th // 2), color, th)
    text, color_hex = badge_text(label, conf, alarm)
    size = max(14, min(48, round(short * (0.05 if alarm else 0.035))))
    bimg = _badge(text, color_hex, size)
    margin = max(8, round(short * 0.03)) + (round(short * 0.022) if alarm else round(short * 0.009))
    bh, bw = bimg.shape[:2]
    x1, y1 = w - margin - bw, margin
    if x1 < 0:                         # khung quá hẹp: cắt bớt huy hiệu
        bimg, x1 = bimg[:, -x1:], 0
        bw = bimg.shape[1]
    bh = min(bh, h - y1)
    if bh > 0:
        roi = frame[y1:y1 + bh, x1:x1 + bw]
        cv2.addWeighted(bimg[:bh], 0.92, roi, 0.08, 0, dst=roi)
    return frame
