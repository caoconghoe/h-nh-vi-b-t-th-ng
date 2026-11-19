# Phát hiện hành vi bất thường trong video (VideoMAE, chạy CPU)

Phân loại mỗi đoạn 3 giây video thành 4 nhãn: **fall** (té ngã), **fight** (đánh nhau),
**normal** (bình thường), **run_chase** (chạy/rượt đuổi). Có hai cách dùng:

- **Ứng dụng web** (khuyên dùng): trang *Giám sát* với 3 nguồn là camera laptop, camera điện thoại (DroidCam)
  và video tải lên; trang *Lịch sử* và trang *Cài đặt*.
- **Dòng lệnh** `app.py`: phân tích một file video hoặc xem camera trong cửa sổ OpenCV.

> ⚠️ **Lưu ý quan trọng:** model **có thể báo nhầm hoặc bỏ sót**. Đây là **công cụ hỗ trợ**,
> **không thay thế** việc giám sát của con người. Hãy luôn kiểm tra lại bằng mắt trước khi xử lý.
> Nút "Đánh dấu báo nhầm" trong Lịch sử giúp gom dữ liệu để cải thiện model sau này.

Mọi dữ liệu (ảnh chụp, video tải lên, CSDL) **chỉ lưu trên máy này**, trong `web/data/`, và không gửi đi đâu.

## Cài đặt (Windows, Python 3.12)

```powershell
py -3.12 -m venv venv
.\venv\Scripts\python.exe -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
.\venv\Scripts\python.exe -m pip install transformers opencv-python numpy pillow fastapi uvicorn python-multipart
```

Model nằm trong `best_model_v3\`. Không sửa thư mục này.

## Chạy ứng dụng web

```powershell
.\venv\Scripts\python.exe -m uvicorn web.server:app
```

Mở trình duyệt tại **http://127.0.0.1:8000**. Lần đầu nạp model mất khoảng 2–8 giây. Bấm nút **🔇 Âm thanh** một lần
để trình duyệt cho phép phát tiếng bíp. Dừng server bằng `Ctrl+C`.

### Trang Giám sát: một khung hình, 3 tab

| Tab | Cách dùng |
|---|---|
| **Camera laptop** | Nhập số camera (thường là `0`) rồi bấm **Bật camera**. |
| **Camera điện thoại** | Xem mục DroidCam bên dưới. Có **Kiểm tra kết nối** (ảnh xem trước) và **xoay 0/90/180/270°**. |
| **Tải video** | Bấm **Chọn file video** hoặc kéo thả file vào khung (mp4, mov, avi, mkv, m4v; tối đa 1 GB). Tải lên xong, video **phát ngay đúng tốc độ gốc (1x)** và được **phân tích đồng thời như một camera**, không phân tích trước. Đoạn báo động hiện dần trên thanh thời gian. Có Phát/Tạm dừng (dừng cả phân tích), Phát lại từ đầu, và bấm vào thanh để tua (sau khi tua, 3 giây đầu hiện "Đang phân tích..."). Hết video thì dừng ở khung cuối. |

- Viền khung: **xám** cùng "Đang phân tích..." khi chưa có kết quả (3 giây đầu, hoặc ngay sau khi tua); **xanh lá** khi bình thường.
- Khi **bình thường**: góc phải trên có huy hiệu xanh "Bình thường".
- Khi **báo động** (độ tin cậy ≥ THR trong K cửa sổ liên tiếp cùng nhãn): **viền đỏ nhấp nháy** và nhãn ở góc phải trên,
  ví dụ "ĐÁNH NHAU 0.87". Kèm thông báo và tiếng bíp; mỗi nhãn có màu và cao độ tiếng bíp riêng.
- Viền và nhãn được **vẽ thẳng lên khung hình ở server**, nên luôn khớp với hình, cả khi xem trực tiếp lẫn khi phát lại.
- Chỉ một nguồn trực tiếp chạy tại một thời điểm. Nếu camera đang chạy mà mở video, ứng dụng sẽ hỏi:
  *dừng camera trong lúc phát video* (camera tự bật lại khi hết video hoặc khi đóng video) hay
  *xếp hàng chạy xen kẽ* (hai bên lần lượt dùng model, cả hai chậm hơn).
- Nếu CPU suy luận chậm hơn chu kỳ, video **vẫn chạy đều 1x**: hệ thống bỏ các cửa sổ cũ, chỉ phân tích cửa sổ 3 giây
  mới nhất, và hiện cảnh báo "Phân tích chậm hơn thời gian thực" kèm số cửa sổ đã bỏ.
- Đoạn báo động (trên thanh thời gian và trong Lịch sử) bắt đầu từ **đầu cửa sổ đầu tiên** của chuỗi K cửa sổ,
  gần với lúc hành động thật bắt đầu, chứ không phải lúc banner hiện. Banner thường hiện sau khoảng 4–7 giây.
- Video quay dọc (ví dụ iPhone .MOV) được tự xoay đứng thẳng theo cờ xoay trong file. Phát lại do server làm,
  nên cả video HEVC mà trình duyệt không đọc được vẫn xem được.

### Xác suất theo thời gian (đo và chỉnh ngưỡng)

Dưới khung hình có bảng **Xác suất theo thời gian**, cho camera (tab Camera laptop/điện thoại) và cho video (tab Tải video):
- Biểu đồ đường thể hiện xác suất của cả 4 nhãn ở từng cửa sổ phân tích, kèm đường ngưỡng (nét đứt) và dải đỏ nhạt
  ở các cửa sổ báo động. Rê chuột hoặc chạm vào biểu đồ để xem số liệu từng cửa sổ; chọn **Bảng** để xem dạng bảng.
- Các ô "… cao nhất" cho biết xác suất cao nhất của từng nhãn trong phiên (ví dụ "TÉ NGÃ cao nhất 0.61 lúc 0:12"),
  giúp chọn ngưỡng riêng hợp lý.
- **Xóa số liệu phiên** để đo lại từ đầu. Số liệu phiên chỉ giữ trong bộ nhớ, mất khi khởi động lại server;
  mở một video khác thì phiên video tự làm mới.

### Nối camera điện thoại bằng DroidCam

**Cách 1: qua Wi-Fi (URL)**
1. Cài ứng dụng **DroidCam** trên điện thoại. Điện thoại và máy tính phải **cùng một mạng Wi-Fi**.
2. Mở DroidCam, ghi lại **WiFi IP** (ví dụ `192.168.1.5`) và **Port** (mặc định `4747`).
3. Ở tab **Camera điện thoại**, chọn *DroidCam qua Wi-Fi*, nhập IP và cổng, rồi bấm **Kiểm tra kết nối**.
   Ứng dụng dùng `http://IP:4747/video`; nếu không được thì tự thử `http://IP:4747/mjpegfeed`.
