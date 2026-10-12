# Employee Attendance & Analytics API

FastAPI and MongoDB implementation of the HROne Employee Attendance & Analytics assignment.

## Requirements

- Python 3.11 or newer
- MongoDB 6.0 or newer, including MongoDB Atlas

## Local setup

Create and activate a virtual environment, then install dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Configure MongoDB through environment variables. Use a real Atlas URI locally; do not put it in source control:

```powershell
$env:MONGO_URI="mongodb+srv://<user>:<password>@<cluster>.mongodb.net/?retryWrites=true&w=majority"
$env:MONGO_DB="attendance_db"
```

Check the connection:

```powershell
python -c "from pymongo import MongoClient; import os; print(MongoClient(os.environ['MONGO_URI'], serverSelectionTimeoutMS=15000).admin.command('ping'))"
```

Load the included sample data:

```powershell
python sample_seed.py
```

The seed script replaces the `employees` and `attendance_logs` collections with the six sample employees and nine sample attendance records.

Start the API:

```powershell
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Interactive API documentation is available at <http://127.0.0.1:8000/docs>.

## API areas

- Employee creation and paginated listing
- Atomic punch-in and punch-out with IST and overnight-shift rules
- Attendance listing with filtering and pagination
- Manual corrections with an append-only audit history
- Employee monthly analytics
- Department summaries including employees with zero logs
- Late-comer leaderboard with standard competition ranking
- Gap-filled department attendance trends
- MongoDB execution-plan inspection through `/admin/explain/{endpoint}`

Instants use epoch milliseconds. Calendar dates use `YYYY-MM-DD`, and months use `YYYY-MM`.

## Validation

The implementation was checked with Python compilation/import checks and live MongoDB-backed endpoint tests covering:

- Health, employee, attendance, and analytics endpoints
- Duplicate punch-in protection
- Punch-out calculations
- Manual correction history
- Invalid dates, months, and timestamps
- MongoDB explain plans using indexes

## Submission and security

The repository intentionally contains no `.env` files, credentials, or Dockerfile. Configure secrets through environment variables only. Rotate any database credential that has been exposed before sharing the repository publicly.
