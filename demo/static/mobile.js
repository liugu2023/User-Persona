/* 《信息流知道你》手机端运行时（第一部分）：背景场、告知页、信息流、阅读页、过场。
   结果页渲染在 mobile-result.js。事件语义与服务端约定保持不变（§6.2 / §6.4 / §8.8）。 */

const API = "";
const I = (n, s, o) => Lucide.icon(n, Object.assign({ size: s || 18 }, o || {}));
const esc = s => String(s == null ? "" : s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const pct = x => Math.round((x || 0) * 100) + "%";
const wait = ms => new Promise(r => setTimeout(r, ms));
const hash = s => { let h = 2166136261; for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); } return h >>> 0; };
const REDUCED = typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;

function newCreateRequestId() {
  const cryptoApi = window.crypto;
  if (cryptoApi && typeof cryptoApi.randomUUID === "function") return "create-" + cryptoApi.randomUUID();
  if (cryptoApi && typeof cryptoApi.getRandomValues === "function") {
    const bytes = new Uint8Array(16); cryptoApi.getRandomValues(bytes);
    return "create-" + [...bytes].map(x => x.toString(16).padStart(2, "0")).join("");
  }
  let randomPart = "";
  while (randomPart.length < 32) {
    randomPart += Math.random().toString(36).slice(2).padEnd(10, "0");
  }
  return "create-" + Date.now().toString(36) + "-" + randomPart.slice(0, 32);
}

/* 画布用色：与 mobile.css 的 :root 保持一致（燕大蓝 + 金），canvas 拿不到级联后的 --ac，所以在这里直接取 */
const CSSVAR = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const PAL = {
  ink: CSSVAR("--ink") || "#0a1322", paper: CSSVAR("--paper") || "#f2f4f8",
  fg: CSSVAR("--fg") || "#f3f5f9", fg2: CSSVAR("--fg-2") || "#c5cddb", mute: CSSVAR("--mute") || "#8391a8", ink4: CSSVAR("--ink-4") || "#1e304c",
  ac: CSSVAR("--ysu-2") || "#4f8be0", acRGB: "79,139,224",        // 墨世界里的强调色（亮一档的燕大蓝）
  gold: CSSVAR("--gold") || "#c9a24a", goldRGB: "201,162,74",
};

/* 域 → 色相（封面 / 证据缩略 / 标签色），颜色只作装饰，文字标签始终并存（内容库设计 §8） */
const HUE = { tech: 214, game: 268, life: 26, sport: 172, av: 342, culture: 42, study: 200, social: 96 };
const hue = t => HUE[t] == null ? 220 : HUE[t];
/* 为每个内容域提供多个虚构发布者，并按内容 ID 稳定选取。 */
const PUB = {
  tech: ["硅基观察", "拆机日报", "终端笔记"], game: ["像素周报", "二周目", "新游速递"],
  life: ["宿舍生活家", "省钱实验室", "厨房手记"], sport: ["操场日志", "装备党", "周末球局"],
  av: ["片单速递", "耳机与黑胶", "剪辑室"], culture: ["书页边角", "博物志", "译者手记"],
  study: ["自习室通讯", "错题本", "学委日报"], social: ["校园热榜", "社团观察", "通知核验站"]
};
const publisher = c => { const l = PUB[c.cover_theme] || ["编辑部"]; return l[hash(c.content_id) % l.length]; };

function toast(t) {
  const e = document.getElementById("toast"); e.textContent = t; e.classList.add("on");
  clearTimeout(e._t); e._t = setTimeout(() => e.classList.remove("on"), 2600);
}
async function fetchTimed(url, opts, timeoutMs = 12000) {
  const options = Object.assign({}, opts || {});
  const canAbort = typeof AbortController === "function" && !options.signal;
  const controller = canAbort ? new AbortController() : null;
  let timer = 0;
  if (controller) {
    options.signal = controller.signal;
    timer = setTimeout(() => controller.abort(), timeoutMs);
  }
  try { return await fetch(url, options); }
  finally { if (timer) clearTimeout(timer); }
}
async function apiJson(url, opts) {
  let r;
  try { r = await fetchTimed(url, opts); }
  catch (e) {
    if (!e || typeof e !== "object") e = new Error("network");
    try { e.code = "network"; } catch (_) {}
    throw e;
  }
  const d = await r.json().catch(() => ({}));
  if (!r.ok) {
    const e = new Error(d.error || ("http_" + r.status));
    e.code = d.error || ("http_" + r.status); e.status = r.status; throw e;
  }
  return d;
}

/* 覆盖层与整页状态共用的无障碍/焦点管理。
   打开弹层时冻结当前页面的滚动容器，关闭后恢复原位置；这样在触屏上拖动弹层
   不会把底下的信息流一起带走。弹层自己的 overflow 容器仍然可以正常滚动。 */
const MODAL_LOCK = { count: 0, view: null, viewScrollTop: 0, appAriaHidden: null };
function lockModalBackground() {
  if (MODAL_LOCK.count++ > 0) return;
  const app = document.getElementById("app");
  if (app) {
    MODAL_LOCK.appAriaHidden = app.getAttribute("aria-hidden");
    app.setAttribute("aria-hidden", "true");
    app.setAttribute("inert", "");
    try { app.inert = true; } catch (_) {}
  }
  MODAL_LOCK.view = document.querySelector("#app .app-view.on");
  if (MODAL_LOCK.view) {
    MODAL_LOCK.viewScrollTop = MODAL_LOCK.view.scrollTop;
    MODAL_LOCK.view.classList.add("scroll-locked");
  }
  document.documentElement.classList.add("modal-lock");
  document.body.classList.add("modal-lock");
}
function unlockModalBackground() {
  if (!MODAL_LOCK.count) return;
  if (--MODAL_LOCK.count > 0) return;
  const view = MODAL_LOCK.view;
  if (view) {
    view.classList.remove("scroll-locked");
    view.scrollTop = MODAL_LOCK.viewScrollTop;
  }
  const app = document.getElementById("app");
  if (app) {
    if (MODAL_LOCK.appAriaHidden == null) app.removeAttribute("aria-hidden");
    else app.setAttribute("aria-hidden", MODAL_LOCK.appAriaHidden);
    app.removeAttribute("inert");
    try { app.inert = false; } catch (_) {}
  }
  MODAL_LOCK.appAriaHidden = null;
  MODAL_LOCK.view = null;
  MODAL_LOCK.viewScrollTop = 0;
  document.documentElement.classList.remove("modal-lock");
  document.body.classList.remove("modal-lock");
}
function modalOpen(el, focusEl) {
  if (!el) return;
  /* 重新打开一个正在退场的弹层时，取消延迟解锁；否则旧的
     transitionend 可能在新弹层仍打开时把页面锁提前释放。 */
  const retainedLock = !!el._modalLocked;
  if (el._unlockTimer) { clearTimeout(el._unlockTimer); el._unlockTimer = 0; }
  if (el._unlockDone) { el.removeEventListener("transitionend", el._unlockDone); el._unlockDone = null; }
  el.classList.remove("closing");
  const wasOpen = el.getAttribute("aria-hidden") === "false";
  if (!wasOpen && retainedLock) {
    el._returnFocus = el._pendingReturnFocus || el._returnFocus || document.activeElement;
    el._pendingReturnFocus = null;
  }
  if (!wasOpen && !retainedLock) {
    el._returnFocus = document.activeElement;
    el._modalLocked = true;
    lockModalBackground();
  }
  el.setAttribute("aria-hidden", "false"); el.classList.add("on");
  const target = focusEl || el.querySelector("button,[href],input,select,textarea,[tabindex]:not([tabindex='-1'])") || el;
  requestAnimationFrame(() => { try { target.focus({ preventScroll: true }); } catch (_) {} });
}
function modalClose(el, restore = true) {
  if (!el) return;
  const wasOpen = el.getAttribute("aria-hidden") === "false";
  const returnFocus = wasOpen ? el._returnFocus : null;
  if (wasOpen) { el._returnFocus = null; el._pendingReturnFocus = returnFocus; }
  el.classList.remove("on"); el.setAttribute("aria-hidden", "true");
  if (el.id === "sheetBell" && notiClock) { clearInterval(notiClock); notiClock = 0; }
  if (el._trigger) {
    el._trigger.setAttribute("aria-expanded", "false");
    el._trigger = null;
  }
  /* 关闭动画期间继续保持 body.modal-lock。#app 会保持 display:none，
     因而 reader/sheet 的退场帧不会与底下信息流并排出现。 */
  let unlockAfter = null;
  if (wasOpen && el._modalLocked) {
    const cs = getComputedStyle(el);
    const parseTime = value => Math.max(0, ...String(value || "0s").split(",").map(v => {
      v = v.trim();
      if (v.endsWith("ms")) return parseFloat(v) || 0;
      if (v.endsWith("s")) return (parseFloat(v) || 0) * 1000;
      return 0;
    }));
    const duration = parseTime(cs.transitionDuration) + parseTime(cs.transitionDelay);
    const waitMs = Math.max(0, Math.min(560, duration));
    unlockAfter = () => {
      if (el._unlockTimer) { clearTimeout(el._unlockTimer); el._unlockTimer = 0; }
      if (el._unlockDone) { el.removeEventListener("transitionend", el._unlockDone); el._unlockDone = null; }
      el.classList.remove("closing");
      if (!el._modalLocked) return;
      el._modalLocked = false;
      unlockModalBackground();
      if (restore && returnFocus && document.contains(returnFocus) && !activeModal()) {
        requestAnimationFrame(() => {
          const hiddenReturn = returnFocus.closest('[aria-hidden="true"]') || returnFocus.closest("[inert]") ||
            getComputedStyle(returnFocus).visibility === "hidden" || !returnFocus.getClientRects().length;
          if (!hiddenReturn) { try { returnFocus.focus({ preventScroll: true }); } catch (_) {} }
        });
      }
      el._pendingReturnFocus = null;
    };
    el.classList.add("closing");
    if (waitMs > 0) {
      const done = e => {
        if (e && e.target !== el) return;
        unlockAfter();
      };
      el._unlockDone = done;
      el.addEventListener("transitionend", done);
      el._unlockTimer = setTimeout(unlockAfter, waitMs + 40);
    } else unlockAfter();
  }
  /* 只在真正打开过的一次关闭时还原焦点；否则重复调用 hidePageState() 等
     清理函数会拿着上一次的节点把用户焦点抢走。 */
  /* 没有过渡的弹层在上面已经同步解锁；有过渡的弹层由 unlockAfter
     在退场完成后恢复焦点。 */
  if (!unlockAfter && restore && returnFocus && document.contains(returnFocus) && !activeModal()) {
    requestAnimationFrame(() => {
      const hiddenReturn = returnFocus.closest('[aria-hidden="true"]') || returnFocus.closest("[inert]") ||
        getComputedStyle(returnFocus).visibility === "hidden" || !returnFocus.getClientRects().length;
      if (!hiddenReturn) { try { returnFocus.focus({ preventScroll: true }); } catch (_) {} }
    });
  }
}
function activeModal() {
  const all = [...document.querySelectorAll('[aria-modal="true"][aria-hidden="false"]')];
  return all.length ? all[all.length - 1] : null;
}

