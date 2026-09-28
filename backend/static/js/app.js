/* Shared enhancements only. Page-specific behavior belongs in extra_js. */
(() => {
  'use strict';
  // Close the mobile menu before navigating so it cannot cover the anchor target.
  const navigation = document.getElementById('public-navigation');
  navigation?.addEventListener('click', (event) => {
    if (!event.target.closest('a[href]') || !navigation.classList.contains('show')) return;
    const toggle = document.querySelector('[aria-controls="public-navigation"]');
    if (toggle && getComputedStyle(toggle).display !== 'none') toggle.click();
  });

  document.addEventListener('htmx:configRequest', (event) => {
    const target = new URL(event.detail.path, window.location.href);
    if (target.origin !== window.location.origin) return;
    const token = document.body.dataset.csrfToken;
    if (token && token !== 'NOTPROVIDED') {
      event.detail.headers['X-CSRFToken'] = token;
    }
  });
})();
