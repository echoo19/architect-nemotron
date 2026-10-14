/** Mod+K: jump to a conversation, run an action, or run a command in the current one. */
import { useEffect, useMemo, useState } from 'react';
import { useApp } from '../../store/app';
import { Menu, type MenuItem } from '../ui/Menu';
import { folderName } from '../chat/ConversationView';
import { chooseFolder } from './folders';

interface CommandRow { name: string; usage: string; summary: string }

export function Palette() {
  const open = useApp((s) => s.palette);
  const setPalette = useApp((s) => s.setPalette);
  const prefs = useApp((s) => s.prefs);
  const sessions = useApp((s) => s.sessions);
  const convs = useApp((s) => s.convs);
  const [commands, setCommands] = useState<CommandRow[]>([]);
  const active = prefs.active;

  useEffect(() => {
    if (!open || !active || convs[active]?.status !== 'ready') return;
    void useApp.getState().request<{ commands: CommandRow[] }>(active, 'commands')
      .then((r) => setCommands(r.commands))
      .catch(() => undefined);
  }, [open, active, convs]);

  const items: MenuItem[] = useMemo(() => {
    const list: MenuItem[] = [
      { id: 'act:new', label: 'New conversation', section: 'Actions' },
      { id: 'act:forge', label: 'Build an agent', section: 'Actions' },
      { id: 'act:folder', label: 'New conversation in another folder', section: 'Actions' },
      { id: 'act:agents', label: 'Your agents', section: 'Actions' },
      { id: 'act:settings', label: 'Settings', section: 'Actions' },
      { id: 'act:models', label: 'Models and sign-in', section: 'Actions' },
      { id: 'act:theme', label: 'Switch light or dark', section: 'Actions' },
    ];
    for (const o of prefs.open) {
      const conv = convs[o.id];
      list.push({ id: `open:${o.id}`, label: conv?.state.title || o.title || 'New conversation', hint: folderName(o.cwd), section: 'Open conversations' });
    }
    const openSessions = new Set(prefs.open.map((o) => o.session));
    for (const s of sessions) {
      if (openSessions.has(s.id)) continue;
      list.push({ id: `session:${s.id}`, label: s.title, hint: s.cwd ? folderName(s.cwd) : undefined, section: 'Earlier conversations' });
    }
    for (const c of commands) list.push({ id: `cmd:${c.name}`, label: c.usage, detail: c.summary, section: 'Commands' });
    return list;
  }, [prefs.open, convs, sessions, commands]);

  if (!open) return null;
  const close = () => setPalette(false);
  const folder = () => prefs.open.find((o) => o.id === active)?.cwd ?? prefs.folders[0] ?? '';

  const pick = (id: string) => {
    close();
    const s = useApp.getState();
    const [kind, ...rest] = id.split(':');
    const value = rest.join(':');
    if (kind === 'open') s.activate(value);
    else if (kind === 'session') {
      const row = sessions.find((x) => x.id === value);
      s.openConversation({ cwd: row?.cwd || folder(), resume: value, title: row?.title });
    } else if (kind === 'cmd' && active) {
      const takesArgs = /[<[]/.test(commands.find((c) => c.name === value)?.usage ?? '');
      if (takesArgs) s.setDraft(active, `${value} `);
      else void s.send(active, value);
      s.setScreen('chat');
    } else if (value === 'new') s.openConversation({ cwd: folder() });
    else if (value === 'forge') s.openConversation({ cwd: folder(), kind: 'forge' });
    else if (value === 'folder') void chooseFolder(folder()).then((cwd) => cwd && s.openConversation({ cwd }));
    else if (value === 'agents') s.setScreen('agents');
    else if (value === 'settings') s.setScreen('settings');
    else if (value === 'models') s.setScreen('settings', 'models');
    else if (value === 'theme') {
      const dark = document.documentElement.dataset.theme === 'dark';
      s.setPrefs({ theme: dark ? 'light' : 'dark' });
    }
  };

  return (
    <div className="scrim scrim-top" onMouseDown={close}>
      <div className="palette" onMouseDown={(e) => e.stopPropagation()}>
        <Menu items={items} onPick={pick} onClose={close} filter side="down" width={620} empty="Nothing matches" />
      </div>
    </div>
  );
}