let stateRetry = null, stateAutoDismiss = false;
function showPageState(kind, title, message, actionText, action, icon, autoDismiss = false) {
  const el = document.getElementById("pageState"); if (!el) return;
  el.dataset.kind = kind || "connection";
  document.getElementById("stateKicker").textContent = kind === "expired" ? "体验提示" :
    kind === "result" ? "结果提示" : kind === "content" ? "内容提示" : "网络提示";
  document.getElementById("stateTitle").textContent = title;
  document.getElementById("stateMessage").textContent = message;
  const mark = document.getElementById("stateMark");
  mark.innerHTML = I(icon || (kind === "expired" ? "timer" : kind === "content" ? "triangle-alert" : "wifi-off"), 24);
  const btn = document.getElementById("stateAction"); btn.textContent = actionText || "重试"; stateRetry = action || (() => hidePageState());
  stateAutoDismiss = !!autoDismiss;
  btn.disabled = false; modalOpen(el, btn); Lucide.mount(mark);
}
function hidePageState() { stateRetry = null; stateAutoDismiss = false; modalClose(document.getElementById("pageState")); }
function handleSessionGone() {
  if (SID === null && document.getElementById("pageState").classList.contains("on")) return;
  clearInterval(pollTimer); pollTimer = 0; clearSessionDeadline(); SID = null;
  profileFailures = 0; lastProf = null; lastClickedCid = null; lastClickedSeq = null;
  /* 会话已经不存在时，队列里的事件也没有可投递的目标；保留它们只会让
     后续重试一直占着内存，或在新会话建立后误发到错误的会话。 */
  Q = [];
  updateRefreshControl();
  showPageState("expired", "体验已过期", "这次体验已结束，请重新开始。", "重新开始", () => location.reload(), "timer");
}
document.getElementById("stateAction").onclick = async () => {
  const b = document.getElementById("stateAction"), fn = stateRetry;
  if (!fn) return;
  b.disabled = true;
  try { await fn(); } finally { if (document.getElementById("pageState").classList.contains("on")) b.disabled = false; }
};

/* ─────────────────────────── 背景场：墨世界里缓慢流动的"信号迹线" ─────────────────────────── */
const FX = (() => {
  const cv = document.getElementById("fx"); const g = cv.getContext("2d");
  let W = 0, H = 0, on = false, raf = 0, t0 = 0, traces = [], burst = [];
  function size() { const d = Math.min(devicePixelRatio || 1, 2); W = innerWidth; H = innerHeight; cv.width = W * d; cv.height = H * d; g.setTransform(d, 0, 0, d, 0, 0); }
  function seed() {
    traces = Array.from({ length: 34 }, () => ({ x: Math.random() * W, y: Math.random() * H, a: Math.random() * Math.PI * 2,
      v: .12 + Math.random() * .25, l: 40 + Math.random() * 120, w: Math.random() < .2 ? 1.2 : .6, o: .05 + Math.random() * .1 }));
  }
  function frame(ts) {
    if (!on) return;
    if (!t0) t0 = ts; const t = (ts - t0) / 1000;
    g.clearRect(0, 0, W, H);
    // 大环：像扫描仪的刻度盘，极慢旋转
    const cx = W * .82, cy = H * .18, R = Math.min(W, H) * .62;
    g.save(); g.translate(cx, cy); g.rotate(t * .02);
    g.strokeStyle = "rgba(255,255,255,.05)"; g.lineWidth = 1; g.beginPath(); g.arc(0, 0, R, 0, 7); g.stroke();
    for (let i = 0; i < 90; i++) { const a = i / 90 * Math.PI * 2, L = i % 15 ? 5 : 12;
      g.beginPath(); g.moveTo(Math.cos(a) * (R - L), Math.sin(a) * (R - L)); g.lineTo(Math.cos(a) * R, Math.sin(a) * R); g.stroke(); }
    g.restore();
    for (const p of traces) {
      p.x += Math.cos(p.a) * p.v; p.y += Math.sin(p.a) * p.v; p.a += (Math.random() - .5) * .02;
      if (p.x < -p.l || p.x > W + p.l || p.y < -p.l || p.y > H + p.l) { p.x = Math.random() * W; p.y = Math.random() * H; }
      g.strokeStyle = "rgba(255,255,255," + p.o + ")"; g.lineWidth = p.w; g.beginPath();
      g.moveTo(p.x, p.y); g.lineTo(p.x - Math.cos(p.a) * p.l, p.y - Math.sin(p.a) * p.l); g.stroke();
    }
    // 删除时的碎屑
    for (let i = burst.length - 1; i >= 0; i--) { const b = burst[i]; b.x += b.vx; b.y += b.vy; b.vy += .12; b.life -= .016;
      if (b.life <= 0) { burst.splice(i, 1); continue; }
      g.fillStyle = "rgba(" + PAL.acRGB + "," + (b.life * .9) + ")"; g.fillRect(b.x, b.y, b.s, b.s); }
    raf = requestAnimationFrame(frame);
  }
  function start() { if (REDUCED) return; size(); if (!traces.length) seed(); on = true; cv.classList.add("on"); cancelAnimationFrame(raf); raf = requestAnimationFrame(frame); }
  function stop() { on = false; cv.classList.remove("on"); cancelAnimationFrame(raf); }
  function boom() { for (let i = 0; i < 140; i++) burst.push({ x: W / 2 + (Math.random() - .5) * W * .8, y: H * .3 + Math.random() * H * .5,
    vx: (Math.random() - .5) * 3, vy: -1 - Math.random() * 3, s: 1 + Math.random() * 3, life: .8 + Math.random() * .6 }); }
  addEventListener("resize", () => { if (on) size(); });
  document.addEventListener("visibilitychange", () => { if (document.hidden) cancelAnimationFrame(raf); else if (on) raf = requestAnimationFrame(frame); });
  return { start, stop, boom };
})();

/* ─────────────────────────── 会话状态 ─────────────────────────── */
let SID = null, SEQ = 0, SCREEN = 0, SHOW_ON_SCREEN = true, CODENAME = "";
// 同一次启动失败后复用；成功进入体验后清除，明确重开会获得新值。
let createRequestId = null;
/* 最近一次打开的卡片只作为下一批推荐的短期上下文；画像仍由服务端事件计算。
   seq 与点击事件共用，避免正文预取/刷新请求乱序时回到旧锚点。 */
let lastClickedCid = null, lastClickedSeq = null;
const refreshBtn = document.getElementById("refreshBtn");
let notiClock = 0;
let activeView = "home";
const MIN_SCREENS = 3, MAX_SCREENS = 6;      // 不"刷满 N 屏自动结算"：由结束卡交给观众主动点
let screensLoaded = 0, converged = false, ended = false, extensionTarget = null;
let Q = [], cards = {}, impressed = {}, opened = {}, readSet = {};
let cardRenderSeq = 0, batchRequestSeq = 0;
let curCid = null, curPos = 0, curOpenAt = 0, curMaxScroll = 0, curInfo = null, curContentReady = false;
let finishing = false, busy = false, screenLoading = false, refreshLoading = false,
  sessionEndEmitted = false, screenRetryPending = false;
let flushInFlight = null;
let discoverClicks = 0;
const discoverClickSet = new Set();
window.__discoverClicks = 0;
const SESSION_DEADLINE_MS = 180000;
let sessionDeadlineTimer = 0, sessionDeadlineAt = 0;

/* 三分钟兜底从会话真正建立时开始计算，而不是从 HTML 脚本加载时开始。
   若到点时恰好正在生成结果，就短暂续等；失败后只恢复“剩余时间”，
   不会因为重试把整个会话的兜底期限不断往后推。 */
