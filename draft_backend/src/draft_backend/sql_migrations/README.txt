The current sql/schema.sql and sql/grants.sql are the application baseline.
Bootstrap installs them once; startup applies the SQL files in this directory.
The first migration, 0001_tournament.sql, installs tournaments and permissions.

Every file ending in .sql is a migration. The whole filename is its case-sensitive
identity and files run in lexicographic order. Use zero-padded numbers to make
that order clear: 0001_tournament.sql, 0002_description.sql, etc. Numbers are not
parsed. Do not rename or edit applied files. SQL files must be UTF-8 without NUL
bytes. Checksums normalize Windows and POSIX line endings.

Each file is one transaction, including its ledger entry in dl.schema_migrations.
SQL runs as dl_owner. Use schema-qualified object names and transaction-compatible
SQL. Do not control transactions, change session settings/roles, manipulate
advisory locks, or modify the ledger. Failure stops startup; earlier successful
files remain applied and are skipped on retry.

Keep the baseline fixed when adding migrations, so new and existing databases
follow the same upgrade history. These files are included in the Python package.
