/**
 * Changes: the folder's diff as a review surface (Claude Code's diff viewer, Codex's
 * review pane). Scopes: Unstaged (default), Staged, Branch (against the default
 * branch) and All (everything since the last commit, or Architect's own checkpoint
 * outside git). Stage, unstage and revert a whole file or a single hunk; click a line
 * to comment and send every comment to Architect at once (Ctrl+Enter).
 */
import {
  IconArrowBackUp, IconChevronDown, IconChevronUp, IconFileDiff, IconGitBranch, IconSparkles, IconLayoutList, IconChevronRight, IconGitCommit, IconMessagePlus, IconMinus, IconPlus, IconRefresh, IconSearch,
  IconSend, IconX,
} from '@tabler/icons-react';
import { isMac, shortcutLabel } from '../../lib/shortcuts';
import clsx from 'clsx';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { highlight, languageFor } from '../../lib/highlight';
import { filePatch, hunkPatch, parsePatch, type FilePatch, type Hunk } from '../../lib/patch';
import { BRANCH_REVIEW, latestFindings, REVIEW_FORMAT, sameFile } from '../../lib/review';
import { useApp, type Conv } from '../../store/app';
import { Menu } from '../ui/Menu';
import { parseDiff } from '../chat/DiffBlock';
import { useConfirm } from '../ui/useConfirm';
import { WorkingRegion } from '../ui/Working';

type Scope = 'unstaged' | 'staged' | 'branch' | 'all';
interface Comment { file: string; line: string; lineNo: number; text: string; /** From Architect's review, not typed here. */ finding?: boolean }

