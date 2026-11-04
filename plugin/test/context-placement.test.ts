import { describe, expect, test } from 'bun:test'
import { Effect } from 'effect'
import { LLMRequest, Message } from '@opencode/ai/effect'
import { AnthropicCompatible } from '@opencode/ai/providers/anthropic-compatible'
import {
  STABLE_CONTEXT_BLOCKS,
  markHistoryCacheBoundary,
  researchStateMessage,
  splitResearchContext,
} from '../src/context-placement'

const R1 = [
  'Research context:',
  '## identity',
  '{"node_id":null,"permissions":"research-read-write","role":"main"}',
  '',
  '## critical_now',
  '{"items":[],"total":0}',
  '',
  '## findings',
  '{"items":[],"total":0}',
  '',
  '## review_backlog',
  '{"pending_total":0}',
  '',
  '## clerical_index',
  '{"by_class":{}}',
  '',
  '## control_instructions',
  '"Use research tools to change the ledger."',
  '',
  '## role_instructions',
  '"You coordinate this research project autonomously."',
  '',
  '## project',
  '{"control":"manual","goal":"Fixture goal","run_state":"manual"}',
  '',
  '## checkpoint_recovery',
  '{"head_revision":0,"node_id":null}',
  '',
  '## questions',
  '[]',
  '',
  '## recent_node_notes',
  '[]',
].join('\n')

const LEGACY = [
  'Research context:',
  '## project',
  '{"control": "manual", "goal": "Fixture goal", "run_state": "manual"}',
  '',
  '## role_instructions',
  '"You coordinate this research project autonomously."',
  '',
  '## control_instructions',
  '"Use research tools to change the ledger."',
  '',
  '## identity',
  '{"node_id": null, "role": "main"}',
  '',
  '## checkpoint_recovery',
  '{"head_revision": 0, "node_id": null}',
  '',
  '## knowledge',
  '{"items": [], "omitted_count": 0}',
  '',
  '## method_guidance',
  '[]',
  '',
  '## index',
  '{"query": "Use research_query to expand project memory."}',
  '',
  '## omitted_index',
  '["specialist_task"]',
].join('\n')

