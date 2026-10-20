\timing off
BEGIN READ ONLY;
SELECT count(*) AS resp FROM atus.respondents;
SELECT count(*) AS rw, (SELECT count(*) FROM atus.pandemic_replicate_weights) AS prw FROM atus.replicate_weights;
-- all-null rep rows: count those where every rep col null
SELECT count(*) FILTER (WHERE num_nonnulls(VARIADIC arr)=0) AS allnull,
       count(*) FILTER (WHERE num_nulls(VARIADIC arr) BETWEEN 1 AND 159) AS partial,
       count(*) FILTER (WHERE num_nulls(VARIADIC arr)=0) AS full
FROM (SELECT ARRAY[ (to_jsonb(w) - 'tucaseid') ] arr, w.* FROM atus.replicate_weights w LIMIT 0) x;
