/*
 * Architect site: behaviour.
 *
 * A classic script, so the page also works from file://. It reads
 * window.ArchitectViz (design/components.js, wrapped by build.mjs). The page
 * reads fine without this script; it adds the tooltips, the copy buttons, the
 * theme switch and the video.
 */
(function () {
  'use strict';

  const V = window.ArchitectViz;
  if (!V) return;
  const $ = (sel, root) => (root || document).querySelector(sel);
  const icon = (name, size) => V.iconSvg(name, { size: size || 16, stroke: 2 });

  function setupTheme() {
    const button = $('#theme');
    if (!button) return;
    const apply = (theme, save) => {
      document.documentElement.setAttribute('data-theme', theme);
      const dark = theme === 'dark';
      const label = dark ? 'Light theme' : 'Dark theme';
      button.innerHTML = icon(dark ? 'sun' : 'moon', 16);
      button.setAttribute('aria-label', label);
      button.setAttribute('data-tip', label);
      if (save) {
        try { localStorage.setItem('architect-site-theme', theme); } catch (e) { /* storage blocked: the switch still works */ }
      }
    };
    apply(document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light', false);
    button.addEventListener('click', () => apply(document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark', true));
  }

  function setupVideo() {
    const figure = $('.video');
    if (!figure) return;
    const id = figure.dataset.youtube;
    const play = $('.video-play', figure);
    play.addEventListener('click', () => {
      if (!id) return;
      // YouTube refuses embeds that carry no referrer, which is every embed on a file:// page.
      if (location.protocol === 'file:') {
        window.open(`https://www.youtube.com/watch?v=${encodeURIComponent(id)}`, '_blank', 'noopener');
        return;
      }
      const frame = document.createElement('iframe');
      frame.src = `https://www.youtube-nocookie.com/embed/${encodeURIComponent(id)}?autoplay=1&rel=0`;
      frame.title = 'Architect, the video';
      frame.allow = 'autoplay; encrypted-media; picture-in-picture; fullscreen';
      frame.allowFullscreen = true;
      frame.referrerPolicy = 'strict-origin-when-cross-origin';
      figure.replaceChildren(frame);
    });
  }

  function start() {
    V.attachTips(document);
    setupTheme();
    setupVideo();
    document.documentElement.classList.add('is-ready');
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
