import React from "react";
import { Button as AstryxButton, type ButtonVariant as AstryxButtonVariant } from "@astryxdesign/core/Button";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
export type ButtonSize = "sm" | "md" | "lg";

const VARIANT: Record<ButtonVariant, AstryxButtonVariant> = {
  primary: "primary",
  secondary: "secondary",
  ghost: "ghost",
  danger: "destructive",
};

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  icon?: React.ReactNode;
  isIconOnly?: boolean;
  busy?: boolean;
}

function textContent(children: React.ReactNode): string {
  if (children == null || typeof children === "boolean") return "";
  if (typeof children === "string" || typeof children === "number") return String(children);
  if (Array.isArray(children)) return children.map(textContent).join("");
  if (React.isValidElement(children)) {
    const nested = (children.props as { children?: React.ReactNode }).children;
    return textContent(nested);
  }
  return "";
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(function Button(props, ref) {
  const { variant = "secondary", size, icon, isIconOnly, busy = false, children, disabled, type, ...rest } = props;
  const text = busy ? "Working…" : textContent(children).trim();
  const label = text || (typeof rest["aria-label"] === "string" ? rest["aria-label"] : "Action");
  const stringChild = typeof children === "string" || typeof children === "number";
  return (
    <AstryxButton
      ref={ref}
      {...rest}
      type={type ?? "submit"}
      variant={VARIANT[variant]}
      size={size}
      icon={icon}
      isIconOnly={isIconOnly}
      label={label}
      isDisabled={Boolean(disabled) || busy}
      aria-busy={busy || undefined}
    >
      {busy || stringChild ? undefined : children}
    </AstryxButton>
  );
});
