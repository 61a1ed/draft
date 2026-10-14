-- TEST DATABASES ONLY. Execute as the test cluster administrator in a transaction.
-- This deliberately separate marker is required before test login can start.
CREATE SCHEMA dl_test AUTHORIZATION dl_owner;
REVOKE ALL ON SCHEMA dl_test FROM PUBLIC;
SET LOCAL ROLE dl_owner;
CREATE TABLE dl_test.marker (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton)
);
INSERT INTO dl_test.marker VALUES (true);
GRANT USAGE ON SCHEMA dl_test TO dl_gateway;
GRANT SELECT ON dl_test.marker TO dl_gateway;

INSERT INTO dl.app_user (google_sub, display_name) VALUES
    ('test:alice', 'Alice'), ('test:bob', 'Bob'), ('test:carol', 'Carol');
INSERT INTO dl.draftable (name, point_cost)
SELECT 'Player ' || lpad(n::text, 2, '0'), n - 1 FROM generate_series(1, 30) AS n;
