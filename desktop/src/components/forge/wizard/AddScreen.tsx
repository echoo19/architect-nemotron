/**
 * Step 6, Add to project (clarity spec section 3, mockups 6a, 6a2 and 6b, with C4 to C6).
 * Three parts, each the person's to start: write the files (the export approval, one
 * yes), connect it in your code (a separate request to Architect's coder, each edit
 * approved on its own), then switch it on and run the tests (the person's, in their
 * terminal). Architect runs no tests here, so no screen shows a test result.
 */
import {
  IconArrowRight, IconCheck, IconChevronDown, IconCopy, IconFile, IconFileDescription, IconFolder, IconLayoutGrid, IconMinus,
  IconSquare, IconTerminal2,
} from '@tabler/icons-react';
import clsx from 'clsx';
import { useEffect, useRef, useState, type ReactNode } from 'react';
import { friendlyLine, type ForgeModel } from '../../../lib/forge';
import { native } from '../../../lib/native';
import type { Item } from '../../../lib/timeline';
import { useCopy } from '../../../lib/useCopy';
import { exportRows, noticeWords } from '../../../lib/vocab';
import {
  approvalDiff, connectAction, connectPrompt, connectState, exportRefusal, exportSize, filePurpose, flagLine, previewChanges, projectName, projectPath,
  saidSteps, testsCommand, type SubState, type WizardView,
} from '../../../lib/wizard';
import { useApp, type Conv } from '../../../store/app';
import { AskCard } from '../../chat/AskCard';
import { Button } from '../../ui/Button';
import { Card, CardBody, CardHeader } from '../../ui/Card';
import { Input } from '../../ui/Field';
import { Row, RowList } from '../../ui/RowList';
import { StatusMark } from '../../ui/StatusMark';
import { Working } from '../../ui/Working';
import { ChangePreview } from './ChangePreview';
import type { ScreenProps } from './types';
import { WizardPage, type BarSpec } from './WizardPage';
import '../../../styles/try-add.css';

// ---------------------------------------------------------------- starting the export

/** The view each build came to Add from, so Back on the export question returns there. */
const cameFrom = new Map<string, WizardView>();

/**
 * Add to project, from Try it or the stepper: show step 6 and send `/export {name}` when
 * no export is under way, so the files' question comes up at once.
 */
export function startAdd(conv: Conv, model: ForgeModel, from: WizardView): void {
  const s = useApp.getState();
  cameFrom.set(conv.id, from);
  s.setWizardView(conv.id, 'add');
  const name = model.result?.name;
  if (!name || model.exported || (model.exportPlan && model.phase === 'exporting')) return;
  if (conv.status !== 'ready' || conv.timeline.running) return;
  void s.send(conv.id, `/export ${name}`);
}

// ---------------------------------------------------------------- connecting it

/** The conversation Connect it opened for each build (fallback F1). */
const handedOff = new Map<string, string>();

/**
 * Connect it, fallback F1 (clarity spec section 3, Gate G1): a new normal conversation in
 * the project with the request typed in and not sent; the person sends it there and
 * allows each edit. Gate G1 failed: in a build conversation started with `/make @<file>`
 * the conductor's transcript holds no `make` call (a direct tool run leaves a note), so
 * build mode sends the request to `make` as a new build (`Conductor.routes_to_make`).
 * The build steps past Connect: it shows what is left, with a way back to that conversation.
 */
export function connectElsewhere(conv: Conv, model: ForgeModel): string {
  const s = useApp.getState();
  const id = s.openConversation({ cwd: conv.state.cwd ?? '', kind: 'chat' });
  // The edit belongs in the project itself, not in a worktree beside it.
  const prefs = useApp.getState().prefs;
  s.setPrefs({ open: prefs.open.map((o) => (o.id === id ? { ...o, wantWorktree: false } : o)) });
  s.setDraft(id, connectPrompt(model));
  handedOff.set(conv.id, id);
  s.setWizardSkipped(conv.id, true);
  return id;
}

// ---------------------------------------------------------------- the three parts

type PartMark = 'done' | 'current' | 'working' | 'todo' | 'skipped';

const PARTS = ['Write the files', 'Connect it in your code', 'Switch it on'] as const;

