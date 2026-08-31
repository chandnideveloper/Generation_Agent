"""Every file-based table's M-query used to point at a fixed external
Supabase bucket with no upload step anywhere in this pipeline - if nothing
else had already staged the exact file there, the report's fields would
never resolve. This exercises the real Fabric Lakehouse upload path that
replaces it, with the OneLake/Fabric REST calls mocked.
"""

from unittest.mock import MagicMock, patch

from app.deploy import fabric_data_provisioner as provisioner
from app.deploy.fabric_deployer import FabricError


def _resp(status=200, json_body=None, text="", headers=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = json_body or {}
    r.text = text
    r.headers = headers or {}
    return r


def test_ensure_lakehouse_reuses_existing_item():
    with patch("app.deploy.fabric_data_provisioner.find_item", return_value="lh-existing"):
        lakehouse_id = provisioner.ensure_lakehouse("ws-1", "MyApp_DataFiles", "tok")
    assert lakehouse_id == "lh-existing"


def test_ensure_lakehouse_creates_when_missing():
    with patch("app.deploy.fabric_data_provisioner.find_item", return_value=None), \
         patch("requests.post", return_value=_resp(201, {"id": "lh-new"})) as mock_post:
        lakehouse_id = provisioner.ensure_lakehouse("ws-1", "MyApp_DataFiles", "tok")
    assert lakehouse_id == "lh-new"
    called_url, called_kwargs = mock_post.call_args
    assert called_url[0] == "https://api.fabric.microsoft.com/v1/workspaces/ws-1/items"
    assert called_kwargs["json"] == {"displayName": "MyApp_DataFiles", "type": "Lakehouse"}


def test_ensure_lakehouse_polls_long_running_creation():
    with patch("app.deploy.fabric_data_provisioner.find_item", return_value=None), \
         patch("requests.post", return_value=_resp(202, headers={"Location": "https://op/status/1"})), \
         patch("app.deploy.fabric_data_provisioner._wait", return_value="lh-polled") as mock_wait:
        lakehouse_id = provisioner.ensure_lakehouse("ws-1", "MyApp_DataFiles", "tok")
    assert lakehouse_id == "lh-polled"
    mock_wait.assert_called_once_with("https://op/status/1", "tok")


def test_onelake_file_url_shape():
    url = provisioner.onelake_file_url("ws-1", "lh-1", "fleet_loads.csv")
    assert url == "https://onelake.dfs.fabric.microsoft.com/ws-1/lh-1/Files/fleet_loads.csv"


def test_upload_file_to_lakehouse_does_create_append_flush_in_order():
    calls = []

    def fake_put(url, headers=None, timeout=None):
        calls.append(("PUT", url))
        return _resp(201)

    def fake_patch(url, headers=None, data=None, timeout=None):
        calls.append(("PATCH", url, data))
        return _resp(200 if "action=append" in url else 200)

    content = b"id,name\n1,Alice"
    with patch("requests.put", side_effect=fake_put), patch("requests.patch", side_effect=fake_patch):
        provisioner.upload_file_to_lakehouse("ws-1", "lh-1", "fleet_loads.csv", content, "tok")

    expected_flush = f"action=flush&position={len(content)}"
    assert calls[0][0] == "PUT" and "resource=file" in calls[0][1]
    assert calls[1][0] == "PATCH" and "action=append&position=0" in calls[1][1]
    assert calls[1][2] == content
    assert calls[2][0] == "PATCH" and expected_flush in calls[2][1]


def test_upload_file_to_lakehouse_raises_on_create_failure():
    with patch("requests.put", return_value=_resp(403, text="Forbidden")):
        try:
            provisioner.upload_file_to_lakehouse("ws-1", "lh-1", "x.csv", b"data", "tok")
            assert False, "expected FabricError"
        except FabricError as exc:
            assert "403" in str(exc)


def test_provision_data_files_uploads_only_files_with_content():
    data_files = [
        {"name": "fleet_loads.csv", "content_text": "id,name\n1,Alice"},
        {"name": "no_content.csv"},  # extraction never got its bytes - must be skipped, not uploaded empty
    ]
    with patch("app.deploy.fabric_data_provisioner.ensure_lakehouse", return_value="lh-1"), \
         patch("app.deploy.fabric_data_provisioner.upload_file_to_lakehouse") as mock_upload:
        result = provisioner.provision_data_files(data_files, "ws-1", "tok", "MyApp_DataFiles")

    assert mock_upload.call_count == 1
    assert mock_upload.call_args[0][2] == "fleet_loads.csv"
    assert result == {"fleet_loads.csv": provisioner.onelake_file_url("ws-1", "lh-1", "fleet_loads.csv")}


def test_provision_data_files_converts_qvd_extension_to_csv():
    data_files = [{"name": "fleet_loads.qvd", "content_text": "id,name\n1,Alice"}]
    with patch("app.deploy.fabric_data_provisioner.ensure_lakehouse", return_value="lh-1"), \
         patch("app.deploy.fabric_data_provisioner.upload_file_to_lakehouse") as mock_upload:
        result = provisioner.provision_data_files(data_files, "ws-1", "tok", "MyApp_DataFiles")

    assert mock_upload.call_args[0][2] == "fleet_loads.csv"
    assert "fleet_loads.csv" in result


def test_provision_data_files_skips_a_file_whose_upload_fails_but_keeps_others():
    data_files = [
        {"name": "good.csv", "content_text": "a,b\n1,2"},
        {"name": "bad.csv", "content_text": "a,b\n3,4"},
    ]

    def fake_upload(workspace_id, lakehouse_id, path, content, token):
        if path == "bad.csv":
            raise FabricError("upload failed")

    with patch("app.deploy.fabric_data_provisioner.ensure_lakehouse", return_value="lh-1"), \
         patch("app.deploy.fabric_data_provisioner.upload_file_to_lakehouse", side_effect=fake_upload):
        result = provisioner.provision_data_files(data_files, "ws-1", "tok", "MyApp_DataFiles")

    assert "good.csv" in result
    assert "bad.csv" not in result


def test_provision_data_files_returns_empty_without_credentials():
    result = provisioner.provision_data_files([{"name": "x.csv", "content_text": "a"}], "", "", "App_DataFiles")
    assert result == {}
