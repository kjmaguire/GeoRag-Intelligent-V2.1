import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

/**
 * Merge Tailwind CSS classes without style conflicts.
 * Drop-in replacement for clsx that also resolves Tailwind class precedence.
 */
export function cn(...inputs: ClassValue[]): string {
    return twMerge(clsx(inputs));
}
