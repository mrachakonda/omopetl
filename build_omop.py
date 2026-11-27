"""Build an OMOP CDM 5.4 extract from MIMIC-IV hosp tables.

Uses the Athena vocabulary in vocabulary/ to map ICD-9-CM and ICD-10-CM
billing diagnoses to standard Condition concepts. Person and visit rows
come from patients.csv and admissions.csv. Procedure vocabularies are
not in this download, so procedure_occurrence is not built.
"""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_ROOT = ROOT.parent
HOSP = DATA_ROOT / "hosp"
VOCAB = DATA_ROOT / "vocabulary"
OUT = DATA_ROOT / "omop"

GENDER_MALE = 8507
GENDER_FEMALE = 8532
RACE_WHITE = 8527
RACE_BLACK = 8516
RACE_ASIAN = 8515
RACE_AIAN = 8657
RACE_NHPI = 8557
RACE_OTHER = 8522
ETHNICITY_HISPANIC = 38003563
ETHNICITY_NOT_HISPANIC = 38003564

INPATIENT_VISIT = 9201
EHR_RECORD = 32817
BILLING_DIAGNOSIS = 32019
PRIMARY_DIAGNOSIS = 32902
SECONDARY_DIAGNOSIS = 32908

UNKNOWN_RACE = {"", "UNKNOWN", "UNABLE TO OBTAIN", "PATIENT DECLINED TO ANSWER"}


def dotted_icd9(code: str) -> str:
    code = code.strip().upper().replace(".", "")
    if len(code) <= 3 or (code.startswith("E") and len(code) <= 4):
        return code
    if code.startswith("E"):
        return code[:4] + "." + code[4:]
    return code[:3] + "." + code[3:]


def dotted_icd10(code: str) -> str:
    code = code.strip().upper().replace(".", "")
    if len(code) <= 3:
        return code
    return code[:3] + "." + code[3:]


def map_race_ethnicity(value: str) -> tuple[int, int]:
    race = (value or "").strip().upper()
    if race in UNKNOWN_RACE:
        return 0, 0
    hispanic = "HISPANIC" in race or "LATINO" in race or race == "SOUTH AMERICAN"
    ethnicity = ETHNICITY_HISPANIC if hispanic else ETHNICITY_NOT_HISPANIC
    if race.startswith("WHITE") or race == "PORTUGUESE":
        race_id = RACE_WHITE
    elif race.startswith("BLACK"):
        race_id = RACE_BLACK
    elif race.startswith("ASIAN"):
        race_id = RACE_ASIAN
    elif "AMERICAN INDIAN" in race or "ALASKA" in race:
        race_id = RACE_AIAN
    elif "HAWAIIAN" in race or "PACIFIC" in race:
        race_id = RACE_NHPI
    elif race == "OTHER":
        race_id = RACE_OTHER
    else:
        race_id = 0
    return race_id, ethnicity


def date_part(value: str) -> str:
    value = (value or "").strip()
    return value[:10] if value else ""


def load_vocabulary() -> tuple[dict[tuple[str, str], list[tuple[int, bool]]], dict[int, set[int]]]:
    """Return ICD code lookup and Maps-to targets that are standard conditions."""
    print("Reading CONCEPT.csv ...", flush=True)
    codes: dict[tuple[str, str], list[tuple[int, bool]]] = defaultdict(list)
    standard_condition: set[int] = set()
    icd_ids: set[int] = set()
    with (VOCAB / "CONCEPT.csv").open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        for row in reader:
            concept_id = int(row["concept_id"])
            if row["domain_id"] == "Condition" and row["standard_concept"] == "S":
                standard_condition.add(concept_id)
            if row["vocabulary_id"] not in ("ICD9CM", "ICD10CM"):
                continue
            valid = row["invalid_reason"] == ""
            codes[(row["vocabulary_id"], row["concept_code"])].append((concept_id, valid))
            icd_ids.add(concept_id)
    print(
        f"  ICD codes {sum(len(v) for v in codes.values()):,} concept rows, "
        f"{len(codes):,} distinct codes, {len(standard_condition):,} standard conditions",
        flush=True,
    )

    print("Reading CONCEPT_RELATIONSHIP.csv ...", flush=True)
    maps: dict[int, set[int]] = defaultdict(set)
    with (VOCAB / "CONCEPT_RELATIONSHIP.csv").open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        for row in reader:
            if row["relationship_id"] != "Maps to" or row["invalid_reason"] != "":
                continue
            source_id = int(row["concept_id_1"])
            if source_id not in icd_ids:
                continue
            target_id = int(row["concept_id_2"])
            if target_id in standard_condition:
                maps[source_id].add(target_id)
    print(f"  ICD concepts with a standard condition map: {len(maps):,}", flush=True)
    return codes, maps


