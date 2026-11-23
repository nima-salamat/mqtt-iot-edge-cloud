(() => {
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
  const $ = (id) => document.getElementById(id);
  const text = (id, value) => { const node = $(id); if (node) node.textContent = value ?? '—'; };
  const boolLabel = (value) => value ? 'متصل' : 'قطع';
  const timeLabel = (value) => {
    if (!value) return '—';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('fa-IR');
  };

  async function refreshState() {
    try {
      const response = await fetch('/api/state/', {cache: 'no-store'});
      if (!response.ok) throw new Error('دریافت وضعیت دستگاه ناموفق بود.');
      const state = await response.json();
      const mqtt = state.mqtt || {};
      const cameraOnline = !!state.camera_ready && !!state.preview_ready;
      const status = $('camera-status');
      status.classList.toggle('online', cameraOnline);
      status.classList.toggle('offline', !cameraOnline);
      const dot = document.createElement('i');
      status.replaceChildren(dot, document.createTextNode(cameraOnline ? ' دوربین آماده' : ' دوربین آماده نیست'));
      text('video-fps', Number(state.video_fps || 0).toFixed(1) + ' FPS');
      text('inference-fps', Number(state.inference_fps || 0).toFixed(1) + ' FPS');
      text('model-status', state.model_loaded ? 'فعال' : 'بارگذاری نشده');
      text('rtsp-status', boolLabel(state.rtsp_connected));
      text('rtsp-health', boolLabel(state.rtsp_connected));
      text('mqtt-status', boolLabel(mqtt.mqtt_connected));
      text('heartbeat-status', state.last_heartbeat_at ? 'فعال' : 'در انتظار');
      text('last-heartbeat', 'آخرین Heartbeat: ' + timeLabel(state.last_heartbeat_at));
      text('pending-alarms', state.pending_alarms ?? 0);
      text('pending-videos', state.pending_video_segments ?? 0);
      text('last-upload', timeLabel(state.last_upload_at));
      text('pairing-state', 'وضعیت اتصال: ' + (state.pairing_status || 'نامشخص'));
      text('device-pair-status', state.pairing_status || 'نامشخص');
      text('uploaded-segments', state.uploaded_segments ?? 0);
      text('model-error', state.model_error || state.camera_error || '—');
      text('upload-error', state.last_upload_error || '—');
      text('preview-status', state.preview_ready ? 'تصویر زنده در دسترس است' : 'در انتظار فریم دوربین');
      const errors = $('camera-errors');
      const messages = [state.camera_error, state.model_error, state.rtsp_error].filter(Boolean);
      errors.hidden = messages.length === 0;
      errors.textContent = messages.join(' · ');
    } catch (error) {
      text('camera-status', 'عدم دسترسی به عامل محلی');
      text('preview-status', error.message);
    }
  }

  $('scan-pairing').addEventListener('click', async () => {
    const button = $('scan-pairing');
    button.disabled = true;
    text('pairing-state', 'دوربین را به سمت QR نمایش‌داده‌شده در پنل وب بگیر…');
    try {
      const response = await fetch('/api/pair/scan/', {method: 'POST', headers: {'X-CSRF-Token': csrf}});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'شروع اسکن QR ناموفق بود.');
      text('pairing-state', 'اسکن فعال شد؛ QR یک‌بارمصرف پنل وب را مقابل دوربین قرار بده.');
    } catch (error) {
      text('pairing-state', error.message);
    } finally {
      button.disabled = false;
      refreshState();
    }
  });

  $('settings-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const data = Object.fromEntries(new FormData(form).entries());
    for (const key of Object.keys(data)) {
      const input = form.elements.namedItem(key);
      data[key] = input.step === '0.01' ? Number(data[key]) : Number.parseInt(data[key], 10);
    }
    const button = form.querySelector('button[type=submit]');
    button.disabled = true;
    text('settings-result', 'در حال ذخیره…');
    try {
      const response = await fetch('/api/settings/', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
        body: JSON.stringify(data)
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'ذخیره تنظیمات ناموفق بود.');
      text('settings-result', result.restart_required?.length
        ? 'ذخیره شد؛ برای اعمال نرخ فریم یا قطعه‌بندی، سرویس را راه‌اندازی مجدد کن.'
        : 'تنظیمات ذخیره شد.');
      for (const [key, value] of Object.entries(result.settings || {})) {
        const input = form.elements.namedItem(key);
        if (input && document.activeElement !== input) input.value = value;
      }
    } catch (error) {
      text('settings-result', error.message);
    } finally {
      button.disabled = false;
      refreshState();
    }
  });

  refreshState();
  window.setInterval(refreshState, 3000);
})();
