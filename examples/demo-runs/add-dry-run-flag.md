Add a --dry-run flag to scripts/backup.sh.

It should print exactly what it would archive and what it would delete,
without touching the filesystem. Keep every existing flag working the same
way it does today, and add one test to tests/test_backup.sh that exercises
the new flag.
