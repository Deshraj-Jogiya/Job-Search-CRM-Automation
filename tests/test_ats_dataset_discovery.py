"""Parsing logic for the ats-scrapers open dataset's unified
companies.csv -- pure, no network call. The real header/row shape
(captured directly from storage.stapply.ai/jobhive/v1/companies.csv
during research) is ats,name,slug,url covering all 65 platforms the
dataset tracks; only the 5 board_discovery.py knows how to probe are
kept, everything else is dropped.
"""

from app.services.ats_dataset_discovery import parse_companies_csv

_SAMPLE_CSV = (
    "ats,name,slug,url\n"
    "greenhouse,1upHealth,1uphealth,https://job-boards.greenhouse.io/1uphealth\n"
    "lever,15Five,15five,https://jobs.lever.co/15five\n"
    "ashby,0x,0x,https://jobs.ashbyhq.com/0x\n"
    "recruitee,12Build,12build,https://12build.recruitee.com\n"
    "personio,1NCE,1nce,https://1nce.jobs.personio.com\n"
    "adp,626,2b2a5668-937b-44ec-a477-89cd0fc7bb42/19000101_000001,https://workforcenow.adp.com/x\n"
)


def test_parses_a_row_for_each_supported_ats():
    result = parse_companies_csv(_SAMPLE_CSV)
    assert result["greenhouse"] == [("1upHealth", "1uphealth")]
    assert result["lever"] == [("15Five", "15five")]
    assert result["ashby"] == [("0x", "0x")]
    assert result["recruitee"] == [("12Build", "12build")]
    assert result["personio"] == [("1NCE", "1nce")]


def test_unsupported_ats_platform_is_dropped():
    result = parse_companies_csv(_SAMPLE_CSV)
    assert "adp" not in result


def test_all_five_supported_ats_keys_always_present_even_when_empty():
    result = parse_companies_csv("ats,name,slug,url\n")
    assert set(result.keys()) == {"greenhouse", "lever", "ashby", "recruitee", "personio"}
    assert all(pairs == [] for pairs in result.values())


def test_row_missing_name_or_slug_is_skipped():
    csv_text = (
        "ats,name,slug,url\n"
        "greenhouse,,somejob,https://job-boards.greenhouse.io/somejob\n"
        "greenhouse,Acme,,https://job-boards.greenhouse.io/acme\n"
    )
    result = parse_companies_csv(csv_text)
    assert result["greenhouse"] == []
