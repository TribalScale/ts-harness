# Work orders

## WO-501 Expose the app version on the health endpoint
| Field | Value |
|---|---|
| Depends on | None |

### Summary
feat: The home endpoint (`HomeController`, `HomeService.appInfo`) keeps its current response and adds a `version` field read from `package.json`.

### Requirements
- REQ-1: The response includes `version` equal to the `version` in `package.json`.
- REQ-2: A unit test and an e2e test cover the field.

### Files
- src/home/home.service.ts
- src/home/home.service.spec.ts
- test/user/home.e2e-spec.ts

## WO-502 Document the version field
| Field | Value |
|---|---|
| Depends on | WO-501 |

### Summary
docs: Mention the `version` field of the home endpoint in the README.

### Files
- README.md
