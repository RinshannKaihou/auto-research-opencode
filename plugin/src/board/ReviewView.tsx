import type { Context } from "@opencode/plugin/tui/context"
import { For, Show } from "solid-js"
import type { Row as RowData } from "./model"
import { palette } from "./palette"
import { fit } from "./process"
import { reviewCard, type ReviewLink } from "./review"
import { Row, Section } from "./ui"

export function ReviewRow(props: { context: Context; row: RowData; index: number; selected: boolean; width: number }) {
  const colors = palette(props.context)
  return <box flexDirection="column" marginBottom={1}>
    <Row context={props.context} id={`research-row-${props.index}`} selected={props.selected}>
      <span style={{ fg: colors.base }}>{fit(props.row.title, props.width - 2)}</span>
    </Row>
    <text fg={colors.muted} wrapMode="none">{`  ${fit(props.row.code, props.width - 2)}`}</text>
  </box>
}

export function ReviewDetail(props: { context: Context; item: any; source: any; selectedLink: number; raw: boolean }) {
  const colors = palette(props.context)
  const card = () => props.item ? reviewCard(props.item, props.source) : null
  return <Show when={card()} fallback={<text fg={colors.muted}>此范围没有条目。未列出风险不代表科学结论已验证。</text>}>
    {(value) => <box flexDirection="column">
      <text fg={colors.base}>{value().title}</text>
      <text fg={colors.base} marginTop={1}>{value().summary}</text>
      <Show when={value().relevance}><text fg={colors.muted} marginTop={1}>{value().relevance}</text></Show>
      <For each={value().sections}>{(section) => <Section context={props.context} title={section.title}>
        <For each={section.lines}>{(line) => <text fg={colors.base}>{line}</text>}</For>
      </Section>}</For>
      <Section context={props.context} title="材料与依据" count={value().links.length}>
        <For each={value().links}>{(link: ReviewLink, index) => <text fg={index() === props.selectedLink ? colors.brand : colors.link}>
          {`${index() === props.selectedLink ? "▸" : " "} ${link.label}  ${link.path ?? link.ref}`}
        </text>}</For>
        <text fg={colors.muted}>l 选择依据 · Enter 打开 · Backspace 返回对象 · x 查看原始数据</text>
      </Section>
      <Show when={props.raw}><Section context={props.context} title="查询数据（调试）">
        <text fg={colors.muted}>{JSON.stringify({ ...props.item, source: props.source }, null, 2)}</text>
      </Section></Show>
    </box>}
  </Show>
}
