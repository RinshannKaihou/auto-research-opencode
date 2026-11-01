/** Parses the raw input of `/research …` into one board action or one RPC command. */
import { USAGE } from "./format"

export type ParsedCommand =
  | { kind: "board" }
  | { kind: "error"; message: string }
  | { kind: "rpc"; action: string; args: Record<string, string | null>; confirm?: string }

const TAKEOVER_WARNING =
  "接管是单向操作：项目原来的主会话（可能在 DSH 里）会失去写入权限，DSH 版之后不能再控制这个项目。只有项目已停止或处于手动状态时才能接管。继续吗？"
const STOP_WARNING =
  "停止自主研究？排队任务会取消，运行中的会话会被请求中断；退出尚未核实时将保留待核对状态。已有记录都会保留。"
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
      // Still `goal` on the wire; on an existing project the engine treats it as guidance for the new run.
      return { kind: "rpc", action, args: { goal: tail || null } }
    case "stop":
      return { kind: "rpc", action, args: {}, confirm: STOP_WARNING }
    case "recover": {
      const controller = /^controller\s+(.+)$/.exec(tail)
      if (controller) return { kind: "rpc", action, args: { kind: "controller", reason: controller[1]! },
        confirm: "请确认已检查原控制器及其执行。仍在运行或状态不明的任务会保留为未核实；接管后项目保持暂停，不会自动重跑。继续吗？" }
      const execution = /^(task|specialist|resource)\s+(\S+)\s+(.+)$/.exec(tail)
      if (execution) return { kind: "rpc", action, args: { kind: execution[1]!, task_id: execution[2]!, reason: execution[3]! },
        confirm: "请确认已独立核实该执行及其外部进程已经退出。此操作会记录你的核对结论，不会自动重新执行。继续吗？" }
      const delivery = /^delivery\s+(report|wake)\s+(\S+)\s+(delivered|not-sent)\s+(.+)$/.exec(tail)
      if (delivery) return { kind: "rpc", action, args: { kind: "delivery", delivery_kind: delivery[1]!,
        notification_id: delivery[2]!, disposition: delivery[3] === 'not-sent' ? 'not_sent' : 'delivered', reason: delivery[4]! },
        confirm: "请确认已核对宿主是否接受了这条消息。选择未发送会允许使用原消息 ID 重试；核对结论会被记录。继续吗？" }
      return { kind: "error", message: "用法：/research recover controller <原因> | task|specialist|resource <任务 ID> <原因> | delivery report|wake <通知 ID> delivered|not-sent <原因>" }
    }
    case "retry": {
      const match = /^(\S+)\s+(.+)$/.exec(tail)
      if (!match) return { kind: "error", message: "用法：/research retry <任务 ID> <重试原因>" }
      return { kind: "rpc", action, args: { task_id: match[1]!, reason: match[2]! },
        confirm: "重新执行该节点？只有已由用户核实退出的任务可以重试，新执行将使用新的启动消息。" }
    }
    case "board":
      return { kind: "board" }
    case "init":
      if (!tail) return { kind: "error", message: "用法：/research init <研究目标>" }
      return { kind: "rpc", action, args: { goal: tail } }
    case "context-policy":
      if (tail && tail !== "legacy" && tail !== "r1") return { kind: "error", message: "用法：/research context-policy legacy|r1" }
      return { kind: "rpc", action, args: { policy: tail || null } }
    case "exploration":
      if (tail && tail !== "on" && tail !== "off") return { kind: "error", message: "用法：/research exploration [on|off]" }
      return { kind: "rpc", action, args: { mode: tail || null } }
    case "conclusion-contract":
      if (tail && tail !== "evidence-v1") return { kind: "error", message: "用法：/research conclusion-contract [evidence-v1]" }
      return { kind: "rpc", action, args: { contract: tail || null } }
    case 'budget': {
      if (!tail) return {kind:'rpc',action,args:{}}
      const resource = /^resource\s+(\S+)\s+(off|(?:0|[1-9]\d*)(?:\.\d+)?)\s+(.+)$/.exec(tail)
      if (resource) {
        if (resource[2]!=='off' && (!Number.isSafeInteger(Number(resource[2])) || Number(resource[2])<=0)) return {kind:'error',message:'资源容量须为正整数单位或 off'}
        return {kind:'rpc',action,args:{limit:'resource',resource:resource[1]!,value:resource[2]!,reason:resource[3]!}}
      }
      const match = /^(turns|nodes|seconds|tokens|cost|work)\s+(off|(?:0|[1-9]\d*)(?:\.\d+)?)\s+(.+)$/.exec(tail)
      if (!match) return {kind:'error',message:'用法：/research budget turns|nodes|seconds|tokens|cost|work <正数|off> <修改原因>；resource <名称> <容量> <修改原因>'}
      if(match[2]!=='off' && (!Number.isFinite(Number(match[2])) || Number(match[2])<=0 || !['cost','work'].includes(match[1]!) && !/^[1-9]\d*$/.test(match[2]!)))return {kind:'error',message:'预算须为正整数；cost/work 可使用正小数'}
      return {kind:'rpc',action,args:{limit:match[1]!,value:match[2]!,reason:match[3]!}}
    }
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