function partMarks(sub: SubState, skipped: boolean, allowed: number, continued: boolean): [PartMark, PartMark, PartMark] {
  switch (sub) {
    case 'add_ready':
    case 'add_preparing':
    case 'add_writing':
      return ['working', 'todo', 'todo'];
    case 'add_confirm':
    case 'add_refused':
      return ['current', 'todo', 'todo'];
    case 'files_written':
    case 'connect_approve':
    case 'connect_question':
    case 'connect_failed':
      return ['done', 'current', 'todo'];
    case 'connecting':
      return ['done', 'working', 'todo'];
    default:
      return ['done', continued ? 'current' : skipped || allowed === 0 ? 'skipped' : 'done', 'current'];
  }
}

/** The small progress line over every step 6 screen: Write the files, Connect it in your code, Switch it on. */
function AddProgress({ marks }: { marks: PartMark[] }) {
  return (
    <ol className="add-progress" aria-label="Add to project">
      {PARTS.map((label, i) => (
        <li key={label} className={clsx('add-progress-part', `is-${marks[i]}`)} aria-current={marks[i] === 'current' || marks[i] === 'working' ? 'true' : undefined}>
          <span className="add-progress-glyph" aria-hidden>
            {marks[i] === 'done' ? <IconCheck size={12} stroke={2} /> : marks[i] === 'working' ? <Working size="xs" /> : marks[i] === 'skipped' ? <IconMinus size={12} stroke={2} /> : null}
          </span>
          <span className="add-progress-label">{label}</span>
          {marks[i] === 'skipped' && <span className="sr-only"> (not done)</span>}
        </li>
      ))}
    </ol>
  );
}

/** A row's state glyph on What happens and What was done: done, next, later or not done. */
function PartGlyph({ mark }: { mark: 'done' | 'next' | 'todo' | 'not' | 'yours' }) {
  return (
    <span className={clsx('add-glyph', `is-${mark}`)} aria-hidden>
      {mark === 'done' ? <IconCheck size={14} stroke={2} /> : mark === 'next' || mark === 'yours' ? <IconArrowRight size={14} stroke={2} /> : mark === 'not' ? <IconMinus size={14} stroke={2} /> : null}
    </span>
  );
}

/** One row of a step list: its glyph, title, a line under it, more under that, and an action on the right. */
function StepRow({ mark, title, meta, children, trailing, label }: { mark: 'done' | 'next' | 'todo' | 'not' | 'yours'; title: ReactNode; meta?: ReactNode; children?: ReactNode; trailing?: ReactNode; label?: string }) {
  return (
    <li className="add-row" data-row={label}>
      <PartGlyph mark={mark} />
      <div className="add-row-text">
        <p className="add-row-title">{title}</p>
        {meta && <p className="add-row-meta">{meta}</p>}
        {children}
      </div>
      {trailing && <div className="add-row-trailing">{trailing}</div>}
    </li>
  );
}

/** A line of code with its own Copy button. */
function CopyLine({ text, label, className }: { text: string; label: string; className?: string }) {
  const { copied, copy } = useCopy();
  return (
    <span className={clsx('add-copy-line', className)}>
      <code className="add-code">{text}</code>
      <Button variant="ghost" size="xs" aria-label={label} onClick={() => copy(text)}>
        <IconCopy size={14} stroke={1.5} aria-hidden />{copied ? 'Copied' : 'Copy'}
      </Button>
    </span>
  );
}

// ---------------------------------------------------------------- the screen

const SUB_CONFIRM = 'First Architect writes the agent\'s files into one new folder. Connecting it to your code is the next step, and you approve each edit.';

type AskItem = Extract<Item, { kind: 'ask' }>;

