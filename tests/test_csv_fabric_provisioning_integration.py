"""Proves the full generate() flow actually wires CSV->Fabric provisioning
together end-to-end, not just the isolated provisioner unit - a file-based
table's placeholder Supabase URL must be replaced with the real OneLake
location it was just uploaded to, when deploying to Fabric with real
downloaded file content available.
"""

from unittest.mock import patch

from app.generator import generate
from app.model.table_tmdl import supabase_file_url
from app.schemas import Deploy, GenerateRequest, Target


def _mapping_with_csv_table():
    return {
        "tables": [{
            "name": "Loads",
            "columns": [{"qlik_column_name": "id", "fabric_column_name": "id", "fabric_datatype": "int64"}],
            # Mirrors what the upstream mapping stage's connection_mapper.py
            # actually produces for a file-based table - _extract_mquery_from_payload
            # only recognizes a pre-built m_query/mquery/power_query field, not a
            # raw load-script string.
            "m_query": 'let\n    Source = Folder.Files("fleet_loads.csv")\nin\n    Source',
            "connection_details": {"driver": "datafiles", "path": "fleet_loads.csv"},
        }],
        "measures": [], "dimensions": [], "relationships": [],
        "data_files": [{"name": "fleet_loads.csv", "content_text": "id\n1\n2"}],
        "visuals": {"sheet_visuals": []},
    }


def test_fabric_deploy_replaces_supabase_placeholder_with_real_onelake_url():
    mapping = _mapping_with_csv_table()

    with patch(
        "app.deploy.fabric_data_provisioner.provision_data_files",
        return_value={"fleet_loads.csv": "https://onelake.dfs.fabric.microsoft.com/ws-1/lh-1/Files/fleet_loads.csv"},
    ) as mock_provision, \
         patch("app.deploy.fabric_deployer.deploy", return_value={"items": {}}):
        result = generate(
            mapping,
            GenerateRequest(
                target=Target.SEMANTIC_MODEL_ONLY, deploy=Deploy.FABRIC,
                workspace_id="ws-1", fabric_access_token="tok", write_to_disk=False,
            ),
        )

    mock_provision.assert_called_once()
    called_files, called_ws, called_token, called_lakehouse_name = mock_provision.call_args[0]
    assert called_files == mapping["data_files"]
    assert called_ws == "ws-1"
    assert called_token == "tok"

    model_text = " ".join(str(v) for v in result.get("semantic_model", {}).values()) if result.get("semantic_model") else ""
    if not model_text:
        # include_artifacts defaults True and populates result["semantic_model"];
        # if that shape changes, fail loudly rather than silently pass.
        assert "semantic_model" in result, result.keys()
    assert supabase_file_url("fleet_loads.csv") not in model_text
    assert "onelake.dfs.fabric.microsoft.com/ws-1/lh-1/Files/fleet_loads.csv" in model_text


def test_no_fabric_deploy_leaves_supabase_placeholder_untouched():
    """Without an actual Fabric deploy (no credentials/target), the
    provisioner must never be called - this must have zero effect on every
    non-Fabric-deploy run."""
    mapping = _mapping_with_csv_table()

    with patch("app.deploy.fabric_data_provisioner.provision_data_files") as mock_provision:
        result = generate(
            mapping,
            GenerateRequest(target=Target.SEMANTIC_MODEL_ONLY, deploy=Deploy.NONE, write_to_disk=False),
        )

    mock_provision.assert_not_called()
    model_text = " ".join(str(v) for v in result.get("semantic_model", {}).values())
    assert supabase_file_url("fleet_loads.csv") in model_text


def test_provisioning_failure_does_not_break_generation():
    """provision_data_files already swallows Lakehouse/OneLake errors
    internally and returns whatever succeeded (possibly nothing) rather than
    raising - this proves generate() itself degrades gracefully when that
    comes back empty (e.g. the Lakehouse couldn't be created), still
    producing a valid package that simply still points at the Supabase
    placeholder rather than crashing the whole run.
    """
    mapping = _mapping_with_csv_table()

    with patch("app.deploy.fabric_data_provisioner.provision_data_files", return_value={}), \
         patch("app.deploy.fabric_deployer.deploy", return_value={"items": {}}):
        result = generate(
            mapping,
            GenerateRequest(
                target=Target.SEMANTIC_MODEL_ONLY, deploy=Deploy.FABRIC,
                workspace_id="ws-1", fabric_access_token="tok", write_to_disk=False,
            ),
        )

    assert result["status"] == "success"
    model_text = " ".join(str(v) for v in result.get("semantic_model", {}).values())
    assert supabase_file_url("fleet_loads.csv") in model_text
