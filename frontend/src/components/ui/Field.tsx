import { useId } from "react";
import type { ComponentProps, InputHTMLAttributes, ReactNode } from "react";

import { cn } from "../../lib/cn";
import { controlClasses } from "./styles";

export function Input({ className, ...rest }: ComponentProps<"input">) {
  return <input className={cn(controlClasses, className)} {...rest} />;
}

export function Textarea({ className, ...rest }: ComponentProps<"textarea">) {
  return <textarea className={cn(controlClasses, "min-h-20", className)} {...rest} />;
}

export function Select({ className, ...rest }: ComponentProps<"select">) {
  return <select className={cn(controlClasses, className)} {...rest} />;
}

/** Props a Field hands to its control so label, hint and error are wired up. */
export interface FieldControlProps {
  id: string;
  "aria-describedby"?: string;
  "aria-invalid"?: true;
}

interface FieldProps {
  label: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
  /** Mark visually as required (the control still needs `required`). */
  required?: boolean;
  /** Keep the label for screen readers only. */
  hideLabel?: boolean;
  className?: string;
  children: (control: FieldControlProps) => ReactNode;
}

/**
 * Label + control + hint/error. The render prop receives the id / aria props to spread on the
 * control, so every input gets a real `<label htmlFor>`:
 *
 *   <Field label="Email">{(p) => <Input {...p} type="email" />}</Field>
 */
export function Field({ label, hint, error, required, hideLabel, className, children }: FieldProps) {
  const id = useId();
  const hintId = hint ? `${id}-hint` : undefined;
  const errorId = error ? `${id}-error` : undefined;
  const describedBy = [hintId, errorId].filter(Boolean).join(" ") || undefined;
  return (
    <div className={cn("min-w-0", className)}>
      <label
        htmlFor={id}
        className={cn("mb-1 block text-xs font-medium text-ink-muted", hideLabel && "sr-only")}
      >
        {label}
        {required ? (
          <span aria-hidden className="text-ember-400">
            {" "}
            *
          </span>
        ) : null}
      </label>
      {children({ id, "aria-describedby": describedBy, "aria-invalid": error ? true : undefined })}
      {hint ? (
        <p id={hintId} className="mt-1 text-xs text-ink-subtle">
          {hint}
        </p>
      ) : null}
      {error ? (
        <p id={errorId} className="mt-1 text-xs text-danger">
          {error}
        </p>
      ) : null}
    </div>
  );
}

type TextFieldProps = Omit<InputHTMLAttributes<HTMLInputElement>, "id"> & {
  label: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
  hideLabel?: boolean;
  fieldClassName?: string;
};

/** Shorthand for `<Field>` + `<Input>`. */
export function TextField({
  label,
  hint,
  error,
  hideLabel,
  fieldClassName,
  required,
  ...input
}: TextFieldProps) {
  return (
    <Field
      label={label}
      hint={hint}
      error={error}
      required={required}
      hideLabel={hideLabel}
      className={fieldClassName}
    >
      {(p) => <Input {...p} required={required} {...input} />}
    </Field>
  );
}
