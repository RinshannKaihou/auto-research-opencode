/** Parses the raw input of `/research …` into one board action or one RPC command. */
import { USAGE } from "./format"

export type ParsedCommand =
  | { kind: "board" }
  | { kind: "error"; message: string }
  | { kind: "rpc"; action: string; args: Record<string, string | null>; confirm?: string }

const TAKEOVER_WARNING =
  "接管是单向操作：项目原来的主会话（可能在 DSH 里）会失去写入权限，DSH 版之后不能再控制这个项目。只有项目已停止或处于手动状态时才能接管。继续吗？"
const STOP_WARNING =
  "停止自主研究？所有正在运行的研究会话会被中断，排队和进行中的节点任务会被取消；已有记录都保留，之后可以用 /research auto 重新开始。"
const DETACH_WARNING =
  "把本会话移出研究项目？对话和已发布的材料都会保留；之后要重新关联，需要用 /research takeover。"

export function parseResearchCommand(raw: string | undefined): ParsedCommand {
  const input = (raw ?? "").trim()
  const [action = "help"] = input ? input.split(/\s+/) : []
  const tail = input.slice(action.length).trim()
  switch (action) {
    case "help":
    case "status":
    case "pause":
    case "resume":
      return { kind: "rpc", action, args: {} }
    case "auto":
      return { kind: "rpc", action, args: { goal: tail || null } }
    case "stop":
      return { kind: "rpc", action, args: {}, confirm: STOP_WARNING }
    case "board":
      return { kind: "board" }
    case "init":
      if (!tail) return { kind: "error", message: "用法：/research init <研究目标>" }
      return { kind: "rpc", action, args: { goal: tail } }
    case "guidance": {
      if (!tail) return { kind: "rpc", action, args: {} }
      const match = tail.match(/^(.*?)(?:\s+--version\s+(\S+))?$/)
      return { kind: "rpc", action, args: { path: match?.[1] ?? tail, version: match?.[2] ?? null } }
    }
    case "takeover":
      return { kind: "rpc", action, args: {}, confirm: TAKEOVER_WARNING }
    case "detach":
      return { kind: "rpc", action, args: {}, confirm: DETACH_WARNING }
    default:
      return { kind: "error", message: `未知的子命令：${action}\n\n${USAGE}` }
  }
}
