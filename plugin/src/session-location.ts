import { realpathSync } from 'node:fs'
import { resolve } from 'node:path'

export function sameDirectory(left: string, right: string): boolean {
  const canonical = (path: string) => { try { return realpathSync(path) } catch { return resolve(path) } }
  return canonical(left) === canonical(right)
}

export function locationError(root: string, directory: string, scope: string): Error & {code: string} {
  return Object.assign(new Error(`research_session_location: 已登记研究会话的位置不受支持。研究根目录：${root}；会话目录：${directory}；插件目录：${scope}。请由用户协调将原会话移回研究根目录，核对执行状态后显式 /research resume；文件操作请使用节点 workspace 的明确路径。不会自动迁移或接管。`),
    {code: 'research_session_location'})
}

export function isSessionEntryError(error: unknown): error is Error & {code: string} {
  return ['research_session_location', 'research_tool_entry_unavailable'].includes((error as any)?.code)
}

// OpenCode 2.2.5/2.2.6 invoke execute.before for this effective name even when the
// invocation comes from execute -> tools.opencode.session_move (not code parsing).
export function isSessionMove(tool: string): boolean {
  return tool === 'opencode_session_move'
}

export function toolEntryError(root: string, missing: string[]): Error & {code: string} {
  return Object.assign(new Error(`research_tool_entry_unavailable: 研究根目录 ${root} 的工具入口缺少 ${missing.join(', ')}；停止续轮，请先核对插件加载和宿主工具目录，再显式 /research resume。`), {code: 'research_tool_entry_unavailable'})
}

/** Full research tools are expected only on an authorized working request. */
export function assertResearchTools(root: string, role: string | undefined, names: ReadonlySet<string>): void {
  const required = role === 'node_core' ? ['research_query', 'research_publish', 'research_checkpoint', 'research_finish']
    : role === 'main' ? ['research_query', 'research_dispatch'] : []
  const missing = required.filter(name => !names.has(name))
  if (missing.length) throw toolEntryError(root, missing)
}
