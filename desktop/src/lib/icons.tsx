/** One icon vocabulary for the whole app (Tabler, Lectern's set). */
import {
  IconAdjustmentsHorizontal, IconBolt, IconBook2, IconBug, IconCode, IconEye, IconFileDescription,
  IconFilePlus, IconFileText, IconFolder, IconHammer, IconLayoutGrid, IconListCheck, IconMessageCircle,
  IconPackageExport, IconPencil, IconPlayerPlay, IconPlug, IconRobot, IconSearch, IconShieldCheck,
  IconSparkles, IconTerminal2, IconTestPipe, IconWorld, type Icon,
} from '@tabler/icons-react';

export type { Icon };

export function toolIcon(name: string): Icon {
  if (name.startsWith('mcp__')) return IconPlug;
  switch (name) {
    case 'read_file': return IconFileText;
    case 'list_dir': return IconFolder;
    case 'grep': case 'glob': return IconSearch;
    case 'code': return IconCode;
    case 'web_fetch': return IconWorld;
    case 'make': case 'forge': return IconHammer;
    case 'spawn': return IconRobot;
    case 'run_agent': return IconPlayerPlay;
    case 'agents': case 'report': return IconFileDescription;
    case 'floor': return IconListCheck;
    case 'compiler': return IconAdjustmentsHorizontal;
    case 'coder': return IconCode;
    default: return IconSparkles;
  }
}

export function approvalIcon(action: string): Icon {
  if (action === 'bash') return IconTerminal2;
  if (action === 'edit') return IconPencil;
  if (action === 'write') return IconFilePlus;
  if (action === 'export') return IconPackageExport;
  if (action.startsWith('mcp ')) return IconPlug;
  return IconShieldCheck;
}

export const STATE_ICONS: Record<string, Icon> = {
  normal: IconMessageCircle,
  forge: IconHammer,
  plan: IconListCheck,
};

export const PERMISSION_ICONS: Record<string, Icon> = {
  ask: IconShieldCheck,
  'auto-edit': IconPencil,
  'full-auto': IconBolt,
  plan: IconEye,
};

/** Starting points for an empty conversation. */
export const CHAT_STARTERS: Array<{ icon: Icon; title: string; prompt: string }> = [
  { icon: IconBook2, title: 'Explain this project', prompt: 'Give me a tour of this project: what it does, how it is laid out, and how to run it.' },
  { icon: IconBug, title: 'Find and fix a bug', prompt: 'Look for a bug in this project, explain it, and fix it.' },
  { icon: IconTestPipe, title: 'Add tests', prompt: 'Find the most important untested code here and add tests for it.' },
  { icon: IconLayoutGrid, title: 'Plan a change', prompt: 'Help me plan a change to this project. Ask me what I want first.' },
];
