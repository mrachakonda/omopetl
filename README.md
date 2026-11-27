# omopetl

Build a small OMOP CDM extract from MIMIC-IV hospital files: person, visit, death, observation period, and diagnoses mapped to standard condition concepts.

Put MIMIC-IV `hosp` files in `hosp/` and an Athena vocabulary unzip in `vocabulary/`. Then:

```text
python build_omop.py
```

Output goes to `omop/`. MIMIC, Athena, and the generated CSVs are not in git.
