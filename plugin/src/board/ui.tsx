/** Small building blocks shared by the board's tabs. */
import type { Context } from "@opencode/plugin/tui/context"
import { TextAttributes, type RGBA, type SyntaxStyle } from "@opentui/core"
import { For, Show, type JSX } from "solid-js"
import { palette } from "./palette"

export function Section(props: { context: Context; title: string; count?: number | string; children: JSX.Element }) {
  const colors = palette(props.context)
  return (
    <box flexDirection="column" marginTop={1}>
      <text wrapMode="none">
        <span style={{ fg: colors.base, attributes: TextAttributes.BOLD }}>{props.title}</span>
        <span style={{ fg: colors.muted }}>{props.count !== undefined ? `  ${props.count}` : ""}</span>
      </text>
      <box flexDirection="column" paddingLeft={2}>
        {props.children}
      </box>
    </box>
  )
}

/** Markdown with syntax markers hidden; `**bold**` shows as bold, not asterisks. */
export function Markdown(props: { style: SyntaxStyle; content: string }) {
  return <markdown content={props.content} syntaxStyle={props.style} conceal />
}

/** A filled label: the one shape for states that must stand out. */
export function Tag(props: { label: string; fg: RGBA; bg: RGBA }) {
  return <span style={{ fg: props.fg, bg: props.bg, attributes: TextAttributes.BOLD }}>{` ${props.label} `}</span>
}

/** One list row: accent gutter and band when selected; `id` lets the list scroll it into view. */
export function Row(props: { context: Context; id: string; selected: boolean; children: JSX.Element }) {
  const colors = palette(props.context)
  return (
    <box id={props.id} backgroundColor={props.selected ? colors.band : undefined}>
      <text wrapMode="none">
        <span style={{ fg: colors.marker }}>{props.selected ? "▌" : " "}</span>
        {props.children}
      </text>
    </box>
  )
}

export function KeyHints(props: { context: Context; hints: [string, string][] }) {
  const colors = palette(props.context)
  return (
    <text wrapMode="none" flexShrink={0}>
      <For each={props.hints}>
        {([key, label], index) => (
          <>
            <Show when={index() > 0}>
              <span style={{ fg: colors.rail }}>{"  "}</span>
            </Show>
            <span style={{ fg: colors.base, attributes: TextAttributes.BOLD }}>{key}</span>
            <span style={{ fg: colors.muted }}>{` ${label}`}</span>
          </>
        )}
      </For>
    </text>
  )
}

export function clock(value: string | null | undefined): string {
  if (!value) return "进行中"
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  const pad = (number: number) => String(number).padStart(2, "0")
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}
