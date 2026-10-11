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

## Tests

The tests use a local MongoDB server and create uniquely named disposable databases, which are dropped after each run.
Set `TEST_MONGO_URI` to another **loopback** MongoDB port if needed. Run the regular suite with:

```powershell
python -m unittest discover -s tests
```

An opt-in scale test seeds 100,000 attendance records in its own disposable database and checks the five explain plans:

```powershell
$env:RUN_SCALE_TESTS = "1"
python -m unittest discover -s tests
Remove-Item Env:RUN_SCALE_TESTS
```

The tests cover key rules, concurrency, validation, analytics edge cases, and index plans. They do not replace evaluation
against the grader's hidden dataset.

## Submission

Submit a public Git repository containing the application, requirements, review, decisions, and this README. Do not
commit `.env`, credentials, virtual environments, generated explain output, or data dumps.
