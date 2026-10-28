import { expect, test } from "bun:test"
import type { Context } from "@opencode/plugin/tui/context"
import { testRender } from "@opentui/solid"
import { TextDialog, showText } from "../src/dialog"

type Command = { bind: string; run: () => void }

/** Just the parts of the host the dialog touches; the dialog stack is one slot. */
function host() {
  const commands: Command[] = []
  const calls: string[] = []
  let onClose: (() => void) | undefined
  const context = {
    theme: { text: { base: "#ffffff", muted: "#888888" } },
    keymap: { layer: (input: () => { commands: Command[] }) => void commands.push(...input().commands) },
    ui: {
      dialog: {
        show: (_render: unknown, close?: () => void) => {
          onClose = close
          calls.push("show")
        },
        set: (options: { size?: string }) => void calls.push(`set:${options.size}`),
        clear: () => {
          calls.push("clear")
          onClose?.()
        },
      },
    },
  } as unknown as Context
  return { context, calls, press: (key: string) => commands.find((command) => command.bind === key)!.run() }
}

/** Places the dialog like the host's container: a quarter of the way down, at most the screen width. */
function render(context: Context, message: string) {
  return testRender(
    () => (
      <box position="absolute" left={0} top={0} width={80} height={24} paddingTop={6} alignItems="center">
        <box paddingTop={1} width={88} maxWidth={78}>
          <TextDialog context={context} title="Research" message={message} />
        </box>
      </box>
    ),
    { width: 80, height: 24 },
  )
}

test("text taller than the screen scrolls inside the dialog instead of running off it", async () => {
  const { context, calls, press } = host()
  const message = Array.from({ length: 40 }, (_, index) => `row ${String(index).padStart(2, "0")}`).join("\n")
  const screen = await render(context, message)
  try {
    await screen.renderOnce()
    let frame = screen.captureCharFrame()
    expect(frame).toContain("row 00")
    expect(frame).not.toContain("row 39")
    // The hint below the text stays on screen.
    expect(frame).toContain("enter")
    expect(calls).toContain("set:large")

    press("end")
    await screen.renderOnce()
    frame = screen.captureCharFrame()
    expect(frame).toContain("row 39")
    expect(frame).not.toContain("row 00")

    press("home")
    await screen.renderOnce()
    expect(screen.captureCharFrame()).toContain("row 00")

    press("return")
    expect(calls.at(-1)).toBe("clear")
  } finally {
    screen.renderer.destroy()
  }
})

test("short text keeps the dialog as small as the text, wrapped lines included", async () => {
  const screen = await render(host().context, "word ".repeat(40).trim())
  try {
    // The first frame lays the text out; the second sizes the box to it.
    await screen.renderOnce()
    await screen.renderOnce()
    const rows = screen.captureCharFrame().split("\n")
    const text = rows.flatMap((row, index) => (row.includes("word") ? [index] : []))
    expect(text.length).toBe(3)
    // One gap row, then the hint: no empty scrollbox below the text.
    expect(rows[text.at(-1)! + 2]).toContain("enter")
  } finally {
    screen.renderer.destroy()
  }
})

test("showText resolves when the dialog closes, like the host alert", async () => {
  const { context, calls } = host()
  let closed = false
  const shown = showText(context, "Research", "short").then(() => (closed = true))
  await Promise.resolve()
  expect(calls).toEqual(["show"])
  expect(closed).toBe(false)
  context.ui.dialog.clear()
  await shown
  expect(closed).toBe(true)
})
