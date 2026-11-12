/**
 * Terminal half of the research plugin: the /research slash command, the
 * full-screen board page, and a status panel and line inside research sessions.
 * Sessions outside a project look exactly as before; the empty app-slot claim
 * only owns the command's keymap layer.
 */
import { Plugin } from "@opencode/plugin/tui"
import { createSignal } from "solid-js"
import { ResearchRpc } from "./rpc"
import { TAKEOVER_WARNING, parseResearchCommand } from "./src/commands"
import { errorText } from "./src/format"
import { sessionTitle } from "./src/session-title"
import { ResearchBoard } from "./src/board/Board"
import { showText } from "./src/dialog"
import { ResearchComposerLine, ResearchSidebar, createSessionStatus } from "./src/status"

const BOARD = "research-board"
const SESSION_OPENERS = new Set(["init", "takeover", "auto"])
const QUIET_STATES = new Set(["manual", "stopped", "complete"])

export default Plugin.define({
  id: "auto-research.tui",
  setup(context) {
    const [previous, setPrevious] = createSignal({ ...context.ui.router.current() })
    const location = () => context.location ?? context.data.location.default()
    const alert = (message: string) => showText(context, "Research", message)
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
     * Prepare a main session for actions that open, adopt, or start a project.
     * On the home screen there is no session yet, so open one after a read-only
     * check; if an existing project has no OpenCode main session, confirm and
     * adopt it before starting autonomous work.
     */
    async function prepareOpener(
      action: string,
      currentSessionID: string | undefined,
      title: string,
    ): Promise<{ sessionID: string | undefined; cancelled?: boolean }> {
      const directory = location().directory
      const summary = (await context.client
        .rpc(ResearchRpc)
        .board({ view: "summary", directory }, { location: location() })) as {
        project?: { goal?: string } | null
        run?: { state?: string }
        has_host_main?: boolean
        error?: string
      }
      if (summary?.error) throw new Error(summary.error)
      const ensureSession = async (fallbackTitle: string) => {
        if (currentSessionID) return currentSessionID
        const session = await context.client.session.create({ title: sessionTitle(fallbackTitle), location: { directory } })
        context.ui.router.navigate({ type: "session", sessionID: session.id })
        return session.id
      }
      if (action === "init") {
        if (summary?.project) {
          throw new Error(`${directory} 已有研究项目。用 /research auto 继续，或 /research recover takeover 接管。`)
        }
        return { sessionID: await ensureSession(title) }
      }
      if (action === "takeover") {
        if (!summary?.project) throw new Error("当前目录及上级目录里没有研究项目。")
        return { sessionID: await ensureSession(summary.project.goal || "Research") }
      }
      if (summary?.project) {
        if (summary.has_host_main === false) {
          const runState = summary.run?.state
          if (runState && !QUIET_STATES.has(runState)) {
            throw new Error(`项目运行状态是「${runState}」，请先在原宿主停止项目再接管。`)
          }
          const confirmed = await context.ui.dialog.confirm({ title: "Research", message: TAKEOVER_WARNING })
          if (!confirmed) return { sessionID: currentSessionID, cancelled: true }
          const adoptedSession = await ensureSession(summary.project.goal || "Research")
          const taken = (await context.client
            .rpc(ResearchRpc)
            .command({ action: "takeover", sessionID: adoptedSession, directory, args: {} }, { location: location() })) as {
            error?: string
          }
          if (taken?.error) throw new Error(taken.error)
          return { sessionID: adoptedSession }
        }
        // An existing project already has a main session; the server returns it.
        return { sessionID: currentSessionID }
      }
      if (!currentSessionID && !title.trim()) throw new Error("当前目录还没有研究项目。用法：/research auto <研究目标>")
      return { sessionID: currentSessionID ?? (await ensureSession(title)) }
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
        if (SESSION_OPENERS.has(command.action)) {
          const title = command.action === "takeover" ? "Research" : String(command.args.goal ?? "")
          const prepared = await prepareOpener(command.action, sessionID, title)
          if (prepared.cancelled) return
          sessionID = prepared.sessionID
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
              description: "研究项目：打开看板，或 auto / pause / stop / budget / exploration / recover",
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
