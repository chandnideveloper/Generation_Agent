# Testing the Generation Agent

Every command below was run against the live service and its real output is
shown. Ready-made request bodies live in `tests/payloads/`.

## Start it

```bash
cd az-wa-repo-generationagent
pip install -r requirements.txt
python main.py                 # http://127.0.0.1:5000
```

Port 5000 is often already taken by another agent on this machine. Override it:

```bash
PORT=5055 python main.py
```

> If you change `.env`, **restart the process** — settings load once at import.
> A stale process keeps the old values and `pkill -f "python main.py"` does not
> always kill it on Windows. Check with `netstat -ano | grep :5055` and
> `Stop-Process -Id <pid> -Force`.

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Service state, deploy readiness, mapping source |
| GET | `/visual-support` | Full visual catalog: native / substituted / manual |
| POST | `/generate` | Build and optionally deploy |
| POST | `/tmdl` | Legacy alias for `/generate` |

---

## 1. Health

```bash
curl -s http://127.0.0.1:5055/health
```

```json
{
  "status": "healthy",
  "service": "qlik-generation-agent",
  "targets": ["powerbi_desktop", "fabric", "semantic_model_only"],
  "deploy": { "fabric": "per-request token", "github": false, "devops": true },
  "mapping_api": "https://mongo-db-k15s.onrender.com",
  "output_dir": "generated"
}
```

`github`/`devops` report whether their credentials are configured. Confirm
`mapping_api` is the MongoDB microservice — if it shows `127.0.0.1:8008` the
process picked up `BASE_API_URL` because `MONGO_API_URL` was unset.

---

## 2. Which visuals convert

```bash
curl -s http://127.0.0.1:5055/visual-support
```

Returns three buckets: `native` (direct equivalent), `substituted` (mapped to a
close visual, with a reason and a suggestion) and `manual` (placeholder plus
instructions).

---

## 3. Generate

`POST /generate`. Supply the mapping **either** inline or by identifier.

| Field | Required | Notes |
|---|---|---|
| `mapping_result` | one of these three | Inline Contract 2.0 payload |
| `run_id` | | Fetched from `/mapping/by-run/{run_id}` |
| `app_id` | | Fetched from `/mapping/by-app/{app_id}` |
| `target` | no | `powerbi_desktop` (default) \| `fabric` \| `semantic_model_only` |
| `deploy` | no | `none` (default) \| `fabric` \| `github` \| `devops` |
| `app_name` | no | Overrides the name from the mapping payload |
| `write_to_disk` | no | Always true for `powerbi_desktop` |
| `workspace_id`, `fabric_access_token` | for `deploy: fabric` | |
| `branch`, `repo`, `commit_message`, `department_repo`, `folder_name` | for git deploys | |

### 3a. Desktop (PBIP on disk)

```bash
curl -s -X POST http://127.0.0.1:5055/generate \
  -H "Content-Type: application/json" \
  -d @tests/payloads/request_desktop.json
```

```
status  : success
message : Generated 2 tables, 2 measures, 1 relationships, 1 pages, 3 visuals;
          needs review: 1 visual(s) substituted, 1 measure(s) need a DAX rewrite.
files   : 18
output  : generated/sales-demo/demo-run-001
```

### 3b. Fabric artifacts (no disk write)

```bash
curl -s -X POST http://127.0.0.1:5055/generate \
  -H "Content-Type: application/json" \
  -d @tests/payloads/request_fabric.json
```

Identical 18 files — only the packaging differs.

### 3c. Semantic model only

```bash
curl -s -X POST http://127.0.0.1:5055/generate \
  -H "Content-Type: application/json" \
  -d @tests/payloads/request_model_only.json
```

9 files, no report, no visual notes.

### 3d. By identifier, fetched from the mapping store

```bash
curl -s -X POST http://127.0.0.1:5055/generate \
  -H "Content-Type: application/json" \
  --data-binary '{"run_id":"demo-run-001","target":"fabric","write_to_disk":false}'
```

```
status: success | app_name: Sales Demo | files: 18
```

`app_id` works the same way. `by-run` is tried first — a run pins one specific
mapping, whereas `by-app` may return an older one.

