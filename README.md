# Employee Attendance & Analytics API

FastAPI + MongoDB implementation of the HROne assignment. All code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI=mongodb://localhost:27017 MONGO_DB=attendance_db   # or put them in a local .env
uvicorn app.main:app --port 8000
```
- `MONGO_URI` and `MONGO_DB` are required; real environment variables take priority over `.env`.
- MongoDB 6.0+ (uses `$densify`, `$setWindowFields` and `$documents` inside `$unionWith`). Tested on 7.0.
- Indexes are created at startup (idempotent); no manual steps.
- `python sample_seed.py` loads the nine sample documents.

## Status
Everything in the contract is implemented: the 25 starter defects (see `REVIEW.md`), punch-out, PATCH, the four
analytics endpoints and `/admin/explain`. Nothing is missing.

## Interpretation choices worth knowing
- **Trend range limit:** "exceeds 92 days" is counted inclusively, so `from`..`to` may cover at most 92 calendar days.
- **Trend moving average** is the mean of the reported (4-decimal) daily `attendance_rate` values in the window.
- **Rounding (R8)** is half-up everywhere. MongoDB's `$round` rounds half to even, so the trend rounds with its own
  half-up expression inside the pipeline; other reported numbers are rounded with `Decimal` in Python.
- **Working days** (R7) are calendar arithmetic, computed in Python; every figure derived from attendance data comes
  from an aggregation pipeline.
- **Punch-out before the open punch-in** (e.g. punched in at 09:35, punch-out sent for 09:00) returns 422, as
  `punched_at <= punch_in`, rather than 409 for an older closed record.
- **PATCH concurrency:** a regularization only applies if the record is unchanged since it was read (same status, punch
  times and history length); otherwise it returns 409 and the client retries, so no history entry is ever lost.
