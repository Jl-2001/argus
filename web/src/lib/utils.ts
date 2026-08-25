import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/** The standard shadcn/ui class-merging helper: clsx for conditional
 * classes, tailwind-merge to resolve conflicting Tailwind utilities
 * (e.g. a caller's `p-2` overriding a component's own `p-4`). */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
