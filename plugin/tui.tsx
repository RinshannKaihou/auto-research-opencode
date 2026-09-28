/**
 * Terminal half of the research plugin: the /research slash command, the
 * full-screen board page, and a status panel and line inside research sessions.
 * Sessions outside a project look exactly as before; the empty app-slot claim
 * only owns the command's keymap layer.
 */
import { Plugin } from "@opencode/plugin/tui"
import { createSignal } from "solid-js"
import { ResearchRpc } from "./rpc"
import { parseResearchCommand } from "./src/commands"
import { errorText } from "./src/format"
import { ResearchBoard } from "./src/board/Board"
import { ResearchComposerLine, ResearchSidebar, createSessionStatus } from "./src/status"

const BOARD = "research-board"
const SESSION_OPENERS = new Set(["init", "takeover", "auto"])

export default Plugin.define({
  id: "auto-research.tui",
  setup(context) {
    const [previous, setPrevious] = createSignal({ ...context.ui.router.current() })
    const location = () => context.location ?? context.data.location.default()
    const alert = (message: string) => context.ui.dialog.alert({ title: "Research", message })
    const status = createSessionStatus(context)

    const disposeRoute = context.ui.router.register({
      name: BOARD,
      render: ({ data }) => (
        <ResearchBoard
          context={context}
          sessionID={data?.sessionID}
          onClose={() => context.ui.router.navigate(previous())}
          openSession={(target) => context.ui.router.navigate({ type: "session", sessionID: target })}
        />
      ),
    })

    /**
     * init and takeover make the current session the research main session. On
     * the home screen there is none yet, so open one instead of asking the user
     * to send a throwaway message first. A quick read-only check comes first,
     * so a command that is going to fail never leaves an empty session behind.
     */
    async function sessionFor(action: string, title: string): Promise<string | undefined> {
      const directory = location().directory
      const summary = (await context.client
        .rpc(ResearchRpc)
        .board({ view: "summary", directory }, { location: location() })) as { project?: unknown; error?: string }
      if (summary?.error) throw new Error(summary.error)
      if (action === "init" && summary?.project) {
        throw new Error(`${directory} 已有研究项目。用 /research auto 继续，或 /research takeover 接管。`)
      }
      if (action === "takeover" && !summary?.project) throw new Error("当前目录及上级目录里没有研究项目。")
      // An existing project already has a main session; the server returns it.
      if (action === "auto" && summary?.project) return undefined
      if (action === "auto" && !title) throw new Error("当前目录还没有研究项目。用法：/research auto <研究目标>")
      const session = await context.client.session.create({ title, location: { directory } })
      context.ui.router.navigate({ type: "session", sessionID: session.id })
      return session.id
    }

    async function run(input?: string) {
      const route = context.ui.router.current()
      let sessionID = route.type === "session" ? route.sessionID : undefined
      const command = parseResearchCommand(input)
      if (command.kind === "error") return alert(command.message)
      if (command.kind === "board") {
        if (route.type === "plugin" && route.name === BOARD) return
        // The router exposes a mutable store; keep a copy of the route to return to.
        setPrevious({ ...route })
        context.ui.dialog.clear()
        context.ui.router.navigate({ type: "plugin", name: BOARD, data: { sessionID } })
        return
      }
      if (command.confirm) {
        const confirmed = await context.ui.dialog.confirm({ title: "Research", message: command.confirm })
        if (!confirmed) return
      }
      try {
        if (!sessionID && SESSION_OPENERS.has(command.action)) {
          const title = command.action === "takeover" ? "Research" : String(command.args.goal ?? "").slice(0, 80)
          sessionID = await sessionFor(command.action, title)
        }
        // Autonomous turns carry no model of their own; they use the one selected here.
        const selected = ["auto", "resume"].includes(command.action) ? context.ui.model.current() : undefined
        const model = selected
          ? { id: selected.modelID, providerID: selected.providerID, ...(selected.variant ? { variant: selected.variant } : {}) }
          : undefined
        const result = (await context.client
          .rpc(ResearchRpc)
          .command(
            { action: command.action, sessionID, directory: location().directory, args: { ...command.args, ...(model ? { model } : {}) } },
            { location: location() },
          )) as {
          message?: string
          error?: string
          sessionID?: string
        }
        const shown = result?.sessionID ?? sessionID
        if (result?.sessionID && result.sessionID !== sessionID) {
          context.ui.router.navigate({ type: "session", sessionID: result.sessionID })
        }
        if (shown) status.refresh(shown)
        await alert(result?.error ?? result?.message ?? "完成")
      } catch (error) {
        await alert(errorText(error))
      }
    }

    const disposeCommand = context.ui.slot({
      append: "app",
      render() {
        context.keymap.layer(() => ({
          mode: "global",
          commands: [
            {
              id: "auto-research.command",
              title: "Research",
              description: "研究项目：init / status / work / board / guidance / takeover / detach",
              group: "Research",
              slash: { name: "research", arguments: true },
              palette: true,
              run,
            },
          ],
        }))
        return null
      },
    })

    const disposeSidebar = context.ui.slot({
      append: "sidebar.content",
      render: (input) => <ResearchSidebar context={context} state={() => status.state(input.sessionID)()} />,
    })
    const disposeLine = context.ui.slot({
      prepend: "session.composer.top",
      render: (input) => <ResearchComposerLine context={context} state={() => status.state(input.sessionID)()} />,
    })

    return () => {
      disposeLine()
      disposeSidebar()
      disposeCommand()
      disposeRoute()
      status.dispose()
    }
  },
})
