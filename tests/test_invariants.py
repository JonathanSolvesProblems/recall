"""Unit tests for the logic that does not need a cluster.

The cluster-dependent behaviour is covered by scripts/smoke.py, expiry.py and
concurrency.py, which run against a real 9-node deployment because that is the
only place transaction and replication behaviour is real. What is left here is
pure logic, and it is worth pinning because each of these encodes a defect that
actually happened.
"""

from __future__ import annotations

from datetime import datetime, timezone

from bench.longmemeval import canonical_key, parse_date
from unsay.ingest import Claim, lots_in, normalize_subject
from unsay.sweep import dedupe_key


class TestDedupeKey:
    """The exactly-once guarantee is only as good as this being deterministic."""

    def _stale(self, v=1):
        return [{"fact_key": "recall:x:LOT-1:D-1", "read_version": v}]

    def test_identical_inputs_give_identical_keys(self):
        a = dedupe_key("d1", "stop", self._stale())
        b = dedupe_key("d1", "stop", self._stale())
        assert a == b, "a replayed sweep must recompute the same key"

    def test_order_of_stale_claims_does_not_matter(self):
        s1 = [{"fact_key": "a", "read_version": 1}, {"fact_key": "b", "read_version": 2}]
        s2 = list(reversed(s1))
        assert dedupe_key("d1", "stop", s1) == dedupe_key("d1", "stop", s2)

    def test_different_verdict_is_a_different_notification(self):
        assert dedupe_key("d1", "stop", self._stale()) != dedupe_key("d1", "caution", self._stale())

    def test_different_evidence_version_is_a_different_notification(self):
        assert dedupe_key("d1", "stop", self._stale(1)) != dedupe_key("d1", "stop", self._stale(2))


class TestContentHash:
    """Re-ingesting unchanged FDA data must not manufacture versions."""

    def _claim(self, **kw):
        base = dict(
            fact_key="recall:x", subject_kind="lot", subject_id="x", predicate="recall",
            claim="text", severity="class_ii",
            valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc), valid_to=None,
            source="openfda:enforcement", source_ref="D-1",
        )
        base.update(kw)
        return Claim(**base)

    def test_same_content_hashes_equal(self):
        assert self._claim().content_hash == self._claim().content_hash

    def test_source_ref_does_not_affect_the_hash(self):
        # Identity of the claim's content, not of the record that carried it.
        assert self._claim(source_ref="D-1").content_hash == self._claim(source_ref="D-2").content_hash

    def test_changed_severity_changes_the_hash(self):
        assert self._claim().content_hash != self._claim(severity="class_i").content_hash

    def test_changed_validity_window_changes_the_hash(self):
        later = datetime(2026, 6, 1, tzinfo=timezone.utc)
        assert self._claim().content_hash != self._claim(valid_to=later).content_hash


class TestCanonicalKey:
    """A qualifier appended to a known attribute is a new value, not a new attribute."""

    def test_value_suffix_snaps_back_to_the_parent(self):
        assert canonical_key("recent_trip_destination_paris", ["recent_trip_destination"]) \
            == "recent_trip_destination"

    def test_exact_match_is_preserved(self):
        assert canonical_key("car_make_model", ["car_make_model"]) == "car_make_model"

    def test_genuinely_new_attribute_is_left_alone(self):
        assert canonical_key("pet_dog_name", ["car_make_model"]) == "pet_dog_name"

    def test_longest_matching_parent_wins(self):
        existing = ["trip", "trip_destination"]
        assert canonical_key("trip_destination_paris", existing) == "trip_destination"

    def test_similar_prefix_without_separator_is_not_a_match(self):
        # "car_make_modelling" is not a value of "car_make_model".
        assert canonical_key("car_make_modelling", ["car_make_model"]) == "car_make_modelling"


class TestLotExtraction:
    """Lot resolution is what makes a recall reach a person rather than a drug."""

    def test_extracts_a_plain_lot(self):
        assert "AC2040A" in lots_in("Recalled: tablets, Lot AC2040A, exp 2027")

    def test_extracts_with_a_hash_prefix(self):
        assert "GB01616" in lots_in("lot #GB01616 affected")

    def test_is_case_insensitive_and_normalises_upward(self):
        assert "PG4360" in lots_in("batch no. pg4360")

    def test_no_lot_yields_nothing_rather_than_a_guess(self):
        # Drug-level scope over-notifies; a wrong lot under-notifies silently.
        assert lots_in("All lots of this product are affected") == []


class TestNormalizeSubject:
    def test_lowercases_and_hyphenates(self):
        assert normalize_subject("Amlodipine Besylate") == "amlodipine-besylate"

    def test_collapses_punctuation(self):
        assert normalize_subject("Aspirin, 325 mg (tablets)") == "aspirin-325-mg-tablets"

    def test_is_stable_under_whitespace(self):
        assert normalize_subject("  Ibuprofen  ") == normalize_subject("Ibuprofen")


class TestParseDate:
    def test_parses_longmemeval_format(self):
        assert parse_date("2023/05/25 (Thu) 20:21") == datetime(2023, 5, 25, 20, 21, tzinfo=timezone.utc)

    def test_unparseable_does_not_raise(self):
        assert isinstance(parse_date("not a date"), datetime)