To seed the store for this test:

```bash
curl -s -X POST https://mongo-db-k15s.onrender.com/mapping \
  -H "Content-Type: application/json" \
  -d '{"folder_name":"Sales Demo","app_id":"demo-app-001","space_id":"personal",
       "app_name":"Sales Demo","run_id":"demo-run-001",
       "mapping_result": <contents of tests/payloads/minimal_mapping.json>}'
```

### 3e. Deploy to Fabric

```bash
curl -s -X POST http://127.0.0.1:5055/generate \
  -H "Content-Type: application/json" \
  --data-binary '{
    "run_id": "demo-run-001",
    "target": "fabric",
    "deploy": "fabric",
    "workspace_id": "<fabric workspace guid>",
    "fabric_access_token": "<AAD token for api.fabric.microsoft.com>"
  }'
```

Creates the semantic model first, then the report bound to its id. Re-running
updates both instead of duplicating. **Not exercised against a live workspace
here** — it needs a real token; the ordering and binding logic are covered by
tests.

### 3f. Deploy to GitHub / DevOps

```bash
curl -s -X POST http://127.0.0.1:5055/generate \
  -H "Content-Type: application/json" \
  --data-binary '{"run_id":"demo-run-001","target":"fabric","deploy":"github",
                  "branch":"main","commit_message":"Generate Sales Demo"}'
```

Credentials come from the environment (`GITHUB_PAT`/`GITHUB_ORG`/`GITHUB_REPO`,
or `AZURE_DEVOPS_PAT`/`AZURE_ORGANIZATION`/`AZURE_PROJECT`/`AZURE_REPO`).

**A deployment failure never discards the package** — it is built and written
first, and the failure is reported separately:

```json
"deployment": { "status": "error", "provider": "github",
                "error": "GitHub deployment needs GITHUB_PAT, GITHUB_ORG and GITHUB_REPO" }
```

---

## Getting the generated files back in the response

By default `/generate` embeds **every generated file** in the response under
`artifacts`. TMDL comes back as readable text (it is not JSON); every
`.json` / `.pbip` / `.pbism` / `.pbir` / `.platform` file is parsed so it
arrives as a real object you can index into, not an escaped string.

| Flag | Response carries | Size on the demo app |
|---|---|---|
| *(default)* | `artifacts` — full contents | 11,627 bytes |
| `"index_only": true` | `artifact_index` — path + byte size per file | 2,340 bytes |
| `"include_artifacts": false` | summary and counts only | 1,390 bytes |

```bash
curl -s -X POST http://127.0.0.1:5057/generate   -H "Content-Type: application/json"   -d @tests/payloads/request_full_artifacts.json
```

Ready-made bodies: `request_full_artifacts.json`,
`request_index_only_artifacts.json`, `request_summary_artifacts.json`.
A complete captured reply is saved at `tests/payloads/sample_response_full.json`.

### Shape

```jsonc
{
  "status": "success",
  "message": "Generated 2 tables, 2 measures, …",
  "file_count": 18,
  "total_bytes": 12286,
  "summary": { "semantic_model": {…}, "report": {…} },
  "visual_notes": [ … ],
  "deployment": { … },

  "artifacts": {
    "pbip": { "$schema": "…", "version": "1.0", "artifacts": [ … ] },

    "semantic_model": {
      "definition/model.tmdl":         "<TMDL text>",
      "definition/database.tmdl":      "<TMDL text>",
      "definition/relationships.tmdl": "<TMDL text>",
      "definition/tables/Sales.tmdl":  "<TMDL text>",
      "definition/cultures/en-US.tmdl":"<TMDL text>",
      "definition.pbism":              { "version": "4.0", "settings": {…} },
      ".platform":                     { "metadata": {…}, "config": {…} }
    },

    "report": {
      "definition/report.json":        { "$schema": "…", "themeCollection": {…} },
      "definition/pages/pages.json":   { "pageOrder": ["overview"],
                                         "activePageName": "overview" },
      "definition/pages/overview/page.json": { "displayName": "Overview",
                                               "height": 720, "width": 1280 },
      "definition/pages/overview/visuals/bar1/visual.json": {
        "name": "bar1",
        "position": { "x": 320, "y": 0, "width": 640, "height": 360, "z": 1 },
        "visual": {
          "visualType": "barChart",
          "query": { "queryState": { "Category": { "projections": [ … ] },
                                     "Y":        { "projections": [ … ] } } }
        }
      }
    },

    "other": { ".gitignore": "<text>" }
  }
}
```

