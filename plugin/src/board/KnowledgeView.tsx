/**
 * Knowledge tab. The kind filter doubles as the legend: each kind keeps one
 * hue in the filter, the rows and the detail. Status is written out, never
 * colored as success, because it does not mean scientific verification.
 */
import type { Context } from "@opencode/plugin/tui/context"
import { TextAttributes } from "@opentui/core"
import { For, Show } from "solid-js"
import { KNOWLEDGE_KINDS, KNOWLEDGE_STATUSES, STATUS_NOTE, knowledgeCode, type Row as RowData } from "./model"
import { palette } from "./palette"
import { displayWidth, fit } from "./process"
import { Row, Section } from "./ui"

const QUIET_STATUSES = new Set(["proposed", "working"])

function kindTag(kind: string): string {
  return `[${KNOWLEDGE_KINDS[kind] ?? kind}]`
}

export function KnowledgeFilter(props: {
  context: Context
  counts: { kind: string; count: number }[]
  total: number
  kind: string
  status: string
}) {
  const colors = palette(props.context)
  const chip = (active: boolean) => ({ bg: active ? colors.band : colors.none, attributes: active ? TextAttributes.BOLD : TextAttributes.NONE })
  return (
    <text wrapMode="none" flexShrink={0}>
      <span style={{ fg: colors.muted }}>类型 </span>
      <span style={{ fg: colors.base, ...chip(!props.kind) }}>{` 全部 ${props.total} `}</span>
      <For each={props.counts}>
        {(entry) => (
          <>
            <span style={{ fg: colors.kind(entry.kind), ...chip(props.kind === entry.kind) }}>
              {` ${KNOWLEDGE_KINDS[entry.kind] ?? entry.kind}`}
            </span>
            <span style={{ fg: colors.muted, ...chip(props.kind === entry.kind) }}>{` ${entry.count} `}</span>
          </>
        )}
      </For>
      <span style={{ fg: colors.muted }}>{"   状态 "}</span>
      <span style={{ fg: colors.base }}>{props.status ? KNOWLEDGE_STATUSES[props.status] ?? props.status : "全部"}</span>
      <span style={{ fg: colors.muted }}>{"   f 切换类型 · s 切换状态"}</span>
    </text>
  )
}

export function KnowledgeRow(props: { context: Context; row: RowData; index: number; selected: boolean; width: number }) {
  const colors = palette(props.context)
  const item = () => props.row.data ?? {}
  const status = () => (QUIET_STATUSES.has(item().status) ? "" : `  ${KNOWLEDGE_STATUSES[item().status] ?? item().status}`)
  const lead = () => 1 + displayWidth(kindTag(item().kind)) + 1 + displayWidth(props.row.code) + 2
  return (
    <Row context={props.context} id={`research-row-${props.index}`} selected={props.selected}>
      <span style={{ fg: colors.kind(item().kind) }}>{kindTag(item().kind)}</span>
      <span style={{ fg: colors.muted }}>{` ${props.row.code}  `}</span>
      <span style={{ fg: colors.base, attributes: props.selected ? TextAttributes.BOLD : TextAttributes.NONE }}>
        {fit(String(item().statement ?? "").replace(/\s+/g, " "), props.width - lead() - displayWidth(status()))}
      </span>
      <span style={{ fg: item().status === "disputed" ? colors.warning : colors.muted }}>{status()}</span>
    </Row>
  )
}

function entries(value: unknown): [string, string][] {
  if (!value || typeof value !== "object") return []
  return Object.entries(value as Record<string, unknown>).map(([key, entry]) => [
    key,
    typeof entry === "string" ? entry : JSON.stringify(entry),
  ])
}

export function KnowledgeDetail(props: {
  context: Context
  item: any
  owners: (item: any) => { node: string; via?: string }[]
  titles: Map<string, string>
}) {
  const colors = palette(props.context)
  return (
    <Show when={props.item} fallback={<text fg={colors.muted}>（没有条目）</text>}>
      {(item) => (
        <box flexDirection="column">
          <text wrapMode="none">
            <span style={{ fg: colors.kind(item().kind), attributes: TextAttributes.BOLD }}>{kindTag(item().kind)}</span>
            <span style={{ fg: colors.muted }}>{` ${knowledgeCode(item().ref)}    `}</span>
            <span style={{ fg: item().status === "disputed" ? colors.warning : colors.base }}>
              {KNOWLEDGE_STATUSES[item().status] ?? item().status}
            </span>
            <span style={{ fg: colors.muted }}>{`（${STATUS_NOTE}）`}</span>
          </text>
          <text fg={colors.base} marginTop={1}>
            {item().statement ?? ""}
          </text>
          <Section context={props.context} title="节点">
            <For each={props.owners(item())} fallback={<text fg={colors.muted}>项目级，证据没有指向节点</text>}>
              {(owner) => (
                <text>
                  <span style={{ fg: colors.link, attributes: TextAttributes.BOLD }}>{owner.node}</span>
                  <span style={{ fg: colors.base }}>{`  ${props.titles.get(owner.node) ?? ""}`}</span>
                  <span style={{ fg: colors.muted }}>{owner.via ? `  经证据 ${owner.via}` : ""}</span>
                </text>
              )}
            </For>
          </Section>
          <Section context={props.context} title="证据" count={(item().evidence_refs ?? []).length}>
            <For each={item().evidence_refs ?? []} fallback={<text fg={colors.muted}>无</text>}>
              {(ref: string) => <text fg={ref.startsWith("pub/") ? colors.link : colors.base}>{ref}</text>}
            </For>
          </Section>
          <Show when={entries(item().conditions).length}>
            <Section context={props.context} title="条件">
              <For each={entries(item().conditions)}>
                {([key, value]) => (
                  <text>
                    <span style={{ fg: colors.muted }}>{`${key}  `}</span>
                    <span style={{ fg: colors.base }}>{value}</span>
                  </text>
                )}
              </For>
            </Section>
          </Show>
          <Show when={entries(item().scope).length}>
            <Section context={props.context} title="范围">
              <For each={entries(item().scope)}>
                {([key, value]) => (
                  <text>
                    <span style={{ fg: colors.muted }}>{`${key}  `}</span>
                    <span style={{ fg: colors.base }}>{value}</span>
                  </text>
                )}
              </For>
            </Section>
          </Show>
          <Show when={(item().supersedes ?? []).length}>
            <Section context={props.context} title="取代">
              <For each={item().supersedes}>{(ref: string) => <text fg={colors.link}>{ref}</text>}</For>
            </Section>
          </Show>
        </box>
      )}
    </Show>
  )
}
