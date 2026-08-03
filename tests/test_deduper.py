import pyarrow as pa

from archiver.rollup import Deduper


class TestAccepts:
    def test_first_occurrence_accepted(self):
        d = Deduper(("a", "b"))
        assert d.accepts(("x", 1)) is True

    def test_repeat_rejected(self):
        d = Deduper(("a", "b"))
        d.accepts(("x", 1))
        assert d.accepts(("x", 1)) is False

    def test_different_key_accepted(self):
        d = Deduper(("a", "b"))
        d.accepts(("x", 1))
        assert d.accepts(("x", 2)) is True


class TestAcceptsRow:
    def test_first_row_accepted(self):
        d = Deduper(("trip_id", "stop_sequence"))
        assert d.accepts_row({"trip_id": "T1", "stop_sequence": 3}) is True

    def test_duplicate_row_rejected(self):
        d = Deduper(("trip_id", "stop_sequence"))
        d.accepts_row({"trip_id": "T1", "stop_sequence": 3})
        assert d.accepts_row({"trip_id": "T1", "stop_sequence": 3}) is False

    def test_missing_key_field_always_accepted_and_not_tracked(self):
        d = Deduper(("trip_id", "stop_sequence"))
        row = {"trip_id": "T1"}  # stop_sequence absent
        assert d.accepts_row(row) is True
        # calling again with the same incomplete row is still accepted --
        # never added to _seen, so it can't collide with anything
        assert d.accepts_row(row) is True
        assert len(d._seen) == 0

    def test_none_key_field_always_accepted_and_not_tracked(self):
        d = Deduper(("trip_id", "stop_sequence"))
        row = {"trip_id": "T1", "stop_sequence": None}
        assert d.accepts_row(row) is True
        assert d.accepts_row(row) is True
        assert len(d._seen) == 0

    def test_two_rows_with_same_missing_key_do_not_collide(self):
        # both rows lack stop_sequence -- neither should be treated as a
        # duplicate of the other
        d = Deduper(("trip_id", "stop_sequence"))
        row1 = {"trip_id": "T1"}
        row2 = {"trip_id": "T2"}
        assert d.accepts_row(row1) is True
        assert d.accepts_row(row2) is True


class TestFilterTable:
    def test_keeps_first_occurrence_drops_duplicate(self):
        d = Deduper(("trip_id", "stop_sequence"))
        table = pa.table(
            {
                "trip_id": ["T1", "T1", "T2"],
                "stop_sequence": [1, 1, 1],
                "other": ["a", "b", "c"],
            }
        )
        result = d.filter_table(table)
        assert result.column("other").to_pylist() == ["a", "c"]

    def test_null_key_rows_always_kept_and_not_tracked(self):
        d = Deduper(("trip_id", "stop_sequence"))
        table = pa.table(
            {
                "trip_id": ["T1", None, "T1"],
                "stop_sequence": [1, 2, 1],
                "other": ["a", "b", "c"],
            }
        )
        result = d.filter_table(table)
        # row 0 kept (first T1/1), row 1 kept (null key -> always kept),
        # row 2 dropped (duplicate of row 0)
        assert result.column("other").to_pylist() == ["a", "b"]
        assert len(d._seen) == 1

    def test_works_on_record_batch(self):
        d = Deduper(("trip_id",))
        batch = pa.record_batch({"trip_id": ["T1", "T1", "T2"]})
        result = d.filter_table(batch)
        assert result.column("trip_id").to_pylist() == ["T1", "T2"]


class TestCrossCallSharedState:
    def test_row_then_table_dedupe_against_shared_seen(self):
        d = Deduper(("trip_id",))
        assert d.accepts_row({"trip_id": "T1"}) is True
        table = pa.table({"trip_id": ["T1", "T2"]})
        result = d.filter_table(table)
        # T1 already seen via accepts_row -- dropped here
        assert result.column("trip_id").to_pylist() == ["T2"]

    def test_table_then_row_dedupe_against_shared_seen(self):
        d = Deduper(("trip_id",))
        table = pa.table({"trip_id": ["T1"]})
        d.filter_table(table)
        assert d.accepts_row({"trip_id": "T1"}) is False
        assert d.accepts_row({"trip_id": "T2"}) is True
