(() => {
  'use strict';

  const panel = document.getElementById('video-history-panel');
  const player = document.getElementById('recorded-video-player');
  if (!panel || !player) return;

  const feedUrl = panel.dataset.videosUrl;
  const chunkUrl = panel.dataset.chunkUrl;
  const playButton = document.getElementById('play-video-history');
  const slider = document.getElementById('archive-timeline');
  const track = document.getElementById('archive-timeline-segments');
  const currentTimeLabel = document.getElementById('archive-current-time');
  const startLabel = document.getElementById('archive-start-label');
  const endLabel = document.getElementById('archive-end-label');
  const durationLabel = document.getElementById('archive-duration-label');
  const chunkStatus = document.getElementById('archive-chunk-status');
  const bufferLabel = document.getElementById('archive-buffer-label');
  const feedStatus = document.getElementById('video-history-status');
  const emptyState = document.getElementById('archive-empty');
  const selectedLabel = document.getElementById('recorded-video-label');

  let segments = [];
  let archiveDuration = 0;
  let chunkSeconds = 12;
  let mediaSource = null;
  let sourceBuffer = null;
  let objectUrl = null;
  let sourceCodec = '';
  let baseArchiveOffset = 0;
  let baseSegmentId = '';
  let activeChunkController = null;
  let nextSegmentIndex = 0;
  let lastAppendedSegmentId = '';
  let chunkLoading = false;
  let feedLoading = false;
  let seekGeneration = 0;
  let seekInProgress = false;
  let lastChunkBytes = 0;
  let lastChunkCount = 0;
  let recordingEnabled = null;
  let pendingVideoCount = 0;
  let lastVideoUploadError = '';

  const numberFa = (value, digits = 1) => Number(value).toLocaleString('fa-IR', {
    maximumFractionDigits: digits
  });

  function formatBytes(value) {
    const bytes = Math.max(0, Number(value) || 0);
    if (bytes < 1024) return numberFa(bytes, 0) + ' بایت';
    if (bytes < 1024 * 1024) return numberFa(bytes / 1024, 1) + ' کیلوبایت';
    if (bytes < 1024 * 1024 * 1024) return numberFa(bytes / (1024 * 1024), 1) + ' مگابایت';
    return numberFa(bytes / (1024 * 1024 * 1024), 2) + ' گیگابایت';
  }

  function formatDuration(seconds) {
    const value = Math.max(0, Math.floor(Number(seconds) || 0));
    const hours = Math.floor(value / 3600);
    const minutes = Math.floor((value % 3600) / 60);
    const remainder = value % 60;
    return [hours, minutes, remainder].map(part => String(part).padStart(2, '0')).join(':');
  }

  function dateLabel(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('fa-IR');
  }

  function timeLabel(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? '—' : date.toLocaleTimeString('fa-IR');
  }

  function numericMetric(value, suffix = '٪') {
    const number = Number(value);
    return Number.isFinite(number) ? numberFa(number) + suffix : '—';
  }

  function setProgress(id, value, max = 100) {
    const node = document.getElementById(id);
    if (!node) return;
    const number = Number(value);
    node.style.width = Number.isFinite(number)
      ? Math.max(0, Math.min(100, number / max * 100)) + '%'
      : '0%';
  }

  function renderSystemMetrics(system) {
    const data = system && typeof system === 'object' ? system : {};
    const memory = data.memory && typeof data.memory === 'object' ? data.memory : {};
    const storage = data.storage && typeof data.storage === 'object' ? data.storage : {};
    const temperature = Number(data.temperature_c);
    const cpu = Number(data.cpu_percent);
    const memoryPercent = Number(memory.used_percent);
    const storagePercent = Number(storage.used_percent);

    const setText = (id, value) => {
      const node = document.getElementById(id);
      if (node) node.textContent = value;
    };

    setText('system-temperature', Number.isFinite(temperature) ? numberFa(temperature) + '°C' : '—');
    setText('system-cpu', Number.isFinite(cpu) ? numberFa(cpu) + '٪' : '—');
    setText('memory-usage', Number.isFinite(memoryPercent) ? numberFa(memoryPercent) + '٪' : '—');
    setText('storage-usage', Number.isFinite(storagePercent) ? numberFa(storagePercent) + '٪' : '—');

    setText('temperature-note', Number.isFinite(temperature)
      ? (temperature >= 80 ? 'دمای بسیار بالا؛ بار یا خنک‌سازی را بررسی کن' : 'دمای فعلی SoC Raspberry Pi')
      : 'داده دما هنوز از Pi دریافت نشده است');
    setText('memory-note', Number(memory.total_bytes) > 0
      ? formatBytes(memory.used_bytes) + ' مصرف‌شده از ' + formatBytes(memory.total_bytes)
      : 'در انتظار داده حافظه');
    setText('storage-note', Number(storage.free_bytes) >= 0 && Number(storage.total_bytes) > 0
      ? formatBytes(storage.free_bytes) + ' فضای آزاد از ' + formatBytes(storage.total_bytes)
      : 'در انتظار داده ذخیره‌سازی');

    setProgress('temperature-progress', temperature, 100);
    setProgress('cpu-progress', cpu);
    setProgress('memory-progress', memoryPercent);
    setProgress('storage-progress', storagePercent);

    const toggle = (selector, warning, hot = false) => {
      const card = document.querySelector(selector);
      if (!card) return;
      card.classList.toggle('is-warning', warning && !hot);
      card.classList.toggle('is-hot', hot);
    };
    toggle('[data-system-metric="temperature"]',
      Number.isFinite(temperature) && temperature >= 70,
      Number.isFinite(temperature) && temperature >= 80);
    toggle('[data-system-metric="cpu"]', Number.isFinite(cpu) && cpu >= 80,
      Number.isFinite(cpu) && cpu >= 95);
    toggle('[data-system-metric="memory"]', Number.isFinite(memoryPercent) && memoryPercent >= 85,
      Number.isFinite(memoryPercent) && memoryPercent >= 95);
    toggle('[data-system-metric="storage"]', Number.isFinite(storagePercent) && storagePercent >= 85,
      Number.isFinite(storagePercent) && storagePercent >= 95);
  }

  function recalculateTimeline() {
    let cursor = 0;
    for (const segment of segments) {
      segment.archiveStart = cursor;
      cursor += segment.duration_seconds;
      segment.archiveEnd = cursor;
    }
    archiveDuration = cursor;
    if (slider) {
      slider.max = String(Math.max(1, archiveDuration));
      slider.disabled = segments.length === 0;
    }
    if (playButton) playButton.disabled = segments.length === 0;
    if (durationLabel) durationLabel.textContent = formatDuration(archiveDuration);
    if (startLabel) startLabel.textContent = segments.length ? timeLabel(segments[0].started_at) : '—';
    const last = segments[segments.length - 1];
    if (endLabel) {
      endLabel.textContent = last
        ? timeLabel(new Date(new Date(last.started_at).getTime() + last.duration_seconds * 1000))
        : '—';
    }

    if (track) {
      track.replaceChildren();
      if (archiveDuration > 0) {
        for (const segment of segments) {
          const marker = document.createElement('span');
          marker.style.width = (segment.duration_seconds / archiveDuration * 100) + '%';
          marker.title = dateLabel(segment.started_at) + ' · ' + segment.duration_seconds + ' ثانیه';
          track.appendChild(marker);
        }
      }
    }
    if (emptyState) emptyState.hidden = segments.length > 0;
    updateTimelinePosition();
  }

  function chooseChunkDuration() {
    if (!segments.length) return 12;
    const sample = segments.slice(-100);
    const durations = sample.map(item => item.duration_seconds).sort((a, b) => a - b);
    const rates = sample
      .filter(item => item.size_bytes > 0 && item.duration_seconds > 0)
      .map(item => item.size_bytes / item.duration_seconds)
      .sort((a, b) => a - b);
    const medianDuration = durations[Math.floor(durations.length / 2)] || 5;
    const medianRate = rates.length ? rates[Math.floor(rates.length / 2)] : 0;
    const targetBytes = 12 * 1024 * 1024;
    const byteDrivenTarget = medianRate > 0 ? targetBytes / medianRate : 12;
    const targetSeconds = Math.max(10, Math.min(15, byteDrivenTarget));
    const multiples = Math.max(1, Math.ceil(targetSeconds / medianDuration));
    return Math.min(30, Math.max(10, Math.ceil(multiples * medianDuration)));
  }

  function segmentAtOffset(offset) {
    if (!segments.length) return {index: -1, segment: null, localOffset: 0};
    const value = Math.max(0, Math.min(archiveDuration, Number(offset) || 0));
    let index = segments.findIndex(item => value < item.archiveEnd);
    if (index < 0) index = segments.length - 1;
    const segment = segments[index];
    return {
      index,
      segment,
      localOffset: Math.max(0, Math.min(segment.duration_seconds - 0.05, value - segment.archiveStart))
    };
  }

  function updateTimelinePosition() {
    if (!segments.length || !slider) return;
    const globalOffset = Math.max(0, Math.min(archiveDuration, baseArchiveOffset + (player.currentTime || 0)));
    slider.value = String(globalOffset);
    const located = segmentAtOffset(globalOffset);
    if (currentTimeLabel) {
      const stamp = new Date(new Date(located.segment.started_at).getTime() + located.localOffset * 1000);
      currentTimeLabel.textContent = dateLabel(stamp);
    }
  }

  function updateChunkStatus() {
    if (chunkStatus) {
      chunkStatus.textContent = 'چانک خودکار: ' + numberFa(chunkSeconds, 0) + ' ثانیه';
    }
    if (bufferLabel) {
      bufferLabel.textContent = lastChunkBytes
        ? 'آخرین دریافت: ' + formatBytes(lastChunkBytes) + ' · ' + numberFa(lastChunkCount, 0) + ' قطعه'
        : 'هنوز چانکی دریافت نشده است.';
    }
  }

  function setStatus(message) {
    if (feedStatus) feedStatus.textContent = message;
  }

  function setPlayButtonState() {
    if (!playButton) return;
    playButton.textContent = player.paused ? 'ادامه پخش آرشیو' : 'توقف پخش';
  }

  function waitForEvent(target, eventName, errorName = 'error', timeoutMs = 15000) {
    return new Promise((resolve, reject) => {
      let timer = null;
      const cleanup = () => {
        if (timer !== null) window.clearTimeout(timer);
        target.removeEventListener(eventName, onSuccess);
        target.removeEventListener(errorName, onError);
      };
      const onSuccess = () => {
        cleanup();
        resolve();
      };
      const onError = () => {
        cleanup();
        reject(new Error('مرورگر نتوانست داده ویدئو را پردازش کند.'));
      };

      target.addEventListener(eventName, onSuccess);
      target.addEventListener(errorName, onError);
      // Attach listeners before checking readiness so updateend/sourceopen
      // cannot be lost in the gap between a state check and listener setup.
      const alreadyReady = (
        (eventName === 'sourceopen' && target.readyState === 'open') ||
        (eventName === 'updateend' && target.updating === false)
      );
      if (alreadyReady) {
        onSuccess();
      } else {
        timer = window.setTimeout(() => {
          cleanup();
          reject(new Error('مرورگر برای آماده‌سازی بافر ویدئو بیش از حد منتظر ماند.'));
        }, timeoutMs);
      }
    });
  }

  async function openMediaSource(codec, generation) {
    if (!('MediaSource' in window) || !MediaSource.isTypeSupported('video/mp4; codecs="' + codec + '"')) {
      throw new Error('مرورگر از پخش پیوسته H.264/MP4 با Media Source Extensions پشتیبانی نمی‌کند.');
    }

    player.pause();
    if (objectUrl) URL.revokeObjectURL(objectUrl);
    objectUrl = null;
    player.removeAttribute('src');
    player.load();

    const nextMediaSource = new MediaSource();
    mediaSource = nextMediaSource;
    objectUrl = URL.createObjectURL(nextMediaSource);
    player.src = objectUrl;
    await waitForEvent(nextMediaSource, 'sourceopen');
    if (generation !== seekGeneration || mediaSource !== nextMediaSource) return false;

    const nextSourceBuffer = nextMediaSource.addSourceBuffer('video/mp4; codecs="' + codec + '"');
    nextSourceBuffer.mode = 'sequence';
    if (generation !== seekGeneration || mediaSource !== nextMediaSource) return false;
    sourceBuffer = nextSourceBuffer;
    sourceCodec = codec;
    return true;
  }

  async function appendToSourceBuffer(bytes, generation) {
    const buffer = sourceBuffer;
    const source = mediaSource;
    if (!buffer || !source || source.readyState !== 'open') {
      throw new Error('بافر ویدئو هنوز آماده نیست.');
    }
    if (buffer.updating) await waitForEvent(buffer, 'updateend');
    // A seek can replace the MediaSource while the previous append waits for
    // updateend. Never append an old chunk into the newly-selected timeline.
    if (
      generation !== seekGeneration
      || buffer !== sourceBuffer
      || source !== mediaSource
      || source.readyState !== 'open'
    ) return false;

    const update = waitForEvent(buffer, 'updateend');
    try {
      buffer.appendBuffer(bytes);
    } catch (error) {
      // appendBuffer can throw synchronously (for example on quota errors).
      // Observe the waiter rejection so it cannot become an unhandled promise
      // when the updateend event will never arrive.
      update.catch(() => {});
      throw new Error('افزودن چانک به پلیر ناموفق بود: ' + error.message);
    }
    await update;
    if (generation !== seekGeneration || buffer !== sourceBuffer) return false;
    updateSourceDuration();
    return true;
  }

  function updateSourceDuration() {
    if (!mediaSource || mediaSource.readyState !== 'open' || !sourceBuffer ||
        !Number.isFinite(archiveDuration)) return;
    const bufferedEnd = sourceBuffer.buffered.length
      ? sourceBuffer.buffered.end(sourceBuffer.buffered.length - 1) : 0;
    const remaining = Math.max(0.1, archiveDuration - baseArchiveOffset);
    try { mediaSource.duration = Math.max(remaining, bufferedEnd); } catch (_) {}
  }

  function bufferAhead() {
    if (!player.buffered || !player.buffered.length) return 0;
    const current = player.currentTime || 0;
    for (let index = 0; index < player.buffered.length; index++) {
      const start = player.buffered.start(index);
      const end = player.buffered.end(index);
      if (current >= start - 0.25 && current <= end + 0.25) return Math.max(0, end - current);
      if (start > current) return 0;
    }
    return 0;
  }

  async function fetchAndAppendChunk(startIndex, reset = false, localOffset = 0, autoplay = false) {
    if (chunkLoading) return;
    if (startIndex < 0 || startIndex >= segments.length) return;
    chunkLoading = true;
    const generation = seekGeneration;
    const controller = new AbortController();
    activeChunkController = controller;
    let requestTimedOut = false;
    const requestTimer = window.setTimeout(() => {
      requestTimedOut = true;
      controller.abort();
    }, 75000);
    try {
      const url = new URL(chunkUrl, window.location.origin);
      url.searchParams.set('start_id', String(segments[startIndex].id));
      url.searchParams.set('seconds', String(chunkSeconds));
      setStatus('در حال دریافت چانک از آرشیو سرور…');

      const response = await fetch(url.toString(), {
        method: 'GET',
        credentials: 'same-origin',
        cache: 'no-store',
        headers: {'Accept': 'video/mp4'},
        signal: controller.signal
      });
      if (!response.ok) {
        let message = 'HTTP ' + response.status;
        try {
          const body = await response.json();
          if (body.error) message = body.error;
        } catch (_) {}
        throw new Error(message);
      }

      const codec = response.headers.get('X-Video-Codec') || '';
      const count = Math.max(1, Number(response.headers.get('X-Video-Segment-Count')) || 1);
      const ids = (response.headers.get('X-Video-Segment-Ids') || '')
        .split(',').map(value => value.trim()).filter(Boolean);
      const bytes = await response.arrayBuffer();
      if (generation !== seekGeneration) return;
      if (!bytes.byteLength) throw new Error('سرور چانک خالی برگرداند.');

      if (reset || !sourceBuffer || sourceCodec !== codec) {
        const opened = await openMediaSource(codec, generation);
        if (!opened) return;
      }
      const appended = await appendToSourceBuffer(bytes, generation);
      if (!appended || generation !== seekGeneration) return;

      lastChunkBytes = bytes.byteLength;
      lastChunkCount = count;
      const lastId = ids[ids.length - 1] || String(segments[Math.min(segments.length - 1, startIndex + count - 1)].id);
      lastAppendedSegmentId = lastId;
      const lastLoadedIndex = segments.findIndex(item => String(item.id) === lastId);
      nextSegmentIndex = lastLoadedIndex >= 0 ? lastLoadedIndex + 1 : startIndex + count;
      updateChunkStatus();

      if (reset) {
        player.currentTime = Math.max(0, localOffset);
        if (autoplay) {
          await player.play();
        } else {
          player.pause();
        }
      } else if (player.paused && autoplay) {
        await player.play();
      }
      updateTimelinePosition();
      setPlayButtonState();
      if (selectedLabel && reset) {
        selectedLabel.textContent = dateLabel(segments[startIndex].started_at) + ' · پخش آرشیو از این زمان';
      }
      setStatus('پخش پیوسته آماده است · ' + numberFa(count, 0) + ' قطعه در یک چانک به هم متصل شد.');
    } catch (error) {
      if (generation === seekGeneration && (error.name !== 'AbortError' || requestTimedOut)) {
        const message = requestTimedOut
          ? 'دریافت چانک آرشیو بیش از ۷۵ ثانیه طول کشید و لغو شد.'
          : error.message;
        setStatus('خطا در دریافت یا پخش آرشیو: ' + message);
        if (selectedLabel) selectedLabel.textContent = message;
      }
    } finally {
      window.clearTimeout(requestTimer);
      if (activeChunkController === controller) activeChunkController = null;
      if (generation === seekGeneration) chunkLoading = false;
    }
  }

  async function resetAndSeek(offset, autoplay) {
    if (!segments.length || !chunkUrl) return;
    seekGeneration += 1;
    const generation = seekGeneration;
    activeChunkController?.abort();
    activeChunkController = null;
    chunkLoading = false;
    const oldBuffer = sourceBuffer;
    if (oldBuffer?.updating) {
      try {
        await waitForEvent(oldBuffer, 'updateend');
      } catch (error) {
        if (generation === seekGeneration) {
          setStatus('بافر قبلی آرشیو آزاد نشد؛ جابه‌جایی زمان لغو شد: ' + error.message);
        }
        return;
      }
    }
    if (generation !== seekGeneration) return;
    const located = segmentAtOffset(offset);
    if (!located.segment) return;
    seekInProgress = true;
    chunkLoading = false;
    baseArchiveOffset = located.segment.archiveStart;
    baseSegmentId = String(located.segment.id);
    nextSegmentIndex = located.index;
    lastAppendedSegmentId = '';
    player.pause();
    if (mediaSource && mediaSource.readyState === 'open') {
      try { mediaSource.endOfStream(); } catch (_) {}
    }
    sourceBuffer = null;
    mediaSource = null;
    sourceCodec = '';
    await fetchAndAppendChunk(located.index, true, located.localOffset, autoplay);
    if (generation === seekGeneration) {
      seekInProgress = false;
      updateTimelinePosition();
    }
  }

  async function loadNextChunk() {
    if (chunkLoading || seekInProgress || !player || !sourceBuffer) return;
    if (nextSegmentIndex < segments.length) {
      await fetchAndAppendChunk(nextSegmentIndex, false, 0, false);
      return;
    }
    if (recordingEnabled === false && pendingVideoCount === 0 &&
        mediaSource && mediaSource.readyState === 'open') {
      try { mediaSource.endOfStream(); } catch (_) {}
    } else if (player.currentTime > 0) {
      setStatus('به انتهای ویدئوی دریافت‌شده رسیدی؛ منتظر قطعه بعدی دوربین هستیم…');
    }
  }

  async function keepBufferAhead() {
    if (player.paused || chunkLoading || seekInProgress) return;
    if (bufferAhead() <= Math.max(4, chunkSeconds * 0.45)) {
      await loadNextChunk();
    }
  }

  function renderSystemMetrics(system) {
    const data = system && typeof system === 'object' ? system : {};
    const memory = data.memory && typeof data.memory === 'object' ? data.memory : {};
    const storage = data.storage && typeof data.storage === 'object' ? data.storage : {};
    const temperature = Number(data.temperature_c);
    const cpu = Number(data.cpu_percent);
    const memoryPercent = Number(memory.used_percent);
    const storagePercent = Number(storage.used_percent);

    const setText = (id, value) => {
      const node = document.getElementById(id);
      if (node) node.textContent = value;
    };
    setText('system-temperature', Number.isFinite(temperature) ? numberFa(temperature) + '°C' : '—');
    setText('system-cpu', Number.isFinite(cpu) ? numberFa(cpu) + '٪' : '—');
    setText('memory-usage', Number.isFinite(memoryPercent) ? numberFa(memoryPercent) + '٪' : '—');
    setText('storage-usage', Number.isFinite(storagePercent) ? numberFa(storagePercent) + '٪' : '—');
    setText('temperature-note', Number.isFinite(temperature) ? 'دمای فعلی SoC Raspberry Pi' : 'در انتظار داده دما');
    setText('memory-note', Number(memory.total_bytes) > 0
      ? formatBytes(memory.used_bytes) + ' مصرف‌شده از ' + formatBytes(memory.total_bytes)
      : 'در انتظار داده حافظه');
    setText('storage-note', Number(storage.total_bytes) > 0
      ? formatBytes(storage.free_bytes) + ' فضای آزاد از ' + formatBytes(storage.total_bytes)
      : 'در انتظار داده ذخیره‌سازی');

    setProgress('temperature-progress', temperature);
    setProgress('cpu-progress', cpu);
    setProgress('memory-progress', memoryPercent);
    setProgress('storage-progress', storagePercent);
    setMetricWarning('temperature', Number.isFinite(temperature) && temperature >= 70,
      Number.isFinite(temperature) && temperature >= 80);
    setMetricWarning('cpu', Number.isFinite(cpu) && cpu >= 80,
      Number.isFinite(cpu) && cpu >= 95);
    setMetricWarning('memory', Number.isFinite(memoryPercent) && memoryPercent >= 85,
      Number.isFinite(memoryPercent) && memoryPercent >= 95);
    setMetricWarning('storage', Number.isFinite(storagePercent) && storagePercent >= 85,
      Number.isFinite(storagePercent) && storagePercent >= 95);
  }

  function setProgress(id, value) {
    const node = document.getElementById(id);
    if (!node) return;
    const number = Number(value);
    node.style.width = Number.isFinite(number)
      ? Math.max(0, Math.min(100, number)) + '%'
      : '0%';
  }

  function setMetricWarning(name, warning, hot) {
    const node = document.querySelector('[data-system-metric="' + name + '"]');
    if (!node) return;
    node.classList.toggle('is-warning', warning && !hot);
    node.classList.toggle('is-hot', hot);
  }

  function applyFeed(payload) {
    const incoming = (Array.isArray(payload.segments) ? payload.segments : [])
      .map(item => ({
        id: Number(item.id),
        segment_id: String(item.segment_id || ''),
        started_at: item.started_at,
        duration_seconds: Math.max(1, Number(item.duration_seconds) || 5),
        size_bytes: Math.max(0, Number(item.size_bytes) || 0)
      }))
      .filter(item => item.id > 0 && item.started_at)
      .sort((a, b) => new Date(a.started_at).getTime() - new Date(b.started_at).getTime());

    const previousLastId = segments.length ? String(segments[segments.length - 1].id) : '';
    segments = incoming;
    recalculateTimeline();
    chunkSeconds = chooseChunkDuration();
    updateChunkStatus();
    renderSystemMetrics(payload.system || {});

    recordingEnabled = typeof payload.recording_enabled === 'boolean' ? payload.recording_enabled : null;
    pendingVideoCount = Math.max(0, Number(payload.pending_video_segments) || 0);
    lastVideoUploadError = String(payload.last_video_upload_error || '');

    if (segments.length) {
      if (emptyState) emptyState.hidden = true;
      const latestId = String(segments[segments.length - 1].id);
      if (lastAppendedSegmentId) {
        const lastIndex = segments.findIndex(item => String(item.id) === lastAppendedSegmentId);
        if (lastIndex >= 0) nextSegmentIndex = Math.max(nextSegmentIndex, lastIndex + 1);
      } else if (!previousLastId && feedStatus) {
        setStatus('آرشیو آماده است؛ برای شروع پخش از ابتدا یا جابه‌جایی روی تایم‌لاین استفاده کن.');
      }
      if (lastVideoUploadError) {
        setStatus('خطای آخرین آپلود ویدئو از Pi: ' + lastVideoUploadError);
      } else if (!chunkLoading && !player.paused && nextSegmentIndex >= segments.length) {
        setStatus('ویدئو در حال پخش است؛ چانک بعدی پس از آپلود دوربین دریافت می‌شود.');
      } else if (feedStatus && !lastVideoUploadError) {
        setStatus('آرشیو به‌روز است · ' + numberFa(segments.length, 0) + ' قطعه ذخیره‌شده');
      }
    } else {
      if (emptyState) {
        emptyState.hidden = false;
        emptyState.textContent = recordingEnabled === false
          ? 'ضبط MP4 روی Pi متوقف است. فعال بودن RTSPS به‌تنهایی فایل آرشیو ایجاد نمی‌کند.'
          : (lastVideoUploadError
            ? 'خطای آپلود ویدئو از Pi: ' + lastVideoUploadError
            : 'هنوز قطعه‌ای از دوربین به سرور نرسیده است؛ وضعیت ضبط و صف ارسال را در بخش سلامت دستگاه بررسی کن.');
      }
      if (playButton) playButton.disabled = true;
      setStatus(lastVideoUploadError || 'در انتظار اولین قطعه MP4 از دوربین…');
    }

    if (baseSegmentId) {
      const baseIndex = segments.findIndex(item => String(item.id) === baseSegmentId);
      if (baseIndex >= 0) baseArchiveOffset = segments[baseIndex].archiveStart;
    }
    updateSourceDuration();
    if (previousLastId && segments.length && !player.paused) keepBufferAhead();
  }

  async function refreshArchive() {
    if (!feedUrl || feedLoading || document.hidden) return;
    feedLoading = true;
    const controller = new AbortController();
    let timedOut = false;
    const timer = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, 8000);
    try {
      const response = await fetch(feedUrl, {
        credentials: 'same-origin',
        cache: 'no-store',
        headers: {'Accept': 'application/json'},
        signal: controller.signal
      });
      if (!response.ok) throw new Error('HTTP ' + response.status);
      applyFeed(await response.json());
    } catch (error) {
      if (error.name !== 'AbortError' || timedOut) {
        setStatus(timedOut
          ? 'دریافت وضعیت آرشیو بیش از ۸ ثانیه طول کشید.'
          : 'دریافت وضعیت آرشیو ناموفق بود: ' + error.message);
      }
    } finally {
      window.clearTimeout(timer);
      feedLoading = false;
    }
  }

  if (playButton) {
    playButton.addEventListener('click', async () => {
      if (!segments.length) return;
      if (!player.paused) {
        player.pause();
        return;
      }
      if (!sourceBuffer) {
        await resetAndSeek(Number(slider?.value || 0), true);
      } else {
        try { await player.play(); } catch (error) {
          setStatus('شروع پخش ناموفق بود: ' + error.message);
        }
      }
    });
  }

  if (slider) {
    let previewOffset = null;
    slider.addEventListener('input', () => {
      previewOffset = Number(slider.value);
      const located = segmentAtOffset(previewOffset);
      if (located.segment && currentTimeLabel) {
        const stamp = new Date(new Date(located.segment.started_at).getTime() + located.localOffset * 1000);
        currentTimeLabel.textContent = dateLabel(stamp);
      }
    });
    slider.addEventListener('change', async () => {
      if (previewOffset === null) return;
      const resume = !player.paused;
      await resetAndSeek(previewOffset, resume);
      previewOffset = null;
    });
  }

  player.addEventListener('seeking', () => {
    if (seekInProgress || !segments.length || !sourceBuffer) return;
    const targetTime = Number(player.currentTime) || 0;
    let insideBufferedRange = false;
    for (let index = 0; index < player.buffered.length; index++) {
      if (targetTime >= player.buffered.start(index) - 0.2 &&
          targetTime <= player.buffered.end(index) + 0.2) {
        insideBufferedRange = true;
        break;
      }
    }
    if (!insideBufferedRange) {
      const shouldResume = !player.paused;
      resetAndSeek(baseArchiveOffset + targetTime, shouldResume);
    }
  });
  player.addEventListener('timeupdate', () => {
    updateTimelinePosition();
    keepBufferAhead();
  });
  player.addEventListener('play', setPlayButtonState);
  player.addEventListener('pause', setPlayButtonState);
  player.addEventListener('waiting', keepBufferAhead);
  player.addEventListener('ended', () => {
    if (recordingEnabled !== false || pendingVideoCount > 0) {
      setStatus('پخش به آخرین قطعه دریافت‌شده رسید؛ در انتظار قطعه جدید…');
      refreshArchive();
    }
    setPlayButtonState();
  });
  player.addEventListener('error', () => {
    setStatus('پخش MP4 در مرورگر ناموفق بود؛ کدک یا ساختار چانک را بررسی کن.');
  });

  refreshArchive();
  window.setInterval(refreshArchive, 5000);
})();
