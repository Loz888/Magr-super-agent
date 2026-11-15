"use strict";
const prefix = "/api/v3/source-reviews";
const element = id => document.getElementById(id);
let csrf = "", selected = null;
const statusText = message => { element("status").textContent = message; };
async function request(path, method = "GET", body) {
  const response = await fetch(path, {method, credentials: "same-origin", cache: "no-store",
    headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf},
    body: body === undefined ? undefined : JSON.stringify(body)});
  if (!response.ok) {
    const failure = await response.json();
    throw new Error(failure.code === "AUTH_REQUIRED" ? "请先返回控制台登录。" :
      "操作被阻止：" + (failure.code || response.status));
  }
  return response;
}
async function json(path, method, body) { return (await request(path, method, body)).json(); }
function safe(action) { return async event => {
  if (event) event.preventDefault();
  try { await action(); } catch (error) { statusText(error.message); }
}; }
async function refresh() {
  const data = await json(prefix);
  const list = element("reviews"); list.replaceChildren();
  if (!data.reviews.length) list.textContent = "目前没有待审查产物。";
  for (const review of data.reviews) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "受控修复 · " + review.target.repository + " · " + review.status +
      " · " + review.ticket_id;
    button.addEventListener("click", safe(() => selectReview(review)));
    list.append(button);
  }
  statusText(data.partial ? "显示最近记录，可通过编码记录定位更早产物。" : "审查列表已更新。");
}
async function selectReview(review) {
  if (["proposed", "ready"].includes(review.status) && !review.invalidated_at) return inspect(review);
  selected = {review}; element("detail").hidden = false;
  element("summary").textContent = "受控修复 · " + review.status + " · " + review.target.repository;
  element("facts").textContent = JSON.stringify(review, null, 2);
  element("diff").textContent = "当前动作仅展示无正文的状态与受控回执。";
  element("files").replaceChildren(); element("expires").textContent = "";
  element("approve").disabled = true; element("cancel").disabled = true;
  element("patch").disabled = true; element("tick").disabled = true;
  element("reconcile").disabled = review.status !== "outcome_unknown";
  statusText("fixture:// 为受控回执标识，未创建真实 GitHub PR。");
}
async function inspect(review) {
  selected = null; element("approve").disabled = true;
  const data = await json(prefix + "/" + review.review_id + "/inspect", "POST", {});
  if (!data.complete || !data.bundle || !Array.isArray(data.bundle.files))
    throw new Error("完整审查包不可用，无法批准。");
  selected = data;
  element("detail").hidden = false;
  element("summary").textContent = "受控 Draft PR · " + data.bundle.repository +
    " · 基线 " + data.bundle.base_sha + " · 目标分支 " + data.bundle.branch;
  element("facts").textContent = JSON.stringify({ticket: data.review.ticket_id,
    run: data.review.run_id, tested_tree: data.bundle.tree_hash,
    verification: data.review.verification, payload_hash: data.bundle.payload_hash,
    action_hash: data.review.action_sha256, expires_at: data.review.expires_at}, null, 2);
  element("diff").textContent = data.bundle.diff;
  const files = element("files"); files.replaceChildren();
  for (const file of data.bundle.files) {
    const heading = document.createElement("h3"), content = document.createElement("pre");
    heading.textContent = file.path + " · 完整修改后内容";
    content.textContent = file.content; files.append(heading, content);
  }
  element("approve").disabled = !data.write_configured;
  element("cancel").disabled = false; element("patch").disabled = false;
  element("reconcile").disabled = true; element("tick").disabled = review.status !== "ready";
  element("expires").textContent = data.write_configured ?
    "本次完整展示的批准期限：" + data.presentation.expires_at :
    "等待单独写权限。当前可以审查和下载补丁。";
  statusText("完整 Diff、变化文件与验证事实已呈现，请核对后批准。");
}
function exact() {
  if (!selected) throw new Error("请先打开完整审查包。");
  return {bundle_sha256: selected.review.bundle_sha256,
    action_sha256: selected.review.action_sha256};
}
element("refresh").addEventListener("click", safe(refresh));
element("prepare").addEventListener("submit", safe(async () => {
  await json(prefix, "POST", {request_id: crypto.randomUUID(), coding_id: element("coding").value.trim(),
    verification_id: element("verification").value.trim(), expected_epoch: Number(element("epoch").value),
    expected_input_revision: Number(element("revision").value), profile_sha256: element("profile").value});
  await refresh();
}));
element("approve").addEventListener("click", safe(async () => {
  const body = {...exact(), presentation_id: selected.presentation.presentation_id};
  await json(prefix + "/" + selected.review.review_id + "/approve", "POST", body);
  element("approve").disabled = true; element("tick").disabled = false;
  await refresh(); statusText("此精确审查包已批准，等待受控派发。");
}));
element("cancel").addEventListener("click", safe(async () => {
  const body = exact();
  await json(prefix + "/" + selected.review.review_id + "/cancel", "POST", body);
  selected = null; element("detail").hidden = true; await refresh();
}));
element("patch").addEventListener("click", safe(async () => {
  exact(); const response = await request(prefix + "/" + selected.review.review_id + "/patch");
  const url = URL.createObjectURL(await response.blob()), link = document.createElement("a");
  link.href = url; link.download = "review.patch"; link.click(); URL.revokeObjectURL(url);
}));
element("reconcile").addEventListener("click", safe(async () => {
  exact(); const result = await json(prefix + "/" + selected.review.review_id + "/reconcile", "POST", {});
  statusText(result.reconciled ? "受控回执核对成功，未重复发送。" : "回执仍未知，未发送新请求。");
}));
element("tick").addEventListener("click", safe(async () => {
  const result = await json(prefix + "/tick", "POST", {limit: 8});
  await refresh(); statusText(result.reason_code || "受控动作处理完成。真实发布仍未运行。");
}));
setInterval(() => {
  if (selected && selected.presentation && Date.now() >= Date.parse(selected.presentation.expires_at)) {
    element("approve").disabled = true;
    element("expires").textContent = "本次展示已过期，请重新打开并审查完整内容。";
  }
}, 1000);
safe(async () => {
  const session = await json("/api/v3/auth/session");
  if (!session.authenticated) throw new Error("请先返回控制台登录。");
  csrf = session.csrf_token;
  const capabilities = await json(prefix + "/capabilities");
  for (const profile of capabilities.profiles) {
    const option = document.createElement("option"); option.value = profile;
    option.textContent = profile; element("profile").append(option);
  }
  await refresh();
})();
