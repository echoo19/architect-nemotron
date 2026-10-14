/*
 * Icons the site adds to the set in design/components.js. Tabler Icons outline
 * paths (MIT, @tabler/icons 3.48.0, the version the desktop ships), on Tabler's
 * 24-unit grid. build.mjs merges them into components.js's ICON_PATHS, so
 * iconSvg() draws them at build time and in the browser alike.
 */

export const SITE_ICONS = {
  brandWindows: ['M17.8 20l-12 -1.5c-1 -.1 -1.8 -.9 -1.8 -1.9v-9.2c0 -1 .8 -1.8 1.8 -1.9l12 -1.5c1.2 -.1 2.2 .8 2.2 1.9v12.1c0 1.2 -1.1 2.1 -2.2 1.9l0 .1', 'M12 5l0 14', 'M4 12l16 0'],
  brandApple: ['M8.286 7.008c-3.216 0 -4.286 3.23 -4.286 5.92c0 3.229 2.143 8.072 4.286 8.072c1.165 -.05 1.799 -.538 3.214 -.538c1.406 0 1.607 .538 3.214 .538s4.286 -3.229 4.286 -5.381c-.03 -.011 -2.649 -.434 -2.679 -3.23c-.02 -2.335 2.589 -3.179 2.679 -3.228c-1.096 -1.606 -3.162 -2.113 -3.75 -2.153c-1.535 -.12 -3.032 1.077 -3.75 1.077c-.729 0 -2.036 -1.077 -3.214 -1.077', 'M12 4a2 2 0 0 0 2 -2a2 2 0 0 0 -2 2'],
  brandGithub: ['M9 19c-4.3 1.4 -4.3 -2.5 -6 -3m12 5v-3.5c0 -1 .1 -1.4 -.5 -2c2.8 -.3 5.5 -1.4 5.5 -6a4.6 4.6 0 0 0 -1.3 -3.2a4.2 4.2 0 0 0 -.1 -3.2s-1.1 -.3 -3.5 1.3a12.3 12.3 0 0 0 -6.2 0c-2.4 -1.6 -3.5 -1.3 -3.5 -1.3a4.2 4.2 0 0 0 -.1 3.2a4.6 4.6 0 0 0 -1.3 3.2c0 4.6 2.7 5.7 5.5 6c-.6 .6 -.6 1.2 -.5 2v3.5'],
};

/** Tabler's filled player-play (MIT), for the video's play button. */
export const PLAY_FILLED = 'M6 4v16a1 1 0 0 0 1.524 .852l13 -8a1 1 0 0 0 0 -1.704l-13 -8a1 1 0 0 0 -1.524 .852z';

/**
 * Lectern's mark as one traced outline: board, stem and foot drawn as their union, in
 * the mark's 100x100 space (MARK_OUTLINE in desktop/src/components/ui/Working.tsx).
 * One path has one edge, so no seam or nub shows at the joints; change them together.
 */
export const MARK_OUTLINE = 'M25 23 L57 16 L57 65.9375 L75 62 L75 75 L43 82 L43 32.0625 L25 36 Z';
