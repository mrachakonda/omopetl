# omopetl

Convert MIMIC-IV **hosp** tables into a small OMOP CDM 5.4 extract that KEEP can read: people, inpatient visits, deaths, observation periods, and billed diagnoses mapped to standard Condition concepts.

This is not a full MIMIC → OMOP ETL. Labs, drugs, notes, ICU stays, and procedures are out of scope. Procedure vocabularies are not in the Athena bundle this script expects.

MIMIC-IV and Athena files are **not in git**. See [hosp/README.md](hosp/README.md) and [vocabulary/README.md](vocabulary/README.md). Generated OMOP CSVs stay in [omop/](omop/README.md) and must not be committed.

```text
python build_omop.py
```

The script looks in `hosp/` and `vocabulary/` here first. If those folders are empty, it uses the sibling `KeepETL/hosp` and `KeepETL/vocabulary` directories.

## Why OMOP

MIMIC stores diagnoses as ICD-9-CM and ICD-10-CM billing codes (`diagnoses_icd`). KEEP (Elhussein et al., CHIL 2025) works on **OMOP condition concepts** and the SNOMED “is-a” graph, not raw ICD strings.

OMOP is a common data model plus a vocabulary:

1. Each person, visit, and diagnosis is a row in a fixed table.
2. Meaning lives in integer **concept ids** from Athena, not in local strings.
3. Source codes (ICD) are kept, and **Maps to** links them to a standard concept (usually SNOMED in the Condition domain).

This ETL builds only the tables needed to stand up that condition history.

## What maps to what

| MIMIC-IV | OMOP table | Rule |
|---|---|---|
| `patients.subject_id` | `person.person_id` | One row per patient. |
| `patients.gender` | `gender_concept_id` | `M` → 8507, `F` → 8532, else 0. |
| `anchor_year - anchor_age` | `year_of_birth` | MIMIC ages over 89 are already 91. |
| `admissions.race` | `race_concept_id` / `ethnicity_concept_id` | First known race across that person’s admissions; Hispanic/Latino is ethnicity. |
| `patients.dod` | `death` | Written only when a date of death is present. |
| `admissions.hadm_id` | `visit_occurrence_id` | One inpatient visit (concept 9201) per hospital admission. |
| Admission / discharge times | visit start / end | Also used as the condition interval. |
| Span of all admissions (and death) | `observation_period` | One period per person. |
| `diagnoses_icd` | `condition_occurrence` | One billing diagnosis (type 32019). `seq_num` 1 is primary, later rows secondary. |

A diagnosis whose `hadm_id` has no matching admission is **kept**, with empty timestamps and no visit id. That row is not dropped.

## Diagnosis mapping (the important part)

MIMIC ICD codes have **no dots** (`E119`, `25000`, `04119`). Athena `concept_code` values **are** dotted (`E11.9`, `250.00`, `041.19`). The script adds dots, then looks up `(ICD9CM or ICD10CM, dotted code)` in `CONCEPT.csv`.

From that source concept it follows valid Athena **Maps to** links whose target is a **standard Condition** concept (`domain_id = Condition`, `standard_concept = S`).

```text
MIMIC icd_code + icd_version
        → dotted ICD string
        → ICD source concept_id
        → Maps to → one or more standard condition_concept_id
```

Outcomes:

- **One Maps-to target:** one `condition_occurrence` row. `condition_concept_id` is the standard concept. `condition_source_value` is the original MIMIC ICD string. `condition_source_concept_id` is the ICD concept.
- **Several Maps-to targets:** one output row **per target**, same diagnosis, new `condition_occurrence_id` each time. Downstream KEEP counting should treat those as **one diagnosis**, not several.
- **ICD found, no standard Condition map:** one row with `condition_concept_id = 0` (OMOP “unmapped”). The source ICD is still stored.
- **ICD not in the vocabulary:** same unmapped row (`concept_id` 0). The run prints the most common missing codes.

A short sanity check runs before the write: `250.00`, `E11.9`, and `572.3` must resolve to at least one standard condition.

## What this extract is for

KEEP’s later steps (not this repo) are:

1. Limit the SNOMED condition graph to about five levels under “Disease” (concept 4274025).
2. Roll each mapped condition up to those retained parents.
3. Call a disease **present** if it appears at least twice in the patient’s full history (Appendix A.4).

This ETL only produces the CDM rows those steps consume. It does not build the graph, roll up ancestors, or train embeddings.

## License reminder

MIMIC-IV is a PhysioNet credentialed dataset. Athena vocabularies are OHDSI-licensed. Neither, nor the OMOP CSVs derived from them, may be published in a public git repository. `.gitignore` blocks those files; only this script and the folder READMEs are meant to be committed.
