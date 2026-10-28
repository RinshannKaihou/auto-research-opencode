/**
 * Read-only text dialog for /research results. The host alert draws its message
 * as one unbounded text, so a long status ran off the screen; this dialog keeps
 * the text in a scrollbox that fits the room the host leaves for a dialog.
 */
import type { Context } from "@opencode/plugin/tui/context"
import { TextAttributes, type ScrollBoxRenderable } from "@opentui/core"
import { useTerminalDimensions } from "@opentui/solid"
import { createSignal, onMount } from "solid-js"

/** Resolves when the dialog closes, like the host alert. */
export function showText(context: Context, title: string, message: string): Promise<void> {
  return new Promise((resolve) => {
    context.ui.dialog.show(() => <TextDialog context={context} title={title} message={message} />, () => resolve())
  })
}

export function TextDialog(props: { context: Context; title: string; message: string }) {
  const theme = props.context.theme
  const dialog = props.context.ui.dialog
  const dimensions = useTerminalDimensions()
  let body: ScrollBoxRenderable | undefined
  // Starts from the line count, which only wrapping changes once the text is laid out.
  const [textHeight, setTextHeight] = createSignal(props.message.split("\n").length)
  // The host draws a dialog a quarter of the way down the screen. Below that, 7
  // rows are not text: the host's top padding, the title, the gap under it, the
  // gap above the hint, the hint, the bottom padding, and one spare row.
  const room = () => Math.max(3, dimensions().height - Math.floor(dimensions().height / 4) - 7)

  // show() resets the size to medium; a wider dialog wraps fewer lines.
  onMount(() => dialog.set({ size: "large" }))
  // The layer lives exactly as long as the dialog, so it needs no input mode of its own.
  // The session's page keys are global as well and close any open dialog; the
  // priority keeps them from winning while this one is up. Escape stays the
  // host's: it closes any dialog, after clearing a text selection.
  props.context.keymap.layer(() => ({
    mode: "global",
    priority: 1,
    commands: [
      { bind: "return", run: () => dialog.clear() },
      { bind: "up", run: () => body?.scrollBy(-1) },
      { bind: "k", run: () => body?.scrollBy(-1) },
      { bind: "down", run: () => body?.scrollBy(1) },
      { bind: "j", run: () => body?.scrollBy(1) },
      { bind: "pageup", run: () => body?.scrollBy(-0.8, "viewport") },
      { bind: "pagedown", run: () => body?.scrollBy(0.8, "viewport") },
      { bind: "home", run: () => body?.scrollTo(0) },
      { bind: "end", run: () => body?.scrollTo(body.scrollHeight) },
    ],
  }))

  return (
    <box flexDirection="column" paddingX={3} paddingBottom={1} gap={1}>
      <box flexDirection="row" justifyContent="space-between">
        <text fg={theme.text.base} attributes={TextAttributes.BOLD}>
          {props.title}
        </text>
        <text fg={theme.text.muted} onMouseUp={() => dialog.clear()}>
          esc
        </text>
      </box>
      {/* A scrollbox fills whatever height it is given, so give it no more than the text needs. */}
      <scrollbox
        ref={(box: ScrollBoxRenderable) => {
          body = box
          // A text reports no size changes of its own; the box around it does.
          box.content.on("resize", () => setTextHeight(box.content.height))
        }}
        height={Math.min(textHeight(), room())}
        scrollX={false}
        contentOptions={{ minHeight: 0 }}
        // Newer OpenTUI draws the bar over the text's last column, which erases a wide character on each line.
        scrollbarOptions={{ visible: false }}
      >
        <text fg={theme.text.muted}>{props.message}</text>
      </scrollbox>
      <text fg={theme.text.muted}>↑↓ 滚动 · enter/esc 关闭</text>
    </box>
  )
}
