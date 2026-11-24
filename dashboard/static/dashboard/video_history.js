(() => {
  'use strict';

  const player = document.getElementById('recorded-video-player');
  const list = document.getElementById('video-segment-list');
  const panel = document.getElementById('video-history-panel');
  if (!player || !list || !panel) return;

  const feedUrl = panel.dataset.videosUrl;
  const label = document.getElementById('recorded-video-label');
  const feedStatus = document.getElementById('video-history-status');
  const playAll = document.getElementById('play-video-history');
  const previous = document.getElementById('previous-video-segment');
  const next = document.getElementById('next-video-segment');
  const empty = list.querySelector('[data-video-empty]');
  const recordingState = document.getElementById('recording-state');
  const lastVideoUpload = document.getElementById('last-video-upload');
  const uploadError = document.getElementById('video-upload-error');

  const optionsById = new Map();
  let currentSegmentId = null;
  let playlistMode = false;
  let lastSuccessfulSync = 0;
  let requestInProgress = false;

  function sortedOptions() {
    return Array.from(optionsById.values()).sort((a, b) => {
      const diff = new Date(a.dataset.startedAt).getTime() - new Date(b.dataset.startedAt).getTime();
      return diff || Number(a.dataset.segmentId) - Number(b.dataset.segmentId);
    });
  }

  function dateLabel(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value || '—') : date.toLocaleString('fa-IR');
  }

  function formatBytes(value) {
    const bytes = Math.max(0, Number(value) || 0);
    if (bytes < 1024) return bytes.toLocaleString('fa-IR') + ' بایت';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1).toLocaleString('fa-IR') + ' کیلوبایت';
    return (bytes / (1024 * 1024)).toFixed(1).toLocaleString('fa-IR') + ' مگابایت';
  }

  function makeOption(segment) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'video-segment-option';
    button.dataset.segmentId = String(segment.id);
    button.dataset.videoUrl = segment.url;
    button.dataset.startedAt = segment.started_at;
    button.dataset.duration = String(segment.duration_seconds || 5);
    button.dataset.label = dateLabel(segment.started_at);

    const time = document.createElement('span');
    time.textContent = button.dataset.label;
    const meta = document.createElement('small');
    meta.textContent = button.dataset.duration + ' ثانیه · ' + formatBytes(segment.size_bytes);
    button.append(time, meta);
    button.addEventListener('click', () => {
      playlistMode = false;
      playAll.textContent = 'پخش پشت‌سرهم آرشیو';
      selectSegmentById(button.dataset.segmentId, true);
    });
    return button;
  }

  function registerExistingOptions() {
    list.querySelectorAll('[data-video-url]').forEach((button) => {
      const id = String(button.dataset.segmentId || '');
      if (!id) return;
      optionsById.set(id, button);
      button.addEventListener('click', () => {
        playlistMode = false;
        playAll.textContent = 'پخش پشت‌سرهم آرشیو';
        selectSegmentById(id, true);
      });
    });
  }

  function rebuildList() {
    const sorted = sortedOptions();
    sorted.forEach((item) => list.appendChild(item));
    const emptyNode = list.querySelector('[data-video-empty]');
    if (emptyNode) emptyNode.hidden = sorted.length > 0;
    if (emptyNode && !sorted.length) emptyNode.hidden = false;

    if (playAll) playAll.disabled = sorted.length === 0;
    const currentIndex = sorted.findIndex((item) => item.dataset.segmentId === currentSegmentId);
    if (previous) previous.disabled = currentIndex <= 0;
    if (next) next.disabled = currentIndex < 0 || currentIndex >= sorted.length - 1;
    sorted.forEach((item) => item.classList.toggle(
      'active', item.dataset.segmentId === currentSegmentId
    ));
  }

  function selectSegment(index, autoplay = false) {
    const sorted = sortedOptions();
    if (index < 0 || index >= sorted.length) return;
    selectSegmentById(sorted[index].dataset.segmentId, autoplay);
  }

  function selectSegmentById(id, autoplay = false) {
    const sorted = sortedOptions();
    const index = sorted.findIndex((item) => item.dataset.segmentId === String(id));
    if (index < 0) return;
    const selected = sorted[index];
    currentSegmentId = selected.dataset.segmentId;
    player.src = selected.dataset.videoUrl;
    player.load();
    label.textContent = selected.dataset.label + ' · ' + selected.dataset.duration + ' ثانیه';
    rebuildList();
    if (autoplay) player.play().catch(() => {
      label.textContent = 'برای پخش این قطعه، پخش‌کننده را دوباره اجرا کن.';
    });
  }

  function updateDeviceStatus(payload) {
    if (recordingState) {
      recordingState.textContent = payload.recording_enabled === true
        ? 'فعال'
        : (payload.recording_enabled === false ? 'متوقف' : 'وضعیت گزارش نشده');
    }
    if (lastVideoUpload) {
      lastVideoUpload.textContent = payload.last_video_upload_at
        ? dateLabel(payload.last_video_upload_at)
        : 'هنوز قطعه‌ای دریافت نشده';
    }
    if (uploadError) {
      const error = String(payload.last_video_upload_error || '').trim();
      uploadError.textContent = error || '—';
      uploadError.title = error;
    }

    const segments = Array.isArray(payload.segments) ? payload.segments : [];
    const serverIds = new Set(segments.map((item) => String(item.id)));
    const preserveCurrent = currentSegmentId && !player.ended;
    for (const [id, option] of optionsById.entries()) {
      if (!serverIds.has(id) && !(preserveCurrent && id === currentSegmentId)) {
        option.remove();
        optionsById.delete(id);
      }
    }

    for (const segment of segments) {
      const id = String(segment.id);
      const existing = optionsById.get(id);
      if (!existing) {
        optionsById.set(id, makeOption(segment));
      } else {
        existing.dataset.videoUrl = segment.url;
        existing.dataset.startedAt = segment.started_at;
        existing.dataset.duration = String(segment.duration_seconds || 5);
        const parts = existing.querySelectorAll('span, small');
        if (parts[0]) parts[0].textContent = dateLabel(segment.started_at);
        if (parts[1]) parts[1].textContent = existing.dataset.duration + ' ثانیه · ' + formatBytes(segment.size_bytes);
      }
    }
    rebuildList();

    const emptyNode = list.querySelector('[data-video-empty]');
    if (!segments.length && emptyNode) {
      const error = String(payload.last_video_upload_error || '').trim();
      if (payload.recording_enabled === false) {
        emptyNode.textContent = 'ضبط محلی روی Pi متوقف است. پخش زنده می‌تواند فعال باشد، اما تا روشن‌کردن ضبط، فایل MP4 برای آرشیو ساخته نمی‌شود.';
      } else if (error) {
        emptyNode.textContent = 'ضبط فعال است اما آپلود قطعه ویدئو خطا دارد: ' + error;
      } else if (payload.recording_enabled === true) {
        emptyNode.textContent = 'ضبط محلی فعال است؛ پس از تکمیل و ارسال اولین قطعه، این فهرست خودکار به‌روز می‌شود.';
      } else {
        emptyNode.textContent = 'در انتظار گزارش وضعیت ضبط از Pi و دریافت اولین قطعه ویدئو…';
      }
    }
  }

  async function refreshArchive() {
    if (!feedUrl || requestInProgress || document.hidden) return;
    requestInProgress = true;
    try {
      const response = await fetch(feedUrl, {
        method: 'GET',
        credentials: 'same-origin',
        cache: 'no-store',
        headers: {'Accept': 'application/json'}
      });
      if (!response.ok) throw new Error('HTTP ' + response.status);
      const payload = await response.json();
      updateDeviceStatus(payload);
      lastSuccessfulSync = Date.now();
      if (feedStatus) {
        feedStatus.textContent = 'همگام‌سازی زنده · ' + new Date(lastSuccessfulSync).toLocaleTimeString('fa-IR');
      }
      if (!currentSegmentId && optionsById.size) {
        const latest = sortedOptions().length - 1;
        selectSegment(latest, false);
      }
    } catch (error) {
      if (feedStatus) feedStatus.textContent = 'به‌روزرسانی آرشیو ناموفق بود: ' + error.message;
    } finally {
      requestInProgress = false;
    }
  }

  registerExistingOptions();
  rebuildList();

  previous?.addEventListener('click', () => {
    playlistMode = false;
    const sorted = sortedOptions();
    const index = sorted.findIndex((item) => item.dataset.segmentId === currentSegmentId);
    selectSegment(index - 1, true);
  });
  next?.addEventListener('click', () => {
    playlistMode = false;
    const sorted = sortedOptions();
    const index = sorted.findIndex((item) => item.dataset.segmentId === currentSegmentId);
    selectSegment(index + 1, true);
  });
  playAll?.addEventListener('click', () => {
    playlistMode = true;
    playAll.textContent = 'پخش پشت‌سرهم فعال است';
    selectSegment(0, true);
  });
  player.addEventListener('ended', () => {
    const sorted = sortedOptions();
    const index = sorted.findIndex((item) => item.dataset.segmentId === currentSegmentId);
    if (playlistMode && index >= 0 && index < sorted.length - 1) {
      selectSegment(index + 1, true);
    } else {
      playlistMode = false;
      if (playAll) playAll.textContent = 'پخش پشت‌سرهم آرشیو';
    }
  });
  player.addEventListener('error', () => {
    if (label && currentSegmentId) label.textContent = 'فایل این قطعه در سرور در دسترس نیست یا قابل پخش نیست.';
  });

  if (!currentSegmentId && optionsById.size) selectSegment(sortedOptions().length - 1, false);
  refreshArchive();
  window.setInterval(refreshArchive, 5000);
})();
