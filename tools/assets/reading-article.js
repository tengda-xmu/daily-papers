(() => {
  'use strict';
  const dialog = document.querySelector('.article-figure-dialog');
  if (!dialog) return;
  document.querySelectorAll('.article-zoom').forEach(link => link.addEventListener('click', event => {
    if (event.ctrlKey || event.metaKey || event.shiftKey) return;
    event.preventDefault();
    const img = link.querySelector('img');
    dialog.querySelector('img').src = img.currentSrc || img.src;
    dialog.querySelector('img').alt = img.alt;
    dialog.showModal();
  }));
  dialog.addEventListener('click', event => { if (event.target === dialog) dialog.close(); });
})();
