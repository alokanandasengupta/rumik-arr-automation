"""
Tests for the pure/stateless helper functions in daily_funnel_job.py.

Deliberately scoped to functions that don't need a live Metabase connection
or a database -- the classification algorithm itself (main()) is exercised
in production every day and would need substantial DB mocking to unit test
meaningfully; these tests cover the smaller pieces that have actually caused
real bugs during development (see git history / DAILY_FUNNEL_AUTOMATION_HANDOFF.md
for the incidents each of these guards against).
"""
import json
from datetime import datetime

import pytest

import daily_funnel_job as job


class TestParseDt:
    def test_iso_with_tz_and_6digit_fraction(self):
        dt = job.parse_dt("2026-08-25T00:00:00.123456+05:30")
        assert dt == datetime(2026, 8, 25, 0, 0, 0, 123456)

    def test_iso_with_tz_stripped_to_naive(self):
        # tz-aware input must come back naive (tzinfo stripped), since the
        # rest of the pipeline compares datetimes as naive IST wall-clock.
        dt = job.parse_dt("2026-09-11T14:03:04+05:30")
        assert dt.tzinfo is None
        assert dt == datetime(2026, 9, 11, 14, 3, 4)

    def test_space_separated_no_timezone(self):
        # The actual bug hit in production: a `::text` cast on a
        # `timestamp without time zone` renders as 'YYYY-MM-DD HH:MI:SS.ffffff'
        # (space separator, no tz suffix) rather than ISO 'T'-separated.
        dt = job.parse_dt("2026-08-17 14:03:04.779060")
        assert dt == datetime(2026, 8, 17, 14, 3, 4, 779060)

    def test_five_digit_fractional_seconds(self):
        # The exact string that broke the naive regex the first time:
        # fractional seconds not padded to 6 digits, and no tz suffix.
        dt = job.parse_dt("2026-08-17 14:03:04.77906")
        assert dt == datetime(2026, 8, 17, 14, 3, 4, 779060)

    def test_z_suffix_normalized_to_utc_offset(self):
        dt = job.parse_dt("2026-09-11T00:00:00Z")
        assert dt == datetime(2026, 9, 11, 0, 0, 0)

    def test_no_fractional_seconds_at_all(self):
        dt = job.parse_dt("2026-09-11 09:15:30")
        assert dt == datetime(2026, 9, 11, 9, 15, 30)

    def test_none_input_returns_none(self):
        assert job.parse_dt(None) is None


class TestMergeById:
    def test_fresh_overrides_old_on_matching_id(self):
        old = [{"id": 1, "status": "pending"}, {"id": 2, "status": "done"}]
        fresh = [{"id": 1, "status": "captured"}]
        merged = job.merge_by_id(old, fresh, "id")
        by_id = {r["id"]: r for r in merged}
        assert by_id[1]["status"] == "captured"
        assert by_id[2]["status"] == "done"

    def test_fresh_only_ids_are_added(self):
        old = [{"id": 1}]
        fresh = [{"id": 2}]
        merged = job.merge_by_id(old, fresh, "id")
        assert {r["id"] for r in merged} == {1, 2}

    def test_empty_old_is_fine(self):
        merged = job.merge_by_id([], [{"id": 1}], "id")
        assert merged == [{"id": 1}]

    def test_is_idempotent_merging_same_fresh_twice(self):
        old = [{"id": 1, "v": "a"}]
        once = job.merge_by_id(old, [{"id": 1, "v": "b"}], "id")
        twice = job.merge_by_id(once, [{"id": 1, "v": "b"}], "id")
        assert once == twice


class TestNorm:
    def test_integer_valued_float_becomes_int(self):
        assert job.norm(999.0) == 999
        assert isinstance(job.norm(999.0), int)

    def test_non_integer_float_stays_float(self):
        assert job.norm(499.5) == 499.5
        assert isinstance(job.norm(499.5), float)

    def test_plain_int_stays_int(self):
        assert job.norm(89) == 89


