/* 可公开核验现场抽签 —— 原生 JavaScript, 无第三方依赖。
 * 关键性质: 种子只在浏览器生成; 核验也全部在浏览器内独立重算。
 */
"use strict";

const RULE_ID = "pick-sha256-v1";

// ---------------------------------------------------------------- 通用工具

const $ = (sel) => document.querySelector(sel);

function toast(msg, isErr = false) {
  const t = document.createElement("div");
  t.className = "toast" + (isErr ? " err" : "");
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3600);
}

async function api(method, url, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(url, opts);
  } catch (e) {
    throw new Error("无法连接服务端: " + e.message);
  }
  let data = null;
  try { data = await res.json(); } catch (_) { /* ignore */ }
  if (!res.ok || !data || data.ok === false) {
    throw new Error((data && data.error) || `HTTP ${res.status}`);
  }
  return data;
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("已复制");
  } catch (_) {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
    toast("已复制");
  }
}

function downloadText(filename, text, mime = "text/plain") {
  const blob = new Blob([text], { type: `${mime};charset=utf-8` });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

const enc = new TextEncoder();

async function sha256Hex(text) {
  const digest = await crypto.subtle.digest("SHA-256", enc.encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function randomHexSeed() {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// ---------------------------------------------------------------- hash 路由

function parseHash() {
  const h = location.hash.replace(/^#\/?/, "");
  const [path, query = ""] = h.split("?");
  const params = new URLSearchParams(query);
  return { path, params };
}

function showView(name) {
  for (const v of ["create", "round", "verify"]) {
    $(`#view-${v}`).classList.toggle("hidden", v !== name);
  }
  document.querySelectorAll("nav a").forEach((a) => a.classList.remove("active"));
  if (name === "round") $("#nav-round").classList.add("active");
  if (name === "verify") $("#nav-verify").classList.add("active");
}

window.addEventListener("hashchange", route);

function route() {
  const { path, params } = parseHash();
  if (path === "round" && params.get("id")) {
    openRoundView(params.get("id"), params.get("token") || "");
  } else if (path === "verify") {
    showView("verify");
  } else {
    showView("create");
  }
}

// ---------------------------------------------------------------- 视图 1: 创建

let draftSeed = "";

$("#c-generate").addEventListener("click", async () => {
  draftSeed = randomHexSeed();
  const commitment = await sha256Hex(draftSeed);
  $("#c-seed").textContent = draftSeed;
  $("#c-commitment").textContent = commitment;
  $("#c-seed-box").classList.remove("hidden");
  $("#c-created").classList.add("hidden");
});

$("#c-copy-seed").addEventListener("click", () => copyText(draftSeed));
$("#c-copy-commitment").addEventListener("click", () => copyText($("#c-commitment").textContent));

$("#c-download-seed").addEventListener("click", () => {
  const commitment = $("#c-commitment").textContent;
  const text =
    `现场抽签 · 种子凭证（请在揭晓前保密并妥善保存，服务端不保存种子）\n` +
    `生成时间 (UTC): ${new Date().toISOString()}\n` +
    `规则: ${RULE_ID}  ——  commitment = lowercase(hex(SHA-256(UTF-8(seed))))\n` +
    `------------------------------------------------------------\n` +
    `seed       : ${draftSeed}\n` +
    `commitment : ${commitment}\n`;
  downloadText(`draw-seed-${commitment.slice(0, 10)}.txt`, text);
});

$("#c-submit").addEventListener("click", async () => {
  const winnerCount = parseInt($("#c-winner-count").value, 10);
  if (!Number.isInteger(winnerCount) || winnerCount < 1) {
    return toast("中奖名额必须是正整数", true);
  }
  const commitment = $("#c-commitment").textContent;
  if (!draftSeed || !commitment) return toast("请先生成种子", true);

  const btn = $("#c-submit");
  btn.disabled = true;
  try {
    const data = await api("POST", "/api/rounds", {
      seed_commitment: commitment,
      winner_count: winnerCount,
    });
    const adminUrl = `${location.origin}/#/round?id=${data.id}&token=${data.admin_token}`;
    const publicUrl = `${location.origin}/#/round?id=${data.id}`;
    $("#c-id").textContent = data.id;
    $("#c-admin-url").textContent = adminUrl;
    $("#c-public-url").textContent = publicUrl;
    $("#c-created").classList.remove("hidden");
    $("#c-open-admin").onclick = () => { location.hash = `#/round?id=${data.id}&token=${data.admin_token}`; };
    $("#c-copy-admin").onclick = () => copyText(adminUrl);
    toast("轮次已创建：请保存含令牌的管理链接");
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
});

// ---------------------------------------------------------------- 视图 2: 轮次

const roundState = { id: null, token: "", data: null, dirty: false, timer: null };

$("#r-editor").addEventListener("input", () => { roundState.dirty = true; });

function parseEditor(text) {
  const out = [];
  const seen = new Set();
  const lines = text.split(/\r?\n/);
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    if (!line) continue;
    const m = line.match(/^(\d+)[\t,，]?\s*(.*)$/);
    if (!m) throw new Error(`第 ${i + 1} 行无法解析（应以编号开头）: ${line}`);
    const id = parseInt(m[1], 10);
    const label = (m[2] || "").trim().slice(0, 100);
    if (seen.has(id)) throw new Error(`编号重复: ${id}`);
    seen.add(id);
    out.push({ id, label });
  }
  return out;
}

function participantsToText(list) {
  return list.map((p) => (p.label ? `${p.id},${p.label}` : String(p.id))).join("\n");
}

function openRoundView(id, token) {
  showView("round");
  roundState.id = id;
  roundState.token = token;
  roundState.dirty = false;
  clearInterval(roundState.timer);
  roundState.timer = setInterval(() => {
    if ($("#r-autorefresh").checked && !roundState.dirty) refreshRound();
  }, 5000);
  refreshRound();
}

async function refreshRound() {
  try {
    roundState.data = await api("GET", `/api/rounds/${roundState.id}`);
    renderRound();
  } catch (e) {
    toast(e.message, true);
  }
}
const STATUS_TEXT = { open: "登记中（名单可改）", sealed: "已封存（名单冻结）", revealed: "已揭晓" };

function renderRound() {
  const d = roundState.data;
  const isAdmin = !!roundState.token;

  $("#r-id").textContent = `#${d.id}`;
  const badge = $("#r-status");
  badge.textContent = STATUS_TEXT[d.status] || d.status;
  badge.className = "badge " + d.status;
  $("#r-winner-count").textContent = d.winner_count;
  $("#r-rule").textContent = `${d.rule_id} v${d.rule_version}`;
  $("#r-commitment").textContent = d.seed_commitment;
  $("#r-digest").textContent = d.participants_digest || "—";
  $("#r-created").textContent = d.created_at;
  $("#r-times").textContent = [d.sealed_at, d.revealed_at].filter(Boolean).join("  /  ") || "—";

  $("#r-edit-panel").classList.toggle("hidden", !(d.status === "open" && isAdmin));
  $("#r-reveal-panel").classList.toggle("hidden", !(d.status === "sealed" && isAdmin));
  $("#r-view-panel").classList.toggle("hidden", d.status === "open");
  $("#r-result-panel").classList.toggle("hidden", d.status !== "revealed");

  let readonlyHint = document.getElementById("r-readonly-hint");
  if (!isAdmin) {
    if (!readonlyHint) {
      readonlyHint = document.createElement("p");
      readonlyHint.id = "r-readonly-hint";
      readonlyHint.className = "hint";
      $("#view-round .card").before(readonlyHint);
    }
    readonlyHint.textContent = d.status === "open"
      ? "🔎 公示视图（只读）：名单登记中，主持人封存后将在此展示名单摘要。"
      : "🔎 公示视图（只读）：本页显示事先承诺、封存名单摘要与开奖结果，可用「导出核验包」自行核验。";
  } else if (readonlyHint) {
    readonlyHint.remove();
  }

  if (d.status === "open") {
    if (isAdmin && !roundState.dirty) {
      $("#r-editor").value = participantsToText(d.participants || []);
    }
    $("#r-count").textContent = `已登记 ${d.participant_count} 人 / 需 ≥ ${d.winner_count} 人才可封存`;
  }

  if (d.status !== "open") {
    $("#r-view-count").textContent = d.participant_count;
    $("#r-canonical").textContent = d.canonical_list;
  }

  if (d.status === "revealed") {
    $("#r-seed").textContent = d.seed;
    const tbody = $("#r-winners-table tbody");
    tbody.innerHTML = "";
    for (const w of d.winners) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${w.rank}</td><td>${w.id}</td><td></td><td></td>`;
      tr.children[2].textContent = w.label;
      tr.children[3].innerHTML = `<code>${w.hash}</code>`;
      tbody.appendChild(tr);
    }
  }
}

$("#r-save").addEventListener("click", async () => {
  let list;
  try {
    list = parseEditor($("#r-editor").value);
  } catch (e) {
    return toast(e.message, true);
  }
  try {
    await api("POST", `/api/rounds/${roundState.id}/participants/set`, {
      admin_token: roundState.token,
      participants: list,
    });
    roundState.dirty = false;
    toast(`已保存 ${list.length} 人`);
    refreshRound();
  } catch (e) {
    toast(e.message, true);
  }
});

$("#r-seal").addEventListener("click", async () => {
  let list;
  try {
    list = parseEditor($("#r-editor").value);
  } catch (e) {
    return toast(e.message, true);
  }
  if (list.length < roundState.data.winner_count) {
    return toast(`人数不足：${list.length} < ${roundState.data.winner_count}，无法封存`, true);
  }
  if (!confirm(`确认封存？\n当前名单共 ${list.length} 人，封存后任何人（含主持人）都不能再修改。`)) return;
  try {
    // 先确保编辑器内容已落库，再封存
    await api("POST", `/api/rounds/${roundState.id}/participants/set`, {
      admin_token: roundState.token,
      participants: list,
    });
    roundState.dirty = false;
    await api("POST", `/api/rounds/${roundState.id}/seal`, { admin_token: roundState.token });
    toast("名单已封存，请截图/抄录名单摘要并公示");
    refreshRound();
  } catch (e) {
    toast(e.message, true);
  }
});

$("#r-reveal-btn").addEventListener("click", async () => {
  const seed = $("#r-seed-input").value.trim().toLowerCase();
  if (!/^[0-9a-f]{32,128}$/.test(seed)) return toast("种子格式应为 32~128 位十六进制", true);
  try {
    const data = await api("POST", `/api/rounds/${roundState.id}/reveal`, {
      admin_token: roundState.token,
      seed,
    });
    if (data.already_revealed) toast("该轮次此前已揭晓，直接展示既有结果，不会重抽");
    else toast("揭晓成功");
    $("#r-seed-input").value = "";
    refreshRound();
  } catch (e) {
    toast(e.message, true);
  }
});

$("#r-download-package").addEventListener("click", () => {
  window.location.href = `/api/rounds/${roundState.id}/package`;
});

$("#r-download-list").addEventListener("click", () => {
  const d = roundState.data;
  const text =
    `现场抽签 · 封存名单快照\n轮次: #${d.id}\n人数: ${d.participant_count}\n` +
    `名单摘要 SHA-256: ${d.participants_digest}\n封存时间 (UTC): ${d.sealed_at}\n` +
    `规范格式: 按编号升序, 每行 编号<TAB>备注, LF 分隔, 无行尾换行\n` +
    `------------------------------------------------------------\n${d.canonical_list}\n`;
  downloadText(`draw-round-${d.id}-snapshot.txt`, text);
});

// ---------------------------------------------------------------- 视图 3: 独立核验

function pickWinnersInBrowser(seed, participants, n) {
  // participants: [{id,label}] —— 与服务端完全相同的确定性算法
  const scored = participants.map((p) => ({ id: p.id, label: p.label }));
  return Promise.all(
    scored.map(async (p) => ({ ...p, hash: await sha256Hex(`${seed}:${p.id}`) }))
  ).then((arr) => {
    arr.sort((a, b) => (a.hash < b.hash ? -1 : a.hash > b.hash ? 1 : a.id - b.id));
    return arr.slice(0, n).map((w, i) => ({ rank: i + 1, id: w.id, label: w.label, hash: w.hash }));
  });
}

$("#v-file").addEventListener("change", (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => verifyPackageText(String(reader.result));
  reader.readAsText(file);
});

$("#v-paste-btn").addEventListener("click", () => {
  verifyPackageText($("#v-paste").value);
});

async function verifyPackageText(text) {
  const report = $("#v-report");
  report.classList.remove("hidden");
  const checks = [];
  const add = (ok, name, detail) => checks.push({ ok, name, detail });

  let pkg;
  try {
    pkg = JSON.parse(text);
  } catch (e) {
    report.innerHTML = `<h3 class="err">核验包不是合法 JSON：${e.message}</h3>`;
    return;
  }

  // 1) 结构检查
  let structuralOk = true;
  const need = ["package_version", "round", "commitment", "participants", "rule", "reveal", "results"];
  for (const k of need) {
    if (!(k in pkg)) { add(false, `缺少字段 package.${k}`); structuralOk = false; }
  }
  if (!structuralOk) return renderReport(false, checks, pkg);

  const hexRe = /^[0-9a-f]{32,128}$/;
  const extCommitment = $("#v-commitment").value.trim().toLowerCase();
  const extDigest = $("#v-digest").value.trim().toLowerCase();

  const seed = String(pkg.reveal.seed || "");
  const storedCommitment = String(pkg.commitment.seed_commitment || "").toLowerCase();
  const storedDigest = String(pkg.participants.digest || "").toLowerCase();
  const canonical = String(pkg.participants.canonical_list || "");
  const n = pkg.round.winner_count;

  add(pkg.round.status === "revealed", "轮次状态为 revealed", `实际: ${pkg.round.status}`);
  add(hexRe.test(seed), "种子为合法十六进制字符串", `长度 ${seed.length}`);
  add(pkg.rule.rule_id === RULE_ID, `规则标识为公开规则 ${RULE_ID}`, `实际: ${pkg.rule.rule_id}`);

  // 2) 承诺: 独立重算 SHA-256(seed) —— 不采信包内任何结果
  let calcCommitment = "";
  if (hexRe.test(seed)) {
    calcCommitment = await sha256Hex(seed);
    add(calcCommitment === storedCommitment,
      "重算承诺 SHA-256(seed) 与包中承诺一致",
      `重算=${calcCommitment}\n包内=${storedCommitment}`);
    if (extCommitment) {
      add(hexRe.test(extCommitment), "外部承诺格式合法");
      add(calcCommitment === extCommitment,
        "重算承诺与【事先留存的承诺】一致",
        `重算=${calcCommitment}\n留存=${extCommitment}`);
    }
  } else {
    add(false, "种子非法，无法重算承诺");
  }

  // 3) 名单: 自行解析规范名单文本, 检查排序/重复, 重算摘要
  let participants = [];
  let listOk = true;
  if (canonical !== "") {
    const lines = canonical.split("\n");
    const ids = [];
    const labelById = new Map();
    for (let i = 0; i < lines.length; i++) {
      const parts = lines[i].split("\t");
      if (parts.length !== 2 || !/^\d+$/.test(parts[0])) {
        add(false, `规范名单第 ${i + 1} 行不符合 '编号<TAB>备注' 格式`, JSON.stringify(lines[i]));
        listOk = false;
        continue;
      }
      const id = parseInt(parts[0], 10);
      if (labelById.has(id)) { add(false, `名单中编号重复: ${id}`); listOk = false; }
      labelById.set(id, parts[1]);
      ids.push(id);
    }
    const sorted = [...ids].sort((a, b) => a - b);
    const isSorted = ids.every((v, i) => v === sorted[i]);
    add(isSorted, "规范名单确按编号数值升序排列");
    if (ids.length !== new Set(ids).size) listOk = false;

    participants = ids.map((id) => ({ id, label: labelById.get(id) }));
    add(pkg.participants.count === participants.length,
      "名单人数字段与实际行数一致",
      `字段=${pkg.participants.count} 实际=${participants.length}`);
    if (pkg.participants.count !== participants.length) listOk = false;

    const calcDigest = await sha256Hex(canonical);
    add(calcDigest === storedDigest,
      "重算名单摘要 SHA-256(canonical_list) 与包中摘要一致",
      `重算=${calcDigest}\n包内=${storedDigest}`);
    if (extDigest) {
      add(hexRe.test(extDigest), "外部名单摘要格式合法");
      add(calcDigest === extDigest,
        "重算名单摘要与【事先留存的名单摘要】一致",
        `重算=${calcDigest}\n留存=${extDigest}`);
    }
  } else {
    add(false, "核验包缺少 canonical_list，无法独立重建名单");
    listOk = false;
  }

  add(participants.length >= n,
    `人数 ${participants.length} ≥ 中奖名额 ${n}（封存前置条件）`);

  // 4) 结果: 完全独立重算, 而不是比对包内中奖列表
  let winnersOk = true;
  const storedWinners = pkg.results && pkg.results.winners;
  if (hexRe.test(seed) && listOk && participants.length >= n && Array.isArray(storedWinners)) {
    const expected = await pickWinnersInBrowser(seed, participants, n);

    add(pkg.results.winner_count === n,
      "结果中的 winner_count 与轮次名额一致",
      `结果=${pkg.results.winner_count} 名额=${n}`);
    add(storedWinners.length === n,
      "中奖结果数量等于名额且编号不重复",
      `结果数=${storedWinners.length} 名额=${n}`);

    if (storedWinners.length !== new Set(storedWinners.map((w) => w.id)).size) {
      add(false, "中奖编号存在重复");
      winnersOk = false;
    }

    const labelById = new Map(participants.map((p) => [p.id, p.label]));
    for (const w of storedWinners) {
      if (!labelById.has(w.id)) {
        add(false, `中奖编号 ${w.id} 不在名单内`);
        winnersOk = false;
        continue;
      } else if (labelById.get(w.id) !== w.label) {
        add(false, `中奖编号 ${w.id} 的备注与名单快照不一致`,
          `包内结果备注=${JSON.stringify(w.label)} 名单备注=${JSON.stringify(labelById.get(w.id))}`);
        winnersOk = false;
      }
      const calc = await sha256Hex(`${seed}:${w.id}`);
      if (calc !== String(w.hash || "").toLowerCase()) {
        add(false, `编号 ${w.id} 的 hash 重算不符`, `重算=${calc}\n包内=${w.hash}`);
        winnersOk = false;
      }
    }

    // 核心: 期望中奖集合/排序必须与包内完全一致
    const expIds = expected.map((w) => w.id);
    const gotIds = storedWinners.map((w) => w.id);
    const sameSet = expIds.length === gotIds.length && expIds.every((id, i) => id === gotIds[i]);
    add(sameSet,
      "【关键】按公开规则对全名单独立重算，中奖编号及名次与包内结果完全一致",
      `独立重算=${JSON.stringify(expIds)}\n包内结果=${JSON.stringify(gotIds)}`);
    if (!sameSet) winnersOk = false;

    const ranksOk = storedWinners.every((w, i) => w.rank === i + 1);
    add(ranksOk, "名次 rank 连续且从 1 开始");
  } else {
    add(false, "因前置检查失败，跳过重算（结果不可信）");
    winnersOk = false;
  }

  const allOk = checks.every((c) => c.ok);
  renderReport(allOk, checks, pkg);
}

function renderReport(allOk, checks) {
  const report = $("#v-report");
  const passed = checks.filter((c) => c.ok).length;
  let html = `<h3 class="${allOk ? "ok" : "err"}">${allOk ? "✅ 核验通过" : "❌ 核验未通过"}（${passed}/${checks.length} 项）</h3>`;
  if (!allOk) {
    html += `<p class="warn">下列不符之处说明核验包被篡改、种子/名单与事先承诺不符，或结果不是按公开规则产生：</p>`;
  }
  for (const c of checks) {
    html += `<div class="check ${c.ok ? "pass" : "fail"}">${c.ok ? "✔" : "✘"} ${escapeHtml(c.name)}`;
    if (!c.ok && c.detail) html += `<pre>${escapeHtml(c.detail)}</pre>`;
    html += `</div>`;
  }
  report.innerHTML = html;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (ch) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
}

// ---------------------------------------------------------------- 启动

route();
