(() => {
  'use strict';
  const player = document.getElementById('recorded-video-player');
  const list = document.getElementById('video-segment-list');
  if (!player || !list) return;

  const options = Array.from(list.querySelectorAll('[data-video-url]'))
    .sort((a, b) => new Date(a.dataset.startedAt).getTime() - new Date(b.dataset.startedAt).getTime());
  if (!options.length) return;

  const label = document.getElementById('recorded-video-label');
  const playAll = document.getElementById('play-video-history');
  const previous = document.getElementById('previous-video-segment');
  const next = document.getElementById('next-video-segment');
  let currentIndex = options.length - 1;
  let playlistMode = false;

  function selectSegment(index, autoplay = false) {
    if (index < 0 || index >= options.length) return;
    currentIndex = index;
    const selected = options[currentIndex];
    player.src = selected.dataset.videoUrl;
    player.load();
    label.textContent = selected.dataset.label + ' · ' + selected.dataset.duration + ' ثانیه';
    options.forEach((item, itemIndex) => item.classList.toggle('active', itemIndex === currentIndex));
    previous.disabled = currentIndex <= 0;
    next.disabled = currentIndex >= options.length - 1;
    if (autoplay) player.play().catch(() => {});
  }

  options.forEach((item, index) => {
    item.addEventListener('click', () => {
      playlistMode = false;
      playAll.textContent = 'پخش پشت‌سرهم آرشیو';
      selectSegment(index, true);
    });
  });

  document.querySelectorAll('[data-play-video-url]').forEach((button) => {
    button.addEventListener('click', () => {
      const index = options.findIndex(item => item.dataset.videoUrl === button.dataset.playVideoUrl);
      if (index >= 0) {
        playlistMode = false;
        playAll.textContent = 'پخش پشت‌سرهم آرشیو';
        selectSegment(index, true);
      }
    });
  });

  previous.addEventListener('click', () => {
    playlistMode = false;
    selectSegment(currentIndex - 1, true);
  });
  next.addEventListener('click', () => {
    playlistMode = false;
    selectSegment(currentIndex + 1, true);
  });
  playAll.addEventListener('click', () => {
    playlistMode = true;
    playAll.textContent = 'پخش پشت‌سرهم فعال است';
    selectSegment(0, true);
  });
  player.addEventListener('ended', () => {
    if (playlistMode && currentIndex < options.length - 1) {
      selectSegment(currentIndex + 1, true);
    } else if (currentIndex >= options.length - 1) {
      playlistMode = false;
      playAll.textContent = 'پخش پشت‌سرهم آرشیو';
    }
  });

  selectSegment(currentIndex, false);
})();