function armSessionDeadline(reset = false) {
  if (reset || !sessionDeadlineAt) sessionDeadlineAt = Date.now() + SESSION_DEADLINE_MS;
  if (sessionDeadlineTimer) { clearTimeout(sessionDeadlineTimer); sessionDeadlineTimer = 0; }
  if (!SID || sessionDeadlineAt <= Date.now()) return;
  const sidAtArm = SID;
  const settle = () => {
    if (!SID || SID !== sidAtArm) { sessionDeadlineTimer = 0; return; }
    const left = sessionDeadlineAt - Date.now();
    if (left > 0) { sessionDeadlineTimer = setTimeout(settle, left); return; }
    if (finishing) { sessionDeadlineTimer = setTimeout(settle, 1000); return; }
    sessionDeadlineTimer = 0;
    finish();
  };
  sessionDeadlineTimer = setTimeout(settle, Math.max(0, sessionDeadlineAt - Date.now()));
}
function clearSessionDeadline() {
  if (sessionDeadlineTimer) clearTimeout(sessionDeadlineTimer);
  sessionDeadlineTimer = 0;
  sessionDeadlineAt = 0;
}
function pauseSessionDeadline() {
  if (sessionDeadlineTimer) clearTimeout(sessionDeadlineTimer);
  sessionDeadlineTimer = 0;
}

/* 事件上报：批量 + 节流 + 失败重试，服务端按 seq 幂等去重（§8.8） */
function emit(type, cid, props) {
  const event = { seq: ++SEQ, ts: Date.now(), type, content_id: cid || null, props: props || {} };
  Q.push(event);
  return event;
}
async function flush() {
  /* 定时器、阅读关闭和结算都会触发 flush；串行化请求，避免后发的停留
     事件赶在前一批点击之前抵达服务端。调用方会等到当前批次完成。 */
  if (!SID) return false;
  /* 即使本地 Q 暂时为空，也可能有一批事件已经从 Q 中取出、仍在
     网络上飞行。先等待它，再判断“已全部送达”，否则刷新/下一屏会
     抢在最近一次点击之前读取旧画像。 */
  while (SID) {
    if (flushInFlight) { await flushInFlight; continue; }
    if (!Q.length) return true;
    const sidAtStart = SID, b = Q; Q = [];
    let failed = false;
    const task = (async () => {
      try {
        const r = await fetchTimed(API + "/api/events?sid=" + encodeURIComponent(sidAtStart), {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ events: b })
        }, 8000);
        if (!r.ok) {
          const d = await r.json().catch(() => ({}));
          if (d.error === "session_gone") { handleSessionGone(); return; }
          throw new Error(d.error || ("http_" + r.status));
        }
      } catch (e) {
        failed = true;
        /* 会话已经切换/过期时不要把旧批次带进下一次会话。 */
        if (SID === sidAtStart) Q = b.concat(Q);
      }
    })();
    flushInFlight = task;
    try { await task; } finally { if (flushInFlight === task) flushInFlight = null; }
    if (failed || SID !== sidAtStart) return false;
  }
  return false;
}
/* 推荐请求与行为上报必须保持同一条因果链：如果最近一次点击还在本地
   队列里，继续请求下一批会让服务端只能按旧画像排序。调用方需要一个
   明确的失败信号，而不是把 flush() 的 false 静默忽略掉。 */
async function flushRequired() {
  const ok = await flush();
  if (!ok || !SID || Q.length) {
    throw Object.assign(new Error("events_pending"), { code: "events_pending" });
  }
  return true;
}
setInterval(flush, 800);

/* ─────────────────────────── 告知页：3 秒读完才能开始 ─────────────────────────── */
Lucide.mount();
FX.start();
const gb = document.getElementById("gateBtn"), gtxt = document.getElementById("gateTxt");
requestAnimationFrame(() => gb.classList.add("arm"));
setTimeout(() => {
  gb.disabled = false; gtxt.textContent = "我知道了，开始体验";
  /* 键盘用户停在页面正文时，告知读完后把焦点送到唯一的行动按钮；
     若用户已经在切换开关上操作，不抢走其焦点。 */
  if (document.activeElement === document.body) {
    try { gb.focus({ preventScroll: true }); } catch (_) {}
  }
}, REDUCED ? 300 : 3000);
gb.onclick = start;

async function start() {
  if (!createRequestId) createRequestId = newCreateRequestId();
  const requestIdAtStart = createRequestId;
  try {
    gb.disabled = true; gtxt.textContent = "正在开始…";
    SHOW_ON_SCREEN = document.getElementById("screenConsent").checked;
    const d = await apiJson(API + "/api/session", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ show_on_screen: SHOW_ON_SCREEN, request_id: requestIdAtStart }) });
    if (!d || typeof d.session_id !== "string" || !d.session_id || typeof d.codename !== "string" || !d.codename) {
      throw Object.assign(new Error("bad session payload"), { code: "session_payload" });
    }
    SID = d.session_id; SHOW_ON_SCREEN = !!d.show_on_screen; CODENAME = d.codename;
    lastClickedCid = null; lastClickedSeq = null; SEQ = 0;
    sessionEndEmitted = false;
    screensLoaded = 0; SCREEN = 0; converged = false; ended = false; extensionTarget = null;
    screenRetryPending = false; refreshLoading = false; screenLoading = false;
    discoverLoaded = false; discoverLoading = false; discoverClicks = 0; discoverClickSet.clear(); window.__discoverClicks = 0;
    Q = []; cards = {}; impressed = {}; opened = {}; readSet = {}; cardRenderSeq = 0; batchRequestSeq = 0;
    lastProf = null; notis = []; unread = 0; profileFailures = 0;
    updateRefreshControl();
    armSessionDeadline(true);
    hidePageState();
    document.getElementById("codename").textContent = CODENAME;
    document.getElementById("mineCode").textContent = CODENAME;
    document.getElementById("gate").classList.add("hide");
    document.getElementById("gate").setAttribute("aria-hidden", "true");
    document.body.classList.remove("ink"); FX.stop();
    document.body.classList.add("app-active");
    document.querySelector('meta[name=theme-color]').setAttribute("content", PAL.paper);
    document.getElementById("app").classList.remove("hide");
    const app = document.getElementById("app");
    app.setAttribute("aria-hidden", "false"); app.removeAttribute("inert");
    try { app.inert = false; } catch (_) {}
    buildChrome(); buildMine(); emit("session_start");
    /* 先取一次基线，后续 5 秒轮询只报告真正发生的变化。 */
    await pollProfile();
    const firstScreenLoaded = await loadScreen();
    if (firstScreenLoaded && activeView === "home" && !ended) MORE_IO.observe(document.getElementById("more"));
    /* 告知页是一个真正的入口页；进入信息流后把键盘焦点交给第一张内容卡，
       不把焦点留在已经 display:none 的开始按钮上。 */
    requestAnimationFrame(() => {
      const target = document.querySelector("#homeView .card") || document.getElementById("navSearch");
      if (target && !document.getElementById("pageState").classList.contains("on") &&
          (document.activeElement === gb || document.activeElement === document.body)) {
        try { target.focus({ preventScroll: true }); } catch (_) {}
      }
    });
    if (createRequestId === requestIdAtStart) createRequestId = null;
  } catch (e) {
    gb.disabled = false; gtxt.textContent = "我知道了，开始体验";
    showPageState("connection", "暂时无法开始", "请检查网络后重试。", "重试", start, "wifi-off");
  }
}

/* 「我的」面板：采集清单原样摆出来，随时可查（§6.3） */
const MINE_YES = ["本次体验的临时代号", "本次体验中的点击、阅读时长和滑动", "根据这些行为生成的兴趣画像"];
const MINE_NO = ["姓名、联系方式和账号标识", "设备标识", "地理位置",
  "通讯录、相册、麦克风和摄像头", "剪贴板和浏览历史"];
function buildMine() {
  document.getElementById("mCollect").innerHTML = MINE_YES.map(x => "<li>" + I("check", 15) + "<span>" + esc(x) + "</span></li>").join("");
  document.getElementById("mNo").innerHTML = MINE_NO.map(x => '<li class="no">' + I("ban", 15) + "<span>" + esc(x) + "</span></li>").join("");
  const pref = document.getElementById("screenPref");
  pref.checked = SHOW_ON_SCREEN;
  pref.onchange = async () => {
    const prev = SHOW_ON_SCREEN; SHOW_ON_SCREEN = pref.checked; pref.disabled = true;
    try {
      await apiJson(API + "/api/session/visibility?sid=" + encodeURIComponent(SID), { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ show_on_screen: SHOW_ON_SCREEN }) });
      toast(SHOW_ON_SCREEN ? "已开启大屏展示" : "已关闭大屏展示");
    } catch (e) {
      SHOW_ON_SCREEN = prev; pref.checked = prev;
      if (e.code === "session_gone") handleSessionGone();
      else toast("切换失败，请再试一次");
    }
    finally { pref.disabled = false; }
  };
}

/* ─────────────────────────── 顶栏：搜索面板 + 通知面板 ───────────────────────────
   两个面板均为只读展示：搜索展示猜测词，通知展示画像变化。 */

let lastProf = null, notis = [], unread = 0, pollTimer = 0, profileFailures = 0, profileLoading = false;

