import { expect, test } from "bun:test"
import { sessionResult } from "../src/specialists"

test("timeout recovery must not return an early progress line as the report", () => {
  const messages = [
    { type: "assistant", content: [{ type: "text", text: "I'll start by exploring the workspace structure and then reading the key files." }] },
    { type: "assistant", content: [{ type: "reasoning", text: "The actual audit findings are here..." }] },
    { type: "idle", outcome: "interrupted" },
  ]
  const result = sessionResult(messages)
  expect(result.text).not.toContain("I'll start")
})
