/* 《信息流知道你》手机端（第二部分）：结果页——"算法之眼"。
   所有结论文案由服务端生成；这里只负责呈现证据链与结果页节奏。
   结果页使用自身的滚动容器，避免 body 与前一页的信息流共享滚动位置。 */

const AXIS_POLES = { price: ["价格敏感", "品质优先"], depth: ["深度阅读", "快速浏览"], novelty: ["乐于尝鲜", "偏好稳妥"],
  expertise: ["专业向", "大众向"], decision: ["决策果断", "反复对比"] };
const AXIS_CN = { price: "价格取向", depth: "阅读深度", novelty: "尝鲜程度", expertise: "专业程度", decision: "决策方式" };
/* 服务端随结果下发的 taxonomy 轴标签（p.axes）；硬编码表只作离线兜底。 */
let AXES_PAYLOAD = null;
const axisLabels = axis => {
  const a = AXES_PAYLOAD && !Array.isArray(AXES_PAYLOAD) ? AXES_PAYLOAD[axis] : null;
  if (a && typeof a === "object") {
    const pro = String(a.pro == null ? "" : a.pro).trim();
    const con = String(a.con == null ? "" : a.con).trim();
    if (pro && con) return [pro, con];
  }
  return AXIS_POLES[axis] || ["", ""];
};
const NEVER = [["姓名", "id-card"], ["手机号", "smartphone"], ["学号", "graduation-cap"], ["位置", "map-pin"], ["任何一个字", "keyboard"]];
const MIS_ICON = ["timer", "mouse-pointer-click", "users"];
const ACT_ICON = ["sliders-horizontal", "trash-2", "shield"];
const easeOut = t => 1 - Math.pow(1 - t, 4);
const finiteNumber = (value, fallback = 0) => Number.isFinite(Number(value)) ? Number(value) : fallback;
const clamp01 = (value, fallback = 0) => Math.max(0, Math.min(1, finiteNumber(value, fallback)));
const wholeNumber = value => Math.max(0, Math.round(finiteNumber(value, 0)));

function normalizeMetricMap(raw, trait = false) {
  const out = {};
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return out;
  Object.entries(raw).forEach(([key, value]) => {
    if (!value || typeof value !== "object" || Array.isArray(value)) return;
    if (trait) {
      const pole = value.pole ? String(value.pole) : null;
      const poles = AXIS_POLES[key] || ["", ""];
      out[key] = Object.assign({}, value, {
        value: clamp01(value.value, .5),
        conf: Math.max(0, finiteNumber(value.conf, 0)),
        n: wholeNumber(value.n),
        pole,
        pole_cn: String(value.pole_cn == null || value.pole_cn === "" ? (pole === "pro" ? poles[0] : pole === "con" ? poles[1] : "") : value.pole_cn),
        axis_cn: String(value.axis_cn == null ? (AXIS_CN[key] || key) : value.axis_cn),
        cross_domain: !!value.cross_domain,
      });
    } else {
      out[key] = Object.assign({}, value, {
        score: clamp01(value.score),
        conf: Math.max(0, finiteNumber(value.conf, 0)),
        n_evidence: wholeNumber(value.n_evidence),
        cn: String(value.cn == null ? key : value.cn),
      });
    }
  });
  return out;
}
function normalizeEvidenceMap(raw) {
  const out = {};
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return out;
  Object.entries(raw).forEach(([key, value]) => {
    out[key] = Array.isArray(value) ? value.filter(x => x && typeof x === "object" && !Array.isArray(x)).map(x => Object.assign({}, x, {
      title: String(x.title == null ? "未命名内容" : x.title),
      domain_cn: String(x.domain_cn == null ? "" : x.domain_cn),
      action: String(x.action == null ? "产生了一次行为" : x.action),
      theme: String(x.theme == null ? "" : x.theme),
      contribution: clamp01(x.contribution),
    })) : [];
  });
  return out;
}

/* 结果页导航：四段锚点始终吸顶，进度条与当前段落同步。 */
let resultScrollHandler = null;
function teardownResultNavigation() {
  const result = document.getElementById("result");
  if (result && resultScrollHandler) result.removeEventListener("scroll", resultScrollHandler);
  resultScrollHandler = null;
  if (result) result.querySelectorAll("[data-result-anchor]").forEach(a => {
    a.onclick = null; a.removeAttribute("aria-controls");
  });
}
function setupResultNavigation() {
  const result = document.getElementById("result"), body = document.getElementById("resultBody");
  if (!result || !body) return;
  teardownResultNavigation();
  const blocks = [...body.querySelectorAll(".blk")];
  const hero = body.querySelector(".hero");
  const textOf = el => (el && (el.querySelector("h2") || el).textContent || "").replace(/\s+/g, "");
  const pick = tests => blocks.find(b => tests.some(re => re.test(textOf(b))));
  const explicit = key => body.querySelector('[data-result-section="' + key + '"]');
  const used = new Set();
  /* 稀疏结果（例如只有一张曝光、没有兴趣域）也要让四个锚点各自
     指向合理的下一段；只有页面真的没有足够段落时才允许复用同一块。 */
  const choose = (candidates, fallback) => {
    for (const el of candidates) if (el && !used.has(el)) { used.add(el); return el; }
    const spare = blocks.find(el => !used.has(el));
    if (spare) { used.add(spare); return spare; }
    return fallback || null;
  };
  const sections = {
    profile: choose([explicit("profile"), hero, blocks[0]], blocks[0]),
    evidence: choose([explicit("evidence"), body.querySelector("#blkOrbit"), body.querySelector("#blkSpark"),
      pick([/兴趣星图/, /形成过程/, /兴趣领域/, /行为特质/])], hero || blocks[0]),
    reversal: choose([explicit("reversal"), pick([/它从没问过你/, /为什么可能看错/])], blocks[blocks.length - 1] || hero),
    action: choose([explicit("action"), pick([/你有权拒绝/])], blocks[blocks.length - 1] || hero),
  };
  Object.entries(sections).forEach(([key, el]) => {
    if (!el) return;
    /* 不覆盖已有的语义标记。若极端稀疏数据不得不复用 hero，
       它仍应保持 profile，而不是被最后一个锚点改名。 */
    if (!el.dataset.resultSection) el.dataset.resultSection = key;
    if (!el.id) el.id = "resultSection-" + key;
    el.style.scrollMarginTop = "78px";
  });
  const anchors = [...result.querySelectorAll("[data-result-anchor]")];
  const fill = document.getElementById("resultProgressFill");
  const mark = key => anchors.forEach(a => {
    const on = a.dataset.resultAnchor === key;
    a.classList.toggle("on", on);
    if (on) a.setAttribute("aria-current", "location"); else a.removeAttribute("aria-current");
  });
  anchors.forEach(a => {
    const target = sections[a.dataset.resultAnchor];
    if (target) a.setAttribute("aria-controls", target.id);
    a.onclick = () => {
      const target = sections[a.dataset.resultAnchor];
      if (!target) return;
      /* 结果页现在是自己的滚动容器，显式滚它，避免浏览器把 body 当成目标。 */
      const rr = result.getBoundingClientRect(), tr = target.getBoundingClientRect();
      const top = Math.max(0, result.scrollTop + tr.top - rr.top - 78);
      result.scrollTo({ top, behavior: REDUCED ? "auto" : "smooth" });
    };
  });
  resultScrollHandler = () => {
    const max = Math.max(1, result.scrollHeight - result.clientHeight);
    if (fill) fill.style.width = Math.round(Math.max(0, Math.min(1, result.scrollTop / max)) * 100) + "%";
    let current = "profile", best = -Infinity;
    Object.entries(sections).forEach(([key, el]) => {
      if (!el) return;
      const top = el.getBoundingClientRect().top;
      if (top <= 118 && top > best) { best = top; current = key; }
    });
    mark(current);
  };
  result.addEventListener("scroll", resultScrollHandler, { passive: true });
  resultScrollHandler();
}