function openSheet(id) {
  const el = document.getElementById(id);
  const trigger = document.querySelector('[aria-controls="' + id + '"]');
  if (trigger) { el._trigger = trigger; trigger.setAttribute("aria-expanded", "true"); }
  modalOpen(el, el.querySelector(".sh-x") || el.querySelector("[data-close]"));
  if (id === "sheetBell") {
    clearInterval(notiClock);
    notiClock = setInterval(() => {
      if (el.classList.contains("on")) renderNotis(); else { clearInterval(notiClock); notiClock = 0; }
    }, 1000);
  }
}
function closeSheet(el) {
  modalClose(el);
  if (el && el._trigger) { el._trigger.setAttribute("aria-expanded", "false"); el._trigger = null; }
}
document.querySelectorAll(".sheet").forEach(sh => {
  sh.querySelectorAll("[data-close]").forEach(btn => btn.onclick = () => closeSheet(sh));
  sh.addEventListener("click", e => { if (e.target === sh) closeSheet(sh); });
});
addEventListener("keydown", e => {
  const m = activeModal();
  if (!m) return;
  if (e.key === "Escape") {
    e.preventDefault();
    /* 告知页是必经承诺，不提供 Esc 绕过入口；整页状态也必须由按钮离开。 */
    if (m.id === "pageState" || m.id === "gate" || m.id === "result") return;
    modalClose(m); return;
  }
  if (e.key !== "Tab") return;
  const focusable = [...m.querySelectorAll("button:not([disabled]),[href],input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex='-1'])")]
    .filter(el => !el.hidden && !el.closest("[hidden]") && !el.closest('[aria-hidden="true"]') &&
      getComputedStyle(el).display !== "none" && getComputedStyle(el).visibility !== "hidden");
  if (!focusable.length) { e.preventDefault(); m.focus(); return; }
  const first = focusable[0], last = focusable[focusable.length - 1];
  if (!m.contains(document.activeElement)) { e.preventDefault(); first.focus(); return; }
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
});
/* 某些 iOS/WebView 版本不会完全遵守 overscroll-behavior：在弹层边缘继续拖动时，
   手势仍可能落到 document。把弹层外的触摸/滚轮明确截断，弹层内部自己的滚动不受影响。 */
addEventListener("touchmove", e => {
  const m = activeModal();
  if (!m) return;
  const rootCanScroll = m.id === "gate" || m.id === "reader" || m.id === "result";
  /* sheet 的滚动根是 .sh-bd；sheet 外壳本身只是关闭用的背景。 */
  const insideScrollableSheet = m.classList.contains("sheet") && e.target && typeof e.target.closest === "function" && !!e.target.closest(".sh-bd");
  if (!m.contains(e.target) || (m.classList.contains("sheet") && !insideScrollableSheet) ||
      (e.target === m && !rootCanScroll && !insideScrollableSheet)) e.preventDefault();
}, { passive: false });
addEventListener("wheel", e => {
  const m = activeModal();
  if (m && !m.contains(e.target)) e.preventDefault();
}, { passive: false });

/* 搜索：把画像里的词直接摆出来。词条不可点——它们是「它推断出的东西」的陈列，不是控件。 */
function buildGuesses() {
  const box = document.getElementById("guessChips"), note = document.getElementById("guessNote");
  const p = lastProf;
  const words = [];
  const addRanked = (obj, weight) => Object.entries(obj || {}).forEach(([key, value]) => {
    if (!value || typeof value !== "object" || Array.isArray(value)) return;
    const cn = String(value.cn == null ? "" : value.cn).trim();
    if (!cn) return;
    const score = Number.isFinite(Number(value.score)) ? Number(value.score) : 0;
    const conf = Number.isFinite(Number(value.conf)) ? Number(value.conf) : 0;
    words.push({ cn, rank: score * weight + conf * .08, key });
  });
  if (p) { addRanked(p.sub_tags, .85); addRanked(p.domains, 1); }
  words.sort((a, b) => b.rank - a.rank || a.key.localeCompare(b.key));
  const uniq = [...new Set(words.map(x => x.cn))].slice(0, 8);
  if (!uniq.length) {
    box.innerHTML = '<div class="noguess">' + I("search", 22) + "<span>暂无兴趣猜测</span><small>浏览或打开几条内容后再来看</small></div>";
    note.textContent = "兴趣猜测会随着浏览更新。";
    return;
  }
  box.innerHTML = uniq.map((w, i) => '<span class="chip' + (i ? "" : " hot") + '">' +
    (i ? "" : I("trending-up", 13)) + esc(w) + "</span>").join("");
  note.textContent = "根据你最近浏览的内容生成。";
}

/* 通知：记录本次体验中的画像变化。 */
function pushNoti(icon, text) {
  notis.unshift({ icon, text, t: Date.now() });
  if (notis.length > 30) notis.length = 30;
  const panel = document.getElementById("sheetBell");
  /* 通知面板正开着时，新变化已经直接呈现在眼前，不再反过来点亮“未读”。 */
  if (!panel || panel.getAttribute("aria-hidden") !== "false") {
    unread++;
    document.getElementById("bellDot").classList.remove("hide");
    document.getElementById("navBell").setAttribute("aria-label", "通知，有" + unread + "条未读");
  }
}
function renderNotis() {
  const el = document.getElementById("notiList");
  if (!notis.length) {
    el.innerHTML = '<li class="empty">' + I("bell", 20) + "<span>暂无变化</span><small>浏览内容后，变化会显示在这里</small></li>";
    return;
  }
  const ago = t => { const s = Math.round((Date.now() - t) / 1000); return s < 60 ? s + " 秒前" : Math.round(s / 60) + " 分钟前"; };
  el.innerHTML = notis.map(n => "<li>" + I(n.icon, 16) + '<div><p>' + esc(n.text) + "</p><small>" + ago(n.t) + "</small></div></li>").join("");
}
function diffProfile(p) {
  if (!lastProf) return;                       // 首次只记基线，不产生通知
  const top = p.top_domain || Object.keys(p.domains || {})[0];
  const prev = lastProf.top_domain || Object.keys(lastProf.domains || {})[0];
  if (top && top !== prev) {
    const topName = (p.domains && p.domains[top] && p.domains[top].cn) || top;
    pushNoti("target", prev ? "首要兴趣更新为「" + topName + "」"
                            : "新增兴趣判断：「" + topName + "」");
  }
  for (const [a, t] of Object.entries(p.traits || {})) {
    if (!t || typeof t !== "object") continue;
    const pv = (lastProf.traits || {})[a];
    if (t.pole && (!pv || pv.pole !== t.pole)) pushNoti("git-merge", "当前倾向更新为「" + (t.pole_cn || t.pole) + "」");
  }
  for (const [d, v] of Object.entries(p.domains || {})) {
    if (!v || typeof v !== "object") continue;
    const pv = (lastProf.domains || {})[d];
    if (v.conf >= 1 && !(pv && pv.conf >= 1)) {
      pushNoti("check", "「" + (v.cn || d) + "」判断更加明确");
    }
  }
}
async function pollProfile() {
  if (!SID || finishing || profileLoading) return false;
  const sidAtStart = SID;
  profileLoading = true;
  try {
    let p;
    try {
      p = await apiJson(API + "/api/profile?sid=" + encodeURIComponent(sidAtStart));
      if (!p || typeof p !== "object" || !p.domains || typeof p.domains !== "object" || Array.isArray(p.domains) ||
          !p.traits || typeof p.traits !== "object" || Array.isArray(p.traits)) throw Object.assign(new Error("bad profile payload"), { code: "profile_payload" });
      if (!SID || SID !== sidAtStart || finishing) return false;
      profileFailures = 0;
    } catch (e) {
      /* 旧请求可能在结算、删除或会话切换后才失败；它无权再覆盖当前页面。 */
      if (SID !== sidAtStart) return false;
      if (e.code === "session_gone") { handleSessionGone(); return false; }
      if (!SID || finishing) return false;
      profileFailures++;
      if (profileFailures >= 3 && !document.getElementById("pageState").classList.contains("on")) {
        showPageState("connection", "网络连接不稳定", "暂时无法更新兴趣画像，请重试。", "重试", pollProfile, "wifi-off", true);
      }
      return false;
    }
    diffProfile(p);
    lastProf = p;
    if (typeof p.converged === "boolean") converged = p.converged;
    if (document.getElementById("sheetSearch").classList.contains("on")) buildGuesses();
    if (document.getElementById("sheetBell").classList.contains("on")) renderNotis();
    if (stateAutoDismiss && document.getElementById("pageState").classList.contains("on") && document.getElementById("pageState").dataset.kind === "connection") hidePageState();
    return true;
  } finally {
    profileLoading = false;
  }
}

function buildChrome() {
  document.getElementById("navSearch").onclick = () => { buildGuesses(); openSheet("sheetSearch"); };
  document.getElementById("navBell").onclick = () => {
    unread = 0; document.getElementById("bellDot").classList.add("hide");
    document.getElementById("navBell").setAttribute("aria-label", "通知");
    renderNotis(); openSheet("sheetBell"); Lucide.mount(document.getElementById("sheetBell"));
  };
  if (refreshBtn) refreshBtn.onclick = () => { if (!refreshBtn.disabled) refreshFeed(); };
  updateRefreshControl();
  clearInterval(pollTimer); pollTimer = setInterval(pollProfile, 5000);
}

