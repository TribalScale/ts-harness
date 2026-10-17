# Context Graph Template

```markdown
# Context Graph

## Work Order Graph

| Work Order | Depends On | Depended On By | Shares Contract With |
|---|---|---|---|
| WO-001 | None | WO-002 | WO-003 |

## Traceability

| Work Order | Epic | Blueprint | FRD |
|---|---|---|---|
| WO-001 | CE-001 | [Blueprint] | FRD-001 |

## Cross-Contract Ownership

| Contract | Owning Work Order | Referencing Work Orders |
|---|---|---|
| [Contract] | WO-001 | WO-002 |
```

## Rules

- Depends-on edges must be mirrored by depended-on-by edges.
- Contract ownership is singular.
- Work Orders with no relationships require explicit justification.
