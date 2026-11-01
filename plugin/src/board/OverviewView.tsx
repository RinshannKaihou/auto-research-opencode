/**
 * Overview tab: the goal and verdict first, then the final report beside what
 * is still open. The conclusion outcome is the only place a feedback color
 * stands for a scientific result; the run state keeps its execution glyph.
 */
import type { Context } from "@opencode/plugin/tui/context"
import { TextAttributes, type SyntaxStyle } from "@opentui/core"
import { For, Show } from "solid-js"
import { DELIVERY, DELIVERY_NOTE, LATEST_NOTE, type OverviewModel, type PublicationCard } from "./model"
import { palette } from "./palette"
import { EXECUTION, runExecution } from "./process"
import { Markdown, Section, Tag } from "./ui"

/** Two columns need room for both a report and the list of open issues. */
const TWO_COLUMNS = 110

export function PublicationBody(props: { context: Context; card: PublicationCard; markdown: SyntaxStyle; summary?: boolean }) {
  const colors = palette(props.context)
  return (
    <box flexDirection="column">
      <Show when={props.card.title}>
        <text>
          <span style={{ fg: colors.base, attributes: TextAttributes.BOLD }}>{props.card.title}</span>
        </text>
      </Show>
      <Show when={props.card.overview}>
        <text fg={colors.base}>{props.card.overview}</text>
      </Show>
      <For each={props.card.sections}>
        {(section) => (
          <box flexDirection="column" marginTop={1}>
            <text fg={colors.muted} wrapMode="none">
              {section.heading}
            </text>
            <For each={section.items}>
              {(item) => (
                <box flexDirection="row">
                  <text fg={colors.link} flexShrink={0}>
                    {"· "}
                  </text>
                  <text fg={colors.base} flexGrow={1}>
                    {item}
                  </text>
                </box>
              )}
            </For>
          </box>
        )}
      </For>
      <Show when={props.summary || (!props.card.overview && !props.card.sections.length)}>
        <box marginTop={props.card.title ? 1 : 0}>
          <Markdown style={props.markdown} content={props.card.summary} />
        </box>
      </Show>
    </box>
  )
}

function Numbered(props: { context: Context; items: string[]; marker: "number" | "dot"; muted?: boolean }) {
  const colors = palette(props.context)
  return (
    <For each={props.items}>
      {(item, index) => (
        <box flexDirection="row">
          <text fg={props.marker === "number" ? colors.warning : colors.muted} flexShrink={0} wrapMode="none">
            {props.marker === "number" ? `${index() + 1}  ` : "·  "}
          </text>
          <text fg={props.muted ? colors.muted : colors.base} flexGrow={1}>
            {item}
          </text>
        </box>
      )}
    </For>
  )
}