/* ─────────────────────────── 加载一屏 ─────────────────────────── */
function skeleton(n) { return Array.from({ length: n }, () => '<div class="sk"><i class="a"></i><div class="b"><i></i><i></i><i></i></div></div>').join(""); }
function normalizeCards(value) {
  if (!Array.isArray(value)) throw Object.assign(new Error("bad cards payload"), { code: "cards_payload" });
  const ids = new Set(), out = [];
  value.forEach(raw => {
    if (!raw || typeof raw !== "object") throw Object.assign(new Error("bad card"), { code: "cards_payload" });
    const contentId = String(raw.content_id == null ? "" : raw.content_id).trim();
    const title = String(raw.title == null ? "" : raw.title).trim();
    if (!contentId || !title) throw Object.assign(new Error("bad card"), { code: "cards_payload" });
    /* 服务端正常不会重复；网络代理/旧缓存偶尔会把同一 ID 拼两次，
       客户端只保留第一次，避免两个 DOM 实例同时竞争曝光计时。 */
    if (ids.has(contentId)) return;
    ids.add(contentId);
    out.push(Object.assign({}, raw, {
      content_id: contentId,
      title,
      summary: String(raw.summary == null ? "" : raw.summary),
      domain_cn: String(raw.domain_cn == null ? "未分类" : raw.domain_cn),
      cover_theme: String(raw.cover_theme == null ? "" : raw.cover_theme),
      body_len: raw.body_len === "L" ? "L" : "S",
    }));
  });
  return out;
}
function newBatchRequestId(mode) {
  /* 会话本身已隔离命名空间，单调序号足以标识一次逻辑请求；同一次失败
     的重试复用这个值，服务端即可返回原批次而不重复推进状态。 */
  batchRequestSeq += 1;
  return (mode === "refresh" ? "refresh" : "next") + "-" + batchRequestSeq.toString(36);
}
function screenRequestUrl(sid, mode = "next", requestId = null) {
  let url = API + "/api/screen?sid=" + encodeURIComponent(sid);
  if (mode === "refresh") url += "&mode=refresh";
  if (requestId) url += "&request_id=" + encodeURIComponent(requestId);
  /* 事件队列通常已在请求前送达；anchor/anchor_seq 是网络抖动时的短暂
     提示，服务端只接受本会话已经见过/打开过的 content_id，不会凭空
     扩展可见内容范围，也不会让旧请求覆盖新点击。 */
  if (lastClickedCid) {
    url += "&anchor=" + encodeURIComponent(lastClickedCid);
    if (lastClickedSeq != null) url += "&anchor_seq=" + encodeURIComponent(String(lastClickedSeq));
  }
  return url;
}
function cardVariant(c, screen) {
  /* 首屏是正交探针，强制同构；后续按内容 ID 稳定分成约 5:2:3。 */
  if (screen === 0) return "row";
  const n = hash(c.content_id + "|" + (c.body_len || "S")) % 10;
  if (n < 2 || (c.body_len === "L" && n < 4)) return "feature";
  if (n >= 7) return "text";
  return "row";
}
/* 单张卡片：首页与「发现」共用同一份渲染，保持两处展示一致。 */
function renderCard(c, pos, screen, delay, source = "home", batchKind = "next") {
  /* 同一内容在刷新后可能再次出现。每次渲染都给一个实例 key，避免旧
     DOM 节点与新节点共用曝光计时器/位次信息；content_id 仍作为行为主键。 */
  const baseKey = source + ":" + c.content_id;
  const key = baseKey + ":" + (++cardRenderSeq);
  const info = { c, pos, screen, source, key, baseKey, batchKind };
  cards[key] = info;
  cards[baseKey] = info;
  if (source === "home") cards[c.content_id] = info; // 保留按 content_id 读取卡片的兼容性
  const hs = hash(c.content_id);
  const el = document.createElement("article");
  /* 刷新批次不是首屏探针，即使当前 screen_index 为 0，也使用普通混排版式。 */
  const variant = cardVariant(c, batchKind === "refresh" ? Math.max(1, Number(screen) || 0) : screen);
  el.className = "card enter variant-" + variant; el.dataset.cid = c.content_id; el.dataset.key = key;
  el.dataset.feed = source; el.setAttribute("role", "button"); el.setAttribute("tabindex", "0");
  el.setAttribute("aria-label", (source === "discover" ? "打开发现内容：" : "打开内容：") + c.title);
  el.style.setProperty("--h", hue(c.cover_theme));
  el.style.animationDelay = (delay || 0) + "ms";
  const cover = variant === "text" ? "" : '<div class="cv"><span class="gl">' + esc((c.domain_cn || "·")[0]) + '</span><span class="len">' + (c.body_len === "L" ? "深度" : "速览") + "</span></div>";
  el.innerHTML =
    cover +
    '<div class="ct"><h3>' + esc(c.title) + '</h3><p class="sm">' + esc(c.summary) + "</p>" +
    '<div class="mt"><span class="src">' + esc(publisher(c)) + '</span><span class="chip">' + esc(c.domain_cn) + "</span>" +
    "<span>" + (1 + hs % 59) + " 分钟前</span>" +
    '<span class="rd">' + I("eye", 12) + ((hs >>> 8) % 9 + 1) + "." + ((hs >>> 4) % 10) + " 万</span></div></div>" +
    '<span class="done">' + I("check", 12) + "</span>";
  const activate = () => openContent(c.content_id, info);
  el.onclick = activate; el.onkeydown = e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); activate(); } };
  IO.observe(el);
  return el;
}

function forgetCardElement(el) {
  if (!el) return;
  const key = el.dataset.key || "";
  const info = cards[key];
  if (key && timers[key]) { clearTimeout(timers[key]); delete timers[key]; }
  if (key) { delete impressed[key]; delete impressed[key + "_s"]; }
  if (key) delete cards[key];
  if (info && info.baseKey && cards[info.baseKey] === info) delete cards[info.baseKey];
  if (info && info.source === "home" && cards[info.c.content_id] === info) delete cards[info.c.content_id];
  IO.unobserve(el);
  el.remove();
}

async function loadScreen(requestId = null) {
  if (screenLoading || refreshLoading || !SID || finishing) return false;
  const logicalRequestId = requestId || newBatchRequestId("next");
  screenLoading = true;
  updateRefreshControl();
  screenRetryPending = false;
  const sidAtStart = SID;
  const feed = document.getElementById("feed"), more = document.getElementById("more");
  feed.setAttribute("aria-busy", "true");
  more.onclick = null; more.onkeydown = null; more.removeAttribute("role"); more.removeAttribute("tabindex"); more.removeAttribute("aria-label");
  const sk = document.createElement("div"); sk.innerHTML = skeleton(screensLoaded ? 3 : 5); feed.appendChild(sk);
  try {
    /* 点击事件可能还在 800ms 批量窗口内；先等当前批次，确保服务端用
       最近点击重排下一屏，而不是按旧画像返回一批无关内容。 */
    await flushRequired();
    if (!SID || SID !== sidAtStart || finishing || ended) { sk.remove(); return false; }
    const d = await apiJson(screenRequestUrl(sidAtStart, "next", logicalRequestId));
    if (!d || typeof d !== "object") throw Object.assign(new Error("bad screen payload"), { code: "cards_payload" });
    const nextCards = normalizeCards(d.cards);
    if (!nextCards.length) throw Object.assign(new Error("empty cards payload"), { code: "cards_payload" });
    await wait(screensLoaded ? 260 : 420);          // 让骨架屏短暂可见
    if (!SID || SID !== sidAtStart || finishing || ended) { sk.remove(); return false; }
    sk.remove();
    SCREEN = Number.isFinite(Number(d.screen_index)) ? Number(d.screen_index) : screensLoaded;
    const fragment = document.createDocumentFragment();
    nextCards.forEach((c, i) => fragment.appendChild(renderCard(c, i, SCREEN, i * 55)));
    feed.appendChild(fragment);
    emit("screen_view", null, { screen_index: SCREEN });
    screensLoaded++; converged = !!d.converged;
    screenRetryPending = false;
    more.innerHTML = '<span class="sp"></span>正在加载';
    flush();
    return true;
  } catch (e) {
    sk.remove();
    if (!SID || SID !== sidAtStart || finishing || ended) return false;
    screenRetryPending = true;
    /* 哨兵在视口内时，失败后继续保持观察会让重试请求与状态页并发；
       先摘掉观察，明确交给状态页/底部入口发起下一次请求。 */
    MORE_IO.unobserve(more);
    more.textContent = "加载失败 · 点击重试";
    more.setAttribute("role", "button"); more.tabIndex = 0; more.setAttribute("aria-label", "加载失败，点击重试");
    more.onclick = async () => {
      more.onclick = null; screenRetryPending = false; hidePageState();
      const ok = await loadScreen(logicalRequestId);
      if (ok && activeView === "home" && !ended && SID) MORE_IO.observe(more);
    };
    more.onkeydown = e => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); more.click(); }
    };
    if (e.code === "session_gone") handleSessionGone();
    else if (e.code === "events_pending") showPageState("connection", "网络连接不稳定", "暂时无法加载更多内容，请重试。", "重新加载", async () => {
      hidePageState(); screenRetryPending = false;
      const ok = await loadScreen(logicalRequestId);
      if (ok && activeView === "home" && !ended && SID) MORE_IO.observe(more);
    }, "wifi-off");
    else if (activeView === "home" && SID && !finishing) showPageState("content", "内容加载失败", "暂时无法加载更多内容，请重试。", "重新加载", async () => {
      hidePageState(); screenRetryPending = false;
      const ok = await loadScreen(logicalRequestId);
      if (ok && activeView === "home" && !ended && SID) MORE_IO.observe(more);
    }, "triangle-alert");
    return false;
  } finally {
    screenLoading = false;
    feed.setAttribute("aria-busy", "false");
    updateRefreshControl();
  }
}

/* ─────────────────────────── 下拉刷新 ───────────────────────────
   首页是独立滚动容器，不能依赖浏览器自带的 overscroll 回弹（不同 WebView
   行为不一致）。在 scrollTop=0 且手指向下拖动时显示轻量指示器，松手超过
   阈值才请求一批新内容；事件先 flush，服务端因此能用最近点击做重排。 */
