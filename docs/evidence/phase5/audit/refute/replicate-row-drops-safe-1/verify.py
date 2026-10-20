import psycopg
c=psycopg.connect("postgresql://atus:atus@localhost:5434/atus"); c.read_only=True
R="(num_nonnulls("+",".join(f"w.tufnwgtp{i:03d}" for i in range(1,81))+")+num_nonnulls("+",".join(f"w.tufnwgtp{i:03d}" for i in range(81,161))+"))"
P="(num_nonnulls("+",".join(f"p.tu20fwgt{i:03d}" for i in range(1,81))+")+num_nonnulls("+",".join(f"p.tu20fwgt{i:03d}" for i in range(81,161))+"))"
Pz=" AND ".join(f"p.tu20fwgt{i:03d}=0" for i in range(1,161))
Pnz=" OR ".join(f"p.tu20fwgt{i:03d}<>0" for i in range(1,161))
Q={
"respondents":"SELECT count(*) FROM atus.respondents",
"rw rows":"SELECT count(*) FROM atus.replicate_weights",
"prw rows":"SELECT count(*) FROM atus.pandemic_replicate_weights",
"rw allnull":f"SELECT count(*) FROM atus.replicate_weights w WHERE {R}=0",
"rw partial-null":f"SELECT count(*) FROM atus.replicate_weights w WHERE {R} BETWEEN 1 AND 159",
"rw allnull by year / final_weight null":f"SELECT r.data_year, count(*), count(*) FILTER (WHERE r.final_weight IS NULL) FROM atus.replicate_weights w JOIN atus.respondents r USING(tucaseid) WHERE {R}=0 GROUP BY 1",
"2020 respondents":"SELECT count(*), count(*) FILTER (WHERE final_weight IS NULL) FROM atus.respondents WHERE data_year=2020",
"final_weight null/<=0 overall by year":"SELECT data_year, count(*) FILTER (WHERE final_weight IS NULL), count(*) FILTER (WHERE final_weight<=0) FROM atus.respondents WHERE final_weight IS NULL OR final_weight<=0 GROUP BY 1",
"fw>0 lacking rw row":"SELECT count(*) FROM atus.respondents r WHERE r.final_weight>0 AND NOT EXISTS(SELECT 1 FROM atus.replicate_weights w WHERE w.tucaseid=r.tucaseid)",
"fw>0 with any null rep":f"SELECT count(*) FROM atus.respondents r JOIN atus.replicate_weights w USING(tucaseid) WHERE r.final_weight>0 AND {R}<160",
"rw rows orphan (no respondent)":"SELECT count(*) FROM atus.replicate_weights w WHERE NOT EXISTS(SELECT 1 FROM atus.respondents r WHERE r.tucaseid=w.tucaseid)",
"prw allzero":f"SELECT count(*) FROM atus.pandemic_replicate_weights p WHERE {Pz}",
"prw allnull":f"SELECT count(*) FROM atus.pandemic_replicate_weights p WHERE {P}=0",
"prw partial null":f"SELECT count(*) FROM atus.pandemic_replicate_weights p WHERE {P} BETWEEN 1 AND 159",
"prw allzero by year / pw":f"SELECT r.data_year, count(*), count(*) FILTER (WHERE r.pandemic_weight=0) FROM atus.pandemic_replicate_weights p JOIN atus.respondents r USING(tucaseid) WHERE {Pz} GROUP BY 1",
"pw=0 respondents by year":"SELECT data_year, count(*) FROM atus.respondents WHERE pandemic_weight=0 GROUP BY 1",
"pw null in 2019/2020":"SELECT count(*) FROM atus.respondents WHERE data_year IN (2019,2020) AND pandemic_weight IS NULL",
"pw<0":"SELECT count(*) FROM atus.respondents WHERE pandemic_weight<0",
"pw>0 lacking prw":"SELECT count(*) FROM atus.respondents r WHERE r.pandemic_weight>0 AND NOT EXISTS(SELECT 1 FROM atus.pandemic_replicate_weights p WHERE p.tucaseid=r.tucaseid)",
"pw>0 with allzero prw":f"SELECT count(*) FROM atus.respondents r JOIN atus.pandemic_replicate_weights p USING(tucaseid) WHERE r.pandemic_weight>0 AND {Pz}",
"pw=0 with any nonzero prw":f"SELECT count(*) FROM atus.respondents r JOIN atus.pandemic_replicate_weights p USING(tucaseid) WHERE r.pandemic_weight=0 AND ({Pnz})",
"pw>0 with any null prw":f"SELECT count(*) FROM atus.respondents r JOIN atus.pandemic_replicate_weights p USING(tucaseid) WHERE r.pandemic_weight>0 AND {P}<160",
"prw any negative":" SELECT count(*) FROM atus.pandemic_replicate_weights p WHERE "+" OR ".join(f"p.tu20fwgt{i:03d}<0" for i in range(1,161)),
"duplicate tucaseid rw/prw":"SELECT (SELECT count(*)-count(DISTINCT tucaseid) FROM atus.replicate_weights),(SELECT count(*)-count(DISTINCT tucaseid) FROM atus.pandemic_replicate_weights)",
}
for k,q in Q.items():
    print(k, "=>", c.execute(q).fetchall(), flush=True)