class TestToDdmmyyyy:
    def test_basic_conversion(self):
        assert job.to_ddmmyyyy("2026-09-11") == "11/09/2026"

    def test_single_digit_day_and_month_preserved_zero_padded(self):
        assert job.to_ddmmyyyy("2026-01-05") == "05/01/2026"


class TestParseAttr:
    def test_none_returns_all_none(self):
        assert job.parse_attr(None) == (None, None, None, None)

    def test_empty_string_returns_all_none(self):
        assert job.parse_attr("") == (None, None, None, None)

    def test_dict_input_maps_swapped_meta_field_names(self):
        # Meta's own field naming is swapped from the human-facing hierarchy:
        # campaignGroupId/Name is the actual Campaign, campaignId/Name is
        # the actual Ad Set. This is intentional, not a bug -- see the
        # handoff doc.
        attr = {
            "campaignGroupId": "120242197544620043",
            "campaignGroupName": "scaling campaign - 999",
            "campaignId": "120250413577370043",
            "campaignName": "statics - Copy",
        }
        cgid, cgname, aid, aname = job.parse_attr(attr)
        assert cgid == "120242197544620043"
        assert cgname == "scaling campaign - 999"
        assert aid == "120250413577370043"
        assert aname == "statics - Copy"

    def test_json_string_input_is_parsed(self):
        attr_json = json.dumps({
            "campaignGroupId": "1",
            "campaignGroupName": "camp",
            "campaignId": "2",
            "campaignName": "adset",
        })
        cgid, cgname, aid, aname = job.parse_attr(attr_json)
        assert (cgid, cgname, aid, aname) == ("1", "camp", "2", "adset")

    def test_malformed_json_string_returns_all_none_not_raises(self):
        assert job.parse_attr("{not valid json") == (None, None, None, None)

    def test_missing_both_ids_returns_all_none(self):
        assert job.parse_attr({"someOtherField": "x"}) == (None, None, None, None)

    def test_ids_cast_to_string(self):
        # Metabase can return these as native ints; downstream code keys
        # dicts on these values, so they must always come back as str.
        attr = {"campaignGroupId": 12345, "campaignGroupName": "c", "campaignId": 6789, "campaignName": "a"}
        cgid, cgname, aid, aname = job.parse_attr(attr)
        assert cgid == "12345" and isinstance(cgid, str)
        assert aid == "6789" and isinstance(aid, str)


class TestAlreadyRanToday:
    def test_false_when_state_file_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(job, "STATE_PATH", str(tmp_path / "last_success_date.txt"))
        assert job.already_ran_today("2026-09-11") is False

    def test_true_when_state_matches_today(self, tmp_path, monkeypatch):
        state_path = tmp_path / "last_success_date.txt"
        state_path.write_text("2026-09-11")
        monkeypatch.setattr(job, "STATE_PATH", str(state_path))
        assert job.already_ran_today("2026-09-11") is True

    def test_false_when_state_is_a_different_day(self, tmp_path, monkeypatch):
        state_path = tmp_path / "last_success_date.txt"
        state_path.write_text("2026-09-10")
        monkeypatch.setattr(job, "STATE_PATH", str(state_path))
        assert job.already_ran_today("2026-09-11") is False

    def test_mark_success_then_already_ran_today_roundtrip(self, tmp_path, monkeypatch):
        state_path = tmp_path / "last_success_date.txt"
        monkeypatch.setattr(job, "STATE_PATH", str(state_path))
        job.mark_success("2026-09-12")
        assert job.already_ran_today("2026-09-12") is True
        assert job.already_ran_today("2026-09-13") is False


class TestLoadJson:
    def test_returns_default_when_file_missing(self, tmp_path):
        assert job.load_json(str(tmp_path / "nope.json"), []) == []
        assert job.load_json(str(tmp_path / "nope.json"), {"x": 1}) == {"x": 1}

    def test_loads_real_file(self, tmp_path):
        p = tmp_path / "data.json"
        p.write_text(json.dumps([{"a": 1}]))
        assert job.load_json(str(p), []) == [{"a": 1}]