const PULL_TRIGGER = 72, PULL_MAX = 132;
const homeView = document.getElementById("homeView");
const pullEl = document.getElementById("pullRefresh");
const pullLabel = document.getElementById("pullRefreshLabel");
let pullGesture = null, pullClickBlockTimer = 0, suppressPullClick = false;
function updateRefreshControl() {
  if (!refreshBtn) return;
  const available = !!SID && activeView === "home" && !finishing && !ended &&
    !screenLoading && !refreshLoading;
  refreshBtn.disabled = !available;
  refreshBtn.setAttribute("aria-disabled", String(!available));
  refreshBtn.classList.toggle("loading", refreshLoading);
  const loading = screenLoading || refreshLoading;
  refreshBtn.setAttribute("aria-busy", String(loading));
  refreshBtn.setAttribute("aria-label", loading ? (refreshLoading ? "正在刷新内容" : "正在加载内容") :
    (screensLoaded > 0 ? "换一批内容" : "重新加载内容"));
}
function blockPullClick() {
  suppressPullClick = true;
  clearTimeout(pullClickBlockTimer);
  /* 触屏浏览器通常在 touchend 后才合成 click；留一小段窗口拦住它，
     避免用户只是下拉回弹却误打开手指下方的卡片。 */
  pullClickBlockTimer = setTimeout(() => { suppressPullClick = false; pullClickBlockTimer = 0; }, 700);
}
homeView.addEventListener("click", e => {
  if (!suppressPullClick) return;
  e.preventDefault(); e.stopPropagation();
  suppressPullClick = false; clearTimeout(pullClickBlockTimer); pullClickBlockTimer = 0;
}, true);
function setPullVisual(distance, state = "dragging") {
  if (!pullEl) return;
  const d = Math.max(0, Math.min(PULL_MAX, Number(distance) || 0));
  const loading = state === "loading";
  pullEl.classList.toggle("dragging", state === "dragging");
  pullEl.classList.toggle("loading", loading);
  pullEl.style.height = loading ? "54px" : d + "px";
  pullEl.setAttribute("aria-hidden", String(!loading && d < 1));
  if (pullLabel) pullLabel.textContent = loading ? "正在刷新" :
    (d >= PULL_TRIGGER ? "松开刷新" : "下拉刷新");
  const icon = pullEl.querySelector(".pull-icon");
  if (icon && !loading) icon.style.transform = "rotate(" + Math.round(180 * d / PULL_TRIGGER) + "deg)";
  if (icon && loading) icon.style.transform = "";
}
function resetPullVisual() {
  if (!pullEl) return;
  pullEl.classList.remove("dragging", "loading");
  pullEl.style.height = "0px";
  pullEl.setAttribute("aria-hidden", "true");
  if (pullLabel) pullLabel.textContent = "下拉刷新";
  const icon = pullEl.querySelector(".pull-icon");
  if (icon) icon.style.transform = "";
}
function pullAllowed() {
  return !!SID && activeView === "home" && !finishing && !ended &&
    !screenLoading && !refreshLoading && !document.body.classList.contains("modal-lock") &&
    homeView && homeView.scrollTop <= 1;
}
homeView.addEventListener("touchstart", e => {
  if (!e.touches || e.touches.length !== 1) {
    /* 第二根手指按下本身就会触发 touchstart，不保证随后还有 touchmove；
       在这里立即作废旧的单指距离，避免任一手指松开时误触发刷新。 */
    if (pullGesture && pullGesture.moved) blockPullClick();
    pullGesture = null; resetPullVisual();
    return;
  }
  if (!pullAllowed()) return;
  const t = e.touches[0];
  pullGesture = { x: t.clientX, y: t.clientY, distance: 0, moved: false };
}, { passive: true });
homeView.addEventListener("touchmove", e => {
  if (!pullGesture) return;
  if (!e.touches || e.touches.length !== 1) {
    /* 双指缩放/系统手势不应沿用单指拖动距离，在 touchend 时误触发刷新。 */
    pullGesture = null; resetPullVisual();
    return;
  }
  if (!pullAllowed()) { pullGesture = null; resetPullVisual(); return; }
  const t = e.touches[0], dx = t.clientX - pullGesture.x, dy = t.clientY - pullGesture.y;
  if (dy <= 0 || (Math.abs(dx) > Math.abs(dy) && !pullGesture.moved)) {
    pullGesture = null; resetPullVisual(); return;
  }
  pullGesture.moved = true;
  blockPullClick();
  /* 阻尼曲线：拖得越远增长越慢，避免把导航栏拽出视口。 */
  const distance = Math.min(PULL_MAX, dy * 0.55);
  pullGesture.distance = distance;
  if (distance > 3 && e.cancelable) e.preventDefault();
  setPullVisual(distance, "dragging");
}, { passive: false });
async function finishPullGesture() {
  if (!pullGesture) return;
  /* 防误触窗口应从松手时开始计算。用户可能拉动后停住片刻再松手；
     若只在 touchmove 时计时，浏览器随后合成的 click 仍可能打开卡片。 */
  if (pullGesture.moved) blockPullClick();
  const shouldRefresh = pullGesture.distance >= PULL_TRIGGER;
  pullGesture = null;
  if (!shouldRefresh) { resetPullVisual(); return; }
  setPullVisual(PULL_TRIGGER, "loading");
  try { await refreshFeed(); }
  finally { resetPullVisual(); }
}
homeView.addEventListener("touchend", finishPullGesture, { passive: true });
homeView.addEventListener("touchcancel", () => { if (pullGesture && pullGesture.moved) blockPullClick(); pullGesture = null; resetPullVisual(); }, { passive: true });

async function refreshFeed(requestId = null) {
  if (refreshLoading || screenLoading || !SID || finishing || ended || activeView !== "home") return false;
  /* 首屏请求失败时，顶部手势/按钮也应能作为重试出口；服务端在
     screen_index=0 时不会接受 refresh 模式，因此这里回到普通加载链路。 */
  if (screensLoaded <= 0) return loadScreen();
  const logicalRequestId = requestId || newBatchRequestId("refresh");
  refreshLoading = true;
  updateRefreshControl();
  const sidAtStart = SID;
  const feed = document.getElementById("feed"), more = document.getElementById("more");
  let refreshSucceeded = false;
  feed.setAttribute("aria-busy", "true");
  const sk = document.createElement("div");
  sk.innerHTML = skeleton(3);
  feed.insertBefore(sk, feed.firstChild);
  try {
    await flushRequired();
    if (!SID || SID !== sidAtStart || finishing || ended) { sk.remove(); return false; }
    const d = await apiJson(screenRequestUrl(sidAtStart, "refresh", logicalRequestId));
    if (!d || typeof d !== "object") throw Object.assign(new Error("bad refresh payload"), { code: "cards_payload" });
    const nextCards = normalizeCards(d.cards);
    if (!nextCards.length) throw Object.assign(new Error("empty refresh payload"), { code: "cards_payload" });
    await wait(REDUCED ? 0 : 220);
    if (!SID || SID !== sidAtStart || finishing || ended) { sk.remove(); return false; }
    sk.remove();
    const refreshScreen = Number.isFinite(Number(d.screen_index)) ? Number(d.screen_index) : screensLoaded;
    /* 服务端在素材耗尽后会轮换旧卡。先移除当前 DOM 中同 ID 的旧实例，
       再把整批插到顶部；这样不会出现两个同 ID 卡片共用曝光状态。 */
    const refreshIds = new Set(nextCards.map(c => c.content_id));
    [...feed.querySelectorAll(".card")].forEach(el => {
      if (refreshIds.has(el.dataset.cid)) forgetCardElement(el);
    });
    const fragment = document.createDocumentFragment();
    nextCards.forEach((c, i) => fragment.appendChild(renderCard(c, i, refreshScreen, i * 45, "home", "refresh")));
    feed.insertBefore(fragment, feed.firstChild);
    /* 连续刷新不让 DOM 无限增长；旧卡的行为已经上报，移除只影响视觉列表。 */
    const allCards = [...feed.querySelectorAll(".card")];
    while (allCards.length > 48) {
      forgetCardElement(allCards.pop());
    }
    /* 刷新本身已经提供了新的首页内容；若此前某一屏加载失败，
       清掉哨兵的失败态，让用户仍能继续请求下一屏，而不是一直看到
       一个过期的“点击重试”提示。 */
    screenRetryPending = false;
    if (more) {
      more.onclick = null; more.onkeydown = null;
      more.removeAttribute("role"); more.removeAttribute("tabindex"); more.removeAttribute("aria-label");
      more.className = "more";
      more.innerHTML = '<span class="sp"></span>正在加载';
    }
    converged = typeof d.converged === "boolean" ? d.converged : converged;
    const anchor = d.personalized_from;
    emit("feed_refresh", null, { refresh_count: Number(d.refresh_count) || 0,
      screen_index: Number.isFinite(Number(d.screen_index)) ? Number(d.screen_index) : screensLoaded,
      /* 只记录服务端实际采用的锚点；客户端本地的 lastClickedCid 可能
         尚未入账，不能把“想采用”伪装成“已采用”。 */
      anchor_content_id: anchor && anchor.content_id || null });
    toast("内容已刷新");
    flush();
    if (homeView) homeView.scrollTop = 0;
    refreshSucceeded = true;
    return true;
  } catch (e) {
    sk.remove();
    if (!SID || SID !== sidAtStart || finishing || ended) return false;
    if (e.code === "session_gone") { handleSessionGone(); return false; }
    if (activeView === "home") showPageState(e.code === "events_pending" ? "connection" : "content",
      e.code === "events_pending" ? "网络连接不稳定" : "刷新暂时失败",
      "暂时无法刷新内容，请重试。", "重新刷新", async () => {
        hidePageState();
        return refreshFeed(logicalRequestId);
      }, e.code === "events_pending" ? "wifi-off" : "refresh-cw");
    return false;
  } finally {
    refreshLoading = false;
    feed.setAttribute("aria-busy", "false");
    updateRefreshControl();
    /* 观察器在 loading=true 时收到的“哨兵可见”回调会主动返回；
       等状态真正恢复后再观察一次，短内容列表也能自动继续加载。 */
    if (refreshSucceeded && more && activeView === "home" && !ended && SID) {
      requestAnimationFrame(() => {
        if (!refreshLoading && !screenLoading && SID && activeView === "home" && !ended) MORE_IO.observe(more);
      });
    }
  }
}

