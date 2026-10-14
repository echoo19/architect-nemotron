/**
 * Side questions (Claude Code's /btw, Codex's side chat): ask about the conversation
 * without adding to it. Answers come from a copy of the conversation with read-only
 * tools, even while a turn runs, and are kept only until the panel is cleared.
 */
import { IconArrowUp, IconPlayerStop, IconTrash, IconX } from '@tabler/icons-react';
import { useEffect, useRef, useState } from 'react';
import { shortcutLabel, SHORTCUTS } from '../../lib/shortcuts';
import { useApp, type Conv } from '../../store/app';
import { IconButton } from '../ui/Button';
import { WorkingMark } from '../ui/Working';
import { Markdown } from './Markdown';

export function SideChat({ conv }: { conv: Conv }) {
  const askSide = useApp((s) => s.askSide);
  const toggleSide = useApp((s) => s.toggleSide);
  const cancelSide = useApp((s) => s.cancelSide);
  const [text, setText] = useState('');
  const box = useRef<HTMLTextAreaElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const items = conv.side?.items ?? [];
  const ready = conv.status === 'ready';

  useEffect(() => {
    box.current?.focus();
  }, []);
  useEffect(() => {
    list.current?.scrollTo({ top: list.current.scrollHeight });
  }, [items]);

  const submit = () => {
    if (!text.trim() || !ready) return;
    void askSide(conv.id, text);
    setText('');
  };
  const close = () => toggleSide(conv.id, false);

  return (
    <aside className="side-chat ai-block" aria-label="Side question" onKeyDown={(e) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        close();
      }
    }}>
      <header className="side-chat-head">
        <span className="side-chat-title" data-tip="Answered from this conversation; nothing is added to it" data-tip-side="down">Side question</span>
        <span className="spacer" />
        {items.length > 0 && (
          <IconButton icon={IconTrash} label="Clear side questions" onClick={() => useApp.setState((st) => ({ convs: { ...st.convs, [conv.id]: { ...st.convs[conv.id]!, side: { open: true, items: [] } } } }))} />
        )}
        <IconButton icon={IconX} label="Close" data-tip={`Close  ${shortcutLabel(SHORTCUTS.sideChat)}`} onClick={close} />
      </header>
      <div ref={list} className="side-chat-list">
        {items.map((item) => (
          <div key={item.id} className="side-item">
            <div className="side-q">{item.question}</div>
            {item.error ? (
              <p className="side-error">{item.error === 'stopped' ? 'Stopped.' : 'That question could not be answered.'}</p>
            ) : item.answer ? (
              <div className="side-a"><Markdown text={item.answer} live={!item.done} /></div>
            ) : (
              <div className="side-working">
                <WorkingMark className="working-xs" />
                <span>{item.steps.at(-1) ?? 'Thinking'}</span>
                <IconButton icon={IconPlayerStop} label="Stop" className="side-stop" onClick={() => cancelSide(conv.id, item.id)} />
              </div>
            )}
          </div>
        ))}
      </div>
      <form className="side-chat-compose" onSubmit={(e) => { e.preventDefault(); submit(); }}>
        <textarea
          ref={box}
          rows={1}
          value={text}
          placeholder="Ask about this conversation, even while it works"
          disabled={!ready}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              submit();
            }
          }}
        />
        <button type="submit" className="send-button" aria-label="Ask" disabled={!text.trim() || !ready}><IconArrowUp size={16} stroke={1.5} aria-hidden /></button>
      </form>
    </aside>
  );
}
