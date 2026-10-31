/**
 * Runtime tab: sessions and node tasks on the execution channel only. Opaque
 * session and task IDs stay dim; the conclusion record lives on the overview.
 */
import type { Context } from "@opencode/plugin/tui/context"
import { TextAttributes } from "@opentui/core"
import { For, Show } from "solid-js"
import { roleName, runState } from "../format"
import { RUN_NOTE, TASK_STATES, runtimeNotes, sessionActivity, type Row as RowData } from "./model"
import { palette } from "./palette"
import { EXECUTION, displayWidth, fit, runExecution, sessionExecution, taskExecution } from "./process"
import { Row, Section } from "./ui"

export function SessionRow(props: { context: Context; row: RowData; index: number; selected: boolean; width: number }) {
  const colors = palette(props.context)
  const session = () => props.row.data ?? {}
  const execution = () => sessionExecution(session())
  const role = () => `${roleName(session().role)}${session().detached ? "（已移出）" : ""}`
  const lead = () => 3 + displayWidth(props.row.code) + 2 + displayWidth(role()) + 3
  return (
    <Row context={props.context} id={`research-row-${props.index}`} selected={props.selected}>
      <span style={{ fg: colors.execution[execution()] }}>{` ${EXECUTION[execution()].glyph}`}</span>
      <span style={{ fg: colors.muted }}>{` ${props.row.code}  `}</span>
      <span style={{ fg: colors.base, attributes: props.selected ? TextAttributes.BOLD : TextAttributes.NONE }}>{role()}</span>
      <span style={{ fg: colors.muted }}>{` · ${fit(sessionActivity(session()), props.width - lead())}`}</span>
    </Row>
  )
}

function Field(props: { context: Context; label: string; value: string; dim?: boolean }) {
  const colors = palette(props.context)
  return (
    <text>
      <span style={{ fg: colors.muted }}>{`${props.label}  `}</span>
      <span style={{ fg: props.dim ? colors.muted : colors.base }}>{props.value}</span>
    </text>
  )
}

export function RuntimeDetail(props: { context: Context; summary: any; session: any; tasks: any[] }) {
  const colors = palette(props.context)
  const run = () => props.summary?.run ?? {}
  const execution = () => runExecution(run().state)
  return (
    <box flexDirection="column">
      <text wrapMode="none">
        <span style={{ fg: colors.execution[execution()], attributes: TextAttributes.BOLD }}>
          {`${EXECUTION[execution()].glyph} ${runState(run().state)}`}
        </span>
        <span style={{ fg: colors.muted }}>{`  ${RUN_NOTE}`}</span>
      </text>
      <For each={runtimeNotes(props.summary)}>{(note) => <text fg={colors.muted}>{note}</text>}</For>
      <Show when={props.summary?.runtime_budget?.available}>
        <text fg={colors.muted}>{`自主调度预算 · 主会话当前工作对象已预留 ${props.summary.runtime_budget.turns} 轮 / ${props.summary.runtime_budget.limits.max_session_turns ?? '未配置'} · 交付未核实 ${props.summary.runtime_budget.unconfirmed_reservations} · 节点 ${props.summary.runtime_budget.total_nodes} / ${props.summary.runtime_budget.limits.max_total_nodes ?? '未配置'}`}</text>
        <text fg={colors.muted}>{`截止 ${props.summary.runtime_budget.deadline_at ?? '未配置'} · token/cost 仅覆盖已观测调用；外部进程未计量`}</text>
        <text fg={colors.muted}>{`已观测 token ${props.summary.runtime_budget.tokens_observed ?? '未知'} / ${props.summary.runtime_budget.limits.max_tokens ?? '未配置'} · cost ${props.summary.runtime_budget.cost_observed ?? '未知'} / ${props.summary.runtime_budget.limits.max_cost ?? '未配置'}`}</text>
      </Show>
      <Field context={props.context} label="研究主会话" value={run().main_session_id ?? "无"} dim />
      <Section context={props.context} title="节点任务" count={props.tasks.length}>
        <For each={props.tasks} fallback={<text fg={colors.muted}>无</text>}>
          {(task) => {
            const state = taskExecution(task.state)
            return (
              <text wrapMode="none">
                <span style={{ fg: colors.execution[state] }}>{EXECUTION[state].glyph}</span>
                <span style={{ fg: colors.base }}>{` ${task.node_id}  `}</span>
                <span style={{ fg: colors.base }}>{TASK_STATES[task.state] ?? task.state}</span>
                <span style={{ fg: colors.warning }}>{task.error ? `  ${task.error}` : ""}</span>
                <span style={{ fg: colors.muted }}>{`  ${task.task_id}`}</span>
              </text>
            )
          }}
        </For>
      </Section>
      <Show when={props.session}>
        {(session) => {
          const state = () => sessionExecution(session())
          return (
            <Section context={props.context} title="会话">
              <text wrapMode="none">
                <span style={{ fg: colors.execution[state()] }}>{`${EXECUTION[state()].glyph} `}</span>
                <span style={{ fg: colors.base, attributes: TextAttributes.BOLD }}>
                  {`${roleName(session().role)}${session().detached ? "（已移出项目）" : ""}`}
                </span>
                <span style={{ fg: colors.muted }}>{`  ${sessionActivity(session())}`}</span>
              </text>
              <Field context={props.context} label="节点" value={session().node_id ?? "无"} />
              <Show when={session().task_id}>
                <Field context={props.context} label="专家任务" value={session().task_id} dim />
              </Show>
              <Field context={props.context} label="本次执行结束状态" value={session().close_state ?? "无"} />
              <Field context={props.context} label="会话 ID" value={session().session_id} dim />
              <text fg={colors.muted} marginTop={1}>
                Enter 打开这个会话
              </text>
            </Section>
          )
        }}
      </Show>
    </box>
  )
}
