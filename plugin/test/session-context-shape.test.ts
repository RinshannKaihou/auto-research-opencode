import { expect, test } from "bun:test"
import { readFileSync } from "node:fs"
import { join } from "node:path"
import { sessionResult, textPrecedesTool } from "../src/specialists"

const evidence = JSON.parse(readFileSync(
  join(import.meta.dir, "../../docs/evidence/session-context/session-context-tool-turn.json"),
  "utf8",
))
const context = evidence.context as any[]

test("a real OpenCode text-tool-text transcript is not flagged as progress-only", () => {
  expect(context.at(-1)).toMatchObject({ type: "idle", outcome: "succeeded" })
  const steps = context.filter((item) => item?.type === "assistant")
  expect(steps).toHaveLength(2)
  expect(steps[0].content.some((part: any) => part?.type === "tool")).toBe(true)
  const result = sessionResult(context)
  expect(result).toEqual({ outcome: "succeeded", text: "The secret word is PINEAPPLE." })
  expect(textPrecedesTool(context)).toBe(false)
})

test("the same transcript without the final assistant item is flagged", () => {
  const steps = context.filter((item) => item?.type === "assistant")
  const trimmed = context.filter((item) => item !== steps.at(-1))
  expect(trimmed.at(-1)).toMatchObject({ type: "idle", outcome: "succeeded" })
  expect(sessionResult(trimmed)).toEqual({ outcome: "succeeded", text: "I'll now read note.txt." })
  expect(textPrecedesTool(trimmed)).toBe(true)
})
