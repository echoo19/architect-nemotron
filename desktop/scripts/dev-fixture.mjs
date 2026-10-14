// Development fixture: a $0 offline workspace (fake model) and a small git project with
// uncommitted changes, for `npm run dev:fake` and window captures.
//   node scripts/dev-fixture.mjs [root]   (default $ARCHITECT_FIXTURE_ROOT, else C:/t on
//   Windows and /tmp/adt elsewhere: the same root dev-fake.mjs serves)
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const root = process.argv[2] || process.env.ARCHITECT_FIXTURE_ROOT || (process.platform === 'win32' ? 'C:/t' : '/tmp/adt');
const ws = path.join(root, 'adw');
const project = path.join(root, 'adproj');
const home = path.join(root, 'adh');
fs.mkdirSync(ws, { recursive: true });
fs.mkdirSync(home, { recursive: true });

const reply = (text) => ({ text, tokens_in: 40, tokens_out: 60, cost_usd: 0.0004, model: 'fake' });
const answer = JSON.stringify({
  text: 'Here is what I found.\n\n- The project has **a server and a client**\n- `src/server.ts` starts it\n\n```ts\nexport function start(port = 8080) {\n  return listen(port);\n}\n```\n\nWant me to add tests?',
});
const look = JSON.stringify({ tool_calls: [{ id: 'call-1', name: 'list_dir', arguments: { path: '.' } }] });
const responses = [];
for (let i = 0; i < 200; i += 1) responses.push(reply(look), reply(answer));
fs.writeFileSync(path.join(ws, 'responses.json'), JSON.stringify(responses));
fs.writeFileSync(path.join(ws, 'session-profile.json'), JSON.stringify({
  workspace: '.',
  models: { fake: { provider: 'fake', fixture: 'responses.json' } },
  inference: { provider: 'fake', model: 'fake', fixture: 'responses.json' },
  compile: { max_candidates: 1, enable_rewriting: false },
}, null, 2));

if (!fs.existsSync(path.join(project, '.git'))) {
  fs.mkdirSync(path.join(project, 'src'), { recursive: true });
  fs.writeFileSync(path.join(project, 'README.md'), '# Demo\n\nA tiny service.\n');
  fs.writeFileSync(path.join(project, 'src', 'server.ts'), "import { listen } from './net';\n\nexport function start(port = 3000) {\n  return listen(port);\n}\n");
  fs.writeFileSync(path.join(project, 'src', 'net.ts'), 'export function listen(port: number) {\n  console.log(`listening on ${port}`);\n  return port;\n}\n');
  const git = (...args) => execFileSync('git', args, { cwd: project, stdio: 'ignore' });
  git('init', '-q');
  git('-c', 'user.email=dev@example.com', '-c', 'user.name=dev', 'add', '.');
  git('-c', 'user.email=dev@example.com', '-c', 'user.name=dev', 'commit', '-qm', 'start');
  // Uncommitted work for the Changes pane.
  fs.writeFileSync(path.join(project, 'src', 'server.ts'), "import { listen } from './net';\n\n/** Start the service. */\nexport function start(port = 8080) {\n  const bound = listen(port);\n  return bound;\n}\n");
  fs.writeFileSync(path.join(project, 'src', 'health.ts'), "export function health() {\n  return { ok: true };\n}\n");
}

fs.writeFileSync(path.join(home, 'prefs.json'), JSON.stringify({ folders: [project.replace(/\\/g, '/')], open: [], probe: false }));
console.log(JSON.stringify({ workspace: ws, project, home }));
