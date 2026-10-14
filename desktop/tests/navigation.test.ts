/** Back and forward through places, and images in answers. */
import { describe, expect, it } from 'vitest';
import { parseMarkdown } from '../src/lib/markdown';
import { goBack, goForward, trackNavigation, useNavigation } from '../src/lib/navigation';
import { DEFAULT_PREFS, useApp } from '../src/store/app';

describe('back and forward', () => {
  it('returns to earlier screens and conversations, and forward again', () => {
    const open = [{ id: 'a', cwd: 'C:/p', kind: 'chat' as const }, { id: 'b', cwd: 'C:/p', kind: 'chat' as const }];
    useApp.setState({ prefs: { ...DEFAULT_PREFS, open, active: 'a' }, screen: 'chat', settingsPane: 'models', convs: {} });
    useNavigation.setState({ back: [], forward: [] });
    const stop = trackNavigation();
    try {
      useApp.getState().activate('b');
      useApp.getState().setScreen('settings', 'general');
      expect(useNavigation.getState().back).toHaveLength(2);

      goBack();
      expect(useApp.getState().screen).toBe('chat');
      expect(useApp.getState().prefs.active).toBe('b');
      goBack();
      expect(useApp.getState().prefs.active).toBe('a');
      goForward();
      goForward();
      expect(useApp.getState().screen).toBe('settings');
      expect(useApp.getState().settingsPane).toBe('general');

      // A new move clears what was ahead.
      goBack();
      useApp.getState().activate('a');
      expect(useNavigation.getState().forward).toHaveLength(0);
    } finally {
      stop();
    }
  });
});

describe('images in answers', () => {
  it('reads ![alt](https or absolute path) as an image, and other targets as before', () => {
    const [para] = parseMarkdown('See ![the chart](https://example.com/c.png) and ![x](C:/out/plot.svg) and ![no](https://example.com/page)', false);
    const spans = para.kind === 'paragraph' ? para.spans : [];
    const images = spans.filter((s) => s.kind === 'image');
    expect(images).toEqual([
      { kind: 'image', src: 'https://example.com/c.png', alt: 'the chart' },
      { kind: 'image', src: 'C:/out/plot.svg', alt: 'x' },
    ]);
    expect(spans.some((s) => s.kind === 'link' && s.href === 'https://example.com/page')).toBe(true);
  });
});

describe('folder trust', () => {
  it('trusts a folder and everything inside it, ignoring case and slashes on Windows', async () => {
    const { isTrusted } = await import('../src/store/app');
    expect(isTrusted('C:\\Work\\proj', ['c:/work/proj/'])).toBe(true);
    expect(isTrusted('C:/work/proj/.architect/worktrees/x', ['C:\\Work\\proj'])).toBe(true);
    expect(isTrusted('C:/work/project2', ['C:/work/proj'])).toBe(false);
    expect(isTrusted('/home/me/Code', ['/home/me/code'])).toBe(false);
  });
});

describe('undo', () => {
  it('takes back a pin and an archive, and redoes them', () => {
    useApp.setState({ prefs: { ...DEFAULT_PREFS }, undoStack: [], redoStack: [], toast: null });
    const app = () => useApp.getState();
    app().togglePin('s1');
    app().archiveSession('s2');
    expect(app().prefs.pinned).toEqual(['s1']);
    expect(app().prefs.archived).toEqual(['s2']);
    expect(app().toast?.action?.label).toBe('Undo');

    app().undoLast();
    expect(app().prefs.archived).toEqual([]);
    app().undoLast();
    expect(app().prefs.pinned).toEqual([]);
    expect(app().undoStack).toHaveLength(0);

    app().redoLast();
    expect(app().prefs.pinned).toEqual(['s1']);
    expect(app().undoStack).toHaveLength(1);
  });
});

describe('review findings', () => {
  it('reads path:line notes out of a review answer', async () => {
    const { parseFindings, sameFile } = await import('../src/lib/review');
    const text = [
      'Here is what I found:',
      '- `src/server.ts:4`: the port ignores PORT from the environment',
      '2. **src/health.ts:2** - returns ok without checking anything',
      'src/net.ts:10-12 — logs every request at info level',
      'Verdict: fine after the first fix.',
    ].join('\n');
    expect(parseFindings(text)).toEqual([
      { file: 'src/server.ts', lineNo: 4, text: 'the port ignores PORT from the environment' },
      { file: 'src/health.ts', lineNo: 2, text: 'returns ok without checking anything' },
      { file: 'src/net.ts', lineNo: 10, text: 'logs every request at info level' },
    ]);
    expect(sameFile('server.ts', 'src/server.ts')).toBe(true);
    expect(sameFile('src/net.ts', 'src/server.ts')).toBe(false);
  });
});

describe('your own shortcuts', () => {
  it('rebinds, unbinds and resets, updating every label', async () => {
    const { SHORTCUTS, applyBindings, shortcutFromEvent, shortcutLabel } = await import('../src/lib/shortcuts');
    const before = shortcutLabel(SHORTCUTS.palette, false);
    const recorded = shortcutFromEvent({ key: 'J', ctrlKey: true, shiftKey: true, altKey: false, metaKey: false } as KeyboardEvent, false);
    expect(recorded).toEqual({ key: 'j', mod: true, shift: true });
    applyBindings({ palette: recorded!, sidebar: { key: '' } });
    expect(shortcutLabel(SHORTCUTS.palette, false)).toBe('Ctrl+Shift+J');
    expect(SHORTCUTS.sidebar.key).toBe('');
    applyBindings({});
    expect(shortcutLabel(SHORTCUTS.palette, false)).toBe(before);
    expect(SHORTCUTS.sidebar.key).toBe('b');
    expect(shortcutFromEvent({ key: 'Shift', shiftKey: true } as KeyboardEvent, false)).toBeNull();
  });
});
