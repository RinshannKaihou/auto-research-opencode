import { expect, test } from "bun:test"
import type { Context } from "@opencode/plugin/tui/context"
import tui from "../tui"
import { sessionTitle } from "../src/session-title"

const goal = "你的唯一工作目录是当前目录\n\n研究目标：\n研究 FlashAttention（FA）输出中的微小数值误差。\n" + "保留完整研究要求。".repeat(40)

function harness() {
  let route: any = { type: "home" }
  let run!: (input: string) => Promise<void>
  const created: any[] = [], commands: any[] = [], titles: string[] = []
  const rpc = {
    board: async () => ({}),
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
      dialog: { show: (_render: any, close: () => void) => close() },
      model: { current: () => ({ modelID: "scripted", providerID: "mock" }) },
      slot: (input: any) => { if (input.append === "app") input.render(); return () => {} },
    },
    keymap: { layer: (value: any) => { run = value().commands[0].run } },
  } as unknown as Context
  const dispose = tui.setup(context) as () => void
  return { created, commands, titles, run: (input: string) => run(input), dispose }
}

for (const action of ["init", "auto"]) {
  test(`home ${action} safely titles a multiline goal without changing the RPC goal`, async () => {
    const host = harness()
    try {
      await host.run(`${action} ${goal}`)
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