/* 「发现」：不按画像排序的固定信息流。
   固定 8 条、无限滚动关闭，不推进首页屏序，也不参与收敛判定；
   这里的点击仍会计入本次侧写。 */
let discoverLoaded = false, discoverLoading = false;
async function loadDiscover() {
  if (discoverLoaded || discoverLoading || !SID || finishing) return false;
  const sidAtStart = SID;
  discoverLoading = true;
  const box = document.getElementById("findFeed");
  box.setAttribute("aria-busy", "true");
  box.innerHTML = skeleton(4);
  try {
    const d = await apiJson(API + "/api/discover");
    if (!d || typeof d !== "object") throw Object.assign(new Error("bad discover payload"), { code: "cards_payload" });
    const nextCards = normalizeCards(d.cards);
    if (finishing || !SID || SID !== sidAtStart) { box.innerHTML = ""; return false; }
    box.innerHTML = "";
    const fragment = document.createDocumentFragment();
    nextCards.forEach((c, i) => fragment.appendChild(renderCard(c, i, -1, i * 45, "discover")));
    box.appendChild(fragment);
    discoverLoaded = true;
    Lucide.mount(box);
    return true;
  } catch (e) {
    if (!SID || SID !== sidAtStart || finishing) { box.innerHTML = ""; return false; }
    box.innerHTML = '<p class="fnote">暂时无法加载发现内容。</p>';
    if (activeView === "find" && SID && !finishing) {
      showPageState("content", "发现暂时不可用", "请稍后重试。", "重新加载", async () => { hidePageState(); await loadDiscover(); }, "triangle-alert");
    }
    return false;
  } finally {
    discoverLoading = false; box.removeAttribute("aria-busy");
  }
}

/* 结束卡：达到结束条件后停在信息流末尾，由观众主动进入结果页。 */
function showEndCard() {
  if (ended) return;
  ended = true; screenRetryPending = false; MORE_IO.disconnect();
  updateRefreshControl();
  const m = document.getElementById("more");
  /* 加载失败时哨兵可能挂过重试 onclick；结束卡接管这个节点时必须清掉旧处理器，
     否则点击卡片空白处会偷偷再次请求上一屏。 */
  m.onclick = null; m.onkeydown = null; m.removeAttribute("role"); m.removeAttribute("tabindex"); m.removeAttribute("aria-label");
  m.className = "endcard";
  const remaining = Math.max(0, MAX_SCREENS - screensLoaded);
  m.innerHTML = '<div class="k">临时代号 · ' + esc(CODENAME) + "</div>" +
    "<h4>可以查看结果了</h4>" +
    "<p>你的兴趣画像已经准备好。</p>" +
    '<button type="button" class="go" id="goResult">查看我的结果' + I("arrow-right", 18) + "</button>" +
    (remaining ? '<button type="button" class="again">继续浏览</button>' : "");
  document.getElementById("goResult").onclick = () => finish();
  const again = m.querySelector(".again");
  if (again) again.onclick = () => {
    extensionTarget = Math.min(MAX_SCREENS, screensLoaded + 2);
    ended = false; screenRetryPending = false; m.onclick = null; m.onkeydown = null; m.removeAttribute("role"); m.removeAttribute("tabindex"); m.removeAttribute("aria-label"); m.className = "more"; m.innerHTML = '<span class="sp"></span>正在加载';
    updateRefreshControl();
    MORE_IO.observe(m);
  };
}

/* 曝光判定：视口 ≥50% 且 ≥500ms 才算曝光；<1.5s 划走记 skip（§6.4） */
const timers = {};
const IO = new IntersectionObserver(es => {
  es.forEach(e => {
    const cid = e.target.dataset.cid;
    const key = e.target.dataset.key || ("home:" + cid);
    const info = cards[key] || cards["home:" + cid] || {};
    const owner = e.target.closest && e.target.closest(".app-view");
    /* 切到「我的」/「发现」，或打开正文、状态页时，旧视图从布局树暂时离开。
       这不是观众把卡片快速划走，不能误记成 skip。 */
    if (document.body.classList.contains("modal-lock") || (owner && owner.getAttribute("aria-hidden") === "true")) {
      if (timers[key]) { clearTimeout(timers[key]); delete timers[key]; }
      return;
    }
    if (e.isIntersecting && e.intersectionRatio >= 0.5) {
      if (!impressed[key] && !timers[key]) timers[key] = setTimeout(() => {
        delete timers[key];
        const currentOwner = e.target.closest && e.target.closest(".app-view");
        /* 计时器到点时重新确认卡片仍在当前可见页面；切页、打开正文或结算
           发生在这 500ms 内，都不应被记成一次曝光。 */
        if (!SID || finishing || document.body.classList.contains("modal-lock") ||
            !document.documentElement.contains(e.target) || (currentOwner && currentOwner.getAttribute("aria-hidden") === "true")) return;
        impressed[key] = Date.now();
        emit("content_impression", cid, { position: info.pos, screen_index: info.screen >= 0 ? info.screen : null, feed: info.source || "home" });
      }, 500);
    } else {
      if (timers[key]) { clearTimeout(timers[key]); delete timers[key]; }
      if (impressed[key] && !opened[cid] && !impressed[key + "_s"] && Date.now() - impressed[key] < 1500) {
        impressed[key + "_s"] = 1;
        emit("content_skip", cid, { position: info.pos, screen_index: info.screen >= 0 ? info.screen : null, feed: info.source || "home" });
      }
    }
  });
}, { threshold: [0, 0.5, 1] });

/* 翻屏由底部哨兵触发（安全区 / 地址栏收放 / 橡皮筋回弹都会让滚动数值判断失灵） */
const MORE_IO = new IntersectionObserver(async es => {
  if (!es[0].isIntersecting || busy || screenLoading || refreshLoading || !SID || finishing || ended || screenRetryPending || document.body.classList.contains("modal-lock")) return;
  if (activeView !== "home") return;   // 离开首页时不推进首页屏序
  busy = true;
  try {
    const enough = screensLoaded >= MAX_SCREENS ||
      (extensionTarget !== null ? screensLoaded >= extensionTarget
                                : (converged && screensLoaded >= MIN_SCREENS));
    if (enough) { extensionTarget = null; showEndCard(); } else await loadScreen();
  } finally {
    busy = false;
  }
}, { threshold: .6 });

/* ─────────────────────────── 阅读页 ─────────────────────────── */
const reader = document.getElementById("reader"), rprog = document.getElementById("rprog");
async function openContent(cid, suppliedInfo) {
  /* 防止双击卡片在阅读层尚未完成过渡时重复记一笔点击。 */
  if (curCid !== null || !SID || finishing) return;
  const info = suppliedInfo || cards["home:" + cid] || Object.values(cards).find(x => x.c && x.c.content_id === cid);
  if (!info) return;
  const sidAtStart = SID;
  curCid = cid; curInfo = info; curPos = info.pos; curOpenAt = 0; curMaxScroll = 0; curContentReady = false;
  if (timers[info.key]) { clearTimeout(timers[info.key]); delete timers[info.key]; }
  if (info.source === "discover") { discoverClickSet.add(cid); discoverClicks = discoverClickSet.size; window.__discoverClicks = discoverClicks; }
  const clickEvent = emit(readSet[cid] ? "content_review" : "content_click", cid, { position: info.pos, screen_index: info.screen >= 0 ? info.screen : null, is_return: !!readSet[cid], feed: info.source || "home" });
  lastClickedCid = cid;
  lastClickedSeq = clickEvent && clickEvent.seq != null ? clickEvent.seq : null;
  const clickSeq = lastClickedSeq;
  opened[cid] = 1;
  const c = info.c, h = hue(c.cover_theme);
  document.getElementById("rtop").style.setProperty("--h", h);
  document.getElementById("rwrap").style.setProperty("--h", h);
  document.getElementById("rchip").textContent = c.domain_cn;
  document.getElementById("rtitle").textContent = c.title;
  document.getElementById("rauthor").textContent = publisher(c);
  document.getElementById("rtime").textContent = (1 + hash(cid) % 59) + " 分钟前";
  document.getElementById("rkick").textContent = (c.body_len === "L" ? "深度长文" : "速览") + " · 阅读约 " +
    (c.body_len === "L" ? "1 分钟" : "30 秒");
  document.getElementById("rbody").innerHTML = skeleton(0) + '<span style="color:var(--pdim)">加载中</span>';
  rprog.style.transform = "scaleX(0)";
  reader.scrollTop = 0; modalOpen(reader, document.getElementById("rback")); flush();
  try {
    /* 正文 GET 先到时，服务端可用 open=1 登记短期推荐锚点；点击事件
       仍会随后按正常规则入账，GET 本身不会重复计分。 */
    const openQuery = "&open=1&position=" + encodeURIComponent(String(info.pos == null ? 0 : info.pos)) +
      "&feed=" + encodeURIComponent(info.source || "home") + "&open_ts=" + encodeURIComponent(String(Date.now())) +
      (clickSeq != null ? "&open_seq=" + encodeURIComponent(String(clickSeq)) : "");
    const d = await apiJson(API + "/api/content?sid=" + encodeURIComponent(sidAtStart) + "&cid=" + encodeURIComponent(cid) + openQuery);
    if (curCid !== cid || SID !== sidAtStart) return;
    if (!d || typeof d !== "object" || typeof d.body !== "string") throw Object.assign(new Error("bad content payload"), { code: "content_payload" });
    document.getElementById("rbody").textContent = d.body;
    reader.scrollTop = 0; curMaxScroll = 0; curOpenAt = Date.now(); curContentReady = true;
  } catch (e) {
    if (curCid !== cid || SID !== sidAtStart || finishing) return;
    if (e.code === "session_gone") { closeReader(); handleSessionGone(); return; }
    document.getElementById("rbody").textContent = "正文暂时无法加载。";
    showPageState("content", "正文加载失败", "这篇内容暂时无法打开，请返回后重试。", "返回信息流", () => { hidePageState(); closeReader(); }, "triangle-alert");
  }
}
reader.onscroll = () => {
  const h = reader.scrollHeight - reader.clientHeight;
  const p = h > 0 ? reader.scrollTop / h : 1;
  if (p > curMaxScroll) curMaxScroll = p;
  rprog.style.transform = "scaleX(" + Math.min(1, Math.max(0, p)) + ")";
};
document.getElementById("rback").onclick = closeReader;
document.getElementById("rback2").onclick = closeReader;
function closeReader() {
  /* 即使正文请求在打开前就被中断，也要允许调用方安全地收起阅读层并释放锁。 */
  if (curCid === null) { modalClose(reader); return; }
  if (curContentReady && curOpenAt) {
    const dwell = Date.now() - curOpenAt;
    const depth = (reader.scrollHeight <= reader.clientHeight) ? 1 : curMaxScroll;
    emit("content_dwell", curCid, { dwell_ms: dwell, scroll_depth: +depth.toFixed(2), position: curPos,
      screen_index: curInfo && curInfo.screen >= 0 ? curInfo.screen : null, feed: curInfo && curInfo.source || "home" });
    readSet[curCid] = 1;
    document.querySelectorAll('[data-cid="' + curCid + '"]').forEach(el => el.classList.add("read"));
  }
  curCid = null; curInfo = null; curOpenAt = 0; curContentReady = false; modalClose(reader); flush();
}