export function ChangesPane({ conv }: { conv: Conv }) {
  const send = useApp((s) => s.send);
  const refreshDiff = useApp((s) => s.refreshDiff);
  const refreshGit = useApp((s) => s.refreshGit);
  const gitCall = useApp((s) => s.gitCall);
  const openDock = useApp((s) => s.openDock);
  const showToast = useApp((s) => s.showToast);
  const git = conv.git;
  const repo = !!git?.repo;
  const cwd = conv.state.cwd ?? '';
  const [scope, setScope] = useState<Scope>(repo ? 'unstaged' : 'all');
  const [confirm, confirmDialog] = useConfirm();
  const [text, setText] = useState<string | null>(null);
  const [comments, setComments] = useState<Comment[]>([]);
  const [draft, setDraft] = useState<{ file: string; key: string; line: string; lineNo: number } | null>(null);
  const [note, setNote] = useState('');
  const ready = conv.status === 'ready';
  const [reviewMenu, setReviewMenu] = useState(false);
  // Findings from the latest review sit beside the lines they are about.
  const findings = useMemo(() => latestFindings(conv.timeline.items), [conv.timeline.items]);
  const reviewBranch = async () => {
    const r = (await fetch(`/api/git/diff?cwd=${encodeURIComponent(cwd)}&scope=branch`).then((x) => x.json()).catch(() => ({ text: '' }))) as { text: string };
    if (!r.text.trim()) {
      showToast('This branch has no changes against its base.');
      return;
    }
    const diff = r.text.length > 60_000 ? `${r.text.slice(0, 60_000)}\n... [diff clipped]` : r.text;
    void send(conv.id, `${BRANCH_REVIEW} ${REVIEW_FORMAT}\n\n<diff>\n${diff}\n</diff>`);
  };
  const [listOn, setListOn] = useState(() => localStorage.getItem('architect-review-list') !== '0');
  const [listPct, setListPct] = useState(() => Number(localStorage.getItem('architect-review-list-w')) || 32);
  const [current, setCurrent] = useState<string | null>(null);
  const [listQuery, setListQuery] = useState('');
  const body = useRef<HTMLDivElement>(null);
  const filesBox = useRef<HTMLDivElement>(null);

  // Outside git there is only "All"; when git status arrives late, start on Unstaged
  // unless a scope was already picked.
  const picked = useRef(false);
  useEffect(() => {
    if (!repo && scope !== 'all') setScope('all');
    else if (repo && scope === 'all' && !picked.current) setScope('unstaged');
  }, [repo, scope]);

  const load = useCallback(async () => {
    if (scope === 'all') {
      setText(conv.diff?.text ?? '');
      return;
    }
    setText(null);
    try {
      const r = (await fetch(`/api/git/diff?cwd=${encodeURIComponent(cwd)}&scope=${scope}`).then((x) => x.json())) as { text: string };
      setText(r.text);
    } catch {
      setText('');
    }
  }, [scope, cwd, conv.diff]);

  useEffect(() => {
    void load();
  }, [load, git?.staged, git?.unstaged, git?.untracked]);

  const files = useMemo(() => (text ? parsePatch(text) : []), [text]);
  // Outside git, Architect's /diff lists new files after the patch.
  const untrackedOnly = useMemo(() => (scope === 'all' && text ? parseDiff(text).filter((f) => f.untracked) : []), [scope, text]);
  const added = files.reduce((n, f) => n + f.added, 0);
  const paths = useMemo(() => [...files.map((f) => f.path), ...untrackedOnly.map((f) => f.path)], [files, untrackedOnly]);

  // Go to a file: scroll its section to the top and remember it as the current one.
  const goTo = (path: string) => {
    setCurrent(path);
    const el = filesBox.current?.querySelector<HTMLElement>(`[data-file="${CSS.escape(path)}"]`);
    if (el && filesBox.current) filesBox.current.scrollTo({ top: el.offsetTop - 10, behavior: 'smooth' });
  };
  const step = (delta: number) => {
    if (!paths.length) return;
    const at = current ? paths.indexOf(current) : -1;
    goTo(paths[Math.max(0, Math.min(paths.length - 1, at + delta))]);
  };
  const toggleList = () => {
    setListOn(!listOn);
    try {
      localStorage.setItem('architect-review-list', listOn ? '0' : '1');
    } catch {
      /* storage blocked */
    }
  };
  const onDragList = (event: React.PointerEvent) => {
    const target = event.currentTarget as HTMLElement;
    target.setPointerCapture(event.pointerId);
    const box = body.current?.getBoundingClientRect();
    if (!box) return;
    const move = (e: PointerEvent) => setListPct(Math.max(18, Math.min(55, ((e.clientX - box.left) / box.width) * 100)));
    const up = () => {
      target.removeEventListener('pointermove', move);
      target.removeEventListener('pointerup', up);
      try {
        localStorage.setItem('architect-review-list-w', String(Math.round(listPct)));
      } catch {
        /* storage blocked */
      }
    };
    target.addEventListener('pointermove', move);
    target.addEventListener('pointerup', up);
  };
  const removed = files.reduce((n, f) => n + f.removed, 0);

  const act = async (label: string, op: string, body: Record<string, unknown>) => {
    try {
      await gitCall(conv.id, op, body);
      await load();
    } catch (err) {
      showToast(`${label}: ${(err as { detail?: string }).detail || 'failed'}`, 'error');
    }
  };

  const revert = (what: string, op: () => Promise<void>) => {
    void confirm({ title: `Discard ${what} for good?`, keep: 'Keep', action: 'Discard' })
      .then((ok) => { if (ok) void op(); });
  };

  const submit = () => {
    if (!comments.length) return;
    const body = comments.map((c) => `${c.file} (line ${c.lineNo}): \`${c.line.trim()}\`\n${c.text}`).join('\n\n');
    void send(conv.id, `Please address these review comments on your changes:\n\n${body}`);
    setComments([]);
  };

  return (
    <div className="changes" onKeyDown={(e) => {
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        submit();
      }
    }}>
      <div className="changes-bar">
        {repo ? (
          <div className="seg scope-seg" role="tablist" aria-label="What to review">
            {(['unstaged', 'staged', 'branch', 'all'] as const).map((s) => (
              <button key={s} type="button" role="tab" aria-selected={scope === s} className={clsx('seg-item', scope === s && 'seg-on')} onClick={() => { picked.current = true; setScope(s); }}>
                {{ unstaged: 'Unstaged', staged: 'Staged', branch: 'Branch', all: 'All' }[s]}
                {s === 'unstaged' && git && git.unstaged + git.untracked > 0 && <span className="scope-count num">{git.unstaged + git.untracked}</span>}
                {s === 'staged' && git && git.staged > 0 && <span className="scope-count num">{git.staged}</span>}
              </button>
            ))}
          </div>
        ) : (
          <span className="tone-faint small">Changes since Architect's last checkpoint</span>
        )}
        <span className="spacer" />
        <button type="button" className="icon-button" aria-label="Refresh" data-tip="Refresh" data-tip-side="down" onClick={() => { void refreshDiff(conv.id); void refreshGit(conv.id); void load(); }}><IconRefresh size={15} /></button>
        <span className="bar-anchor">
          <button type="button" className="button button-small" disabled={!ready} onClick={() => setReviewMenu(!reviewMenu)}><IconSearch size={14} aria-hidden /> Review <IconChevronDown size={12} aria-hidden /></button>
          {reviewMenu && (
            <Menu
              side="down"
              align="end"
              width={290}
              onClose={() => setReviewMenu(false)}
              items={[
                { id: 'working', label: 'Review uncommitted changes', icon: IconFileDiff },
                { id: 'branch', label: 'Review this branch against its base', icon: IconGitBranch, disabled: !repo },
              ]}
              onPick={(id) => {
                setReviewMenu(false);
                if (id === 'working') void send(conv.id, `/review ${REVIEW_FORMAT}`);
                else void reviewBranch();
              }}
            />
          )}
        </span>
      </div>

      {repo && files.length > 0 && (
        <div className="changes-summary">
          {/* The tab badges already count files; the count is in the tooltip. */}
          <span className="num" data-tip={`${files.length} ${files.length === 1 ? 'file' : 'files'}`} data-tip-side="down"><span className="tone-ok">+{added}</span> <span className="tone-err">-{removed}</span></span>
          <span className="spacer" />
          <span className="review-nav">
            <button type="button" className={clsx('icon-button tiny', listOn && 'is-on')} aria-label="File list" aria-pressed={listOn} data-tip="File list" data-tip-side="down" onClick={toggleList}><IconLayoutList size={14} /></button>
            <button type="button" className="icon-button tiny" aria-label="Previous file" data-tip="Previous file" data-tip-side="down" onClick={() => step(-1)}><IconChevronUp size={14} /></button>
            <button type="button" className="icon-button tiny" aria-label="Next file" data-tip="Next file" data-tip-side="down" onClick={() => step(1)}><IconChevronDown size={14} /></button>
          </span>
          {scope === 'unstaged' && <button type="button" className="link-button small" onClick={() => void act('Stage all', 'stage', {})}><IconPlus size={13} aria-hidden /> Stage all</button>}
          {scope === 'staged' && <button type="button" className="link-button small" onClick={() => void act('Unstage all', 'unstage', {})}><IconMinus size={13} aria-hidden /> Unstage all</button>}
          {scope === 'unstaged' && <button type="button" className="link-button small danger" onClick={() => revert('every unstaged change', () => act('Revert all', 'revert', {}))}><IconArrowBackUp size={13} aria-hidden /> Revert all</button>}
          {scope === 'all' && <button type="button" className="link-button small" disabled={!ready} onClick={() => void send(conv.id, '/undo')}><IconArrowBackUp size={13} aria-hidden /> Undo last turn</button>}
        </div>
      )}

      {git && !git.git && <p className="pane-note">Git is not installed, so only Architect's own checkpoints are shown. Install Git to review, stage and commit here.</p>}
      {git && git.git && !git.repo && (
        <p className="pane-note">
          This folder isn't a git repository.{' '}
          <button type="button" className="link-button" onClick={() => void fetch('/api/git/init', { method: 'POST', body: JSON.stringify({ cwd }) }).then(() => refreshGit(conv.id))}>Create one</button>
        </p>
      )}

      {text === null ? <WorkingRegion label="Reading changes" /> : files.length === 0 && untrackedOnly.length === 0 ? (
        <div className="pane-empty">
          <IconGitCommit size={26} stroke={1.5} aria-hidden />
          <span>{scope === 'staged' ? 'Nothing staged.' : scope === 'branch' ? 'No commits beyond the default branch.' : 'No changes.'}</span>
        </div>
      ) : (
        <div ref={body} className={clsx('changes-body', listOn && 'with-list')}>
          {listOn && (
            <aside className="review-list" style={{ width: `${listPct}%` }} aria-label="Changed files">
              <label className="tree-search">
                <IconSearch size={13} aria-hidden />
                <input value={listQuery} placeholder="Filter files" spellCheck={false} onChange={(e) => setListQuery(e.target.value)} />
              </label>
              <div className="review-list-rows">
                {[...files.map((f) => ({ path: f.path, added: f.added, removed: f.removed, kind: f.isNew ? 'A' : f.isDeleted ? 'D' : 'M' })), ...untrackedOnly.map((f) => ({ path: f.path, added: 0, removed: 0, kind: 'A' }))]
                  .filter((f) => !listQuery || f.path.toLowerCase().includes(listQuery.toLowerCase()))
                  .map((f) => {
                    const slash = f.path.lastIndexOf('/');
                    return (
                      <button key={f.path} type="button" className={clsx('review-row', current === f.path && 'is-current')} onClick={() => goTo(f.path)} title={f.path}>
                        <span className={clsx('review-kind', `is-${f.kind}`)}>{f.kind}</span>
                        <span className="review-name">{f.path.slice(slash + 1)}</span>
                        {slash > 0 && <span className="review-dir">{f.path.slice(0, slash)}</span>}
                        {f.added + f.removed > 0 && <span className="review-stat num"><span className="tone-ok">+{f.added}</span> <span className="tone-err">-{f.removed}</span></span>}
                      </button>
                    );
                  })}
              </div>
              <div className="review-list-resize" onPointerDown={onDragList} role="separator" aria-orientation="vertical" />
            </aside>
          )}
        <div ref={filesBox} className="changes-files">
          {files.map((file) => (
            <FileSection
              key={file.path}
              file={file}
              scope={scope}
              repo={repo}
              comments={[...comments.filter((c) => c.file === file.path), ...findings.filter((f) => sameFile(f.file, file.path)).map((f) => ({ file: file.path, line: '', lineNo: f.lineNo, text: f.text, finding: true }))]}
              draftKey={draft?.file === file.path ? draft.key : null}
              onLine={(key, line, lineNo) => {
                setDraft({ file: file.path, key, line, lineNo });
                setNote('');
              }}
              onOpen={(line?: number) => openDock('files', file.path, line ?? file.hunks[0]?.newStart)}
              onStage={(patch) => void (patch ? act('Stage', 'apply', { patch, cached: true }) : act('Stage', 'stage', { file: file.path }))}
              onUnstage={(patch) => void (patch ? act('Unstage', 'apply', { patch, cached: true, reverse: true }) : act('Unstage', 'unstage', { file: file.path }))}
              onRevert={(patch) => revert(patch ? 'this change' : file.path, () => (patch ? act('Revert', 'apply', { patch, reverse: true }) : act('Revert', 'revert', { file: file.path })))}
              draftEditor={draft?.file === file.path ? (
                <div className="comment-editor">
                  <textarea
                    className="field"
                    autoFocus
                    rows={3}
                    placeholder="Leave a comment"
                    value={note}
                    onChange={(e) => setNote(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Escape') setDraft(null);
                      if (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey && !e.metaKey) {
                        e.preventDefault();
                        if (note.trim()) setComments((c) => [...c, { file: draft.file, line: draft.line, lineNo: draft.lineNo, text: note.trim() }]);
                        setDraft(null);
                      }
                    }}
                  />
                  <div className="comment-actions">
                    <button type="button" className="button button-small button-quiet" onClick={() => setDraft(null)}>Cancel</button>
                    <button type="button" className="button button-small button-primary" disabled={!note.trim()} onClick={() => {
                      setComments((c) => [...c, { file: draft.file, line: draft.line, lineNo: draft.lineNo, text: note.trim() }]);
                      setDraft(null);
                    }}>Add comment</button>
                  </div>
                </div>
              ) : null}
              onRemoveComment={(c) => setComments((all) => all.filter((x) => x !== c))}
              onFix={(c) => void send(conv.id, `Fix this review finding in ${c.file} at line ${c.lineNo}: ${c.text}`)}
            />
          ))}
          {untrackedOnly.map((f) => (
            <section key={f.path} className="change-file" data-file={f.path}>
              <header className="change-file-head">
                <span className="change-toggle"><span className="mono change-path">{f.path}</span></span>
                <span className="new-badge">New</span>
                <button type="button" className="link-button small" onClick={() => openDock('files', f.path)}>View file</button>
              </header>
            </section>
          ))}
        </div>
        </div>
      )}
      {comments.length > 0 && (
        <div className="comments-submit">
          <span>{comments.length} {comments.length === 1 ? 'comment' : 'comments'}</span>
          <button type="button" className="button button-primary button-small" disabled={!ready} onClick={submit}>
            <IconSend size={14} aria-hidden /> Send to Architect <kbd>{shortcutLabel({ key: '↵', mod: true })}</kbd>
          </button>
        </div>
      )}
      {confirmDialog}
    </div>
  );
}

