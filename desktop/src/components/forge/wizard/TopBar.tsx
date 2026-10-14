/**
 * The build's top bar (clarity spec §2.1): where you are, which step, and the way out.
 *
 *   [show sidebar] Mark Architect | folder project / Agent title | 1-2-3-4-5-6 | Your agents  conversation  settings
 *
 * The bar is the window's drag region; every control in it is `no-drag`. The sidebar
 * steps aside on a build (App.tsx): the show-sidebar button leads the bar while it is
 * hidden, and bringing it back holds for this session (`useForgeSidebar`), not saved.
 */
import { IconFolder, IconLayoutGrid, IconLayoutSidebarLeftExpand, IconLayoutSidebarRight, IconSettings } from '@tabler/icons-react';
import { create } from 'zustand';
import { shortcutLabel, SHORTCUTS } from '../../../lib/shortcuts';
import { projectName, stepItems, type WizardStep } from '../../../lib/wizard';
import type { ForgeModel } from '../../../lib/forge';
import { showSidebar, useApp, type Conv } from '../../../store/app';
import { Button, IconButton } from '../../ui/Button';
import { Mark } from '../../ui/Working';
import { Stepper } from './Stepper';

/** The sidebar on a build: hidden unless the person brought it back in this session. */
export const useForgeSidebar = create<{ on: boolean }>(() => ({ on: false }));

/** Bring the sidebar back on a build, for the rest of this session. */
export function showForgeSidebar(): void {
  useForgeSidebar.setState({ on: true });
  showSidebar();
}

export interface TopBarProps {
  conv: Conv;
  model: ForgeModel;
  ws: WizardStep;
  /** The agent's name (the slug); unset before the plan names it. */
  name?: string;
  /** The sidebar is hidden: the bar leads with the button that brings it back. */
  sidebarHidden: boolean;
  /** The conversation rail is open. */
  railOpen: boolean;
  /** The rail holds an answer only it can give, so it cannot close. */
  railHeld: boolean;
  onToggleRail(): void;
}

export function TopBar({ conv, model, ws, name, sidebarHidden, railOpen, railHeld, onToggleRail }: TopBarProps) {
  const setScreen = useApp((s) => s.setScreen);
  const setWizardView = useApp((s) => s.setWizardView);
  const project = projectName(conv.state.cwd);
  const title = ws.step === 1 ? 'New agent' : agentTitle(name);
  return (
    <header className="wizard-top drag">
      <div className="wizard-top-start">
        {sidebarHidden && (
          <IconButton
            icon={IconLayoutSidebarLeftExpand}
            label="Show the sidebar"
            tooltip={false}
            data-tip={`Show sidebar  ${shortcutLabel(SHORTCUTS.sidebar)}`}
            data-tip-side="down"
            size="sm"
            className="no-drag wizard-top-sidebar"
            onClick={showForgeSidebar}
          />
        )}
        <span className="wizard-brand">
          <Mark className="wizard-brand-mark" />
          <span className="wizard-brand-word">Architect</span>
        </span>
        <span className="wizard-top-divider" aria-hidden />
        <span className="wizard-crumb" title={conv.state.cwd}>
          <IconFolder size={15} stroke={1.5} aria-hidden className="wizard-crumb-icon" />
          {project && <span className="wizard-crumb-project">{project}</span>}
          {project && <span className="wizard-crumb-slash" aria-hidden>/</span>}
          <span className="wizard-crumb-agent">{title}</span>
        </span>
      </div>
      <div className="wizard-top-centre">
        <Stepper items={stepItems(ws, model)} onPick={(item) => item.view && setWizardView(conv.id, item.view)} />
      </div>
      <div className="wizard-top-end">
        <Button variant="ghost" size="sm" className="no-drag wizard-agents" aria-label="Your agents" onClick={() => setScreen('agents')}>
          <IconLayoutGrid size={16} stroke={1.5} aria-hidden />
          <span className="wizard-agents-label">Your agents</span>
        </Button>
        <IconButton
          icon={IconLayoutSidebarRight}
          label={railOpen ? 'Hide the conversation' : 'Show the conversation'}
          size="sm"
          aria-pressed={railOpen}
          disabled={railHeld}
          className="no-drag"
          onClick={onToggleRail}
        />
        <IconButton icon={IconSettings} label="Settings" size="sm" className="no-drag" onClick={() => setScreen('settings')} />
      </div>
    </header>
  );
}

/** "receipt-reader" as a title: "Receipt reader". */
export function agentTitle(name: string | undefined): string {
  const words = (name ?? '').replace(/[-_]+/g, ' ').trim();
  return words ? words[0].toUpperCase() + words.slice(1) : 'New agent';
}
