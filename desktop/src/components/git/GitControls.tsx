/**
 * Git in the conversation header: the branch (click to copy, as in Claude Code),
 * whether this is a worktree, and a Commit button whose menu holds push, pull request,
 * create branch and hand off (Codex's chat header actions).
 */
import {
  IconArrowDown, IconArrowUp, IconArrowsExchange, IconChevronDown, IconCloudUpload, IconGitBranch, IconGitCommit,
  IconGitFork, IconGitPullRequest,
} from '@tabler/icons-react';
import { useState } from 'react';
import { shortcutLabel } from '../../lib/shortcuts';
import { useApp, type Conv } from '../../store/app';
import { useConfirm } from '../ui/useConfirm';
import { Menu, type MenuItem } from '../ui/Menu';
import { WorkingMark } from '../ui/Working';

export function BranchChip({ conv }: { conv: Conv }) {
  const showToast = useApp((s) => s.showToast);
  const git = conv.git;
  if (!git?.repo) return null;
  const linked = git.worktree?.isLinked;
  const name = git.branch ?? 'detached HEAD';
  return (
    <button
      type="button"
      className="branch-chip no-drag"
      onClick={() => void navigator.clipboard.writeText(git.branch ?? '').then(() => showToast(`Copied ${name}`))}
      data-tip={linked ? `Worktree on ${name}. Click to copy the branch` : 'Click to copy the branch'}
      data-tip-side="down"
    >
      {linked ? <IconGitFork size={14} aria-hidden /> : <IconGitBranch size={14} aria-hidden />}
      <span className="branch-name mono">{name}</span>
      {(git.ahead ?? 0) > 0 && <span className="branch-ab num"><IconArrowUp size={11} aria-hidden />{git.ahead}</span>}
      {(git.behind ?? 0) > 0 && <span className="branch-ab num"><IconArrowDown size={11} aria-hidden />{git.behind}</span>}
    </button>
  );
}

export function CommitButton({ conv }: { conv: Conv }) {
  const [menu, setMenu] = useState(false);
  const [dialog, setDialog] = useState<null | 'commit' | 'branch'>(null);
  const [confirm, confirmDialog] = useConfirm();
  const gitCall = useApp((s) => s.gitCall);
  const showToast = useApp((s) => s.showToast);
  const open = useApp((s) => s.prefs.open.find((o) => o.id === conv.id));
  const moveConversation = useApp((s) => s.moveConversation);
  const [busy, setBusy] = useState(false);
  const git = conv.git;
  if (!git?.repo) return null;
  const changes = git.staged + git.unstaged + git.untracked;
  const canPush = !!git.remote && ((git.ahead ?? 0) > 0 || !git.upstream);

  const items: MenuItem[] = [
    { id: 'commit', label: 'Commit', icon: IconGitCommit, disabled: changes === 0 },
    { id: 'push', label: (git.ahead ?? 0) > 0 ? `Push ${git.ahead} commit${git.ahead === 1 ? '' : 's'}` : 'Push', icon: IconCloudUpload, disabled: !canPush },
    { id: 'pr', label: 'Create pull request', icon: IconGitPullRequest, disabled: !git.remote, detail: git.gh ? undefined : 'Needs the GitHub CLI (gh)' },
    { id: 'branch', label: git.detached ? 'Create branch here' : 'New branch from here', icon: IconGitBranch },
    ...(open?.worktree
      ? [{ id: 'handoff-local', label: 'Hand off to your checkout', icon: IconArrowsExchange, detail: 'Moves the changes and this conversation' }]
      : [{ id: 'handoff-worktree', label: 'Move to a new worktree', icon: IconGitFork, detail: 'Moves the changes and this conversation' }]),
  ];

  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    try {
      await action();
    } catch (err) {
      showToast((err as { detail?: string }).detail || 'That git command failed.', 'error');
    } finally {
      setBusy(false);
    }
  };

  const pick = (id: string) => {
    setMenu(false);
    if (id === 'commit') setDialog('commit');
    else if (id === 'branch') setDialog('branch');
    else if (id === 'push') void run(async () => { await gitCall(conv.id, 'push'); showToast('Pushed'); });
    else if (id === 'pr') setDialog('commit');
    else if (id === 'handoff-local' && open?.worktree) {
      void confirm({ title: 'Hand off to your checkout?', detail: `Uncommitted changes move to ${open.worktree.root}, and this conversation goes with them.`, keep: 'Cancel', action: 'Hand off', tone: 'primary' })
        .then((ok) => { if (ok) void run(async () => {
          const r = await gitCall<{ files: number }>(conv.id, 'handoff', { to: open.worktree!.root });
          moveConversation(conv.id, open.worktree!.root, null);
          showToast(`Handed off ${r.files} file${r.files === 1 ? '' : 's'}`);
        }); });
    } else if (id === 'handoff-worktree') {
      void run(async () => {
        const root = git.root ?? conv.state.cwd ?? '';
        const wt = await gitCall<{ path: string; branch: string }>(conv.id, 'worktree-add', { name: conv.state.title || 'session', prefix: useApp.getState().prefs.branchPrefix });
        void useApp.getState().pruneWorktrees(wt.path);
        await fetch('/api/git/handoff', { method: 'POST', body: JSON.stringify({ cwd: root, to: wt.path }) });
        moveConversation(conv.id, wt.path, { root, branch: wt.branch });
        showToast(`Moved to ${wt.branch}`);
      });
    }
  };

  return (
    <div className="commit-split bar-anchor no-drag">
      <button type="button" className="commit-main" onClick={() => setDialog('commit')} disabled={busy || changes === 0} data-tip={changes ? `${changes} changed file${changes === 1 ? '' : 's'}` : 'Nothing to commit'} data-tip-side="down">
        {busy ? <WorkingMark className="working-xs" /> : <IconGitCommit size={15} aria-hidden />}
        <span>Commit</span>
      </button>
      <button type="button" className="commit-more" aria-label="More git actions" onClick={() => setMenu(!menu)}>
        <IconChevronDown size={13} />
      </button>
      {menu && <Menu side="down" align="end" width={270} items={items} onClose={() => setMenu(false)} onPick={pick} />}
      {dialog === 'commit' && <CommitDialog conv={conv} onClose={() => setDialog(null)} />}
      {dialog === 'branch' && <BranchDialog conv={conv} onClose={() => setDialog(null)} />}
      {confirmDialog}
    </div>
  );
}

