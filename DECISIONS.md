# Design decisions

1. **Indexes.** Unique employee codes and employee/date pairs enforce identity and one attendance record per day. Employee department/code supports sorted lists; join-date/department and department/join-date support eligible headcount. Attendance date/code supports global lists and monthly ranges, employee/punch supports punch-out, and status/date/code supports status-filtered pages. I rejected a history index because no endpoint searches history.

2. **Punch-in race.** Both requests resolve the employee and compute the attendance date. Both attempt an insert. MongoDB's unique employee/date index permits one write; that request returns 201. The other insert raises DuplicateKeyError and returns 409. A preliminary read cannot guarantee this outcome.

3. **Ties.** The database computes competition ranks before filtering rank <= limit. Everyone tied at an included rank is returned, even when the response exceeds limit rows. Employee code orders tied rows afterward.

4. **Headcount.** Department summary starts with eligible employees, then looks up each employee's monthly totals. An empty lookup contributes zero attendance but still one employee. Work-hour averages divide record sums by record counts.

5. **100x data.** I would consider daily summary documents after profiling. Corrections would update the affected summaries consistently; otherwise faster reads could silently report stale attendance.
