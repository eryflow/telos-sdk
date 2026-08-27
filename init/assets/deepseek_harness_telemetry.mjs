/**
 * TELOS tracing backend for deepseek-ai/deepseek-harness.
 *
 * This is shipped as runtime-ready ESM.  The installer loads it from the
 * TELOS home, while DeepSeek Harness owns the telemetry service definition.
 */

import { createHash } from 'node:crypto'
import { readFileSync, statSync } from 'node:fs'
import { createRequire } from 'node:module'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

const dshHome = process.env.DSH_HOME || join(process.env.HOME || '', '.dsh')
const requireFromProfile = createRequire(pathToFileURL(join(dshHome, 'profiles', 'package.json')))
const telemetryModule = process.env.TELOS_DSH_TELEMETRY_MODULE_URL
  || pathToFileURL(requireFromProfile.resolve('@deepseek-ai/dsh-session-telemetry')).href
const { SessionTelemetryBackend, SessionTelemetryCoordinator } = await import(telemetryModule)

const SOURCE = 'deepseek-harness'
const UUID_NAMESPACE_URL = Buffer.from('6ba7b8119dad11d180b400c04fd430c8', 'hex')

function uuid5(name) {
  const bytes = createHash('sha1').update(UUID_NAMESPACE_URL).update(name).digest().subarray(0, 16)
  bytes[6] = (bytes[6] & 0x0f) | 0x50
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = bytes.toString('hex')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

const entityId = (kind, externalId) => uuid5(`${SOURCE}:${kind}:${externalId}`)
const operation = (entity, body) => ({ entity, op: 'upsert', body })
const micros = (millis) => Math.trunc(millis * 1000)

function statusForTurn(reason) {
  switch (reason?.kind) {
    case 'aborted': return 'cancelled'
    case 'error': return 'error'
    case 'interrupted': return 'cancelled'
    case 'completed':
    case 'blocked':
    case 'max-tokens': return 'ok'
    default: return 'unknown'
  }
}

function errorForTurn(reason) {
  return reason?.kind === 'error' ? reason.error : undefined
}

function toolFailed(body) {
  return body?.message?.content?.[0]?.isError === true
}

function newSessionState(timeUs, attributes) {
  return {
    startTimeUs: timeUs,
    attributes,
    turn: undefined,
    turnStartUs: undefined,
    traceInput: undefined,
    lastAssistant: undefined,
    step: undefined,
    stepStartUs: undefined,
    llmInput: undefined,
    llmSequence: undefined,
    llmStartUs: undefined,
    llmModel: undefined,
    llmProvider: undefined,
    ttftUs: undefined,
    tools: new Map(),
  }
}

function threadBody(sessionId, state, timeUs, status = 'running') {
  const body = {
    id: entityId('thread', sessionId),
    project_name: 'default',
    harness: SOURCE,
    external_id: sessionId,
    name: `DeepSeek Harness ${sessionId}`,
    status,
    start_time_us: state.startTimeUs,
    metadata: {
      model_span_source: 'adapter',
      cwd: state.attributes['session.cwd'],
      parent_session_id: state.attributes['session.parent_id'],
      seed_length: state.attributes['session.seed_length'],
    },
  }
  if (status !== 'running') body.end_time_us = timeUs
  return body
}

function traceBody(sessionId, state, timeUs, fields = {}) {
  const externalId = `${sessionId}:${state.turn}`
  return {
    id: entityId('trace', externalId),
    project_name: 'default',
    thread_id: entityId('thread', sessionId),
    harness: SOURCE,
    source: SOURCE,
    external_id: externalId,
    name: `Turn ${state.turn}`,
    status: 'running',
    start_time_us: state.turnStartUs ?? timeUs,
    input: state.traceInput,
    metadata: { turn: state.turn, model_span_source: 'adapter' },
    tags: ['deepseek-harness'],
    source_updated_at_us: timeUs,
    ...fields,
  }
}

function stepBody(sessionId, state, timeUs, fields = {}) {
  const traceExternalId = `${sessionId}:${state.turn}`
  const externalId = `${traceExternalId}:${state.step}:step`
  return {
    id: entityId('span', externalId),
    trace_id: entityId('trace', traceExternalId),
    parent_span_id: null,
    source: SOURCE,
    external_id: externalId,
    name: `Step ${state.step}`,
    type: 'agent',
    status: 'running',
    start_time_us: state.stepStartUs ?? timeUs,
    metadata: { turn: state.turn, step: state.step },
    tags: [],
    source_updated_at_us: timeUs,
    ...fields,
  }
}

function llmBody(sessionId, state, timeUs, fields = {}) {
  const traceExternalId = `${sessionId}:${state.turn}`
  const stepExternalId = `${traceExternalId}:${state.step}:step`
  const externalId = `${traceExternalId}:${state.step}:llm:${state.llmSequence}`
  return {
    id: entityId('span', externalId),
    trace_id: entityId('trace', traceExternalId),
    parent_span_id: entityId('span', stepExternalId),
    source: SOURCE,
    external_id: externalId,
    name: 'LLM',
    type: 'llm',
    status: 'running',
    start_time_us: state.llmStartUs ?? state.stepStartUs ?? timeUs,
    input: state.llmInput,
    metadata: { turn: state.turn, step: state.step },
    tags: [],
    model: state.llmModel,
    provider: state.llmProvider,
    ttft_us: state.ttftUs,
    source_updated_at_us: timeUs,
    ...fields,
  }
}

function toolBody(sessionId, state, callId, timeUs, fields = {}) {
  const traceExternalId = `${sessionId}:${state.turn}`
  const stepExternalId = `${traceExternalId}:${state.step}:step`
  const externalId = `${traceExternalId}:${state.step}:tool:${callId}`
  const opened = state.tools.get(String(callId))
  return {
    id: entityId('span', externalId),
    trace_id: entityId('trace', traceExternalId),
    parent_span_id: entityId('span', stepExternalId),
    source: SOURCE,
    external_id: externalId,
    name: opened?.name || 'tool',
    type: 'tool',
    status: 'running',
    start_time_us: opened?.startTimeUs ?? timeUs,
    input: opened?.input,
    metadata: { turn: state.turn, step: state.step, call_id: callId },
    tags: [],
    source_updated_at_us: timeUs,
    ...fields,
  }
}

function ancestors(sessionId, state, timeUs, includeStep = false) {
  const out = [operation('thread', threadBody(sessionId, state, timeUs))]
  if (state.turn !== undefined) out.push(operation('trace', traceBody(sessionId, state, timeUs)))
  if (includeStep && state.step !== undefined) out.push(operation('span', stepBody(sessionId, state, timeUs)))
  return out
}

/** Convert one native SessionTelemetryRecord into idempotent entity snapshots. */
function mapRecord(record, sessions) {
  const attributes = record?.attributes || {}
  const sessionId = String(attributes['session.id'] || '')
  if (!sessionId) return []
  const timeUs = micros(Number(record.time) || Date.now())
  let state = sessions.get(sessionId)
  if (!state) {
    state = newSessionState(timeUs, attributes)
    sessions.set(sessionId, state)
  } else {
    state.attributes = { ...state.attributes, ...attributes }
  }

  if (record.channel === 'ops') {
    const op = attributes['telemetry.op']
    if (op === 'agent-error') {
      state.turn ??= attributes.turn
      state.step ??= attributes.step
      state.turnStartUs ??= timeUs
      state.stepStartUs ??= timeUs
      const detail = record.body || { message: 'agent error' }
      const out = ancestors(sessionId, state, timeUs, state.step !== undefined)
      for (const callId of state.tools.keys()) {
        out.push(operation('span', toolBody(sessionId, state, callId, timeUs, {
          status: 'error', end_time_us: timeUs, error: detail,
        })))
      }
      if (state.step !== undefined && state.llmSequence !== undefined) {
        out.push(operation('span', llmBody(sessionId, state, timeUs, {
          status: 'error', end_time_us: timeUs, error: detail,
        })))
      }
      if (state.step !== undefined) {
        out.push(operation('span', stepBody(sessionId, state, timeUs, {
          status: 'error', end_time_us: timeUs, error: detail,
        })))
      }
      if (state.turn !== undefined) {
        out.push(operation('trace', traceBody(sessionId, state, timeUs, {
          status: 'error', end_time_us: timeUs, error: detail,
        })))
      }
      return out
    }
    if (op === 'shutdown') {
      const out = ancestors(sessionId, state, timeUs, state.step !== undefined)
      for (const callId of state.tools.keys()) {
        out.push(operation('span', toolBody(sessionId, state, callId, timeUs, {
          status: 'abandoned', end_time_us: timeUs,
        })))
      }
      if (state.step !== undefined && state.llmSequence !== undefined) {
        out.push(operation('span', llmBody(sessionId, state, timeUs, {
          status: 'abandoned', end_time_us: timeUs,
        })))
      }
      if (state.step !== undefined) {
        out.push(operation('span', stepBody(sessionId, state, timeUs, {
          status: 'abandoned', end_time_us: timeUs,
        })))
      }
      if (state.turn !== undefined) {
        out.push(operation('trace', traceBody(sessionId, state, timeUs, {
          status: 'abandoned', end_time_us: timeUs,
        })))
      }
      out.push(operation('thread', threadBody(sessionId, state, timeUs, 'ok')))
      sessions.delete(sessionId)
      return out
    }
    return []
  }

  const type = String(attributes['event.type'] || '')
  const body = record.body || {}
  if (type === 'agent/error') {
    return mapRecord({
      ...record,
      channel: 'ops',
      attributes: { ...attributes, 'telemetry.op': 'agent-error' },
    }, sessions)
  }
  switch (type) {
    case 'turn/start': {
      state.turn = body.turn
      state.turnStartUs = timeUs
      state.traceInput = undefined
      state.lastAssistant = undefined
      state.step = undefined
      state.stepStartUs = undefined
      state.llmInput = undefined
      state.llmSequence = undefined
      state.llmStartUs = undefined
      state.llmModel = undefined
      state.llmProvider = undefined
      state.ttftUs = undefined
      state.tools.clear()
      return ancestors(sessionId, state, timeUs)
    }
    case 'user/message': {
      if (state.turn === undefined || body?.source?.kind !== 'user') return []
      state.traceInput = body
      return [
        ...ancestors(sessionId, state, timeUs),
        operation('trace', traceBody(sessionId, state, timeUs)),
      ]
    }
    case 'step/start': {
      state.turn = body.turn
      state.step = body.step
      state.stepStartUs = timeUs
      state.llmInput = undefined
      state.llmSequence = undefined
      state.llmStartUs = undefined
      state.llmModel = undefined
      state.llmProvider = undefined
      state.ttftUs = undefined
      return [
        ...ancestors(sessionId, state, timeUs),
        operation('span', stepBody(sessionId, state, timeUs)),
      ]
    }
    case 'request/header': {
      if (state.turn === undefined || state.step === undefined) return []
      const config = body?.header?.config || {}
      state.llmSequence = attributes['event.seq']
      state.llmStartUs = timeUs
      state.ttftUs = undefined
      state.llmInput = body.header
      state.llmModel = config.model
      state.llmProvider = config.provider
      return [
        ...ancestors(sessionId, state, timeUs, true),
        operation('span', llmBody(sessionId, state, timeUs, {
          input: body.header,
        })),
      ]
    }
    case 'assistant/chunk': {
      if (state.turn === undefined || state.step === undefined) return []
      state.llmSequence ??= attributes['event.seq']
      state.ttftUs ??= Math.max(0, timeUs - (state.llmStartUs ?? state.stepStartUs ?? timeUs))
      return [
        ...ancestors(sessionId, state, timeUs, true),
        operation('span', llmBody(sessionId, state, timeUs, {
          ttft_us: state.ttftUs,
        })),
      ]
    }
    case 'assistant/message': {
      state.turn = body.turn
      state.step = body.step
      state.llmSequence ??= attributes['event.seq']
      const usage = body.usage || {}
      const source = body?.message?.source || {}
      state.lastAssistant = body.message
      state.llmModel = source.model || state.llmModel
      state.llmProvider = source.provider || state.llmProvider
      return [
        ...ancestors(sessionId, state, timeUs, true),
        operation('span', llmBody(sessionId, state, timeUs, {
          name: source.model ? `LLM ${source.model}` : 'LLM',
          status: body.interrupted ? 'cancelled' : 'ok',
          end_time_us: timeUs,
          output: body.message,
          usage: body.usage,
          input_tokens: usage.inputTokens,
          output_tokens: usage.outputTokens,
          cache_read_tokens: usage.cacheReadTokens,
          cache_write_tokens: usage.cacheWriteTokens,
          reasoning_tokens: usage.reasoningTokens,
          model: state.llmModel,
          provider: state.llmProvider,
        })),
      ]
    }
    case 'tool/call': {
      state.turn = body.turn
      state.step = body.step
      state.tools.set(String(body.callId), {
        name: body.name,
        startTimeUs: timeUs,
        input: { arguments: body.arguments },
      })
      return [
        ...ancestors(sessionId, state, timeUs, true),
        operation('span', toolBody(sessionId, state, body.callId, timeUs, {
          name: body.name,
          input: { arguments: body.arguments },
        })),
      ]
    }
    case 'tool/result': {
      state.turn = body.turn
      state.step = body.step
      const callId = body?.message?.source?.callId || body?.message?.content?.[0]?.toolCallId
      if (callId === undefined) return []
      const failed = toolFailed(body)
      const result = [
        ...ancestors(sessionId, state, timeUs, true),
        operation('span', toolBody(sessionId, state, callId, timeUs, {
          status: failed ? 'error' : 'ok',
          end_time_us: timeUs,
          output: { message: body.message, meta: body.meta },
          error: failed ? (body.error || { message: 'tool returned an error' }) : undefined,
        })),
      ]
      state.tools.delete(String(callId))
      return result
    }
    case 'step/end': {
      state.turn = body.turn
      state.step = body.step
      const out = [
        ...ancestors(sessionId, state, timeUs),
        ...[...state.tools.keys()].map(callId => operation(
          'span', toolBody(sessionId, state, callId, timeUs, {
            status: 'abandoned', end_time_us: timeUs,
          }),
        )),
        operation('span', stepBody(sessionId, state, timeUs, {
          status: 'ok', end_time_us: timeUs,
        })),
      ]
      state.step = undefined
      state.stepStartUs = undefined
      state.llmInput = undefined
      state.llmSequence = undefined
      state.llmStartUs = undefined
      state.llmModel = undefined
      state.llmProvider = undefined
      state.ttftUs = undefined
      state.tools.clear()
      return out
    }
    case 'turn/end': {
      state.turn = body.turn
      const status = statusForTurn(body.reason)
      const out = [
        ...ancestors(sessionId, state, timeUs, state.step !== undefined),
        operation('trace', traceBody(sessionId, state, timeUs, {
          status,
          end_time_us: timeUs,
          output: { reason: body.reason, message: state.lastAssistant },
          error: errorForTurn(body.reason),
        })),
      ]
      for (const callId of state.tools.keys()) {
        out.push(operation('span', toolBody(sessionId, state, callId, timeUs, {
          status, end_time_us: timeUs, error: errorForTurn(body.reason),
        })))
      }
      if (state.step !== undefined && state.llmSequence !== undefined) {
        out.push(operation('span', llmBody(sessionId, state, timeUs, {
          status, end_time_us: timeUs, error: errorForTurn(body.reason),
        })))
      }
      if (state.step !== undefined) {
        out.push(operation('span', stepBody(sessionId, state, timeUs, {
          status, end_time_us: timeUs, error: errorForTurn(body.reason),
        })))
      }
      state.turn = undefined
      state.turnStartUs = undefined
      state.traceInput = undefined
      state.lastAssistant = undefined
      state.step = undefined
      state.stepStartUs = undefined
      state.llmInput = undefined
      state.llmSequence = undefined
      state.llmStartUs = undefined
      state.llmModel = undefined
      state.llmProvider = undefined
      state.ttftUs = undefined
      state.tools.clear()
      return out
    }
    default:
      return []
  }
}

function positiveInteger(value, name, fallback, max = Number.MAX_SAFE_INTEGER) {
  if (value === undefined) return fallback
  if (!Number.isInteger(value) || value <= 0 || value > max) {
    throw new Error(`${name} must be a positive integer no greater than ${max}`)
  }
  return value
}

export class TelosSessionTelemetryBackend extends SessionTelemetryBackend {
  static inject = ['sessions']
  sharing = 'full'

  constructor(ctx, config = {}) {
    super(ctx)
    const endpoint = new URL(config.endpoint || 'http://127.0.0.1:7171/__telos/tracing/v1/batch')
    if (endpoint.protocol !== 'http:' && endpoint.protocol !== 'https:') {
      throw new Error(`telos tracing endpoint must be http(s), got ${endpoint.protocol}`)
    }
    if (!config.tokenFile) throw new Error('telos tracing tokenFile is required')
    if (process.platform !== 'win32' && (statSync(config.tokenFile).mode & 0o077) !== 0) {
      throw new Error(`telos tracing token file must be mode 0600: ${config.tokenFile}`)
    }
    this.ctx = ctx
    this.endpoint = endpoint.href
    this.token = readFileSync(config.tokenFile, 'utf8').trim()
    if (!this.token) throw new Error('telos tracing token file is empty')
    this.queueSize = positiveInteger(config.queueSize, 'queueSize', 2048)
    this.batchSize = positiveInteger(config.batchSize, 'batchSize', 256, 256)
    this.maxBatchBytes = positiveInteger(config.maxBatchBytes, 'maxBatchBytes', 900 * 1024, 1024 * 1024)
    this.requestTimeoutMs = positiveInteger(config.requestTimeoutMs, 'requestTimeoutMs', 2000, 2_147_483_647)
    this.retryDelayMs = positiveInteger(config.retryDelayMs, 'retryDelayMs', 1000, 2_147_483_647)
    this.shutdownTimeoutMs = positiveInteger(config.shutdownTimeoutMs, 'shutdownTimeoutMs', 3000, 2_147_483_647)
    this.queue = []
    this.sessions = new Map()
    this.pending = undefined
    this.retryTimer = undefined
    this.retryPending = false
    this.closed = false
    this.dropped = 0
    new SessionTelemetryCoordinator(ctx, this, 'live')
  }

  /** The capture hot path: one bounded in-memory push and a microtask hint. */
  emit(record) {
    if (this.closed) return
    if (this.queue.length >= this.queueSize) {
      this.queue.shift()
      this.dropped += 1
    }
    this.queue.push({ record })
    this.schedule()
  }

  schedule(delay = 0) {
    if (this.closed || this.pending || this.retryTimer) return
    if (delay > 0) {
      this.retryTimer = setTimeout(() => {
        this.retryTimer = undefined
        this.schedule()
      }, delay)
      return
    }
    queueMicrotask(() => {
      if (this.closed || this.pending) return
      this.pending = this.drain().finally(() => {
        this.pending = undefined
        const retry = this.retryPending
        this.retryPending = false
        if (!this.closed && this.queue.length > 0 && !this.retryTimer) {
          this.schedule(retry ? this.retryDelayMs : 0)
        }
      })
    })
  }

  async drain(deadline = Number.POSITIVE_INFINITY) {
    while (this.queue.length > 0 && Date.now() < deadline) {
      const items = []
      const operations = []
      let operationBytes = 0
      while (this.queue.length > 0 && operations.length < this.batchSize) {
        const item = this.queue.shift()
        const next = item.operations || mapRecord(item.record, this.sessions)
        if (next.length === 0) continue
        if (operations.length > 0 && operations.length + next.length > this.batchSize) {
          this.queue.unshift(item)
          break
        }
        const nextBytes = Buffer.byteLength(JSON.stringify(next))
        if (nextBytes > this.maxBatchBytes) {
          this.dropped += 1
          this.ctx.logger.warn(`telos tracing record exceeds ${this.maxBatchBytes} bytes and was dropped`)
          continue
        }
        if (operations.length > 0 && operationBytes + nextBytes > this.maxBatchBytes) {
          this.queue.unshift(item)
          break
        }
        items.push(item)
        operations.push(...next)
        operationBytes += nextBytes
      }
      if (operations.length === 0) continue
      try {
        await this.post(operations, Math.min(this.requestTimeoutMs, Math.max(1, deadline - Date.now())))
      } catch (error) {
        if (!this.closed) {
          if (this.queue.length >= this.queueSize) {
            this.dropped += items.length || 1
            this.ctx.logger.warn(`telos tracing batch failed and the queue is full; batch dropped: ${String(error)}`)
          } else {
            this.queue.unshift({ operations })
            this.ctx.logger.warn(`telos tracing batch failed; will retry: ${String(error)}`)
            this.retryPending = true
          }
        } else {
          this.dropped += items.length || 1
          this.ctx.logger.warn(`telos tracing shutdown dropped a batch: ${String(error)}`)
        }
        return
      }
    }
  }

  async post(operations, timeoutMs) {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), timeoutMs)
    try {
      const response = await fetch(this.endpoint, {
        method: 'POST',
        headers: {
          authorization: `Bearer ${this.token}`,
          'content-type': 'application/json',
        },
        body: JSON.stringify({ schema_version: 1, operations }),
        signal: controller.signal,
      })
      await response.arrayBuffer()
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
    } finally {
      clearTimeout(timer)
    }
  }

  async shutdown() {
    if (this.closed) return
    if (this.retryTimer) clearTimeout(this.retryTimer)
    this.retryTimer = undefined
    if (this.pending) await this.pending
    if (this.retryTimer) clearTimeout(this.retryTimer)
    this.retryTimer = undefined
    const time = Date.now()
    for (const sessionId of this.sessions.keys()) {
      this.queue.push({ record: {
        channel: 'ops', time, severity: 'info',
        attributes: { 'session.id': sessionId, 'telemetry.op': 'shutdown' },
        body: {},
      } })
    }
    this.closed = true
    const deadline = Date.now() + this.shutdownTimeoutMs
    await this.drain(deadline)
    if (this.queue.length > 0) {
      this.dropped += this.queue.length
      this.queue.length = 0
      this.ctx.logger.warn(`telos tracing shutdown deadline dropped ${this.dropped} record(s)`)
    }
  }
}

/** Small fixture seam: tests the same mapping used by the live backend. */
export function mapRecordsForTest(records) {
  const sessions = new Map()
  return records.flatMap(record => mapRecord(record, sessions))
}

export default TelosSessionTelemetryBackend
