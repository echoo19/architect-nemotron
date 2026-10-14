/** Reading a bridge question: its numbered choices and its words (moved from AskCard). */
import { questionWords } from './vocab';

/**
 * Interview questions often end in "(1/2)" or "[default]"; the card shows those as
 * controls. The question itself reads in the app's words ("Build this agent?").
 */
export function cleanQuestion(text: string): string {
  return questionWords(text).replace(/\s*\([^)]*\/[^)]*\)\s*$/, '').replace(/\s*\[[^\]]*\]\s*$/, '').trim();
}

/**
 * "Where should it go?\n  1. run it here\n  2. into software" style questions: pull
 * the labels for numbered choices out of the text so the buttons can say them.
 */
export function numbered(question: string, choices: string[]): { prompt: string; labels: string[] } {
  const labels: string[] = [];
  const lines = question.split('\n');
  const kept: string[] = [];
  for (const line of lines) {
    const match = /^\s*\[?(\d+)[.)\]]\s+(.*)$/.exec(line);
    if (match && choices.includes(match[1])) labels[choices.indexOf(match[1])] = match[2];
    else if (!(labels.length && /^\s*choose\s*$/i.test(line))) kept.push(line);
  }
  return { prompt: kept.join('\n').trim(), labels };
}
