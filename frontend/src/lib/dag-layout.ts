/**
 * Layered DAG layout: column = longest path from a root, rows ordered to
 * reduce edge crossings (one barycenter pass), then centred vertically.
 * Small and deterministic, which is all a ≤50-node workflow needs.
 */

export interface LayoutInput {
  key: string;
  depends_on: string[];
}

export interface Positioned {
  key: string;
  column: number;
  row: number;
  x: number;
  y: number;
}

export const COLUMN_GAP = 250;
export const ROW_GAP = 104;

export function layoutDag(nodes: LayoutInput[]): Positioned[] {
  const byKey = new Map(nodes.map((n) => [n.key, n]));
  const depth = new Map<string, number>();

  const visit = (key: string, stack: Set<string>): number => {
    const known = depth.get(key);
    if (known !== undefined) return known;
    if (stack.has(key)) throw new Error(`cycle at ${key}`);
    stack.add(key);
    const node = byKey.get(key);
    const deps = (node?.depends_on ?? []).filter((d) => byKey.has(d));
    const d = deps.length ? 1 + Math.max(...deps.map((dep) => visit(dep, stack))) : 0;
    stack.delete(key);
    depth.set(key, d);
    return d;
  };
  nodes.forEach((n) => visit(n.key, new Set()));

  const columns: string[][] = [];
  nodes.forEach((n) => {
    const c = depth.get(n.key)!;
    (columns[c] ??= []).push(n.key);
  });

  // Barycenter ordering: sort each column by the mean row of its parents.
  const rowOf = new Map<string, number>();
  columns.forEach((col, c) => {
    if (c > 0) {
      const score = (key: string) => {
        const parents = byKey.get(key)!.depends_on.filter((d) => rowOf.has(d));
        return parents.length
          ? parents.reduce((sum, p) => sum + rowOf.get(p)!, 0) / parents.length
          : Number.POSITIVE_INFINITY;
      };
      col.sort((a, b) => score(a) - score(b));
    }
    col.forEach((key, r) => rowOf.set(key, r));
  });

  const tallest = Math.max(...columns.map((col) => col.length));
  return columns.flatMap((col, c) =>
    col.map((key, r) => ({
      key,
      column: c,
      row: r,
      x: c * COLUMN_GAP,
      y: (r - (col.length - 1) / 2 + (tallest - 1) / 2) * ROW_GAP,
    })),
  );
}
