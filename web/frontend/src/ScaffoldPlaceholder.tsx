// Exists only to prove the build -> Docker/CI -> static-serve -> mount
// pipeline works end-to-end before any real section is ported. The Blocklist
// island replaces this with the first real component; this file and its
// registration in main.tsx are removed once that lands.
export function ScaffoldPlaceholder({ payload }: { payload: Record<string, unknown> }) {
  return (
    <div className="inkdrop-react-scaffold" data-testid="inkdrop-react-scaffold">
      InkDrop React build pipeline is wired up. Rows loaded: {String((payload as { rows?: unknown[] })?.rows?.length ?? 0)}.
    </div>
  );
}