function FileSection({
  file, scope, repo, comments, draftKey, onLine, onOpen, onStage, onUnstage, onRevert, draftEditor, onRemoveComment, onFix,
}: {
  onFix: (c: Comment) => void;
  file: FilePatch;
  scope: Scope;
  repo: boolean;
  comments: Comment[];
  draftKey: string | null;
  onLine: (key: string, line: string, lineNo: number) => void;
  onOpen: (line?: number) => void;
  onStage: (patch?: string) => void;
  onUnstage: (patch?: string) => void;
  onRevert: (patch?: string) => void;
  draftEditor: React.ReactNode;
  onRemoveComment: (c: Comment) => void;
}) {
  const [open, setOpen] = useState(file.added + file.removed < 400);
  const lang = languageFor(file.path);
  const canStage = repo && scope === 'unstaged';
  const canUnstage = repo && scope === 'staged';
  return (
    <section className="change-file" data-file={file.path}>
      <header className="change-file-head" onClick={(e) => { if (e.target === e.currentTarget) setOpen(!open); }}>
        <button type="button" className="change-toggle" onClick={() => setOpen(!open)} aria-expanded={open}>
          <IconChevronRight size={14} className={clsx('work-chevron', open && 'is-open')} aria-hidden />
          <span className="mono change-path">{file.path}</span>
        </button>
        {file.isNew ? <span className="new-badge">New</span> : file.isDeleted ? <span className="del-badge">Deleted</span> : (
          <span className="num"><span className="tone-ok">+{file.added}</span> <span className="tone-err">-{file.removed}</span></span>
        )}
        <span className="file-actions">
          {canStage && <button type="button" className="icon-button tiny" aria-label="Stage file" data-tip="Stage file" data-tip-side="down" onClick={() => onStage()}><IconPlus size={14} /></button>}
          {canUnstage && <button type="button" className="icon-button tiny" aria-label="Unstage file" data-tip="Unstage file" data-tip-side="down" onClick={() => onUnstage()}><IconMinus size={14} /></button>}
          {canStage && <button type="button" className="icon-button tiny" aria-label="Revert file" data-tip="Revert file" data-tip-side="down" onClick={() => onRevert()}><IconArrowBackUp size={14} /></button>}
          <button type="button" className="link-button small" onClick={() => onOpen()} data-tip={`${isMac() ? '⌘' : 'Ctrl+'}click a line to open it there`} data-tip-side="down">View</button>
        </span>
      </header>
      {open && !file.binary && (
        <div className="change-lines">
          {file.hunks.map((hunk, hi) => (
            <HunkView
              onFix={onFix}
              onOpenLine={(n) => onOpen(n)}
              key={hi}
              hunk={hunk}
              hunkIndex={hi}
              lang={lang}
              comments={comments}
              draftKey={draftKey}
              draftEditor={draftEditor}
              onLine={onLine}
              onRemoveComment={onRemoveComment}
              actions={(canStage || canUnstage) && !file.isNew ? (
                <span className="hunk-actions">
                  {canStage && <button type="button" onClick={() => onStage(hunkPatch(file, hunk))}><IconPlus size={12} aria-hidden /> Stage</button>}
                  {canUnstage && <button type="button" onClick={() => onUnstage(hunkPatch(file, hunk))}><IconMinus size={12} aria-hidden /> Unstage</button>}
                  {canStage && <button type="button" onClick={() => onRevert(hunkPatch(file, hunk))}><IconArrowBackUp size={12} aria-hidden /> Revert</button>}
                </span>
              ) : null}
            />
          ))}
        </div>
      )}
      {file.binary && <p className="pane-note">A binary file changed.</p>}
    </section>
  );
}