function CommitDialog({ conv, onClose }: { conv: Conv; onClose: () => void }) {
  const gitCall = useApp((s) => s.gitCall);
  const showToast = useApp((s) => s.showToast);
  const git = conv.git!;
  const [message, setMessage] = useState('');
  const [all, setAll] = useState(git.staged === 0);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState('');
  const canPr = !!git.remote && !!git.gh;

  const go = async (then: 'none' | 'push' | 'pr') => {
    if (!message.trim()) return;
    setBusy(then);
    setError('');
    try {
      await gitCall(conv.id, 'commit', { message: message.trim(), all });
      if (then !== 'none') await gitCall(conv.id, 'push');
      if (then === 'pr') {
        const [title, ...rest] = message.trim().split('\n');
        const pr = await gitCall<{ url?: string }>(conv.id, 'pr', { title, body: rest.join('\n').trim() });
        showToast(pr.url ? `Pull request opened: ${pr.url}` : 'Pull request opened');
      } else showToast(then === 'push' ? 'Committed and pushed' : 'Committed');
      onClose();
    } catch (err) {
      setError((err as { detail?: string }).detail || 'That did not work.');
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="scrim" onMouseDown={onClose}>
      <div className="dialog commit-dialog" role="dialog" aria-label="Commit" onMouseDown={(e) => e.stopPropagation()}>
        <div className="confirm-head"><span className="dialog-icon"><IconGitCommit size={18} aria-hidden /></span><h2>Commit to {git.branch ?? 'HEAD'}</h2></div>
        <textarea
          className="field commit-message"
          autoFocus
          rows={4}
          placeholder="Describe the change"
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) void go('none');
            if (e.key === 'Escape') onClose();
          }}
        />
        <label className="check-row">
          <input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} />
          <span>Include every change ({git.unstaged + git.untracked} not staged{git.staged ? `, ${git.staged} staged` : ''})</span>
        </label>
        {error && <p className="onboarding-error">{error}</p>}
        <div className="button-row confirm-buttons">
          <button type="button" className="button button-quiet" onClick={onClose}>Cancel</button>
          {git.remote && <button type="button" className="button" disabled={!message.trim() || !!busy} onClick={() => void go('push')}><IconCloudUpload size={15} aria-hidden /> Commit and push</button>}
          {git.remote && (
            <button type="button" className="button" disabled={!message.trim() || !!busy || !canPr} onClick={() => void go('pr')} data-tip={canPr ? undefined : 'Install the GitHub CLI (gh) to open pull requests'}>
              <IconGitPullRequest size={15} aria-hidden /> Commit and open PR
            </button>
          )}
          <button type="button" className="button button-primary" disabled={!message.trim() || !!busy} onClick={() => void go('none')}>
            {busy === 'none' ? <WorkingMark className="working-xs" /> : null} Commit <kbd>{shortcutLabel({ key: '↵', mod: true })}</kbd>
          </button>
        </div>
      </div>
    </div>
  );
}

function BranchDialog({ conv, onClose }: { conv: Conv; onClose: () => void }) {
  const gitCall = useApp((s) => s.gitCall);
  const prefix = useApp((s) => s.prefs.branchPrefix);
  const showToast = useApp((s) => s.showToast);
  const [name, setName] = useState(prefix);
  const [error, setError] = useState('');
  const go = async () => {
    try {
      await gitCall(conv.id, 'branch', { name: name.trim() });
      showToast(`On ${name.trim()}`);
      onClose();
    } catch (err) {
      setError((err as { detail?: string }).detail || 'That branch could not be created.');
    }
  };
  return (
    <div className="scrim" onMouseDown={onClose}>
      <form className="dialog commit-dialog" onMouseDown={(e) => e.stopPropagation()} onSubmit={(e) => { e.preventDefault(); void go(); }}>
        <div className="confirm-head"><span className="dialog-icon"><IconGitBranch size={18} aria-hidden /></span><h2>New branch</h2></div>
        <input className="field mono" autoFocus value={name} onChange={(e) => setName(e.target.value.replace(/\s+/g, '-'))} />
        {error && <p className="onboarding-error">{error}</p>}
        <div className="button-row confirm-buttons">
          <button type="button" className="button button-quiet" onClick={onClose}>Cancel</button>
          <button type="submit" className="button button-primary" disabled={!name.trim() || name.trim() === prefix}>Create and switch</button>
        </div>
      </form>
    </div>
  );
}