export function AddScreen({ conv, model, sub }: ScreenProps) {
  const send = useApp((s) => s.send);
  const answer = useApp((s) => s.answer);
  const cancel = useApp((s) => s.cancel);
  const setWizardView = useApp((s) => s.setWizardView);
  const setWizardSkipped = useApp((s) => s.setWizardSkipped);
  const skipped = useApp((s) => !!s.wizardSkipped[conv.id]);
  const setScreen = useApp((s) => s.setScreen);
  const openDock = useApp((s) => s.openDock);
  const openConversation = useApp((s) => s.openConversation);
  const [filesOpen, setFilesOpen] = useState(false);
  const [why, setWhy] = useState<string | null>(null);

  const ready = conv.status === 'ready';
  const running = conv.timeline.running;
  const items = conv.timeline.items;
  const cwd = conv.state.cwd;
  const project = projectName(cwd);
  const name = model.result?.name;
  const revision = model.result?.revision;
  const rows = exportRows(model);
  const folder = rows.folder;
  const files = rows.files;
  const size = exportSize(model);
  const folderPath = folder && cwd ? `${cwd.replace(/[\\/]+$/, '')}/${folder}` : undefined;
  const connect = connectState(items);
  const back = () => setWizardView(conv.id, cameFrom.get(conv.id) ?? 'results');
  const reveal = (path: string | undefined) => () => {
    if (path) void native()?.reveal(path);
  };
  const sendConnect = () => connectElsewhere(conv, model);
  const elsewhere = handedOff.get(conv.id);
  const activate = useApp((s) => s.activate);
  const elsewhereOpen = useApp((s) => !!elsewhere && s.prefs.open.some((o) => o.id === elsewhere));
  const marks = partMarks(sub, skipped, connect.allowed, skipped && elsewhereOpen);

  // Add to project from the stepper: the export starts at once, as the button's does.
  const started = useRef(false);
  useEffect(() => {
    if (sub !== 'add_ready' || started.current || !ready || running) return;
    started.current = true;
    startAdd(conv, model, cameFrom.get(conv.id) ?? 'results');
  }, [sub, ready, running, conv, model]);

  const exportItem = items.find((i): i is AskItem => i.kind === 'ask' && i.ask.kind === 'approval' && i.ask.action === 'export' && !i.closed && i.answer === undefined);
  const stop = <Button key="stop" variant="outline" size="md" data-action="stop" onClick={() => void cancel(conv.id)}><IconSquare size={14} stroke={1.5} aria-hidden />Stop adding</Button>;
  const nameLine = name ? `as ${name}${revision !== undefined ? `, version ${revision}` : ''}` : undefined;

  // ---- write the files: preparing, the question, writing, not written
  if (sub === 'add_ready' || sub === 'add_preparing' || sub === 'add_confirm' || sub === 'add_writing') {
    const confirming = sub === 'add_confirm' && !!exportItem;
    const decline = () => {
      if (!exportItem) return;
      void answer(conv.id, exportItem.ask.id, 'no');
      const note = why?.trim();
      if (note) void send(conv.id, note);
      back();
    };
    const working = sub === 'add_writing' ? 'Writing the files'
      : sub === 'add_preparing' ? 'Preparing the files'
        : sub === 'add_ready' && running ? 'Waiting for Architect to finish what it is doing'
          : null;
    // add_ready with nothing running: the export starts as soon as Architect is up (the effect above).
    const idle = sub === 'add_ready' && !running ? (ready ? 'Preparing the files' : 'The files are prepared once Architect is running again.') : null;
    const bar: BarSpec = {
      back: { label: 'Back', run: confirming ? decline : back, disabled: !confirming },
      secondary: confirming && why === null ? [<Button key="why" variant="link" size="sm" data-action="say-why" onClick={() => setWhy('')}>Don't write them, and tell Architect why</Button>] : undefined,
      ask: false,
      note: 'Nothing has been written yet.',
      primary: (
        <Button
          variant="primary"
          size="md"
          data-action="write"
          pending={sub === 'add_writing'}
          disabled={!confirming || !ready}
          onClick={() => exportItem && void answer(conv.id, exportItem.ask.id, 'yes')}
        >
          Write the files<IconArrowRight size={16} stroke={1.5} aria-hidden />
        </Button>
      ),
    };
    return (
      <WizardPage
        status={<AddProgress marks={marks} />}
        title="Add the agent to your project"
        sub={SUB_CONFIRM}
        bar={bar}
      >
        <Card className="add-card" aria-label="Project">
          <CardHeader>Project</CardHeader>
          <CardBody>
            <p className="add-project">
              <IconFolder size={16} stroke={1.5} aria-hidden />
              <span className="add-project-name">{project}</span>
              {cwd && <span className="add-project-path mono">{cwd}</span>}
            </p>
          </CardBody>
        </Card>
        <Card className="add-card" aria-label="Write the files">
          <CardHeader>Write the files</CardHeader>
          <CardBody>
            {working || idle || !folder ? (
              <p className="add-wait">{(working || (idle && ready) || !idle) && <Working size="sm" />}{working ?? idle ?? 'Preparing the files'}</p>
            ) : (
              <>
                <p className="add-line">
                  {files.length} {files.length === 1 ? 'file' : 'files'} in one new folder, <code className="add-code">{folder}</code>. Nothing in your app uses it yet. Writing the files changes no existing file.
                </p>
                {size && <p className="add-line is-mute num">{size}</p>}
                {files.length > 0 && (
                  <>
                    <Button variant="link" size="sm" className="add-disclose" aria-expanded={filesOpen} onClick={() => setFilesOpen((o) => !o)}>
                      {filesOpen ? 'Hide the files' : 'Show the files'}
                      <IconChevronDown size={14} stroke={1.5} aria-hidden className={clsx('add-chevron', filesOpen && 'is-open')} />
                    </Button>
                    {filesOpen && (
                      <RowList aria-label="The files" className="add-files">
                        {files.map((file) => (
                          <Row key={file} title={<span className="mono">{file}</span>} meta={filePurpose(file)} leading={<IconFile size={16} stroke={1.5} aria-hidden />} />
                        ))}
                      </RowList>
                    )}
                  </>
                )}
              </>
            )}
            {why !== null && (
              <Input
                autoFocus
                size="sm"
                className="add-why"
                aria-label="What should Architect do instead?"
                placeholder="What should Architect do instead?"
                value={why}
                onChange={(e) => setWhy(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    e.preventDefault();
                    decline();
                  }
                }}
              />
            )}
          </CardBody>
        </Card>
      </WizardPage>
    );
  }

  if (sub === 'add_refused') {
    const refusal = exportRefusal(model, items);
    const exists = refusal?.code === 'export_target_exists';
    // The notice names the folder: "not exported (export_target_exists: agents/receipt_reader)" (build.py).
    const named = exists ? /export_target_exists:\s*([^)\s]+)/.exec(refusal?.text ?? '')?.[1] : undefined;
    const target = exists ? (named ? `${named.replace(/\/+$/, '')}/` : folder) : undefined;
    const subText = exists
      ? `${target ?? 'The folder'} already exists in this project. Architect never overwrites files.`
      : refusal ? noticeWords(refusal.text) : undefined;
    const primary = exists
      ? <Button variant="primary" size="md" data-action="folder" onClick={reveal(target && cwd ? `${cwd.replace(/[\\/]+$/, '')}/${target}` : folderPath)}><IconFolder size={16} stroke={1.5} aria-hidden />Open folder</Button>
      : <Button variant="primary" size="md" data-action="again" disabled={!ready || running || !name} onClick={() => name && void send(conv.id, `/export ${name}`)}>Try again</Button>;
    return (
      <WizardPage status={<AddProgress marks={marks} />} title="The files were not written" sub={subText} bar={{ back: { label: 'Back', run: back }, ask: { placeholder: 'Ask Architect about the agent', disabled: !ready }, primary }}>
        {null}
      </WizardPage>
    );
  }

  // ---- connect it: the edit question, a question, working
  if (sub === 'connect_approve' && connect.open) {
    const ask = connect.open;
    const action = connectAction(ask) ?? 'edit';
    const preview = action === 'bash' ? null : previewChanges(ask.detail);
    const path = preview ? projectPath(preview.path, cwd) : undefined;
    const title = action === 'bash'
      ? 'Allow this command?'
      : action === 'write'
        ? <>Allow Architect to create <code className="wizard-title-code">{path ?? 'this file'}</code>?</>
        : <>Allow this edit to <code className="wizard-title-code">{path ?? 'this file'}</code>?</>;
    const said = saidSteps(connect.said);
    const verb = action === 'bash' ? 'Allow this command' : 'Allow this edit';
    // Line counts only from a whole preview: a cut one would undercount, and the coder reports the real ones after.
    const counts = preview && !preview.cut
      ? { added: preview.changes.reduce((n, c) => n + c.added, 0), removed: preview.changes.reduce((n, c) => n + c.removed, 0) }
      : undefined;
    return (
      <WizardPage
        status={<AddProgress marks={marks} />}
        title={title}
        sub={action === 'bash'
          ? 'Architect wants to run this in your project. Nothing runs until you allow it.'
          : 'Architect wants to change this file in your project. Nothing is saved until you allow it.'}
        bar={{
          secondary: [stop], ownStop: true,
          ask: { placeholder: 'Ask Architect about this edit', disabled: !ready },
          primary: (
            <>
              <Button variant="outline" size="md" data-action="deny" disabled={!ready} onClick={() => void answer(conv.id, ask.id, 'no')}>Don't allow</Button>
              <Button variant="primary" size="md" data-action="allow" disabled={!ready} onClick={() => void answer(conv.id, ask.id, 'yes')}>
                <IconCheck size={16} stroke={1.5} aria-hidden />{verb}
              </Button>
            </>
          ),
        }}
      >
        <Card className="add-card add-change" aria-label="What it changes">
          <CardHeader action={counts ? <span className="add-counts num"><span className="is-add">+{counts.added}</span> <span className="is-del">{'−'}{counts.removed}</span> lines</span> : undefined}>
            What it changes
          </CardHeader>
          {(said.lead || said.steps.length > 0) && (
            <CardBody className="add-said" aria-label="What Architect says it will do">
              <p className="add-said-label">{said.steps.length ? "Architect's own summary of the edit:" : 'Architect says:'}</p>
              {said.steps.length ? (
                <ol className="add-said-steps">
                  {said.steps.map((step, i) => <li key={i}>{step}</li>)}
                </ol>
              ) : <p className="add-line">{said.lead}</p>}
            </CardBody>
          )}
          <CardBody pad="bleed" className="add-diff">
            {action === 'bash'
              ? <pre className="add-command mono">$ {ask.detail ?? ''}</pre>
              : ask.detail ? <ChangePreview detail={ask.detail} path={path} /> : null}
          </CardBody>
        </Card>
      </WizardPage>
    );
  }

  if (sub === 'connect_question' && connect.open) {
    const item = items.find((i): i is AskItem => i.kind === 'ask' && i.ask.id === connect.open!.id);
    return (
      <WizardPage status={<AddProgress marks={marks} />} title="Architect has a question" bar={{ secondary: [stop], ownStop: true, ask: false }}>
        <Card className="add-card">
          <CardBody>
            {item && <AskCard item={item} onAnswer={(ask, value) => void answer(conv.id, ask.id, value)} />}
          </CardBody>
        </Card>
      </WizardPage>
    );
  }

  if (sub === 'connecting') {
    return (
      <WizardPage
        status={<AddProgress marks={marks} />}
        title="Connecting it in your code"
        sub="Architect is reading your code. You will see each edit before it is saved."
        bar={{ secondary: [stop], ownStop: true, ask: false }}
      >
        <Card className="add-card">
          <CardBody>
            <p className="add-wait"><Working size="sm" />{coderLine(items) ?? 'Reading your code'}</p>
          </CardBody>
        </Card>
      </WizardPage>
    );
  }

  // ---- files written, connected, not connected
  const filesMeta = (
    <>
      <code className="add-code">{folder}</code>
      {' · '}<span className="num">{size ?? `${files.length} files`}</span>
    </>
  );
  const readme = folderPath ? () => void native()?.openPath(`${folderPath.replace(/\/+$/, '')}/README.md`) : undefined;

  if (sub === 'files_written') {
    return (
      <WizardPage
        status={(
          <div className="add-status-stack">
            <AddProgress marks={marks} />
            <span className="add-status">
              <StatusMark tone="ok" label="Files written" showLabel />
              {folder && <code className="add-code">{folder}</code>}
              {nameLine && <span>{nameLine}</span>}
            </span>
          </div>
        )}
        title="Connect it in your code"
        sub="Architect's coder can find where your app does this job today and switch it to the agent. You see every edit and allow it or not."
        bar={{
          secondary: [<Button key="skip" variant="outline" size="md" data-action="skip" onClick={() => { handedOff.delete(conv.id); setWizardSkipped(conv.id, true); }}>Skip: I'll connect it myself</Button>],
          ask: { placeholder: 'Ask Architect about the agent', disabled: !ready },
          primary: (
            <Button variant="primary" size="md" data-action="connect" disabled={!ready || running} onClick={sendConnect}>
              Connect it<IconArrowRight size={16} stroke={1.5} aria-hidden />
            </Button>
          ),
        }}
      >
        <Card className="add-card" aria-label="What happens">
          <CardHeader>What happens</CardHeader>
          <CardBody pad="flush">
            <ol className="add-rows">
              <StepRow mark="done" label="files" title="Wrote the agent's files" meta={filesMeta}
                trailing={<Button variant="ghost" size="sm" onClick={reveal(folderPath)}><IconFolder size={14} stroke={1.5} aria-hidden />Open folder</Button>} />
              <StepRow mark="next" label="connect" title="Connect it in your code"
                meta={<>Architect reads <code className="add-code">{folder}README.md</code> and your code, then proposes the edit.</>} />
              <StepRow mark="todo" label="switch" title="Switch it on and run the tests" meta="You do this last." />
            </ol>
          </CardBody>
        </Card>
      </WizardPage>
    );
  }

  // connected, connect_failed, or skipped
  const flag = flagLine(model);
  const tests = testsCommand(model);
  const allowed = connect.allowed;
  const failed = sub === 'connect_failed';
  const connected = !skipped && !failed && allowed > 0;
  const continued = skipped && elsewhereOpen;
  const applied = connect.applied;
  const allowedEdit = connect.edits.find((e) => (e.answer === 'yes' || e.answer === 'session') && connectAction(e.ask) !== 'bash');
  const allowedDiff = allowedEdit ? approvalDiff(allowedEdit.ask.detail) : null;
  const changedPath = applied[0] ? projectPath(applied[0].path, cwd) : allowedDiff ? projectPath(allowedDiff.path, cwd) : undefined;
  const added = applied.reduce((n, a) => n + a.added, 0);
  const removed = applied.reduce((n, a) => n + a.removed, 0);
  const subLine = connected
    ? <>The files are written and <code className="add-code">{allowedDiff?.fn ? `${allowedDiff.fn}()` : changedPath ?? 'your code'}</code> now calls the agent. One thing is left: switch it on.</>
    : continued
      ? 'The files are written. Connecting it continues in a new conversation: the request is typed in there, ready to send, and you allow each edit.'
      : skipped
        ? 'The files are written. Connect it in your code when you\'re ready: the README says how.'
        : 'The files are written. Your code was not changed.';
  const connectRow = connected ? (
    <StepRow mark="done" label="connect" title="Connected it in your code"
      meta={(
        <>
          {changedPath && <code className="add-code">{changedPath}</code>}
          {applied.length > 0 && <>{' · '}<span className="add-counts num"><span className="is-add">+{added}</span> <span className="is-del">{'−'}{removed}</span> lines</span></>}
          {' · '}{allowed === 1 ? 'you allowed this edit' : `you allowed ${allowed} edits`}
        </>
      )}
      trailing={<Button variant="ghost" size="sm" onClick={() => openDock('changes')}>View change</Button>} />
  ) : continued ? (
    <StepRow mark="next" label="connect" title="Continue in the conversation" meta="Send the request there, then allow or refuse each edit."
      trailing={elsewhereOpen ? <Button variant="ghost" size="sm" data-action="open-conversation" onClick={() => activate(elsewhere!)}>Open the conversation</Button> : undefined} />
  ) : (
    <StepRow mark="not" label="connect" title={failed && connect.failure ? `Not connected: ${connect.failure}` : 'Not connected'}
      trailing={<Button variant="ghost" size="sm" data-action="connect" disabled={!ready || running} onClick={sendConnect}>{failed ? 'Try again' : 'Connect it'}</Button>} />
  );
  return (
    <WizardPage
      status={(
        <div className="add-status-stack">
          <AddProgress marks={marks} />
          <span className="add-status">
            <StatusMark tone="ok" label="Added" showLabel />
            {project && <span>{project}</span>}
            {nameLine && <span>{nameLine}</span>}
          </span>
        </div>
      )}
      title="Your agent is in your project."
      sub={subLine}
      warning={failed ? connect.failure : undefined}
      bar={{
        secondary: [<Button key="another" variant="outline" size="md" data-action="another" onClick={() => openConversation({ cwd: cwd ?? '', kind: 'forge' })}>Build another agent</Button>],
        ask: false,
        primary: (
          <Button variant="primary" size="md" data-action="agents" onClick={() => setScreen('agents')}>
            <IconLayoutGrid size={16} stroke={1.5} aria-hidden />See it in Your agents
          </Button>
        ),
      }}
    >
      {flag && <SwitchOn flag={flag} connected={connected} fingerprint={typeof model.plan?.data?.spec_hash === 'string' ? model.plan.data.spec_hash : undefined} />}
      <Card className="add-card" aria-label="What was done">
        <CardHeader action={<Button variant="ghost" size="sm" onClick={reveal(folderPath)}><IconFolder size={14} stroke={1.5} aria-hidden />Open folder</Button>}>
          What was done
        </CardHeader>
        <CardBody pad="flush">
          <ol className="add-rows">
            <StepRow mark="done" label="files" title="Wrote the agent's files" meta={filesMeta}
              trailing={readme ? <Button variant="ghost" size="sm" onClick={readme}><IconFileDescription size={14} stroke={1.5} aria-hidden />Read the README</Button> : undefined} />
            {connectRow}
            <StepRow mark="yours" label="tests" title="Your turn: run the tests" meta="Architect leaves the tests to you. With the flag set, run the agent's own tests, then your project's:">
              <div className="add-tests-row">
                {tests && <CopyLine text={tests} label="Copy the test command" className="add-tests" />}
                <Button variant="ghost" size="sm" className="add-terminal" onClick={() => {
                  openDock('terminal');
                  if (cwd) window.dispatchEvent(new CustomEvent('architect:terminal', { detail: cwd }));
                }}>
                  <IconTerminal2 size={14} stroke={1.5} aria-hidden />Open a terminal here
                </Button>
              </div>
            </StepRow>
          </ol>
          {rows.line && (
            <p className="add-anywhere">
              <span>To call it anywhere else:</span>
              <CopyLine text={rows.line} label="Copy the import line" />
            </p>
          )}
        </CardBody>
      </Card>
    </WizardPage>
  );
}

