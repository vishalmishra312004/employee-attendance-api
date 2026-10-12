# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `punch_in` and `punch_out` timestamp selection | A supplied timestamp of `0` was treated as omitted and replaced with the server clock. | Send a request with `punched_at: 0`; the response used the current time instead of returning validation 422. | Check explicitly for `is not None` before applying the default. |
| 2 | Request timestamp models and `validate_epoch_ms` | Pydantic could coerce floats, strings, and booleans into integers before validation, contrary to the epoch-millisecond contract. | Send `punched_at` as `"1783312500000"`, `1783312500000.0`, or `true`. | Use `StrictInt` for all timestamp request fields and retain the explicit range/type guard. |
| 3 | Employee and date inputs | Calendar strings were only checked by shape in several paths; invalid dates could reach `fromisoformat` or MongoDB filters. | Send `joined_on: "2026-02-30"` or an invalid attendance/trend date. | Validate ISO calendar dates and require the canonical `YYYY-MM-DD` representation. |
| 4 | Startup indexes | Employee department queries used the department/code index, but headcount-by-day trend queries also need the join date in the compound index. | Inspect the trend explain plan on a large employee collection. | Create an idempotent `(department, joined_on)` index at startup. |
| 5 | `late_leaderboard_pipeline` | MongoDB 7 does not allow `$rank` to use a compound `sortBy`; including `emp_code` made the endpoint fail with a 500. | Request the leaderboard against MongoDB 7. | Rank only by total late minutes, then apply the required employee-code tie ordering in a following `$sort`. |
| 6 | `explain_endpoint` | PyMongo's `Cursor.explain()` in the installed driver accepts no verbosity argument, so the keyword/positional calls raised a 500. | Request `/admin/explain/attendance_list`. | Use MongoDB's `explain` command with `executionStats` verbosity while preserving the endpoint's filter, sort, skip, and limit. |
| 7 | `explain_endpoint` response | Explain output contains BSON `Timestamp` values that FastAPI's default encoder cannot serialize. | Request any explain endpoint after the database command succeeds. | Convert raw explain documents through MongoDB Extended JSON before returning them. |

I also checked the unique employee and attendance indexes, the atomic insert/update filters used for concurrent punches and corrections, the aggregation-based analytics, and the overnight-shift date calculation. Those were retained because they match the contract: uniqueness is enforced by MongoDB, correction history is guarded by its current length, and analytics read stored derived fields.

Also note anything you looked at and decided was **not** a defect, and why.