export function OverviewView(props: { context: Context; model: OverviewModel | null; width: number; markdown: SyntaxStyle }) {
  const colors = palette(props.context)
  const wide = () => props.width >= TWO_COLUMNS
  const leftWidth = () => (wide() ? Math.floor((props.width - 3) * 0.56) : props.width)
  const rightWidth = () => (wide() ? props.width - 3 - leftWidth() : props.width)

  return (
    <Show
      when={props.model}
      fallback={<text fg={colors.muted}>{"当前目录没有研究项目。用 /research init <研究目标> 新建。"}</text>}
    >
      {(model) => {
        const execution = () => runExecution(model().runState)
        const report = () => (
          <box flexDirection="column" width={leftWidth()}>
            <Show
              when={model().final}
              fallback={
                <Section context={props.context} title="最新阶段成果" count={model().latest ? LATEST_NOTE : undefined}>
                  <Show when={model().latest} fallback={<text fg={colors.muted}>尚无已登记的阶段成果。</text>}>
                    {(card) => <Card context={props.context} card={card()} markdown={props.markdown} />}
                  </Show>
                </Section>
              }
            >
              {(card) => (
                <Section context={props.context} title="最终报告">
                  <Card context={props.context} card={card()} markdown={props.markdown} reference={model().conclusion?.finalRef} />
                </Section>
              )}
            </Show>
            <Show when={model().presentation}>
              {(shown) => (
                <Section context={props.context} title="项目展示摘要" count={shown().stale ? "绑定的发布已过期，请核对新材料" : undefined}>
                  <Show when={shown().title}>
                    <text>
                      <span style={{ fg: colors.base, attributes: TextAttributes.BOLD }}>{shown().title}</span>
                    </text>
                  </Show>
                  <Show when={shown().summary}>
                    <text fg={colors.base}>{shown().summary}</text>
                  </Show>
                  <For each={shown().metrics}>
                    {(metric) => (
                      <box flexDirection="column">
                        <text>
                          <span style={{ fg: colors.muted }}>{`${metric.label}  `}</span>
                          <span style={{ fg: colors.base, attributes: TextAttributes.BOLD }}>{metric.values}</span>
                        </text>
                        <Show when={metric.warning}>
                          <text fg={colors.warning}>{`  ${metric.warning}`}</text>
                        </Show>
                      </box>
                    )}
                  </For>
                  <For each={shown().deliverables}>
                    {(item) => (
                      <text>
                        <span style={{ fg: colors.muted }}>交付  </span>
                        <span style={{ fg: colors.base }}>{`${item.label}  `}</span>
                        <span style={{ fg: colors.link }}>{item.ref}</span>
                      </text>
                    )}
                  </For>
                </Section>
              )}
            </Show>
            <Show when={model().presentationError !== undefined}>
              <Section context={props.context} title="项目展示摘要">
                <text fg={colors.warning}>{`不可用：${model().presentationError}`}</text>
              </Section>
            </Show>
          </box>
        )
        const open = () => (
          <box flexDirection="column" width={rightWidth()}>
            <Show when={model().conclusion}>
              {(conclusion) => (
                <>
                  <Section context={props.context} title="未解决项" count={conclusion().gaps.length}>
                    <Show when={conclusion().gaps.length} fallback={<text fg={colors.muted}>未登记未解决项</text>}>
                      <Numbered context={props.context} items={conclusion().gaps} marker="number" />
                    </Show>
                  </Section>
                  <Section context={props.context} title="审阅" count={`${conclusion().reviewLabel}（${conclusion().reviewNote}）`}>
                    <Numbered context={props.context} items={conclusion().limitations} marker="dot" muted />
                    <For each={conclusion().refs}>
                      {(ref) => (
                        <text>
                          <span style={{ fg: colors.muted }}>审阅材料  </span>
                          <span style={{ fg: colors.link }}>{ref}</span>
                        </text>
                      )}
                    </For>
                  </Section>
                  <Show when={conclusion().closure}>
                    <Section context={props.context} title="证据与停止">
                      <text fg={colors.base}>{`证据状态  ${conclusion().closure}`}</text>
                      <text fg={colors.base}>{`停止理由  ${conclusion().stopReason ?? '未记录'}`}</text>
                      <Show when={conclusion().appendixRef}>
                        <text fg={colors.link}>{`证据附录  ${conclusion().appendixRef}`}</text>
                      </Show>
                    </Section>
                  </Show>
                </>
              )}
            </Show>
            <Show when={model().final && model().latest}>
              <Section context={props.context} title="最新阶段成果" count={LATEST_NOTE}>
                <Card context={props.context} card={model().latest!} markdown={props.markdown} />
              </Section>
            </Show>
            <Section context={props.context} title="记录">
              <text fg={colors.muted}>{`项目目录  ${model().root}`}</text>
              <text fg={colors.muted}>{DELIVERY_NOTE}</text>
            </Section>
          </box>
        )
        // The full goal, wrapped: the header shortens it to one line, and a long
        // goal's scope and constraints come last. The header omits it on this tab.
        return (
          <box flexDirection="column">
            <text>
              <span style={{ fg: colors.base, attributes: TextAttributes.BOLD }}>{model().goal}</span>
            </text>
            <text wrapMode="none" marginTop={1}>
              <span style={{ fg: colors.execution[execution()] }}>
                {`${EXECUTION[execution()].glyph} ${model().runLabel}`}
              </span>
              <span style={{ fg: colors.muted }}>{`  ${model().runNote}`}</span>
              <Show when={model().conclusion}>
                {(conclusion) => (
                  <>
                    <span style={{ fg: colors.muted }}>{"     结项  "}</span>
                    <Tag
                      label={conclusion().outcomeLabel}
                      fg={colors.background}
                      bg={colors.outcome[conclusion().outcome] ?? colors.muted}
                    />
                  </>
                )}
              </Show>
            </text>
            <For each={model().notices}>{(notice) => <text fg={colors.warning}>{notice}</text>}</For>
            <Show when={model().exploration}>
              {(exploration) => <text fg={colors.base}>{`当前选择  ${exploration().objective} · 候选 ${exploration().routesTotal} · 待处理结果 ${exploration().pendingTotal}`}</text>}
            </Show>
            <text wrapMode="none" marginTop={1}>
              <For each={model().counts}>
                {(count) => (
                  <>
                    <span style={{ fg: colors.base, attributes: TextAttributes.BOLD }}>{String(count.value)}</span>
                    <span style={{ fg: colors.muted }}>{` ${count.label}     `}</span>
                  </>
                )}
              </For>
            </text>
            <box flexDirection={wide() ? "row" : "column"} columnGap={3}>
              {report()}
              {open()}
            </box>
          </box>
        )
      }}
    </Show>
  )
}

function Card(props: { context: Context; card: PublicationCard; markdown: SyntaxStyle; reference?: string }) {
  const colors = palette(props.context)
  return (
    <box flexDirection="column">
      <PublicationBody context={props.context} card={props.card} markdown={props.markdown} />
      <text wrapMode="none" marginTop={1}>
        <span style={{ fg: colors.link }}>{props.reference ?? props.card.id}</span>
        <span style={{ fg: colors.muted }}>
          {`  ${DELIVERY[props.card.status] ?? props.card.status}${props.card.node ? ` · ${props.card.node}` : ""}`}
        </span>
      </text>
    </box>
  )
}
