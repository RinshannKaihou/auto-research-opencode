/** Native capabilities needed by a coordinator to inspect existing evidence. */
export const COORDINATOR_NATIVE_READS = new Set(["read", "glob", "grep"])

/** Unknown native tools are not assumed to be read-only. */
export function coordinatorMayUseNative(name: string): boolean {
  return COORDINATOR_NATIVE_READS.has(name)
}

export function controlInstructions(run: string | undefined, pause: string | null | undefined): string | null {
  const sessionPaused = !!pause && pause !== "wait"
  if (run === "manual") return null
  if (run === "running" && !sessionPaused) return null
  if (!run) return "Research control state is unavailable. You may read existing information, but cannot start research or assume it is running."
  return `Research execution is ${run}${pause ? ` (session reason: ${pause})` : ""}. You may read and synthesize existing evidence. Do not start new research, dispatch nodes, or perform node work yourself. Ordinary messages do not resume research; an explicit /research resume is required.`
}
