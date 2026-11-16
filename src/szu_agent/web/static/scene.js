// 荔园背景：只有天空。颜色随一天的时间变化，大团柔和的云像隔着磨砂玻璃看到的一样，天气会改变云量和明暗。
//
// 实现：
//   - 天空是 CSS 渐变；云是一组普通 div（径向渐变 + CSS blur）。只对云做 transform 动画，
//     浏览器把模糊后的云当成图层交给 GPU 平移，不需要每帧重新算模糊，所以大半径模糊也不卡
//   - 颜色由一天中的关键时刻插值得到，连续变化；所有颜色通过 CSS 变量下发，之后每 30 秒只改变量
//   - 首次显示前先把变量算好、并暂时关闭过渡，避免页面一打开时出现从默认值“渐变”过来的闪烁
//   - 纯函数 palette / 云布局可以在 Node 里调用，previewSVG() 用同样的数据画一张近似图，用来离线检查
//   - 调试/演示：?t=19:30 固定时刻；?t=cycle 60 秒快进一整天；?w=rain / overcast / clear 模拟天气
(function (root) {
  "use strict";

  // ---------------- 颜色关键帧 ----------------
  // sky: 顶 / 中 / 底；haze: 底部的亮雾；cloud: 云受光面；glow: 太阳附近的暖光
  const KEYS = [
    { h: 0,    sky: ["#0d1226", "#1e2340", "#353456"], haze: ["#4a4568", 0.35], cloud: ["#5b5f86", 0.45], glow: ["#6d5a8f", 0.0], stars: 0.6 },
    { h: 5.0,  sky: ["#1a2142", "#39385f", "#6a5978"], haze: ["#8a6f86", 0.4],  cloud: ["#7a7396", 0.5],  glow: ["#c08a8f", 0.2], stars: 0.4 },
    { h: 6.2,  sky: ["#5468a4", "#b39bbd", "#f2c4a8"], haze: ["#ffd8c0", 0.7],  cloud: ["#fbe0d6", 0.85], glow: ["#ffc69a", 0.7], stars: 0 },
    { h: 8.0,  sky: ["#5f93d0", "#a9c7e6", "#e9eef3"], haze: ["#f6f1ea", 0.5],  cloud: ["#ffffff", 0.95], glow: ["#fff3d6", 0.5], stars: 0 },
    { h: 12,   sky: ["#4f8fd8", "#94bde6", "#d6e4f1"], haze: ["#eef2f6", 0.45], cloud: ["#ffffff", 1],    glow: ["#fffbe8", 0.55], stars: 0 },
    { h: 16.5, sky: ["#5a8fcc", "#a3c2e0", "#efe4d4"], haze: ["#f8ecdc", 0.55], cloud: ["#fffaf2", 0.95], glow: ["#ffe6b8", 0.55], stars: 0 },
    { h: 17.9, sky: ["#6278b8", "#c9a3b3", "#f5c39b"], haze: ["#ffd6b4", 0.8],  cloud: ["#ffe2cf", 0.9],  glow: ["#ffb57a", 0.75], stars: 0 },
    { h: 18.8, sky: ["#454d8e", "#9c7a9e", "#e5a184"], haze: ["#f0b79a", 0.7],  cloud: ["#e9b8b0", 0.8],  glow: ["#ff9a6e", 0.6], stars: 0.05 },
    { h: 19.8, sky: ["#232a55", "#4a4672", "#7b6782"], haze: ["#8b7489", 0.45], cloud: ["#8d84a8", 0.6],  glow: ["#a27791", 0.15], stars: 0.35 },
    { h: 21,   sky: ["#121833", "#262a4b", "#3f3c5e"], haze: ["#55506f", 0.35], cloud: ["#646891", 0.5],  glow: ["#6d5a8f", 0.0], stars: 0.6 },
    { h: 24,   sky: ["#0d1226", "#1e2340", "#353456"], haze: ["#4a4568", 0.35], cloud: ["#5b5f86", 0.45], glow: ["#6d5a8f", 0.0], stars: 0.6 },
  ];
  const SUNRISE = 6.0, SUNSET = 18.8;

  const hex = (c) => [1, 3, 5].map((i) => parseInt(c.slice(i, i + 2), 16));
  const toHex = (rgb) => "#" + rgb.map((v) => Math.round(Math.max(0, Math.min(255, v))).toString(16).padStart(2, "0")).join("");
  const mix = (a, b, t) => toHex(hex(a).map((v, i) => v + (hex(b)[i] - v) * t));
  const lerp = (a, b, t) => a + (b - a) * t;
  const smooth = (t) => t * t * (3 - 2 * t);
  const rgbTriplet = (c) => hex(c).join(" ");

  function palette(h) {
    h = ((h % 24) + 24) % 24;
    let i = 0;
    while (KEYS[i + 1].h < h) i++;
    const a = KEYS[i], b = KEYS[i + 1], t = smooth((h - a.h) / (b.h - a.h));
    const p = {
      skyTop: mix(a.sky[0], b.sky[0], t), skyMid: mix(a.sky[1], b.sky[1], t), skyLow: mix(a.sky[2], b.sky[2], t),
      haze: mix(a.haze[0], b.haze[0], t), hazeOp: lerp(a.haze[1], b.haze[1], t),
      cloud: mix(a.cloud[0], b.cloud[0], t), cloudOp: lerp(a.cloud[1], b.cloud[1], t),
      glow: mix(a.glow[0], b.glow[0], t), glowOp: lerp(a.glow[1], b.glow[1], t),
      stars: lerp(a.stars, b.stars, t),
    };
    p.cloudShade = mix(p.cloud, p.skyTop, 0.38);  // 云的背光面：带一点天空的颜色
    const [r, g, bl] = hex(p.skyMid);
    const lum = (0.2126 * r + 0.7152 * g + 0.0722 * bl) / 255;
    p.mode = lum > 0.55 ? "day" : lum > 0.32 ? "dusk" : "night";
    return p;
  }

  // 直接写在天空上的文字（左侧时钟、天气、欢迎语）用深色还是浅色：
  // 背后可能是天空上段、中段，也可能飘过一朵云；配一层浓度 0.4 的柔和光晕后，选两种字色里在最差背景上更清楚的那个。
  // 离线计算：这样全天（含阴雨）最差对比度 5.6；只看天空亮度不加光晕时，傍晚最差只有 1.6
  function lum(c) {
    const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
    const [r, g, b] = Array.isArray(c) ? c : hex(c);
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  }
  const contrast = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05); };
  const SCRIM = 0.4, INK_DARK = [30, 24, 40], INK_LIGHT = [245, 241, 251];
  function textInk(p) {
    const mixa = (a, b, t) => a.map((v, i) => v + (b[i] - v) * t);
    const bgs = [hex(p.skyTop), hex(p.skyMid), mixa(hex(p.skyMid), hex(p.cloud), Math.min(1, p.cloudOp))];
    const worst = (ink, scrim) => Math.min(...bgs.map((b) => contrast(ink, mixa(b, scrim, SCRIM))));
    return worst(INK_DARK, [255, 255, 255]) >= worst(INK_LIGHT, [14, 10, 30]) ? "dark" : "light";
  }

  // 天气：阴天云更多更灰、阳光变弱，下雨有雨丝。w = {kind, cloud(0~1)}
  function withWeather(p, w) {
    const q = { ...p, extra: 0, rain: 0, sunOp: 1 };
    if (!w) return q;
    const grey = { overcast: 0.55, fog: 0.6, drizzle: 0.65, rain: 0.8, storm: 0.9 }[w.kind] ?? (w.cloud > 0.6 ? 0.2 : 0);
    const dim = 1 - Math.min(1, p.stars * 1.6);  // 夜里本来就暗，不再压灰
    const g = grey * dim;
    q.skyTop = mix(p.skyTop, mix("#7b8494", p.skyTop, 0.2), g);
    q.skyMid = mix(p.skyMid, mix("#a5acb8", p.skyMid, 0.15), g);
    q.skyLow = mix(p.skyLow, mix("#cdd1d8", p.skyLow, 0.15), g);
    q.cloud = mix(p.cloud, "#dfe2e8", g * 0.8);
    q.cloudShade = mix(p.cloudShade, "#8f97a6", g);
    q.glowOp = p.glowOp * (1 - g);
    q.sunOp = 1 - g;
    q.extra = Math.max(0, Math.min(1, Math.max(((w.cloud ?? 0) - 0.4) / 0.4, grey > 0.5 ? 1 : 0)));
    q.rain = { drizzle: 0.35, rain: 0.7, storm: 0.9 }[w.kind] || 0;
    // 天变灰以后界面明暗也要跟着重新判断
    const [r, gg, bl] = hex(q.skyMid);
    const lum = (0.2126 * r + 0.7152 * gg + 0.0722 * bl) / 255;
    q.mode = lum > 0.55 ? "day" : lum > 0.32 ? "dusk" : "night";
    return q;
  }

  // 太阳 / 月亮的位置（占屏幕宽高的比例），沿一条低矮的弧线
  function celestial(h) {
    h = ((h % 24) + 24) % 24;
    const arc = (p) => ({ x: 0.12 + 0.76 * p, y: 0.62 - Math.sin(Math.PI * p) * 0.5 });
    if (h >= SUNRISE - 0.3 && h <= SUNSET + 0.3) return { sun: arc((h - SUNRISE + 0.3) / (SUNSET - SUNRISE + 0.6)), moon: null };
    const span = 24 - SUNSET + SUNRISE - 0.6;
    return { sun: null, moon: arc(Math.min(Math.max(((h - SUNSET - 0.3 + 24) % 24) / span, 0), 1)) };
  }

  // ---------------- 云的布局（固定随机种子，每次一样） ----------------
  function rng(seed) {
    return function () {
      seed |= 0; seed = (seed + 0x6d2b79f5) | 0;
      let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  // 每朵云 = 若干椭圆团块。坐标、尺寸都是屏幕宽高的比例。extra=true 的只在阴天出现
  function cloudLayout() {
    const r = rng(1114);
    const clouds = [];
    const add = (x, y, w, h, blobs, blur, extra, dur) => {
      const parts = [];
      for (let i = 0; i < blobs; i++) {
        parts.push({ x: r() * 0.7 + 0.15, y: 0.3 + r() * 0.4 + (i % 2) * 0.08, rx: 0.22 + r() * 0.2, ry: 0.28 + r() * 0.22, a: 0.55 + r() * 0.45 });
      }
      clouds.push({ x, y, w, h, parts, blur, extra, dur: dur || 260 + r() * 260, delay: r() * 200 });
    };
    // 高处的薄云
    add(0.05, 0.04, 0.38, 0.14, 5, 26, false, 320);
    add(0.62, 0.02, 0.42, 0.16, 6, 30, false, 380);
    // 中间的大团云
    add(0.22, 0.12, 0.34, 0.22, 7, 34, false);
    add(-0.08, 0.34, 0.36, 0.26, 7, 40, false);
    add(0.72, 0.30, 0.40, 0.26, 7, 40, false);
    // 底部的云堤：又大又亮，和地平线的雾融在一起（Hark 那种感觉的关键）
    add(-0.1, 0.66, 0.65, 0.42, 9, 60, false, 520);
    add(0.45, 0.70, 0.70, 0.40, 9, 60, false, 560);
    // 阴天才出现的
    add(0.35, 0.22, 0.45, 0.28, 8, 42, true);
    add(0.0, 0.10, 0.40, 0.22, 7, 40, true);
    add(0.55, 0.48, 0.5, 0.3, 8, 48, true);
    return clouds;
  }
  const CLOUDS = cloudLayout();

  function varValues(p) {
    return {
      "sky-top": p.skyTop, "sky-mid": p.skyMid, "sky-low": p.skyLow,
      haze: rgbTriplet(p.haze), "haze-op": p.hazeOp.toFixed(3),
      cloud: rgbTriplet(p.cloud), "cloud-shade": rgbTriplet(p.cloudShade), "cloud-op": p.cloudOp.toFixed(3),
      "cloud-extra": (p.extra * p.cloudOp).toFixed(3),
      glow: rgbTriplet(p.glow), "glow-op": (p.glowOp * (p.sunOp ?? 1)).toFixed(3),
      stars: p.stars.toFixed(3), rain: (p.rain || 0).toFixed(3), "sun-op": (p.sunOp ?? 1).toFixed(3),
    };
  }

  // ---------------- 浏览器 DOM ----------------
  function buildDOM() {
    const r = rng(77);
    let s = `<div class="sky-grad"></div><div class="sky-glow" id="sc-sun"></div><div class="sky-moon" id="sc-moon"></div><div class="sky-stars">`;
    for (let i = 0; i < 70; i++) s += `<i style="left:${(r() * 100).toFixed(2)}%;top:${(r() * 55).toFixed(2)}%;--d:${(2 + r() * 4).toFixed(1)}s;--delay:-${(r() * 5).toFixed(1)}s;--s:${(1 + r() * 1.4).toFixed(1)}px"></i>`;
    s += `</div>`;
    for (const c of CLOUDS) {
      s += `<div class="cloud${c.extra ? " extra" : ""}" style="left:${c.x * 100}%;top:${c.y * 100}%;width:${c.w * 100}vw;height:${c.h * 100}vh;--blur:${c.blur}px;--dur:${c.dur.toFixed(0)}s;--delay:-${c.delay.toFixed(0)}s">`;
      for (const b of c.parts) {
        s += `<span style="left:${((b.x - b.rx) * 100).toFixed(1)}%;top:${((b.y - b.ry) * 100).toFixed(1)}%;width:${(b.rx * 200).toFixed(1)}%;height:${(b.ry * 200).toFixed(1)}%;--a:${b.a.toFixed(2)}"></span>`;
      }
      s += `</div>`;
    }
    s += `<div class="sky-haze"></div><div class="sky-rain">`;
    for (let i = 0; i < 90; i++) s += `<i style="left:${(r() * 104 - 2).toFixed(2)}%;top:${(r() * 100).toFixed(2)}%;--d:${(0.6 + r() * 0.5).toFixed(2)}s;--delay:-${(r() * 1.1).toFixed(2)}s;--l:${(40 + r() * 50).toFixed(0)}px"></i>`;
    return s + `</div>`;
  }

  function hourNow() {
    const q = new URLSearchParams(location.search).get("t");
    if (q === "cycle") return ((Date.now() / 1000) % 60) / 60 * 24;
    if (q && /^\d{1,2}(:\d{2})?$/.test(q)) { const [hh, mm] = q.split(":"); return +hh + (+(mm || 0)) / 60; }
    const d = new Date();
    return d.getHours() + d.getMinutes() / 60 + d.getSeconds() / 3600;
  }
  function previewWeather() {
    const w = new URLSearchParams(location.search).get("w");
    return w ? { kind: w, cloud: w === "clear" ? 0 : 0.9 } : null;
  }

  let weather = null, sceneEl = null;
  function update(h) {
    if (!sceneEl) return;
    const p = withWeather(palette(h), previewWeather() || weather);
    for (const [k, v] of Object.entries(varValues(p))) sceneEl.style.setProperty(`--sc-${k}`, v);
    const pos = celestial(h);
    const place = (id, o) => {
      const el = sceneEl.querySelector(id);
      if (!el) return;
      el.style.visibility = o ? "" : "hidden";
      if (o) { el.style.left = `${(o.x * 100).toFixed(2)}%`; el.style.top = `${(o.y * 100).toFixed(2)}%`; }
    };
    place("#sc-sun", pos.sun); place("#sc-moon", pos.moon);
    sceneEl.dataset.rain = p.rain > 0 ? "1" : "";  // 只有下雨时才跑雨丝动画
    document.documentElement.dataset.scene = p.mode;
    document.documentElement.dataset.ink = textInk(p);
    document.querySelector('meta[name="theme-color"]')?.setAttribute("content", p.skyTop);
  }

  function mount() {
    sceneEl = document.getElementById("scene");
    if (!sceneEl) return;
    sceneEl.classList.add("no-transition");  // 首次显示不做颜色过渡，避免一打开就“渐变”
    update(hourNow());                       // 先把变量算好……
    sceneEl.innerHTML = buildDOM();          // ……再插入画面
    update(hourNow());
    requestAnimationFrame(() => requestAnimationFrame(() => sceneEl.classList.remove("no-transition")));
    const cycling = new URLSearchParams(location.search).get("t") === "cycle";
    setInterval(() => update(hourNow()), cycling ? 100 : 30_000);
  }

  // app.js 拿到天气后调用
  function setWeather(w) { weather = w; update(hourNow()); }

  // ---------------- 离线检查用：用同样的数据画一张近似的 SVG ----------------
  function previewSVG(h, W, H, w) {
    const p = withWeather(palette(h), w), v = varValues(p);
    const fill = (t, a) => `fill="rgb(${t.split(" ").join(",")})" fill-opacity="${a}"`;
    let s = `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}"><defs>
      <linearGradient id="g" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${p.skyTop}"/><stop offset="0.55" stop-color="${p.skyMid}"/><stop offset="1" stop-color="${p.skyLow}"/></linearGradient>
      <linearGradient id="hz" x1="0" y1="0" x2="0" y2="1"><stop offset="0.55" stop-color="${p.haze}" stop-opacity="0"/><stop offset="1" stop-color="${p.haze}" stop-opacity="${p.hazeOp}"/></linearGradient>
      <radialGradient id="sun"><stop offset="0" stop-color="#fffbe8" stop-opacity="${0.95 * (p.sunOp ?? 1)}"/><stop offset="0.12" stop-color="${p.glow}" stop-opacity="${v["glow-op"]}"/><stop offset="1" stop-color="${p.glow}" stop-opacity="0"/></radialGradient>`;
    CLOUDS.forEach((c, i) => { s += `<filter id="b${i}" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="${c.blur * 0.9}"/></filter>`; });
    s += `</defs><rect width="${W}" height="${H}" fill="url(#g)"/>`;
    const pos = celestial(h);
    if (pos.sun) s += `<circle cx="${pos.sun.x * W}" cy="${pos.sun.y * H}" r="${W * 0.16}" fill="url(#sun)"/>`;
    if (pos.moon) s += `<circle cx="${pos.moon.x * W}" cy="${pos.moon.y * H}" r="${W * 0.012}" fill="#f2efff" opacity="0.8" filter="url(#b0)"/>`;
    CLOUDS.forEach((c, i) => {
      const op = c.extra ? +v["cloud-extra"] : +v["cloud-op"];
      if (op <= 0.01) return;
      s += `<g filter="url(#b${i})" opacity="${op}">`;
      for (const b of c.parts) {
        const cx = (c.x + b.x * c.w) * W, cy = (c.y + b.y * c.h) * H, rx = b.rx * c.w * W, ry = b.ry * c.h * H;
        s += `<ellipse cx="${cx}" cy="${cy + ry * 0.25}" rx="${rx}" ry="${ry * 0.8}" ${fill(v["cloud-shade"], b.a * 0.7)}/>`;
        s += `<ellipse cx="${cx}" cy="${cy - ry * 0.1}" rx="${rx * 0.92}" ry="${ry * 0.75}" ${fill(v.cloud, b.a)}/>`;
      }
      s += `</g>`;
    });
    return s + `<rect width="${W}" height="${H}" fill="url(#hz)"/></svg>`;
  }

  const api = { palette, withWeather, celestial, textInk, previewSVG, setWeather, CLOUDS, KEYS };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else {
    root.LycheeScene = api;
    // 在 <head> 里执行：页面画出来之前就定好明暗主题，夜里打开不会先闪一下白色
    const p0 = withWeather(palette(hourNow()), previewWeather());
    document.documentElement.dataset.scene = p0.mode;
    document.documentElement.dataset.ink = textInk(p0);
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount); else mount();
  }
})(typeof window !== "undefined" ? window : globalThis);
