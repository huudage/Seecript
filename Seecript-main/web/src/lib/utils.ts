import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

/** shadcn 风格的 className 合并器：clsx 拼条件，twMerge 去 Tailwind 重复。 */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}
