/**
 * Queue selection for the TracePilot composer.
 *
 * The assistant runs one task at a time, but queued messages belong to individual
 * conversations. A plain global FIFO head — what this used to be — meant a backlog in
 * one conversation could delay the message the user was actually looking at, and a
 * message whose conversation had been deleted was dropped without a trace.
 */

export interface QueuedTaskLike {
  id: string;
  conversationId: string;
}

/**
 * Choose the next task to run, or `null` when nothing can run.
 *
 * - Only tasks whose conversation still exists are runnable; orphans are skipped
 *   rather than silently dequeued by the caller.
 * - The active conversation wins ties, so a backlog elsewhere cannot push the visible
 *   thread to the back of the line.
 * - Falls back to the oldest runnable task (`queue` is append-ordered).
 *
 * Pure: the caller owns dequeuing.
 */
export function selectNextQueuedTask<T extends QueuedTaskLike>(
  queue: readonly T[],
  knownConversationIds: ReadonlySet<string>,
  activeConversationId: string | undefined,
): T | null {
  if (!queue.length) return null;
  const runnable = queue.filter((item) => knownConversationIds.has(item.conversationId));
  if (!runnable.length) return null;
  return runnable.find((item) => item.conversationId === activeConversationId) ?? runnable[0];
}
