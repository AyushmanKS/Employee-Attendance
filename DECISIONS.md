# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?

I created 6 indexes: a) employees.emp_code unique - for lookup, and prevent duplicate employee
b) attendance (emp_code, date) unique - for maintaining unique records mainly for the punch in race fix, and also for employee history, monthly
c) employees (department, emp_code) - department filter
d) attendance (date -1, emp_code 1) - for exactly matching the list sort and leaderboard range
e) attendance (emp_code, punch_in -1) - punch out and latest punch in
f) employees.joined_on - for summary headcount
rejected - index on status, it has only 5 values so date index + filter is enough


2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.

Both requests pass validation and go to insert_one at same time. unique index on (emp_code, date) allows only one insert, other one gets DuplicateKeyError and I return 409. I dont do find then insert, so there is no gap where both can pass.


3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?

I use $rank - tied employees get same rank and next rank is skipped (1, 2, 2, 4). limit is applied after ranking (rank <= limit), so all employees tied at the cutoff are returned, even if rows are more than limit. tied rows are sorted by emp_code.


4. **Headcount.** How does your department summary make sure employees with zero logs are counted?

Pipeline starts from employees (joined_on <= month end), not from logs. $lookup adds that month logs, empty array if no logs. $unwind with preserveNullAndEmptyArrays keeps these employees, so they are counted in headcount and add 0 in other fields.


5. **One thing you would change** if this had to serve 100x the data.

Right now analytics read all raw logs on every request. I would keep a daily summary per employee, update it on punch in, punch out and patch, and run analytics on that. also replace skip pagination with keyset pagination (continue after last date, emp_code).
