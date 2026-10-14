/**
 * Right-click on a path (Claude Code's file context menu): attach it, open it here or
 * in an editor, show it in Explorer, copy it. A folder can also open a terminal.
 */
import {
  IconCode, IconCopy, IconFileCode, IconFolder, IconPaperclip, IconTerminal2,
} from '@tabler/icons-react';
import { useEffect, useState } from 'react';
import { native, revealLabel } from '../../lib/native';
import { useApp } from '../../store/app';
import { Menu, type MenuItem } from '../ui/Menu';
import { editors, openIn } from './OpenIn';

export interface PathTarget { x: number; y: number; path: string; rel: string; folder: boolean }

export function PathMenu({ target, onClose }: { target: PathTarget; onClose: () => void }) {
  const [list, setList] = useState<Array<{ id: string; label: string }>>([]);
  useEffect(() => {
    void editors().then(setList);
  }, []);
  const n = native();
  const items: MenuItem[] = [
    ...(target.folder
      ? [{ id: 'terminal', label: 'Open in terminal', icon: IconTerminal2 }]
      : [{ id: 'open', label: 'Open', icon: IconFileCode }, { id: 'attach', label: 'Attach as context', icon: IconPaperclip }]),
    ...list.map((e) => ({ id: `editor:${e.id}`, label: `Open in ${e.label}`, icon: IconCode, section: 'Open in' })),
    ...(n ? [{ id: 'reveal', label: revealLabel(), icon: IconFolder, section: 'Open in' }] : []),
    { id: 'copy', label: 'Copy path', icon: IconCopy, section: 'Copy' },
    { id: 'copy-rel', label: 'Copy relative path', icon: IconCopy, section: 'Copy' },
  ];
  const pick = (id: string) => {
    onClose();
    if (id === 'open') useApp.getState().openDock('files', target.rel);
    else if (id === 'attach') window.dispatchEvent(new CustomEvent('architect:attach', { detail: target.path }));
    else if (id === 'terminal') {
      useApp.getState().openDock('terminal');
      window.dispatchEvent(new CustomEvent('architect:terminal', { detail: target.path }));
    } else if (id === 'reveal') void n?.reveal(target.path);
    else if (id === 'copy') void navigator.clipboard?.writeText(target.path);
    else if (id === 'copy-rel') void navigator.clipboard?.writeText(target.rel);
    else if (id.startsWith('editor:')) openIn(id.slice(7), target.path);
  };
  const up = target.y > window.innerHeight - 300;
  const left = Math.min(target.x, window.innerWidth - 236);
  return (
    <div className="ctx-anchor" style={{ left, top: target.y }}>
      <Menu items={items} onPick={pick} onClose={onClose} side={up ? 'up' : 'down'} width={220} />
    </div>
  );
}