4. Nếu người trong ảnh xem trước bị nằm ngang, chọn **Xoay 90°** hoặc **270°** cho tới khi đứng thẳng,
   rồi bấm **Bật camera điện thoại**. Có thể đổi góc xoay cả khi đang chạy.
5. DroidCam chỉ cho **1 kết nối** tại một thời điểm, nên hãy đóng các trình duyệt hoặc ứng dụng khác đang xem DroidCam.

**Cách 2: qua camera ảo (DroidCam Client trên Windows)**
1. Cài **DroidCam Client** trên Windows và kết nối với điện thoại (qua Wi-Fi hoặc USB).
2. Ở tab **Camera điện thoại**, chọn *Camera ảo DroidCam Client* và nhập **số camera ảo** (thường là `1` hoặc `2`;
   số `0` thường là webcam laptop). Bấm **Kiểm tra kết nối** để chắc chắn đã chọn đúng camera.

Nếu mất kết nối giữa chừng (ví dụ điện thoại khóa màn hình), ứng dụng hiện "Mất kết nối – đang kết nối lại"
và thử lại tối đa 5 lần trước khi báo lỗi.

### Lịch sử

- Mỗi chuỗi cửa sổ báo động liên tiếp cùng nhãn được gộp thành **một sự kiện**, cho cả 3 nguồn.
  Sự kiện gồm: nhãn, thời điểm bắt đầu và kết thúc, độ tin cậy cao nhất, xác suất cả 4 nhãn tại cửa sổ cao nhất,
  nguồn, ảnh chụp và cờ báo nhầm.
