/** Model ids as people read them: "nvidia/Nemotron-3_5-Lightning" is "Nemotron 3.5 Lightning". */

const NEMOTRON = /^(?:nvidia\/)?(?:nvidia-)?nemotron-(\d+(?:_\d+)?)-([a-z]+)/i;

/** "claude-haiku-4-5-20251001" reads as "Claude Haiku 4.5". Unknown ids stay as they are. */
export function prettyModel(id: string): string {
  if (!id) return '';
  // NVIDIA Nemotron on Nebius: nvidia/Nemotron-3_5-Lightning reads "Nemotron 3.5 Lightning".
  const nemotron = NEMOTRON.exec(id);
  if (nemotron) {
    const tier = nemotron[2].charAt(0).toUpperCase() + nemotron[2].slice(1).toLowerCase();
    return `Nemotron ${nemotron[1].replace('_', '.')} ${tier}`;
  }
  const known = /^(claude|gpt|gemini|grok|llama|mistral|deepseek|qwen|gemma)[-_]?(.*)$/i.exec(id);
  if (!known) return id;
  const family = known[1].toLowerCase();
  const rest = known[2].replace(/-\d{8}$/, '');
  const words = rest.split(/[-_]/).filter(Boolean);
  const parts: string[] = [];
  for (const word of words) {
    if (/^\d+$/.test(word) && parts.length && /^\d+$/.test(parts[parts.length - 1].replace('.', ''))) parts[parts.length - 1] += `.${word}`;
    else parts.push(/^\d/.test(word) ? word : word.charAt(0).toUpperCase() + word.slice(1));
  }
  const name = family === 'gpt' ? 'GPT' : family.charAt(0).toUpperCase() + family.slice(1);
  return family === 'gpt' ? `${name}-${parts.join(' ')}` : [name, ...parts].join(' ');
}

/** A ladder rung's short name: a Nemotron's tier ("Lightning"), else the readable name. */
export function rungName(id: string): string {
  const nemotron = NEMOTRON.exec(id);
  if (nemotron) return nemotron[2].charAt(0).toUpperCase() + nemotron[2].slice(1).toLowerCase();
  return prettyModel(id);
}

/** Every model id in a line of text, made readable ("tuned preset only over nvidia/..."). */
export function prettyModels(text: string, ids: string[]): string {
  let out = text;
  for (const id of [...ids].sort((a, b) => b.length - a.length)) {
    if (id && out.includes(id)) out = out.split(id).join(prettyModel(id));
  }
  return out;
}
