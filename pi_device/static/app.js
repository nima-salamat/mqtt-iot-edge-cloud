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

  const formatBytes = (value) => {
    const bytes = Math.max(0, Number(value) || 0);
    if (bytes < 1024) return bytes.toLocaleString('fa-IR') + ' بایت';
    const units = ['KB', 'MB', 'GB', 'TB'];
    let size = bytes;
    let unit = -1;
    do { size /= 1024; unit += 1; } while (size >= 1024 && unit < units.length - 1);
    return size.toLocaleString('fa-IR', {maximumFractionDigits: 1}) + ' ' + units[unit];
  };

  function refreshQueueCards(queues = {}) {
    for (const name of ['videos', 'alarms', 'logs']) {
      const queue = queues[name] || {};
      text('queue-' + name + '-count', Number(queue.count || 0).toLocaleString('fa-IR'));
      text('queue-' + name + '-size', formatBytes(queue.bytes));
      const button = document.querySelector('[data-clear-queue="' + name + '"]');
      if (button) button.disabled = Number(queue.count || 0) === 0;
    }
  }

  async function refreshAlarms() {
    const gallery = $('alarm-gallery');
    if (!gallery) return;
    try {
      const response = await fetch('/api/alarms/', {cache: 'no-store'});
      if (!response.ok) throw new Error('دریافت تصاویر تشخیص ناموفق بود.');
      const data = await response.json();
      const alarms = Array.isArray(data.alarms) ? data.alarms : [];
      text('alarm-gallery-count', alarms.length.toLocaleString('fa-IR') + ' تصویر');

      const existing = new Map(
        Array.from(gallery.querySelectorAll('[data-alert-id]'))
          .map(card => [card.dataset.alertId, card])
      );
      const currentIds = new Set(alarms.map(alarm => String(alarm.alert_id)));

      for (const [id, card] of existing) {
        if (!currentIds.has(id)) card.remove();
      }

      if (!alarms.length) {
        if (!gallery.querySelector('.gallery-empty')) {
          gallery.replaceChildren();
          const empty = document.createElement('div');
          empty.className = 'gallery-empty';
          empty.textContent = 'هنوز تصویری از تشخیص انسان ثبت نشده است.';
          gallery.append(empty);
        }
        return;
      }

      gallery.querySelector('.gallery-empty')?.remove();

      // Keep existing cards and their image elements alive. Only new alert IDs
      // create an <img> request; polling this JSON endpoint never reloads old photos.
      for (const alarm of [...alarms].reverse()) {
        const alertId = String(alarm.alert_id);
        if (existing.has(alertId)) continue;

        const card = document.createElement('article');
        card.className = 'alarm-thumb-card';
        card.dataset.alertId = alertId;

        const link = document.createElement('a');
        link.className = 'alarm-thumb-link';
        link.href = alarm.image_url;
        link.target = '_blank';
        link.rel = 'noopener';
        const image = document.createElement('img');
        image.src = alarm.image_url;
        image.alt = 'تصویر ثبت‌شده از تشخیص انسان';
        image.loading = 'lazy';
        image.decoding = 'async';
        image.onerror = () => {
          image.remove();
          const missing = document.createElement('span');
          missing.className = 'alarm-image-missing';
          missing.textContent = 'تصویر محلی موجود نیست';
          link.append(missing);
        };
        link.append(image);

        const body = document.createElement('div');
        body.className = 'alarm-thumb-body';
        const heading = document.createElement('div');
        heading.className = 'alarm-thumb-heading';
        const title = document.createElement('b');
        title.textContent = 'تشخیص انسان';
        const confidence = document.createElement('span');
        const score = Number(alarm.confidence);
        confidence.textContent = Number.isFinite(score)
          ? (score * 100).toLocaleString('fa-IR', {maximumFractionDigits: 1}) + '٪'
          : '—';
        heading.append(title, confidence);

        const time = document.createElement('small');
        time.textContent = timeLabel(alarm.detected_at);
        const model = document.createElement('small');
        model.textContent = 'مدل: ' + (alarm.model || 'yolov5n-ncnn');
        const status = document.createElement('span');
        status.className = 'alarm-upload-status ' + (
          alarm.published ? 'complete' : (alarm.uploaded ? 'partial' : 'pending')
        );
        status.textContent = alarm.published
          ? 'تصویر و اعلان ارسال شده'
          : (alarm.uploaded ? 'تصویر ذخیره شده؛ اعلان در صف' : 'در انتظار ارسال');

        const details = document.createElement('details');
        const summary = document.createElement('summary');
        summary.textContent = 'جزئیات تشخیص (' + Number(alarm.bbox_count || 0).toLocaleString('fa-IR') + ')';
        const detailText = document.createElement('p');
        detailText.className = 'alarm-box-details';
        const boxes = Array.isArray(alarm.bbox) ? alarm.bbox : [];
        detailText.textContent = boxes.length
          ? boxes.map((box, index) => {
              const coords = ['x1', 'y1', 'x2', 'y2'].map(key =>
                (Number(box[key] || 0) * 100).toFixed(1) + '%'
              ).join(' · ');
              return 'مورد ' + (index + 1) + ': ' + coords;
            }).join(' | ')
          : 'مختصات محدوده تشخیص در دسترس نیست.';
        details.append(summary, detailText);

        body.append(heading, time, model, status, details);
        card.append(link, body);
        gallery.prepend(card);
      }
    } catch (error) {
      const message = document.createElement('div');
      message.className = 'gallery-empty';
      message.textContent = error.message;
      if (!gallery.querySelector('.alarm-thumb-card')) gallery.replaceChildren(message);
    }
  }

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
      const pool = state.inference_pool || {};
      const workers = Number(pool.workers || state.settings?.inference_workers || 0);
      const readyWorkers = Number(pool.ready_workers || 0);
      const busyWorkers = Number(pool.busy_workers || 0);
      const latency = Number(pool.average_latency_ms || state.inference_latency_ms || 0);
      const workerLatencies = Array.isArray(pool.worker_latency_ms)
        ? pool.worker_latency_ms
        : [];
      const workerBreakdown = workerLatencies.map((value, index) =>
        'W' + (index + 1) + ' ' + Number(value || 0).toFixed(0) + 'ms'
      ).join(' · ');
      const estimatedCapacity = Number(pool.estimated_capacity_fps || 0);
      text('inference-workers',
        readyWorkers.toLocaleString('fa-IR') + '/' + workers.toLocaleString('fa-IR') +
        ' Worker آماده · ' + busyWorkers.toLocaleString('fa-IR') +
        ' مشغول · میانگین ' + latency.toFixed(0) + ' ms' +
        (workerBreakdown ? ' · ' + workerBreakdown : '') +
        (estimatedCapacity ? ' · ظرفیت تخمینی ' + estimatedCapacity.toFixed(1) + ' FPS' : '')
      );
      const recordingEnabled = !!state.recording_enabled;
      const recordingButton = $('toggle-recording');
      const recordingStatus = $('recording-status');
      if (recordingButton && !recordingButton.dataset.busy) {
        recordingButton.textContent = recordingEnabled ? 'توقف ضبط' : 'شروع ضبط';
        recordingButton.setAttribute('aria-pressed', String(recordingEnabled));
        recordingButton.classList.toggle('is-recording', recordingEnabled);
        recordingButton.classList.toggle('is-paused', !recordingEnabled);
      }
      if (recordingStatus && !recordingButton?.dataset.busy) {
        recordingStatus.textContent = recordingEnabled ? 'ضبط محلی فعال است' : 'ضبط محلی متوقف است';
      }
      text('rtsp-status', boolLabel(state.rtsp_connected));
      text('rtsp-health', boolLabel(state.rtsp_connected));
      text('mqtt-status', boolLabel(mqtt.mqtt_connected));
      text('heartbeat-status', state.last_heartbeat_at ? 'فعال' : 'در انتظار');
      text('last-heartbeat', 'آخرین Heartbeat: ' + timeLabel(state.last_heartbeat_at));
      text('pending-alarms', state.pending_alarms ?? 0);
      text('pending-videos', state.pending_video_segments ?? 0);
      text('last-upload', timeLabel(state.last_upload_at));
      const pairingStatus = state.pairing_status || 'نامشخص';
      const pairingLabels = {
        not_paired: 'دستگاه هنوز به حسابی متصل نشده است.',
        scanning: 'در حال اسکن QR؛ اگر تا پایان مهلت تشخیص داده نشد، کد را دستی وارد کن.',
        submitting: 'کد دریافت شد؛ در حال اعتبارسنجی توسط سرور…',
        paired: 'اتصال دستگاه با موفقیت انجام شد.',
        expired: 'مهلت اسکن تمام شد و QR تشخیص داده نشد؛ کد را دستی وارد کن یا دوباره اسکن کن.'
      };
      text('pairing-state', pairingLabels[pairingStatus] || (
        pairingStatus.startsWith('failed:') ? 'خطای اتصال: ' + pairingStatus.slice(7) : 'وضعیت اتصال: ' + pairingStatus
      ));
      text('device-pair-status', pairingStatus);
      text('uploaded-segments', state.uploaded_segments ?? 0);
      refreshQueueCards(state.queues || {});
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

  $('toggle-recording').addEventListener('click', async () => {
    const button = $('toggle-recording');
    const current = button.getAttribute('aria-pressed') === 'true';
    const enabled = !current;
    button.dataset.busy = '1';
    button.disabled = true;
    text('recording-status', enabled ? 'در حال شروع ضبط…' : 'در حال توقف ضبط در مرز فریم کلیدی…');
    try {
      const response = await fetch('/api/recording/', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
        body: JSON.stringify({enabled})
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'تغییر وضعیت ضبط ناموفق بود.');
      button.setAttribute('aria-pressed', String(!!result.recording_enabled));
      button.textContent = result.recording_enabled ? 'توقف ضبط' : 'شروع ضبط';
      button.classList.toggle('is-recording', !!result.recording_enabled);
      button.classList.toggle('is-paused', !result.recording_enabled);
      text('recording-status', result.recording_enabled
        ? 'ضبط محلی فعال است'
        : 'ضبط محلی متوقف است؛ پخش زنده و تشخیص همچنان فعال‌اند.');
      await refreshState();
    } catch (error) {
      text('recording-status', error.message);
    } finally {
      delete button.dataset.busy;
      button.disabled = false;
      refreshState();
    }
  });

  $('scan-pairing').addEventListener('click', async () => {
    const button = $('scan-pairing');
    button.disabled = true;
    text('pairing-state', 'دوربین را به سمت QR نمایش‌داده‌شده در پنل وب بگیر…');
    try {
      const response = await fetch('/api/pair/scan/', {method: 'POST', headers: {'X-CSRF-Token': csrf}});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'شروع اسکن QR ناموفق بود.');
      text('pairing-state', 'اسکن فعال شد؛ QR یک‌بارمصرف پنل وب را مقابل دوربین قرار بده. اگر تشخیص نداد، کد را دستی وارد کن.');
    } catch (error) {
      text('pairing-state', error.message);
    } finally {
      button.disabled = false;
      refreshState();
    }
  });

  $('manual-pairing-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const input = $('manual-pairing-code');
    const button = $('submit-manual-pairing');
    const pairingCode = input.value.trim();
    if (!pairingCode) {
      text('pairing-state', 'ابتدا کد اتصال را وارد کن.');
      input.focus();
      return;
    }

    button.disabled = true;
    text('pairing-state', 'در حال ارسال کد اتصال به سرور…');
    try {
      const response = await fetch('/api/pair/manual/', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
        body: JSON.stringify({pairing_code: pairingCode})
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'ارسال کد اتصال ناموفق بود.');
      input.value = '';
      text('pairing-state', 'کد پذیرفته شد؛ در حال تکمیل Pairing با سرور…');
      await refreshState();
    } catch (error) {
      text('pairing-state', error.message);
    } finally {
      button.disabled = false;
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
        ? 'ذخیره شد؛ برای اعمال نرخ فریم، بیت‌ریت، تعداد پردازه‌های YOLO یا قطعه‌بندی، سرویس را راه‌اندازی مجدد کن.'
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

  document.querySelectorAll('[data-clear-queue]').forEach((button) => {
    button.addEventListener('click', async () => {
      const queue = button.dataset.clearQueue;
      const labels = {
        videos: 'همه قطعه‌های ویدئویی که هنوز آپلود نشده‌اند',
        alarms: 'هشدارها و تصاویرشان که اعلان نهایی آن‌ها هنوز ارسال نشده است',
        logs: 'لاگ‌هایی که هنوز به سرور ارسال نشده‌اند'
      };
      if (!labels[queue]) return;
      const approved = window.confirm(
        'پاک‌سازی غیرقابل‌بازگشت است.\n' + labels[queue] +
        ' از حافظه Raspberry Pi حذف می‌شوند.\nادامه می‌دهی؟'
      );
      if (!approved) return;

      button.disabled = true;
      text('queue-action-status', 'در حال پاک‌سازی صف…');
      try {
        const response = await fetch('/api/queue/clear/', {
          method: 'POST',
          headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
          body: JSON.stringify({queue})
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || 'پاک‌سازی صف ناموفق بود.');
        const count = Number(result.removed_count || 0).toLocaleString('fa-IR');
        text('queue-action-status', count + ' مورد حذف شد؛ حجم آزادشده: ' + formatBytes(result.removed_bytes));
        refreshQueueCards(result.remaining || {});
        if (queue === 'alarms') refreshAlarms();
      } catch (error) {
        text('queue-action-status', error.message);
      } finally {
        button.disabled = false;
        refreshState();
      }
    });
  });

  refreshState();
  refreshAlarms();
  window.setInterval(refreshState, 3000);
  window.setInterval(refreshAlarms, 5000);
})();
