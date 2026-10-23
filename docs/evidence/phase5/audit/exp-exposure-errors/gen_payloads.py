import json, sys, os
d = sys.argv[1]
def w(name, obj=None, raw=None):
    p = os.path.join(d, name)
    with open(p, "wb") as f:
        f.write(raw if raw is not None else json.dumps(obj).encode())
base = {"measure": "average_minutes_per_day", "activity": {"include": ["0101"]}, "years": [2023], "variance": "replicate"}
w("valid.json", base)
# big bodies: valid JSON, mostly one big extra string field / big label
def big_extra(mb):
    o = dict(base); o["padding"] = "A" * (mb * 1024 * 1024); return o
w("big5mb.json", big_extra(5))
w("big50mb.json", big_extra(50))
# big body via giant label (field exists, max_length=120)
o = json.loads(json.dumps(base)); o["activity"]["label"] = "L" * (5 * 1024 * 1024); w("biglabel5mb.json", o)
# deeply nested
for n in (1000, 10000, 100000):
    w(f"nested{n}.json", raw=(b'{"measure":"average_minutes_per_day","activity":{"include":["0101"]},"years":[2023],"x":' + b"[" * n + b"]" * n + b"}"))
# long strings
o = json.loads(json.dumps(base)); o["activity"] = {"preset": "P" * 1_000_000}; w("preset1mb.json", o)
o = json.loads(json.dumps(base)); o["activity"] = {"preset": "x" * 100}; w("preset100.json", o)
o = json.loads(json.dumps(base)); o["activity"]["label"] = "Z" * 10000; w("label10k.json", o)
o = json.loads(json.dumps(base)); o["K" * 100000] = 1; w("longkey100k.json", o)
# many extra keys -> amplification of the error list
o = json.loads(json.dumps(base))
for i in range(20000): o[f"extra_key_{i:06d}_" + "k" * 40] = 1
w("extrakeys20k.json", o)
# unicode
o = json.loads(json.dumps(base)); o["activity"]["label"] = "Sleep ‮\U0001F634 \u0000 \ud800x é 中"; w("unicode_label.json", raw=json.dumps(o).encode())
o = json.loads(json.dumps(base)); o["activity"] = {"preset": "sleep\u0000; DROP TABLE x;--\U0001F634"}; w("unicode_preset.json", o)
o = json.loads(json.dumps(base)); o["activity"]["include"] = ["０１"]; w("fullwidth_code.json", o)
o = json.loads(json.dumps(base)); o["activity"]["include"] = ["01\n"]; w("code_trailing_newline.json", o)
o = json.loads(json.dumps(base)); o["population"] = {"state_fips": "06\n"}; w("fips_trailing_newline.json", o)
o = json.loads(json.dumps(base)); o["population"] = {"state_fips": "١٢"}; w("fips_arabic_digits.json", o)
w("invalid_utf8.json", raw=b'{"measure":"average_minutes_per_day","activity":{"include":["0101"],"label":"\xff\xfe"},"years":[2023]}')
# include lists
o = json.loads(json.dumps(base)); o["activity"]["include"] = ["0101"] * 1000; w("include1000.json", o)
o = json.loads(json.dumps(base)); o["activity"]["include"] = [f"{i:06d}" for i in range(100)]; w("include100_unknown.json", o)
o = json.loads(json.dumps(base)); o["activity"]["include"] = ["bad!"] * 100; w("include100_badpattern.json", o)
o = json.loads(json.dumps(base)); o["activity"]["include"] = ["bad!"] * 1000; w("include1000_badpattern.json", o)
o = json.loads(json.dumps(base)); o["activity"]["include"] = ["bad!"] * 100000; w("include100k_badpattern.json", o)
o = json.loads(json.dumps(base)); o["activity"]["include"] = ["0101", "0101"]; w("include_dupes.json", o)
# years
for n in (50, 51, 1000):
    o = json.loads(json.dumps(base)); o["years"] = list(range(2003, 2003 + n)); w(f"years{n}.json", o)
o = json.loads(json.dumps(base)); o["years"] = ["x"] * 1000; w("years1000_strings.json", o)
o = json.loads(json.dumps(base)); o["years"] = [2023, 2023]; w("years_dupe.json", o)
o = json.loads(json.dumps(base)); o["years"] = ["2023"]; w("years_string.json", o)
o = json.loads(json.dumps(base)); o["years"] = [2023.0]; w("years_float.json", o)
o = json.loads(json.dumps(base)); o["years"] = [True]; w("years_bool.json", o)
o = json.loads(json.dumps(base)); o["years"] = [2100]; w("years2100.json", o)
o = json.loads(json.dumps(base)); o["years"] = [2101]; w("years2101.json", o)
o = json.loads(json.dumps(base)); o["years"] = []; w("years_empty.json", o)
# ages
for name, pop in {
    "age_neg": {"age_min": -1}, "age_huge": {"age_min": 10**30}, "age_131": {"age_max": 131},
    "age_inverted": {"age_min": 80, "age_max": 20}, "age_float": {"age_min": 15.5}, "age_str": {"age_min": "15"},
}.items():
    o = json.loads(json.dumps(base)); o["population"] = pop; w(f"{name}.json", o)