def resolve_code(
    codes: dict[tuple[str, str], list[tuple[int, bool]]],
    maps: dict[int, set[int]],
    vocabulary_id: str,
    concept_code: str,
) -> tuple[int, list[int]]:
    entries = codes.get((vocabulary_id, concept_code), [])
    if not entries:
        return 0, []
    valid_ids = [concept_id for concept_id, valid in entries if valid]
    invalid_ids = [concept_id for concept_id, valid in entries if not valid]
    source_ids = valid_ids or invalid_ids
    source_concept_id = source_ids[0]
    targets: list[int] = []
    seen: set[int] = set()
    for concept_id in source_ids:
        for target_id in maps.get(concept_id, ()):
            if target_id not in seen:
                seen.add(target_id)
                targets.append(target_id)
    if not targets and valid_ids and invalid_ids:
        for concept_id in invalid_ids:
            for target_id in maps.get(concept_id, ()):
                if target_id not in seen:
                    seen.add(target_id)
                    targets.append(target_id)
    return source_concept_id, targets


def sanity_check(codes, maps) -> None:
    expected = {
        ("ICD9CM", "250.00"): dotted_icd9("25000"),
        ("ICD10CM", "E11.9"): dotted_icd10("E119"),
        ("ICD9CM", "572.3"): dotted_icd9("5723"),
    }
    print("Sanity check", flush=True)
    for (vocabulary_id, concept_code), converted in expected.items():
        if converted != concept_code:
            raise SystemExit(f"Dot conversion failed: {converted} != {concept_code}")
        source_id, targets = resolve_code(codes, maps, vocabulary_id, concept_code)
        print(f"  {vocabulary_id} {concept_code} -> source {source_id}, conditions {targets[:5]}", flush=True)
        if source_id == 0 or not targets:
            raise SystemExit(f"Expected a condition map for {vocabulary_id} {concept_code}")


def write_visits(admissions_path: Path, visit_path: Path):
    print("Reading admissions and writing visit_occurrence ...", flush=True)
    by_person: dict[str, list[dict[str, str]]] = defaultdict(list)
    with admissions_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            by_person[row["subject_id"]].append(row)

    visits: dict[str, tuple[str, str, str]] = {}
    race_source: dict[str, str] = {}
    spans: dict[str, tuple[str, str]] = {}
    visit_rows = 0
    with visit_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "visit_occurrence_id",
                "person_id",
                "visit_concept_id",
                "visit_start_date",
                "visit_start_datetime",
                "visit_end_date",
                "visit_end_datetime",
                "visit_type_concept_id",
                "provider_id",
                "care_site_id",
                "visit_source_value",
                "visit_source_concept_id",
                "admitted_from_concept_id",
                "admitted_from_source_value",
                "discharged_to_concept_id",
                "discharged_to_source_value",
                "preceding_visit_occurrence_id",
            ]
        )
        for subject_id, rows in by_person.items():
            rows.sort(key=lambda row: (row["admittime"], row["hadm_id"]))
            chosen_race = ""
            for row in rows:
                if row["race"].strip().upper() not in UNKNOWN_RACE:
                    chosen_race = row["race"]
                    break
            if not chosen_race:
                chosen_race = rows[0]["race"]
            race_source[subject_id] = chosen_race

            start_dates = [date_part(row["admittime"]) for row in rows if date_part(row["admittime"])]
            end_dates = [date_part(row["dischtime"]) or date_part(row["admittime"]) for row in rows]
            end_dates = [value for value in end_dates if value]
            if start_dates and end_dates:
                spans[subject_id] = (min(start_dates), max(end_dates))

            previous = ""
            for row in rows:
                start_dt = row["admittime"].strip()
                end_dt = row["dischtime"].strip()
                start_date = date_part(start_dt)
                end_date = date_part(end_dt) or start_date
                visits[row["hadm_id"]] = (subject_id, start_dt, end_dt)
                writer.writerow(
                    [
                        row["hadm_id"],
                        subject_id,
                        INPATIENT_VISIT,
                        start_date,
                        start_dt,
                        end_date,
                        end_dt,
                        EHR_RECORD,
                        "",
                        "",
                        row["hadm_id"],
                        0,
                        0,
                        row["admission_location"],
                        0,
                        row["discharge_location"],
                        previous,
                    ]
                )
                previous = row["hadm_id"]
                visit_rows += 1
    print(f"  visits {visit_rows:,}", flush=True)
    return visits, race_source, spans, visit_rows


