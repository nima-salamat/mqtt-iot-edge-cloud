(() => {
  'use strict';

  const root = document.querySelector('[data-live-alerts]');
  if (!root) return;

  let cursor = Number.parseInt(root.dataset.alarmCursor || '0', 10);
  if (!Number.isFinite(cursor) || cursor < 0) cursor = 0;

  const deviceId = root.dataset.deviceId || '';
  const toastRegion = document.getElementById('alert-toast-region');
  const notificationButton = document.getElementById('enable-alert-notifications');
  const notificationStatus = document.getElementById('alert-notification-status');
  const homeBody = document.getElementById('recent-alarm-body');
  const detailGrid = root.querySelector('[data-live-alert-grid]');
  const seen = new Set(
    Array.from(document.querySelectorAll('[data-alarm-id]'))
      .map((node) => Number.parseInt(node.dataset.alarmId || '0', 10))
      .filter((id) => Number.isFinite(id) && id > 0)
  );

  const formatTime = (value) => {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? 'زمان نامشخص' : date.toLocaleString('fa-IR');
  };

  const makeCell = (text, className) => {
    const cell = document.createElement('td');
    if (className) cell.className = className;
    cell.textContent = text;
    return cell;
  };

  function addHomeRow(alert) {
    if (!homeBody) return;
    homeBody.querySelector('.empty')?.closest('tr')?.remove();

    const row = document.createElement('tr');
    row.dataset.alarmId = String(alert.id);

    const imageCell = document.createElement('td');
    imageCell.className = 'alarm-image-cell';
    if (alert.image_url) {
      const link = document.createElement('a');
      link.href = alert.image_url;
      link.target = '_blank';
      link.rel = 'noopener';
      const image = document.createElement('img');
      image.className = 'alarm-thumb';
      image.src = alert.image_url;
      image.alt = 'تصویر هشدار جدید';
      image.loading = 'lazy';
      image.decoding = 'async';
      link.append(image);
      imageCell.append(link);
    } else {
      imageCell.textContent = '—';
    }

    const eventCell = makeCell('● تشخیص انسان', 'event-type');
    const deviceCell = document.createElement('td');
    const deviceLink = document.createElement('a');
    deviceLink.href = alert.device_url;
    deviceLink.textContent = alert.device_name;
    deviceCell.append(deviceLink);

    const confidenceCell = makeCell(Number(alert.confidence || 0).toFixed(2));
    const timeCell = makeCell(formatTime(alert.detected_at));
    const stateCell = document.createElement('td');
    const state = document.createElement('span');
    state.className = 'state warning';
    state.textContent = 'جدید';
    stateCell.append(state);

    row.append(imageCell, eventCell, deviceCell, confidenceCell, timeCell, stateCell);
    homeBody.prepend(row);

    while (homeBody.querySelectorAll('tr[data-alarm-id]').length > 12) {
      homeBody.querySelector('tr[data-alarm-id]:last-of-type')?.remove();
    }
    const count = document.getElementById('alarm-count');
    if (count) {
      count.textContent = homeBody.querySelectorAll('tr[data-alarm-id]').length.toLocaleString('fa-IR');
    }
  }

  function addDetailCard(alert) {
    if (!detailGrid) return;
    detailGrid.querySelector('.empty-card')?.remove();

    const card = document.createElement('article');
    card.className = 'alarm-card live-new-alarm';
    card.dataset.alarmId = String(alert.id);

    if (alert.image_url) {
      const link = document.createElement('a');
      link.href = alert.image_url;
      link.target = '_blank';
      link.rel = 'noopener';
      const image = document.createElement('img');
      // The only image request made for this alarm: this node is inserted once.
      image.src = alert.image_url;
      image.alt = 'تصویر هشدار جدید؛ تشخیص انسان';
      image.loading = 'lazy';
      image.decoding = 'async';
      link.append(image);
      card.append(link);
    } else {
      const missing = document.createElement('div');
      missing.className = 'no-image';
      missing.textContent = 'تصویر موجود نیست';
      card.append(missing);
    }

    const body = document.createElement('div');
    body.className = 'alarm-body';

    const title = document.createElement('div');
    title.className = 'alarm-title';
    const heading = document.createElement('b');
    heading.textContent = 'تشخیص انسان · جدید';
    const badge = document.createElement('span');
    badge.className = 'state warning';
    badge.textContent = 'جدید';
    title.append(heading, badge);

    const confidence = document.createElement('p');
    confidence.className = 'muted';
    confidence.textContent = 'Confidence: ' + Number(alert.confidence || 0).toFixed(3);
    const time = document.createElement('p');
    time.className = 'muted';
    time.textContent = formatTime(alert.detected_at);

    const details = document.createElement('details');
    const summary = document.createElement('summary');
    summary.textContent = 'جزئیات تشخیص';
    const pre = document.createElement('pre');
    pre.textContent = JSON.stringify(Array.isArray(alert.bbox) ? alert.bbox : [], null, 2);
    details.append(summary, pre);

    body.append(title, confidence, time, details);
    if (!alert.acknowledged) {
      const form = document.createElement('form');
      form.className = 'ack-form live-alert-ack-form';
      form.action = '/alarms/' + encodeURIComponent(alert.id) + '/acknowledge/';
      form.method = 'post';
      const button = document.createElement('button');
      button.className = 'btn secondary';
      button.type = 'submit';
      button.textContent = 'علامت‌گذاری به‌عنوان بررسی‌شده';
      form.append(button);
      body.append(form);
    }
    card.append(body);
    detailGrid.prepend(card);

    while (detailGrid.querySelectorAll('.alarm-card[data-alarm-id]').length > 50) {
      detailGrid.querySelector('.alarm-card[data-alarm-id]:last-of-type')?.remove();
    }
  }

  function showToast(alert) {
    if (!toastRegion) return;
    const toast = document.createElement('div');
    toast.className = 'alert-toast';

    const content = document.createElement('div');
    content.className = 'alert-toast-content';
    const title = document.createElement('b');
    title.textContent = 'هشدار جدید: تشخیص انسان';
    const description = document.createElement('span');
    description.textContent =
      alert.device_name + ' · اطمینان ' + Number(alert.confidence || 0).toFixed(2);
    const time = document.createElement('small');
    time.textContent = formatTime(alert.detected_at);
    const link = document.createElement('a');
    link.href = alert.device_url;
    link.textContent = 'مشاهده هشدار و تصویر';
    content.append(title, description, time, link);

    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'alert-toast-close';
    close.setAttribute('aria-label', 'بستن اعلان');
    close.textContent = '×';
    close.addEventListener('click', () => toast.remove());

    toast.append(content, close);
    toastRegion.prepend(toast);
    window.setTimeout(() => toast.remove(), 12000);
    while (toastRegion.children.length > 5) {
      toastRegion.lastElementChild?.remove();
    }
  }

  function showBrowserNotification(alert) {
    if (!('Notification' in window) || Notification.permission !== 'granted') return;
    try {
      const notification = new Notification('هشدار جدید · تشخیص انسان', {
        body: alert.device_name + ' · اطمینان ' + Number(alert.confidence || 0).toFixed(2),
        tag: 'iot-alarm-' + alert.id,
        renotify: false
      });
      notification.onclick = () => {
        window.focus();
        window.location.href = alert.device_url;
        notification.close();
      };
    } catch (error) {
      console.warn('Could not show browser notification', error);
    }
  }

  function updateNotificationControl() {
    if (!notificationButton || !notificationStatus) return;
    if (!('Notification' in window)) {
      notificationButton.disabled = true;
      notificationStatus.textContent = 'اعلان دسکتاپ در این مرورگر پشتیبانی نمی‌شود؛ اعلان داخل صفحه فعال است.';
      return;
    }
    if (Notification.permission === 'granted') {
      notificationButton.disabled = true;
      notificationButton.textContent = 'اعلان مرورگر فعال است';
      notificationStatus.textContent = 'اعلان دسکتاپ و اعلان داخل صفحه فعال‌اند.';
    } else if (Notification.permission === 'denied') {
      notificationButton.disabled = true;
      notificationStatus.textContent = 'اجازه اعلان در تنظیمات مرورگر مسدود شده؛ اعلان داخل صفحه همچنان فعال است.';
    } else {
      notificationButton.disabled = false;
      notificationStatus.textContent = 'برای نمایش اعلان دسکتاپ، اجازه مرورگر را فعال کن.';
    }
  }

  notificationButton?.addEventListener('click', async () => {
    if (!('Notification' in window)) return;
    try {
      await Notification.requestPermission();
    } catch (error) {
      console.warn('Notification permission request failed', error);
    }
    updateNotificationControl();
  });
  updateNotificationControl();

  document.addEventListener('submit', async (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.classList.contains('live-alert-ack-form')) return;
    event.preventDefault();
    const button = form.querySelector('button');
    if (button) button.disabled = true;
    const csrf = document.querySelector('input[name=csrfmiddlewaretoken]')?.value || '';
    try {
      const response = await fetch(form.action, {
        method: 'POST',
        headers: {'X-CSRFToken': csrf, 'Accept': 'application/json'}
      });
      if (!response.ok) throw new Error('تغییر وضعیت هشدار ناموفق بود.');
      const badge = form.closest('.alarm-card')?.querySelector('.alarm-title .state');
      if (badge) {
        badge.className = 'state online';
        badge.textContent = 'بررسی‌شده';
      }
      form.remove();
    } catch (error) {
      if (button) button.disabled = false;
      console.warn(error);
    }
  });

  function processAlert(alert) {
    const id = Number(alert.id);
    if (!Number.isFinite(id) || id <= 0 || seen.has(id)) return;
    seen.add(id);
    if (homeBody) addHomeRow(alert);
    if (detailGrid) addDetailCard(alert);
    showToast(alert);
    showBrowserNotification(alert);
  }

  let stopped = false;
  async function pollAlerts() {
    if (stopped) return;
    const query = new URLSearchParams({after: String(cursor)});
    if (deviceId) query.set('device_id', deviceId);
    let delay = document.hidden ? 7000 : 2200;
    try {
      const response = await fetch('/api/dashboard/alerts/?' + query.toString(), {
        cache: 'no-store',
        headers: {'Accept': 'application/json'}
      });
      if (response.status === 401 || response.status === 403) {
        stopped = true;
        return;
      }
      if (!response.ok) throw new Error('Live alert feed returned ' + response.status);
      const payload = await response.json();
      const alerts = Array.isArray(payload.alerts) ? payload.alerts : [];
      for (const alert of alerts) {
        processAlert(alert);
        cursor = Math.max(cursor, Number(alert.id) || 0);
      }
      cursor = Math.max(cursor, Number(payload.next_cursor) || 0);
      if (payload.has_more) delay = 100;
    } catch (error) {
      console.warn('Live alert check failed', error);
      delay = 8000;
    }
    window.setTimeout(pollAlerts, delay);
  }

  pollAlerts();
})();
