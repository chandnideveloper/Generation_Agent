# Testing the Generation API in Postman

Every request below was run against the live service. The output shown is real.

## Setup

Start the service:

```bash
cd az-wa-repo-generationagent
PORT=5061 python main.py
```

Base URL: `http://127.0.0.1:5061`

> Port 5000 is already taken on this machine by another agent, which is why
> these examples use 5061. Use whatever port you started on.

**No authentication.** No headers needed beyond `Content-Type: application/json`.

---

## The endpoint you want

```
POST http://127.0.0.1:5061/generate
Content-Type: application/json
```

`POST /tmdl` is a legacy alias for the same handler — use `/generate`.

---

## The payload — start here

This is the one to paste into Postman first:

```json
{
  "run_id": "pm-test-001",
  "target": "powerbi_desktop",
  "offline_sample_data": true,
  "include_artifacts": false
}
```

**Verified result:**

```
status : success
valid  : true    (0 errors)
files  : 107
message: Generated 12 tables, 29 measures, 15 relationships, 5 pages, 57 visuals;
         needs review: 2 visual(s) substituted, 1 measure(s) need a DAX rewrite
output : generated/fleetvision-ksa/pm-test-001
```

`pm-test-001` is a real FleetVision KSA mapping already seeded in the store, so
this works as-is.

---

## Field reference

**You must supply exactly one of these three** — this is the only hard rule:

| Field | Meaning |
|---|---|
| `run_id` | Fetches from `GET {mongo}/mapping/by-run/{run_id}` — **preferred** |
| `app_id` | Fetches from `GET {mongo}/mapping/by-app/{app_id}` — may return an older run |
| `mapping_result` | The whole Contract 2.0 mapping payload inline |

Everything else is optional:

| Field | Values | Default | Notes |
|---|---|---|---|
| `target` | `powerbi_desktop` · `fabric` · `semantic_model_only` | `powerbi_desktop` | |
| `deploy` | `none` · `fabric` · `github` · `devops` | `none` | |
| `offline_sample_data` | bool | `false` | **Set `true` to open in Desktop without database credentials** |
| `include_artifacts` | bool | `true` | Embeds every generated file in the response |
| `index_only` | bool | `false` | Returns a path + size listing instead of contents |
| `write_to_disk` | bool | `true` | Always true for `powerbi_desktop` |
| `app_name` | string | from the payload | Overrides the folder/item name |
| `workspace_id`, `fabric_access_token` | string | — | Required for `deploy: fabric` |
| `branch`, `repo`, `commit_message`, `department_repo`, `folder_name` | string | — | Git deploys |

---

## Verified request/response pairs

### 1. Desktop, openable offline

```json
{ "run_id": "pm-test-001", "target": "powerbi_desktop",
  "offline_sample_data": true, "include_artifacts": false }
```
→ `107 files`, written to disk, no connection needed.

### 2. Fabric artifacts, nothing written to disk

```json
{ "run_id": "pm-test-001", "target": "fabric",
  "write_to_disk": false, "include_artifacts": false }
```
→ `108 files`. One more than above: the real connection adds
`definition/expressions.tmdl`, which offline mode omits.

### 3. Semantic model only

```json
{ "run_id": "pm-test-001", "target": "semantic_model_only",
  "write_to_disk": false, "include_artifacts": false }
```
→ `23 files`, no report.

### 4. By `app_id` instead of `run_id`

```json
{ "app_id": "a4bbab64-3d8f-49de-8fcb-296c021b0ffa",
  "target": "powerbi_desktop", "include_artifacts": false }
```
→ `116 files`. **Note the different count** — `by-app` returned an older,
larger mapping (17 tables). Prefer `run_id` when you care which one you get.

### 5. Full file contents in the response

```json
{ "run_id": "pm-test-001", "target": "fabric", "write_to_disk": false }
```
→ adds `artifacts`: `{ pbip, semantic_model{}, report{}, other{} }`. TMDL comes
back as text, every `.json` as a real object.

### 6. Just the file listing

```json
{ "run_id": "pm-test-001", "target": "fabric",
  "index_only": true, "write_to_disk": false }
```
→ adds `artifact_index` — path + byte size per file.

### 7. Deploy to Fabric

```json
{ "run_id": "pm-test-001", "target": "fabric", "deploy": "fabric",
  "workspace_id": "<workspace guid>",
  "fabric_access_token": "<AAD token for api.fabric.microsoft.com>" }
```

Creates the semantic model first, then the report bound to its id. Re-running
updates rather than duplicating. **Not exercised against a live workspace** —
it needs a real token.

### 8. Deploy to Git

```json
{ "run_id": "pm-test-001", "target": "fabric", "deploy": "github",
  "branch": "main", "commit_message": "Generate FleetVision" }
```

Credentials come from the environment. Verified failure behaviour when they
are absent:

```json
"deployment": { "status": "error", "provider": "github",
  "error": "GitHub deployment needs GITHUB_PAT, GITHUB_ORG and GITHUB_REPO" }
```

The package is still built and `status` is still `success` — a deployment
failure never discards it.

---

## What to check in the response

Two fields tell you whether the output is usable:

```json
"validation": { "ok": true, "errors": [], "warnings": [] }
```

`ok: false` means the package will not open — every error names the file and
the problem. This runs on every generate.

```json
"summary": {
  "semantic_model": { "tables": 12, "measures": 29, "relationships_written": 15,
                      "dax_needs_rewrite": 1, "dax_problems": [ … ] },
  "report": { "pages": 5, "visuals": 57, "native": 55, "substituted": 2,
              "filters_applied": 10, "navigation_buttons": 5 }
}
```

- `dax_problems` — measures written as `BLANK()` because they still contain
  Qlik syntax, each with the original expression and a suggested rewrite.
- `visual_notes` — visuals that were substituted or placeheld, with a reason
  and a concrete action.

---

## Error responses

| Request | Status | Meaning |
|---|---|---|
| `{}` | **400** | No `run_id`, `app_id` or `mapping_result` |
| `{"run_id": "nope"}` | **404** | Nothing in the mapping store for that id |
| — | **502** | Mapping store unreachable |
| — | **500** | Mapping payload could not be processed |

---

## Other endpoints

```
GET /health          service state, deploy readiness, mapping source
GET /visual-support  which Qlik visuals convert natively, are substituted, or need work
```

`/health` is worth checking first — if `mapping_api` shows `127.0.0.1:8008`
instead of the MongoDB service, `MONGO_API_URL` was not picked up and every
`run_id` lookup will 404.

---

## Seeding your own test data

To test with a different app, store its mapping first:

```
POST https://mongo-db-k15s.onrender.com/mapping
Content-Type: application/json

{
  "folder_name": "My App",
  "app_id":   "<qlik app id>",
  "space_id": "personal",
  "app_name": "My App",
  "run_id":   "my-run-001",
  "mapping_result": { …Contract 2.0 payload from the mapping agent… }
}
```

Then `{"run_id": "my-run-001", "target": "powerbi_desktop"}`.

Or skip the store entirely and pass `mapping_result` inline — see
`tests/payloads/request_desktop.json` for a complete self-contained body.
