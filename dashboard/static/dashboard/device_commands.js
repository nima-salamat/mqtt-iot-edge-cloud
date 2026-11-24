(() => {
  'use strict';

  const panel = document.getElementById('remote-device-controls');
  const button = document.getElementById('remote-recording-toggle');
  const status = document.getElementById('remote-command-status');
  const description = document.getElementById('remote-recording-description');
  const mqttBadge = document.getElementById('remote-mqtt-state');
  const csrf = document.querySelector('#csrf-token-form input[name=csrfmiddlewaretoken]')?.value || '';
  if (!panel || !button || !status) return;

  const baseUrl = panel.dataset.commandUrl;
  const deviceId = panel.dataset.deviceId;
  let busy = false;
  let canControl = false;
  const wait = ms => new Promise(resolve => window.setTimeout(resolve, ms));

  async function fetchWithTimeout(url, options = {}, timeoutMs = 8000) {
    const controller = new AbortController();
    let timedOut = false;
    const timer = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, timeoutMs);
    try {
      return await fetch(url, {...options, signal: controller.signal});
    } catch (error) {
      if (timedOut) {
        throw new Error('سرور در مهلت تعیین‌شده پاسخ نداد؛ وضعیت اتصال را دوباره بررسی کن.');
      }
      throw error;
    } finally {
      window.clearTimeout(timer);
    }
  }

  async function refreshConnectivity() {
    try {
      const response = await fetchWithTimeout(
        '/api/dashboard/overview/', {cache: 'no-store'}, 6000
      );
      if (!response.ok) return;
      const overview = await response.json();
      const device = (overview.devices || []).find(item => item.device_id === deviceId);
      if (!device) return;
      canControl = !!(
        device.is_paired
        && device.is_online
        && device.mqtt_connected
        && device.mqtt_command_subscription_ready
        && device.camera_ready
        && device.recording_supported
      );
      if (!busy) button.disabled = !canControl;
      if (mqttBadge) {
        mqttBadge.classList.toggle('online', !!device.mqtt_connected);
        mqttBadge.classList.toggle('offline', !device.mqtt_connected);
        mqttBadge.replaceChildren(
          Object.assign(document.createElement('i'), {}),
          document.createTextNode(device.mqtt_connected ? 'MQTT متصل' : 'MQTT قطع')
        );
      }
      if (!canControl && !busy) {
        status.textContent = device.is_paired
          ? 'برای کنترل از راه دور، دوربین و کانال MQTT باید آنلاین باشند.'
          : 'اتصال دوربین لغو شده است؛ ابتدا آن را دوباره Pair کن.';
      } else if (canControl && !busy) {
        status.textContent = 'اتصال MQTT و اشتراک فرمان آماده است.';
      } else if (device.is_online && device.mqtt_connected && !device.mqtt_command_subscription_ready && !busy) {
        status.textContent = 'MQTT متصل است، اما اشتراک فرمان هنوز تأیید نشده است.';
      } else if (device.is_online && device.mqtt_command_subscription_ready && !device.camera_ready && !busy) {
        status.textContent = 'دوربین هنوز فریم تازه تولید نمی‌کند یا آماده ضبط نیست.';
      } else if (device.is_online && device.camera_ready && !device.recording_supported && !busy) {
        status.textContent = 'backend فعلی دوربین از ضبط MP4 پشتیبانی نمی‌کند؛ Picamera2 لازم است.';
      }
    } catch (_) {
      canControl = false;
      if (!busy) {
        button.disabled = true;
        status.textContent = 'وضعیت دوربین از سرور دریافت نشد؛ برای جلوگیری از ارسال فرمان روی وضعیت قدیمی، کنترل موقتاً غیرفعال شد.';
      }
    }
  }

  async function readCommand(commandId) {
    const response = await fetchWithTimeout(
      baseUrl + encodeURIComponent(commandId) + '/',
      {
        cache: 'no-store',
        headers: {'Accept': 'application/json'}
      },
      6000
    );
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || 'وضعیت فرمان قابل دریافت نیست.');
    return payload;
  }

  function updateRecordingState(enabled) {
    const node = document.getElementById('recording-state');
    if (node) node.textContent = enabled ? 'فعال' : 'متوقف';
    if (description) description.textContent = enabled ? 'ضبط در حال اجرا است.' : 'ضبط متوقف است.';
    button.dataset.enabled = enabled ? 'false' : 'true';
    button.textContent = enabled ? 'توقف ضبط از راه دور' : 'شروع ضبط از راه دور';
  }

  button.addEventListener('click', async () => {
    if (busy || !canControl) return;
    const enabled = button.dataset.enabled === 'true';
    busy = true;
    button.disabled = true;
    status.textContent = 'در حال ارسال فرمان امضاشده به Broker…';
    try {
      const response = await fetchWithTimeout(baseUrl, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRFToken': csrf,
          'Accept': 'application/json'
        },
        body: JSON.stringify({
          command_type: 'recording.set',
          payload: {enabled}
        })
      }, 15000);
      const accepted = await response.json();
      if (!response.ok) throw new Error(accepted.error || 'ارسال فرمان ناموفق بود.');
      status.textContent = 'فرمان به Broker تحویل شد؛ منتظر تأیید اجرای Raspberry Pi هستیم…';

      const deadline = Date.now() + 30000;
      let finalState = null;
      while (Date.now() < deadline) {
        await wait(850);
        finalState = await readCommand(accepted.command_id);
        if (finalState.status === 'applied') break;
        if (finalState.status === 'failed' || finalState.status === 'expired') {
          throw new Error(finalState.result?.error || 'دستگاه فرمان را اجرا نکرد یا مهلت آن تمام شد.');
        }
      }

      if (!finalState || finalState.status !== 'applied') {
        status.textContent = 'فرمان ارسال شده، اما تأیید دستگاه در زمان انتظار دریافت نشد. وضعیت آن در سرور نگه‌داری شده است.';
      } else {
        const applied = finalState.result?.recording_enabled;
        updateRecordingState(typeof applied === 'boolean' ? applied : enabled);
        status.textContent = 'فرمان با موفقیت اجرا و توسط دستگاه تأیید شد.';
      }
    } catch (error) {
      status.textContent = error.message || 'ارتباط با سرور ناموفق بود.';
    } finally {
      busy = false;
      button.disabled = !canControl;
    }
  });

  refreshConnectivity();
  window.setInterval(refreshConnectivity, 5000);
})();