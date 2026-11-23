(() => {
  const $ = (id) => document.getElementById(id);
  let toastTimer;
  let previousMessageIds = "";

  function toast(message, error = false) {
    const node = $("toast");
    node.textContent = message;
    node.classList.toggle("error", error);
    node.classList.add("show");
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => node.classList.remove("show"), 3200);
  }

  function formatTime(value) {
    if (!value) return "—";
    const date = new Date(value);
    return date.toLocaleTimeString("fa-IR", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  function formatNumber(value) {
    return Number(value || 0).toLocaleString("fa-IR");
  }

  function renderDevice(device) {
    const badge = $("status-badge");
    badge.classList.toggle("online", Boolean(device.is_online));
    badge.classList.toggle("offline", !device.is_online);
    badge.querySelector("span").textContent = device.is_online ? "آنلاین" : "آفلاین";
    $("device-online").textContent = device.is_online ? "آنلاین" : "آفلاین";
    $("last-seen").textContent = `آخرین ارتباط: ${formatTime(device.last_seen)}`;
    const current = device.state || {};
    if (current.output_state) $("output-state").textContent = current.output_state === "on" ? "روشن" : "خاموش";
  }

  function renderMessages(messages) {
    const identity = messages.map((m) => `${m.topic}:${m.received_at}`).join("|");
    if (identity === previousMessageIds) return;
    previousMessageIds = identity;
    const tbody = $("message-rows");
    tbody.replaceChildren();
    if (!messages.length) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 5;
      cell.className = "empty-state";
      cell.textContent = "هنوز پیامی از رزبری‌پای دریافت نشده است. صفحه محلی رزبری‌پای را باز کن و یک پیام بفرست.";
      row.append(cell);
      tbody.append(row);
      return;
    }

    for (const message of messages) {
      const row = document.createElement("tr");
      const type = document.createElement("td");
      type.textContent = message.topic.split("/").pop();
      const topic = document.createElement("td");
      const topicCode = document.createElement("code");
      topicCode.textContent = message.topic;
      topic.append(topicCode);
      const payload = document.createElement("td");
      const payloadChip = document.createElement("span");
      payloadChip.className = "payload-chip";
      payloadChip.textContent = JSON.stringify(message.payload);
      payloadChip.title = payloadChip.textContent;
      payload.append(payloadChip);
      const qos = document.createElement("td");
      const qosPill = document.createElement("span");
      qosPill.className = "qos-mini";
      qosPill.textContent = String(message.qos);
      qos.append(qosPill);
      const time = document.createElement("td");
      time.textContent = formatTime(message.received_at);
      row.append(type, topic, payload, qos, time);
      tbody.append(row);
    }
  }

  async function refresh() {
    try {
      const response = await fetch("/api/state/", { headers: { Accept: "application/json" }, cache: "no-store" });
      if (response.status === 401 || response.redirected) {
        window.location.href = "/accounts/login/";
        return;
      }
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      $("device-count").textContent = formatNumber(data.devices.length);
      $("message-count").textContent = formatNumber(data.total_messages);
      $("last-sync").textContent = formatTime(data.server_time);
      if (data.devices.length) renderDevice(data.devices[0]);
      renderMessages(data.recent_messages);
    } catch (error) {
      console.error("Cloud dashboard refresh failed", error);
    }
  }

  function getCookie(name) {
    const match = document.cookie.match(new RegExp(`(^| )${name}=([^;]+)`));
    return match ? decodeURIComponent(match[2]) : "";
  }

  async function sendCommand(state, button) {
    button.disabled = true;
    try {
      const response = await fetch("/api/devices/raspberry-pi-01/command/", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": getCookie("csrftoken") },
        body: JSON.stringify({ command: "set_output", state }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "ارسال فرمان ناموفق بود.");
      toast(`فرمان «${state === "on" ? "روشن" : "خاموش"}» با QoS 1 منتشر شد.`);
    } catch (error) {
      toast(error.message || "خطا در ارسال فرمان", true);
    } finally {
      button.disabled = false;
      window.setTimeout(refresh, 600);
    }
  }

  $("output-on").addEventListener("click", (event) => sendCommand("on", event.currentTarget));
  $("output-off").addEventListener("click", (event) => sendCommand("off", event.currentTarget));
  refresh();
  window.setInterval(refresh, 2000);
})();
