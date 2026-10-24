/**
 * Materials tab: files grouped under their publication. Delivery is a neutral
 * label; a complete publication is a delivered report, not a verified result.
 */
import type { Context } from "@opencode/plugin/tui/context"
import { TextAttributes, type SyntaxStyle } from "@opentui/core"
import { For, Show } from "solid-js"
import { DELIVERY, DELIVERY_NOTE, publicationCard, type Row as RowData } from "./model"
import { PublicationBody } from "./OverviewView"
import { palette } from "./palette"
import { displayWidth, fit } from "./process"
import { Markdown, Row, Section, clock } from "./ui"

export function MaterialRow(props: {
  context: Context
  rows: RowData[]
  index: number
  selected: boolean
  width: number
}) {
  const colors = palette(props.context)
  const row = () => props.rows[props.index]!
  const publication = () => row().data ?? {}
  const first = () => props.index === 0 || props.rows[props.index - 1]?.data?.publication_id !== publication().publication_id
  const header = () => {
    const meta = `  ${DELIVERY[publication().status] ?? publication().status}${publication().node_id ? ` · ${publication().node_id}` : ""}`
    const title = publication().display?.title ?? ""
    return { title: fit(title, props.width - 1 - displayWidth(publication().publication_id) - 2 - displayWidth(meta)), meta }
  }
  const item = () => publication().item
  return (
    <box flexDirection="column">
      <Show when={first()}>
        <text wrapMode="none" marginTop={props.index === 0 ? 0 : 1}>
          <span style={{ fg: colors.muted }}>{` ${publication().publication_id}  `}</span>
          <span style={{ fg: colors.base, attributes: TextAttributes.BOLD }}>{header().title}</span>
          <span style={{ fg: colors.muted }}>{header().meta}</span>
        </text>
      </Show>
      <Row context={props.context} id={`research-row-${props.index}`} selected={props.selected}>
        <Show
          when={item()}
          fallback={<span style={{ fg: colors.muted }}>{"   （没有文件）"}</span>}
        >
          {(entry) => (
            <>
              <span style={{ fg: row().ref ? colors.base : colors.muted, attributes: props.selected ? TextAttributes.BOLD : TextAttributes.NONE }}>
                {`   ${entry().item_id}`}
              </span>
              <span style={{ fg: colors.muted }}>
                {`  ${fit(entry().source_path ?? entry().kind ?? "", props.width - 4 - displayWidth(entry().item_id) - 2)}`}
              </span>
            </>
          )}
        </Show>
      </Row>
    </box>
  )
}

export function PublicationDetail(props: { context: Context; row: RowData | undefined; markdown: SyntaxStyle; width: number }) {
  const colors = palette(props.context)
  return (
    <Show when={props.row?.data ? publicationCard(props.row.data) : undefined} fallback={<text fg={colors.muted}>（没有条目）</text>}>
      {(card) => {
        const publication = () => props.row!.data
        const shown = () => ({ ...card(), title: undefined })
        return (
          <box flexDirection="column">
            <text wrapMode="none">
              <span style={{ fg: colors.muted }}>{`${card().id}  `}</span>
              <span style={{ fg: colors.base, attributes: TextAttributes.BOLD }}>
                {fit(card().title ?? "", props.width - 1 - displayWidth(card().id) - 2)}
              </span>
            </text>
            <text wrapMode="none">
              <span style={{ fg: colors.base, bg: colors.chip }}>{` ${DELIVERY[card().status] ?? card().status} `}</span>
              <span style={{ fg: colors.link }}>{card().node ? `  ${card().node}` : "  未挂节点"}</span>
              <span style={{ fg: colors.muted }}>{`  ${clock(publication().created_at)}`}</span>
            </text>
            <Show when={card().overview || card().sections.length}>
              <box marginTop={1}>
                <PublicationBody context={props.context} card={shown()} markdown={props.markdown} />
              </box>
            </Show>
            <Section context={props.context} title="说明">
              <Markdown style={props.markdown} content={card().summary} />
            </Section>
            <Show when={card().gaps.length}>
              <Section context={props.context} title="尚未解决的问题" count={card().gaps.length}>
                <For each={card().gaps}>
                  {(gap, index) => (
                    <box flexDirection="row">
                      <text fg={colors.warning} flexShrink={0} wrapMode="none">{`${index() + 1}  `}</text>
                      <text fg={colors.base} flexGrow={1}>
                        {gap}
                      </text>
                    </box>
                  )}
                </For>
              </Section>
            </Show>
            <Show when={publication().item}>
              {(item) => (
                <Section context={props.context} title="选中的文件">
                  <text>
                    <span style={{ fg: colors.base, attributes: TextAttributes.BOLD }}>{item().item_id}</span>
                    <span style={{ fg: colors.muted }}>{`  ${item().source_path ?? item().kind ?? ""}`}</span>
                  </text>
                  <text fg={colors.muted}>{props.row!.ref ? "Enter 打开这个文件" : "这一项不是文件，不能打开"}</text>
                </Section>
              )}
            </Show>
            <text fg={colors.muted} marginTop={1}>
              {DELIVERY_NOTE}
            </text>
          </box>
        )
      }}
    </Show>
  )
}
