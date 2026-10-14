// @vitest-environment jsdom
/** Model names as the composer shows them. */
import { describe, expect, it } from 'vitest';
import { prettyModel } from '../src/components/chat/Composer';

describe('prettyModel', () => {
  it('names NVIDIA Nemotron models on Nebius Token Factory', () => {
    expect(prettyModel('nvidia/Nemotron-3-Ultra-550b-a55b')).toBe('Nemotron 3 Ultra');
    expect(prettyModel('nvidia/nemotron-3-super-120b-a12b')).toBe('Nemotron 3 Super');
    expect(prettyModel('nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B')).toBe('Nemotron 3 Nano');
    expect(prettyModel('nvidia/Nemotron-3_5-Lightning')).toBe('Nemotron 3.5 Lightning');
  });

  it('keeps the other families and unknown ids as they were', () => {
    expect(prettyModel('claude-sonnet-5')).toBe('Claude Sonnet 5');
    expect(prettyModel('gpt-4o-mini')).toBe('GPT-4o Mini');
    expect(prettyModel('zai-org/GLM-5.3')).toBe('zai-org/GLM-5.3');
    expect(prettyModel('')).toBe('');
  });
});
