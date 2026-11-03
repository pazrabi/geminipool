(function () {
"use strict";
var KEY = "gemini-pool-token";
var token = null, filter = "active", showAllPipes = false, showAllEvents = false, openTasks = {}, taskDetail = {}, quotaBusy = {}, quotaFresh = {}, data = null, timer = null, lastSig = "";
var modelLists = {}, modelLoading = {}, modelBusy = {}, modelErr = {}, clockOffset = 0, recovering = {}, cancelling = {}, addOpen = false, addErr = "";

function store(v) { try { if (v === null) localStorage.removeItem(KEY); else localStorage.setItem(KEY, v); } catch (e) {} }
function load() { try { return localStorage.getItem(KEY); } catch (e) { return null; } }

// All text from agents is untrusted: build DOM with textContent only, never innerHTML.
function el(tag, cls, text) {
  var n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = text;
  return n;
}
function pill(cls, text) { return el("span", "pill " + cls, text); }
function pre(text) { return el("pre", "", text); }
function linkified(text) {
  var box = el("pre", "");
  String(text).split(/(https:\/\/[^\s<>"')]+)/g).forEach(function (part, i) {
    if (i % 2 === 1) {
      var a = el("a", "", part); a.href = part; a.target = "_blank"; a.rel = "noopener noreferrer"; box.appendChild(a);
    } else if (part) box.appendChild(document.createTextNode(part));
  });
  return box;
}

// agy /usage prints tab-separated rows: <model group> <limit name> <percent> <ISO reset time>.
var LIMITS = { "Weekly Limit Remaining": "Неделя", "Five Hour Limit Remaining": "5 часов" };
function quotaView(text) {
  var rows = String(text || "").split("\n").map(function (l) { return l.split("\t"); })
    .filter(function (r) { return r.length === 4 && /^\d+%$/.test(r[2].trim()) && !isNaN(Date.parse(r[3])); })
    .filter(function (r) { return /^gemini/i.test(r[0]); });  // only Gemini limits are shown
  if (!rows.length) return null;
  var box = el("div", "quota");
  rows.forEach(function (r) {
    var pct = parseInt(r[2], 10), reset = Date.parse(r[3]) / 1000;
    var line = el("div", "qrow");
    line.appendChild(el("span", "qname", LIMITS[r[1]] || r[1]));
    var bar = el("span", "bar"), fill = el("i", pct >= 50 ? "hi" : pct >= 20 ? "mid" : "lo");
    fill.style.width = pct + "%"; bar.appendChild(fill); line.appendChild(bar);
    line.appendChild(el("b", "", pct + "%"));
    line.appendChild(el("span", "muted", "сброс " + dateTime(reset)));
    box.appendChild(line);
  });
  return box;
}

function pad(n) { return n < 10 ? "0" + n : "" + n; }
function clock(ts) { var d = new Date(ts * 1000); return pad(d.getHours()) + ":" + pad(d.getMinutes()); }
function dateTime(ts) { var d = new Date(ts * 1000); return pad(d.getDate()) + "." + pad(d.getMonth() + 1) + " " + clock(ts); }
function span(sec) {
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return sec + " с";
  if (sec < 3600) return Math.floor(sec / 60) + " мин " + (sec % 60) + " с";
  return Math.floor(sec / 3600) + " ч " + Math.floor((sec % 3600) / 60) + " мин";
}
function ago(ts, now) { return span(now - ts) + " назад"; }

// Live timers: the server's clock is used, so a wrong clock on this computer cannot skew them.
function serverNow() { return Date.now() / 1000 + clockOffset; }
function liveSpan(since, prefix) {
  var s = el("span", "live");
  s.dataset.since = String(since); s.dataset.prefix = prefix;
  s.textContent = prefix + span(serverNow() - since);
  return s;
}
function tick() {
  var nodes = document.querySelectorAll("[data-since]"), now = serverNow();
  for (var i = 0; i < nodes.length; i++) {
    nodes[i].textContent = nodes[i].dataset.prefix + span(now - Number(nodes[i].dataset.since));
  }
}
setInterval(tick, 1000);

function api(path, opts) {
  opts = opts || {};
  opts.headers = { "Authorization": "Bearer " + token };
  return fetch(path, opts).then(function (r) {
    if (r.status === 401) { var e = new Error("auth"); e.auth = true; throw e; }
    if (!r.ok) return r.json().catch(function () { return {}; }).then(function (j) { throw new Error(j.error || ("HTTP " + r.status)); });
    return r.json();
  });
}

// ---- screens -----------------------------------------------------------
function showLogin(msg) {
  document.getElementById("app").hidden = true;
  document.getElementById("login").hidden = false;
  document.getElementById("login-err").textContent = msg || "";
  clearTimeout(timer);
}
function showApp() {
  document.getElementById("login").hidden = true;
  document.getElementById("app").hidden = false;
}

// ---- rendering ---------------------------------------------------------
var STATUS = { queued: "В очереди", running: "Выполняется", done: "Готово", failed: "Ошибка", needs_lead: "Нужен тимлид" };
var PIPE_STAGE = { develop: "Разработка", awaiting_review: "Ждёт ревью", review: "Идёт ревью", rework: "Доработка",
  approved: "Одобрено", needs_lead: "Нужен тимлид", failed: "Ошибка" };
var PIPE_CLASS = { develop: "s-running", awaiting_review: "s-queued", review: "s-running", rework: "s-running",
  approved: "s-done", needs_lead: "s-needs", failed: "s-failed" };
var REASON = { max_rounds: "ревьюер не одобрил за отведённые раунды", review_no_verdict: "ревьюер не дал вердикт",
  review_failed: "ревью не удалось выполнить", no_changes: "разработчик ничего не изменил", violation: "нарушены правила (лишние файлы)",
  internal: "внутренняя ошибка пула", quota: "лимит исчерпан", auth: "проблема со входом агента", network: "сеть",
  timeout: "таймаут", agent_error: "ошибка агента", cancelled: "отменено владельцем" };
function shortModel(m) { return m ? String(m).replace(/^gemini-/, "") : ""; }
function stageLabel(t) {
  if (!t.stage) return "";
  if (t.pipeline_id === t.id) return "конвейер";
  return t.stage === "review" ? "ревью" : t.stage === "rework" ? "доработка " + t.round : "";
}
var KIND = {
  "quota limit": "Лимит", "account auth error": "Нет входа", "network error": "Сеть", "timeout": "Таймаут",
  "agent_error": "Ошибка агента", "violation": "Нарушение правил", "push_error": "Ошибка пуша",
  "internal": "Внутренняя ошибка", "quota_check_failed": "Проверка лимита", "disk_low": "Мало места на диске"
};

function accountState(a, now) {
  if (!a.enabled) return { cls: "s-idle", text: "Выключен" };
  if (a.logged_in === false) return { cls: "s-bad", text: "Нужен вход" };
  if (a.state === "auth_error") {
    var refused = /not eligible|Verify your account|Eligibility/i.test(a.last_error || "");
    return { cls: "s-bad", text: refused ? "Нужно подтвердить аккаунт в Google" : "Нужен вход" };
  }
  if (a.paused_until && a.paused_until > now) return { cls: "s-wait", text: "Лимит, пауза ещё " + span(a.paused_until - now) };
  if (a.busy_task) return { cls: "s-run", text: "Выполняет задачу" };
  return { cls: "s-ok", text: "Свободен" };
}

function renderCounters() {
  var box = document.getElementById("counters"); box.textContent = "";
  [["queued", "В очереди"], ["running", "Выполняется"], ["done", "Готово"], ["failed", "Ошибки"]].forEach(function (p) {
    var c = el("div", "counter"); c.appendChild(el("b", "", String(data.counts[p[0]] || 0))); c.appendChild(el("span", "muted", p[1])); box.appendChild(c);
  });
}

function modelRow(a) {
  var row = el("div", "row"), list = modelLists[a.id];
  row.appendChild(el("span", "muted", "Модель: "));
  if (!list && !modelLoading[a.id]) loadModels(a.id);
  var sel = el("select", ""), def = el("option", "", "по умолчанию (выбирает agy)");
  def.value = ""; if (!a.model) def.selected = true; sel.appendChild(def);
  var seen = false;
  (list || []).forEach(function (m) {
    var o = el("option", "", m.name); o.value = m.id; o.title = m.id;
    if (m.id === a.model) { o.selected = true; seen = true; }
    sel.appendChild(o);
  });
  if (a.model && !seen) { var extra = el("option", "", a.model); extra.value = a.model; extra.selected = true; sel.appendChild(extra); }
  sel.disabled = !!modelBusy[a.id];
  sel.onchange = function () { setModel(a.id, sel.value); };
  row.appendChild(sel);
  if (!list) row.appendChild(el("span", "muted", " загрузка списка…"));
  if (modelErr[a.id]) row.appendChild(el("div", "err", modelErr[a.id]));
  return row;
}

function loadModels(id) {
  modelLoading[id] = true;
  api("/api/accounts/" + encodeURIComponent(id) + "/models").then(function (r) {
    modelLists[id] = !r.ok ? [] : r.text.split("\n").map(function (l) { return l.split("\t"); })
      .filter(function (p) { return p.length === 2 && p[0].trim(); })
      .map(function (p) { return { id: p[0].trim(), name: p[1].trim() }; });
  }).catch(function () { modelLists[id] = []; })
    .then(function () { modelLoading[id] = false; if (data) renderAccounts(); });
}

function setModel(id, value) {
  modelBusy[id] = true; modelErr[id] = ""; renderAccounts();
  api("/api/accounts/" + encodeURIComponent(id) + "/model", { method: "POST", body: JSON.stringify({ model: value || null }) })
    .catch(function (e) { modelErr[id] = "Не удалось сменить модель: " + e.message; })
    .then(function () { modelBusy[id] = false; lastSig = ""; refresh(); });
}

function renderAccounts() {
  var box = document.getElementById("accounts"); box.textContent = "";
  if (!data.accounts.length) { box.appendChild(el("div", "empty", "Агентов нет")); box.appendChild(addAgentCard()); return; }
  data.accounts.forEach(function (a) {
    var st = accountState(a, data.now), card = el("div", "card");
    var top = el("div", "top");
    top.appendChild(el("span", "name", a.id)); top.appendChild(pill("s-idle", a.role)); top.appendChild(pill(st.cls, st.text));
    card.appendChild(top);

    var info = el("div", "row muted");
    if (a.busy_task) {
      var cur = data.tasks.filter(function (t) { return t.id === a.busy_task; })[0];
      info.appendChild(document.createTextNode("Задача " + a.busy_task + " · "));
      info.appendChild(liveSpan(cur && (cur.started_at || cur.updated_at) || a.last_used, "идёт "));
    } else {
      info.textContent = a.last_used ? "Последняя работа: " + ago(a.last_used, data.now) : "Ещё не работал";
    }
    card.appendChild(info);
    card.appendChild(modelRow(a));
    if (a.egress) {
      var net = a.egress.mode === "own_ipv6" ? "свой IPv6-адрес (порт " + a.egress.port + ")"
        : a.egress.mode === "external_proxy" ? "внешний прокси " + a.egress.host : "общий IPv4 сервера";
      card.appendChild(el("div", "row muted", "Выход в сеть: " + net));
    }

    if (a.logged_in === false) {
      card.appendChild(el("div", "row muted", "Агент не войдёт в задачи, пока не выполнен вход в Google. Выполните на вашем Mac:"));
      card.appendChild(pre("~/projects/gemini-pool/scripts/add_account.sh " + a.id));
    }

    if (a.state === "auth_error" && /not eligible|Verify your account|Eligibility/i.test(a.last_error || "")) {
      card.appendChild(el("div", "row muted", "Google не допускает этот аккаунт, пока его не подтвердят. Откройте ссылку из результата проверки лимита ниже, подтвердите аккаунт и нажмите «Проверить лимит» ещё раз: если проверка пройдёт, агент заработает сам."));
    }
    if (a.last_error && (a.state !== "ok")) {
      card.appendChild(el("div", "row muted", "Последняя ошибка:")); card.appendChild(pre(a.last_error));
    }

    var q = el("div", "row");
    var btn = el("button", "", quotaBusy[a.id] ? "Проверяю…" : "Проверить лимит");
    btn.disabled = !!quotaBusy[a.id];
    btn.onclick = function () { checkQuota(a.id); };
    q.appendChild(btn);
    var res = quotaFresh[a.id] || (a.quota_checked_at ? { checked_at: a.quota_checked_at, ok: !!a.quota_ok, text: a.quota_text } : null);
    if (res) {
      q.appendChild(el("span", "muted", "  проверено " + dateTime(res.checked_at) + (res.ok ? "" : " — не удалось")));
      q.appendChild((res.ok && quotaView(res.text)) || linkified(res.text || "(пустой ответ)"));
    }
    card.appendChild(q);

    var ops = el("div", "row ops");
    var toggle = el("button", "", a.enabled ? "Выключить" : "Включить");
    toggle.onclick = function () { setEnabled(a.id, !a.enabled); };
    ops.appendChild(toggle);
    var del = el("button", "danger", "Удалить");
    del.disabled = !!a.busy_task;
    del.title = a.busy_task ? "Агент сейчас работает" : "Удалить агента вместе с его входом в Google";
    del.onclick = function () {
      if (confirm("Удалить агента " + a.id + "? Его вход в Google будет стёрт, для возврата нужно войти заново.")) removeAccount(a.id);
    };
    ops.appendChild(del);
    card.appendChild(ops);
    box.appendChild(card);
  });
  box.appendChild(addAgentCard());
}

function nextAccountId() {
  var taken = {}; data.accounts.forEach(function (a) { taken[a.id] = true; });
  for (var n = 1; n < 100; n++) if (!taken["acc" + n]) return "acc" + n;
  return "";
}

function addAgentCard() {
  var card = el("div", "card add");
  if (!addOpen) {
    var open = el("button", "primary", "+ Добавить агента");
    open.onclick = function () { addOpen = true; addErr = ""; renderAccounts(); };
    card.appendChild(open);
    card.appendChild(el("div", "row muted", "Новый Google-аккаунт для пула. После добавления нужно один раз войти в него (команда появится в карточке)."));
    return card;
  }
  card.appendChild(el("div", "name", "Новый агент"));
  var idInput = el("input", ""); idInput.value = nextAccountId(); idInput.maxLength = 16; idInput.setAttribute("aria-label", "Имя агента");
  var role = el("select", "");
  [["developer", "developer: пишет код"], ["reviewer", "reviewer: проверяет"], ["debugger", "debugger: чинит ошибки"]].forEach(function (r) {
    var o = el("option", "", r[1]); o.value = r[0]; role.appendChild(o);
  });
  var form = el("div", "row");
  form.appendChild(idInput); form.appendChild(document.createTextNode(" ")); form.appendChild(role);
  card.appendChild(form);
  if (addErr) card.appendChild(el("div", "err", addErr));
  var go = el("button", "primary", "Добавить");
  go.onclick = function () { addAccount(idInput.value.trim(), role.value); };
  var cancel = el("button", "", "Отмена");
  cancel.onclick = function () { addOpen = false; addErr = ""; renderAccounts(); };
  var btns = el("div", "row"); btns.appendChild(go); btns.appendChild(document.createTextNode(" ")); btns.appendChild(cancel);
  card.appendChild(btns);
  return card;
}

function accountCall(path, method, obj) {
  return api(path, { method: method, body: obj ? JSON.stringify(obj) : undefined });
}
function addAccount(id, role) {
  accountCall("/api/accounts", "POST", { id: id, role: role })
    .then(function () { addOpen = false; addErr = ""; })
    .catch(function (e) { if (e.auth) return showLogin("Токен не подошёл"); addErr = e.message; })
    .then(function () { lastSig = ""; refresh(); });
}
function setEnabled(id, enabled) {
  accountCall("/api/accounts/" + encodeURIComponent(id) + "/enabled", "POST", { enabled: enabled })
    .catch(function (e) { if (e.auth) showLogin("Токен не подошёл"); else alert("Не удалось: " + e.message); })
    .then(function () { lastSig = ""; refresh(); });
}
function removeAccount(id) {
  accountCall("/api/accounts/" + encodeURIComponent(id), "DELETE")
    .catch(function (e) { if (e.auth) showLogin("Токен не подошёл"); else alert("Не удалось: " + e.message); })
    .then(function () { lastSig = ""; refresh(); });
}

function githubUrl(repo, branch) {
  var m = /^https:\/\/github\.com\/([^/]+)\/([^/]+?)(?:\.git)?\/?$/.exec(repo || "");
  return m && branch ? "https://github.com/" + m[1] + "/" + m[2] + "/tree/" + branch : null;
}

function kv(box, key, value) {
  box.appendChild(el("div", "", key));
  var v = el("div", ""); if (value instanceof Node) v.appendChild(value); else v.textContent = value; box.appendChild(v);
}

function renderDetail(t, d) {
  var body = el("div", "body"), r = (d && d.report) || {}, grid = el("div", "kv");
  kv(grid, "Репозиторий", t.repo);
  kv(grid, "Ветка", t.branch ? (function () {
    var url = githubUrl(t.repo, t.branch), s = el("span", "mono");
    if (url && r.pushed) { var a = el("a", "mono", t.branch); a.href = url; a.target = "_blank"; a.rel = "noopener noreferrer"; return a; }
    s.textContent = t.branch + (r.pushed ? "" : " (на сервере, в GitHub не отправлена)"); return s;
  })() : "—");
  kv(grid, "Попыток", String(t.attempts));
  if (t.status === "running") kv(grid, "Время работы", liveSpan(t.started_at || t.updated_at, ""));
  else if (t.started_at) kv(grid, "Время работы", span(t.updated_at - t.started_at));
  if (t.created_at) kv(grid, "Поставлена", dateTime(t.created_at));
  if ("model" in r) kv(grid, "Модель", r.model || "по умолчанию (выбирает agy)");
  if (r.rounds) kv(grid, "Раундов ревью", r.rounds + (r.max_rounds ? " из " + r.max_rounds : ""));
  if (r.verdict) kv(grid, "Вердикт ревьюера", r.verdict === "APPROVE" ? "одобрено" : "есть замечания");
  if (r.reviewer_model) kv(grid, "Модель ревьюера", r.reviewer_model + (r.reviewer_account ? " (" + r.reviewer_account + ")" : ""));
  if (t.status === "needs_lead" && r.reason) kv(grid, "Нужен тимлид", REASON[r.reason] || r.reason);
  else if (t.model) kv(grid, "Модель (задана)", t.model);
  if (d) kv(grid, "Разрешённые файлы", (d.allowed_files || []).join(", "));
  if (d && d.test_cmd) kv(grid, "Команда тестов", d.test_cmd);
  if (r.tests) kv(grid, "Тесты", r.tests.passed ? "пройдены" : "не пройдены");
  if (r.stats) kv(grid, "Изменения", r.stats);
  if (r.changed_files && r.changed_files.length) kv(grid, "Файлы", r.changed_files.join(", "));
  if (r.violations && r.violations.length) kv(grid, "Нарушения", r.violations.join("; "));
  if (r.push_error) kv(grid, "Ошибка пуша", r.push_error);
  if (r.error) kv(grid, "Ошибка", r.error);
  if (r.last_error) kv(grid, "Последний сбой", r.last_error);
  body.appendChild(grid);
  if (d) body.appendChild(el("div", "row muted", "Задача: ")), body.lastChild.appendChild(pre(d.goal));
  if (r.summary) { body.appendChild(el("div", "row muted", "Ответ агента:")); body.appendChild(pre(r.summary)); }
  if (r.review_summary) { body.appendChild(el("div", "row muted", "Итог ревьюера:")); body.appendChild(pre(r.review_summary)); }
  if (r.tests && r.tests.tail) { body.appendChild(el("div", "row muted", "Вывод тестов:")); body.appendChild(pre(r.tests.tail)); }
  if (r.tail) { body.appendChild(el("div", "row muted", "Вывод агента при ошибке:")); body.appendChild(pre(r.tail)); }
  if (t.status === "failed" && /^internal/.test(r.error || "")) {
    var rec = el("div", "row");
    var rb = el("button", "", recovering[t.id] ? "Выполняется…" : "Повторить завершение");
    rb.disabled = !!recovering[t.id];
    rb.title = "Пул заново проверит файлы, прогонит тесты, закоммитит и запушит. Агент не запускается, лимит не тратится.";
    rb.onclick = function (ev) { ev.stopPropagation(); recoverTask(t.id); };
    rec.appendChild(rb);
    rec.appendChild(el("span", "muted", "  работа агента сохранена на сервере; лимит не тратится"));
    body.appendChild(rec);
  }
  if (t.status === "queued") {
    var cancelRow = el("div", "row");
    var cb = el("button", "danger", cancelling[t.id] ? "Отменяю…" : "Отменить задачу");
    cb.disabled = !!cancelling[t.id];
    cb.onclick = function (ev) {
      ev.stopPropagation();
      if (confirm("Отменить задачу " + t.id + "? Она ещё не началась, ничего не потеряется.")) cancelTask(t.id);
    };
    cancelRow.appendChild(cb);
    body.appendChild(cancelRow);
  }
  if (!d) body.appendChild(el("div", "row muted", "Загрузка…"));
  return body;
}

var LIST_MAX = 50;  // the full lists hold the last 50 entries
function moreButton(box, shown, total, expanded, set) {
  if (total <= shown && !expanded) return;
  var b = el("button", "more", expanded ? "Свернуть" : "Показать все (" + total + ")");
  b.onclick = function () { set(!expanded); render(); };
  box.appendChild(b);
}
function isActiveTask(t) { return t.status === "queued" || t.status === "running"; }

function renderTasks() {
  var filters = document.getElementById("filters"); filters.textContent = "";
  var all = data.tasks.slice(0, LIST_MAX);
  var test = {
    active: isActiveTask, all: function () { return true; },
    done: function (t) { return t.status === "done"; },
    failed: function (t) { return t.status === "failed" || t.status === "needs_lead"; },
  };
  [["active", "Активные"], ["done", "Готово"], ["failed", "Ошибки"], ["all", "Все"]].forEach(function (f) {
    var n = all.filter(test[f[0]]).length;
    var b = el("button", filter === f[0] ? "on" : "", f[1] + " (" + n + ")"); b.onclick = function () { filter = f[0]; renderTasks(); }; filters.appendChild(b);
  });
  var box = document.getElementById("tasks"); box.textContent = "";
  var list = all.filter(test[filter] || test.all);
  if (!list.length) { box.appendChild(el("div", "empty", filter === "active" ? "Активных задач нет" : "Задач нет")); return; }
  list.forEach(function (t) {
    var card = el("div", "task"), head = el("div", "head");
    var statusBox = el("span", "");
    statusBox.appendChild(pill("s-" + t.status, STATUS[t.status] || t.status));
    if (stageLabel(t)) { statusBox.appendChild(document.createTextNode(" ")); statusBox.appendChild(pill("s-idle", stageLabel(t))); }
    head.appendChild(statusBox);
    head.appendChild(el("span", "mono", t.id));
    head.appendChild(el("span", "goal", t.goal));
    var meta = el("span", "meta");
    meta.appendChild(document.createTextNode((t.account_id ? t.account_id + " · " : "") + t.role + " · "));
    if (t.status === "running") meta.appendChild(liveSpan(t.started_at || t.updated_at, "идёт "));
    else if (t.status === "queued") meta.appendChild(liveSpan(t.updated_at, "ждёт "));
    else meta.appendChild(document.createTextNode(
      (t.started_at ? "заняло " + span(t.updated_at - t.started_at) + " · " : "") + ago(t.updated_at, data.now)));
    head.appendChild(meta);
    head.onclick = function () {
      openTasks[t.id] = !openTasks[t.id];
      if (openTasks[t.id]) loadDetail(t.id);
      renderTasks();
    };
    card.appendChild(head);
    if (t.note && !openTasks[t.id]) { var n = el("div", "muted", REASON[t.note] || t.note); n.style.padding = "0 14px 10px"; card.appendChild(n); }
    if (openTasks[t.id]) card.appendChild(renderDetail(t, taskDetail[t.id]));
    box.appendChild(card);
  });
}

function stepChip(step) {
  var label = step.stage === "review" ? "Ревью" : step.stage === "rework" ? "Доработка " + step.round : "Разработка";
  var cls = "s-" + step.status, detail;
  var chip = el("div", "chip");
  var who = (step.account_id || (step.stage === "review" ? "ревьюер" : "разработчик")) + (step.model ? " · " + shortModel(step.model) : "");
  if (step.status === "queued") {
    detail = liveSpan(step.created_at, step.stage === "review" ? "ждёт ревьюера " : "ждёт агента ");
  } else if (step.status === "running") {
    detail = liveSpan(step.started_at || step.updated_at, "идёт ");
  } else if (step.status === "done") {
    var took = step.started_at ? "заняло " + span(step.updated_at - step.started_at) : "готово";
    if (step.stage === "review" && step.verdict) {
      cls = step.verdict === "APPROVE" ? "s-done" : "s-needs";
      took = (step.verdict === "APPROVE" ? "одобрено" : "есть замечания") + " · " + took;
    }
    detail = document.createTextNode(took);
  } else {
    detail = document.createTextNode("ошибка");
  }
  chip.className = "chip " + cls;
  chip.appendChild(el("b", "", label));
  chip.appendChild(el("span", "", who)); chip.appendChild(document.createElement("br"));
  var line = el("span", ""); line.appendChild(detail); chip.appendChild(line);
  return chip;
}

function renderPipelines() {
  var box = document.getElementById("pipelines"); box.textContent = "";
  var every = (data.pipelines || []).slice(0, LIST_MAX);
  if (!every.length) { box.appendChild(el("div", "empty", "Конвейеров пока нет. Задача с review=true проходит разработку, ревью и доработку сама.")); return; }
  var live = every.filter(function (p) { return p.status === "active"; });
  var list = showAllPipes ? every : live;
  if (!list.length) box.appendChild(el("div", "empty", "Активных конвейеров нет"));
  list.forEach(function (p) {
    var card = el("div", "task"), head = el("div", "head");
    var queuedDev = p.waiting && (p.stage === "develop" || p.stage === "rework");
    head.appendChild(pill(queuedDev ? "s-queued" : (PIPE_CLASS[p.stage] || "s-idle"),
                          queuedDev ? PIPE_STAGE[p.stage] + ": ждёт агента" : (PIPE_STAGE[p.stage] || p.stage)));
    head.appendChild(el("span", "mono", p.id));
    head.appendChild(el("span", "goal", p.goal.split("\n")[0]));
    head.appendChild(el("span", "meta", "раунд " + p.round + " из " + p.max_rounds));
    head.onclick = function () {
      openTasks[p.id] = !openTasks[p.id];
      if (openTasks[p.id]) loadDetail(p.id);
      renderPipelines();
    };
    card.appendChild(head);
    var steps = el("div", "pipe-steps");
    p.steps.forEach(function (st, i) {
      if (i) steps.appendChild(el("span", "arrow", "→"));
      steps.appendChild(stepChip(st));
    });
    var endText = p.status === "approved" ? ["s-done", "Одобрено, можно коммитить"]
      : p.status === "needs_lead" ? ["s-needs", "Нужен тимлид: " + (REASON[p.reason] || p.reason || "")]
      : p.status === "failed" ? ["s-failed", "Ошибка: " + (REASON[p.reason] || p.reason || "")] : null;
    if (endText) { steps.appendChild(el("span", "arrow", "→")); steps.appendChild(pill(endText[0], endText[1])); }
    card.appendChild(steps);
    if (openTasks[p.id]) {
      var root = data.tasks.filter(function (t) { return t.id === p.id; })[0] || { id: p.id, repo: "", attempts: 1, status: p.status };
      card.appendChild(renderDetail(root, taskDetail[p.id]));
    }
    box.appendChild(card);
  });
  moreButton(box, live.length, every.length, showAllPipes, function (v) { showAllPipes = v; });
}

function renderEvents() {
  var box = document.getElementById("events"); box.textContent = "";
  var evs = data.events.slice(0, LIST_MAX);
  if (!evs.length) { box.appendChild(el("div", "empty", "Ошибок не было")); return; }
  var RECENT = 3;
  (showAllEvents ? evs : evs.slice(0, RECENT)).forEach(function (ev) {
    var card = el("div", "event"), line = el("div", "line");
    line.appendChild(pill("s-bad", KIND[ev.kind] || ev.kind));
    line.appendChild(el("span", "muted", dateTime(ev.ts)));
    if (ev.account_id) line.appendChild(el("span", "", ev.account_id));
    if (ev.task_id) line.appendChild(el("span", "mono", ev.task_id));
    card.appendChild(line);
    if (ev.message) {
      var det = el("details", ""); det.appendChild(el("summary", "muted", ev.message.split("\n")[0].slice(0, 140) || "подробнее"));
      det.appendChild(pre(ev.message)); card.appendChild(det);
    }
    box.appendChild(card);
  });
  moreButton(box, Math.min(RECENT, evs.length), evs.length, showAllEvents, function (v) { showAllEvents = v; });
}

function renderMaintenance() {
  var banner = document.getElementById("banner"), btn = document.getElementById("drain");
  btn.textContent = data.draining ? "Возобновить приём" : "Пауза приёма задач";
  var lowDisk = data.disk && data.disk.low;
  banner.hidden = !data.draining && !lowDisk;
  if (lowDisk) {
    banner.className = "banner bad";
    banner.textContent = "Мало места на диске пула: свободно " + Math.floor(data.disk.free / 1048576) +
      " МБ из " + Math.floor(data.disk.total / 1048576) + " МБ. Новые задачи не запускаются, пока место не освободится" +
      " (пул чистит старые рабочие копии сам; если не помогает, увеличьте том в Railway).";
    return;
  }
  if (!data.draining) return;
  banner.className = "banner" + (data.safe_to_deploy ? " safe" : "");
  banner.textContent = data.safe_to_deploy
    ? "Режим обслуживания: ничего не выполняется, сервис можно безопасно обновлять. Новые задачи ждут в очереди."
    : "Режим обслуживания: новые задачи не запускаются. Ещё выполняется задач: " + data.running +
      ". Обновлять сервис можно, когда их станет 0.";
}

function recoverTask(id) {
  recovering[id] = true; renderTasks();
  api("/api/tasks/" + id + "/recover", { method: "POST" })
    .catch(function (e) { if (e.auth) showLogin("Токен не подошёл"); else alert("Не удалось: " + e.message); })
    .then(function () { recovering[id] = false; lastSig = ""; refresh(); });
}

function cancelTask(id) {
  cancelling[id] = true; renderTasks();
  api("/api/tasks/" + id + "/cancel", { method: "POST" })
    .catch(function (e) { if (e.auth) showLogin("Токен не подошёл"); else alert("Не удалось: " + e.message); })
    .then(function () { cancelling[id] = false; lastSig = ""; refresh(); });
}

function toggleDrain() {
  var btn = document.getElementById("drain"); btn.disabled = true;
  api("/api/drain", { method: "POST", body: JSON.stringify({ drain: !(data && data.draining) }) })
    .catch(function (e) { if (e.auth) showLogin("Токен не подошёл"); })
    .then(function () { btn.disabled = false; lastSig = ""; refresh(); });
}

function render() {
  renderMaintenance();
  var c = document.getElementById("conn"); c.className = "pill s-ok"; c.textContent = "онлайн";
  document.getElementById("updated").textContent = "обновлено " + clock(data.now);
  renderCounters(); renderAccounts(); renderPipelines(); renderTasks(); renderEvents();
}

// ---- actions -----------------------------------------------------------
function loadDetail(id) {
  api("/api/tasks/" + id).then(function (d) { taskDetail[id] = d; if (data) { renderTasks(); renderPipelines(); } }).catch(function () {});
}

function checkQuota(id) {
  quotaBusy[id] = true; renderAccounts();
  api("/api/accounts/" + encodeURIComponent(id) + "/quota", { method: "POST" })
    .then(function (r) { quotaFresh[id] = r; })
    .catch(function (e) {
      if (e.auth) return showLogin("Токен не подошёл");
      quotaFresh[id] = { checked_at: Date.now() / 1000, ok: false, text: "Не удалось выполнить проверку: " + e.message };
    })
    .then(function () { quotaBusy[id] = false; if (data) renderAccounts(); refresh(); });
}

function refresh() {
  clearTimeout(timer);
  loadFiles();
  api("/api/overview").then(function (d) {
    clockOffset = d.now - Date.now() / 1000;
    // Rebuild the page only when something changed (or every 15 s for the countdowns), and never
    // while the user has text selected, so clicks and selections are not destroyed by a refresh.
    var sig = JSON.stringify([d.accounts, d.tasks, d.events, d.draining, d.running, d.disk && d.disk.low, d.pipelines, Math.floor(d.now / 15)]);
    var selecting = String(window.getSelection ? window.getSelection() : "").length > 0 ||
      (document.activeElement && ["SELECT", "INPUT"].indexOf(document.activeElement.tagName) >= 0);
    var first = !data;
    if (sig === lastSig || (selecting && !first)) { showApp(); return; }
    lastSig = sig; data = d; showApp(); render();
    Object.keys(openTasks).forEach(function (id) { if (openTasks[id]) loadDetail(id); });
  }).catch(function (e) {
    if (e.auth) { store(null); token = null; return showLogin("Токен не подошёл"); }
    var c = document.getElementById("conn"); c.className = "pill s-bad"; c.textContent = "нет связи";
  }).then(function () {
    if (token) timer = setTimeout(refresh, 4000);
  });
}


// ---- uploaded files ------------------------------------------------------
var filesData = null, filesSig = "", uploading = false;
function bytes(n) {
  if (n < 1024) return n + " Б";
  var u = ["КБ", "МБ", "ГБ"], i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < 2);
  return n.toFixed(n < 10 ? 1 : 0) + " " + u[i];
}
function loadFiles() {
  if (uploading) return;
  api("/api/uploads").then(function (d) {
    var sig = JSON.stringify(d);
    if (sig === filesSig) return;
    var box = document.getElementById("filelist");
    if (document.activeElement && box.contains(document.activeElement)) return;  // do not rebuild under a focused control
    filesSig = sig; filesData = d; renderFiles(); renderRqForm();
  }).catch(function (e) { if (e.auth) return; });
}
function patchUpload(id, obj) {
  api("/api/uploads/" + id, { method: "POST", body: JSON.stringify(obj) })
    .catch(function (e) { alert("Не удалось: " + e.message); })
    .then(function () { filesSig = ""; loadFiles(); });
}
function downloadUpload(id, name, branch) {
  var url = "/api/uploads/" + id + "/download" + (branch ? "?branch=" + encodeURIComponent(branch) : "");
  fetch(url, { headers: { "Authorization": "Bearer " + token } }).then(function (r) {
    if (!r.ok) return r.json().then(function (j) { throw new Error(j.error || r.status); });
    return r.blob();
  }).then(function (b) {
    var a = document.createElement("a"); a.href = URL.createObjectURL(b);
    a.download = name + (branch ? "-" + branch.replace(/\//g, "_") : "") + ".zip";
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 10000);
  }).catch(function (e) { alert("Не удалось скачать: " + e.message); });
}
function deleteUpload(u) {
  if (!confirm("Удалить «" + u.name + "» с сервера? Это нельзя отменить.")) return;
  api("/api/uploads/" + u.id, { method: "DELETE" })
    .catch(function (e) { alert("Не удалось: " + e.message); })
    .then(function () { filesSig = ""; loadFiles(); });
}
var openUp = {}, upDetail = {};
function loadUpDetail(id) {
  api("/api/uploads/" + id).then(function (d) { upDetail[id] = d; renderFiles(); }).catch(function () {});
}
function deleteOneFile(u, name) {
  if (!confirm("Удалить файл «" + name + "» из «" + u.name + "»?")) return;
  api("/api/uploads/" + u.id + "/file?path=" + encodeURIComponent(name), { method: "DELETE" })
    .catch(function (e) { alert("Не удалось: " + e.message); })
    .then(function () { delete upDetail[u.id]; filesSig = ""; loadFiles(); loadUpDetail(u.id); });
}
function uploadCard(u) {
  var card = el("div", "card");
  var top = el("div", "top");
  top.appendChild(el("span", "name", u.name));
  top.appendChild(u.pinned ? pill("s-ok", "постоянный") : pill("s-idle", "обычный"));
  if (u.project) top.appendChild(pill("s-run", "проект"));
  card.appendChild(top);
  card.appendChild(el("div", "row muted", u.files + " файл. · " + bytes(u.size) + " · " + dateTime(u.created_at)));
  var idRow = el("div", "row muted"); idRow.appendChild(document.createTextNode("id для Claude: "));
  idRow.appendChild(el("span", "mono", u.id)); card.appendChild(idRow);
  if (u.note) card.appendChild(el("div", "row", u.note));
  var det = el("details", "row");
  if (openUp[u.id]) det.open = true;
  det.appendChild(el("summary", "muted", "Файлы внутри"));
  var names = upDetail[u.id] ? upDetail[u.id].file_names : null;
  if (!names) det.appendChild(el("div", "muted", "Загрузка списка…"));
  else {
    names.forEach(function (n) {
      var line = el("div", "fline"); line.appendChild(el("span", "mono", n));
      var x = el("button", "danger small", "×"); x.title = "Удалить файл"; x.onclick = function () { deleteOneFile(u, n); };
      line.appendChild(x); det.appendChild(line);
    });
    if (u.files > names.length) det.appendChild(el("div", "muted", "… и ещё " + (u.files - names.length)));
  }
  det.ontoggle = function () {
    openUp[u.id] = det.open;
    if (det.open && !upDetail[u.id]) loadUpDetail(u.id);
  };
  card.appendChild(det);
  var opts = el("div", "row");
  var pin = el("input", ""); pin.type = "checkbox"; pin.checked = u.pinned;
  pin.onchange = function () { patchUpload(u.id, { pinned: pin.checked }); };
  var pl = el("label", ""); pl.appendChild(pin); pl.appendChild(document.createTextNode(" постоянный (не удалять автоматически)"));
  opts.appendChild(pl); card.appendChild(opts);
  var ops = el("div", "row ops");
  var dl = el("button", "", "Скачать"); dl.onclick = function () { downloadUpload(u.id, u.name); };
  ops.appendChild(dl);
  if (u.project) {
    var br = el("button", "", "Скачать ветку…");
    br.onclick = function () { var b = prompt("Имя ветки, например pool/0123456789ab"); if (b) downloadUpload(u.id, u.name, b.trim()); };
    ops.appendChild(br);
  }
  var del = el("button", "danger", "Удалить"); del.onclick = function () { deleteUpload(u); };
  ops.appendChild(del); card.appendChild(ops);
  return card;
}
function renderFiles() {
  var d = filesData, box = document.getElementById("filelist");
  box.textContent = "";
  var lim = d.limits;
  document.getElementById("files-hint").textContent = "Занято " + bytes(lim.used) + " из " + bytes(lim.max_total) +
    ". Один файл до " + bytes(lim.max_file) + ". Обычные файлы, которые " + lim.ttl_days + " дн. не использовались, удаляются сами; постоянные остаются.";
  [["Постоянные", true], ["Обычные", false]].forEach(function (g) {
    var list = d.uploads.filter(function (u) { return u.pinned === g[1]; });
    box.appendChild(el("h2", "", g[0] + " (" + list.length + ")"));
    if (!list.length) { box.appendChild(el("div", "empty", g[1] ? "Постоянных файлов нет: отметьте «постоянный» при загрузке или в карточке" : "Обычных файлов нет")); return; }
    var grid = el("div", "file-list");
    list.forEach(function (u) { grid.appendChild(uploadCard(u)); });
    box.appendChild(grid);
  });
}

function putFile(uploadId, file, rel, onBytes) {
  return new Promise(function (resolve, reject) {
    var x = new XMLHttpRequest();
    x.open("PUT", "/api/uploads/" + uploadId + "/file?path=" + encodeURIComponent(rel));
    x.setRequestHeader("Authorization", "Bearer " + token);
    x.upload.onprogress = function (e) { if (e.lengthComputable) onBytes(e.loaded); };
    x.onload = function () {
      if (x.status === 200) return resolve();
      var msg = "HTTP " + x.status; try { msg = JSON.parse(x.responseText).error || msg; } catch (e) {}
      reject(new Error(rel + ": " + msg));
    };
    x.onerror = function () { reject(new Error(rel + ": нет связи")); };
    x.send(file);
  });
}
function startUpload(items) {  // items: [{file, rel}]
  if (uploading || !items.length) return;
  var status = document.getElementById("up-status");
  var total = items.reduce(function (n, i) { return n + i.file.size; }, 0), done = 0;
  var first = items[0].rel.split("/")[0];
  var name = document.getElementById("up-name").value.trim() || (items.length > 1 || items[0].rel.indexOf("/") >= 0 ? first : items[0].rel);
  uploading = true; status.textContent = "";
  var line = el("div", "row", "Загрузка…"), prog = el("div", "prog"), bar = el("i"); prog.appendChild(bar);
  status.appendChild(line); status.appendChild(prog);
  var uploadId;
  api("/api/uploads", { method: "POST", body: JSON.stringify({
    name: name, note: document.getElementById("up-note").value.trim(),
    pinned: document.getElementById("up-pin").checked }) })
    .then(function (u) {
      uploadId = u.id;
      return items.reduce(function (chain, it, idx) {
        return chain.then(function () {
          line.textContent = "Загрузка " + (idx + 1) + " из " + items.length + ": " + it.rel;
          return putFile(uploadId, it.file, it.rel, function (loaded) {
            bar.style.width = Math.min(100, (done + loaded) / Math.max(total, 1) * 100) + "%";
          }).then(function () { done += it.file.size; });
        });
      }, Promise.resolve());
    })
    .then(function () {
      status.textContent = "Готово: " + items.length + " файл.";
      document.getElementById("up-name").value = ""; document.getElementById("up-note").value = "";
    })
    .catch(function (e) {
      status.textContent = "";
      status.appendChild(el("div", "err", "Не удалось загрузить: " + e.message +
        (uploadId ? " (то, что успело загрузиться, осталось в списке; можно удалить)" : "")));
    })
    .then(function () { uploading = false; filesSig = ""; loadFiles(); });
}
function entryFiles(entry, prefix) {  // dropped folders: walk the tree
  if (entry.isFile) return new Promise(function (res) { entry.file(function (f) { res([{ file: f, rel: prefix + entry.name }]); }, function () { res([]); }); });
  var reader = entry.createReader(), all = [];
  function more() {
    return new Promise(function (res) { reader.readEntries(res, function () { res([]); }); }).then(function (batch) {
      if (!batch.length) return all;
      all = all.concat(batch); return more();
    });
  }
  return more().then(function (children) {
    return Promise.all(children.map(function (c) { return entryFiles(c, prefix + entry.name + "/"); }));
  }).then(function (lists) { return [].concat.apply([], lists); });
}
(function setupUploads() {
  var drop = document.getElementById("drop");
  document.getElementById("pick-files").onclick = function () { document.getElementById("in-files").click(); };
  document.getElementById("pick-dir").onclick = function () { document.getElementById("in-dir").click(); };
  function fromInput(ev) {
    var list = Array.prototype.map.call(ev.target.files, function (f) { return { file: f, rel: f.webkitRelativePath || f.name }; });
    ev.target.value = ""; startUpload(list);
  }
  document.getElementById("in-files").onchange = fromInput;
  document.getElementById("in-dir").onchange = fromInput;
  ["dragenter", "dragover"].forEach(function (n) { drop.addEventListener(n, function (e) { e.preventDefault(); drop.classList.add("over"); }); });
  ["dragleave", "drop"].forEach(function (n) { drop.addEventListener(n, function (e) { e.preventDefault(); drop.classList.remove("over"); }); });
  drop.addEventListener("drop", function (e) {
    var items = e.dataTransfer && e.dataTransfer.items;
    if (items && items.length && items[0].webkitGetAsEntry) {
      var entries = Array.prototype.map.call(items, function (i) { return i.webkitGetAsEntry(); }).filter(Boolean);
      Promise.all(entries.map(function (en) { return entryFiles(en, ""); })).then(function (lists) { startUpload([].concat.apply([], lists)); });
    } else {
      startUpload(Array.prototype.map.call(e.dataTransfer.files, function (f) { return { file: f, rel: f.name }; }));
    }
  });
})();

document.getElementById("login-form").onsubmit = function (e) {
  e.preventDefault();
  token = document.getElementById("token").value.trim();
  document.getElementById("token").value = "";
  store(token); refresh();
};
var PANELS = { storage: ["storage-panel", "to-storage"], task: ["task-panel", "to-task"] };
function setPanel(name) {  // one drawer at a time: "storage", "task" or null
  Object.keys(PANELS).forEach(function (k) {
    var on = k === name;
    document.getElementById(PANELS[k][0]).classList.toggle("open", on);
    document.getElementById(PANELS[k][0]).setAttribute("aria-hidden", on ? "false" : "true");
    document.getElementById(PANELS[k][1]).classList.toggle("on", on);
  });
  document.body.classList.toggle("drawer-open", !!name);
  try { localStorage.setItem("pool-panel", name || ""); } catch (e) {}
  if (name === "task") { loadModelsForForm(); loadRequests(); renderRqForm(); }
  if (name === "storage") { filesSig = ""; loadFiles(); }
}
function currentPanel() { return document.getElementById("task-panel").classList.contains("open") ? "task" : document.getElementById("storage-panel").classList.contains("open") ? "storage" : null; }
document.getElementById("to-storage").onclick = function () { setPanel(currentPanel() === "storage" ? null : "storage"); };
document.getElementById("to-task").onclick = function () { setPanel(currentPanel() === "task" ? null : "task"); };
document.getElementById("storage-close").onclick = function () { setPanel(null); };
document.getElementById("task-close").onclick = function () { setPanel(null); };

// ---- task panel ----------------------------------------------------------
var rqChecked = {}, rqModelsLoaded = false, rqSig = "", rqData = [];
function loadModelsForForm() {
  if (rqModelsLoaded) return;
  api("/api/models").then(function (d) {
    rqModelsLoaded = true;
    var sel = document.getElementById("rq-model");
    d.models.forEach(function (m) { var o = el("option", "", m.name || m.id); o.value = m.id; sel.appendChild(o); });
  }).catch(function () {});
}
function fillSelect(sel, items, first) {
  var keep = sel.value;
  sel.textContent = "";
  if (first) { var o0 = el("option", "", first); o0.value = ""; sel.appendChild(o0); }
  items.forEach(function (u) { var o = el("option", "", u.name + " (" + bytes(u.size) + ")"); o.value = u.id; sel.appendChild(o); });
  if (keep) sel.value = keep;
}
function renderRqForm() {
  if (!filesData) return;
  var ups = filesData.uploads, box = document.getElementById("rq-attach");
  box.textContent = "";
  if (!ups.length) box.appendChild(el("div", "muted", "В хранилище пока пусто"));
  ups.forEach(function (u) {
    var lab = el("label", ""), cb = el("input", ""); cb.type = "checkbox"; cb.checked = !!rqChecked[u.id];
    cb.onchange = function () { rqChecked[u.id] = cb.checked; };
    lab.appendChild(cb); lab.appendChild(el("span", "", u.name + " · " + bytes(u.size))); box.appendChild(lab);
  });
  fillSelect(document.getElementById("rq-project"), ups, "");
  fillSelect(document.getElementById("rq-save"), ups, "в новую загрузку");
}
function modeIsProject() { return document.querySelector('input[name="rq-mode"]:checked').value === "project"; }
document.querySelectorAll('input[name="rq-mode"]').forEach(function (r) {
  r.onchange = function () { document.getElementById("rq-review-line").hidden = !modeIsProject(); };
});
function sendRequest() {
  var err = document.getElementById("rq-err"), btn = document.getElementById("rq-send");
  err.textContent = "";
  var goal = document.getElementById("rq-goal").value.trim();
  if (!goal) { err.textContent = "Напишите, что нужно сделать"; return; }
  var project = modeIsProject() ? document.getElementById("rq-project").value : "";
  if (modeIsProject() && !project) { err.textContent = "Выберите проект из хранилища"; return; }
  var body = { goal: goal, role: document.getElementById("rq-role").value,
    model: document.getElementById("rq-model").value || null,
    uploads: Object.keys(rqChecked).filter(function (k) { return rqChecked[k]; }),
    project: project || null, save_to: document.getElementById("rq-save").value || null,
    review: modeIsProject() && document.getElementById("rq-review").checked };
  btn.disabled = true;
  api("/api/requests", { method: "POST", body: JSON.stringify(body) })
    .then(function () { document.getElementById("rq-goal").value = ""; rqChecked = {}; renderRqForm(); rqSig = ""; loadRequests(); refresh(); })
    .catch(function (e) { err.textContent = "Не удалось отправить: " + e.message; })
    .then(function () { btn.disabled = false; });
}
document.getElementById("rq-send").onclick = sendRequest;
document.getElementById("rq-upload").onclick = function () { document.getElementById("rq-file").click(); };
document.getElementById("rq-file").onchange = function (ev) {
  var files = Array.prototype.slice.call(ev.target.files); ev.target.value = "";
  if (!files.length) return;
  var note = document.getElementById("rq-note"); note.textContent = "Загрузка…";
  var uploadId;
  api("/api/uploads", { method: "POST", body: JSON.stringify({ name: files[0].name, pinned: false }) }).then(function (u) {
    uploadId = u.id;
    return files.reduce(function (chain, f) {
      return chain.then(function () { return putFile(uploadId, f, f.name, function () {}); });
    }, Promise.resolve());
  }).then(function () { rqChecked[uploadId] = true; note.textContent = "Загружено и приложено"; filesSig = ""; loadFiles(); })
    .catch(function (e) { note.textContent = "Не удалось: " + e.message; });
};
function loadRequests() {
  api("/api/requests").then(function (d) {
    var sig = JSON.stringify(d.requests);
    rqData = d.requests;
    if (sig === rqSig) return;
    rqSig = sig; renderRequests();
  }).catch(function () {});
}
function renderRequests() {
  var box = document.getElementById("rq-list"); box.textContent = "";
  if (!rqData.length) { box.appendChild(el("div", "empty", "Запросов пока нет")); return; }
  rqData.forEach(function (r) {
    var card = el("div", "req"), top = el("div", "top");
    top.appendChild(pill("s-" + r.status, STATUS[r.status] || r.status));
    if (r.model) top.appendChild(el("span", "muted", shortModel(r.model)));
    if (r.account_id) top.appendChild(el("span", "muted", r.account_id));
    if (r.status === "running" && r.started_at) top.appendChild(liveSpan(r.started_at, "идёт "));
    else if (r.status === "queued") top.appendChild(el("span", "muted", "ждёт очереди"));
    card.appendChild(top);
    card.appendChild(el("div", "goal", r.goal.length > 160 ? r.goal.slice(0, 160) + "…" : r.goal));
    if (r.summary) { var det = el("details", "row"); det.appendChild(el("summary", "muted", "Ответ агента")); det.appendChild(pre(r.summary)); det.open = r.status === "done"; card.appendChild(det); }
    if (r.error && r.status !== "done") card.appendChild(el("div", "err", String(r.error).slice(0, 300)));
    var ops = el("div", "row ops");
    if (r.outputs && r.outputs.upload_id) {
      card.appendChild(el("div", "row", "Файлы агента в хранилище: " + r.outputs.saved.join(", ") + (r.outputs.skipped && r.outputs.skipped.length ? " (не сохранены из-за лимита: " + r.outputs.skipped.join(", ") + ")" : "")));
      var dl = el("button", "", "Скачать файлы"); dl.onclick = function () { downloadUpload(r.outputs.upload_id, "result"); }; ops.appendChild(dl);
      var op = el("button", "", "Открыть хранилище"); op.onclick = function () { setPanel("storage"); }; ops.appendChild(op);
    }
    var m = /\/uploads\/([0-9a-f]{12})$/.exec(r.repo || "");
    if (m && r.status === "done" && r.branch) {
      card.appendChild(el("div", "row muted", "Ветка с правками: " + r.branch + " (лежит в проекте в хранилище)"));
      var bd = el("button", "", "Скачать ветку"); bd.onclick = function () { downloadUpload(m[1], "project", r.branch); }; ops.appendChild(bd);
    }
    if (ops.childNodes.length) card.appendChild(ops);
    box.appendChild(card);
  });
}
setInterval(function () {
  if (!token) return;
  var active = rqData.some(function (r) { return r.status === "queued" || r.status === "running"; });
  if (currentPanel() === "task" || active) loadRequests();
}, 4000);

try { var saved = localStorage.getItem("pool-panel"); if (saved === "storage" || saved === "task") setPanel(saved); } catch (e) {}
document.getElementById("drain").onclick = toggleDrain;
var ssoLogout = "";
document.getElementById("logout").onclick = function () {
  store(null); token = null; data = null; lastSig = "";
  if (ssoLogout) { location.href = ssoLogout; return; }  // single sign-on: log out everywhere, or the cookie logs us straight back in
  showLogin("");
};
document.addEventListener("visibilitychange", function () { if (!document.hidden && token) refresh(); });

// Behind the single sign-on gate the server hands over the token for a valid sign-in cookie: no second login form.
function trySso() {
  return fetch("/api/sso/session", { credentials: "same-origin" }).then(function (r) { return r.ok ? r.json() : null; })
    .then(function (j) { if (j && j.token) { ssoLogout = j.logout_url || ""; token = j.token; store(token); return true; } return false; })
    .catch(function () { return false; });
}
trySso().then(function (ok) {
  if (!ok) token = load();
  if (token) refresh(); else showLogin("");
});
})();
