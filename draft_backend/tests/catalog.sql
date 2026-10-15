INSERT INTO dl.draftable (name, point_cost)
SELECT 'Player ' || lpad(n::text, 2, '0'), n - 1 FROM generate_series(1, 30) AS n;