/* 滚动到视口才开始动：进度条、轴标记、划线、画布 */
const REV = new IntersectionObserver(es => es.forEach(e => {
  if (!e.isIntersecting) return;
  e.target.classList.add("in"); e.target.dispatchEvent(new CustomEvent("reveal")); REV.unobserve(e.target);
}), { threshold: .15 });

function animate(ms, fn) {
  if (typeof fn !== "function") return;
  if (REDUCED) { fn(1); return; }
  const t0 = performance.now();
  const step = now => {
    const t = Math.min(1, (now - t0) / Math.max(1, ms));
    fn(easeOut(t));
    if (t < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function canvasMetrics(cv) {
  if (!cv || !cv.isConnected) return null;
  const W = Math.floor(cv.clientWidth), H = Math.floor(cv.clientHeight);
  if (W < 2 || H < 2) return null;
  const rawDpr = Number(window.devicePixelRatio || 1);
  const dpr = Math.max(1, Math.min(3, Number.isFinite(rawDpr) ? rawDpr : 1));
  cv.width = Math.max(2, Math.round(W * dpr));
  cv.height = Math.max(2, Math.round(H * dpr));
  let g;
  try { g = cv.getContext("2d"); } catch (_) { g = null; }
  if (!g) return null;
  try {
    if (typeof g.setTransform === "function") g.setTransform(dpr, 0, 0, dpr, 0, 0);
    else g.scale(dpr, dpr);
  } catch (_) { return null; }
  return { g, W, H, dpr };
}

/* ─────────────────────────── 兴趣星图：8 个域固定方位，半径 = 分数，虚线 = 证据不足 ─────────────────────────── */
function drawOrbit(cv, order, dmap, t) {
  const m = canvasMetrics(cv); if (!m) return;
  const { g, W, H } = m;
  order = (Array.isArray(order) ? order : []).filter(d => d && typeof d === "object").map(d => ({
    domain: String(d.domain == null ? "" : d.domain), domain_cn: String(d.domain_cn == null ? "未命名" : d.domain_cn)
  })).filter(d => d.domain || d.domain_cn);
  dmap = dmap && typeof dmap === "object" ? dmap : {};
  const cx = W / 2, cy = H / 2 + 2, n = order.length;
  if (n < 3) {
    g.fillStyle = PAL.mute; g.font = "600 14px " + getComputedStyle(document.body).getPropertyValue("--sans");
    g.textAlign = "center"; g.textBaseline = "middle"; g.fillText("证据不足，暂不形成星图", cx, cy);
    return;
  }
  const R = Math.max(18, Math.min(W, H) / 2 - 44);
  t = clamp01(t, 1);
  const ang = i => -Math.PI / 2 + i * 2 * Math.PI / n;
  for (let r = 1; r <= 4; r++) {
    g.beginPath(); g.arc(cx, cy, R * r / 4, 0, 7);
    g.setLineDash(r === 4 ? [] : [2, 5]); g.strokeStyle = r === 4 ? "rgba(22,35,58,.16)" : "rgba(22,35,58,.09)"; g.lineWidth = 1; g.stroke();
  }
  g.setLineDash([]);
  for (let i = 0; i < n; i++) { const a = ang(i); g.beginPath(); g.moveTo(cx, cy); g.lineTo(cx + Math.cos(a) * R, cy + Math.sin(a) * R); g.strokeStyle = "rgba(22,35,58,.08)"; g.stroke(); }
  const pts = order.map((d, i) => { const v = dmap[d.domain]; const s = (v ? v.score : 0) * t; const a = ang(i);
    return { x: cx + Math.cos(a) * R * s, y: cy + Math.sin(a) * R * s, s, v, a }; });
  g.beginPath(); pts.forEach((p, i) => i ? g.lineTo(p.x, p.y) : g.moveTo(p.x, p.y)); g.closePath();
  g.fillStyle = "rgba(" + PAL.acRGB + ",.14)"; g.fill(); g.strokeStyle = PAL.ac; g.lineWidth = 1.6; g.stroke();
  pts.forEach(p => {
    if (!p.v) return;
    const weak = p.v.conf < 1;
    g.beginPath(); g.arc(p.x, p.y, 3 + 7 * p.s, 0, 7); g.fillStyle = weak ? "rgba(" + PAL.goldRGB + ",.9)" : PAL.ac; g.fill();
    if (weak) { g.beginPath(); g.arc(p.x, p.y, 8 + 7 * p.s, 0, 7); g.setLineDash([2, 3]); g.strokeStyle = "rgba(" + PAL.goldRGB + ",.7)"; g.lineWidth = 1; g.stroke(); g.setLineDash([]); }
  });
  order.forEach((d, i) => {
    const a = ang(i), lx = cx + Math.cos(a) * (R + 22), ly = cy + Math.sin(a) * (R + 18), v = dmap[d.domain];
    g.textAlign = Math.abs(Math.cos(a)) < .3 ? "center" : (Math.cos(a) > 0 ? "left" : "right"); g.textBaseline = "middle";
    g.font = "600 12px " + getComputedStyle(document.body).getPropertyValue("--sans"); g.fillStyle = v ? PAL.fg2 : PAL.mute;
    g.fillText(d.domain_cn, lx, ly - (v ? 7 : 0));
    if (v) { g.font = "700 12px " + getComputedStyle(document.body).getPropertyValue("--mono"); g.fillStyle = v.conf < 1 ? PAL.gold : PAL.ac; g.fillText(pct(v.score * t), lx, ly + 8); }
  });
}

/* ─────────────────────────── 形成过程：首要兴趣的把握程度随时间的曲线，描线动画 ─────────────────────────── */
function drawSpark(cv, hist, t) {
  const m = canvasMetrics(cv); if (!m) return;
  const { g, W, H } = m;
  let pts = (Array.isArray(hist) ? hist : []).filter(p => p && Number.isFinite(Number(p.t)) && Number.isFinite(Number(p.s)))
    .map(p => ({ t: Math.max(0, Number(p.t)), s: clamp01(p.s) })).sort((a, b) => a.t - b.t);
  if (!pts.length) pts = [{ t: 0, s: 0 }];
  const pad = { l: Math.min(34, Math.max(24, W * .14)), r: 10, t: 12, b: 20 },
    w = Math.max(1, W - pad.l - pad.r), h = Math.max(1, H - pad.t - pad.b);
  t = clamp01(t, 1);
  const tmax = Math.max(1, pts[pts.length - 1].t);
  const mono = getComputedStyle(document.body).getPropertyValue("--mono");
  g.font = "10px " + mono; g.fillStyle = PAL.mute;
  [0, .5, 1].forEach(v => { const y = pad.t + h * (1 - v); g.strokeStyle = "rgba(22,35,58,.09)"; g.setLineDash([2, 4]); g.beginPath(); g.moveTo(pad.l, y); g.lineTo(W - pad.r, y); g.stroke(); g.setLineDash([]); g.textAlign = "right"; g.fillText(Math.round(v * 100), pad.l - 7, y + 3); });
  g.textAlign = "left"; g.fillText("0s", pad.l, H - 5); g.textAlign = "right"; g.fillText(Math.round(tmax) + "s", W - pad.r, H - 5);
  const xy = p => [pad.l + w * (p.t / tmax), pad.t + h * (1 - p.s)];
  const upto = Math.max(1, Math.round(pts.length * t));
  const vis = pts.slice(0, upto);
  g.beginPath(); vis.forEach((p, i) => { const [x, y] = xy(p); i ? g.lineTo(x, y) : g.moveTo(x, y); });
  const L = xy(vis[vis.length - 1]); g.lineTo(L[0], pad.t + h); g.lineTo(pad.l, pad.t + h); g.closePath();
  const gr = g.createLinearGradient(0, pad.t, 0, pad.t + h); gr.addColorStop(0, "rgba(" + PAL.acRGB + ",.2)"); gr.addColorStop(1, "rgba(" + PAL.acRGB + ",0)");
  g.fillStyle = gr; g.fill();
  g.beginPath(); vis.forEach((p, i) => { const [x, y] = xy(p); i ? g.lineTo(x, y) : g.moveTo(x, y); });
  g.strokeStyle = PAL.ac; g.lineWidth = 1.8; g.lineJoin = "round"; g.stroke();
  g.beginPath(); g.arc(L[0], L[1], 3.5, 0, 7); g.fillStyle = "#16233a"; g.fill();
}

/* ─────────────────────────── 结果页拼装 ─────────────────────────── */
function evRow(e, extra) {
  e = e || {};
  return '<div class="evrow"><div class="th" style="--h:' + hue(e.theme) + '"><span>' + esc((e.domain_cn || "·")[0]) + "</span></div>" +
    '<div class="tx"><b>' + esc(e.title) + "</b><span>" + (e.domain_cn ? esc(e.domain_cn) + " · " : "") + esc(e.action) + "</span></div>" +
    (extra !== undefined ? '<div class="pc">' + extra + "</div>" : "") + "</div>";
}
function dimBlock(label, v, ev, tagText, isTop) {
  v = v || {}; ev = Array.isArray(ev) ? ev.filter(Boolean) : [];
  const score = Number.isFinite(Number(v.score)) ? Math.max(0, Math.min(1, Number(v.score))) : 0;
  const conf = Number.isFinite(Number(v.conf)) ? Number(v.conf) : 0;
  const nEvidence = Number.isFinite(Number(v.n_evidence)) ? Number(v.n_evidence) : 0;
  return '<div class="dim' + (isTop ? " top" : "") + '" data-toggle role="button" tabindex="0" aria-expanded="false">' +
    '<div class="h"><b>' + esc(label || "未命名领域") + "</b>" + (tagText ? "<u>" + esc(tagText) + "</u>" : "") + "<i>" + pct(score) + "</i></div>" +
    '<div class="bar' + (conf < 1 ? " dash" : "") + '"><u style="width:' + pct(score) + '"></u></div>' +
    '<div class="n">基于 ' + nEvidence + " 条内容" + (conf < 1 ? " · 证据不足" : "") +
    '<span class="go">证据' + I("chevron-down", 14) + "</span></div>" +
    '<div class="ev">' + ev.map(e => evRow(e, pct(e.contribution))).join("") + "</div></div>";
}
function axisBlock(axis, v, ev, isTop) {
  v = v || {}; ev = Array.isArray(ev) ? ev.filter(Boolean) : [];
  const [pro, con] = axisLabels(axis);
  const value = Number.isFinite(Number(v.value)) ? Math.max(0, Math.min(1, Number(v.value))) : .5;
  const decided = !!v.pole, strength = Math.abs(value - .5) * 2;
  const weak = Number(v.conf) < 1;
  const n = Number.isFinite(Number(v.n)) ? Number(v.n) : 0;
  return '<div class="axis' + (isTop ? " top" : "") + (decided ? "" : " none") + '" ' + (decided ? 'data-toggle role="button" tabindex="0" aria-expanded="false"' : "") + ">" +
    '<div class="h"><b>' + (decided ? esc(v.pole_cn) : "未形成判断") + "</b><u>" + esc(v.axis_cn || AXIS_CN[axis] || axis) + "</u>" +
    "<i>" + (decided ? pct(strength) : "—") + "</i></div>" +
    '<div class="tr"><div class="ln"></div><div class="mid"></div><div class="mk' + (weak ? " soft" : "") + '" style="left:' + pct(value) + '"></div></div>' +
    '<div class="pl"><span class="' + (decided && value < .5 ? "w" : "") + '">' + con + '</span><span class="' + (decided && value > .5 ? "w" : "") + '">' + pro + "</span></div>" +
    '<div class="n">' + (decided ? "基于 " + n + " 条内容" + (v.cross_domain ? " · 来自 3 个以上板块" : "") + (weak ? " · 证据不足" : "") : "证据不足，不展示判断 · " + n + " 条内容") +
    (decided ? '<span class="go">证据' + I("chevron-down", 14) + "</span>" : "") + "</div>" +
    (decided ? '<div class="ev">' + ev.map(e => evRow(e)).join("") + "</div>" : "") + "</div>";
}

function renderResult(p) {
  p = p && typeof p === "object" && !Array.isArray(p) ? Object.assign({}, p) : {};
  p.codename = String(p.codename == null ? "临时编号" : p.codename);
  p.persona = String(p.persona == null ? "" : p.persona).trim() || "证据还不够形成稳定侧写";
  p.summary = String(p.summary == null ? "" : p.summary).trim() || "本次行为样本较少，下面只展示已经能够解释的部分。";
  p.click_count = wholeNumber(p.click_count);
  p.impression_count = wholeNumber(p.impression_count);
  p.event_count = wholeNumber(p.event_count);
  AXES_PAYLOAD = p.axes && typeof p.axes === "object" && !Array.isArray(p.axes) ? p.axes : null;
  p.domains = normalizeMetricMap(p.domains);
  p.sub_tags = normalizeMetricMap(p.sub_tags);
  p.traits = normalizeMetricMap(p.traits, true);
  p.domain_evidence = normalizeEvidenceMap(p.domain_evidence);
  p.subtag_evidence = normalizeEvidenceMap(p.subtag_evidence);
  p.trait_evidence = normalizeEvidenceMap(p.trait_evidence);
  p.history = Array.isArray(p.history) ? p.history
    .filter(x => x && Number.isFinite(Number(x.t)) && Number.isFinite(Number(x.s)))
    .map(x => ({ t: Math.max(0, Number(x.t)), s: clamp01(x.s) }))
    .sort((a, b) => a.t - b.t) : [];
  p.ads = Array.isArray(p.ads) ? p.ads.filter(x => x && typeof x === "object").map(x => Object.assign({}, x, {
    title: String(x.title == null ? "" : x.title),
    body: String(x.body == null ? "" : x.body),
    reasons: Array.isArray(x.reasons) ? x.reasons.map(String) : []
  })) : [];
  const privacy = p.privacy && typeof p.privacy === "object" && !Array.isArray(p.privacy) ? p.privacy : {};
  p.privacy = Object.assign({}, privacy, {
    collected: Array.isArray(privacy.collected) && privacy.collected.length
      ? privacy.collected.map(String) : ["本次体验里的点击、停留和划过"],
    retention: "本次体验数据 2 小时后自动删除，也可以随时手动删除",
    misread: Array.isArray(privacy.misread) ? privacy.misread.filter(x => x && typeof x === "object").map(x => ({
      title: String(x.title == null ? "可能存在偏差" : x.title), desc: String(x.desc == null ? "" : x.desc)
    })) : [],
    actions: Array.isArray(privacy.actions) ? privacy.actions.filter(x => x && typeof x === "object").map((x, i) => ({
      n: x.n == null ? i + 1 : x.n,
      title: String(x.title == null ? "可采取的操作" : x.title),
      desc: String(x.desc == null ? "" : x.desc)
    })) : [],
  });
  if (!p.privacy.misread.length) p.privacy.misread = [
    { title: "数据稀疏", desc: "这份侧写只依据本次短暂浏览，样本很少。" },
    { title: "行为不等于意图", desc: "点开或停留可能只是好奇，并不等于长期偏好。" },
    { title: "没有上下文", desc: "系统不知道你当时的目的、心情与现实场景。" },
  ];
  if (!p.privacy.actions.length) p.privacy.actions = [
    { n: 1, title: "关闭个性化推荐", desc: "在相关服务的隐私或推荐设置中，选择不基于个人特征的内容。" },
    { n: 2, title: "行使删除权", desc: "清理浏览与搜索记录，并在相关设置中请求删除个人信息。" },
    { n: 3, title: "权限最小化", desc: "通讯录、位置、相册等权限只在确有需要时开启，用完即关。" },
  ];
  const bubble = p.bubble_demo && typeof p.bubble_demo === "object" && !Array.isArray(p.bubble_demo) ? p.bubble_demo : {};
  p.bubble_demo = {
    before: Array.isArray(bubble.before) ? bubble.before.filter(x => x && typeof x === "object").map(x => ({
      domain: String(x.domain == null ? "" : x.domain), domain_cn: String(x.domain_cn == null ? "" : x.domain_cn)
    })) : [],
    after: Array.isArray(bubble.after) ? bubble.after.filter(x => x && typeof x === "object").map(x => ({
      title: String(x.title == null ? "未命名内容" : x.title), domain_cn: String(x.domain_cn == null ? "" : x.domain_cn)
    })) : [],
  };
  if (p.cross_domain && typeof p.cross_domain === "object" && !Array.isArray(p.cross_domain)) {
    p.cross_domain = Object.assign({}, p.cross_domain, {
      pole_cn: String(p.cross_domain.pole_cn == null ? "共同倾向" : p.cross_domain.pole_cn),
      evidence: Array.isArray(p.cross_domain.evidence) ? p.cross_domain.evidence
        .filter(x => x && typeof x === "object").map(x => ({
          domain_cn: String(x.domain_cn == null ? "行为证据" : x.domain_cn),
          title: String(x.title == null ? "未命名内容" : x.title),
          action: String(x.action == null ? "产生了一次行为" : x.action),
        })) : [],
    });
  } else p.cross_domain = null;
  p.feedback = p.feedback === "accurate" || p.feedback === "inaccurate" ? p.feedback : null;
  const result = document.getElementById("result");
  if (result) result.setAttribute("aria-labelledby", "resultPageTitle");
  const resultNav = document.querySelector("#result .result-anchors");
  const resultProgress = document.querySelector("#result .result-progress");
  if (resultNav) { resultNav.hidden = false; resultNav.setAttribute("aria-hidden", "false"); }
  if (resultProgress) { resultProgress.hidden = false; resultProgress.setAttribute("aria-hidden", "true"); }
  const ok = !!p.converged;
  const persona = String(p.persona || "").split(" · ");
  const doms = Object.entries(p.domains).filter(([, v]) => v && typeof v === "object");
  const discoverN = Number(window.__discoverClicks || 0);
  const suppliedOrder = p.bubble_demo && p.bubble_demo.before && p.bubble_demo.before.length
    ? p.bubble_demo.before.filter(x => x.domain) : [];
  const order = suppliedOrder.length ? suppliedOrder : doms.map(([k, v]) => ({ domain: k, domain_cn: v.cn }));
  const topEvidence = doms.length && Array.isArray(p.domain_evidence[doms[0][0]]) ? p.domain_evidence[doms[0][0]] : [];
  const topHue = doms.length ? hue((topEvidence[0] || {}).theme) : 220;
  let no = 0; const num = () => (++no < 10 ? "0" : "") + no;
  let h = "";

  /* 主视觉 */
  h += '<header class="blk hero" data-reveal id="resultPageTitle" data-result-section="profile">' +
    '<div class="top"><span>本次侧写 · ' + esc(p.codename) + '</span><span class="st' + (ok ? " ok" : "") + '"><i></i>' + (ok ? "证据较充分" : "证据仍有限") + "</span></div>" +
    '<div class="cn">本次浏览显示</div>' +
    '<h1 class="big">' + (persona.length > 1
      ? '<span class="l1">' + esc(persona[0]) + '</span><span class="l2">' + esc(persona.slice(1).join(" · ")) + "</span>"
      : '<span class="l1 only">' + esc(persona[0]) + "</span>") + "</h1>" +
    '<p class="sum">' + esc(p.summary) + "</p>" +
    '<span class="tag">' + I("scan-line", 13) + "基于本次浏览的判断</span>" +
    '<div class="kpi"><div><b>' + p.click_count + "</b><span>" + I("mouse-pointer-click", 11) + "次点击</span></div>" +
    "<div><b>" + p.impression_count + "</b><span>" + I("eye", 11) + "条看过</span></div>" +
    "<div><b>" + p.event_count + "</b><span>" + I("activity", 11) + "次浏览动作</span></div>" +
    '<div class="z"><b>0</b><span>' + I("keyboard", 11) + "字输入</span></div></div></header>";

  /* 准 / 不准 */
  h += '<section class="blk" data-reveal><div class="fb"><div class="q">这份侧写接近你刚才的浏览吗？</div>' +
    '<button type="button" id="fbGood" aria-label="准">' + I("thumbs-up", 20) + '</button><button type="button" id="fbBad" aria-label="不准">' + I("thumbs-down", 20) + "</button></div>" +
    '<div class="note" id="fbMsg" style="margin-top:8px;padding-left:4px"></div></section>';

  if (!doms.length) {
    h += '<section class="blk" data-reveal data-result-section="evidence"><div class="bh"><span class="no">' + num() + '</span><h2>证据还不够</h2></div>' +
      '<div class="pnl"><p class="para">本次只看到你浏览了 <b>' + p.impression_count + ' 条内容、点开 ' + p.click_count +
      ' 次</b>，还不足以形成稳定的兴趣领域。证据不足的部分不会展示判断。</p></div></section>';
  }

  /* 星图 */
  if (doms.length) {
    h += '<section class="blk" data-reveal id="blkOrbit"><div class="bh"><span class="no">' + num() + '</span><h2>兴趣星图</h2><span class="hint">8 个板块 · 半径 = 分数</span></div>' +
      '<div class="pnl"><canvas class="orbit" id="cvOrbit"></canvas>' +
      '<div class="legend"><span><i></i>证据充分</span><span><i class="d"></i>证据不足</span></div></div></section>';
  }
  /* 形成过程 */
  if (p.history && p.history.length > 2) {
    h += '<section class="blk" data-reveal id="blkSpark"><div class="bh"><span class="no">' + num() + '</span><h2>判断如何变化</h2><span class="hint">' + Math.round(p.history[p.history.length - 1].t) + 's</span></div>' +
      '<div class="pnl"><canvas class="spark" id="cvSpark"></canvas>' +
      '<div class="note">横轴是浏览时间，纵轴是它对首要兴趣的把握程度，最终为 <b>' + pct(p.history[p.history.length - 1].s) + "</b>。</div></div></section>";
  }
  /* 跨域推断 */
  if (p.cross_domain && Array.isArray(p.cross_domain.evidence) && p.cross_domain.evidence.length) {
    const c = p.cross_domain;
    c.evidence = c.evidence.filter(Boolean);
    h += '<section class="blk" data-reveal><div class="bh"><span class="no">' + num() + '</span><h2>它没问过你，<br>但它注意到了</h2></div><div class="cross">' +
      c.evidence.map(e => '<div class="ev3"><div class="d">' + esc(e.domain_cn || "行为证据") + '</div><div class="t">' + esc(e.title) + '</div><div class="a">' + I("mouse-pointer-click", 12) + esc(e.action) + "</div></div>").join("") +
      '<div class="conv"><div class="lead">' + I("git-merge", 14) + esc(c.evidence.length) + ' 个不相关的板块，指向同一件事</div>' +
      '<div class="word">' + esc(c.pole_cn) + "</div>" +
      '<div class="sub">这是本次浏览里最值得注意的一条推断。它不来自任何一次单独的点击，而来自几次点击之间的共同点，也可能存在偏差。</div></div></div></section>';
  }
  /* 信息茧房 */
  if (p.bubble_demo && p.bubble_demo.after && p.bubble_demo.after.length) {
    const b = p.bubble_demo;
    h += '<section class="blk" data-reveal><div class="bh"><span class="no">' + num() + '</span><h2>推荐如何收窄</h2><span class="hint">' + I("filter", 12) + " 未来预演</span></div>" +
      '<div class="bub"><div class="ph"><h4>刚开始</h4>' +
      b.before.map(x => '<div class="row" style="--h:' + hue(themeOf(x.domain)) + '"><i></i><span>' + esc(x.domain_cn) + "</span></div>").join("") + "</div>" +
      '<div class="ph after" style="--h:' + topHue + '"><h4>按当前侧写继续推荐后</h4>' +
      b.after.slice(0, 8).map(x => '<div class="row" style="--h:' + topHue + '"><i></i><span>' + esc(x.title) + "</span></div>").join("") + "</div></div>" +
      '<p class="bubq">这是更懂你，<br>还是<em>限制了你</em>？</p>' +
      '<div class="note">左边是刚开始时均衡出现的八个板块；右边是按本次侧写继续推荐后的样子——更贴近，也更少出现别的板块。</div></section>';
  }
  /* 兴趣领域 */
  if (doms.length) {
    const tags = Object.entries(p.sub_tags).filter(([, v]) => v && Number(v.n_evidence) >= 2).slice(0, 3);
    h += '<section class="blk" data-reveal><div class="bh"><span class="no">' + num() + '</span><h2>兴趣领域</h2><span class="hint">点开看证据</span></div><div class="pnl">' +
      doms.slice(0, 3).map(([k, v], i) => dimBlock(v.cn, v, p.domain_evidence[k] || [], null, i === 0)).join("") + "</div>" +
      (tags.length ? '<div class="pnl">' + tags.map(([k, v]) => dimBlock(v.cn, v, p.subtag_evidence[k] || [], "子标签")).join("") + "</div>" : "") + "</section>";
  }
  /* 行为特质 */
  const traits = Object.entries(p.traits).filter(([, v]) => v && typeof v === "object");
  if (traits.length) {
    const decided = traits.filter(([k, v]) => v.pole).sort((a, b) => Math.abs(b[1].value - .5) - Math.abs(a[1].value - .5));
    const rest = traits.filter(([k, v]) => !v.pole);
    h += '<section class="blk" data-reveal><div class="bh"><span class="no">' + num() + '</span><h2>行为特质</h2><span class="hint">五条轴 · 跨板块推断</span></div><div class="pnl">' +
      decided.map(([k, v], i) => axisBlock(k, v, (p.trait_evidence || {})[k] || [], i === 0)).join("") +
      rest.map(([k, v]) => axisBlock(k, v, [], false)).join("") + "</div>" +
      '<div class="note">每条轴都是本次浏览里可观察的行为倾向，不是人格测评。中间区间和证据不足的轴，它<b>不下判断</b>——宁可少说一条。</div></section>';
  }
  /* 广告 */
  if (p.ads && p.ads.length && p.ads[0].reasons && p.ads[0].reasons.length) {
    const a = p.ads[0];
    h += '<section class="blk" data-reveal><div class="bh"><span class="no">' + num() + '</span><h2>于是，你会看到这条广告</h2></div><div class="adcard">' +
      '<div class="tp"><span class="bd">模拟广告</span><span class="mono">基于上面的侧写生成</span></div>' +
      "<h3>" + esc(a.title) + '</h3><p class="bd2">' + esc(a.body) + "</p>" +
      '<div class="why"><div class="t">为什么给你看这条</div>' + a.reasons.map(r => "<p>" + I("circle-check", 15) + "<span>" + esc(r) + "</span></p>").join("") +
      '<div class="kick">这里列出推荐理由，方便你核对这次推断。</div></div></div></section>';
  }
  /* 反转 */
  const pv = p.privacy || {};
  h += '<section class="blk" data-reveal data-result-section="reversal"><div class="bh"><span class="no">' + num() + '</span><h2>它从没问过你</h2></div>' +
    '<ul class="never">' + NEVER.map(n => '<li><span class="ic">' + I(n[1], 17) + '</span><span class="w">' + n[0] + "</span><small>未曾询问</small></li>").join("") + "</ul>" +
    '<p class="para" style="margin-top:18px">整个过程你没有输入一个字，没有注册、没有登录、没有授权任何权限。本次体验只会用到：<b>' + esc((pv.collected || []).join("、")) + "</b>。" +
    '<br><b>删除方式：</b>' + esc(pv.retention) +
    "<br>这份结果只根据本次体验中的行为生成，不代表完整的个人偏好。</p>" +
    '<div class="bh" style="margin-top:26px"><span class="no">' + num() + '</span><h2>它为什么可能看错</h2></div>' +
    '<div class="mis">' + (pv.misread || []).map((m, i) => "<div>" + I(MIS_ICON[i] || "info", 17) + "<span><b>" + esc(m.title) + "</b><p>" + esc(m.desc) + "</p></span></div>").join("") + "</div>" +
    '<p class="state">数据画像不是你本人，<br>只是<em>算法眼中</em>的你。</p>' +
    '<div class="contrast">你可以点「不准」纠正这次结果，也可以一键删除本次体验的数据。</div>' +
    '<div class="contrast discover-note">' + (discoverN
      ? '你在「发现」里点的 <b>' + discoverN + ' 条</b>，一样进了侧写。'
      : '你没有在「发现」里点开内容；如果点开，一样会进侧写。') +
      '关闭个性化推荐，关掉的是<b>推荐</b>，不是<b>记录</b>。</div></section>';
  /* 行动建议 */
  h += '<section class="blk" data-reveal data-result-section="action"><div class="bh"><span class="no">' + num() + '</span><h2>你有权拒绝</h2><span class="hint">手机上现在就能做</span></div><div class="acts">' +
    (pv.actions || []).map((a, i) => '<div class="act"><span class="no">' + esc(String(a.n == null ? i + 1 : a.n).padStart(2, "0")) + '</span><span class="ic">' + I(ACT_ICON[i] || "shield", 18) + "</span><div><b>" + esc(a.title) + "</b><p>" + esc(a.desc) + "</p></div></div>").join("") + "</div></section>";
  /* 按钮 */
  h += '<section class="blk" data-reveal><div class="btns"><button type="button" class="rbtn danger" id="del">' + I("trash-2", 18) + '立即删除我的数据</button>' +
    '<button type="button" class="rbtn" id="share">' + I("download", 18) + "保存分享图</button></div>" +
    '</section>';

  const body = document.getElementById("resultBody");
  /* 重新生成结果前清掉旧节点的观察关系，避免重试生成时保留整棵旧 DOM。 */
  REV.disconnect();
  body.innerHTML = h;
  resultVisual = { p, order };
  setupResultNavigation();
  body.querySelectorAll("[data-toggle]").forEach(el => {
    const toggle = ev => {
      if (ev && ev.target && ev.target.closest(".ev")) return;
      const open = !el.classList.contains("open"); el.classList.toggle("open", open); el.setAttribute("aria-expanded", String(open));
    };
    el.addEventListener("click", toggle);
    el.addEventListener("keydown", ev => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); toggle(ev); } });
  });
  body.querySelectorAll(".blk[data-reveal]").forEach(el => REV.observe(el));
  const bo = document.getElementById("blkOrbit");
  if (bo) bo.addEventListener("reveal", () => {
    const cv = document.getElementById("cvOrbit");
    if (cv) animate(1200, t => { if (cv.isConnected) drawOrbit(cv, order, p.domains, t); });
  });
  const bs = document.getElementById("blkSpark");
  if (bs) bs.addEventListener("reveal", () => {
    const cv = document.getElementById("cvSpark");
    if (cv) animate(1400, t => { if (cv.isConnected) drawSpark(cv, p.history, t); });
  });
  /* 删除不可逆且整页碎裂，误触代价高：两段式确认（4 秒内再按一次），
     不引入系统弹窗打断结果页的叙事，也不削弱“行使删除权”的动作感。 */
  const delBtn = document.getElementById("del");
  const delLabel = delBtn.innerHTML;
  let delArm = 0;
  const disarmDel = () => {
    if (!delBtn.isConnected) return;
    delArm = 0;
    delBtn.classList.remove("arm");
    delBtn.innerHTML = delLabel;
  };
  delBtn.onclick = () => {
    if (delArm && Date.now() < delArm) { disarmDel(); doDelete(); return; }
    delArm = Date.now() + 4000;
    delBtn.classList.add("arm");
    delBtn.innerHTML = I("trash-2", 18) + "再按一次，确认删除";
    Lucide.mount(delBtn);
    setTimeout(() => { if (delArm && Date.now() >= delArm) disarmDel(); }, 4200);
  };
  document.getElementById("share").onclick = () => shareCard(p);
  bindFeedback(p.feedback);
  Lucide.mount(body);
}
const THEME_OF = { tech_digital: "tech", game_acg: "game", life_shopping: "life", sport_outdoor: "sport", av_ent: "av", culture_art: "culture", study_exam: "study", social_hot: "social" };
function themeOf(domain) { return THEME_OF[domain] || "tech"; }