def write_person_death(patients_path: Path, person_path: Path, death_path: Path, race_source, spans):
    print("Writing person and death ...", flush=True)
    person_rows = 0
    death_rows = 0
    with patients_path.open(newline="", encoding="utf-8") as source, person_path.open(
        "w", newline="", encoding="utf-8"
    ) as person_handle, death_path.open("w", newline="", encoding="utf-8") as death_handle:
        person_writer = csv.writer(person_handle)
        death_writer = csv.writer(death_handle)
        person_writer.writerow(
            [
                "person_id",
                "gender_concept_id",
                "year_of_birth",
                "month_of_birth",
                "day_of_birth",
                "birth_datetime",
                "race_concept_id",
                "ethnicity_concept_id",
                "location_id",
                "provider_id",
                "care_site_id",
                "person_source_value",
                "gender_source_value",
                "gender_source_concept_id",
                "race_source_value",
                "race_source_concept_id",
                "ethnicity_source_value",
                "ethnicity_source_concept_id",
            ]
        )
        death_writer.writerow(
            [
                "person_id",
                "death_date",
                "death_datetime",
                "death_type_concept_id",
                "cause_concept_id",
                "cause_source_value",
                "cause_source_concept_id",
            ]
        )
        for row in csv.DictReader(source):
            gender = row["gender"].strip().upper()
            gender_concept_id = GENDER_MALE if gender == "M" else GENDER_FEMALE if gender == "F" else 0
            race_value = race_source.get(row["subject_id"], "")
            race_concept_id, ethnicity_concept_id = map_race_ethnicity(race_value)
            year_of_birth = int(row["anchor_year"]) - int(row["anchor_age"])
            person_writer.writerow(
                [
                    row["subject_id"],
                    gender_concept_id,
                    year_of_birth,
                    "",
                    "",
                    "",
                    race_concept_id,
                    ethnicity_concept_id,
                    "",
                    "",
                    "",
                    row["subject_id"],
                    gender,
                    0,
                    race_value,
                    0,
                    race_value,
                    0,
                ]
            )
            person_rows += 1
            dod = date_part(row["dod"])
            if dod:
                death_writer.writerow([row["subject_id"], dod, "", EHR_RECORD, 0, "", 0])
                death_rows += 1
                span = spans.get(row["subject_id"])
                if span:
                    spans[row["subject_id"]] = (min(span[0], dod), max(span[1], dod))
                else:
                    spans[row["subject_id"]] = (dod, dod)
    print(f"  persons {person_rows:,}  deaths {death_rows:,}", flush=True)
    return person_rows, death_rows


def write_observation_periods(path: Path, spans) -> int:
    print("Writing observation_period ...", flush=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "observation_period_id",
                "person_id",
                "observation_period_start_date",
                "observation_period_end_date",
                "period_type_concept_id",
            ]
        )
        for period_id, (subject_id, (start_date, end_date)) in enumerate(sorted(spans.items()), start=1):
            writer.writerow([period_id, subject_id, start_date, end_date, EHR_RECORD])
    print(f"  observation periods {len(spans):,}", flush=True)
    return len(spans)


def write_conditions(codes, maps, visits, condition_path: Path) -> dict[str, int]:
    print("Writing condition_occurrence ...", flush=True)
    stats = Counter()
    unmapped = Counter()
    diagnosis_path = HOSP / "diagnoses_icd.csv"
    with diagnosis_path.open(newline="", encoding="utf-8") as source, condition_path.open(
        "w", newline="", encoding="utf-8", buffering=1024 * 1024
    ) as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "condition_occurrence_id",
                "person_id",
                "condition_concept_id",
                "condition_start_date",
                "condition_start_datetime",
                "condition_end_date",
                "condition_end_datetime",
                "condition_type_concept_id",
                "condition_status_concept_id",
                "stop_reason",
                "provider_id",
                "visit_occurrence_id",
                "visit_detail_id",
                "condition_source_value",
                "condition_source_concept_id",
                "condition_status_source_value",
            ]
        )
        next_id = 1
        for row in csv.DictReader(source):
            stats["diagnosis_rows"] += 1
            version = row["icd_version"].strip()
            raw_code = row["icd_code"].strip().upper()
            if version == "9":
                vocabulary_id = "ICD9CM"
                concept_code = dotted_icd9(raw_code)
            elif version == "10":
                vocabulary_id = "ICD10CM"
                concept_code = dotted_icd10(raw_code)
            else:
                vocabulary_id = ""
                concept_code = raw_code
            source_concept_id, targets = (
                resolve_code(codes, maps, vocabulary_id, concept_code) if vocabulary_id else (0, [])
            )
            if source_concept_id == 0:
                stats["source_code_not_found"] += 1
                unmapped[f"{version}:{raw_code}"] += 1
            if not targets:
                targets = [0]
                stats["rows_without_standard_condition"] += 1
            else:
                stats["rows_with_standard_condition"] += 1
                if len(targets) > 1:
                    stats["rows_with_multiple_maps"] += 1

            visit = visits.get(row["hadm_id"])
            if visit is None:
                stats["missing_visit"] += 1
                start_dt = ""
                end_dt = ""
                start_date = ""
                end_date = ""
            else:
                _, start_dt, end_dt = visit
                start_date = date_part(start_dt)
                end_date = date_part(end_dt) or start_date
            seq = row["seq_num"].strip()
            if seq == "1":
                status_concept_id = PRIMARY_DIAGNOSIS
            elif seq:
                status_concept_id = SECONDARY_DIAGNOSIS
            else:
                status_concept_id = 0
            for target_id in targets:
                writer.writerow(
                    [
                        next_id,
                        row["subject_id"],
                        target_id,
                        start_date,
                        start_dt,
                        end_date,
                        end_dt,
                        BILLING_DIAGNOSIS,
                        status_concept_id,
                        "",
                        "",
                        row["hadm_id"],
                        "",
                        raw_code,
                        source_concept_id,
                        seq,
                    ]
                )
                next_id += 1
                stats["condition_rows"] += 1
            if stats["diagnosis_rows"] % 1000000 == 0:
                print(f"  diagnoses {stats['diagnosis_rows']:,}", flush=True)
    stats["unmapped_codes"] = len(unmapped)
    print("Top unmapped ICD codes:", flush=True)
    for code, count in unmapped.most_common(15):
        print(f"  {count}\t{code}", flush=True)
        stats[f"unmapped:{code}"] = count
    return stats