function blockNames(text: string): string[] {
  return [...text.matchAll(/^## ([^\n]+)\n/gm)].map((match) => match[1]!)
}

function blockChunks(text: string): string[] {
  const body = text.startsWith('Research context:\n') ? text.slice('Research context:\n'.length) : text
  return body.split('\n\n').filter((chunk) => /^## [^\n]+\n/.test(chunk))
}

describe('splitResearchContext', () => {
  test('R1 blocks go to the allowlisted side exactly once', () => {
    const { stable, volatile } = splitResearchContext(R1)
    expect(blockNames(stable)).toEqual([
      'identity', 'control_instructions', 'role_instructions', 'project',
    ])
    expect(blockNames(volatile)).toEqual([
      'critical_now', 'findings', 'review_backlog', 'clerical_index',
      'checkpoint_recovery', 'questions', 'recent_node_notes',
    ])
    expect(blockNames(stable).length + blockNames(volatile).length).toBe(blockNames(R1).length)
    for (const chunk of blockChunks(R1)) {
      const count = blockChunks(`${stable}\n\n${volatile}`).filter((item) => item === chunk).length
      expect(count).toBe(1)
    }
  })

  test('legacy blocks use the same allowlist and keep their values byte-identical', () => {
    const { stable, volatile } = splitResearchContext(LEGACY)
    expect(blockNames(stable)).toEqual([
      'project', 'role_instructions', 'control_instructions', 'identity', 'method_guidance', 'index',
    ])
    expect(blockNames(volatile)).toEqual(['checkpoint_recovery', 'knowledge', 'omitted_index'])
    for (const chunk of blockChunks(LEGACY)) {
      expect(blockChunks(`${stable}\n\n${volatile}`)).toContain(chunk)
    }
  })

  test('the Research context header stays with the stable part', () => {
    const { stable, volatile } = splitResearchContext(R1)
    expect(stable.startsWith('Research context:\n## identity\n')).toBe(true)
    expect(volatile.startsWith('## ')).toBe(true)
  })

  test('the stale notice moves to the volatile part', () => {
    const stale = `${R1}\n\n[Research memory is stale. Execution control is unavailable; do not start research or assume it is running.]`
    const { stable, volatile } = splitResearchContext(stale)
    expect(volatile.endsWith('[Research memory is stale. Execution control is unavailable; do not start research or assume it is running.]')).toBe(true)
    expect(stable).not.toContain('Research memory is stale')
  })

  test('blockless safety text stays whole in the stable part', () => {
    const blind = 'You review materials independently. You see only the inputs assigned to you; judge them on their own content.'
    expect(splitResearchContext(blind)).toEqual({ stable: blind, volatile: '' })
    const unavailable = 'Research context and execution control are unavailable. Do not start research or assume it is running.'
    expect(splitResearchContext(unavailable)).toEqual({ stable: unavailable, volatile: '' })
  })

  test('an unknown block is volatile', () => {
    const text = 'Research context:\n## identity\n{"role":"main"}\n\n## future_block\n{"new":true}'
    const { stable, volatile } = splitResearchContext(text)
    expect(blockNames(stable)).toEqual(['identity'])
    expect(blockNames(volatile)).toEqual(['future_block'])
    expect(STABLE_CONTEXT_BLOCKS).not.toContain('future_block' as any)
  })

  test('text before the first block follows the stable part', () => {
    const text = 'Research context:\nPreamble line.\n\n## identity\n{"role":"main"}\n\n## findings\n[]'
    const { stable, volatile } = splitResearchContext(text)
    expect(stable).toContain('Preamble line.')
    expect(blockNames(volatile)).toEqual(['findings'])
  })
})

describe('researchStateMessage', () => {
  test('labels injected state as one user message outside the persisted history', () => {
    const message = researchStateMessage(['## findings\n[]', 'Evidence delivery was not fully confirmed (timeout).'])
    expect(message.role).toBe('user')
    expect(message.content).toHaveLength(1)
    const text = message.content[0]!.text
    expect(text.startsWith('<research_state>\n')).toBe(true)
    expect(text).toContain('not user input')
    expect(text).toContain('## findings\n[]')
    expect(text).toContain('Evidence delivery was not fully confirmed (timeout).')
    expect(text.endsWith('</research_state>')).toBe(true)
  })

  test('drops empty parts instead of leaving blank separators', () => {
    const text = researchStateMessage(['## findings\n[]', '   ']).content[0]!.text
    expect(text).not.toContain('\n\n\n')
  })
})

describe('markHistoryCacheBoundary', () => {
  const textPart = (text: string) => ({ type: 'text', text })

  test('marks the last text part of the persistent history and clones it', () => {
    const messages = [
      { role: 'user', content: [textPart('hello')] },
      { role: 'assistant', content: [textPart('working')] },
    ]
    const marked = markHistoryCacheBoundary({ messages })!
    expect(marked).not.toBe(messages)
    expect((messages[1]!.content[0] as any).cache).toBeUndefined()
    expect((marked[1]!.content[0] as any).cache).toEqual({ type: 'ephemeral' })
    expect((marked[1]!.content[0] as any).text).toBe('working')
    expect(marked[0]).toBe(messages[0]!)
  })

  test('marks a tool result and preserves its identity and result', () => {
    const messages = [
      { role: 'assistant', content: [{ type: 'tool-call', id: 'call_1', name: 'research_note', input: { body: 'x' } }] },
      { role: 'tool', content: [{ type: 'tool-result', id: 'call_1', name: 'research_note', result: { type: 'text', value: 'ok' } }] },
    ]
    const marked = markHistoryCacheBoundary({ messages })!
    const part = marked[1]!.content[0] as any
    expect(part.cache).toEqual({ type: 'ephemeral' })
    expect(part.id).toBe('call_1')
    expect(part.result).toEqual({ type: 'text', value: 'ok' })
  })

  test('skips reasoning, tool-call and compaction parts and walks back to a lowered boundary', () => {
    const messages = [
      { role: 'user', content: [textPart('hello')] },
      { role: 'assistant', content: [
        { type: 'reasoning', text: 'why', encrypted: 'sig' },
        { type: 'tool-call', id: 'call_1', name: 'research_note', input: {} },
      ] },
      { role: 'system', content: [{ type: 'compaction', provider: 'p', encrypted: 'x' }] },
    ]
    const marked = markHistoryCacheBoundary({ messages })!
    expect((marked[0]!.content[0] as any).cache).toEqual({ type: 'ephemeral' })
    expect(marked[1]).toBe(messages[1]!)
    expect((marked[1]!.content[0] as any).cache).toBeUndefined()
    expect((marked[1]!.content[0] as any).encrypted).toBe('sig')
    expect((marked[1]!.content[1] as any).cache).toBeUndefined()
    expect(marked[2]).toBe(messages[2]!)
  })

  test('a tail of reasoning, tool calls or media alone has no lowered boundary', () => {
    const messages = [
      { role: 'assistant', content: [
        { type: 'reasoning', text: 'why', encrypted: 'sig' },
        { type: 'tool-call', id: 'call_1', name: 'research_note', input: {} },
        { type: 'media', media: {} },
      ] },
    ]
    expect(markHistoryCacheBoundary({ messages })).toBeUndefined()
  })

  test('the installed Anthropic adapter keeps hints only on lowered boundary parts', async () => {
    const hint = { type: 'ephemeral' as const }
    const model = AnthropicCompatible.configure({ baseURL: 'http://127.0.0.1:1' }).model('claude-test')
    const request = new LLMRequest({
      model,
      system: [{ type: 'text', text: 'sys', cache: hint }],
      messages: [
        new Message({ role: 'assistant', content: [
          { type: 'reasoning', text: 'why', encrypted: 'sig', cache: hint },
          { type: 'tool-call', id: 'call_1', name: 'research_note', input: {}, cache: hint },
          { type: 'text', text: 'visible', cache: hint },
        ] as any }),
        new Message({ role: 'tool', content: [
          { type: 'tool-result', id: 'call_1', name: 'research_note',
            result: { type: 'text', value: 'ok' }, cache: hint },
        ] as any }),
      ],
      tools: [],
    })
    const body: any = await Effect.runPromise(model.route.body.from(request as any))
    const assistant = body.messages.find((message: any) => message.role === 'assistant')
    const tool = body.messages.find((message: any) => message.role === 'user')
    expect(assistant.content.find((part: any) => part.type === 'thinking').cache_control).toBeUndefined()
    expect(assistant.content.find((part: any) => part.type === 'tool_use').cache_control).toBeUndefined()
    expect(assistant.content.find((part: any) => part.type === 'text').cache_control).toEqual({ type: 'ephemeral' })
    expect(tool.content[0].cache_control).toEqual({ type: 'ephemeral' })
    expect(body.system[0].cache_control).toEqual({ type: 'ephemeral' })
  })

  test('returns undefined when no legal boundary exists', () => {
    expect(markHistoryCacheBoundary({ messages: [] })).toBeUndefined()
    expect(markHistoryCacheBoundary({ messages: [{ role: 'user', content: 'hello' }] })).toBeUndefined()
    expect(markHistoryCacheBoundary({ messages: [{ role: 'system', content: [{ type: 'compaction', provider: 'p' }] }] })).toBeUndefined()
  })

  test('a hint on a part the protocol drops still spends the SDK budget', () => {
    const messages = [
      { role: 'assistant', content: [{ type: 'reasoning', text: 'why', encrypted: 'sig', cache: { type: 'ephemeral' } }] },
      { role: 'user', content: [textPart('later')] },
    ]
    const system = [{ type: 'text', text: 'a', cache: { type: 'ephemeral' } }]
    const tools = {
      one: { description: 'one', input: {}, cache: { type: 'ephemeral' } },
      two: { description: 'two', input: {}, cache: { type: 'ephemeral' } },
    }
    expect(markHistoryCacheBoundary({ messages, system, tools })).toBeUndefined()
  })

  test('keeps an existing explicit hint and leaves the array alone', () => {
    const messages = [{ role: 'user', content: [{ type: 'text', text: 'hello', cache: { type: 'ephemeral' } }] }]
    expect(markHistoryCacheBoundary({ messages })).toBe(messages)
  })

  test('respects the SDK marker limit across tools, system and messages', () => {
    const system = [{ type: 'text', text: 'a', cache: { type: 'ephemeral' } }]
    const tools = {
      one: { description: 'one', input: {}, cache: { type: 'ephemeral' } },
      two: { description: 'two', input: {}, cache: { type: 'ephemeral' } },
      none: { description: 'none', input: {} },
    }
    const earlier = { role: 'user', content: [{ type: 'text', text: 'old', cache: { type: 'ephemeral' } }] }
    const target = { role: 'assistant', content: [textPart('new')] }
    expect(markHistoryCacheBoundary({ messages: [earlier, target], system, tools })).toBeUndefined()
    // An explicit hint already at the tail needs no new slot.
    const tailHinted = { role: 'assistant', content: [{ type: 'text', text: 'new', cache: { type: 'ephemeral' } }] }
    expect(markHistoryCacheBoundary({ messages: [earlier, tailHinted], system, tools })).toEqual([earlier, tailHinted])
  })
})
