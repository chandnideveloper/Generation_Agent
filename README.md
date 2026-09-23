# Unified Generation Agent

Consolidated report generation service that translates **Qlik Sense** and **Tableau** workbooks into **Power BI / Microsoft Fabric** PBIP/TMDL models and PBIR reports, with automated atomic deployment to GitHub.

---

## Features

- **Single Port (5000)**: Serves all migration workflows (`POST /migrate`) from one unified process.
- **Zero Inter-Service HTTP Overhead**: Qlik and Tableau generation engines run entirely in-process.
- **Fabric Git Integration Ready**: Pushes semantic models and reports directly into `<target_folder>/<AppName>` (e.g. `Test-workspace/`).
- **Atomic Git Deployments**: Uses GitHub Git Data API (blobs -> tree -> commit -> ref) for zero partial commits.
- **Support for Both Mapping Modes**:
  - **Qlik**: Direct inline `mapping_result` or MongoDB retrieval via `run_id` / `app_id`.
  - **Tableau**: MongoDB retrieval from `mapping_results` / `mapping` collections via `project_id`, `workbook_id`, and `run_id`.

---

## Quick Start

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure `.env`
Ensure your MongoDB Atlas URL and Azure OpenAI keys are set in `.env`.

### 3. Run the Service
```bash
python main.py
```
The service will be listening at `http://0.0.0.0:5000`.

---

## API Reference

### Health Check
```http
GET http://localhost:5000/health
```

### Unified Migration
```http
POST http://localhost:5000/migrate
Content-Type: application/json
```

#### Qlik Payload
```json
{
  "source_type": "qlik",
  "run_id": "01191464-2731-46ba-a8b1-63a8cbf5e84c",
  "source": {
    "app_id": "8dceba25-573e-4501-b470-cac2b210ee79",
    "space_id": "personal"
  },
  "target": {
    "deployment_type": "git",
    "github": {
      "owner": "chandnideveloper",
      "repo_name": "tmdl",
      "branch": "main",
      "git_pat": "ghp_...",
      "folder": "Test-workspace"
    }
  }
}
```

#### Tableau Payload
```json
{
  "source_type": "tableau",
  "run_id": "11234",
  "source": {
    "project_id": "c5134699-2aa2-4ab2-b201-7fcd06cf453a",
    "workbook_id": "3d814c59-137c-4311-b60a-0f631ea66eda"
  },
  "target": {
    "deployment_type": "git",
    "github": {
      "owner": "chandnideveloper",
      "repo_name": "tmdl",
      "branch": "main",
      "git_pat": "ghp_...",
      "folder": "Test-workspace"
    }
  }
}
```
