The current sql/schema.sql and sql/grants.sql are the application baseline.
Bootstrap installs them once; startup applies the SQL files in this directory.
There are no migrations after the baseline yet.

Name the first migration a001-description.sql, then a002-description.sql, etc.
Names are case-insensitive identities, ordered lexicographically. Keep sequence
numbers zero-padded. A later series can use timestamps such as
B2026-10-16-120000-description.sql; it sorts after the a-series.
The whole filename identifies the migration. Do not rename or edit applied files.

Each file is one transaction, including its ledger entry in dl.schema_migrations.
SQL runs as dl_owner. Use schema-qualified object names and transaction-compatible
SQL. Do not control transactions, change session settings/roles, manipulate
advisory locks, or modify the ledger. Failure stops startup; earlier successful
files remain applied and are skipped on retry.

Keep the baseline fixed when adding migrations, so new and existing databases
follow the same upgrade history. These files are included in the Python package.
