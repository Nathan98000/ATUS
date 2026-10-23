from atus_pipeline.api.schemas.analysis import EstimateRequest
from atus_pipeline.api.cache import cache_key
bodies=[{"activity":{"include":["0101"]},"years":[2023]},
{"activity":{"include":["0101"],"label":"a"},"years":[2023]},
{"activity":{"include":["0101"],"label":"b"},"years":[2023]},
{"activity":{"include":["0101","0101"]},"years":[2023]},
{"activity":{"include":["0101","0101","0101"]},"years":[2023]},
{"activity":{"include":["0101"]},"years":[2023],"confidence_level":0.9500001},
{"activity":{"include":["0101"]},"years":[2023],"confidence_level":0.95000000001},
{"activity":{"include":["010101","010102","010199"]},"years":[2023]},
{"activity":{"include":["0101"]},"years":[2023],"variance":"none"},
]
for b in bodies:
    s=EstimateRequest(**b).to_spec()
    print(cache_key("estimate",s.to_dict(),"v","d")[:16], b)
