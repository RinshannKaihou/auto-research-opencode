/** A display title is never the research goal: native terminal titles reject controls. */
export function sessionTitle(value: string): string {
  const text = value.replace(/[\u0000-\u001f\u007f-\u009f]/gu, " ").replace(/\s+/gu, " ").trim()
  // Count code points so truncation cannot split a surrogate pair.
  return Array.from(text).slice(0, 80).join("") || "Research"
}
