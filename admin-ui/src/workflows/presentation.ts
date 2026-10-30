type Schema = Record<string, unknown>;

function asSchema(value: unknown): Schema | null {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Schema) : null;
}

/** Dotted paths of every string property in a JSON schema, including nested objects and anyOf/oneOf branches. */
export function stringFieldPaths(schema: unknown, prefix = ""): string[] {
  const node = asSchema(schema);
  if (!node) return [];
  const out: string[] = [];
  const properties = asSchema(node.properties) || {};
  for (const [name, raw] of Object.entries(properties)) {
    const prop = asSchema(raw);
    if (!prop) continue;
    if (prop.type === "string") out.push(`${prefix}${name}`);
    else if (prop.type === "object") out.push(...stringFieldPaths(prop, `${prefix}${name}.`));
  }
  for (const key of ["anyOf", "oneOf"]) {
    const branches = Array.isArray(node[key]) ? (node[key] as unknown[]) : [];
    for (const branch of branches) out.push(...stringFieldPaths(branch, prefix));
  }
  return Array.from(new Set(out));
}
