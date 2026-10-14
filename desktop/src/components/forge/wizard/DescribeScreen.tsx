/**
 * Step 1, Describe (clarity spec §3, mockup 1-describe): the job in one sentence. The
 * field is the describe Composer (its @-file list, its draft, its worktree start); the
 * examples under it fill the field and send nothing; "Open a saved plan" puts `/make @`
 * in it with the file list open. The bar's Write the plan sends what the field holds,
 * through the Composer's own send (Mod+Enter on its field), so every rule of a first
 * message (a slash command, a worktree, a missing tool) holds here too.
 *
 * A reopened build loading its history is not a new one: it shows only "Opening this build".
 */
import { IconArrowRight, IconFile, IconFolder } from '@tabler/icons-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { isMac } from '../../../lib/shortcuts';
import { projectName } from '../../../lib/wizard';
import { useApp, type Conv } from '../../../store/app';
import { Composer, DESCRIBE_EXAMPLES } from '../../chat/Composer';
import { Button } from '../../ui/Button';
import { Card, CardBody } from '../../ui/Card';
import { Row, RowList } from '../../ui/RowList';
import { WorkingRegion } from '../../ui/Working';
import { countWord, modelFamily } from './PlanScreen';
import type { ScreenProps } from './types';
import { WizardPage } from './WizardPage';
import '../../../styles/wizard-plan.css';

/** The describe field's text in the Composer: set it as "Edit and send again" does (its draft event). */
function fillField(convId: string, text: string): void {
  useApp.getState().setDraft(convId, text);
  window.dispatchEvent(new CustomEvent('architect:draft', { detail: text }));
}

/** The models the profile's build tries (B5, `m_models().ladder`), once the conversation is ready. */
function useLadder(conv: Conv): string[] | undefined {
  const request = useApp((s) => s.request);
  const ready = conv.status === 'ready';
  const [ladder, setLadder] = useState<string[]>();
  useEffect(() => {
    if (!ready) return undefined;
    let live = true;
    request<{ ladder?: unknown }>(conv.id, 'models')
      .then((reply) => {
        if (live && Array.isArray(reply?.ladder)) setLadder(reply.ladder.filter((m): m is string => typeof m === 'string'));
      })
      .catch(() => undefined);
    return () => { live = false; };
  }, [conv.id, ready, request]);
  return ladder;
}

/**
 * "Describe one small task. Architect tests three NVIDIA Nemotron models on example cases
 * of it ...". Until the profile's list arrives (or when it cannot be read) the sentence
 * names no family and no count: that would be a guess.
 */
export function describeSub(ladder: string[] | undefined): string {
  const family = ladder ? modelFamily(ladder) : undefined;
  const what = ladder?.length
    ? `${countWord(ladder.length)} ${family ? `NVIDIA ${family} ` : ''}${ladder.length === 1 ? 'model' : 'models'}`
    : 'several models';
  return `Describe one small task. Architect tests ${what} on example cases of that task and gives you the cheapest one that does it well.`;
}

export function DescribeScreen({ conv }: ScreenProps) {
  const session = useApp((s) => !!s.prefs.open.find((o) => o.id === conv.id)?.session);
  const draft = useApp((s) => s.prefs.drafts[conv.id] ?? '');
  const send = useApp((s) => s.send);
  const ladder = useLadder(conv);
  const field = useRef<HTMLDivElement>(null);
  // What the field holds: typed (its input events), filled here, or caught up through the saved draft.
  const [text, setText] = useState(draft);
  useEffect(() => setText(draft), [draft]);
  const fill = useCallback((value: string) => {
    setText(value);
    fillField(conv.id, value);
  }, [conv.id]);

  const loadingHistory = !!conv.resuming || (conv.status === 'starting' && session && conv.timeline.items.length === 0);
  if (loadingHistory) {
    return <div className="describe-loading"><WorkingRegion label="Opening this build" /></div>;
  }

  const ready = conv.status === 'ready';
  const project = projectName(conv.state.cwd);
  const write = () => {
    const box = field.current?.querySelector('textarea');
    if (box) {
      // The Composer sends on Mod+Enter whichever send key the person chose.
      box.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true, ctrlKey: !isMac(), metaKey: isMac() }));
      return;
    }
    const message = text.trim();
    if (message) void send(conv.id, message);
  };

  return (
    <WizardPage
      title="What job should your agent do?"
      sub={describeSub(ladder)}
      bar={{
        note: (
          <span className="plan-note">
            {ready ? (
              <>
                <span>Type the job or pick an example to continue.</span>
                <span>You see the plan and the most it can cost before anything runs.</span>
              </>
            ) : <span>Starting Architect</span>}
          </span>
        ),
        primary: (
          <Button variant="primary" size="md" data-action="write-plan" disabled={!ready || !text.trim()} onClick={write}>
            Write the plan<IconArrowRight size={16} stroke={1.5} aria-hidden />
          </Button>
        ),
      }}
    >
      <Card className="describe-step">
        <CardBody>
          <span id={`describe-label-${conv.id}`} className="describe-step-label">The job, in one sentence</span>
          <div
            ref={field}
            className="describe-step-field"
            onInput={(e) => {
              const target = e.target as HTMLElement;
              if (target instanceof HTMLTextAreaElement) setText(target.value);
            }}
          >
            <Composer conv={conv} variant="describe" labelledBy={`describe-label-${conv.id}`} />
          </div>
          {project && (
            <p className="describe-where">
              <IconFolder size={16} stroke={1.5} aria-hidden />
              <span>The finished agent goes into <b>{project}</b>, the project you opened Architect in.</span>
            </p>
          )}
          <div className="describe-examples-head">
            <span>Or start from an example</span>
            <Button variant="link" size="sm" data-action="spec" disabled={!ready} onClick={() => fill('/make @')}>
              <IconFile size={14} stroke={1.5} aria-hidden />Open a plan file you saved (.json)
            </Button>
          </div>
          <RowList className="describe-example-list" aria-label="Examples">
            {DESCRIBE_EXAMPLES.map((example) => (
              <Row
                key={example}
                title={example}
                onSelect={() => fill(example)}
                trailing={<IconArrowRight size={16} stroke={1.5} aria-hidden />}
              />
            ))}
          </RowList>
        </CardBody>
      </Card>
    </WizardPage>
  );
}
