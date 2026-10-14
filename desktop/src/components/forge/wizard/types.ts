/**
 * The props every step screen takes (clarity spec §6, S1): ForgeWorkspace renders the
 * TopBar, then one screen by `wizardStep(...).step`, with these.
 */
import type { ForgeModel, Ladder } from '../../../lib/forge';
import type { SubState } from '../../../lib/wizard';
import type { Conv } from '../../../store/app';
import type { AgentDetail } from '../ForgeWorkspace';

export interface ScreenProps {
  conv: Conv;
  model: ForgeModel;
  /** `ladderView(model, conv.forge)`, computed once by the page. */
  ladder: Ladder | null;
  /** The installed agent (`useAgentDetail`), once a model passed; null while it loads. */
  detail: AgentDetail | null;
  /** The code the agent detail request failed with, if it did. */
  detailError?: string;
  /** What the step shows (`wizardStep(...).sub`). */
  sub: SubState;
}
