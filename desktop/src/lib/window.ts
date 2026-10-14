/**
 * Which window this is. The first window is the main one; others are opened from it
 * (New Window, or a conversation or pane popped out) and say what they are in the URL
 * hash: `#window`, `#chat=<conversation>`, `#pane=<tab>&conv=<conversation>`.
 */
export type WindowKind = 'main' | 'window' | 'chat' | 'pane';
export interface WindowRole { kind: WindowKind; conv?: string; pane?: string }

/** This window's id, so it can tell its own settings changes from another window's. */
export const windowId = Math.random().toString(36).slice(2, 10);

export function windowRole(hash = typeof location === 'undefined' ? '' : location.hash): WindowRole {
  const params = new URLSearchParams(hash.replace(/^#/, ''));
  if (params.has('chat')) return { kind: 'chat', conv: params.get('chat') ?? undefined };
  if (params.has('pane')) return { kind: 'pane', pane: params.get('pane') ?? undefined, conv: params.get('conv') ?? undefined };
  if (params.has('window')) return { kind: 'window' };
  return { kind: 'main' };
}

/** Settings that belong to one window, not shared with the others. */
export const WINDOW_ONLY = ['active', 'sidebar'] as const;
