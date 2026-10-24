/**
 * Research-process tab: a rail of nodes on the left, the selected node on the
 * right. Each state channel has its own visual form so they are never read as
 * one another: execution is a colored circle, delivery is a neutral label, and
 * knowledge is a square or tag in a per-kind hue.
 */
import type { Context } from "@opencode/plugin/tui/context"
import { TextAttributes, type RGBA, type SyntaxStyle } from "@opentui/core"
import { For, Show, createMemo } from "solid-js"
import { palette } from "./palette"
import {
  EXECUTION,
  displayWidth,
  fit,
  formatDuration,
  kindName,
  relationName,
  type Edge,
  type NodeView,
  type ProcessView,
} from "./process"
import { Markdown, Section, clock } from "./ui"

const KNOWLEDGE_SHOWN = 6

const DELIVERY: Record<string, string> = { complete: "交付完成", partial: "阶段材料" }

function deliveryText(node: NodeView): string {
  return node.delivery ? `${DELIVERY[node.delivery.status] ?? node.delivery.status} ${node.delivery.id}` : "未发布"
}

function bold(on: boolean) {
  return on ? TextAttributes.BOLD : TextAttributes.NONE
}

export function ProcessList(props: { context: Context; view: ProcessView; selected: number; width: number }) {
  const colors = palette(props.context)
  const current = () => props.view.nodes[props.selected]
  const related = createMemo(() => {
    const node = current()
    return {
      up: new Set(node?.upstream.map((edge) => edge.from)),
      down: new Set(node?.downstream.map((edge) => edge.to)),
    }
  })
  // Item blocks pair a node line with its meta line, so the selection band covers both.
  const blocks = createMemo(() => {
    const result: ({ kind: "link"; rail: string } | { kind: "item"; index: number; rail: string; meta: string; column: number })[] = []
    for (const line of props.view.rail) {
      if (line.kind === "link") result.push({ kind: "link", rail: line.rail })
      else if (line.kind === "node") result.push({ kind: "item", index: line.node!, rail: line.rail, meta: "", column: line.column! })
      else {
        const item = result[result.length - 1]
        if (item?.kind === "item") item.meta = line.rail
      }
    }
    return result
  })

  return (
    <For each={blocks()}>
      {(block) => (
        <Show
          when={block.kind === "item" ? block : null}
          fallback={
            <text wrapMode="none">
              <span style={{ fg: colors.rail }}>{` ${block.rail}`}</span>
            </text>
          }
        >
          {(item) => {
            const node = () => props.view.nodes[item().index]!
            const selected = () => item().index === props.selected
            const mark = () => (related().up.has(node().id) ? " ↑" : related().down.has(node().id) ? " ↓" : "")
            const lead = () => 1 + props.view.width + 1
            const title = () =>
              fit(node().title, props.width - lead() - displayWidth(node().id) - 2 - displayWidth(mark()))
            const meta = () => {
              const parts = [
                EXECUTION[node().execution].label,
                node().duration !== undefined ? formatDuration(node().duration!) : "",
                deliveryText(node()),
              ].filter(Boolean)
              return fit(parts.join(" · "), props.width - lead() - (node().knowledge.length ? 10 : 0))
            }
            return (
              <box id={`research-row-${item().index}`} flexDirection="column" backgroundColor={selected() ? colors.band : undefined}>
                <text wrapMode="none">
                  <span style={{ fg: colors.marker }}>{selected() ? "▌" : " "}</span>
                  <span style={{ fg: colors.rail }}>{item().rail.slice(0, item().column)}</span>
                  <span style={{ fg: colors.execution[node().execution] }}>{EXECUTION[node().execution].glyph}</span>
                  <span style={{ fg: colors.rail }}>{item().rail.slice(item().column + 1)}</span>
                  <span style={{ fg: colors.muted }}>{` ${node().id}  `}</span>
                  <span style={{ fg: colors.base, attributes: bold(selected()) }}>{title()}</span>
                  <span style={{ fg: colors.link }}>{mark()}</span>
                </text>
                <text wrapMode="none">
                  <span style={{ fg: colors.marker }}>{selected() ? "▌" : " "}</span>
                  <span style={{ fg: colors.rail }}>{item().meta}</span>
                  <span style={{ fg: colors.muted }}>{` ${meta()}`}</span>
                  <Show when={node().knowledge.length}>
                    <span style={{ fg: colors.muted }}>{"  "}</span>
                    <For each={node().knowledge.slice(0, 5)}>
                      {(knowledge) => <span style={{ fg: colors.kind(knowledge.kind) }}>▪</span>}
                    </For>
                    <span style={{ fg: colors.muted }}>
                      {node().knowledge.length > 5 ? `+${node().knowledge.length - 5}` : ""}
                    </span>
                  </Show>
                </text>
              </box>
            )
          }}
        </Show>
      )}
    </For>
  )
}

