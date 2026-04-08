function setLog(el, lines) {
  if (Array.isArray(lines)) {
    el.textContent = lines.join("\n");
  } else {
    el.textContent = String(lines || "");
  }
}

async function readApiResponse(resp) {
  const text = await resp.text();
  try {
    return { ok: resp.ok, status: resp.status, data: JSON.parse(text) };
  } catch (_err) {
    return { ok: false, status: resp.status, data: { error: `非JSON响应(${resp.status})`, raw: text } };
  }
}

function updateAttemptInputs() {
  const count = Number(document.getElementById("attempt-count").value || 1);
  const secondWrap = document.getElementById("second-wrap");
  const thirdWrap = document.getElementById("third-wrap");
  secondWrap.style.display = count >= 2 ? "flex" : "none";
  thirdWrap.style.display = count >= 3 ? "flex" : "none";
}

function toScheduleString(localValue) {
  if (!localValue) return "";
  const [datePart, timePartRaw] = localValue.split("T");
  if (!datePart || !timePartRaw) return "";
  const timePart = timePartRaw.length === 5 ? `${timePartRaw}:00` : timePartRaw;
  return `${datePart} ${timePart}`;
}

function initEditorToolbar() {
  const toolbar = document.getElementById("editor-toolbar");
  const editor = document.getElementById("body-editor");
  toolbar.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-cmd]");
    if (!btn) return;
    const cmd = btn.dataset.cmd;
    editor.focus();
    if (cmd === "createLink") {
      const url = window.prompt("输入链接URL");
      if (url) document.execCommand(cmd, false, url);
      return;
    }
    document.execCommand(cmd, false, null);
  });
}

function initDateTimePickers() {
  ["first-send-at", "second-send-at", "third-send-at"].forEach((id) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.addEventListener("keydown", (e) => e.preventDefault());
    el.addEventListener("focus", () => {
      if (typeof el.showPicker === "function") el.showPicker();
    });
  });
}

function attemptSummaryLines(attempts) {
  const lines = [];
  (attempts || []).forEach((a) => {
    lines.push(
      `Attempt #${a.attempt_index} | ${a.status} | scheduled=${a.scheduled_for || "-"} | start=${a.started_at || "-"} | end=${a.finished_at || "-"} | ms=${a.duration_ms || "-"} | err=${a.error || "-"}`
    );
  });
  return lines;
}

async function pollTask(taskId, logEl) {
  while (true) {
    const resp = await fetch(`/api/tasks/${taskId}`);
    const parsed = await readApiResponse(resp);
    const data = parsed.data;
    if (!parsed.ok) {
      setLog(logEl, JSON.stringify(data, null, 2));
      return;
    }

    const lines = [
      `任务ID: ${data.id}`,
      `投递ID: ${data.delivery_id}`,
      `状态: ${data.status}`,
      `创建时间: ${data.created_at}`,
      `更新时间: ${data.updated_at}`,
      `时区: ${data.timezone || "Asia/Shanghai"}`,
      "",
      "[Attempts]",
      ...attemptSummaryLines(data.attempts),
      "",
      "[Logs]",
      ...data.logs,
    ];
    setLog(logEl, lines);

    if (["success", "failed", "partial_success"].includes(data.status)) {
      await loadDeliveries();
      return;
    }
    await new Promise((r) => setTimeout(r, 1200));
  }
}

function deliveryCard(item) {
  const div = document.createElement("div");
  div.className = "delivery-item";

  const schedule = (item.schedule || []).filter(Boolean).join(" | ");
  const attempts = attemptSummaryLines(item.attempts || []).join("<br>");

  div.innerHTML = `
    <div><strong>${item.subject || "(无主题)"}</strong></div>
    <div class="meta">
      投递ID: ${item.delivery_id}<br>
      任务ID: ${item.task_id}<br>
      状态: ${item.status}<br>
      创建时间: ${item.created_at}<br>
      目标: ${item.host}:${item.port}<br>
      发件人: ${item.sender}<br>
      收件人: ${item.recipient}<br>
      计划时间: ${schedule}<br>
      成功/失败: ${item.total_success}/${item.total_failed}<br>
      ${attempts}
    </div>
    <div class="actions"><button type="button" data-id="${item.delivery_id}">查看完整时间戳事件</button></div>
  `;

  div.querySelector("button").addEventListener("click", () => showDeliveryEvents(item.delivery_id));
  return div;
}

async function loadDeliveries() {
  const box = document.getElementById("deliveries");
  box.innerHTML = "加载中...";

  const resp = await fetch("/api/deliveries?limit=50");
  const parsed = await readApiResponse(resp);
  const data = parsed.data;
  if (!parsed.ok) {
    box.innerHTML = `<pre>${JSON.stringify(data, null, 2)}</pre>`;
    return;
  }

  box.innerHTML = "";
  if (!data.items || data.items.length === 0) {
    box.textContent = "暂无投递记录";
    return;
  }

  data.items.forEach((item) => box.appendChild(deliveryCard(item)));
}

async function showDeliveryEvents(deliveryId) {
  const logEl = document.getElementById("delivery-events");
  setLog(logEl, `加载投递 ${deliveryId} 的事件中...`);

  const resp = await fetch(`/api/deliveries/${deliveryId}`);
  const parsed = await readApiResponse(resp);
  const data = parsed.data;
  if (!parsed.ok) {
    setLog(logEl, JSON.stringify(data, null, 2));
    return;
  }

  const lines = [
    `投递ID: ${data.delivery_id}`,
    `状态: ${data.status}`,
    `时区: ${data.timezone || "Asia/Shanghai"}`,
    "",
    "[Attempts]",
    ...attemptSummaryLines(data.attempts || []),
    "",
    "[Events]",
  ];

  (data.events || []).forEach((ev, idx) => {
    lines.push(`#${idx + 1} ${ev.ts} | ${ev.event} | attempt=${ev.attempt_index || "-"}`);
    lines.push(JSON.stringify(ev.payload || {}));
  });

  setLog(logEl, lines);
}

document.getElementById("send-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const logEl = document.getElementById("task-log");
  setLog(logEl, "提交任务中...");

  const formData = new FormData(e.target);
  const firstSendAt = toScheduleString(document.getElementById("first-send-at").value);
  const secondSendAt = toScheduleString(document.getElementById("second-send-at").value);
  const thirdSendAt = toScheduleString(document.getElementById("third-send-at").value);
  formData.set("first_send_at", firstSendAt);
  formData.set("second_send_at", secondSendAt);
  formData.set("third_send_at", thirdSendAt);

  const editor = document.getElementById("body-editor");
  formData.set("html_body", editor.innerHTML || "");
  formData.set("body", editor.innerText || "");

  const resp = await fetch("/api/send", { method: "POST", body: formData });
  const parsed = await readApiResponse(resp);
  const data = parsed.data;
  if (!parsed.ok) {
    setLog(logEl, JSON.stringify(data, null, 2));
    return;
  }

  setLog(logEl, [
    "任务创建成功",
    `task_id=${data.task_id}`,
    `delivery_id=${data.delivery_id}`,
    `timezone=${data.timezone}`,
  ]);
  pollTask(data.task_id, logEl);
});

document.getElementById("attempt-count").addEventListener("change", updateAttemptInputs);
document.getElementById("refresh-deliveries").addEventListener("click", loadDeliveries);

initEditorToolbar();
initDateTimePickers();
updateAttemptInputs();
loadDeliveries();
