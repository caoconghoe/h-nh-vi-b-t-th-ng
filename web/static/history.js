(async () => {
  const $ = id => document.getElementById(id);
  const PAGE = 50;
  let offset = 0, total = 0, current = null;
  App.bindSoundButton($("soundBtn"));

  const labelList = await App.loadLabels();
  for (const l of labelList.filter(l => l.alert)) {
    const o = document.createElement("option");
    o.value = l.name; o.textContent = l.display;
    $("fLabel").append(o);
  }

  const fmtDateTime = ts => new Date(ts * 1000).toLocaleString("vi-VN");
  const fmtDur = s => s < 60 ? `${Math.round(s)} giây` : `${Math.floor(s / 60)} phút ${Math.round(s % 60)} giây`;
  const srcName = ev => ev.source_type === "video" ? ev.source
    : ev.source_type === "phone" ? `Điện thoại · ${/^\d+$/.test(ev.source) ? "camera ảo số " + ev.source : ev.source}`
    : `Camera laptop · số ${ev.source}`;
  const SRC_TYPE = { laptop: "Camera laptop", phone: "Camera điện thoại", video: "Video tải lên" };
  const replayUrl = ev => `/?tab=video&video=${encodeURIComponent(ev.video_id)}&t=${(ev.video_start || 0).toFixed(2)}`;
  const mmss = s => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
  const inVideo = ev => ev.video_start != null ? `trong video ${mmss(ev.video_start)}–${mmss(ev.video_end)}` : "";
  const imgUrl = (name, ev) => name ? `/snapshots/${encodeURIComponent(name)}?v=${ev.best_ts || ev.end_ts}` : "";

  function query() {
    const p = new URLSearchParams();
    for (const [k, v] of new FormData($("filters"))) if (v) p.set(k, v);
    p.set("limit", PAGE); p.set("offset", offset);
    return p;
  }

  async function load() {
    const r = await fetch("/api/events?" + query());
    if (!r.ok) { App.toast("Lỗi tải lịch sử", (await r.json()).detail || r.statusText, "#64748b"); return; }
    const data = await r.json();
    total = data.total;
    render(data.items);
  }

  function chip(ev) {
    const c = document.createElement("span");
    c.className = "chip"; c.style.setProperty("--c", ev.color); c.style.setProperty("--ink", App.inkFor(ev.color)); c.textContent = ev.display;
    return c;
  }

  function render(items) {
    const tb = $("rows");
    tb.innerHTML = "";
    $("count").textContent = total ? `${total} sự kiện` : "Không có sự kiện nào.";
    const page = Math.floor(offset / PAGE) + 1, pages = Math.max(1, Math.ceil(total / PAGE));
    $("pageInfo").textContent = `Trang ${page}/${pages}`;
    $("prevBtn").disabled = offset === 0;
    $("nextBtn").disabled = offset + PAGE >= total;
    for (const ev of items) {
      const tr = document.createElement("tr");
      if (ev.false_positive) tr.classList.add("fp");
      const cells = Array.from({ length: 7 }, () => tr.insertCell());
      if (ev.thumb) {
        const im = document.createElement("img");
        im.className = "thumb"; im.loading = "lazy"; im.alt = ev.display; im.src = imgUrl(ev.thumb, ev);
        cells[0].append(im);
      } else cells[0].textContent = "—";
      cells[1].innerHTML = `<div></div><small class="muted"></small>`;
      cells[1].children[0].textContent = fmtDateTime(ev.start_ts);
      cells[1].children[1].textContent = (ev.ongoing ? "đang diễn ra · " : "") + fmtDur(ev.end_ts - ev.start_ts);
      cells[2].append(chip(ev));
      cells[3].textContent = App.pct(ev.max_conf);
      cells[3].className = "num";
      cells[4].textContent = srcName(ev);
      cells[4].title = ev.source;
      cells[4].className = "src";
      if (inVideo(ev)) {
        const sm = document.createElement("small"); sm.className = "muted"; sm.style.display = "block";
        sm.textContent = inVideo(ev); cells[4].append(sm);
      }
      cells[5].textContent = ev.false_positive ? "Báo nhầm" : "—";
      const fpBtn = document.createElement("button");
      fpBtn.type = "button"; fpBtn.className = "btn ghost sm";
      fpBtn.textContent = ev.false_positive ? "Bỏ đánh dấu" : "Đánh dấu báo nhầm";
      fpBtn.addEventListener("click", e => { e.stopPropagation(); toggleFp(ev); });
      const delBtn = document.createElement("button");
      delBtn.type = "button"; delBtn.className = "btn ghost sm"; delBtn.textContent = "Xóa";
      delBtn.addEventListener("click", e => { e.stopPropagation(); del(ev); });
      cells[6].className = "row-actions";
      if (ev.video_id) {
        const rp = document.createElement("a");
        rp.className = "btn sm"; rp.href = replayUrl(ev); rp.textContent = "▶ Xem lại";
        rp.title = "Xem lại đoạn này trong khung Giám sát";
        rp.addEventListener("click", e => e.stopPropagation());
        cells[6].append(rp);
      }
      cells[6].append(fpBtn, delBtn);
      tr.addEventListener("click", () => openViewer(ev));
      tb.append(tr);
    }
  }

  async function toggleFp(ev) {
    const r = await fetch(`/api/events/${ev.id}/false_positive`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value: !ev.false_positive }),
    });
    if (!r.ok) return App.toast("Lỗi", (await r.json()).detail || r.statusText, "#64748b");
    const upd = await r.json();
    if (current && current.id === upd.id) fillViewer(upd);
    load();
  }

  function askDelete(ev) {
    if (!ev.video_id) return Promise.resolve(confirm(`Xóa sự kiện "${ev.display}" lúc ${fmtDateTime(ev.start_ts)}? Ảnh chụp cũng bị xóa.`) ? "event" : "cancel");
    $("delName").textContent = ev.source;
    const dlg = $("askDel");
    dlg.returnValue = "cancel";
    dlg.showModal();
    return new Promise(res => dlg.addEventListener("close", () => res(dlg.returnValue || "cancel"), { once: true }));
  }
  async function del(ev) {
    const how = await askDelete(ev);
    if (how === "cancel") return;
    const r = await fetch(`/api/events/${ev.id}${how === "video" ? "?with_video=1" : ""}`, { method: "DELETE" });
    if (!r.ok) return App.toast("Lỗi", (await r.json()).detail || r.statusText, "#64748b");
    if (current && current.id === ev.id) $("viewer").close();
    load();
  }

  // ----- xem ảnh lớn
  function fillViewer(ev) {
    current = ev;
    $("vImg").src = imgUrl(ev.snapshot, ev);
    $("vTitle").replaceChildren(chip(ev));
    const st = $("vStats");
    st.innerHTML = "";
    const add = (k, v) => { const dt = document.createElement("dt"), dd = document.createElement("dd"); dt.textContent = k; dd.textContent = v; st.append(dt, dd); };
    add("Bắt đầu", fmtDateTime(ev.start_ts));
    add("Kết thúc", fmtDateTime(ev.end_ts) + (ev.ongoing ? " (đang diễn ra)" : ""));
    add("Thời lượng", fmtDur(ev.end_ts - ev.start_ts));
    add("Tin cậy cao nhất", App.pct(ev.max_conf));
    if (ev.best_ts) add("Lúc tin cậy cao nhất", fmtDateTime(ev.best_ts));
    add("Nguồn", `${SRC_TYPE[ev.source_type] || ""}${ev.source_type === "video" ? " · " + ev.source : ""}`);
    if (ev.source_type !== "video") add("Thiết bị", srcName(ev));
    if (inVideo(ev)) add("Vị trí", inVideo(ev));
    add("Trạng thái", ev.false_positive ? "Báo nhầm" : "Chưa đánh dấu");
    const pb = $("vProbs");
    pb.innerHTML = "";
    for (const l of labelList) {
      const p = ev.probs?.[l.name] ?? 0;
      const row = document.createElement("div");
      row.className = "prob-row";
      row.innerHTML = `<span></span><div class="prob-bar"><span></span></div><span class="prob-val"></span>`;
      row.children[0].textContent = l.display;
      row.children[1].firstChild.style.cssText = `background:${l.color};width:${(p * 100).toFixed(1)}%`;
      row.children[2].textContent = (p * 100).toFixed(1) + "%";
      pb.append(row);
    }
    $("vFp").textContent = ev.false_positive ? "Bỏ đánh dấu báo nhầm" : "Đánh dấu báo nhầm";
    $("vReplay").hidden = !ev.video_id;
    if (ev.video_id) $("vReplay").href = replayUrl(ev);
  }
  function openViewer(ev) { fillViewer(ev); $("viewer").showModal(); }
  $("vClose").addEventListener("click", () => $("viewer").close());
  $("viewer").addEventListener("click", e => { if (e.target === $("viewer")) $("viewer").close(); });
  $("vFp").addEventListener("click", () => current && toggleFp(current));
  $("vDel").addEventListener("click", () => current && del(current));
  $("viewer").addEventListener("close", () => { current = null; });

  // ----- lọc, phân trang, xóa tất cả
  $("filters").addEventListener("change", () => { offset = 0; load(); });
  $("resetBtn").addEventListener("click", () => { $("filters").reset(); offset = 0; load(); });
  $("prevBtn").addEventListener("click", () => { offset = Math.max(0, offset - PAGE); load(); });
  $("nextBtn").addEventListener("click", () => { offset += PAGE; load(); });
  $("deleteAllBtn").addEventListener("click", async () => {
    if (!total) return;
    if (!confirm("Xóa TẤT CẢ sự kiện và ảnh chụp (mọi nguồn, không theo bộ lọc)? Không thể hoàn tác.")) return;
    const withVideos = confirm("Xóa luôn các file video đã tải lên (để giải phóng dung lượng)?\n\n" +
                               "OK = xóa cả video · Cancel = giữ video, chỉ xóa sự kiện");
    const r = await fetch(`/api/events?confirm=yes${withVideos ? "&with_videos=1" : ""}`, { method: "DELETE" });
    const d = await r.json();
    if (!r.ok) return App.toast("Lỗi", d.detail || r.statusText, "#64748b");
    App.toast("Đã xóa", `${d.deleted} sự kiện${d.videos_deleted ? `, ${d.videos_deleted} video` : ""}`, "#64748b", 3000);
    offset = 0; load();
  });

  // ----- cập nhật trực tiếp: sự kiện mới / kéo dài / kết thúc, và cảnh báo
  let pending = null;
  const reloadSoon = () => { clearTimeout(pending); pending = setTimeout(load, 500); };
  App.events({
    event: () => { if (offset === 0) reloadSoon(); },
    alert: a => { App.toast(a.display, `Tin cậy ${App.pct(a.conf)} · ${App.fmtTime(a.ts)}`, a.color); App.beep(a.tone || 880); },
  });

  load();

  // mở thẳng một sự kiện: /history?event=ID (từ trang Tải video)
  const evId = new URLSearchParams(location.search).get("event");
  if (evId) {
    const r = await fetch(`/api/events/${encodeURIComponent(evId)}`);
    if (r.ok) openViewer(await r.json());
    else App.toast("Không tìm thấy", "Sự kiện có thể đã bị xóa.", "#64748b");
  }
})();
