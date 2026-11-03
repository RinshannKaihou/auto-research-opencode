import { expect, test } from "bun:test"
import { sessionResult } from "../src/specialists"

test("a succeeded turn keeps its report when a later assistant message has no text", () => {
  const result = sessionResult([
    { type: "assistant", content: [{ type: "text", text: "FINAL REPORT" }] },
    { type: "assistant", content: [{ type: "reasoning", text: "still thinking" }] },
    { type: "idle", outcome: "succeeded" },
  ])
  expect(result.outcome).toBe("succeeded")
  expect(result.text).toBe("FINAL REPORT")
})
