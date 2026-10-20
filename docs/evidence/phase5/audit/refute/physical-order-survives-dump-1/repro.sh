set -e
P="psql -U postgres -v ON_ERROR_STOP=1"
$P -qc "create database atus_refute_src"
$P -d atus_refute_src -q <<SQL
create table activities(tucaseid bigint, activity_code char(6), duration_minutes smallint);
insert into activities select (random()*200000)::bigint, lpad(((random()*400)::int)::text,6,'0'), (random()*300)::int from generate_series(1,2000000) order by 2,1;
create index on activities(activity_code);
SQL
$P -d atus_refute_src -qc "vacuum analyze activities"
echo "== source correlation"; $P -d atus_refute_src -Atc "select attname, correlation from pg_stats where tablename='activities'"
pg_dump -U postgres -Fc -f /tmp/x.dump atus_refute_src; ls -l /tmp/x.dump
$P -qc "create database atus_refute_dst"
pg_restore -U postgres --no-owner --no-acl -d atus_refute_dst /tmp/x.dump; echo restore=$?
$P -d atus_refute_dst -qc "vacuum analyze"
echo "== restored correlation"; $P -d atus_refute_dst -Atc "select attname, correlation from pg_stats where tablename='activities'"
$P -d atus_refute_dst -c "explain (analyze, buffers, costs off) select sum(duration_minutes) from activities where activity_code = any('{000010,000011,000012}')"
