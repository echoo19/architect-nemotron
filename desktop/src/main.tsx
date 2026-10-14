import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import { useApp, wire } from './store/app';

// Development builds expose the store so a browser session can drive and inspect it.
if (import.meta.env.DEV) Object.assign(window, { __architect: useApp, __wire: wire });
import './styles/fonts.css';
import './styles/tokens.css';
import './styles/base.css';
import './styles/motion.css';
import './styles/ui.css';
import './styles/shell.css';
import './styles/sidebar.css';
import './styles/chat.css';
import './styles/composer.css';
import './styles/forge.css';
import './styles/viz.css';
import './styles/agentview.css';
import './styles/panes.css';
import './styles/screens.css';

const root = createRoot(document.getElementById('root')!);
if (import.meta.env.DEV && location.hash === '#/ui') {
  // The primitives on one page, for a visual check (components/ui/__showroom__.tsx).
  void import('./components/ui/__showroom__').then(({ Showroom }) => root.render(<StrictMode><Showroom /></StrictMode>));
} else {
  root.render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}
