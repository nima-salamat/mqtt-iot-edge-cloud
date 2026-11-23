(() => {
  const $ = (id) => document.getElementById(id);
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  let toastTimer;
  let latestEvents = "";

  function toast(message, error = false) {
    const el = $("toast");
    el.textContent = message;
    el.classList.toggle("error", error);
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), 3200);
  }

  function localTime(value) {
    if (!value) return "—";
    return new Date(value).toLocaleTimeString("fa-IR", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  async function post(path, body = {}) {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: JSON.stringify(body),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    return result;
  }

  function renderEvents(events) {
    const signature = events.map((item) => item.at).join("|");
    if (signature === latestEvents) return;
    latestEvents = signature;
    const list = $("activity-list");
    list.replaceChildren();
    if (!events.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "هنوز رویدادی ثبت نشده است.";
      list.append(empty);
      return;
    }
    for (const event of events) {
      const item = document.createElement("article");
      item.className = "activity";
      const content = document.createElement("div");
      const title = document.createElement("b");
      title.textContent = event.title;
      const payload = document.createElement("p");
      payload.textContent = JSON.stringify(event.payload);
      content.append(title, payload);
      const time = document.createElement("time");
      time.textContent = localTime(event.at);
      item.append(content, time);
      list.append(item);
    }
  }

  async function refresh() {
    try {
      const response = await fetch("/api/state/", { cache: "no-store" });
      if (response.status === 401 || response.redirected) {
        location.href = "/login/";
        return;
      }
      const state = await response.json();
      $("connection-status").textContent = state.connected ? "متصل به VPS" : "قطع اتصال";
      $("connection-status").style.color = state.connected ? "#54db9a" : "#ff8997";
      $("output-status").textContent = state.output_state === "on" ? "روشن" : "خاموش";
      $("gpio-detail").textContent = state.gpio_enabled ? "LED واقعی روی GPIO17 فعال است" : "خروجی نرم‌افزاری (GPIO غیرفعال)";
      $("last-publish").textContent = localTime(state.last_publish_at);
      renderEvents(state.messages || []);
    } catch (error) {
      $("connection-status").textContent = "خطای دریافت وضعیت";
      $("connection-status").style.color = "#ff8997";
    }
  }

  $("send-message").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const message = $("message-input").value.trim();
    if (!message) return toast("ابتدا متن پیام را وارد کن.", true);
    button.disabled = true;
    try {
      await post("/api/send/", { message });
      $("message-input").value = "";
      toast("پیام با موفقیت روی Topic رزبری‌پای منتشر شد.");
      refresh();
    } catch (error) { toast(error.message, true); }
    finally { button.disabled = false; }
  });

  $("send-sample").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const result = await post("/api/sample/");
      $("sample-temperature").innerHTML = `${Number(result.payload.temperature_c).toLocaleString("fa-IR")}<small>°C</small>`;
      $("sample-humidity").innerHTML = `${Number(result.payload.humidity_percent).toLocaleString("fa-IR")}<small>%</small>`;
      toast("داده آزمایشی سنسور به VPS ارسال شد.");
      refresh();
    } catch (error) { toast(error.message, true); }
    finally { button.disabled = false; }
  });

  async function setOutput(state, button) {
    button.disabled = true;
    try {
      await post("/api/output/", { state });
      toast(state === "on" ? "خروجی روشن و رویداد آن ارسال شد." : "خروجی خاموش و رویداد آن ارسال شد.");
      refresh();
    } catch (error) { toast(error.message, true); }
    finally { button.disabled = false; }
  }
  $("local-on").addEventListener("click", (event) => setOutput("on", event.currentTarget));
  $("local-off").addEventListener("click", (event) => setOutput("off", event.currentTarget));

  refresh();
  setInterval(refresh, 2000);
})();
