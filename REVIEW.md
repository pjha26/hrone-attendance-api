# Starter review

Locations refer to the original starter, before the implementation changed its line numbers.

| # | Where | What was wrong | How to notice it | Fix |
|---|---|---|---|---|
| 1 | MongoClient, line 22 | Datetimes were decoded without explicit timezone awareness. | A seeded UTC punch could be confused with a host-local datetime or fail subtraction against an aware shift boundary. | Use tz_aware=True and UTC-aware boundary conversion. |
| 2 | Startup | No uniqueness or query indexes were created. | Duplicate concurrent writes and collection scans at scale. | Create named unique and compound indexes at startup, without modifying seeded documents. |
| 3 | compute_late_minutes, line 35 | Shift start was built in the punch's timezone and on its calendar date. | UTC punches and early-morning overnight punches were compared with the wrong shift. | Construct IST shift boundaries using the attendance date. |
| 4 | compute_late_minutes, lines 36 to 37 | Grace was checked after truncating to minutes. | 09:40:01 for a 09:30 shift returned zero instead of ten. | Compare elapsed seconds with 600, then floor minutes. |
| 5 | compute_work_hours, line 41 | Floating-point round did not implement half-up. | 4.485 hours must round to 4.49. | Divide integer seconds with Decimal and quantize using ROUND_HALF_UP. |
| 6 | compute_overtime, line 46 | Overnight shift end was on the starting day. | 22:00-06:00 followed by next-day 06:40 produced excessive overtime. | Move an overnight end to the next calendar day. |
| 7 | compute_overtime, line 47 | Overtime below thirty minutes was counted. | Shift-end plus 29:59 must produce zero. | Floor minutes, then enforce the thirty-minute minimum. |
| 8 | EmployeeIn | Formats, lengths, real dates, and differing shift times were not validated. | Invalid email, EMP1, February 30, and equal shift times were accepted. | Match the contract's fields and patterns, with calendar and cross-field validators. |
| 9 | PunchInIn | Integer coercion, absent bounds, and explicit null allowed invalid timestamps. | Seconds, strings, floats, booleans, or out-of-range values could be accepted or crash conversion. | Strict bounded epoch milliseconds; distinguish omission from explicit null. |
| 10 | PunchInIn.status | Any status string was accepted. | ABSENT or an arbitrary value could create a punch-in record. | Restrict punch-in to PRESENT, WFH, ON_DUTY. |
| 11 | health | Readiness did not check MongoDB. | The endpoint returned 200 with an unavailable database. | Ping MongoDB and return 503 on failure. |
| 12 | create_employee, lines 79 to 83 | Check-then-insert allowed duplicate races. | Simultaneous creates could both return 201. | Unique emp_code index; insert directly and translate DuplicateKeyError to 409. |
| 13 | create_employee, line 82 | created_at was a host-local naive instant. | Running under another timezone changed the stored meaning. | Generate a UTC-aware, whole-second server instant. |
| 14 | Employee responses | Datetimes were returned as JSON date strings. | created_at did not have the required integer epoch-ms type. | Serialize instants explicitly. |
| 15 | list_employees, line 93 | page * page_size skipped the first page. | page=1 omitted the first twenty employees. | Use (page-1) * page_size. |
| 16 | list_employees, line 94 | Total ignored department filtering. | The total included Sales employees in an Engineering-only page. | Count with the exact same filter. |
| 17 | list_employees, line 95 | Employee ordering was unspecified. | Insertion order affected pages. | Sort emp_code ascending in MongoDB. |
| 18 | List parameters | Pagination, date ranges, and status values lacked validation. | page=0, page_size=101, an impossible date, or a reversed range was accepted. | Shared bounded query types and date/range validation. |
| 19 | punch_in, lines 101 and 113 | A missing employee was dereferenced. | Unknown emp_code caused a server error. | Return 404 before accessing shift values. |
| 20 | punch_in, lines 102 to 103 | Instants used the host timezone and retained milliseconds; overnight dates were not attributed correctly. | UTC/IST date boundaries and punches before an overnight end failed. | Normalize milliseconds to whole UTC seconds, then derive the IST attendance date. |
| 21 | punch_in, lines 104 to 118 | Check-then-insert did not prevent duplicate attendance. | Parallel punch-ins could create multiple employee/day records. | Unique (emp_code,date) index and duplicate-key 409. |
| 22 | punch_in line 119; list_attendance line 149 | An invented public id exposed the internal identifier. | Responses contained a field the contract excludes. | Explicit employee/attendance output fields; no public id or _id. |
| 23 | list_attendance, lines 144 to 147 | All matched documents were fetched, sorted, counted, and sliced in Python. | Memory and latency grew with 100,000 matched records. | Database sort/skip/limit and indexed filtered count; materialize only the page. |
| 24 | list_attendance, line 145 | The required employee tie-breaker was absent. | Employees on the same date appeared in unstable order. | Sort date descending, emp_code ascending. |
| 25 | Attendance output | Nested BSON times were not converted; legacy fields were not defaulted. | history changes contained datetime strings; older records lacked half_day/history. | Shared recursive instant serialization and output defaults. |

Inspected and kept: synchronous `def` routes are appropriate for synchronous PyMongo; FastAPI runs them in a thread pool. The fixed IST offset matches the requirement. `load_dotenv()` already preserves existing environment values by default; that behaviour is made explicit. Local connection defaults are acceptable because injected environment variables win. Open records correctly start with null punch_out/work_hours and empty history. Pydantic's ignoring of extra derived inputs is retained because the contract requires ignoring them.

New operations use conditional single-document updates rather than document replacement. PATCH sets final values and pushes one entry together, checking the expected material values and history length. Punch-out compares the state used for its calculations. This avoids stale computations and audit loss without requiring a replica set or transactions.