/* ─────────────────────────── 底部导航 ───────────────────────────
   三个页面互斥显示，各自保留自己的 scrollTop；切换时首页 DOM 会真正离开可视层，
   不再出现「在遮罩上滑动却看见后面信息流」的穿透感。 */
const VIEW_SCROLL = new WeakMap();
function setViewVisible(view, on) {
  if (!view) return;
  if (!on) VIEW_SCROLL.set(view, view.scrollTop);
  view.classList.toggle("on", !!on);
  /* hidden + inert 是硬隔离；class/aria 只负责样式和读屏状态。这样即使浏览器
     在滚动回弹期间重排，也不会把另一页当成可见的底层内容。 */
  view.hidden = !on;
  view.setAttribute("aria-hidden", String(!on));
  if (on) view.removeAttribute("inert"); else view.setAttribute("inert", "");
  try { view.inert = !on; } catch (_) {}
  if (on && VIEW_SCROLL.has(view)) {
    const top = VIEW_SCROLL.get(view);
    requestAnimationFrame(() => { if (!view.hidden) view.scrollTop = top; });
  }
}
/* 页面脚本加载后再同步一次初始状态，兼容缓存页面/浏览器恢复表单时留下的旧 class。
   这里不改当前滚动位置，只确保只有一个 view 真实存在于布局树中。 */
function syncInitialViews() {
  const views = [...document.querySelectorAll("#app .app-view")];
  const initial = views.find(v => v.dataset.view === "home") || views[0];
  views.forEach(v => setViewVisible(v, v === initial));
}
syncInitialViews();
function switchView(k) {
  if (!["home", "find", "mine"].includes(k)) return;
  if (document.body.classList.contains("modal-lock")) return;
  const view = document.querySelector('#app .app-view[data-view="' + k + '"]');
  if (!view || finishing) return;
  if (k !== "home") { pullGesture = null; resetPullVisual(); }
  activeView = k;
  updateRefreshControl();
  document.querySelectorAll("#app .app-view").forEach(v => {
    setViewVisible(v, v === view);
  });
  [...document.getElementById("tabbar").children].forEach(x => {
    const on = x.dataset.k === k;
    x.classList.toggle("on", on);
    if (on) x.setAttribute("aria-current", "page"); else x.removeAttribute("aria-current");
  });
  const more = document.getElementById("more");
  MORE_IO.unobserve(more);
  if (k === "home" && !finishing && !ended && !screenRetryPending) MORE_IO.observe(more);
  if (k === "home" && !finishing && !ended && extensionTarget === null &&
      converged && screensLoaded >= MIN_SCREENS && !screenLoading) showEndCard();
  if (k === "find") loadDiscover();
  Lucide.mount(view);
}
document.getElementById("tabbar").onclick = e => {
  const d = e.target.closest("[data-k]"); if (d) switchView(d.dataset.k);
};
document.getElementById("findBack").onclick = () => switchView("home");

/* ─────────────────────────── 结算：幕 + 启动序列 + 结果页 ─────────────────────────── */
async function finish() {
  if (finishing || !SID) return;
  finishing = true; MORE_IO.disconnect(); clearInterval(pollTimer); pauseSessionDeadline();
  updateRefreshControl();
  pullGesture = null; resetPullVisual();
  extensionTarget = null;
  const finishButton = document.getElementById("goResult");
  if (finishButton) finishButton.disabled = true;
  document.querySelectorAll(".sheet.on").forEach(closeSheet);
  hidePageState();
  /* 超时结算可能发生在正文阅读层打开期间；先正常结算停留并收起阅读层，
     避免它以更低层级残留在结果页下面。 */
  if (reader && reader.classList.contains("on")) closeReader();
  const appDuringFinish = document.getElementById("app");
  appDuringFinish.setAttribute("inert", ""); appDuringFinish.setAttribute("aria-busy", "true");
  try { appDuringFinish.inert = true; } catch (_) {}
  if (!sessionEndEmitted) { emit("session_end"); sessionEndEmitted = true; }
  try {
    const flushed = await flush();
    if (!SID) throw Object.assign(new Error("session_gone"), { code: "session_gone" });
    if (!flushed || Q.length) throw Object.assign(new Error("events_pending"), { code: "events_pending" });
    const p = await apiJson(API + "/api/result?sid=" + encodeURIComponent(SID));
    document.body.classList.add("ink");
    document.querySelector('meta[name=theme-color]').setAttribute("content", PAL.ink);
    await playVeil(p);
    document.getElementById("app").classList.add("hide");
    document.getElementById("app").setAttribute("aria-hidden", "true");
    document.querySelectorAll("#app .app-view").forEach(v => setViewVisible(v, false));
    activeView = "home";
    const result = document.getElementById("result");
    if (result) result.scrollTop = 0;
    modalOpen(result, document.querySelector("#result .result-anchors button"));
    renderResult(p);
    appDuringFinish.removeAttribute("aria-busy");
    clearSessionDeadline();
    FX.start();
    const veil = document.getElementById("veil");
    veil.classList.add("out");
    setTimeout(() => veil.classList.remove("on", "out"), 700);
  } catch (e) {
    finishing = false;
    updateRefreshControl();
    if (finishButton) finishButton.disabled = false;
    REV.disconnect(); teardownResultNavigation(); resultVisual = null;
    clearTimeout(resultResizeTimer); resultResizeTimer = 0;
    /* 结果请求或渲染失败时，恢复到可继续浏览的首页，避免页面停在不可操作状态。 */
    const result = document.getElementById("result");
    if (result && result.getAttribute("aria-hidden") === "false") modalClose(result);
    const app = document.getElementById("app");
    app.classList.remove("hide"); app.setAttribute("aria-hidden", "false"); app.removeAttribute("inert"); app.removeAttribute("aria-busy");
    try { app.inert = false; } catch (_) {}
    document.querySelectorAll("#app .app-view").forEach(v => setViewVisible(v, v.dataset.view === "home"));
    activeView = "home";
    document.body.classList.remove("ink");
    document.getElementById("veil").classList.remove("on", "out");
    if (SID && activeView === "home" && !ended) MORE_IO.observe(document.getElementById("more"));
    clearInterval(pollTimer); pollTimer = SID ? setInterval(pollProfile, 5000) : 0;
    if (SID && sessionDeadlineAt > Date.now()) armSessionDeadline(false);
    if (e.code === "session_gone") handleSessionGone();
    else if (e.code === "events_pending") showPageState("connection", "网络连接不稳定", "暂时无法生成结果，请检查网络后重试。", "重新生成", finish, "wifi-off");
    else showPageState("result", "结果生成失败", "结果暂时无法生成，请稍后重试。", "重新生成", finish, "refresh-cw");
  }
}
async function playVeil(p) {
  p = p && typeof p === "object" ? p : {};
  const veil = document.getElementById("veil"), boot = document.getElementById("boot");
  const count = value => Math.max(0, Math.round(Number.isFinite(Number(value)) ? Number(value) : 0));
  const lines = [
    ["临时代号", p.codename || "未生成", ""],
    ["浏览记录", "点击 " + count(p.click_count) + " 次 · 略过 " + count(p.skip_count) + " 次", ""],
    ["兴趣画像", "正在生成", "hl"],
  ];
  boot.innerHTML = lines.map(l => '<div class="ln"><span class="k">' + esc(l[0]) + '</span><span class="v ' + l[2] + '">' + esc(l[1]) + "</span></div>").join("") +
    '<div class="bar"><i></i></div>';
  veil.classList.add("on");
  if (REDUCED) return;
  await wait(760);
  const ls = [...boot.querySelectorAll(".ln")];
  for (const l of ls) { l.classList.add("on"); await wait(210); }
  const bar = boot.querySelector(".bar"); bar.classList.add("on"); await wait(60); bar.classList.add("go");
  await wait(1150);
}

/* 超时兜底由 armSessionDeadline() 在会话建立后启动（§9.5）。 */
