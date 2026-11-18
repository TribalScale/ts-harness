import type { ButtonHTMLAttributes } from 'react';

type Variant = 'primary' | 'secondary' | 'danger';

const styles: Record<Variant, string> = {
  primary: 'bg-gray-900 text-white hover:bg-gray-700',
  secondary: 'border border-gray-300 bg-white text-gray-900 hover:bg-gray-50',
  danger: 'bg-red-600 text-white hover:bg-red-500',
};

export function Button({
  variant = 'primary',
  className = '',
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  return (
    <button
      type="button"
      className={`rounded-md px-4 py-2 text-sm font-medium focus:outline-2 focus:outline-offset-2 focus:outline-gray-900 disabled:opacity-50 ${styles[variant]} ${className}`}
      {...props}
    />
  );
}