function bindFeedback(value) {
  const good = document.getElementById("fbGood"), bad = document.getElementById("fbBad"), msg = document.getElementById("fbMsg");
  if (!good || !bad || !msg) return;
  const mark = v => { good.classList.toggle("on", v === "accurate"); bad.classList.toggle("on", v === "inaccurate"); };
  mark(value);
  const send = async v => {
    good.disabled = bad.disabled = true;
    try {
      await apiJson(API + "/api/feedback?sid=" + encodeURIComponent(SID), { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ value: v }) });
      mark(v); msg.textContent = v === "accurate" ? "已记录为「准」。" : "已记录为「不准」。";
    } catch (e) {
      if (e.code === "session_gone") handleSessionGone();
      else msg.textContent = "反馈提交失败，请再试一次。";
    }
    finally { good.disabled = bad.disabled = false; }
  };
  good.onclick = () => send("accurate"); bad.onclick = () => send("inaccurate");
}

/* ─────────────────────────── 删除：整页碎裂、消散，然后只剩一句话 ─────────────────────────── */
async function doDelete() {
  const btn = document.getElementById("del");
  if (!btn || btn.disabled || !SID) return;
  btn.disabled = true;
  let d;
  try { d = await apiJson(API + "/api/session?sid=" + encodeURIComponent(SID), { method: "DELETE" }); }
  catch (e) {
    btn.disabled = false;
    if (e.code === "session_gone") handleSessionGone();
    else toast("删除失败，请再试一次");
    return;
  }
  if (!d || d.deleted !== true) {
    btn.disabled = false;
    handleSessionGone();
    return;
  }
  SID = null;
  /* 服务端删除后同步清掉浏览器内存中的会话副本；结果页只保留删除确认文案，
     不让返回/重试路径继续持有卡片、通知或待发事件。 */
  Q = []; lastProf = null; cards = {}; impressed = {}; opened = {}; readSet = {};
  /* 删除不仅清服务端画像，也清掉前端用于“最近点击推荐”的短期上下文；
     否则同页重新开始时，旧内容 ID 可能被带进下一次会话的请求参数。 */
  lastClickedCid = null; lastClickedSeq = null; SEQ = 0; SCREEN = 0; screensLoaded = 0;
  discoverClickSet.clear(); discoverClicks = 0; window.__discoverClicks = 0;
  CODENAME = ""; curCid = null; curInfo = null; curOpenAt = 0; curContentReady = false;
  if (typeof updateRefreshControl === "function") updateRefreshControl();
  notis = []; unread = 0; clearInterval(pollTimer); pollTimer = 0;
  clearSessionDeadline();
  REV.disconnect();
  teardownResultNavigation();
  resultVisual = null;
  clearTimeout(resultResizeTimer); resultResizeTimer = 0;
  const blocks = [...document.querySelectorAll("#resultBody .blk")];
  if (!REDUCED) {
    const result = document.getElementById("result");
    if (result) result.scrollTo({ top: 0, behavior: "smooth" });
    FX.boom();
    blocks.forEach((b, i) => {
      b.style.transition = "transform .9s var(--ease), opacity .8s ease, filter .9s ease";
      b.style.transitionDelay = (i * 40) + "ms";
      b.style.transform = "translate(" + ((Math.random() - .5) * 70).toFixed(0) + "px," + (60 + Math.random() * 90).toFixed(0) + "px) rotate(" + ((Math.random() - .5) * 8).toFixed(1) + "deg)";
      b.style.opacity = "0"; b.style.filter = "blur(8px)";
    });
    await wait(900 + blocks.length * 40);
  }
  /* 删除后只剩「已删除」这一页，章节锚点和进度条不再指向已经不存在的内容。 */
  const resultNav = document.querySelector("#result .result-anchors");
  const resultProgress = document.querySelector("#result .result-progress");
  if (resultNav) { resultNav.hidden = true; resultNav.setAttribute("aria-hidden", "true"); }
  if (resultProgress) { resultProgress.hidden = true; resultProgress.setAttribute("aria-hidden", "true"); }
  const result = document.getElementById("result");
  if (result) result.setAttribute("aria-labelledby", "goneTitle");
  document.getElementById("resultBody").innerHTML =
    '<div class="gone"><div class="ring">' + I("check", 28) + "</div>" +
    '<h2 id="goneTitle" tabindex="-1">已删除<span>本次记录已清除。</span></h2>' +
    "<p>与这次体验对应的临时代号、浏览记录和浏览侧写已清除。</p>" +
    '<div class="box"><p>现在可以关闭这个页面。</p></div></div>';
  const goneTitle = document.getElementById("goneTitle");
  if (goneTitle) requestAnimationFrame(() => { try { goneTitle.focus({ preventScroll: true }); } catch (_) {} });
}

