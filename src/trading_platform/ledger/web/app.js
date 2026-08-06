const API = "/api/v1";
const state = { view: "overview", admissions: [] };
const titles = {
  overview: "运行总览",
  orders: "订单账本",
  trades: "成交记录",
  positions: "当前持仓",
  admissions: "交易池准入",
};

const $ = (id) => document.getElementById(id);
const escapeHtml = (value) => String(value ?? "--").replace(/[&<>'"]/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
}[char]));
const number = (value, digits = 4) => Number(value ?? 0).toLocaleString("zh-CN", {
  maximumFractionDigits: digits,
});
const dateTime = (value) => value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "--";

async function api(path, options) {
  const response = await fetch(`${API}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${response.status}`);
  }
  return response.json();
}

function filters() {
  const params = new URLSearchParams();
  [["account_id", $("account-filter").value], ["strategy_id", $("strategy-filter").value], ["symbol", $("symbol-filter").value]]
    .forEach(([key, value]) => { if (value.trim()) params.set(key, value.trim()); });
  return params;
}

function table(target, columns, rows) {
  const head = columns.map(([label]) => `<th>${label}</th>`).join("");
  const body = rows.length
    ? rows.map((row) => `<tr>${columns.map(([, render]) => `<td>${render(row)}</td>`).join("")}</tr>`).join("")
    : `<tr><td class="empty" colspan="${columns.length}">暂无记录</td></tr>`;
  target.innerHTML = `<thead><tr>${head}</tr></thead><tbody>${body}</tbody>`;
}

const orderColumns = [
  ["时间", (row) => dateTime(row.created_at)],
  ["交易对", (row) => escapeHtml(row.symbol)],
  ["方向", (row) => escapeHtml(row.side)],
  ["价格", (row) => number(row.price)],
  ["数量", (row) => number(row.quantity, 8)],
  ["状态", (row) => escapeHtml(row.status)],
  ["Client Order ID", (row) => escapeHtml(row.client_order_id)],
];
const tradeColumns = [
  ["成交时间", (row) => dateTime(row.exchange_time)],
  ["交易对", (row) => escapeHtml(row.symbol)],
  ["方向", (row) => escapeHtml(row.side)],
  ["价格", (row) => number(row.price)],
  ["数量", (row) => number(row.quantity, 8)],
  ["手续费", (row) => number(row.commission)],
  ["已实现 PnL", (row) => `<span class="${Number(row.realized_pnl) >= 0 ? "positive" : "negative"}">${number(row.realized_pnl)}</span>`],
];
const positionColumns = [
  ["更新时间", (row) => dateTime(row.updated_at)],
  ["交易对", (row) => escapeHtml(row.symbol)],
  ["方向", (row) => escapeHtml(row.position_side)],
  ["持仓量", (row) => number(row.quantity, 8)],
  ["开仓均价", (row) => number(row.entry_price)],
  ["标记价格", (row) => number(row.mark_price)],
  ["未实现 PnL", (row) => `<span class="${Number(row.unrealized_pnl) >= 0 ? "positive" : "negative"}">${number(row.unrealized_pnl)}</span>`],
];

async function loadHealth() {
  try {
    await api("/health");
    $("health-dot").className = "status-dot is-good";
    $("health-label").textContent = "账本服务正常";
  } catch {
    $("health-dot").className = "status-dot is-bad";
    $("health-label").textContent = "账本服务异常";
  }
}

async function loadOverview() {
  const query = filters();
  const account = query.get("account_id");
  const [orders, trades, positions] = await Promise.all([
    api(`/orders?${query}&limit=8`), api(`/trades?${query}&limit=1`), api(`/positions?${query}&limit=100`),
  ]);
  let pnl = null;
  if (account) pnl = await api(`/pnl?${query}`);
  $("metric-pnl").textContent = pnl ? number(pnl.net_pnl, 2) : "筛选账户";
  $("metric-unrealized").textContent = pnl ? number(pnl.total_unrealized_pnl, 2) : "--";
  $("metric-positions").textContent = positions.total;
  $("metric-trades").textContent = trades.total;
  $("overview-updated").textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  table($("overview-table"), orderColumns, orders.items);
}

