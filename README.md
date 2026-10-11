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

Set `MONGO_URI` and `MONGO_DB` in your environment, or copy `.env.example` to `.env` and adjust it for your local
MongoDB. Environment variables take precedence over `.env`. Start the API:

```powershell
uvicorn app.main:app --port 8000
```

`GET /health` checks MongoDB connectivity. Indexes are created automatically at startup. If you want to load the sample
documents, configure a **disposable local database** first: `python sample_seed.py` replaces the contents of its
`employees` and `attendance_logs` collections.

## Tests

Run the regression tests with:

```powershell
python -m unittest discover -s tests
```

The bundled sample data is only a small shape/example dataset; it does not cover all assignment edge cases or guarantee
analytics correctness on the grader's larger dataset.

## Submission

Submit a public Git repository containing the application, requirements, review, decisions, and this README. Do not
commit `.env`, credentials, virtual environments, generated explain output, or data dumps.
