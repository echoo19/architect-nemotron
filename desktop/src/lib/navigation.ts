/**
 * Back and forward through where you have been (Codex's header arrows): which
 * screen, which conversation, which settings page. Moves made by going back or
 * forward are not recorded as new places.
 */
import { create } from 'zustand';
import { useApp } from '../store/app';

interface Place { screen: string; active: string | null; pane: string }

const same = (a: Place, b: Place) => a.screen === b.screen && a.active === b.active && (a.screen !== 'settings' || a.pane === b.pane);

export const useNavigation = create<{ back: Place[]; forward: Place[] }>(() => ({ back: [], forward: [] }));

let current: Place | null = null;
let moving = false;

function here(): Place {
  const s = useApp.getState();
  return { screen: s.screen, active: s.prefs.active, pane: s.settingsPane };
}

/** Start recording; call once after the app is ready. */
export function trackNavigation(): () => void {
  current = here();
  return useApp.subscribe(() => {
    const next = here();
    if (!current || same(current, next)) return;
    if (!moving) {
      useNavigation.setState((n) => ({ back: [...n.back, current!].slice(-50), forward: [] }));
    }
    current = next;
  });
}

function go(place: Place): void {
  const s = useApp.getState();
  // A conversation closed since then cannot be returned to.
  if (place.active && !s.prefs.open.some((o) => o.id === place.active)) return;
  moving = true;
  try {
    if (place.active && place.active !== s.prefs.active) s.activate(place.active);
    s.setScreen(place.screen as Parameters<typeof s.setScreen>[0], place.screen === 'settings' ? place.pane : undefined);
  } finally {
    moving = false;
  }
}

export function goBack(): void {
  const { back, forward } = useNavigation.getState();
  const target = back.at(-1);
  if (!target || !current) return;
  useNavigation.setState({ back: back.slice(0, -1), forward: [...forward, current] });
  go(target);
}

export function goForward(): void {
  const { back, forward } = useNavigation.getState();
  const target = forward.at(-1);
  if (!target || !current) return;
  useNavigation.setState({ back: [...back, current], forward: forward.slice(0, -1) });
  go(target);
}
