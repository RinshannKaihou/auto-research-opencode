/**
 * Board colors, all taken from the host theme. Each color or glyph means one
 * thing on every tab: feedback colors show execution state, the categorical
 * palette tells knowledge kinds apart, and only the conclusion outcome uses
 * feedback colors for a scientific result.
 *
 * This file is .tsx on purpose: OpenCode points value imports of
 * `@opentui/core` at its own OpenTUI only in files it compiles as JSX. From a
 * .ts file the import loads the plugin's local copy, which clashes with the
 * host's and stops the TUI half of the plugin from loading.
 */
import type { Context } from "@opencode/plugin/tui/context"
import { RGBA, SyntaxStyle, type StyleDefinitionInput } from "@opentui/core"
import type { Execution } from "./process"

const KIND_HUE: Record<string, number> = { observation: 0, claim: 1, lesson: 2, hypothesis: 3, decision: 4, open_question: 5 }

export type Palette = ReturnType<typeof palette>

export function palette(context: Context) {
  const theme = context.theme
  const dark = context.themeMode !== "light"
  const tone = (scale: unknown, step: number) => (scale as Record<number, RGBA>)[dark ? step : 1000 - step]!
  const feedback = theme.text.feedback
  return {
    base: theme.text.base,
    muted: theme.text.muted,
    background: theme.background.base,
    /** "No background" for a span: `undefined` would leave a previously set color in place. */
    none: RGBA.fromValues(0, 0, 0, 0),
    rail: theme.border.base,
    band: tone(theme.hue.accent, 800),
    marker: tone(theme.hue.accent, 300),
    brand: tone(theme.hue.accent, 300),
    tab: tone(theme.hue.accent, 600),
    link: tone(theme.hue.interactive, 200),
    chip: theme.background.raised.high,
    warning: feedback.warning.base,
    kind: (kind: string) =>
      tone(theme.categorical[(KIND_HUE[kind] ?? 0) % Math.max(1, theme.categorical.length)], 300) ?? theme.text.muted,
    execution: {
      pending: theme.text.muted,
      queued: feedback.info.base,
      running: feedback.info.base,
      paused: feedback.warning.base,
      finished: feedback.success.base,
      failed: feedback.error.base,
      stopped: feedback.warning.base,
      unknown: feedback.warning.base,
    } as Record<Execution, RGBA>,
    outcome: {
      answered: feedback.success.base,
      partial: feedback.warning.base,
      unresolved: feedback.error.base,
    } as Record<string, RGBA>,
  }
}

/**
 * OpenTUI 0.8, which the host substitutes when it compiles this JSX file, ties a
 * style's native resources to an owner and throws without renderer.nativeScene.
 * Typecheck still sees the 0.5.12 devDependency: its fromStyles type has no owner
 * parameter and that runtime ignores the extra argument, hence the cast.
 */
type StyleFactory = { fromStyles(styles: Record<string, StyleDefinitionInput>, owner?: unknown): SyntaxStyle }

/** Markdown styled with the theme's own markdown and syntax tokens, as in the chat. */
export function markdownStyle(context: Context): SyntaxStyle {
  const theme = context.theme
  const owner = (context.renderer as unknown as { nativeScene?: unknown }).nativeScene
  // The highlighter tags headings by level; style names do not fall back to a parent.
  const heading = { fg: theme.markdown.heading, bold: true }
  const headings = Object.fromEntries([1, 2, 3, 4, 5, 6].map((level) => [`markup.heading.${level}`, heading]))
  return (SyntaxStyle as unknown as StyleFactory).fromStyles({
    default: { fg: theme.markdown.text },
    conceal: { fg: theme.text.muted },
    "markup.heading": heading,
    ...headings,
    "markup.raw.block": { fg: theme.markdown.codeBlock },
    "markup.list.checked": { fg: theme.markdown.listItem },
    "markup.list.unchecked": { fg: theme.markdown.listItem },
    "markup.strong": { fg: theme.markdown.strong, bold: true },
    "markup.italic": { fg: theme.markdown.emphasis, italic: true },
    "markup.strikethrough": { fg: theme.text.muted },
    "markup.raw": { fg: theme.markdown.code },
    "markup.quote": { fg: theme.markdown.blockQuote, italic: true },
    "markup.list": { fg: theme.markdown.listItem },
    "markup.link": { fg: theme.markdown.link, underline: true },
    "markup.link.label": { fg: theme.markdown.linkText },
    "markup.link.url": { fg: theme.markdown.link, underline: true },
    comment: { fg: theme.syntax.comment, italic: true },
    keyword: { fg: theme.syntax.keyword },
    function: { fg: theme.syntax.function },
    variable: { fg: theme.syntax.variable },
    string: { fg: theme.syntax.string },
    number: { fg: theme.syntax.number },
    type: { fg: theme.syntax.type },
    operator: { fg: theme.syntax.operator },
    punctuation: { fg: theme.syntax.punctuation },
  }, owner)
}
