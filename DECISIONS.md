# Design decisions

1. **Indexes.** Unique indexes on `emp_code` and `(emp_code, date)` prevent duplicates. Employee indexes on `(department, emp_code)`, `(joined_on, department)` and `(department, joined_on)` support sorted lists and eligible headcounts. Attendance indexes on `(date, emp_code)`, `(emp_code, punch_in)` and `(status, date, emp_code)` support monthly queries, latest punch-in lookup and filtered pages. I skipped a history index because nothing queries it.

2. **Punch-in race.** Both requests find the employee, calculate the IST attendance date and try to insert. The unique employee/date index lets one succeed with 201. MongoDB rejects the duplicate; the code catches DuplicateKeyError and returns 409.

3. **Ties.** I filter by rank <= limit, so both tied employees are returned, even if that exceeds limit rows. Competition ranks can be 1, 2, 2, 4. Employee code gives tied rows a stable order.

4. **Headcount.** I start with employees who joined by month-end, then look up attendance. Someone with no logs still counts in headcount. Average hours use record totals and counts, rather than averaging each employee's average.

5. **100x data.** I would profile the slow queries first, then add daily summaries if needed. Corrections would also update those summaries so analytics stayed accurate.