export function ProcessLegend(props: { context: Context }) {
  const colors = palette(props.context)
  const entries: [string, RGBA, string][] = [
    [EXECUTION.finished.glyph, colors.execution.finished, "已结束"],
    [EXECUTION.running.glyph, colors.execution.running, "运行中"],
    [EXECUTION.pending.glyph, colors.execution.pending, "未开始"],
    [EXECUTION.paused.glyph, colors.execution.paused, "暂停"],
    [EXECUTION.failed.glyph, colors.execution.failed, "失败"],
    ["▪", colors.kind("claim"), "知识"],
  ]
  return (
    <text wrapMode="none" flexShrink={0}>
      <For each={entries}>
        {([glyph, color, label]) => (
          <>
            <span style={{ fg: color }}>{` ${glyph}`}</span>
            <span style={{ fg: colors.muted }}>{` ${label} `}</span>
          </>
        )}
      </For>
    </text>
  )
}

function Relation(props: { context: Context; view: ProcessView; edge: Edge; direction: "up" | "down"; width: number }) {
  const colors = palette(props.context)
  const other = () => (props.direction === "up" ? props.edge.from : props.edge.to)
  const title = () => props.view.nodes.find((node) => node.id === other())?.title ?? ""
  const why = () =>
    [relationName(props.edge.type), props.edge.inputs ? `输入 ${props.edge.inputs} 项` : "", props.edge.rationale]
      .filter(Boolean)
      .join(" · ")
  return (
    <box flexDirection="column">
      <text wrapMode="none">
        <span style={{ fg: colors.link }}>{props.direction === "up" ? "← " : "→ "}</span>
        <span style={{ fg: colors.base, attributes: TextAttributes.BOLD }}>{other()}</span>
        <span style={{ fg: colors.base }}>{`  ${fit(title(), props.width - 4 - displayWidth(other()) - 2)}`}</span>
      </text>
      <text wrapMode="none">
        <span style={{ fg: colors.muted }}>{`   ${fit(why(), props.width - 5)}`}</span>
      </text>
    </box>
  )
}

