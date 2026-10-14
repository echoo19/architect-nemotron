/**
 * Fields (Lectern's field, input, label and control tokens;
 * design language §9 to §11). A label sits above its field: sentence case, three words
 * or fewer, no colon, no hint. At rest a field is rounded-md with a 1px border on the
 * ground; hover steps the border; focus is the accent border with a 3px halo; invalid is
 * the err border and halo. md is 36 tall (forms), sm 32 (toolbars, rows). A field's
 * error is one line under it in small err.
 */
import clsx from 'clsx';
import { forwardRef, type InputHTMLAttributes, type LabelHTMLAttributes, type ReactNode, type TextareaHTMLAttributes } from 'react';

export function Label({ className, ...props }: LabelHTMLAttributes<HTMLLabelElement>) {
  return <label className={clsx('ui-label', className)} {...props} />;
}

export interface FieldProps {
  label: string;
  htmlFor?: string;
  /** One line under the field. */
  error?: string | null;
  children: ReactNode;
  className?: string;
}

export function Field({ label, htmlFor, error, children, className }: FieldProps) {
  return (
    <div className={clsx('ui-field', className)}>
      <Label htmlFor={htmlFor}>{label}</Label>
      {children}
      {error ? <p role="alert" className="ui-field-error">{error}</p> : null}
    </div>
  );
}

export interface InputProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'size'> {
  size?: 'sm' | 'md';
  invalid?: boolean;
}

export const Input = forwardRef<HTMLInputElement, InputProps>(function Input({ size = 'md', invalid, className, ...props }, ref) {
  return (
    <input
      ref={ref}
      data-invalid={invalid || undefined}
      aria-invalid={invalid || undefined}
      className={clsx('ui-input', `ui-input-${size}`, className)}
      {...props}
    />
  );
});

export interface TextAreaProps extends TextareaHTMLAttributes<HTMLTextAreaElement> {
  invalid?: boolean;
}

/** The Input frame on a textarea: at least 76 tall. */
export const TextArea = forwardRef<HTMLTextAreaElement, TextAreaProps>(function TextArea({ invalid, className, ...props }, ref) {
  return (
    <textarea
      ref={ref}
      data-invalid={invalid || undefined}
      aria-invalid={invalid || undefined}
      className={clsx('ui-textarea', className)}
      {...props}
    />
  );
});
