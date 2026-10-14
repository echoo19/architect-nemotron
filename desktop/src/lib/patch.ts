/** A unified diff as files and hunks, keeping each file's header so one hunk can be applied alone. */
export interface Hunk { header: string; lines: string[]; oldStart: number; newStart: number }
export interface FilePatch { path: string; header: string; hunks: Hunk[]; added: number; removed: number; isNew: boolean; isDeleted: boolean; binary: boolean }

export function parsePatch(text: string): FilePatch[] {
  const files: FilePatch[] = [];
  let file: FilePatch | null = null;
  let hunk: Hunk | null = null;
  let inHeader = false;
  for (const line of text.split('\n')) {
    const start = /^diff --git a\/(.+?) b\/(.+)$/.exec(line);
    if (start) {
      file = { path: start[2], header: `${line}\n`, hunks: [], added: 0, removed: 0, isNew: false, isDeleted: false, binary: false };
      files.push(file);
      hunk = null;
      inHeader = true;
      continue;
    }
    if (!file) continue;
    const h = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(line);
    if (h) {
      hunk = { header: line, lines: [], oldStart: Number(h[1]), newStart: Number(h[2]) };
      file.hunks.push(hunk);
      inHeader = false;
      continue;
    }
    if (inHeader) {
      if (line.startsWith('new file')) file.isNew = true;
      if (line.startsWith('deleted file')) file.isDeleted = true;
      if (line.startsWith('Binary files')) file.binary = true;
      const plus = /^\+\+\+ (?:b\/)?(.+)$/.exec(line);
      if (plus && plus[1] !== '/dev/null') file.path = plus[1].replace(/\t.*$/, '');
      file.header += `${line}\n`;
      continue;
    }
    // A line that is not part of a hunk (" ", "+", "-", "\") ends it: text after the
    // patch, like the "Untracked files:" list Architect's /diff adds, is not code.
    if (hunk && line !== '' && !/^[ +\-\\]/.test(line)) {
      hunk = null;
      continue;
    }
    if (hunk && line !== '\\ No newline at end of file') {
      if (line.startsWith('+')) file.added += 1;
      else if (line.startsWith('-')) file.removed += 1;
      hunk.lines.push(line);
    } else if (hunk) hunk.lines.push(line);
  }
  // A trailing empty context line comes from the final newline of the text.
  for (const f of files) for (const hk of f.hunks) while (hk.lines.length && hk.lines[hk.lines.length - 1] === '') hk.lines.pop();
  return files;
}

/** The patch text for one hunk of a file (for git apply). */
export function hunkPatch(file: FilePatch, hunk: Hunk): string {
  return `${file.header}${hunk.header}\n${hunk.lines.join('\n')}\n`;
}

export function filePatch(file: FilePatch): string {
  return `${file.header}${file.hunks.map((h) => `${h.header}\n${h.lines.join('\n')}\n`).join('')}`;
}
