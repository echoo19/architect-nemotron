/**
 * When a part of the window fails to draw, it says so in place instead of leaving a
 * blank screen: what broke, a way to try again, and the details to copy. Nothing
 * else is lost; conversations keep running in their own processes.
 */
import { IconAlertTriangle, IconCopy, IconRefresh } from '@tabler/icons-react';
import { Component, type ErrorInfo, type ReactNode } from 'react';

interface Props { children: ReactNode; label: string; compact?: boolean; resetKey?: unknown }
interface State { error: Error | null; stack: string }

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, stack: '' };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    this.setState({ stack: `${error.stack ?? error.message}\n${info.componentStack ?? ''}` });
    console.error(`[Architect] ${this.props.label} failed to draw`, error);
  }

  componentDidUpdate(previous: Props): void {
    // Moving to another conversation or pane is a fresh start.
    if (this.state.error && previous.resetKey !== this.props.resetKey) this.setState({ error: null, stack: '' });
  }

  render(): ReactNode {
    const { error, stack } = this.state;
    if (!error) return this.props.children;
    return (
      <div className={this.props.compact ? 'crash crash-compact' : 'crash'} role="alert">
        <div className="failure-icon"><IconAlertTriangle size={22} aria-hidden /></div>
        <h1>{this.props.label} could not be shown</h1>
        <p>Something in the window went wrong. Your conversations are safe and still running.</p>
        <div className="button-row">
          <button type="button" className="button button-primary" onClick={() => this.setState({ error: null, stack: '' })}>
            <IconRefresh size={15} aria-hidden /> Try again
          </button>
          <button type="button" className="button" onClick={() => window.location.reload()}>Reload the window</button>
          <button type="button" className="button button-quiet" onClick={() => void navigator.clipboard?.writeText(stack || String(error))}>
            <IconCopy size={14} aria-hidden /> Copy details
          </button>
        </div>
      </div>
    );
  }
}