/* 设备旋转或桌面窗口改尺寸后，已揭示的 canvas 需要按新像素尺寸重画。 */
let resultVisual = null, resultResizeTimer = 0;
addEventListener("resize", () => {
  clearTimeout(resultResizeTimer);
  resultResizeTimer = setTimeout(() => {
    resultResizeTimer = 0;
    if (!resultVisual || !document.getElementById("result").classList.contains("on")) return;
    const orbit = document.getElementById("cvOrbit"), spark = document.getElementById("cvSpark");
    if (orbit) drawOrbit(orbit, resultVisual.order, resultVisual.p.domains, 1);
    if (spark) drawSpark(spark, resultVisual.p.history, 1);
    if (resultScrollHandler) resultScrollHandler();
    /* 旋转时首个 resize 事件可能早于布局重排；下一帧再补一次，
       避免 canvas 取到旧的 clientWidth/clientHeight 而留下空白。 */
    requestAnimationFrame(() => {
      const live = document.getElementById("result");
      if (!resultVisual || !live || !live.classList.contains("on")) return;
      const orbit2 = document.getElementById("cvOrbit"), spark2 = document.getElementById("cvSpark");
      if (orbit2) drawOrbit(orbit2, resultVisual.order, resultVisual.p.domains, 1);
      if (spark2) drawSpark(spark2, resultVisual.p.history, 1);
      if (resultScrollHandler) resultScrollHandler();
    });
  }, 120);
});