function HunkView({
  hunk, hunkIndex, lang, comments, draftKey, draftEditor, onLine, onRemoveComment, actions, onOpenLine, onFix,
}: {
  onFix: (c: Comment) => void;
  onOpenLine: (line: number) => void;
  hunk: Hunk;
  hunkIndex: number;
  lang: string | undefined;
  comments: Comment[];
  draftKey: string | null;
  draftEditor: React.ReactNode;
  onLine: (key: string, line: string, lineNo: number) => void;
  onRemoveComment: (c: Comment) => void;
  actions: React.ReactNode;
}) {
  let next = hunk.newStart;
  return (
    <div className="hunk">
      <div className="change-line is-hunk">
        <span className="change-no" />
        <span className="change-sign" />
        <code className="change-code">{hunk.header}</code>
        {actions}
      </div>
      {hunk.lines.map((line, i) => {
        const kind = line.startsWith('+') ? 'add' : line.startsWith('-') ? 'del' : 'ctx';
        const no = kind === 'del' ? 0 : next++;
        const body = line.slice(1);
        const html = highlight(body, lang);
        const key = `${hunkIndex}:${i}`;
        const lineComments = comments.filter((c) => c.lineNo === no && no > 0);
        return (
          <div key={i}>
            <div className={clsx('change-line', `is-${kind}`)} onClick={(e) => { if ((e.ctrlKey || e.metaKey) && (no || hunk.newStart)) onOpenLine(no || hunk.newStart); }}>
              <span className="change-no num">{no || ''}</span>
              <span className="change-sign">{kind === 'add' ? '+' : kind === 'del' ? '-' : ''}</span>
              {html !== null
                ? <code className="change-code hljs" dangerouslySetInnerHTML={{ __html: html || ' ' }} />
                : <code className="change-code">{body || ' '}</code>}
              <button type="button" className="change-comment" aria-label="Comment on this line" onClick={() => onLine(key, body, no || hunk.newStart)}>
                <IconMessagePlus size={13} />
              </button>
            </div>
            {lineComments.map((c, ci) => (c.finding ? (
              <div key={ci} className="line-comment is-finding">
                <IconSparkles size={13} aria-hidden />
                <span>{c.text}</span>
                <button type="button" className="link-button small" onClick={() => onFix(c)}>Fix this</button>
              </div>
            ) : (
              <div key={ci} className="line-comment">
                <span>{c.text}</span>
                <button type="button" className="icon-button" aria-label="Remove comment" onClick={() => onRemoveComment(c)}><IconX size={12} /></button>
              </div>
            )))}
            {draftKey === key && draftEditor}
          </div>
        );
      })}
    </div>
  );
}

export { filePatch };
