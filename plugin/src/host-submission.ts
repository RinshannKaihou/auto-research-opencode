/** Only failures before invoking a host API carry this proof. RPC errors do not. */
export async function preflight<T>(operation: 'create' | 'synthetic', work: () => Promise<T>): Promise<T> {
  try { return await work() }
  catch (error) {
    throw Object.assign(error instanceof Error ? error : new Error(String(error)), {
      host_submission: 'not_sent', host_operation: operation,
    })
  }
}

export function wasNotSent(error: unknown, operation: 'create' | 'synthetic'): boolean {
  return (error as any)?.host_submission === 'not_sent' && (error as any)?.host_operation === operation
}
