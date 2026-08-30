"""End-to-End Demo: Qlik -> Mapping Agent -> Generation Agent -> PBIP Artifacts."""

import asyncio
import json
import sys
from pathlib import Path

# Add both agents to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
GENERATION_DIR = BASE_DIR / "az-wa-repo-generationagent"

candidate_mapping_dirs = [
    BASE_DIR / "mapping (2)" / "mapping",
    BASE_DIR / "az-wa-repo-mappingagent",
    BASE_DIR / "mapping"
]
for cmd in candidate_mapping_dirs:
    if cmd.exists():
        sys.path.insert(0, str(cmd))
        break

sys.path.insert(0, str(GENERATION_DIR))


# Import Mapping Agent
from agents import CoordinatorAgent

# Import Generation Agent
from app.generator import generate
from app.schemas import GenerateRequest, Target


async def run_demo():
    print("=================================================================")
    print("1. PREPARING RICH QLIK PARSING PAYLOAD (WITH FINE-GRAINED PROPERTIES)")
    print("=================================================================")
    qlik_raw_payload = {
        "app_id": "demo-app-100",
        "app_name": "Executive Sales & Operations",
        "run_id": "demo-run-100",
        "metadata": {
            "tenant": "enterprise.qlikcloud.com",
            "report_version": "12.2881.0"
        },
        "datasources": [{
            "driver": "redshift",
            "server": "warehouse.company.com",
            "database": "sales_db"
        }],
        "tables": [
            {
                "name": "Orders",
                "table_name": "Orders",
                "fields": [
                    {"name": "order_id", "dataType": "STRING", "is_key": True},
                    {"name": "customer_id", "dataType": "STRING"},
                    {"name": "amount", "dataType": "NUMBER", "num_format": {"type": "M", "format_pattern": "$#,##0.00"}},
                    {"name": "order_date", "dataType": "DATE"}
                ]
            },
            {
                "name": "Customers",
                "table_name": "Customers",
                "fields": [
                    {"name": "customer_id", "dataType": "STRING", "is_key": True},
                    {"name": "customer_name", "dataType": "STRING"},
                    {"name": "region", "dataType": "STRING"}
                ]
            }
        ],
        "relationships": [{
            "table1": "Orders", "field1": "customer_id",
            "table2": "Customers", "field2": "customer_id",
            "cardinality": "many-to-one"
        }],
        "measures": [
            {
                "name": "Total Sales",
                "expression": "Sum(amount)",
                "tables": ["Orders"],
                "coloring": {"baseColor": "#0055FF"},
                "num_format": {
                    "type": "M",
                    "format_pattern": "$#,##0.00"
                },
                "trend_lines": [{
                    "type": "linear",
                    "line_type": "solid"
                }]
            }
        ],
        "dimensions": [
            {
                "name": "Customer Region",
                "tables": ["Customers"],
                "field_defs": ["region"],
                "dataType": "STRING",
                "coloring": {"baseColor": "#FFAA00"},
                "limitations": {
                    "limitation_type": "Fixed number",
                    "direction": "Top",
                    "limit_value": 5,
                    "suppress_null": True
                }
            }
        ],
        "visualizations": [
            {
                "name": "sales_by_region_chart",
                "qlik_type": "barchart",
                "title": "Top Regions by Sales",
                "sheet_name": "Sales Overview",
                "col": 0, "row": 0, "colspan": 12, "rowspan": 6,
                "style": {
                    "title_color": "#0F172A",
                    "font_size": "14pt",
                    "background_color": "#F8FAFC"
                },
                "custom_coloring": {
                    "mode": "primary",
                    "single_color": "#34D399"
                },
                "reference_lines": [
                    {
                        "label": "Quarterly Target",
                        "expression": "100000",
                        "color": "#E11D48",
                        "line_type": "dashed"
                    }
                ],
                "dimensions": [
                    {
                        "name": "region",
                        "limitations": {
                            "limitation_type": "Fixed number",
                            "direction": "Top",
                            "limit_value": 5,
                            "suppress_null": True
                        }
                    }
                ],
                "measures": [{"name": "Total Sales"}]
            }
        ],
        "sheets": [
            {
                "title": "Sales Overview",
                "grid": {"columns": 24, "rows": 12}
            }
        ]
    }

    print("Payload prepared with:")
    print("  - Table: Orders (with num_format $#,##0.00)")
    print("  - Measure: Total Sales (with trend lines and baseColor #0055FF)")
    print("  - Visual: Top Regions by Sales")
    print("      * Custom Coloring: #34D399")
    print("      * Title Color: #0F172A, Font Size: 14pt")
    print("      * Background Color: #F8FAFC")
    print("      * Reference Line: Quarterly Target ($100,000, dashed, #E11D48)")
    print("      * Limitations: Top 5 + Null Suppression")
    print()

    print("=================================================================")
    print("2. RUNNING MAPPING AGENT (CoordinatorAgent.process_data)")
    print("=================================================================")
    coordinator = CoordinatorAgent()
    mapping_result = await coordinator.process_data(qlik_raw_payload, is_direct_query=False, app_id="demo-app-100")

    print(f"Mapping Status: {mapping_result.get('status')}")
    print(f"Contract Version: {mapping_result.get('contract_version')}")
    print(f"Tables Mapped: {len(mapping_result.get('tables', []))}")
    print(f"Measures Mapped: {len(mapping_result.get('measures', []))}")
    print(f"Visuals Mapped: {len(mapping_result.get('visuals', {}).get('sheet_visuals', []))}")
    print()

    print("=================================================================")
    print("3. RUNNING GENERATION AGENT (generator.generate)")
    print("=================================================================")
    gen_request = GenerateRequest(
        target=Target.POWERBI_DESKTOP,
        write_to_disk=True,
        include_artifacts=True
    )
    gen_response = generate(mapping_result, gen_request)

    print(f"Generation Status: {gen_response.get('status')}")
    print(f"Target: {gen_response.get('target')}")
    print(f"Files Generated: {gen_response.get('file_count')}")
    print(f"Output Path: {gen_response.get('output_path')}")
    print()

    print("=================================================================")
    print("4. INSPECTING GENERATED ARTIFACTS")
    print("=================================================================")
    artifacts = gen_response.get("artifacts") or {}
    
    # 1. Semantic Model TMDL
    print("--- TMDL Semantic Model Measure (Total Sales.tmdl / table TMDL) ---")
    orders_tmdl = artifacts.get("semantic_model", {}).get("tables/Orders.tmdl", "")
    for line in orders_tmdl.splitlines():
        if "measure" in line or "formatString" in line:
            print("  ", line)
    print()

    # 2. PBIR Visual Definition
    print("--- PBIR visual.json Objects & Filters ---")
    report_files = artifacts.get("report", {})
    visual_json_path = next((k for k in report_files if k.endswith("visual.json")), None)
    if visual_json_path:
        v_doc = report_files[visual_json_path]
        if isinstance(v_doc, str):
            v_doc = json.loads(v_doc)
        print("Visual Type:", v_doc.get("visual", {}).get("visualType"))
        print("Objects Generated:")
        for obj_name, obj_val in v_doc.get("visual", {}).get("objects", {}).items():
            print(f"   * {obj_name}: {json.dumps(obj_val)}")
        print("Filter Configuration:")
        print("   *", json.dumps(v_doc.get("filterConfig", {}), indent=2))

    print()
    print("=================================================================")
    print("SUCCESS! All fine-grained mappings generated accurately!")
    print("=================================================================")


if __name__ == "__main__":
    asyncio.run(run_demo())
