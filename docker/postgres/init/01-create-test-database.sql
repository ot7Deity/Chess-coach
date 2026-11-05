-- Runs once, while the Postgres data directory is being initialised, as
-- POSTGRES_USER against POSTGRES_DB. It does not run again on later starts -
-- `docker compose down -v` is what re-triggers it.
--
-- Why the test database has to be a separate database:
--
-- tests/conftest.py proves the Alembic migrations can build the schema from
-- nothing, on every run, by executing `DROP SCHEMA public CASCADE` before
-- upgrading to head. A fixture that destructive must not be able to point at
-- real data, so it refuses to run at all unless the database name contains
-- "test". Pointing it at `chess_coach` would be refused; pointing it at a
-- database that happened to be named with "test" and held real games would be
-- a disaster. Two databases, one of which is expendable, removes the question.
--
-- Without this, every Postgres-backed test skips locally and the schema and
-- migration tests only ever run in CI.
CREATE DATABASE chess_coach_test OWNER chess;

COMMENT ON DATABASE chess_coach_test IS
    'Expendable. tests/conftest.py drops and recreates the public schema here on every run.';