/** Switch it on: the flag line to set, with its copy button. */
function SwitchOn({ flag, connected, fingerprint }: { flag: string; connected: boolean; fingerprint?: string }) {
  const { copied, copy } = useCopy();
  // The flag's name ends in the start of the agent's spec fingerprint (export.py): say so when it does.
  const suffix = /_([0-9A-F]{6,})=1$/.exec(flag)?.[1];
  const own = suffix && fingerprint?.toUpperCase().startsWith(suffix) ? suffix : undefined;
  return (
    <Card className="add-card add-switch" aria-label="Switch it on">
      <CardHeader>Switch it on</CardHeader>
      <CardBody>
        <p className="add-line">
          The agent starts switched off, so nothing new runs in your app until you have looked it over. To switch it on, add this line to the environment your app runs in, for example its <code className="add-code">.env</code> file or your host's settings.{connected ? ' Until it is set, the new code stops with an error, so set it before you run your app.' : ''}
        </p>
        <div className="add-flag">
          <code className="add-flag-line mono">{flag}</code>
          <Button variant="primary" size="sm" data-action="copy-flag" onClick={() => copy(flag)}>
            <IconCopy size={14} stroke={1.5} aria-hidden />{copied ? 'Copied' : 'Copy the flag line'}
          </Button>
        </div>
        {own && <p className="add-line is-mute">{own} is the start of this agent's fingerprint, so each agent you add has its own switch.</p>}
      </CardBody>
    </Card>
  );
}

/** The coder's latest step, in the app's words, while Connect it runs. */
function coderLine(items: Item[]): string | undefined {
  for (let i = items.length - 1; i >= 0; i -= 1) {
    const item = items[i];
    if (item.kind === 'plan' && item.title.startsWith('Exported')) return undefined;
    if (item.kind !== 'work') continue;
    for (let s = item.steps.length - 1; s >= 0; s -= 1) {
      const step = item.steps[s];
      const lines = step.subagent ? [{ agent: step.name, text: step.summary }, ...step.lines] : step.lines;
      const last = lines.filter((l) => l.agent === 'coder' && l.text.trim()).at(-1);
      if (last) return friendlyLine(last.text.trim().split('\n')[0]);
    }
  }
  return undefined;
}
