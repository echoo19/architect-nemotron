/** Syntax highlighting for code blocks, the file viewer and diffs (highlight.js, common languages). */
import hljs from 'highlight.js/lib/common';

const EXT: Record<string, string> = {
  ts: 'typescript', tsx: 'typescript', js: 'javascript', jsx: 'javascript', mjs: 'javascript', cjs: 'javascript',
  py: 'python', rs: 'rust', go: 'go', java: 'java', kt: 'kotlin', rb: 'ruby', php: 'php', cs: 'csharp',
  c: 'c', h: 'c', cpp: 'cpp', hpp: 'cpp', css: 'css', scss: 'scss', html: 'xml', xml: 'xml', svg: 'xml',
  json: 'json', md: 'markdown', yml: 'yaml', yaml: 'yaml', toml: 'ini', ini: 'ini', sh: 'bash', bash: 'bash',
  ps1: 'powershell', sql: 'sql', swift: 'swift', lua: 'lua', r: 'r', dockerfile: 'dockerfile', makefile: 'makefile',
};

export function languageFor(nameOrLang: string): string | undefined {
  const lower = nameOrLang.toLowerCase();
  if (hljs.getLanguage(lower)) return lower;
  const base = lower.split(/[\\/]/).pop() ?? lower;
  if (base === 'dockerfile' || base === 'makefile') return base;
  const ext = base.includes('.') ? base.split('.').pop()! : '';
  const lang = EXT[ext];
  return lang && hljs.getLanguage(lang) ? lang : undefined;
}

const MAX = 200_000;

/** Highlighted HTML for `code`, or null when the language is unknown or the text is huge. */
export function highlight(code: string, lang: string | undefined): string | null {
  if (!lang || code.length > MAX) return null;
  try {
    return hljs.highlight(code, { language: lang, ignoreIllegals: true }).value;
  } catch {
    return null;
  }
}

/** "TypeScript", "Python": the language's own name for the status bar. */
export function languageName(file: string): string {
  const lang = languageFor(file);
  return (lang && hljs.getLanguage(lang)?.name?.split(',')[0]) || 'Plain text';
}
