/** The attribution chip (pinned seam 4): 'Claude' for the MCP actor, the raw principal otherwise, nothing when unknown. */
export function ActorChip({ actor }: { actor: string | null | undefined }) {
  if (!actor) return null;
  return (
    <span className="inline-flex items-center rounded border border-line bg-surface px-2 py-0.5 text-xs font-medium text-ink-2">
      {actor === 'service:mcp' ? 'Claude' : actor}
    </span>
  );
}
