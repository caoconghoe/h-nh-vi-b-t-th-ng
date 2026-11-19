// Bảng "Xác suất theo thời gian": biểu đồ đường (hoặc bảng) xác suất 4 nhãn của từng cửa sổ trong phiên,
// kèm xác suất cao nhất của từng nhãn. Dữ liệu: GET /api/probs/<live|video>, cập nhật qua SSE "probs".
const ProbsPanel = (() => {
  const $ = id => document.getElementById(id);
  const NS = "http://www.w3.org/2000/svg";
  const CHART_N = 90;                       // số cửa sổ gần nhất vẽ trên biểu đồ (Bảng hiện đủ cả phiên)
  let labels = [], source = "live", data = null, view = "chart", hoverI = null;

  const fmtT = t => {
    if (data?.time_kind === "video") return `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, "0")}`;
    return new Date(t * 1000).toLocaleTimeString("vi-VN");
  };
  const el = (tag, attrs = {}, parent) => {
    const e = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    if (parent) parent.append(e);
    return e;
  };

  async function load() {
    try {
      const r = await fetch(`/api/probs/${source}`);
      data = await r.json();
    } catch (e) { data = { windows: [], max: {}, thr: {}, time_kind: source === "video" ? "video" : "clock" }; }
    render();
  }

  function setSource(src) {
    if (src === source && data) return;
    source = src; hoverI = null;
    $("pcSource").textContent = src === "video" ? "· video đang phát" : "· camera trực tiếp";
    load();
  }

  function onProbs(m) {
    if (m.source !== source || !data) return;
    data.windows.push(m.w);
    if (data.windows.length > 1500) data.windows.shift();
    data.max = m.max;
    data.n = m.w.i;                           // số thứ tự cửa sổ trong phiên
    render();
  }
  function onReset(m) { if (m.source === source) load(); }
  function onSettings() { load(); }          // ngưỡng có thể đã đổi

  function render() {
    if (!data) return;
    const ws = data.windows;
    $("pcEmpty").hidden = ws.length > 0;
    $("pcEmpty").textContent = source === "video"
      ? "Chưa có cửa sổ nào. Phát video ở tab Tải video để bắt đầu đo."
      : "Chưa có cửa sổ nào. Bật camera để bắt đầu đo (mỗi chu kỳ thêm 1 cửa sổ).";
    renderMax();
    $("pcChartWrap").hidden = view !== "chart" || !ws.length;
    $("pcLegend").hidden = view !== "chart" || !ws.length;
    $("pcTableWrap").hidden = view !== "table" || !ws.length;
    if (!ws.length) return;
    if (view === "chart") renderChart(); else renderTable();
  }

  function renderMax() {
    const box = $("pcMax");
    box.innerHTML = "";
    for (const l of labels) {
      const m = data.max?.[l.name];
      const d = document.createElement("div");
      d.className = "pc-stat";
      d.innerHTML = `<i></i><span class="pc-stat-name"></span><b></b><small class="muted"></small>`;
      d.querySelector("i").style.background = l.color;
      d.querySelector(".pc-stat-name").textContent = `${l.display} cao nhất`;
      d.querySelector("b").textContent = m ? m.p.toFixed(2) : "—";
      d.querySelector("small").textContent = m ? `lúc ${fmtT(m.t)}` : "";
      box.append(d);
    }
    $("pcCount").textContent = data.windows.length ? `${data.n} cửa sổ trong phiên` : "";
  }

  function renderChart() {
    const ws = data.windows.slice(-CHART_N);
    const svg = $("pcChart");
    const W = Math.max(320, $("pcChartWrap").clientWidth), H = 210;
    const m = { l: 34, r: 160, t: 14, b: 24 };
    const iw = W - m.l - m.r, ih = H - m.t - m.b;
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.setAttribute("width", W); svg.setAttribute("height", H);
    svg.innerHTML = "";
    const x = i => m.l + (ws.length === 1 ? iw : (i / (ws.length - 1)) * iw);
    const y = p => m.t + (1 - p) * ih;
    const g = el("g", {}, svg);
    // dải báo động (nền đỏ nhạt) ở các cửa sổ đang báo động
    const step = ws.length > 1 ? iw / (ws.length - 1) : iw;
    ws.forEach((w, i) => { if (w.alarm) el("rect", { x: x(i) - step / 2, y: m.t, width: step, height: ih, class: "pc-alarm" }, g); });
    // lưới + trục y (mờ)
    for (const p of [0, 0.25, 0.5, 0.75, 1]) {
      el("line", { x1: m.l, x2: m.l + iw, y1: y(p), y2: y(p), class: "pc-grid" }, g);
      const t = el("text", { x: m.l - 6, y: y(p) + 4, class: "pc-axis", "text-anchor": "end" }, g); t.textContent = p.toFixed(2).replace(/0$/, "");
    }
    // đường ngưỡng (gộp các nhãn có cùng ngưỡng)
    const byThr = {};
    for (const l of labels) if (l.alert && data.thr?.[l.name] != null) (byThr[data.thr[l.name]] ||= []).push(l.display);
    for (const [thr, names] of Object.entries(byThr)) {
      el("line", { x1: m.l, x2: m.l + iw, y1: y(+thr), y2: y(+thr), class: "pc-thr" }, g);
      const t = el("text", { x: m.l + 4, y: y(+thr) - 4, class: "pc-axis" }, g);
      t.textContent = `ngưỡng ${(+thr).toFixed(2)}` + (Object.keys(byThr).length > 1 ? ` (${names.join(", ")})` : "");
    }
    // vạch ngắt: video phát lại/tua (thời gian lùi) hoặc camera gián đoạn > 15 giây
    for (let i = 1; i < ws.length; i++) {
      const back = ws[i].t < ws[i - 1].t, gap = data.time_kind !== "video" && ws[i].t - ws[i - 1].t > 15;
      if (!back && !gap) continue;
      const bx = (x(i) + x(i - 1)) / 2;
      el("line", { x1: bx, x2: bx, y1: m.t, y2: m.t + ih, class: "pc-break" }, g);
      const t = el("text", { x: bx + 3, y: m.t + 9, class: "pc-axis" }, g);
      t.textContent = back ? "phát lại / tua" : "gián đoạn";
    }
    // trục x: đầu, giữa, cuối
    const idx = [...new Set([0, Math.floor((ws.length - 1) / 2), ws.length - 1])];
    for (const i of idx) {
      const t = el("text", { x: x(i), y: H - 6, class: "pc-axis", "text-anchor": i === 0 ? "start" : i === ws.length - 1 ? "end" : "middle" }, g);
      t.textContent = fmtT(ws[i].t);
    }
    // đường xác suất (2px) — màu theo nhãn
    for (const l of labels) {
      const pts = ws.map((w, i) => `${x(i).toFixed(1)},${y(w.probs[l.name] ?? 0).toFixed(1)}`).join(" ");
      el("polyline", { points: pts, fill: "none", stroke: l.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, g);
      if (ws.length === 1) el("circle", { cx: x(0), cy: y(ws[0].probs[l.name] ?? 0), r: 4, fill: l.color }, g);
    }
    // nhãn trực tiếp ở cuối đường (chữ màu chữ thường, gạch màu nhãn), tránh chồng nhau
    const last = ws[ws.length - 1];
    const tags = labels.map(l => ({ l, p: last.probs[l.name] ?? 0, y: y(last.probs[l.name] ?? 0) })).sort((a, b) => a.y - b.y);
    for (let i = 1; i < tags.length; i++) tags[i].y = Math.max(tags[i].y, tags[i - 1].y + 13);
    const over = tags.length ? tags[tags.length - 1].y - (m.t + ih) : 0;
    if (over > 0) tags.forEach(t => { t.y -= over; });
    for (const t of tags) {
      el("line", { x1: m.l + iw + 6, x2: m.l + iw + 16, y1: t.y, y2: t.y, stroke: t.l.color, "stroke-width": 3, "stroke-linecap": "round" }, g);
      const tx = el("text", { x: m.l + iw + 20, y: t.y + 4, class: "pc-tag" }, g);
      tx.textContent = `${t.l.display} ${t.p.toFixed(2)}`;
    }
    // lớp rê chuột: đường dọc + chấm + khung chi tiết
    const cross = el("line", { y1: m.t, y2: m.t + ih, class: "pc-cross", visibility: "hidden" }, g);
    const dots = labels.map(l => el("circle", { r: 4, fill: l.color, class: "pc-dot", visibility: "hidden" }, g));
    const hit = el("rect", { x: m.l, y: m.t, width: iw, height: ih, fill: "transparent" }, g);
    const tip = $("pcTip");
    const show = ev => {
      const r = svg.getBoundingClientRect();
      const px = (ev.clientX - r.left) * (W / r.width);
      const i = Math.max(0, Math.min(ws.length - 1, Math.round((px - m.l) / (step || 1))));
      const w = ws[i];
      cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
      labels.forEach((l, k) => { dots[k].setAttribute("cx", x(i)); dots[k].setAttribute("cy", y(w.probs[l.name] ?? 0)); dots[k].setAttribute("visibility", "visible"); });
      const rows = labels.map(l => ({ l, p: w.probs[l.name] ?? 0 })).sort((a, b) => b.p - a.p);
      tip.innerHTML = `<b></b>${rows.map(() => `<div><i></i><span></span><em></em></div>`).join("")}`;
      tip.querySelector("b").textContent = `${fmtT(w.t)}${w.alarm ? " · BÁO ĐỘNG" : ""}`;
      tip.querySelectorAll("div").forEach((d, k) => {
        d.querySelector("i").style.background = rows[k].l.color;
        d.querySelector("span").textContent = rows[k].l.display;
        d.querySelector("em").textContent = rows[k].p.toFixed(2);
      });
      tip.hidden = false;
      const left = (x(i) / W) * r.width;
      tip.style.left = Math.min(r.width - tip.offsetWidth - 4, Math.max(4, left + 12)) + "px";
    };
    hit.addEventListener("pointermove", show);
    hit.addEventListener("pointerdown", show);
    hit.addEventListener("pointerleave", () => {
      tip.hidden = true; cross.setAttribute("visibility", "hidden"); dots.forEach(d => d.setAttribute("visibility", "hidden"));
    });
    svg.setAttribute("aria-label", `Xác suất ${labels.map(l => l.display).join(", ")} theo ${ws.length} cửa sổ gần nhất`);
  }

  function renderTable() {
    const head = $("pcHead"), body = $("pcBody");
    head.innerHTML = ""; body.innerHTML = "";
    const hr = head.insertRow();
    for (const h of ["Thời điểm", "Nhãn", ...labels.map(l => l.display), "Báo động"]) {
      const th = document.createElement("th"); th.textContent = h; hr.append(th);
    }
    const ws = data.windows.slice(-400).reverse();     // mới nhất ở trên
    for (const w of ws) {
      const tr = body.insertRow();
      tr.insertCell().textContent = fmtT(w.t);
      const info = labels.find(l => l.name === w.label) || { display: w.label, color: "#64748b" };
      const c = tr.insertCell(), chip = document.createElement("span");
      chip.className = "chip"; chip.textContent = info.display;
      chip.style.setProperty("--c", info.color); chip.style.setProperty("--ink", App.inkFor(info.color));
      c.append(chip);
      for (const l of labels) {
        const td = tr.insertCell(); td.className = "num";
        const p = w.probs[l.name] ?? 0;
        td.textContent = p.toFixed(2);
        if (l.name === w.label) td.style.fontWeight = 700;
      }
      tr.insertCell().textContent = w.alarm ? "●" : "";
      if (w.alarm) tr.classList.add("alarm-row");
    }
  }

  function init(labelList) {
    labels = labelList;
    const lg = $("pcLegend");
    for (const l of labels) {
      const s = document.createElement("span");
      s.innerHTML = "<i></i>"; s.firstChild.style.background = l.color; s.append(l.display);
      lg.append(s);
    }
    for (const r of document.querySelectorAll("input[name=pcView]")) {
      r.addEventListener("change", () => { view = r.value; render(); });
    }
    $("pcReset").addEventListener("click", async () => {
      try {
        const r = await fetch(`/api/probs/${source}/reset`, { method: "POST" });
        data = await r.json(); render();
        App.toast("Đã xóa số liệu phiên", source === "video" ? "Video" : "Camera trực tiếp", "#64748b", 2500);
      } catch (e) { App.toast("Lỗi", e.message, "#64748b"); }
    });
    window.addEventListener("resize", () => { if (view === "chart") render(); });
  }

  return { init, setSource, onProbs, onReset, onSettings };
})();