w("age_5000digits.json", raw=b'{"measure":"average_minutes_per_day","activity":{"include":["0101"]},"years":[2023],"population":{"age_min":' + b"9" * 5000 + b"}}")
# dates
for name, pop in {
    "date_feb30": {"diary_date_min": "2023-02-30"}, "date_year0": {"diary_date_min": "0000-01-01"},
    "date_year1": {"diary_date_min": "0001-01-01"}, "date_9999": {"diary_date_max": "9999-12-31"},
    "date_month13": {"diary_date_min": "2023-13-01"}, "date_compact": {"diary_date_min": "20230101"},
    "date_int": {"diary_date_min": 1672531200}, "date_bigint": {"diary_date_min": 10**20},
    "date_datetime": {"diary_date_min": "2023-01-01T00:00:00"}, "date_datetime_tz": {"diary_date_min": "2023-01-01T05:00:00+05:00"},
    "date_inverted": {"diary_date_min": "2023-12-31", "diary_date_max": "2023-01-01"},
    "date_sqlish": {"diary_date_min": "2023-01-01'; SELECT pg_sleep(5);--"},
}.items():
    o = json.loads(json.dumps(base)); o["population"] = pop; w(f"{name}.json", o)
# spec params possibly reaching SQL
for name, patch in {
    "weights_sqli": {"weights": "multiyear; DROP TABLE atus.respondents;--"},
    "measure_sqli": {"measure": "average_minutes_per_day' OR '1'='1"},
    "variance_sqli": {"variance": "none) --"},
    "region_str": {"population": {"region": "1 OR 1=1"}},
    "region_bool": {"population": {"region": True}},
    "sex_sqli": {"population": {"sex": "male' --"}},
    "fips_sqli": {"population": {"state_fips": "06' OR '1'='1"}},
    "pop_extra": {"population": {"tucaseid": "1"}},
    "conf_nan": None, "conf_inf": None,
    "conf_1": {"confidence_level": 1.0}, "conf_str": {"confidence_level": "0.9"},
    "null_activity": {"activity": None}, "array_body": None,
}.items():
    if patch is None: continue
    o = json.loads(json.dumps(base)); o.update(patch); w(f"{name}.json", o)
w("conf_nan.json", raw=b'{"measure":"average_minutes_per_day","activity":{"include":["0101"]},"years":[2023],"confidence_level":NaN}')
w("conf_inf.json", raw=b'{"measure":"average_minutes_per_day","activity":{"include":["0101"]},"years":[2023],"confidence_level":Infinity}')
w("array_body.json", [base])
w("malformed_trunc.json", raw=b'{"measure":"average_minutes_per_day","activity":')
w("malformed_trailing.json", raw=json.dumps(base).encode() + b" garbage")
w("malformed_single_quotes.json", raw=b"{'measure':'average_minutes_per_day'}")
w("dup_keys.json", raw=b'{"measure":"participation_rate","measure":"average_minutes_per_day","activity":{"include":["0101"]},"years":[2023]}')
w("empty.json", raw=b"")
w("pandemic_ok.json", {"measure": "average_minutes_per_day", "activity": {"preset": "sleep"}, "years": [2020], "weights": "pandemic"})
w("year2020_multiyear.json", {"measure": "average_minutes_per_day", "activity": {"preset": "sleep"}, "years": [2020]})
w("year_missing.json", {"measure": "average_minutes_per_day", "activity": {"preset": "sleep"}, "years": [2030]})
w("no_match_pop.json", {"measure": "average_minutes_per_day", "activity": {"preset": "sleep"}, "years": [2023], "population": {"state_fips": "99"}})
w("compare_conflict.json", {"measure": "average_minutes_per_day", "activity": {"preset": "sleep"}, "years": [2023], "population": {"sex": "male"}, "group_a": {"sex": "female"}, "group_b": {}})
w("compare_ok.json", {"measure": "average_minutes_per_day", "activity": {"preset": "sleep"}, "years": [2023], "group_a": {"sex": "male"}, "group_b": {"sex": "female"}})
w("compare_label_long.json", {"measure": "average_minutes_per_day", "activity": {"preset": "sleep"}, "years": [2023], "group_a": {"sex": "male"}, "group_b": {"sex": "female"}, "label_a": "<script>alert(1)</script>"})
print("ok")
