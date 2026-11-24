(() => {
  'use strict';
  const csrf = document.querySelector('input[name=csrfmiddlewaretoken]')?.value || '';
  const status = document.getElementById('cleanup-status');
  const labels = {
    alarms: 'همه تصاویر و هشدارهای ذخیره‌شده',
    videos: 'همه ویدئوهای آرشیوشده',
    logs: 'همه لاگ‌های دستگاه'
  };

  async function fetchWithTimeout(url, options = {}, timeoutMs = 30000) {
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
        throw new Error('پاک‌سازی بیش از زمان مجاز طول کشید؛ وضعیت باقی‌مانده فایل‌ها را پس از refresh بررسی کن.');
      }
      throw error;
    } finally {
      window.clearTimeout(timer);
    }
  }

  document.querySelectorAll('[data-cleanup-kind]').forEach((button) => {
    button.addEventListener('click', async () => {
      const kind = button.dataset.cleanupKind;
      const deviceId = button.dataset.deviceId || '';
      const scope = deviceId ? ' این دوربین' : ' تمام دوربین‌های حساب';
      const title = labels[kind];
      if (!title) return;
      if (!window.confirm('این عملیات غیرقابل‌بازگشت است. ' + title + scope +
          ' از سرور حذف می‌شوند. نسخه‌های محلی روی Raspberry Pi پاک نمی‌شوند. ادامه می‌دهی؟')) return;

      const original = button.textContent;
      button.disabled = true;
      button.textContent = 'در حال پاک‌سازی…';
      if (status) status.textContent = 'در حال حذف اطلاعات انتخاب‌شده از سرور…';
      try {
        const response = await fetchWithTimeout(
          button.dataset.cleanupUrl || '/cleanup/',
          {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              'X-CSRFToken': csrf,
              'Accept': 'application/json'
            },
            body: JSON.stringify({kind, device_id: deviceId})
          },
          60000
        );
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || 'پاک‌سازی اطلاعات ناموفق بود.');
        const count = Number(result.removed_count || 0).toLocaleString('fa-IR');
        const size = (Number(result.removed_bytes || 0) / (1024 * 1024)).toLocaleString('fa-IR', {
          maximumFractionDigits: 1
        });
        const message = count + ' مورد حذف شد؛ ' + size + ' مگابایت از فایل‌های ذخیره‌شده آزاد شد.';
        if (status) status.textContent = message;
        else window.alert(message);
        window.setTimeout(() => window.location.reload(), 700);
      } catch (error) {
        if (status) status.textContent = error.message;
        else window.alert(error.message);
        button.disabled = false;
        button.textContent = original;
      }
    });
  });
})();
