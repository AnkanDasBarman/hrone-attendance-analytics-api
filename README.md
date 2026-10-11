# Employee Attendance & Analytics API

FastAPI and MongoDB implementation for the HROne attendance assignment. The API includes employee and attendance
endpoints, punch-in/out rules, attendance regularization with history, monthly and department analytics, and MongoDB
explain plans.

## Requirements

- Python 3.11+
- MongoDB 6.0+

## Run locally

From the project root, create and activate a virtual environment, then install dependencies:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Set the `MONGO_URI` and `MONGO_DB` environment variables for your MongoDB instance before starting the API. For example,
in PowerShell:

```powershell
$env:MONGO_URI = "mongodb://127.0.0.1:27017"
$env:MONGO_DB = "attendance_db"
```

Start the API:

```powershell
uvicorn app.main:app --port 8000
```

`GET /health` checks MongoDB connectivity. Indexes are created automatically at startup. This repository does not
include sample data or a seed script; provide employee and attendance documents in MongoDB before trying data-dependent
routes. The assignment grader supplies its own dataset.

## Submission

Submit a public Git repository containing the application, requirements, review, decisions, and this README. Do not
commit `.env`, credentials, virtual environments, generated explain output, or data dumps.
