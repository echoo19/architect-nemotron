/**
 * The build page (clarity spec §2): one build conversation is a six-step guided page.
 * The top bar says where you are (the project, the agent, the six steps); under it the
 * step's own screen (wizard/screens.ts), picked by `wizardStep`, with its action bar.
 *
 * The conversation is a rail on the right, closed by default. It opens by itself while an
 * ask the page does not answer is waiting (a command to run, a file to edit outside the
 * Add step, the coder's question), so nothing waits where the person cannot see it.
 *
 * The page also owns the Plan and Report sheets, opened through the store
 * (`openForgeSheet`), and the dock beside it.
 */
import clsx from 'clsx';
import { useCallback, useEffect, useMemo, useRef, useState, type ComponentType } from 'react';
import { forgeModel, ladderView, type ForgeModel } from '../../lib/forge';
import type { Ask, Item } from '../../lib/timeline';
import { planName } from '../../lib/vocab';
import { connectState, wizardStep, type StepNumber, type WizardStep } from '../../lib/wizard';
import { sidebarYields, useApp, type Conv } from '../../store/app';
import { ConversationView, Stopped } from '../chat/ConversationView';
import { Dock } from '../panes/Dock';
import { PlanSheet } from './PlanSheet';
import { ReportSheet } from './ReportSheet';
import { useWidth } from './vizHooks';
import { WizardConv } from './wizard/BottomBar';
import { AddScreen, BuildScreen, DescribeScreen, PlanScreen, ResultsScreen, TryScreen, type ScreenProps } from './wizard/screens';
import { agentTitle, TopBar, useForgeSidebar } from './wizard/TopBar';

export { agentTitle };

export interface AgentDetail {
  name?: string;
  licensed?: boolean;
  spec?: Record<string, unknown> & { input_schema?: Record<string, unknown>; output_schema?: Record<string, unknown>; task?: string };
  spec_hash?: string;
  report?: Record<string, unknown> | null;
  forge?: Record<string, unknown> | null;
  design?: string | null;
  history?: Array<Record<string, unknown>>;
  drift?: unknown;
  files?: Array<{ path: string; size: number }>;
  subharnesses?: unknown[];
  status?: Record<string, unknown>;
  [key: string]: unknown;
}

export function useAgentDetail(convId: string, name: string | undefined, refreshKey: unknown) {
  const request = useApp((s) => s.request);
  const [detail, setDetail] = useState<AgentDetail | null>(null);
  const [error, setError] = useState('');
  const load = useCallback(async () => {
    if (!name) return;
    try {
      setDetail(await request<AgentDetail>(convId, 'agent', { name }));
      setError('');
    } catch (err) {
      setError((err as { code?: string }).code ?? 'failed');
    }
  }, [convId, name, request]);
  useEffect(() => {
    setDetail(null);
    void load();
  }, [load, refreshKey]);
  return { detail, error, reload: load };
}

/** The step column's least width beside the dock; narrower than this, the conversation rail steps aside. */
export const CARD_MIN = 560;
/** The conversation rail's width. */
const RAIL_W = 340;

type OpenAsk = Extract<Item, { kind: 'ask' }>;

/** An ask still waiting: not closed and not yet answered (the local echo of an answer counts at once). */
export function waiting(item: Item): item is OpenAsk {
  return item.kind === 'ask' && !item.closed && item.answer === undefined;
}

/** The open export approval (Write the files), if one is waiting. */
export function exportAsk(items: Item[]): Ask | undefined {
  const item = items.find((i): i is OpenAsk => waiting(i) && i.ask.kind === 'approval' && i.ask.action === 'export');
  return item?.ask;
}

/**
 * The open asks the page answers itself, so the transcript shows them held: the build's
 * approval (Plan's Build agent), the interview question (Plan's Answer), the export
 * approval (Add's Write the files), and, while the Add step shows it, the coder's edit
 * approval or question. Any other open ask is the transcript's to answer.
 */
export function ownedAsks(model: ForgeModel, items: Item[], ws?: WizardStep): Set<number> {
  const owned = new Set<number>();
  if (model.approval) owned.add(model.approval.id);
  if (model.phase === 'interview' && model.question && model.question.kind !== 'approval') owned.add(model.question.id);
  // The export question is the page's only while Add shows it: on Results or Try it the rail answers it.
  const exporting = exportAsk(items);
  if (exporting && (!ws || ws.step === 6)) owned.add(exporting.id);
  if (ws?.step === 6 && (ws.sub === 'connect_approve' || ws.sub === 'connect_question')) {
    const open = connectState(items).open;
    if (open) owned.add(open.id);
  }
  return owned;
}

/** The step screens, by step number. */
const SCREENS: Record<StepNumber, ComponentType<ScreenProps>> = {
  1: DescribeScreen, 2: PlanScreen, 3: BuildScreen, 4: ResultsScreen, 5: TryScreen, 6: AddScreen,
};

