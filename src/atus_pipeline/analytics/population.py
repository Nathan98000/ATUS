"""Population filters -> parameterized SQL at the respondent grain.

Every condition applies to exactly one row per respondent:

* ``atus.respondents r`` — years, diary dates, day type, employment,
  household children;
* ``atus.household_members hm`` at ``lineno = 1`` — the respondent's own
  interview-time age and sex (the roster row for the respondent; published BLS
  tables use these interview-updated values);
* ``atus.cps_persons c`` at ``lineno = 1`` — region, state, education from the
  final CPS interview 2-5 months before the diary day.

Joins are added only when a dimension needs them, always on the (tucaseid,
lineno=1) primary key, so a respondent can never be duplicated. A filtered
dimension excludes respondents with NULL in that dimension by construction
(SQL comparisons with NULL are not true); this "missing is excluded, never
'no'" rule is part of the engine's population semantics and is tested.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .spec import PopulationFilter

# TELFS: 1 employed-at work, 2 employed-absent, 3 unemployed-on layoff,
# 4 unemployed-looking, 5 not in labor force.
_EMPLOYMENT_CODES = {
    "employed": (1, 2),
    "unemployed": (3, 4),
    "not_in_labor_force": (5,),
}

# PEEDUCA (CPS educational attainment): 31-38 below high-school diploma,
# 39 high-school graduate/GED, 40 some college no degree, 41-42 associate,
# 43 bachelor's, 44 master's, 45 professional school, 46 doctorate.
_EDUCATION_RANGES = {
    "less_than_high_school": (31, 38),
    "high_school": (39, 39),
    "some_college_or_associate": (40, 42),
    "bachelor_or_higher": (43, 46),
}

# TUDIARYDAY: 1 = Sunday ... 7 = Saturday.
_DAY_TYPE_CODES = {"weekend": (1, 7), "weekday": (2, 3, 4, 5, 6)}

_SEX_CODES = {"male": 1, "female": 2}


@dataclass
class PopulationSQL:
    conditions: list[str] = field(default_factory=list)
    params: dict[str, object] = field(default_factory=dict)
    needs_roster: bool = False
    needs_cps: bool = False
    warnings: list[str] = field(default_factory=list)


def build_population_sql(population: PopulationFilter) -> PopulationSQL:
    out = PopulationSQL()

    if population.age_min is not None:
        out.needs_roster = True
        out.conditions.append("hm.age >= %(age_min)s")
        out.params["age_min"] = population.age_min
    if population.age_max is not None:
        out.needs_roster = True
        out.conditions.append("hm.age <= %(age_max)s")
        out.params["age_max"] = population.age_max
    if (population.age_min is not None and population.age_min >= 80) or (
        population.age_max is not None and population.age_max >= 80
    ):
        out.warnings.append(
            "Roster age is topcoded (80 in 2003-04, 85 from 2005), so age bounds of "
            "80+ select topcoded groups, not exact ages."
        )

    if population.sex is not None:
        out.needs_roster = True
        out.conditions.append("hm.sex = %(sex)s")
        out.params["sex"] = _SEX_CODES[population.sex]

    if population.employment_status is not None:
        out.conditions.append("r.labor_force_status = ANY(%(employment_codes)s)")
        out.params["employment_codes"] = list(_EMPLOYMENT_CODES[population.employment_status])

    if population.has_household_children is not None:
        op = ">" if population.has_household_children else "="
        out.conditions.append(f"r.household_children {op} 0")

    if population.region is not None:
        out.needs_cps = True
        out.conditions.append("c.region = %(region)s")
        out.params["region"] = population.region

    if population.state_fips is not None:
        out.needs_cps = True
        out.conditions.append("c.state_fips = %(state_fips)s")
        out.params["state_fips"] = population.state_fips

    if population.education_level is not None:
        out.needs_cps = True
        lo, hi = _EDUCATION_RANGES[population.education_level]
        out.conditions.append("c.education BETWEEN %(education_lo)s AND %(education_hi)s")
        out.params.update(education_lo=lo, education_hi=hi)
        out.warnings.append(
            "Education comes from the CPS interview 2-5 months before the diary day "
            "and excludes respondents with missing CPS education."
        )

    if population.day_type is not None:
        out.conditions.append("r.diary_day_of_week = ANY(%(day_codes)s)")
        out.params["day_codes"] = list(_DAY_TYPE_CODES[population.day_type])
        out.warnings.append(
            "Day-type estimates represent the average such day (weights are "
            "day-of-week calibrated), not a share of the overall week."
        )

    if population.diary_date_min is not None:
        out.conditions.append("r.diary_date >= %(diary_date_min)s")
        out.params["diary_date_min"] = population.diary_date_min
    if population.diary_date_max is not None:
        out.conditions.append("r.diary_date <= %(diary_date_max)s")
        out.params["diary_date_max"] = population.diary_date_max

    return out


def describe_dimensions() -> list[dict]:
    """Machine-readable description of every supported population dimension.

    This is the domain-owned source of truth the API's population-metadata
    endpoint serves, derived from the same constants the filters use — so the
    advertised vocabulary can never drift from what filtering accepts. Every
    dimension shares one missing-data rule: filtering on it excludes
    respondents whose value is missing (never treated as "no").
    """
    return [
        {
            "name": "age_min", "type": "integer", "unit": "years",
            "description": "Inclusive lower age bound (interview-time roster age).",
            "notes": "Age is topcoded: 80 in 2003-04, 85 from 2005 onward.",
        },
        {
            "name": "age_max", "type": "integer", "unit": "years",
            "description": "Inclusive upper age bound (interview-time roster age).",
            "notes": "Age is topcoded: 80 in 2003-04, 85 from 2005 onward.",
        },
        {
            "name": "sex", "type": "category",
            "values": sorted(_SEX_CODES),
            "description": "Respondent sex from the ATUS household roster (TESEX).",
        },
        {
            "name": "employment_status", "type": "category",
            "values": sorted(_EMPLOYMENT_CODES),
            "description": "ATUS-interview labor force status (TELFS): employed {1,2}, "
                           "unemployed {3,4}, not_in_labor_force {5}.",
        },
        {
            "name": "has_household_children", "type": "boolean",
            "description": "Any household child under 18 present (TRCHILDNUM > 0). "
                           "Household children, not necessarily own children.",
        },
        {
            "name": "region", "type": "category",
            "values": [1, 2, 3, 4],
            "description": "Census region at the CPS interview (GEREG): 1 Northeast, "
                           "2 Midwest, 3 South, 4 West.",
            "notes": "Measured 2-5 months before the diary day.",
        },
        {
            "name": "state_fips", "type": "category",
            "description": "Two-digit state FIPS code at the CPS interview (GESTFIPS), "
                           "e.g. '06' for California.",
            "notes": "Measured 2-5 months before the diary day.",
        },
        {
            "name": "education_level", "type": "category",
            "values": sorted(_EDUCATION_RANGES),
            "description": "Educational attainment at the CPS interview (PEEDUCA), grouped.",
            "notes": "Measured 2-5 months before the diary day; missing education is "
                     "excluded when this filter is used.",
        },
        {
            "name": "day_type", "type": "category",
            "values": sorted(_DAY_TYPE_CODES),
            "description": "Diary day of week: weekend = Saturday/Sunday. Estimates then "
                           "represent the average such day.",
        },
        {
            "name": "diary_date_min", "type": "date",
            "description": "Inclusive lower bound on the diary date (YYYY-MM-DD) for "
                           "within-year period estimates.",
        },
        {
            "name": "diary_date_max", "type": "date",
            "description": "Inclusive upper bound on the diary date (YYYY-MM-DD).",
        },
    ]
