import { expect, test } from "bun:test"
import type { Context } from "@opencode/plugin/tui/context"
import tui from "../tui"
import { sessionTitle } from "../src/session-title"

const goal = "你的唯一工作目录是当前目录\n\n研究目标：\n研究 FlashAttention（FA）输出中的微小数值误差。\n" + "保留完整研究要求。".repeat(40)

function harness(options?: { board?: () => Promise<any>; confirm?: boolean }) {
  let route: any = { type: "home" }
  let run!: (input: string) => Promise<void>
  const created: any[] = [], commands: any[] = [], titles: string[] = [], confirms: string[] = [], alerts: number[] = []
  const rpc = {
    board: options?.board ?? (async () => ({})),
    command: async (input: any) => { commands.push(input); return { message: "完成" } },
    events: { on: () => () => {} },
  }
  const context = {
    location: { directory: "/fixture" },
    client: { rpc: () => rpc, session: { create: async (input: any) => {
      created.push(input)
      return { id: "ses_created" }
    } } },
    ui: {
      router: { current: () => route, register: () => () => {}, navigate: (next: any) => {
        // Simulate the native terminal rejecting control characters in titles.
        const title = created.at(-1)?.title ?? "Existing session"
        if (/[\u0000-\u001f\u007f-\u009f]/u.test(title)) throw new Error("ot_session_control failed: InvalidArgument")
        titles.push(title); route = next
      } },
      dialog: {
        clear: () => {},
        confirm: async (input: any) => { confirms.push(input.message); return options?.confirm ?? true },
        show: (_render: any, close: () => void) => { alerts.push(1); close() },
      },
      model: { current: () => ({ modelID: "scripted", providerID: "mock" }) },
      slot: (input: any) => { if (input.append === "app") input.render(); return () => {} },
    },
    keymap: { layer: (value: any) => { run = value().commands[0].run } },
  } as unknown as Context
  const dispose = tui.setup(context) as () => void
  return { created, commands, titles, confirms, alerts, route: () => route, run: (input: string) => run(input), dispose }
}

for (const [cli, action] of [
  ["auto --pause", "init"],
  ["auto", "auto"],
] as const) {
  test(`home ${cli} safely titles a multiline goal without changing the RPC goal`, async () => {
    const host = harness()
    try {
      await host.run(`${cli} ${goal}`)
      expect(host.created).toHaveLength(1)
      expect(host.titles).toHaveLength(1)
      expect(host.created[0].title).not.toMatch(/[\u0000-\u001f\u007f-\u009f]/u)
      expect(Array.from(host.created[0].title).length).toBeLessThanOrEqual(80)
      expect(host.commands).toHaveLength(1)
      expect(host.commands[0]).toMatchObject({ action, sessionID: "ses_created", args: { goal } })
      if (action === "init") {
        await host.run("auto")
        expect(host.created).toHaveLength(1)
        expect(host.commands[1]).toMatchObject({ action: "auto", sessionID: "ses_created", args: { goal: null } })
      }
    } finally { host.dispose() }
  })
}

for (const [input, expected] of [
  ["FA\n\r\tFFN", "FA FFN"],
  ["a\0b\x1bc\x7fd\x85e\x9ff", "a b c d e f"],
  ["\n\0\t\x1b", "Research"],
  ["中文\u2028FA\u2029FFN", "中文 FA FFN"],
  ["a".repeat(79) + "🧪尾", "a".repeat(79) + "🧪"],
]) {
  test(`safe display title ${JSON.stringify(input)}`, () => {
    expect(sessionTitle(input!)).toBe(expected!)
    expect(sessionTitle(sessionTitle(input!))).toBe(expected!)
  })
}

test("bare /research opens the read-only board", async () => {
  const host = harness()
  try {
    await host.run("")
    expect(host.route()).toMatchObject({ type: "plugin", name: "research-board" })
    expect(host.commands).toHaveLength(0)
  } finally { host.dispose() }
})

test("auto adopts a quiet project without an OpenCode main session after confirmation", async () => {
  const quietBoard = async () => ({ project: { goal: "Existing quiet project" }, run: { state: "stopped" }, has_host_main: false })
  const cancelled = harness({ board: quietBoard, confirm: false })
  try {
    await cancelled.run("auto")
    expect(cancelled.confirms).toHaveLength(1)
    expect(cancelled.created).toHaveLength(0)
    expect(cancelled.commands).toHaveLength(0)
  } finally { cancelled.dispose() }

  const confirmed = harness({ board: quietBoard, confirm: true })
  try {
    await confirmed.run("auto focus on Y")
    expect(confirmed.confirms).toHaveLength(1)
    expect(confirmed.created).toHaveLength(1)
    expect(confirmed.commands.map((c) => c.action)).toEqual(["takeover", "auto"])
    expect(confirmed.commands[1]).toMatchObject({ action: "auto", sessionID: "ses_created", args: { goal: "focus on Y" } })
  } finally { confirmed.dispose() }
})