export function ProcessDetail(props: {
  context: Context
  view: ProcessView
  selected: number
  node: any
  width: number
  markdown: SyntaxStyle
}) {
  const colors = palette(props.context)
  const current = () => props.view.nodes[props.selected]
  // The full record arrives after the selection moves; never mix in the previous node's.
  const record = () => ({
    ...(current()?.node ?? {}),
    ...(props.node?.node_id === current()?.id ? props.node : {}),
  })
  // Section bodies are indented by two cells.
  const inner = () => props.width - 2

  return (
    <Show when={current()} fallback={<text fg={colors.muted}>（没有节点）</text>}>
      {(node) => (
        <box flexDirection="column">
          {/* One line: inside a scrollbox a wrapped line that exactly fills the width is measured as two. */}
          <text wrapMode="none">
            <span style={{ fg: colors.muted }}>{`${node().id}  `}</span>
            <span style={{ fg: colors.base, attributes: TextAttributes.BOLD }}>
              {fit(node().title, props.width - 1 - displayWidth(node().id) - 2)}
            </span>
          </text>
          <text wrapMode="none">
            <span style={{ fg: colors.execution[node().execution] }}>
              {`${EXECUTION[node().execution].glyph} ${EXECUTION[node().execution].label}`}
            </span>
            <span style={{ fg: colors.muted }}>
              {node().duration !== undefined ? ` · 执行 ${formatDuration(node().duration!)}` : ""}
            </span>
            <span style={{ fg: colors.muted }}>{"    "}</span>
            <span style={{ fg: colors.base, bg: colors.chip }}>
              {` ${node().delivery ? DELIVERY[node().delivery!.status] ?? node().delivery!.status : "未发布"} `}
            </span>
            <span style={{ fg: colors.muted }}>
              {node().delivery ? ` ${node().delivery!.id} · ${node().delivery!.items} 项` : ""}
            </span>
            <span style={{ fg: colors.muted }}>
              {record().status === "closed" ? "    节点已关闭" : record().status === "proposed" ? "    提议中" : ""}
            </span>
          </text>

          <Section context={props.context} title="上游" count={node().upstream.length}>
            <For each={node().upstream} fallback={<text fg={colors.muted}>独立开展，没有上游节点</text>}>
              {(edge) => <Relation context={props.context} view={props.view} edge={edge} direction="up" width={inner()} />}
            </For>
          </Section>
          <Section context={props.context} title="下游" count={node().downstream.length}>
            <For each={node().downstream} fallback={<text fg={colors.muted}>暂无</text>}>
              {(edge) => <Relation context={props.context} view={props.view} edge={edge} direction="down" width={inner()} />}
            </For>
          </Section>
          <Section context={props.context} title="知识" count={node().knowledge.length}>
            <For each={node().knowledge.slice(0, KNOWLEDGE_SHOWN)} fallback={<text fg={colors.muted}>暂无</text>}>
              {(item) => {
                const tag = `[${kindName(item.kind)}]`
                const code = String(item.ref ?? "").replace(/^knowledge\//, "").replace(/@1$/, "")
                return (
                  <text wrapMode="none">
                    <span style={{ fg: colors.kind(item.kind) }}>{tag}</span>
                    <span style={{ fg: colors.muted }}>{` ${code}  `}</span>
                    <span style={{ fg: colors.base }}>
                      {fit(String(item.statement ?? "").replace(/\s+/g, " "), inner() - displayWidth(tag) - displayWidth(code) - 3)}
                    </span>
                  </text>
                )
              }}
            </For>
            <Show when={node().knowledge.length > KNOWLEDGE_SHOWN}>
              <text fg={colors.muted}>{`还有 ${node().knowledge.length - KNOWLEDGE_SHOWN} 条，见「成果与知识」`}</text>
            </Show>
          </Section>

          <Section context={props.context} title="问题">
            <text fg={colors.base}>{record().question ?? ""}</text>
          </Section>
          <Show when={record().why_now}>
            <Section context={props.context} title="为什么现在做">
              <text fg={colors.base}>{record().why_now}</text>
            </Section>
          </Show>
          <Show when={record().root_reason}>
            <Section context={props.context} title="独立开展的原因">
              <text fg={colors.base}>{record().root_reason}</text>
            </Section>
          </Show>
          <Show when={record().plan}>
            <Section context={props.context} title="计划">
              <Markdown style={props.markdown} content={record().plan} />
            </Section>
          </Show>
          <Section context={props.context} title="执行批次" count={node().attempts.length}>
            <For each={node().attempts} fallback={<text fg={colors.muted}>无</text>}>
              {(attempt) => (
                <text wrapMode="none">
                  <span style={{ fg: colors.muted }}>{`${attempt.attempt_id}  `}</span>
                  <span style={{ fg: colors.base }}>{`${clock(attempt.started_at)} → ${clock(attempt.ended_at)}`}</span>
                  <span style={{ fg: colors.muted }}>{`  ${attempt.state}`}</span>
                </text>
              )}
            </For>
          </Section>
          <Section context={props.context} title="记录">
            <text fg={colors.muted}>{`问题版本：${record().question_ref ?? "未记录"}`}</text>
            <text fg={colors.muted}>
              {`固定输入：${(record().inputs ?? []).length ? record().inputs.join(", ") : "无"}`}
            </text>
          </Section>
        </box>
      )}
    </Show>
  )
}
