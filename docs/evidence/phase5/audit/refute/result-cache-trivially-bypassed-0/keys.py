from atus_pipeline.api.schemas.analysis import EstimateRequest
from atus_pipeline.api.cache import cache_key
base={"activity":{"include":["0101"]},"years":[2023]}
vars={"base":base,
 "label_a":{"activity":{"include":["0101"],"label":"a"},"years":[2023]},
 "label_b":{"activity":{"include":["0101"],"label":"b"},"years":[2023]},
 "dupes":{"activity":{"include":["0101","0101","0101"]},"years":[2023]},
 "cl":{"activity":{"include":["0101"]},"years":[2023],"confidence_level":0.9500001},
 "leaf":{"activity":{"include":["010101","010102","010199"]},"years":[2023]},
 "reorder_years":{"activity":{"include":["0101"]},"years":[2023]},
}
for k,v in vars.items():
    s=EstimateRequest(**v).to_spec()
    print(k, cache_key("estimate", s.to_dict(), "X", "Y")[:16], s.to_dict()["activity"], s.confidence_level)
