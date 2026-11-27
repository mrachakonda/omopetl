import csv
from pathlib import Path

import pytest

import build_omop as etl


def test_dotted_icd9_keeps_leading_zero():
    assert etl.dotted_icd9("04119") == "041.19"
    assert etl.dotted_icd9("25000") == "250.00"
    assert etl.dotted_icd9("E8809") == "E880.9"


def test_dotted_icd10():
    assert etl.dotted_icd10("E119") == "E11.9"
    assert etl.dotted_icd10("I214") == "I21.4"


def test_map_race_ethnicity():
    assert etl.map_race_ethnicity("WHITE") == (etl.RACE_WHITE, etl.ETHNICITY_NOT_HISPANIC)
    assert etl.map_race_ethnicity("HISPANIC/LATINO")[1] == etl.ETHNICITY_HISPANIC
    assert etl.map_race_ethnicity("UNKNOWN") == (0, 0)


def test_date_part():
    assert etl.date_part("2180-01-15 08:00:00") == "2180-01-15"
    assert etl.date_part("") == ""


def test_resolve_code_unions_one_to_many_maps():
    codes = {("ICD9CM", "041.19"): [(100, True)]}
    maps = {100: {10, 11}}
    source_id, targets = etl.resolve_code(codes, maps, "ICD9CM", "041.19")
    assert source_id == 100
    assert set(targets) == {10, 11}


def test_resolve_code_missing_icd():
    source_id, targets = etl.resolve_code({}, {}, "ICD9CM", "999.99")
    assert source_id == 0
    assert targets == []


def _write_vocab(path: Path) -> None:
    path.mkdir()
    with (path / "CONCEPT.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "concept_id",
                "concept_code",
                "vocabulary_id",
                "domain_id",
                "standard_concept",
                "invalid_reason",
            ],
            delimiter="\t",
        )
        writer.writeheader()
        rows = [
            ("100", "041.19", "ICD9CM", "Condition", "", ""),
            ("200", "250.00", "ICD9CM", "Condition", "", ""),
            ("300", "E11.9", "ICD10CM", "Condition", "", ""),
            ("400", "572.3", "ICD9CM", "Condition", "", ""),
            ("10", "A", "SNOMED", "Condition", "S", ""),
            ("11", "B", "SNOMED", "Condition", "S", ""),
            ("12", "C", "SNOMED", "Condition", "S", ""),
            ("201826", "E11.9-std", "SNOMED", "Condition", "S", ""),
        ]
        for row in rows:
            writer.writerow(
                dict(
                    zip(
                        [
                            "concept_id",
                            "concept_code",
                            "vocabulary_id",
                            "domain_id",
                            "standard_concept",
                            "invalid_reason",
                        ],
                        row,
                    )
                )
            )
    with (path / "CONCEPT_RELATIONSHIP.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "concept_id_1",
                "concept_id_2",
                "relationship_id",
                "invalid_reason",
            ],
            delimiter="\t",
        )
        writer.writeheader()
        for source, target in (
            ("100", "10"),
            ("100", "11"),
            ("200", "201826"),
            ("300", "201826"),
            ("400", "12"),
        ):
            writer.writerow(
                {
                    "concept_id_1": source,
                    "concept_id_2": target,
                    "relationship_id": "Maps to",
                    "invalid_reason": "",
                }
            )


def _write_hosp(path: Path) -> None:
    path.mkdir()
    (path / "patients.csv").write_text(
        "subject_id,gender,anchor_year,anchor_age,dod\n"
        "1,F,2150,40,\n"
        "2,M,2140,91,2150-06-01\n",
        encoding="utf-8",
    )
    (path / "admissions.csv").write_text(
        "subject_id,hadm_id,admittime,dischtime,race,admission_location,discharge_location\n"
        "1,10,2150-01-01 00:00:00,2150-01-05 00:00:00,WHITE,ED,HOME\n"
        "2,20,2150-02-01 00:00:00,2150-02-03 00:00:00,UNKNOWN,ED,HOME\n",
        encoding="utf-8",
    )
    (path / "diagnoses_icd.csv").write_text(
        "subject_id,hadm_id,seq_num,icd_code,icd_version\n"
        "1,10,1,04119,9\n"
        "1,10,2,E119,10\n"
        "1,99,1,99999,9\n"
        "2,20,1,25000,9\n",
        encoding="utf-8",
    )


@pytest.fixture
def extract(tmp_path, monkeypatch):
    hosp = tmp_path / "hosp"
    vocab = tmp_path / "vocabulary"
    out = tmp_path / "omop"
    _write_hosp(hosp)
    _write_vocab(vocab)
    out.mkdir()
    monkeypatch.setattr(etl, "HOSP", hosp)
    monkeypatch.setattr(etl, "VOCAB", vocab)
    monkeypatch.setattr(etl, "OUT", out)
    return hosp, vocab, out


def test_synthetic_extract_maps_one_to_many_and_keeps_unmapped(extract):
    hosp, vocab, out = extract
    codes, maps = etl.load_vocabulary()
    etl.sanity_check(codes, maps)
    visits, race_source, spans, visit_rows = etl.write_visits(
        hosp / "admissions.csv", out / "visit_occurrence.csv"
    )
    assert visit_rows == 2
    person_rows, death_rows = etl.write_person_death(
        hosp / "patients.csv",
        out / "person.csv",
        out / "death.csv",
        race_source,
        spans,
    )
    assert person_rows == 2
    assert death_rows == 1
    etl.write_observation_periods(out / "observation_period.csv", spans)
    stats = etl.write_conditions(codes, maps, visits, out / "condition_occurrence.csv")

    with (out / "condition_occurrence.csv").open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    staph = [row for row in rows if row["condition_source_value"] == "04119"]
    assert {row["condition_concept_id"] for row in staph} == {"10", "11"}
    assert staph[0]["condition_source_concept_id"] == "100"
    assert staph[0]["visit_occurrence_id"] == "10"

    unmatched = [row for row in rows if row["visit_occurrence_id"] == "99"]
    assert unmatched[0]["condition_source_value"] == "99999"
    assert unmatched[0]["condition_concept_id"] == "0"
    assert unmatched[0]["condition_start_date"] == ""

    assert stats["rows_with_multiple_maps"] == 1
    assert stats["missing_visit"] == 1
    assert stats["source_code_not_found"] == 1
