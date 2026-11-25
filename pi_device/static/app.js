(() => {
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';

  async function fetchWithTimeout(url, options = {}, timeoutMs = 12000) {
    const controller = new AbortController();
    let timedOut = false;
    const timer = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, timeoutMs);
    try {
      return await fetch(url, {...options, signal: controller.signal});
    } catch (error) {
      if (timedOut) throw new Error('درخواست به سرور در مهلت تعیین‌شده پاسخ نداد.');
      throw error;
    } finally {
      window.clearTimeout(timer);
    }
  }

  let stateRefreshInFlight = false;
  let alarmRefreshInFlight = false;
  const $ = (id) => document.getElementById(id);
  const text = (id, value) => { const node = $(id); if (node) node.textContent = value ?? '—'; };
  const boolLabel = (value) => value ? 'متصل' : 'قطع';

  function badgeText(id, value) {
    const node = $(id);
    if (!node) return;
    const indicator = node.querySelector('i');
    if (indicator) node.replaceChildren(indicator, document.createTextNode(' ' + value));
    else node.textContent = value;
  }

  function askConfirmation(title, message, acceptLabel = 'ادامه') {
    const dialog = $('confirm-dialog');
    if (!dialog || typeof dialog.showModal !== 'function') {
      return Promise.resolve(window.confirm(title + '\n\n' + message));
    }
    return new Promise((resolve) => {
      const accept = $('confirm-accept');
      const cancel = $('confirm-cancel');
      $('confirm-title').textContent = title;
      $('confirm-message').textContent = message;
      accept.textContent = acceptLabel;
      let settled = false;
      const finish = (approved) => {
        if (settled) return;
        settled = true;
        resolve(approved);
      };
      dialog.addEventListener('close', () => {
        finish(dialog.returnValue === 'confirm');
      }, {once: true});
      accept.onclick = () => dialog.close('confirm');
      cancel.onclick = () => dialog.close('cancel');
      dialog.showModal();
    });
  }
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

  function setBidiParts(id, parts) {
    const node = $(id);
    if (!node) return;
    node.replaceChildren();
    for (const part of parts) {
      const item = document.createElement(part.dir ? 'bdi' : 'span');
      if (part.dir) item.dir = part.dir;
      if (part.className) item.className = part.className;
      item.textContent = part.text;
      node.append(item);
    }
  }

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
    if (!gallery || alarmRefreshInFlight) return;
    alarmRefreshInFlight = true;
    try {
      const response = await fetchWithTimeout('/api/alarms/', {cache: 'no-store'});
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
        const menu = document.createElement('details');
        menu.className = 'alarm-card-menu';
        const menuSummary = document.createElement('summary');
        menuSummary.textContent = '⋮';
        menuSummary.setAttribute('aria-label', 'عملیات تشخیص');
        const deleteButton = document.createElement('button');
        deleteButton.type = 'button';
        deleteButton.className = 'alarm-delete-action';
        deleteButton.dataset.deleteAlarm = alertId;
        deleteButton.textContent = 'حذف این تشخیص';
        menu.append(menuSummary, deleteButton);
        heading.append(title, confidence, menu);

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
    } finally {
      alarmRefreshInFlight = false;
    }
  }

  const alarmGallery = $('alarm-gallery');
  alarmGallery?.addEventListener('click', async (event) => {
    const button = event.target.closest('[data-delete-alarm]');
    if (!button) return;
    const alertId = button.dataset.deleteAlarm;
    if (!(await askConfirmation(
      'حذف تصویر تشخیص',
      'تصویر، جزئیات و فایل‌های صف ارسال این تشخیص از Raspberry Pi حذف می‌شوند. نسخه‌ای که قبلاً به سرور ارسال شده از سرور حذف نخواهد شد.',
      'حذف تشخیص'
    ))) return;
    button.disabled = true;
    try {
      const response = await fetchWithTimeout('/api/alarms/' + encodeURIComponent(alertId) + '/', {
        method: 'DELETE',
        headers: {'X-CSRF-Token': csrf, 'Accept': 'application/json'}
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'حذف تشخیص ناموفق بود.');
      alarmGallery.querySelector('[data-alert-id="' + CSS.escape(alertId) + '"]')?.remove();
      if (!alarmGallery.querySelector('[data-alert-id]')) {
        const empty = document.createElement('div');
        empty.className = 'gallery-empty';
        empty.textContent = 'هنوز تصویری از تشخیص انسان ثبت نشده است.';
        alarmGallery.replaceChildren(empty);
      }
      text('queue-action-status', 'تشخیص حذف شد؛ ' + formatBytes(result.removed_bytes) + ' فضا آزاد شد.');
      await refreshState();
      await refreshAlarms();
    } catch (error) {
      text('queue-action-status', error.message);
      button.disabled = false;
    }
  });

  $('cleanup-alarms')?.addEventListener('click', async () => {
    const button = $('cleanup-alarms');
    if (!(await askConfirmation(
      'پاک‌سازی همه تشخیص‌های محلی',
      'تمام تصاویر و metadata تشخیص روی Raspberry Pi حذف می‌شوند؛ چه ارسال شده باشند چه هنوز در صف باشند. تاریخچه‌ای که قبلاً در پنل وب ذخیره شده حذف نمی‌شود.',
      'پاک‌سازی همه'
    ))) return;
    button.disabled = true;
    try {
      const response = await fetchWithTimeout('/api/alarms/cleanup/', {
        method: 'POST',
        headers: {'X-CSRF-Token': csrf, 'Accept': 'application/json'}
      }, 30000);
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'پاک‌سازی تشخیص‌ها ناموفق بود.');
      text('queue-action-status', Number(result.removed_count || 0).toLocaleString('fa-IR') +
        ' تشخیص حذف شد؛ ' + formatBytes(result.removed_bytes) + ' فضا آزاد شد.');
      refreshQueueCards(result.remaining || {});
      await refreshAlarms();
      await refreshState();
    } catch (error) {
      text('queue-action-status', error.message);
    } finally {
      button.disabled = false;
    }
  });

  async function refreshState() {
    if (stateRefreshInFlight) return;
    stateRefreshInFlight = true;
    try {
      const response = await fetchWithTimeout('/api/state/', {cache: 'no-store'}, 7000);
      if (!response.ok) throw new Error('دریافت وضعیت دستگاه ناموفق بود.');
      const state = await response.json();
      const mqtt = state.mqtt || {};
      const system = state.system || {};
      const cpuPercent = system.cpu_percent === null || system.cpu_percent === undefined
        ? null : Number(system.cpu_percent);
      const temperature = system.temperature_c === null || system.temperature_c === undefined
        ? null : Number(system.temperature_c);
      const memory = system.memory || {};
      const storage = system.storage || {};

      if (cpuPercent !== null && Number.isFinite(cpuPercent)) {
        text('system-cpu', cpuPercent.toLocaleString('fa-IR', {maximumFractionDigits: 1}) + '٪');
        text('system-cpu-note', cpuPercent >= 90
          ? 'بار پردازنده بسیار بالا است'
          : 'مصرف لحظه‌ای پردازنده');
      } else {
        text('system-cpu', 'در حال اندازه‌گیری');
        text('system-cpu-note', 'نمونه‌برداری از /proc/stat');
      }
      const cpuCard = $('system-cpu')?.closest('.metric-card');
      if (cpuCard) cpuCard.classList.toggle('is-warning', cpuPercent !== null && cpuPercent >= 90);

      if (temperature !== null && Number.isFinite(temperature)) {
        text('system-temperature', temperature.toLocaleString('fa-IR', {maximumFractionDigits: 1}) + '°C');
        text('system-temperature-note', temperature >= 80
          ? 'دمای بالا؛ خنک‌کاری و محل نصب را بررسی کن'
          : (temperature >= 70 ? 'دمای دستگاه نسبتاً بالا است' : 'دمای فعلی پردازنده'));
      } else {
        text('system-temperature', 'ناموجود');
        text('system-temperature-note', 'حسگر دما در دسترس نیست');
      }
      const temperatureCard = $('system-temperature')?.closest('.metric-card');
      if (temperatureCard) temperatureCard.classList.toggle('is-warning', temperature !== null && temperature >= 75);

      if (Number.isFinite(Number(memory.total_bytes)) && Number(memory.total_bytes) > 0) {
        text('memory-usage', formatBytes(memory.used_bytes) + ' / ' + formatBytes(memory.total_bytes));
        setBidiParts('memory-usage-note', [
          {text: 'RAM آزاد: '},
          {text: formatBytes(memory.available_bytes), dir: 'ltr'},
          {text: ' · مصرف: '},
          {text: Number(memory.used_percent || 0).toLocaleString('fa-IR', {maximumFractionDigits: 0}) + '٪', dir: 'ltr'}
        ]);
      } else {
        text('memory-usage', 'ناموجود');
        text('memory-usage-note', 'اطلاعات حافظه در دسترس نیست');
      }
      const memoryCard = $('memory-usage')?.closest('.metric-card');
      if (memoryCard) memoryCard.classList.toggle('is-warning', Number(memory.used_percent) >= 90);

      if (Number.isFinite(Number(storage.total_bytes)) && Number(storage.total_bytes) > 0) {
        const usedPercent = Math.max(0, Math.min(100, Number(storage.used_percent) || 0));
        text('storage-usage', formatBytes(storage.free_bytes) + ' آزاد');
        text('storage-usage-note', formatBytes(storage.used_bytes) + ' از ' + formatBytes(storage.total_bytes) +
          ' استفاده شده · ' + usedPercent.toLocaleString('fa-IR', {maximumFractionDigits: 0}) + '٪');
        const progress = $('storage-progress');
        if (progress) progress.style.width = usedPercent + '%';
      } else {
        text('storage-usage', 'ناموجود');
        text('storage-usage-note', 'اطلاعات فضای ذخیره‌سازی در دسترس نیست');
        if ($('storage-progress')) $('storage-progress').style.width = '0%';
      }
      const storageCard = $('storage-usage')?.closest('.metric-card');
      if (storageCard) storageCard.classList.toggle('is-warning', Number(storage.used_percent) >= 90);

      const cameraOnline = !!state.camera_ready && !!state.preview_ready;
      const status = $('camera-status');
      status.classList.toggle('online', cameraOnline);
      status.classList.toggle('offline', !cameraOnline);
      const dot = document.createElement('i');
      status.replaceChildren(dot, document.createTextNode(cameraOnline ? ' دوربین آماده' : ' دوربین آماده نیست'));
      text('video-fps', Number(state.video_fps || 0).toFixed(1) + ' FPS');
      text('video-fps-target', (state.video_fps_auto ? 'انتخاب خودکار · ' : 'هدف دستی · ') +
        Number(state.target_video_fps || state.settings?.video_fps || 30).toLocaleString('fa-IR') +
        ' FPS · ' + (state.camera_resolution || 'وضوح نامشخص'));
      refreshCameraModeControls(state);
      text('inference-fps', Number(state.inference_fps || 0).toFixed(1) + ' FPS');
      const pool = state.inference_pool || {};
      const workers = Number(pool.workers || state.settings?.inference_workers || 0);
      const readyWorkers = Number(pool.ready_workers || 0);
      const busyWorkers = Number(pool.busy_workers || 0);
      const modelLabel = workers > 0 && readyWorkers === workers
        ? 'همه آماده'
        : (readyWorkers > 0 ? readyWorkers + '/' + workers + ' آماده' :
          (state.model_error ? 'خطای بارگذاری' : 'در حال بارگذاری'));
      text('model-status', modelLabel);
      const latency = Number(pool.average_latency_ms || state.inference_latency_ms || 0);
      const workerLatencies = Array.isArray(pool.worker_latency_ms)
        ? pool.worker_latency_ms
        : [];
      const workerBreakdown = workerLatencies.map((value, index) =>
        'W' + (index + 1) + ' ' + Number(value || 0).toFixed(0) + 'ms'
      ).join(' · ');
      const estimatedCapacity = Number(pool.estimated_capacity_fps || 0);
      const droppedFrames = Number(state.inference_dropped_frames || 0);
      text('inference-workers',
        readyWorkers.toLocaleString('fa-IR') + '/' + workers.toLocaleString('fa-IR') +
        ' Worker آماده · ' + busyWorkers.toLocaleString('fa-IR') +
        ' مشغول · میانگین ' + latency.toFixed(0) + ' ms' +
        (workerBreakdown ? ' · ' + workerBreakdown : '') +
        (estimatedCapacity ? ' · ظرفیت تخمینی ' + estimatedCapacity.toFixed(1) + ' FPS' : '') +
        ' · فریم ردشده ' + droppedFrames.toLocaleString('fa-IR')
      );
      const recordingEnabled = !!state.recording_enabled;
      const recordingButton = $('toggle-recording');
      const recordingStatus = $('recording-status');
      if (recordingButton && !recordingButton.dataset.busy) {
        recordingButton.disabled = state.recording_supported === false;
        recordingButton.title = state.recording_supported === false
          ? 'ضبط محلی فعلاً فقط با Picamera2 پشتیبانی می‌شود'
          : '';
        recordingButton.textContent = state.recording_supported === false
          ? 'ضبط نیازمند Picamera2'
          : (recordingEnabled ? 'توقف ضبط' : 'شروع ضبط');
        recordingButton.setAttribute('aria-pressed', String(recordingEnabled));
        recordingButton.classList.toggle('is-recording', recordingEnabled);
        recordingButton.classList.toggle('is-paused', !recordingEnabled);
      }
      if (recordingStatus && !recordingButton?.dataset.busy) {
        recordingStatus.textContent = state.recording_supported === false
          ? 'این منبع فقط برای پیش‌نمایش و YOLO فعال است؛ ضبط/RTSPS هنوز پشتیبانی نمی‌شود.'
          : (recordingEnabled ? 'ضبط محلی فعال است' : 'ضبط محلی متوقف است');
      }
      text('camera-source-status', (state.camera_model || state.camera_backend || '—') +
        (state.camera_source ? ' · ' + state.camera_source : ''));
      text('camera-resolution-status', state.camera_resolution || '—');
      const picameraBackend = state.camera_backend !== 'opencv';
      text('rtsp-status', state.live_publish_supported ? boolLabel(state.rtsp_connected) : 'نیازمند Picamera2');
      text('rtsp-health', state.live_publish_supported ? boolLabel(state.rtsp_connected) : 'نیازمند Picamera2');
      const capabilityNote = $('camera-capability-note');
      if (capabilityNote) capabilityNote.textContent = picameraBackend
        ? 'پیش‌نمایش محلی حداکثر ۱۵ FPS تازه‌سازی می‌شود تا پردازش تصویر اصلی به نرخ ضبط فشار نیاورد. ضبط و RTSPS از انکودر H.264 و نرخ هدف مستقل استفاده می‌کنند؛ وضوح انتخابی ثابت می‌ماند و FPS فقط تا سقف modeهای سازگار با همان وضوح قابل تنظیم است.'
        : 'OpenCV: پیش‌نمایش محلی محدود به ۱۵ FPS است و YOLO از جریان دریافتی استفاده می‌کند؛ برای FPS بالاتر فقط modeهای اعلام‌شده توسط درایور قابل استفاده‌اند. ضبط MP4 و RTSPS سخت‌افزاری در این backend پشتیبانی نمی‌شود.';
      text('mqtt-status', boolLabel(mqtt.mqtt_connected));
      text('heartbeat-status', state.heartbeat_recent
        ? 'فعال'
        : (state.last_heartbeat_at ? 'قدیمی' : 'در انتظار'));
      text('last-heartbeat', 'آخرین Heartbeat: ' + timeLabel(state.last_heartbeat_at));
      const heartbeatNode = $('heartbeat-status');
      heartbeatNode?.classList.toggle('is-warning', !!state.last_heartbeat_at && !state.heartbeat_recent);
      text('pending-alarms', state.pending_alarms ?? 0);
      text('pending-videos', state.pending_video_segments ?? 0);
      text('last-upload', timeLabel(state.last_upload_at));
      const pairingStatus = state.pairing_status || 'نامشخص';
      const pairingCard = $('pair-card');
      const pairedCard = $('paired-connection-card');
      const isPaired = pairingStatus === 'paired';
      if (pairingCard) pairingCard.hidden = isPaired;
      if (pairedCard) pairedCard.hidden = !isPaired;
      pairingCard?.classList.toggle('is-paired', isPaired);
      const trackingSeconds = Number(state.tracking_seconds || 0);
      const trackingLabel = state.tracking_active
        ? 'در حال رهگیری · ' + trackingSeconds.toLocaleString('fa-IR', {maximumFractionDigits: 1}) + ' ثانیه'
        : (trackingSeconds > 0
          ? 'آخرین رهگیری · ' + trackingSeconds.toLocaleString('fa-IR', {maximumFractionDigits: 1}) + ' ثانیه'
          : 'رهگیری فعالی ثبت نشده است');
      text('tracking-duration', trackingLabel);
      text('tracking-duration-note', state.tracking_active
        ? 'مدت حضور پیوسته هدف در تصویر'
        : 'زمان آخرین حضور پیوسته هدف');
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
      badgeText('preview-status', state.preview_ready ? 'تصویر زنده در دسترس است' : 'در انتظار فریم دوربین');
      const errors = $('camera-errors');
      const messages = [state.camera_error, state.model_error, state.rtsp_error].filter(Boolean);
      errors.hidden = messages.length === 0;
      errors.textContent = messages.join(' · ');
    } catch (error) {
      badgeText('camera-status', 'عدم دسترسی به عامل محلی');
      text('preview-status', error.message);
    } finally {
      stateRefreshInFlight = false;
    }
  }

  const previewImage = $('camera-preview');
  const previewFrame = document.querySelector('.preview-frame');
  const reconnectButton = $('preview-reconnect');
  const fullscreenButton = $('preview-fullscreen');

  $('disconnect-camera')?.addEventListener('click', async () => {
    const button = $('disconnect-camera');
    const approved = await askConfirmation(
      'قطع اتصال دوربین از حساب',
      'توکن دستگاه و مجوزهای پخش زنده از سرور باطل می‌شوند. تصویر محلی و تشخیص YOLO باقی می‌مانند و تاریخچه قبلی پاک نمی‌شود.',
      'قطع اتصال'
    );
    if (!approved) return;
    button.disabled = true;
    try {
      const response = await fetchWithTimeout('/api/disconnect/', {
        method: 'POST',
        headers: {'X-CSRF-Token': csrf, 'Accept': 'application/json'}
      }, 25000);
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'قطع اتصال ناموفق بود.');
      text('pairing-state', 'اتصال حساب لغو شد؛ برای اتصال دوباره QR تازه‌ای از پنل وب بساز.');
      await refreshState();
    } catch (error) {
      text('pairing-state', error.message);
      button.disabled = false;
    }
  });

  reconnectButton?.addEventListener('click', () => {
    if (!previewImage) return;
    const source = previewImage.dataset.previewUrl || previewImage.src.split('?')[0];
    previewImage.src = source + '?refresh=' + Date.now();
    badgeText('preview-status', 'در حال بازیابی تصویر زنده…');
  });

  if (fullscreenButton && (!previewFrame || !previewFrame.requestFullscreen)) {
    fullscreenButton.disabled = true;
    fullscreenButton.title = 'تمام‌صفحه در این مرورگر پشتیبانی نمی‌شود';
  }
  fullscreenButton?.addEventListener('click', async () => {
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else await previewFrame.requestFullscreen();
    } catch (error) {
      badgeText('preview-status', 'نمایش تمام‌صفحه در دسترس نیست');
    }
  });
  document.addEventListener('fullscreenchange', () => {
    if (fullscreenButton) fullscreenButton.textContent =
      document.fullscreenElement === previewFrame ? 'خروج از تمام‌صفحه' : 'تمام‌صفحه';
  });

  function updatePreviewClock() {
    text('preview-clock', new Date().toLocaleTimeString('fa-IR'));
  }
  updatePreviewClock();
  window.setInterval(updatePreviewClock, 1000);

  $('toggle-recording').addEventListener('click', async () => {
    const button = $('toggle-recording');
    const current = button.getAttribute('aria-pressed') === 'true';
    const enabled = !current;
    if (current && !(await askConfirmation(
      'توقف ضبط ویدئو',
      'ضبط ویدئو متوقف می‌شود و قطعه نیمه‌تمام حذف خواهد شد. تصویر زنده، تشخیص YOLO و ارسال هشدارها فعال می‌مانند.',
      'توقف ضبط'
    ))) return;
    button.dataset.busy = '1';
    button.disabled = true;
    text('recording-status', enabled ? 'در حال شروع ضبط…' : 'در حال توقف ضبط در مرز فریم کلیدی…');
    try {
      const response = await fetchWithTimeout('/api/recording/', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
        body: JSON.stringify({enabled})
      }, 18000);
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'تغییر وضعیت ضبط ناموفق بود.');
      button.setAttribute('aria-pressed', String(!!result.recording_enabled));
      button.textContent = result.recording_enabled ? 'توقف ضبط' : 'شروع ضبط';
      button.classList.toggle('is-recording', !!result.recording_enabled);
      button.classList.toggle('is-paused', !result.recording_enabled);
      text('recording-status', result.recording_enabled
        ? 'ضبط فعال شد؛ صف ویدئوهای قبلی هم برای ارسال از سر گرفته می‌شود.'
        : 'ضبط و ارسال ویدئو متوقف شد؛ قطعه نیمه‌تمام حذف شد و صف قبلی حفظ شد. پخش زنده و YOLO فعال‌اند.');
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
      const response = await fetchWithTimeout('/api/pair/scan/', {method: 'POST', headers: {'X-CSRF-Token': csrf}});
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
      const response = await fetchWithTimeout('/api/pair/manual/', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
        body: JSON.stringify({pairing_code: pairingCode})
      }, 25000);
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

  const settingsForm = $('settings-form');
  const settingsSaveButton = settingsForm.querySelector('button[type=submit]');
  const settingsDiscardButton = $('settings-discard');
  const confidenceInput = $('alert-confidence');
  const confidenceOutput = $('confidence-output');
  let savedSettings = captureSettings();
  let savingSettings = false;

  function captureSettings() {
    return Object.fromEntries(
      Array.from(settingsForm.querySelectorAll('[name]')).map(input => [input.name, input.value])
    );
  }

  const autoFpsSelect = settingsForm.elements.namedItem('video_fps_auto');
  const manualFpsInput = settingsForm.elements.namedItem('video_fps');
  const resolutionSelect = $('video-resolution');
  const videoWidthInput = settingsForm.elements.namedItem('video_width');
  const videoHeightInput = settingsForm.elements.namedItem('video_height');
  const fpsList = $('camera-fps-options');
  const inferenceFpsInput = settingsForm.elements.namedItem('inference_fps');
  let latestVideoModes = [];
  function parseResolution(value) {
    const match = /^(\d+)x(\d+)$/.exec(String(value || ''));
    return match ? {width: Number(match[1]), height: Number(match[2])} : null;
  }

  function getCompatibleVideoModes(value) {
    const selected = parseResolution(value);
    if (!selected) return [];
    return latestVideoModes.filter(mode => {
      const resolution = parseResolution(mode.resolution);
      return resolution && resolution.width >= selected.width &&
        resolution.height >= selected.height && Number(mode.fps) > 0;
    });
  }

  function updateResolutionFields() {
    const selected = parseResolution(resolutionSelect?.value);
    if (!selected) return;
    if (videoWidthInput) videoWidthInput.value = String(selected.width);
    if (videoHeightInput) videoHeightInput.value = String(selected.height);
    updateCameraFpsLimits();
  }

  function updateCameraFpsLimits() {
    const selectedValue = resolutionSelect?.value || '';
    const compatible = getCompatibleVideoModes(selectedValue);
    const rates = [...new Set(compatible
      .map(mode => Number(mode.fps))
      .filter(rate => Number.isFinite(rate) && rate > 0)
      .map(rate => Math.floor(rate)))].sort((a, b) => a - b);
    const maximum = rates.length ? Math.max(...rates) : 0;
    const inputMaximum = maximum > 0 ? maximum : 1000;

    if (manualFpsInput) manualFpsInput.max = String(inputMaximum);
    if (inferenceFpsInput) inferenceFpsInput.max = String(inputMaximum);
    if (fpsList) {
      fpsList.replaceChildren();
      for (const rate of rates) {
        const option = document.createElement('option');
        option.value = String(rate);
        option.label = rate.toLocaleString('fa-IR') + ' FPS در وضوح ' + selectedValue;
        fpsList.append(option);
      }
    }

    const maxLabel = $('camera-max-fps');
    if (maxLabel) {
      maxLabel.textContent = maximum > 0
        ? maximum.toLocaleString('fa-IR') + ' FPS در ' + selectedValue
        : 'در انتظار دریافت modeهای دوربین';
    }
    const fpsHint = manualFpsInput?.closest('label')?.querySelector('small');
    if (fpsHint) {
      fpsHint.textContent = maximum > 0
        ? 'سقف این وضوح ' + maximum.toLocaleString('fa-IR') +
          ' FPS است. برای نرخ بالاتر، وضوح دیگری را از فهرست پشتیبانی‌شده انتخاب کن.'
        : 'پس از شناسایی دوربین، FPSهای پشتیبانی‌شده برای وضوح انتخابی نمایش داده می‌شوند.';
    }
    const inferenceHint = inferenceFpsInput?.closest('label')?.querySelector('small');
    if (inferenceHint) {
      inferenceHint.textContent = maximum > 0
        ? 'سقف فریم حسگر در این وضوح ' + maximum.toLocaleString('fa-IR') +
          ' FPS است؛ قدرت پردازنده و Workerها ظرفیت نهایی YOLO را پایین‌تر می‌آورند.'
        : 'ظرفیت Workerها و پردازنده نیز نرخ واقعی YOLO را محدود می‌کند.';
    }

    if (maximum > 0 && manualFpsInput &&
        autoFpsSelect?.value === 'false' &&
        Number(manualFpsInput.value) > maximum) {
      manualFpsInput.value = String(maximum);
      text('settings-result', 'نرخ FPS به سقف وضوح انتخاب‌شده (' +
        maximum.toLocaleString('fa-IR') + ') محدود شد.');
    }
  }

  function populateCameraResolutions(modes, preferredValue) {
    if (!resolutionSelect) return;
    latestVideoModes = (Array.isArray(modes) ? modes : [])
      .filter(mode => parseResolution(mode.resolution) && Number(mode.fps) > 0);
    const selectedValue = preferredValue || resolutionSelect.value ||
      (videoWidthInput?.value + 'x' + videoHeightInput?.value);
    const resolutionRates = new Map();

    for (const mode of latestVideoModes) {
      const rate = Number(mode.fps);
      const current = resolutionRates.get(mode.resolution) || 0;
      if (rate > current) resolutionRates.set(mode.resolution, rate);
    }

    const options = [...resolutionRates.keys()]
      .map(resolution => ({resolution, size: parseResolution(resolution)}))
      .sort((a, b) =>
        (b.size.width * b.size.height) - (a.size.width * a.size.height)
      );

    resolutionSelect.replaceChildren();
    for (const {resolution} of options) {
      const compatible = getCompatibleVideoModes(resolution);
      const maximum = compatible.reduce(
        (value, mode) => Math.max(value, Number(mode.fps) || 0), 0
      );
      if (!maximum) continue;
      const option = document.createElement('option');
      option.value = resolution;
      option.textContent = resolution + ' · حداکثر ' +
        maximum.toLocaleString('fa-IR', {maximumFractionDigits: 1}) + ' FPS';
      resolutionSelect.append(option);
    }

    if (selectedValue && !Array.from(resolutionSelect.options)
      .some(option => option.value === selectedValue)) {
      const compatible = getCompatibleVideoModes(selectedValue);
      const maximum = compatible.reduce(
        (value, mode) => Math.max(value, Number(mode.fps) || 0), 0
      );
      const option = document.createElement('option');
      option.value = selectedValue;
      option.textContent = maximum > 0
        ? selectedValue + ' · خروجی مقیاس‌شده از mode حسگر · حداکثر ' +
          maximum.toLocaleString('fa-IR', {maximumFractionDigits: 1}) + ' FPS'
        : selectedValue + ' · در حسگر فعلی پشتیبانی نمی‌شود';
      resolutionSelect.append(option);
    }
    if (selectedValue) resolutionSelect.value = selectedValue;
    updateCameraFpsLimits();
  }

  function refreshCameraModeControls(state) {
    const selectedValue = resolutionSelect?.value ||
      (Number(state.settings?.video_width || 0) + 'x' +
       Number(state.settings?.video_height || 0));
    populateCameraResolutions(state.video_modes, selectedValue);
    if (!latestVideoModes.length) {
      const fallbackMaximum = Number(state.camera_max_fps || 0);
      if (fallbackMaximum > 0) {
        const maximum = Math.max(1, Math.floor(fallbackMaximum));
        if (manualFpsInput) manualFpsInput.max = String(maximum);
        if (inferenceFpsInput) inferenceFpsInput.max = String(maximum);
        if (fpsList) {
          fpsList.replaceChildren();
          for (const rate of (Array.isArray(state.video_fps_options) ? state.video_fps_options : [])) {
            const option = document.createElement('option');
            option.value = String(Math.max(1, Math.floor(Number(rate))));
            option.label = option.value + ' FPS · نرخ گزارش‌شده توسط درایور';
            fpsList.append(option);
          }
        }
        const maxLabel = $('camera-max-fps');
        if (maxLabel) {
          maxLabel.textContent = maximum.toLocaleString('fa-IR') +
            ' FPS · گزارش درایور، نه فهرست کامل modeها';
        }
      }
    }
  }

  function updateVideoFpsControls() {
    if (!autoFpsSelect || !manualFpsInput) return;
    manualFpsInput.disabled = autoFpsSelect.value === 'true';
    manualFpsInput.setAttribute('aria-disabled', String(manualFpsInput.disabled));
  }
  autoFpsSelect?.addEventListener('change', () => {
    updateVideoFpsControls();
    updateSettingsDirty();
  });
  resolutionSelect?.addEventListener('change', () => {
    updateResolutionFields();
    updateSettingsDirty();
  });
  updateVideoFpsControls();
  updateResolutionFields();

  function updateConfidenceOutput() {
    if (confidenceInput && confidenceOutput) {
      confidenceOutput.textContent = Math.round(Number(confidenceInput.value) * 100).toLocaleString('fa-IR') + '٪';
    }
  }

  function updateSettingsDirty() {
    const current = captureSettings();
    const dirty = Object.keys(savedSettings).some(key => current[key] !== savedSettings[key]);
    text('settings-dirty', dirty ? 'تغییرات ذخیره‌نشده داری' : 'همه تنظیمات ذخیره‌اند');
    settingsSaveButton.disabled = !dirty || savingSettings;
    settingsDiscardButton.disabled = !dirty || savingSettings;
    settingsForm.classList.toggle('has-unsaved-changes', dirty);
  }

  settingsForm.addEventListener('input', () => {
    updateConfidenceOutput();
    updateSettingsDirty();
  });
  settingsDiscardButton.addEventListener('click', () => {
    for (const [key, value] of Object.entries(savedSettings)) {
      const input = settingsForm.elements.namedItem(key);
      if (input) input.value = value;
    }
    updateConfidenceOutput();
    updateVideoFpsControls();
    updateResolutionFields();
    text('settings-result', 'تغییرات فرم لغو شد.');
    text('settings-restart-hint', 'مقادیر ذخیره‌شده دوباره بارگذاری شدند.');
    updateSettingsDirty();
  });
  updateConfidenceOutput();
  updateVideoFpsControls();
  updateSettingsDirty();

  async function discoverCameras() {
    const button = $('camera-discover');
    const datalist = $('camera-sources');
    if (!datalist) return;
    if (button) button.disabled = true;
    try {
      const response = await fetchWithTimeout('/api/cameras/', {cache: 'no-store'});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'جست‌وجوی دوربین‌ها ناموفق بود.');
      datalist.replaceChildren();
      const selectedBackend = settingsForm.elements.namedItem('camera_backend')?.value || 'picamera2';
      const available = (result.cameras || []).filter(camera => camera.backend === selectedBackend);
      for (const camera of available) {
        const option = document.createElement('option');
        option.value = camera.source;
        option.label = camera.label || camera.source;
        datalist.append(option);
      }
      text('settings-result', (result.cameras || []).length
        ? (result.cameras.length.toLocaleString('fa-IR') + ' منبع دوربین پیدا شد.')
        : 'دوربینی پیدا نشد؛ می‌توانی شماره یا مسیر منبع را دستی وارد کنی.');
    } catch (error) {
      text('settings-result', error.message);
    } finally {
      if (button) button.disabled = false;
    }
  }
  $('camera-discover')?.addEventListener('click', discoverCameras);
  settingsForm.elements.namedItem('camera_backend')?.addEventListener('change', discoverCameras);
  discoverCameras();

  settingsForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!settingsForm.reportValidity()) return;
    const data = Object.fromEntries(new FormData(settingsForm).entries());
    for (const key of Object.keys(data)) {
      const input = settingsForm.elements.namedItem(key);
      if (key === 'video_resolution') {
        continue;
      } else if (key === 'video_fps_auto') {
        data[key] = data[key] === 'true';
      } else if (key === 'camera_backend' || key === 'camera_source') {
        data[key] = String(data[key] || '').trim();
      } else {
        data[key] = input.type === 'range' || input.step === '0.01'
          ? Number(data[key])
          : Number.parseInt(data[key], 10);
      }
    }

    savingSettings = true;
    updateSettingsDirty();
    text('settings-result', 'در حال ذخیره تنظیمات…');
    try {
      delete data.video_resolution;
      const response = await fetchWithTimeout('/api/settings/', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
        body: JSON.stringify(data)
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'ذخیره تنظیمات ناموفق بود.');
      for (const [key, value] of Object.entries(result.settings || {})) {
        const input = settingsForm.elements.namedItem(key);
        if (input) input.value = value;
      }
      const savedResolution = Number(result.settings?.video_width || videoWidthInput?.value || 0) +
        'x' + Number(result.settings?.video_height || videoHeightInput?.value || 0);
      if (resolutionSelect) resolutionSelect.value = savedResolution;
      updateResolutionFields();
      savedSettings = captureSettings();
      updateConfidenceOutput();
      updateVideoFpsControls();

      const restartLabels = {
        video_width: 'وضوح افقی دوربین',
        video_height: 'وضوح عمودی دوربین',
        video_fps: 'نرخ فریم ویدئو',
        video_bitrate: 'بیت‌ریت ویدئو',
        segment_seconds: 'مدت قطعه ویدئو',
        inference_workers: 'تعداد پردازه‌های YOLO',
        video_fps_auto: 'حالت انتخاب نرخ فریم',
        camera_backend: 'نوع دوربین',
        camera_source: 'منبع دوربین'
      };
      const restartItems = (result.restart_required || []).map(key => restartLabels[key] || key);
      if (restartItems.length) {
        text('settings-result', 'تنظیمات ذخیره شد.');
        text('settings-restart-hint', 'برای اعمال ' + restartItems.join('، ') +
          ' سرویس را با دستور sudo systemctl restart mqtt-iot-pi راه‌اندازی مجدد کن.');
      } else {
        text('settings-result', 'تنظیمات با موفقیت ذخیره شدند.');
        text('settings-restart-hint', 'همه تغییرات قابل اعمال بدون راه‌اندازی مجدد هستند.');
      }
    } catch (error) {
      text('settings-result', error.message);
    } finally {
      savingSettings = false;
      updateSettingsDirty();
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
      const approved = await askConfirmation(
        'پاک‌سازی صف ارسال',
        'این عملیات غیرقابل‌بازگشت است. ' + labels[queue] +
          ' از حافظه Raspberry Pi حذف می‌شوند. فایل‌های ارسال‌شده و در حال ضبط حذف نمی‌شوند.',
        'حذف موارد'
      );
      if (!approved) return;

      button.disabled = true;
      text('queue-action-status', 'در حال پاک‌سازی صف…');
      try {
        const response = await fetchWithTimeout('/api/queue/clear/', {
          method: 'POST',
          headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
          body: JSON.stringify({queue})
        }, 30000);
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