- Lọc theo nhãn, nguồn, ngày và trạng thái. Bấm vào một dòng để xem ảnh lớn.
- Sự kiện từ video tải lên có nút **▶ Xem lại**, mở đúng đoạn đó trong khung Giám sát.
- Có thể xóa từng sự kiện (với video: chọn chỉ xóa sự kiện hoặc xóa cả video), hoặc xóa tất cả.
  Nút **Đánh dấu báo nhầm** lưu cờ `false_positive` trong CSDL.

### Cài đặt (áp dụng ngay, không cần khởi động lại)

| Tham số | Ý nghĩa | Mặc định |
|---|---|---|
| **THR** | Ngưỡng độ tin cậy CHUNG để tính là báo động (nhãn khác normal) | 0.7 |
| **K** | Số cửa sổ liên tiếp cùng nhãn CHUNG cần có để báo động | 2 |
| **Ngưỡng / K riêng theo nhãn** | Đặt riêng cho TÉ NGÃ, ĐÁNH NHAU, CHẠY/RƯỢT ĐUỔI (ví dụ hạ ngưỡng té ngã cho nhạy hơn, tăng K của đánh nhau để bớt báo nhầm). Ô để trống = dùng THR/K chung | trống |
| **Chu kỳ suy luận** | Bao lâu phân tích 3 giây gần nhất một lần khi xem trực tiếp. Thực tế không nhanh hơn thời gian suy luận (khoảng 1.2–3 giây trên CPU) | 2 giây |
| **Chờ chống lặp** | Không phát lại thông báo hoặc tiếng bíp cho cùng một nhãn trong khoảng này | 10 giây |
| **Bước video** | Khoảng cách giữa các cửa sổ khi phân tích video tải lên | 1 giây |

Độ trễ thực tế từ lúc hành động bắt đầu đến lúc báo động vào khoảng **5–9 giây** với K=2 (do cửa sổ 3 giây, K lần
phân tích và thời gian suy luận trên CPU).

### Xem từ điện thoại hoặc máy khác cùng Wi-Fi

```powershell
.\venv\Scripts\python.exe -m web.server --host 0.0.0.0
```

Terminal sẽ in địa chỉ dạng `http://192.168.x.x:8000`; mở địa chỉ này trên điện thoại.
Windows có thể hỏi cho phép Python qua tường lửa; chọn *Private network*.

> ⚠️ Với `--host 0.0.0.0`, **bất kỳ ai cùng mạng** đều xem được camera, video, ảnh chụp, lịch sử và đổi được cài đặt
> (không có mật khẩu). Chỉ dùng trong mạng tin cậy và tắt khi không cần.

## Dòng lệnh (`app.py`)

```powershell
.\venv\Scripts\python.exe app.py                                  # menu: [1] camera laptop [2] DroidCam [3] video file
.\venv\Scripts\python.exe app.py --source video.mov --thr 0.7 --k 2 --step 1
```

Chế độ file in kết quả từng cửa sổ và các đoạn báo động, lưu `<tên>_result.mp4` (có nhãn, viền đỏ)
và `check_frame.jpg` (khung giữa video) để kiểm tra chiều. `--rotate 90/180/270` xoay thêm nếu cần.
*(Chế độ camera trực tiếp trong `app.py` chưa được làm; hãy dùng ứng dụng web.)*

## Cấu trúc

```
core.py            nạp model, tiền xử lý khung, suy luận 16 khung, quy tắc báo động (dùng chung)
app.py             dòng lệnh; open_video_file() đọc cờ xoay video (web dùng lại hàm này)
web/server.py      FastAPI: trang, API, /stream (trực tiếp), /replay/<id> (phát lại), /events (SSE)
web/detector.py    luồng đọc khung + luồng suy luận cho camera; mở nguồn, kết nối lại
web/overlay.py     vẽ viền đỏ + huy hiệu nhãn (Pillow, tiếng Việt có dấu)
web/recorder.py    gộp cửa sổ báo động thành sự kiện, lưu ảnh chụp
web/uploads.py     hàng đợi phân tích video tải lên
web/replay.py      phát lại MJPEG có lớp phủ
web/db.py          SQLite: settings, events, videos
web/static/        HTML/CSS/JS thuần (không cần build)
web/data/          app.db, snapshots/, uploads/ (dữ liệu cục bộ)
```
