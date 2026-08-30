# Qlik Generation Agent

Turns a mapping-agent Contract 2.0 payload into Power BI artifacts — a TMDL
semantic model and a PBIR report — then optionally deploys them.

**No LLM is involved.** Generation is a deterministic transform, so the same
mapping payload always produces byte-identical files and git diffs stay
meaningful. The previous version required Azure OpenAI credentials at import
and refused to start without them.

## Targets

| `target` | What you get | Opens in |
|---|---|---|
| `powerbi_desktop` | Full PBIP folder + `.pbip` pointer | Power BI Desktop, and Fabric via Git |
| `fabric` | Same artifacts as base64 item definitions | Fabric workspace via the items API |
| `semantic_model_only` | TMDL model, no report | Desktop / Fabric |

`powerbi_desktop` and `fabric` produce **identical artifacts** — only the
packaging differs. There is one generator, two envelopes.

## Deployment

| `deploy` | Needs |
|---|---|
| `none` (default) | — |
| `fabric` | `workspace_id` + `fabric_access_token` in the request |
| `github` | `GITHUB_PAT`, `GITHUB_ORG`, `GITHUB_REPO` |
| `devops` | `AZURE_DEVOPS_PAT`, `AZURE_ORGANIZATION`, `AZURE_PROJECT`, `AZURE_REPO` |

Fabric deploys the semantic model first, then rewrites the report's
`definition.pbir` to bind to the created dataset id — on disk the report points
at a relative path, which is meaningless inside a workspace. Re-running
updates the existing items instead of duplicating them.

GitHub and DevOps both commit the whole package atomically (blobs → tree →
commit → ref; a single push respectively), so a failure never leaves a
half-written tree.

**A deployment failure never discards the package.** It is built and written
first; the deployment result is reported separately in `deployment`.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/generate` | Build and optionally deploy. `/tmdl` is a legacy alias. |
| GET | `/visual-support` | Which Qlik objects convert natively, which are substituted, which need manual work. |
| GET | `/health` | Service, deploy readiness, mapping source. |

```jsonc
POST /generate
{
  "run_id": "…",                 // or app_id, or an inline mapping_result
  "target": "powerbi_desktop",
  "deploy": "none"
}
```

## Output

```
<App>.pbip                                    ← Desktop opens this
<App>.SemanticModel/
  definition/model.tmdl                       model header + table refs
  definition/database.tmdl                    compatibility level
  definition/relationships.tmdl
  definition/tables/<T>.tmdl                  columns, measures, partition
  definition/cultures/en-US.tmdl
  definition.pbism  .platform
<App>.Report/
  definition/report.json
  definition/pages/pages.json
  definition/pages/<page>/page.json
  definition/pages/<page>/visuals/<v>/visual.json
  definition.pbir  .platform
```

Measured on FleetVision KSA: 76 files, 12 tables, 29 measures, 15
relationships, 5 pages, 57 visuals.

## Visuals with no Power BI equivalent

Every visual is emitted — nothing is dropped — and anything that could not map
cleanly is reported in `visual_notes` with a concrete next step:

| Severity | Meaning |
|---|---|
| `native` | Direct equivalent; no note. |
| `substituted` | Close native visual carries the intent; note says what changed. |
| `manual` | Nothing native reproduces it; a textbox placeholder is emitted **on the canvas** naming the object and what to install. |

For example, a Qlik `boxplot` becomes a `columnChart` with:

> Power BI has no native box plot. Rendered as a column chart of the median.
> For true quartiles install the 'Box and Whisker chart' visual from
> AppSource, or add DAX measures for P25/P50/P75.

`GET /visual-support` returns the whole catalog.

## Measures that are not valid DAX

The mapping agent's converter handles common aggregations but passes Qlik
constructs through unchanged — `Aggr(…)`, set analysis `{<…>}`, `Num(…)`,
`$(vVar)`, `ApplyMap(…)`. **A single one of those stops Power BI from opening
the entire model.**

So every expression is validated before it is written. One that fails is
emitted as `BLANK()` with the original preserved above it:

```tmdl
	/// TODO (Qlik AGGR() has no DAX equivalent): Rebuild with SUMMARIZE/ADDCOLUMNS
	///   and an iterator, e.g. MAXX(SUMMARIZE(Table, Table[Key], "v", SUM(…)), [v]).
	/// Original Qlik: Max( Aggr( SUM('Loads'[revenue]), 'Customers'[customer_name] ))
	measure 'Customer Analytics' = BLANK()
```

The model opens, every other measure works, and nothing is lost. On the test
app **21 of 29 measures** hit this — the count is reported as
`summary.semantic_model.dax_needs_rewrite`, with per-measure detail in
`dax_problems`.

Relationships whose columns do not exist in the generated tables are skipped
and listed in `relationships_skipped`, for the same reason: emitting
`fromColumn: ''.key` produces a model Desktop refuses to load.

## Layout

```
main.py                       FastAPI entry
app/config.py  schemas.py  generator.py
app/sources/mapping_client.py fetch the mapping result
app/model/                    datatypes, table, measure, relationship, dax_guard,
                              semantic_model
app/report/                   visual_catalog, visual_builder, layout, report_writer
app/package/                  pbip_packager, fabric_packager
app/deploy/                   fabric, github, devops
app/util/                     ids, payload, logging
tests/
```

Every module is under 200 lines. The previous single `tmdl_generator.py` was
3,882.

The original implementation is preserved under `legacy/` for reference.

## Running

```bash
pip install -r requirements.txt
cp .env.example .env
python main.py           # http://127.0.0.1:5000
```

```bash
uvicorn main:app --host 0.0.0.0 --port 5000
```

## Tests

```bash
python -m pytest tests/ -q
```

46 tests, no network. They assert what actually breaks Power BI: illegal
`dataType` values, empty partition sources, `''.` relationship endpoints,
Qlik syntax leaking into DAX, visuals falling off the canvas, and unbound
field references.
