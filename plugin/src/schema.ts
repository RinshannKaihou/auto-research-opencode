/**
 * Parameter specs carry `required: true` on the property itself, as in the DSH
 * plugin's tool-definition.js. These helpers turn a spec into JSON Schema and
 * validate model input against it before anything reaches the ledger.
 */
export type Spec = {
  type?: "string" | "number" | "boolean" | "array" | "object"
  required?: boolean
  enum?: readonly unknown[]
  description?: string
  items?: Spec
  properties?: Record<string, Spec>
  additionalProperties?: boolean
}

export function jsonSchema(spec: Spec): Record<string, unknown> {
  const { required: _required, properties, items, ...rest } = spec
  const value: Record<string, unknown> = { ...rest }
  if (spec.type === "object") {
    value.properties = Object.fromEntries(
      Object.entries(properties ?? {}).map(([name, child]) => [name, jsonSchema(child)]),
    )
    const required = Object.entries(properties ?? {})
      .filter(([, child]) => child.required)
      .map(([name]) => name)
    if (required.length) value.required = required
  }
  if (spec.type === "array" && items) value.items = jsonSchema(items)
  return value
}

export function parameters(specs: Record<string, Spec>): Record<string, unknown> {
  return jsonSchema({ type: "object", additionalProperties: false, properties: specs })
}

export function validate(value: unknown, spec: Spec, path: string): void {
  if (value === undefined) {
    if (spec.required) throw new Error(`${path} is required`)
    return
  }
  if (spec.type === "string" && typeof value !== "string") throw new Error(`${path} must be text`)
  if (spec.type === "number" && (typeof value !== "number" || !Number.isFinite(value))) {
    throw new Error(`${path} must be a finite number`)
  }
  if (spec.type === "boolean" && typeof value !== "boolean") {
    throw new Error(`${path} must be true or false`)
  }
  if (spec.type === "array") {
    if (!Array.isArray(value)) throw new Error(`${path} must be an array`)
    value.forEach((item, index) => validate(item, spec.items ?? {}, `${path}[${index}]`))
  }
  if (spec.type === "object") {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      throw new Error(`${path} must be an object`)
    }
    for (const [name, child] of Object.entries(spec.properties ?? {})) {
      validate((value as Record<string, unknown>)[name], child, `${path}.${name}`)
    }
  }
  if (spec.enum && !spec.enum.includes(value)) throw new Error(`${path} is not an allowed value`)
}

export function validateArgs(args: unknown, specs: Record<string, Spec>): Record<string, unknown> {
  if (!args || typeof args !== "object" || Array.isArray(args)) {
    throw new Error("tool arguments must be an object")
  }
  for (const [name, spec] of Object.entries(specs)) {
    validate((args as Record<string, unknown>)[name], spec, name)
  }
  return args as Record<string, unknown>
}
