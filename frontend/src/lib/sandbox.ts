/**
 * The visitor's demo sandbox id: random, made in the browser, kept in
 * localStorage so a reload keeps your runs. It identifies nothing about you.
 */
const KEY = "conductor-sandbox";
let memory: string | null = null;

function fresh(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

export function sandboxId(): string {
  if (typeof window === "undefined") return "";
  try {
    const existing = localStorage.getItem(KEY);
    if (existing && /^[A-Za-z0-9_-]{16,64}$/.test(existing)) return existing;
    const id = fresh();
    localStorage.setItem(KEY, id);
    return id;
  } catch {
    // Storage blocked (private mode): a per-tab id still works.
    memory ??= fresh();
    return memory;
  }
}