TMDL values are the literal file text, newlines and tabs included — write one
straight to a `.tmdl` file and it is byte-identical to what lands on disk.

Paths are **item-relative** — the `<App>.SemanticModel/` and `<App>.Report/`
prefixes are stripped, because that is the form both Fabric's items API and a
Git commit expect.

### Reading it

```python
r = requests.post(f"{base}/generate", json=body).json()
model  = r["artifacts"]["semantic_model"]
report = r["artifacts"]["report"]

print(model["definition/tables/Sales.tmdl"])          # text
print(report["definition/pages/pages.json"]["pageOrder"])   # object

for path, visual in report.items():
    if path.endswith("visual.json"):
        print(visual["name"], visual["visual"]["visualType"])
```

Use `index_only` when you only need to know what was produced — a large app
returns ~150 KB of artifacts, which is fine for a service call but noisy in a
terminal.

---

## Response shape

```jsonc
{
  "status": "success",
  "message": "Generated 2 tables, 2 measures, … needs review: …",
  "target": "powerbi_desktop",
  "deploy": "none",
  "app_name": "Sales Demo",
  "run_id": "demo-run-001",
  "output_path": "…/generated/sales-demo/demo-run-001",
  "file_count": 18,
  "summary": {
    "semantic_model": {
      "tables": 2, "measures": 2, "calculated_columns": 1,
      "relationships_written": 1, "relationships_skipped": [],
      "dax_needs_rewrite": 1,
      "dax_problems": [{ "name": "Revenue High Band", "original": "…",
                         "reason": "Qlik set analysis {<...>} has no DAX equivalent",
                         "suggestion": "Rewrite as CALCULATE(…)" }]
    },
    "report": { "pages": 1, "visuals": 3, "native": 2, "substituted": 1, "manual": 0 }
  },
  "visual_notes": [{ "qlik_type": "boxplot", "mapped_to": "columnChart",
                     "severity": "substituted", "reason": "…", "suggestion": "…" }],
  "deployment": { "status": "skipped", "reason": "no deployment requested" }
}
```

The two fields to watch: **`dax_problems`** (measures that need a hand-written
rewrite) and **`visual_notes`** (visuals that were substituted or placeheld).

---

## Error cases

| Request | Status |
|---|---|
| `{}` — no identifier and no mapping | `400` |
| `{"run_id": "does-not-exist"}` | `404` |
| Mapping store unreachable | `502` |
| Malformed mapping payload | `500` |

Verified:

```
no identifier      -> HTTP 400
unknown run_id     -> HTTP 404
legacy /tmdl alias -> HTTP 200
```

---

## Unit tests

```bash
python -m pytest tests/ -q      # 46 passed
```

No network and no disk. They assert what actually breaks Power BI: illegal
`dataType` values, empty partition sources, `''.` relationship endpoints, Qlik
syntax leaking into DAX, visuals off the canvas, and unbound field references.

---

## What the demo payload deliberately exercises

`tests/payloads/minimal_mapping.json` — 2 tables, 1 relationship, 2 measures,
1 calculated dimension, 3 visuals — is built so the interesting paths fire:

| Included | Proves |
|---|---|
| `Total Revenue` = `SUM('Sales'[revenue])` | Valid DAX passes through untouched |
| `Revenue High Band` = `SUM({<…>} …)` | Set analysis is caught, becomes `BLANK()` with the original in a `///` comment |
| `Region Label` calculated dimension | Becomes a DAX calculated column |
| `many-to-one` relationship | Correct `fromColumn`/`toColumn` direction |
| `kpi` + `barchart` visuals | Native mapping, field binding to model entities |
| `boxplot` visual | Substitution with an AppSource suggestion |

Swap in a real mapping result to scale up — the FleetVision app produces 76
files, 5 pages and 57 visuals.
