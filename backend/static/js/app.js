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

  const sidebarButtons = [...document.querySelectorAll('[data-dms-sidebar-toggle]')];
  const sidebar = document.getElementById('dms-sidebar');
  const syncSidebarButtons = () => {
    const mobile = window.matchMedia('(max-width: 991.98px)').matches;
    const mobileOpen = document.body.classList.contains('dms-sidebar-open');
    const expanded = mobile
      ? mobileOpen
      : !document.body.classList.contains('dms-sidebar-collapsed');
    if (sidebar) {
      sidebar.inert = mobile && !mobileOpen;
      sidebar.setAttribute('aria-hidden', String(mobile && !mobileOpen));
    }
    sidebarButtons.forEach((button) => {
      button.setAttribute('aria-expanded', String(expanded));
      button.setAttribute('aria-label', expanded ? 'Collapse workspace navigation' : 'Expand workspace navigation');
    });
  };
  sidebarButtons.forEach((button) => button.addEventListener('click', () => {
    if (window.matchMedia('(max-width: 991.98px)').matches) {
      document.body.classList.toggle('dms-sidebar-open');
    } else {
      document.body.classList.toggle('dms-sidebar-collapsed');
    }
    syncSidebarButtons();
  }));
  document.querySelector('[data-dms-sidebar-close]')?.addEventListener('click', () => {
    document.body.classList.remove('dms-sidebar-open');
    syncSidebarButtons();
    document.querySelector('.dms-nav-toggle')?.focus();
  });
  sidebar?.addEventListener('click', (event) => {
    if (event.target.closest('a[href]') && window.matchMedia('(max-width: 991.98px)').matches) {
      document.body.classList.remove('dms-sidebar-open');
      syncSidebarButtons();
    }
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && document.body.classList.contains('dms-sidebar-open')) {
      document.body.classList.remove('dms-sidebar-open');
      syncSidebarButtons();
      document.querySelector('.dms-nav-toggle')?.focus();
    }
  });
  window.addEventListener('resize', syncSidebarButtons, { passive: true });
  syncSidebarButtons();

  document.addEventListener('click', (event) => {
    document.querySelectorAll('.dms-menu[open]').forEach((menu) => {
      if (!menu.contains(event.target)) menu.removeAttribute('open');
    });
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
