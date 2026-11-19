// Dùng chung cho mọi trang: nhãn, âm thanh (Web Audio), toast, SSE.
const App = (() => {
  let labels = {};
  let audioCtx = null;
  let soundOn = false;
  try { soundOn = localStorage.getItem("soundOn") === "1"; } catch (e) {}

  async function loadLabels() {
    const res = await fetch("/api/labels");
    const list = await res.json();
    labels = Object.fromEntries(list.map(l => [l.name, l]));
    return list;
  }
  const info = name => labels[name] || { name, display: (name || "").toUpperCase(), color: "#64748b", tone: 760 };

  function ensureAudio() {
    if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === "suspended") audioCtx.resume();
  }
  function beep(freq = 880) {
    if (!soundOn || !audioCtx) return;
    const t0 = audioCtx.currentTime;
    for (let i = 0; i < 3; i++) {                 // 3 tiếng bíp ngắn
      const osc = audioCtx.createOscillator(), g = audioCtx.createGain();
      osc.type = "square"; osc.frequency.value = freq;
      const s = t0 + i * 0.22;
      g.gain.setValueAtTime(0.0001, s);
      g.gain.exponentialRampToValueAtTime(0.25, s + 0.01);
      g.gain.exponentialRampToValueAtTime(0.0001, s + 0.16);
      osc.connect(g).connect(audioCtx.destination);
      osc.start(s); osc.stop(s + 0.17);
    }
  }
  function bindSoundButton(btn) {
    const render = () => { btn.textContent = soundOn ? "🔊 Âm thanh: bật" : "🔇 Âm thanh: tắt"; };
    render();
    btn.addEventListener("click", () => {
      soundOn = !soundOn;
      try { localStorage.setItem("soundOn", soundOn ? "1" : "0"); } catch (e) {}
      if (soundOn) { ensureAudio(); beep(660); }
      render();
    });
    // Trình duyệt chỉ cho phát âm sau một thao tác của người dùng
    document.addEventListener("click", () => { if (soundOn) ensureAudio(); }, { once: true });
  }

  // màu chữ trên nền màu nhãn: trắng nếu tương phản >= 4.5, ngược lại gần đen (vd. nền vàng TÉ NGÃ)
  function inkFor(hex) {
    const v = (hex || "#64748b").replace("#", "");
    const ch = i => { const c = parseInt(v.substr(i, 2), 16) / 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; };
    const L = 0.2126 * ch(0) + 0.7152 * ch(2) + 0.0722 * ch(4);
    return 1.05 / (L + 0.05) >= 4.5 ? "#ffffff" : "#111111";
  }

  function toast(title, sub, color, ms = 6000) {
    const box = document.getElementById("toasts");
    if (!box) return;
    const el = document.createElement("div");
    el.className = "toast";
    el.style.setProperty("--c", color);
    el.style.setProperty("--ink", inkFor(color));
    const b = document.createElement("b"); b.textContent = title;
    const s = document.createElement("small"); s.textContent = sub;
    el.append(b, s);
    box.prepend(el);
    setTimeout(() => el.remove(), ms);
    while (box.children.length > 4) box.lastChild.remove();
  }

  function events(handlers) {
    const es = new EventSource("/events");
    for (const [name, fn] of Object.entries(handlers)) {
      es.addEventListener(name, e => fn(JSON.parse(e.data)));
    }
    es.onerror = () => handlers.disconnected && handlers.disconnected();
    return es;
  }

  const fmtTime = ts => new Date(ts * 1000).toLocaleTimeString("vi-VN");
  const pct = v => (v * 100).toFixed(0) + "%";

  return { loadLabels, info, beep, bindSoundButton, toast, events, fmtTime, pct, inkFor };
})();
