# Design decisions

1. **Indexes.** Unique `employees.emp_code` enforces identity and supports lookups. `(department, joined_on)` serves filtered headcount; `(joined_on)` serves overall headcount. Unique attendance `(emp_code, date)` prevents duplicate daily records; `(emp_code, punch_in descending)` finds the latest punch-in; `(date descending, emp_code ascending)` supports attendance-list order. I considered a standalone `status` index but omitted it because status has few values and the date index supports the main list order.

2. **Punch-in race.** Both requests truncate timestamps to seconds and derive the same employee/date key. The unique index allows one insert; the other raises `DuplicateKeyError` and becomes HTTP 409. The winner receives 201.

3. **Ties.** MongoDB `$rank` assigns equal late-minute totals the same rank. The pipeline filters by `rank <= limit`, so it returns every employee tied at the cutoff; employee code orders tied rows.

4. **Headcount.** The summary starts from employees joined by period end, looks up logs while preserving employees without matches, then groups per employee before department totals. Thus each eligible employee with no logs still counts once.

5. **100x data.** I would measure query and index costs, then consider pre-aggregated summaries and archiving older attendance without changing API results.
