/**
 * A form generated from an agent's input JSON Schema, so trying an agent needs no
 * JSON. Strings are text boxes that grow with what is pasted (a whole receipt) up to
 * GROW_LINES lines and then scroll, numbers number fields, booleans switches, enums
 * menus; anything nested falls back to a JSON box for that one field. Fields are Lectern
 * fields (label small and mute, no hints), and Run sits beside them, outline and small:
 * the card's footer keeps its one action for the next step.
 *
 * A screen can own the form's actions (`actions`, in place of Run, so Try it's Run agent
 * sits under the field) and its values (`values` and `onValues`, so it can load a sample
 * and know when the field is empty). `single` draws a lone string field as Try it's paste
 * box: a mono text area of a fixed height, with a placeholder.
 */
import clsx from 'clsx';
import { useId, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { Button } from '../ui/Button';
import { Field, Input, TextArea } from '../ui/Field';

type Schema = {
  type?: string | string[];
  properties?: Record<string, Schema>;
  required?: string[];
  enum?: unknown[];
  description?: string;
  title?: string;
  items?: Schema;
};

function typeOf(schema: Schema): string {
  const t = Array.isArray(schema.type) ? schema.type.find((x) => x !== 'null') : schema.type;
  if (schema.enum) return 'enum';
  return t ?? 'string';
}

/** Lines a text box grows to before it scrolls. */
export const GROW_LINES = 12;
/** Last words of a field name that say its value is a document, not a word or a number. */
const LONG_WORDS = new Set(['text', 'body', 'content', 'document', 'transcript', 'email', 'message', 'receipt', 'invoice', 'note', 'notes', 'description', 'review', 'comment', 'article', 'letter', 'report']);

/**
 * Rows a string field starts with: a few for a long text (its name ends in such a word, as
 * in invoice_text, or it is the only field, so it is the agent's whole input), else one.
 * Either grows as it fills.
 */
export function startRows(name: string, only: boolean): number {
  const last = humanize(name).toLowerCase().split(' ').at(-1) ?? '';
  return only || LONG_WORDS.has(last) ? 3 : 1;
}

/**
 * The height of a growing text box, in pixels: its content's (`content`, borders
 * included), no less than `minRows` lines and no more than GROW_LINES; past that it
 * scrolls. `line` is the line height and `chrome` the padding and borders.
 */
export function grownHeight(content: number, line: number, chrome: number, minRows: number): { height: number; scrolls: boolean } {
  const most = line * GROW_LINES + chrome;
  const least = line * minRows + chrome;
  return { height: Math.min(most, Math.max(least, content)), scrolls: content > most };
}

const px = (value: string) => Number.parseFloat(value) || 0;

/** A text box that is as tall as what it holds, up to GROW_LINES lines, then scrolls. */
function GrowingText({ id, value, minRows, onChange }: { id?: string; value: string; minRows: number; onChange: (value: string) => void }) {
  const box = useRef<HTMLTextAreaElement>(null);
  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    const style = getComputedStyle(el);
    const line = px(style.lineHeight) || px(style.fontSize) * 1.5 || 19.5;
    const borders = px(style.borderTopWidth) + px(style.borderBottomWidth);
    el.style.height = 'auto';
    const { height, scrolls } = grownHeight(el.scrollHeight + borders, line, px(style.paddingTop) + px(style.paddingBottom) + borders, minRows);
    el.style.height = `${height}px`;
    el.style.overflowY = scrolls ? 'auto' : 'hidden';
  }, [value, minRows]);
  return <TextArea ref={box} id={id} className="is-growing" rows={minRows} value={value} onChange={(e) => onChange(e.target.value)} />;
}

