/** Native capabilities needed by a coordinator to inspect existing evidence. */
export const COORDINATOR_NATIVE_READS = new Set(["read", "glob", "grep"])

/** Unknown native tools are not assumed to be read-only. */
export function coordinatorMayUseNative(name: string): boolean {
  return COORDINATOR_NATIVE_READS.has(name)
}

export function controlInstructions(run: string | undefined, pause: string | null | undefined): string | null {
  if (pause === 'finished' || pause === 'segment_complete') return 'This work segment has ended. Read existing evidence only; /research auto does not reopen it. New node work needs an explicitly scheduled work segment.'
  if (run === 'running' && pause === 'awaiting_user') {
    return "Research is waiting for the user's reply to your question. Present your pending question and options concisely in plain text and end this turn without starting new research or calling write tools. When the user replies in this session (or runs /research auto), autonomous research resumes automatically."
  }
  const sessionPaused = !!pause && pause !== "wait"
  if (run === "manual") return null
  if (run === "running" && !sessionPaused) return null
  if (!run) return "Research control state is unavailable. You may read existing information, but cannot start research or assume it is running."
  return `Research execution is ${run}${pause ? ` (session reason: ${pause})` : ""}. You may read and synthesize existing evidence. Do not start new research, dispatch nodes, or perform node work yourself. Ordinary messages do not resume research; an explicit /research auto is required.`
}
