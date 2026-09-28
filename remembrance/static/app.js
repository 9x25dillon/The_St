'use strict';
let installPrompt;
window.addEventListener('beforeinstallprompt', event => {
  event.preventDefault();
  installPrompt = event;
  const button = document.getElementById('install-app');
  if (button) button.hidden = false;
});
document.getElementById('install-app')?.addEventListener('click', async () => {
  if (!installPrompt) return;
  await installPrompt.prompt();
  await installPrompt.userChoice;
  installPrompt = null;
  document.getElementById('install-app').hidden = true;
});
document.querySelector('[data-print]')?.addEventListener('click', () => window.print());
document.querySelector('[data-share]')?.addEventListener('click', async event => {
  const url = event.currentTarget.dataset.share;
  const status = document.getElementById('share-status');
  try {
    if (navigator.share) await navigator.share({title:document.title, url});
    else {
      await navigator.clipboard.writeText(url);
      status.textContent = 'Memorial link copied. Share it with someone who remembers.';
    }
  } catch (error) {
    if (error.name !== 'AbortError') status.textContent = `Share this address: ${url}`;
  }
});
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