async function loadLedgerView(view, path, columns) {
  const payload = await api(`/${path}?${filters()}&limit=200`);
  $(`${view}-count`).textContent = `${payload.total} 条`;
  table($(`${view}-table`), columns, payload.items);
}

function renderAdmissions() {
  $("admissions-count").textContent = `${state.admissions.length} 项`;
  $("admission-list").innerHTML = state.admissions.length ? state.admissions.map((item) => `
    <div class="admission-row" data-name="${escapeHtml(item.subcategory)}">
      <div class="admission-meta"><strong>${escapeHtml(item.subcategory)}</strong><small>版本 ${item.version} · ${dateTime(item.updated_at)}</small></div>
      <button class="switch" type="button" role="switch" aria-checked="${item.enabled}" aria-label="切换 ${escapeHtml(item.subcategory)}"></button>
      <input class="reason" maxlength="240" placeholder="变更原因" value="">
      <span>${item.enabled ? "允许交易" : "禁止入场"}</span>
    </div>`).join("") : `<p class="empty">暂无准入项</p>`;
}

async function loadAdmissions() {
  const payload = await api("/subcategory-admissions?limit=1000");
  state.admissions = payload.items;
  renderAdmissions();
}

async function updateAdmission(name, enabled, version, reason) {
  const operator = $("operator").value.trim();
  if (!operator) throw new Error("请填写操作者");
  await api(`/subcategory-admissions/${encodeURIComponent(name)}`, {
    method: "PUT",
    body: JSON.stringify({ enabled, expected_version: version, updated_by: operator, reason: reason || null }),
  });
  await loadAdmissions();
}

async function loadCurrent() {
  $("refresh").disabled = true;
  try {
    await loadHealth();
    if (state.view === "overview") await loadOverview();
    if (state.view === "orders") await loadLedgerView("orders", "orders", orderColumns);
    if (state.view === "trades") await loadLedgerView("trades", "trades", tradeColumns);
    if (state.view === "positions") await loadLedgerView("positions", "positions", positionColumns);
    if (state.view === "admissions") await loadAdmissions();
  } catch (error) {
    notify(error.message, true);
  } finally {
    $("refresh").disabled = false;
  }
}

function notify(message, isError = false) {
  const notice = $("notice");
  notice.textContent = message;
  notice.style.background = isError ? "var(--bad)" : "var(--ink)";
  notice.classList.add("is-visible");
  window.setTimeout(() => notice.classList.remove("is-visible"), 2600);
}

document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => {
  document.querySelector(".nav-item.is-active").classList.remove("is-active");
  document.querySelector(".view.is-visible").classList.remove("is-visible");
  button.classList.add("is-active");
  state.view = button.dataset.view;
  $(`${state.view}-view`).classList.add("is-visible");
  $("view-title").textContent = titles[state.view];
  loadCurrent();
}));
$("refresh").addEventListener("click", loadCurrent);
$("apply-filters").addEventListener("click", loadCurrent);
$("new-admission-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await updateAdmission($("new-subcategory").value.trim(), true, 0, "新增准入项");
    $("new-subcategory").value = "";
    notify("准入项已创建");
  } catch (error) { notify(error.message, true); }
});
$("admission-list").addEventListener("click", async (event) => {
  const toggle = event.target.closest(".switch");
  if (!toggle) return;
  const row = toggle.closest(".admission-row");
  const item = state.admissions.find((entry) => entry.subcategory === row.dataset.name);
  try {
    await updateAdmission(item.subcategory, !item.enabled, item.version, row.querySelector(".reason").value.trim());
    notify(item.enabled ? "已禁止新入场" : "已允许新入场");
  } catch (error) { notify(error.message, true); }
});

loadCurrent();