/* ─────────────────────────── 分享图：本地 canvas 生成，不含任何身份信息 ─────────────────────────── */
function shareCard(p) {
  const W = 750, H = 1334, cv = document.createElement("canvas"); cv.width = W; cv.height = H;
  const g = cv.getContext("2d");
  const sans = getComputedStyle(document.body).getPropertyValue("--sans"), mono = getComputedStyle(document.body).getPropertyValue("--mono"), serif = getComputedStyle(document.body).getPropertyValue("--serif");
  g.fillStyle = PAL.ink; g.fillRect(0, 0, W, H);
  g.strokeStyle = "rgba(22,35,58,.08)"; g.lineWidth = 1; g.beginPath(); g.arc(W * .85, 190, 380, 0, 7); g.stroke();
  g.beginPath(); g.arc(W * .85, 190, 300, 0, 7); g.setLineDash([3, 7]); g.stroke(); g.setLineDash([]);
  g.fillStyle = PAL.ac; g.fillRect(60, 78, 14, 14);
  g.fillStyle = PAL.mute; g.font = "22px " + mono; g.textBaseline = "alphabetic"; g.fillText("FEED KNOWS YOU  ·  信息流知道你", 88, 92);
  g.fillStyle = PAL.mute; g.font = "22px " + mono; g.fillText("本次侧写 · " + (p.codename || ""), 60, 150);
  g.fillStyle = PAL.ac; g.font = "24px " + mono; g.fillText("本次浏览显示", 60, 300);
  const persona = String(p.persona || "").split(" · ");
  g.fillStyle = PAL.fg; g.font = "800 74px " + sans;
  let y = wrapText(g, persona[0], 60, 392, 630, 84);
  if (persona.length > 1) { g.fillStyle = PAL.ac; g.font = "800 62px " + sans; y = wrapText(g, persona.slice(1).join(" · "), 60, y + 80, 630, 72); }
  g.fillStyle = PAL.fg2; g.font = "26px " + sans; y = wrapText(g, p.summary || "", 60, y + 66, 630, 42);
  const doms = Object.entries(p.domains || {}).slice(0, 3);
  let by = Math.max(y + 80, 820);
  doms.forEach(([k, v], i) => {
    const yy = by + i * 58;
    g.fillStyle = PAL.fg; g.font = "600 24px " + sans; g.textAlign = "left"; g.fillText(v.cn, 60, yy);
    g.fillStyle = PAL.ink4; g.fillRect(220, yy - 14, 400, 6);
    g.fillStyle = i ? PAL.fg2 : PAL.ac; g.fillRect(220, yy - 14, 400 * v.score, 6);
    g.fillStyle = PAL.fg; g.font = "700 24px " + mono; g.textAlign = "right"; g.fillText(pct(v.score), 690, yy); g.textAlign = "left";
  });
  g.fillStyle = "rgba(22,35,58,.12)"; g.fillRect(60, 1040, 630, 1);
  g.fillStyle = PAL.fg; g.font = "600 38px " + serif;
  wrapText(g, "数据画像不是你本人，只是算法眼中的你。", 60, 1112, 630, 54);
  g.fillStyle = PAL.mute; g.font = "22px " + mono;
  g.fillText(p.click_count + " 次点击 · 0 字输入 · 数据 2 小时后自动删除", 60, 1250);
  g.fillStyle = PAL.gold; g.font = "20px " + mono;
  g.fillText("燕山大学 · 2026 年国家网络安全宣传周", 60, 1292);
  const dataURL = cv.toDataURL("image/png");
  /* iOS Safari 对 data URL 不响应 download 属性（只会打开预览）。
     之前“已保存到相册”的提示在 iPhone 上是空话，分享就此中断；
     改为展示图片引导长按保存，让提示与真实行为一致。 */
  const isIOS = /iP(hone|od|ad)/.test(navigator.userAgent) ||
    (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  if (isIOS) {
    const ov = document.createElement("div");
    ov.id = "shareOverlay";
    ov.setAttribute("role", "dialog");
    ov.setAttribute("aria-modal", "true");
    ov.setAttribute("aria-label", "保存分享图");
    ov.innerHTML = '<div class="share-card"><img alt="本次浏览侧写分享图，长按保存到相册" src="' + dataURL + '">' +
      '<p>长按图片保存到相册，或截图后分享</p>' +
      '<button type="button">完成</button></div>';
    ov.addEventListener("click", e => { if (e.target === ov) ov.remove(); });
    ov.querySelector("button").onclick = () => ov.remove();
    document.body.appendChild(ov);
    return;
  }
  const a = document.createElement("a");
  a.download = "本次浏览侧写.png"; a.href = dataURL; a.click();
  toast("已保存到下载目录");
}
function wrapText(g, text, x, y, maxw, lh) {
  let line = "", yy = y;
  for (const ch of String(text || "")) {
    if (g.measureText(line + ch).width > maxw) { g.fillText(line, x, yy); line = ch; yy += lh; } else line += ch;
  }
  g.fillText(line, x, yy); return yy;
}