/** Whether this conversation shows as a build page: opened as one, or a chat in build mode whose build started. */
export function isBuildPage(conv: Conv, kind: string | undefined): boolean {
  if (kind === 'forge') return true;
  if (conv.state.state !== 'forge') return false;
  return kind === undefined || forgeModel(conv.timeline).phase !== 'empty';
}

/**
 * Whether the sidebar shows (clarity spec §2.1): on a build page only once the person
 * brought it back in this session (`useForgeSidebar`); elsewhere as the preferences say.
 */
export function useSidebarShown(buildPage: boolean): boolean {
  const pref = useApp((s) => s.prefs.sidebar && !sidebarYields(s));
  const back = useForgeSidebar((s) => s.on);
  return pref && (!buildPage || back);
}

export function ForgeWorkspace({ conv }: { conv: Conv }) {
  const dockTab = useApp((s) => s.dock.tab);
  const dockFull = useApp((s) => !!s.dock.full);
  const railWanted = useApp((s) => s.forgeTranscript);
  const setForgeTranscript = useApp((s) => s.setForgeTranscript);
  const openForgeSheet = useApp((s) => s.openForgeSheet);
  const view = useApp((s) => s.wizardView[conv.id]);
  const skipped = useApp((s) => !!s.wizardSkipped[conv.id]);
  const model = useMemo(() => forgeModel(conv.timeline), [conv.timeline]);
  const ladder = useMemo(() => ladderView(model, conv.forge), [model, conv.forge]);
  const root = useRef<HTMLDivElement>(null);
  const width = useWidth(root);
  const agentName = model.result?.licensed ? model.result.name : undefined;
  const { detail, error: detailError, reload } = useAgentDetail(conv.id, agentName, model.result?.title);

  // A sheet belongs to the build it was opened on: it closes when the page moves to another build or goes.
  const firstConv = useRef(conv.id);
  useEffect(() => {
    if (conv.id === firstConv.current) return;
    firstConv.current = conv.id;
    openForgeSheet(null);
  }, [conv.id, openForgeSheet]);
  useEffect(() => () => openForgeSheet(null), [openForgeSheet]);
  const openKind = useApp((s) => s.prefs.open.find((o) => o.id === conv.id)?.kind);
  const sidebarShown = useSidebarShown(true);

  const name = model.result?.name ?? model.name ?? planName(model);
  useEffect(() => {
    document.title = `${agentTitle(name)} - Architect`;
  }, [name]);

  const ws = wizardStep(model, conv, view, { skipped });
  const { step, sub } = ws;
  const owned = useMemo(() => ownedAsks(model, conv.timeline.items, { step, sub }), [model, conv.timeline.items, step, sub]);
  const openAsks = conv.timeline.items.filter(waiting);
  const heldAsk = openAsks.find((i) => owned.has(i.ask.id))?.ask.id;
  const foreignAsk = openAsks.some((i) => !owned.has(i.ask.id));
  // Beside the dock the step keeps its room: the rail steps aside unless it holds an ask.
  const besideDock = !!dockTab && !dockFull;
  const roomForRail = !besideDock || width <= 0 || width - RAIL_W >= CARD_MIN;
  const railOpen = foreignAsk || (railWanted && roomForRail);

  // Sign-in, an untrusted folder and a failed start are the conversation's own screens.
  if (conv.status === 'setup' || conv.status === 'trust' || conv.status === 'failed') return <ConversationView conv={conv} />;

  // A chat switched into build mode stays a chat (its history and mode switch) until a build starts in it.
  if (openKind && openKind !== 'forge' && model.phase === 'empty') return <ConversationView conv={conv} />;

  const Screen = SCREENS[step];
  // Plan and Build say themselves that the build stopped; on the other steps the conversation's own line offers Retry.
  const stopped = conv.status === 'stopped' && step !== 2 && step !== 3;

  return (
    <div className={clsx('forge-row', dockTab && 'has-dock', dockTab && dockFull && 'has-full-dock')}>
      <div className={clsx('forge', railOpen && 'has-transcript')} ref={root} data-step={step} data-sub={sub}>
        <TopBar
          conv={conv}
          model={model}
          ws={ws}
          name={name}
          sidebarHidden={!sidebarShown}
          railOpen={railOpen}
          railHeld={foreignAsk}
          onToggleRail={() => setForgeTranscript(!railOpen)}
        />
        <div className="forge-body">
          <div className="forge-main">
            {stopped && <div className="forge-stopped"><Stopped conv={conv} /></div>}
            <WizardConv.Provider value={conv}>
              <Screen conv={conv} model={model} ladder={ladder} detail={detail} detailError={detailError || undefined} sub={sub} />
            </WizardConv.Provider>
          </div>
          {railOpen && (
            <aside className="forge-transcript" aria-label="Conversation">
              <ConversationView conv={conv} compact transcript heldAsk={heldAsk} onClose={foreignAsk ? undefined : () => setForgeTranscript(false)} />
            </aside>
          )}
        </div>
      </div>
      <Dock conv={conv} />
      <PlanSheet conv={conv} model={model} />
      <ReportSheet conv={conv} model={model} detail={detail} reloadDetail={reload} />
    </div>
  );
}
