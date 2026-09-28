/**
 * Persistent research status inside a session: a sidebar panel and a line above
 * the composer. Both render nothing for sessions outside a research project,
 * and neither reaches the model.
 */
import type { Context } from "@opencode/plugin/tui/context"
import { Show, createSignal, type Accessor, type Setter } from "solid-js"
import { ResearchRpc } from "../rpc"
import { pauseReason, roleName, runState } from "./format"
import type { SessionResearch } from "./research"

export interface SessionStatusStore {
  state(sessionID: string): Accessor<SessionResearch | null>
  refresh(sessionID: string): void
  dispose(): void
}

/** One cached state per session, refreshed whenever the ledger reports a change. */
export function createSessionStatus(context: Context): SessionStatusStore {
  const rpc = context.client.rpc(ResearchRpc)
  const location = () => context.location ?? context.data.location.default()
  const states = new Map<string, [Accessor<SessionResearch | null>, Setter<SessionResearch | null>]>()

  async function load(sessionID: string) {
    try {
      const value = (await rpc.session({ sessionID }, { location: location() })) as SessionResearch & { error?: string }
      states.get(sessionID)?.[1](value && !value.error ? value : null)
    } catch {
      // A status panel must never interrupt the session; keep the last state.
    }
  }

  const stop = rpc.events.on("changed", () => {
    for (const sessionID of states.keys()) void load(sessionID)
  })

  return {
    state(sessionID) {
      if (!states.has(sessionID)) {
        states.set(sessionID, createSignal<SessionResearch | null>(null))
        void load(sessionID)
      }
      return states.get(sessionID)![0]
    },
    refresh(sessionID) {
      if (states.has(sessionID)) void load(sessionID)
    },
    dispose: stop,
  }
}

function activity(state: SessionResearch): string {
  if (state.runState !== "running") return runState(state.runState)
  if (state.pauseReason) return pauseReason(state.pauseReason)
  return "自主推进中"
}

function workLine(state: SessionResearch): string {
  if (state.role === "node_core") return `负责节点 ${state.nodeID ?? "?"}`
  if (state.role === "specialist") return `节点 ${state.nodeID ?? "?"} 的专家（只读）`
  const tasks = state.tasks ?? { queued: 0, running: 0, finished: 0 }
  return `节点任务：运行 ${tasks.running} · 排队 ${tasks.queued} · 完成 ${tasks.finished}`
}

export function ResearchSidebar(props: { context: Context; state: () => SessionResearch | null }) {
  const theme = props.context.theme
  const warn = (state: SessionResearch) =>
    state.pauseReason && !["wait", "segment_complete"].includes(state.pauseReason)
  return (
    <Show when={props.state()?.associated ? props.state() : null}>
      {(state) => (
        <box flexDirection="column" marginTop={1}>
          <text fg={theme.text.base}>Research</text>
          <text fg={theme.text.muted}>{roleName(state().role)}</text>
          <text fg={warn(state()) ? theme.text.feedback.warning.base : theme.text.muted}>{activity(state())}</text>
          <text fg={theme.text.muted}>{workLine(state())}</text>
          <text fg={theme.text.muted}>
            {`节点 ${state().counts?.nodes ?? 0} · 发布 ${state().counts?.publications ?? 0} · 知识 ${state().counts?.knowledge ?? 0}`}
          </text>
          <text fg={theme.text.muted} marginTop={1}>
            /research board 看板
          </text>
          <text fg={theme.text.muted}>{state().runState === "running" ? "/research pause 暂停" : "/research resume 恢复"}</text>
        </box>
      )}
    </Show>
  )
}

export function ResearchComposerLine(props: { context: Context; state: () => SessionResearch | null }) {
  const theme = props.context.theme
  return (
    <Show when={props.state()?.associated ? props.state() : null}>
      {(state) => (
        <text fg={theme.text.muted} wrapMode="none" paddingLeft={1}>
          {`Research · ${roleName(state().role)} · ${activity(state())} · ${workLine(state())} · /research board`}
        </text>
      )}
    </Show>
  )
}
