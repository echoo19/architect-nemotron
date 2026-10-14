/**
 * A subagent's mark (Codex's identicons): a 5x5 symmetric pattern and a flat colour,
 * both from the agent's name, so the same subagent looks the same everywhere.
 */
const COLORS = ['170 55% 32%', '169 44% 52%', '212 50% 52%', '262 38% 58%', '30 62% 48%', '5 55% 55%', '186 50% 40%'];

function hash(text: string): number {
  let h = 2166136261;
  for (let i = 0; i < text.length; i += 1) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

export function agentCells(name: string): boolean[] {
  const h = hash(name.toLowerCase());
  const cells: boolean[] = [];
  for (let row = 0; row < 5; row += 1) {
    for (let col = 0; col < 5; col += 1) {
      const mirror = col < 3 ? col : 4 - col; // left-right symmetric
      cells.push(((h >> (row * 3 + mirror)) & 1) === 1);
    }
  }
  // Never blank: the middle column always has something.
  if (!cells.some(Boolean)) cells[12] = true;
  return cells;
}

export function AgentMark({ name, size = 14 }: { name: string; size?: number }) {
  const color = COLORS[hash(name) % COLORS.length];
  const cells = agentCells(name);
  return (
    <svg className="agent-mark" width={size} height={size} viewBox="0 0 7 7" aria-hidden>
      <rect width="7" height="7" rx="1.6" fill={`hsl(${color} / 0.16)`} />
      {cells.map((on, i) => (on ? <rect key={i} x={1 + (i % 5)} y={1 + Math.floor(i / 5)} width="1" height="1" fill={`hsl(${color})`} /> : null))}
    </svg>
  );
}