def write_report(path: Path, stats: dict[str, int], visit_rows: int, person_rows: int, death_rows: int, period_rows: int) -> None:
    lines = [
        "OMOP CDM 5.4 extract from MIMIC-IV hosp tables",
        f"person rows: {person_rows}",
        f"death rows: {death_rows}",
        f"visit_occurrence rows: {visit_rows}",
        f"observation_period rows: {period_rows}",
        f"diagnosis rows: {stats['diagnosis_rows']}",
        f"condition_occurrence rows: {stats['condition_rows']}",
        f"diagnosis rows with a standard condition: {stats['rows_with_standard_condition']}",
        f"diagnosis rows without a standard condition: {stats['rows_without_standard_condition']}",
        f"diagnosis rows whose ICD code was not in the vocabulary: {stats['source_code_not_found']}",
        f"diagnosis rows mapped to more than one standard condition: {stats['rows_with_multiple_maps']}",
        f"distinct unmapped ICD codes: {stats['unmapped_codes']}",
        f"diagnosis rows with no matching admission: {stats['missing_visit']}",
        "",
        "concept ids used",
        f"gender male {GENDER_MALE}, female {GENDER_FEMALE}",
        f"inpatient visit {INPATIENT_VISIT}",
        f"visit and death type {EHR_RECORD} (EHR)",
        f"condition type {BILLING_DIAGNOSIS} (EHR billing diagnosis)",
        f"condition status primary {PRIMARY_DIAGNOSIS}, secondary {SECONDARY_DIAGNOSIS}",
        "",
        "Notes",
        "ICD codes are dotted to match Athena concept_code before lookup.",
        "A diagnosis is written once per standard Condition concept it maps to.",
        "Unmapped diagnoses are kept with condition_concept_id 0.",
        "year_of_birth is anchor_year - anchor_age. MIMIC sets ages over 89 to 91.",
        "procedure_occurrence was not built; ICD9Proc and ICD10PCS are not in this vocabulary download.",
        "Gender and race concept ids are the standard OMOP ids. This vocabulary extract does not include those demographic concept rows.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    OUT.mkdir(exist_ok=True)
    codes, maps = load_vocabulary()
    sanity_check(codes, maps)
    visits, race_source, spans, visit_rows = write_visits(HOSP / "admissions.csv", OUT / "visit_occurrence.csv")
    person_rows, death_rows = write_person_death(
        HOSP / "patients.csv", OUT / "person.csv", OUT / "death.csv", race_source, spans
    )
    period_rows = write_observation_periods(OUT / "observation_period.csv", spans)
    stats = write_conditions(codes, maps, visits, OUT / "condition_occurrence.csv")
    write_report(OUT / "build_report.txt", stats, visit_rows, person_rows, death_rows, period_rows)
    print("Done", flush=True)
    for key in (
        "diagnosis_rows",
        "condition_rows",
        "rows_with_standard_condition",
        "rows_without_standard_condition",
        "source_code_not_found",
        "rows_with_multiple_maps",
        "missing_visit",
    ):
        print(f"{key}: {stats[key]:,}", flush=True)


if __name__ == "__main__":
    main()
