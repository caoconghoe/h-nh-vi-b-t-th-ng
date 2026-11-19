(async () => {
  const $ = id => document.getElementById(id);
  const form = $("form");
  App.bindSoundButton($("soundBtn"));
  const alertLabels = (await App.loadLabels()).filter(l => l.alert);   // nhãn lấy từ model, không hardcode
  let saved = null, lastInferMs = null;

  // --- bảng ngưỡng/K riêng theo nhãn
  for (const l of alertLabels) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td><span class="chip"></span></td>
      <td><input type="number" min="0.05" max="0.99" step="0.01" data-thr="${l.name}" aria-label="Ngưỡng ${l.display}"></td>
      <td><input type="number" min="1" max="10" step="1" data-k="${l.name}" aria-label="K ${l.display}"></td>
      <td class="muted small" data-eff="${l.name}"></td>`;
    const chip = tr.querySelector(".chip");
    chip.textContent = l.display; chip.style.setProperty("--c", l.color); chip.style.setProperty("--ink", App.inkFor(l.color));
    $("perLabel").append(tr);
  }
  const numOrNull = el => el.value.trim() === "" ? null : +el.value;
  function perLabelValues() {
    const thr = {}, k = {};
    for (const el of document.querySelectorAll("[data-thr]")) thr[el.dataset.thr] = numOrNull(el);
    for (const el of document.querySelectorAll("[data-k]")) k[el.dataset.k] = numOrNull(el);
    return { thr, k };
  }
  function renderEffective() {
    const gThr = +$("thr").value, gK = +$("k").value, { thr, k } = perLabelValues();
    for (const l of alertLabels) {
      const t = thr[l.name] ?? gThr, kk = k[l.name] ?? gK;
      document.querySelector(`[data-eff="${l.name}"]`).textContent =
        `≥ ${t.toFixed(2)} trong ${kk} cửa sổ liên tiếp` + (thr[l.name] == null && k[l.name] == null ? " (chung)" : " (riêng)");
      document.querySelector(`[data-thr="${l.name}"]`).placeholder = `chung ${gThr.toFixed(2)}`;
      document.querySelector(`[data-k="${l.name}"]`).placeholder = `chung ${gK}`;
    }
  }

  function fill(s) {
    saved = s;
    $("thr").value = s.thr; $("k").value = s.k; $("step").value = s.step;
    $("cooldown").value = s.cooldown; $("videoStep").value = s.video_step;
    for (const el of document.querySelectorAll("[data-thr]")) el.value = s.thr_by_label?.[el.dataset.thr] ?? "";
    for (const el of document.querySelectorAll("[data-k]")) el.value = s.k_by_label?.[el.dataset.k] ?? "";
    updateHints();
  }
  function updateHints() {
    renderEffective();
    $("thrVal").textContent = (+$("thr").value).toFixed(2);
    const step = +$("step").value || 2, k = +$("k").value || 2;
    const inf = lastInferMs ? lastInferMs / 1000 : null;
    const period = inf ? Math.max(step, inf * 1.15) : step;
    $("stepHint").textContent = inf
      ? `Suy luận mất ~${inf.toFixed(1)} s trên máy này → thực tế mỗi ${period.toFixed(1)} s phân tích một lần.`
      : "Mỗi bao nhiêu giây lấy 3 giây gần nhất để phân tích (không nhanh hơn thời gian suy luận).";
    const lo = 1.5 + (k - 1) * period + (inf || 1.4), hi = 3 + k * period + (inf || 1.4);
    $("delayHint").textContent = `Ước tính độ trễ từ lúc hành động bắt đầu đến lúc báo động: khoảng ${lo.toFixed(0)}–${hi.toFixed(0)} giây.`;
  }

  const STATUS = { running: "Đang chạy", opening: "Đang mở nguồn", reconnecting: "Mất kết nối – đang kết nối lại",
                   error: "Lỗi", stopped: "Đã dừng" };
  function renderState(s) {
    const st = $("srcStatus");
    st.className = "status-dot " + s.status;
    st.textContent = s.message || STATUS[s.status] || s.status;
    $("stSource").textContent = !s.running || !s.source ? "—"
      : s.kind === "phone" ? `Điện thoại · ${s.source}` : `Camera laptop · số ${s.source}`;
    $("stFps").textContent = s.running && s.fps ? s.fps.toFixed(1) : "—";
    if (s.infer_ms) { lastInferMs = s.infer_ms; updateHints(); }
    $("stInfer").textContent = lastInferMs ? (lastInferMs / 1000).toFixed(2) + " s" : "—";
  }
  App.events({
    state: renderState, status: renderState,
    alert: a => { App.toast(a.display, `Tin cậy ${App.pct(a.conf)} · ${App.fmtTime(a.ts)}`, a.color); App.beep(a.tone || 880); },
  });

  form.addEventListener("input", () => { updateHints(); $("saveMsg").textContent = "Có thay đổi chưa lưu"; });
  $("resetBtn").addEventListener("click", () => { fill(saved); $("saveMsg").textContent = ""; });
  form.addEventListener("submit", async e => {
    e.preventDefault();
    const { thr, k } = perLabelValues();
    const body = { thr: +$("thr").value, k: +$("k").value, step: +$("step").value,
                   cooldown: +$("cooldown").value, video_step: +$("videoStep").value,
                   thr_by_label: thr, k_by_label: k };
    $("saveBtn").disabled = true;
    try {
      const r = await fetch("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || r.statusText);
      fill(d.settings); renderState(d.state);
      $("saveMsg").textContent = "Đã lưu và áp dụng lúc " + new Date().toLocaleTimeString("vi-VN");
    } catch (err) {
      App.toast("Không lưu được", err.message, "#64748b", 8000);
    } finally { $("saveBtn").disabled = false; }
  });

  fill(await (await fetch("/api/settings")).json());
  renderState(await (await fetch("/api/state")).json());
})();