export function humanize(name: string): string {
  const spaced = name.replace(/[_-]+/g, ' ').replace(/([a-z])([A-Z])/g, '$1 $2').trim();
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

/** A field's label: its title, else a short description ("Receipt text"), else its name in words. */
export function fieldLabel(name: string, field: Schema): string {
  if (field.title) return field.title;
  const about = field.description?.trim();
  if (about && about.length <= 32 && !/[.:;]/.test(about)) return about.charAt(0).toUpperCase() + about.slice(1);
  return humanize(name);
}

/**
 * `onlyLabel` names the one field when the form has one (the plan's Takes, "Receipt
 * text"), so the field says what to paste.
 */
export type FormValues = Record<string, string | boolean>;

/** Whether a schema is one string field, the shape Try it draws as its paste box. */
export function singleString(schema: Schema | undefined): string | undefined {
  const fields = Object.entries(schema?.properties ?? {});
  return typeOf(schema ?? {}) === 'object' && fields.length === 1 && typeOf(fields[0][1]) === 'string' ? fields[0][0] : undefined;
}

export function SchemaForm({
  schema, busy, onSubmit, className, onlyLabel, actions, values: held, onValues, single, id: formId,
}: {
  schema: Schema | undefined; busy: boolean; onSubmit: (value: unknown) => void; className?: string; onlyLabel?: string;
  /** In place of Run: the screen's own buttons, inside the form (a submit button submits it). */
  actions?: ReactNode;
  /** The values, when the screen holds them. */
  values?: FormValues;
  onValues?: (values: FormValues) => void;
  /** A lone string field as a fixed mono text area: its rows and placeholder. */
  single?: { rows: number; placeholder?: string };
  id?: string;
}) {
  const uid = useId();
  const fields = useMemo(() => Object.entries(schema?.properties ?? {}), [schema]);
  const objectForm = typeOf(schema ?? {}) === 'object' && fields.length > 0;
  const [own, setOwn] = useState<FormValues>({});
  const values = held ?? own;
  const setValues = (next: (v: FormValues) => FormValues) => {
    const value = next(values);
    if (onValues) onValues(value);
    if (!held) setOwn(value);
  };
  const lone = single && singleString(schema);
  const [raw, setRaw] = useState('');
  const [error, setError] = useState('');

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    setError('');
    if (!objectForm) {
      try {
        onSubmit(raw.trim().startsWith('{') || raw.trim().startsWith('[') ? JSON.parse(raw) : raw);
      } catch {
        setError('That is not valid JSON.');
      }
      return;
    }
    const out: Record<string, unknown> = {};
    for (const [name, field] of fields) {
      const value = values[name];
      const kind = typeOf(field);
      if (value === undefined || value === '') continue;
      if (kind === 'number' || kind === 'integer') out[name] = Number(value);
      else if (kind === 'boolean') out[name] = value === true;
      else if (kind === 'object' || kind === 'array') {
        try {
          out[name] = JSON.parse(String(value));
        } catch {
          setError(`${humanize(name)} needs valid JSON.`);
          return;
        }
      } else out[name] = value;
    }
    const missing = (schema?.required ?? []).filter((r) => out[r] === undefined);
    if (missing.length) {
      setError(`Fill in ${missing.map(humanize).join(', ')}.`);
      return;
    }
    onSubmit(out);
  };

  return (
    <form id={formId} className={clsx('schema-form', lone && 'is-single', className)} onSubmit={submit}>
      <div className="schema-fields">
        {objectForm ? fields.map(([name, field]) => {
          const kind = typeOf(field);
          // Its own title or short description first; else, for a one-field form, the plan's Takes.
          const own = fieldLabel(name, field);
          const label = fields.length === 1 && onlyLabel && own === humanize(name) ? onlyLabel : own;
          const id = `${uid}-${name}`;
          const set = (value: string | boolean) => setValues((v) => ({ ...v, [name]: value }));
          return (
            <Field key={name} label={label} htmlFor={id} className="schema-field">
              {kind === 'boolean' ? (
                <input id={id} type="checkbox" className="schema-check" checked={values[name] === true} onChange={(e) => set(e.target.checked)} />
              ) : kind === 'enum' ? (
                <select id={id} className="ui-input ui-input-md" value={String(values[name] ?? '')} onChange={(e) => set(e.target.value)}>
                  <option value="">Choose</option>
                  {field.enum!.map((option) => <option key={String(option)} value={String(option)}>{String(option)}</option>)}
                </select>
              ) : kind === 'number' || kind === 'integer' ? (
                <Input id={id} type="number" className="num" value={String(values[name] ?? '')} onChange={(e) => set(e.target.value)} />
              ) : kind === 'object' || kind === 'array' ? (
                <TextArea id={id} className="mono" rows={3} placeholder={kind === 'array' ? '[ ]' : '{ }'} value={String(values[name] ?? '')} onChange={(e) => set(e.target.value)} />
              ) : lone === name ? (
                <TextArea id={id} className="mono schema-single" rows={single!.rows} placeholder={single!.placeholder} value={String(values[name] ?? '')} onChange={(e) => set(e.target.value)} />
              ) : (
                <GrowingText id={id} minRows={startRows(name, fields.length === 1)} value={String(values[name] ?? '')} onChange={set} />
              )}
            </Field>
          );
        }) : (
          <Field label="Input" htmlFor={`${uid}-input`} className="schema-field">
            <GrowingText id={`${uid}-input`} minRows={5} value={raw} onChange={setRaw} />
          </Field>
        )}
        {error && <p className="schema-error" role="alert">{error}</p>}
      </div>
      {actions ?? <Button type="submit" variant="outline" size="sm" pending={busy} data-action="run" className="schema-submit">Run</Button>}
    </form>
  );
}
