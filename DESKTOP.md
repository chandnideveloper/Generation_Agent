# Opening the generated report in Power BI Desktop

## The short version

```bash
curl -s -X POST http://127.0.0.1:5055/generate \
  -H "Content-Type: application/json" \
  --data-binary '{
    "run_id": "<your run>",
    "target": "powerbi_desktop",
    "offline_sample_data": true,
    "include_artifacts": false
  }'
```

Then open the `.pbip` file from the reported `output_path`.

**Two preview features must be enabled first, or Desktop will not open it at
all** — see below.

---

## 1. Enable the two preview features

The generated folder is a **PBIP project using the PBIR report format**. Both
are preview features in Power BI Desktop and both are off by default:

> **File → Options and settings → Options → Preview features**
> - ☑ **Power BI Project (.pbip) save option**
> - ☑ **Store reports using enhanced metadata format (PBIR)**

**Restart Desktop** after enabling them.

Without the first, Desktop cannot open a `.pbip` at all. Without the second,
Desktop expects a single `report.json` and will not understand the
`definition/pages/<page>/visuals/<visual>/visual.json` tree this agent writes.

Use a recent Desktop build — the PBIR format was still moving through preview,
and older builds will not read it.

---

## 2. Why you need `offline_sample_data`

Every generated table normally points at the real Qlik source. For the
FleetVision app that is Amazon Redshift:

```m
let
    Source = AmazonRedshift.Database("fleetvision-wg….redshift-serverless.amazonaws.com:5439", "dev"),
    Result = Value.NativeQuery(Source, "SELECT * FROM fleetvision.loads", null, [EnableFolding=false])
in
    Result
```

I checked the last build: **12 of 12 tables need a live connection**. Open
that without Redshift credentials and every table fails to load, so the report
renders blank and there is nothing to review.

`"offline_sample_data": true` swaps each partition for inline rows with the
same column names and types:

```m
let
    Source = Table.FromRows({{"Sample A", …, #datetime(2024,1,1,0,0,0), 10.5}, …},
                            {"customer_id", "customer_name", …}),
    Typed = Table.TransformColumnTypes(Source, {{"customer_id", text}, …})
in
    Typed
```

Verified on the last run: **0 external connections, 12 self-contained tables.**
Desktop opens it with no prompt, builds the model, and draws every page.

| Mode | Use it for |
|---|---|
| `offline_sample_data: true` | Reviewing layout, visuals, measures, relationships |
| *(default)* | The real migration — point at the actual source and enter credentials |

Three rows per table, with distinct values per row, so relationship
cardinality resolves correctly rather than collapsing to many-to-many.

---

## 3. What you will see

**Renders correctly**

- One page per Qlik sheet, in order
- Every visual at its scaled Qlik position (the Qlik grid mapped onto
  1280×720)
- Visual titles
- Native visual types — card, bar, line, pie, treemap, gauge, table, slicer
- The full semantic model: tables, columns with types and format strings,
  measures, relationships with correct direction

**Does not render — and why**

| Missing | Reason |
|---|---|
| Top-N filters | Dropped by the parsing agent — `limitationsSummary` is not in the `example.json` contract |
| Visual-level filters | Present in the mapping payload (20 of 57 visuals); the generator counts them but does not emit them |
| Sorting | Same — available upstream, not consumed |
| Colours / conditional formatting | Same |
| Bookmarks, navigation buttons | Not built |
| Themes | A base theme name only |
| RLS roles | Dropped by the parsing agent |

So this is a **structural** conversion: the right visuals, in the right
places, bound to the right fields, on a working model. It is not yet a
pixel-faithful reproduction.

**Substituted visuals** carry a note in the response. A Qlik `boxplot` becomes
a column chart; anything with no equivalent becomes a textbox that states, on
the canvas, what it was and what to install.

---

## 4. If a measure shows blank

Measures the converter could not translate are written as `BLANK()` with the
original Qlik preserved directly above:

```tmdl
	/// TODO (Qlik AGGR() has no DAX equivalent): Rebuild with SUMMARIZE/ADDCOLUMNS…
	/// Original Qlik: Max( Aggr( SUM('Loads'[revenue]), 'Customers'[customer_name] ))
	measure 'Customer Analytics' = BLANK()
```

This is deliberate: one invalid expression stops Desktop from opening the
**entire** model, so the bad one is neutralised and everything else still
works. After the recent converter fixes this count is **0 of 29** on the test
app, but it will reappear on apps using constructs the converter has not
learned yet. Check `summary.semantic_model.dax_problems` in the response.

---

## 5. Editing and saving

Once open, Desktop treats it as a normal project. Saving writes the same
folder structure back, so you can commit the result. Changing a visual by hand
and re-saving is the fastest way to see what the correct PBIR JSON for that
visual should look like — useful if you want to extend the generator.

---

## 6. If it still will not open

1. **Confirm both preview flags are on and Desktop was restarted.** This is by
   far the most common cause.
2. **Check the folder layout** — the `.pbip` and both `.Report` / `.SemanticModel`
   folders must sit side by side. `definition.pbir` points at the model by
   relative path (`../<App>.SemanticModel`), so moving one folder breaks it.
3. **Try `semantic_model_only`.** If the model opens and the report does not,
   the problem is in the PBIR report JSON, not the model.
4. **Compare against a known-good project.** Save any Desktop report as
   `.pbip` with PBIR on and diff its `visual.json` against a generated one.

### Schema versions

The report schema versions are no longer inferred. They were taken from a
working Power BI project and are pinned in `app/report/pbir_schemas.py`:

| File | Schema version |
|---|---|
| `report.json` | report **3.2.0** |
| `page.json` | page **2.0.0** |
| `visual.json` | visualContainer **2.9.0** |
| `pages.json` | pagesMetadata 1.0.0 |
| `version.json` | versionMetadata 1.0.0, declaring `"version": "2.0.0"` |

An earlier build emitted 1.0.0 for the first three and shipped no
`version.json` at all, which is why nothing opened. `app/package/validator.py`
now checks every one of these on each generate, and the result is returned as
`validation` in the response.
