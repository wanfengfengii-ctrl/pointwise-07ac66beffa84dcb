"""Paper forms and V1 starting states for the inventory examples."""
from contextlib import closing, contextmanager
from copy import deepcopy
import json
import sqlite3

from exchange.issue import issue_totals
from handling.operations import load_operation_log, record_operation
from handling.replay import replay_segment
from handling.segments import get_segment, open_segment
from intake.batches import receive_batch
from inventory.close import close_inventory
from inventory.readback import read_inventory
from storage import open_source


def _row(line_no, operation_id, kind, input_candidates, outputs):
    return dict(line_no=line_no, operation_id=operation_id, kind=kind,
                input_candidates=input_candidates, outputs=outputs)


def _mixes(n_candidates):
    return [
        _row(4, "mix-M", "mix", [["q", "s"], ["u"]], [("M", 6)]),
        _row(9, "mix-N", "mix", [n_candidates, ["v"]], [("N", 6)]),
    ]


_CASES = {
    "ambiguous": (
        [("q", 3), ("s", 3), ("u", 3), ("v", 3)], [],
        _mixes(["q", "s"]), [("M", 6), ("N", 6)],
    ),
    "unique": (
        [("q", 3), ("s", 3), ("u", 3), ("v", 3)], [],
        _mixes(["q"]), [("M", 6), ("N", 6)],
    ),
    "cross-segment": (
        [("q", 3), ("s", 3), ("u", 3)],
        [("side", "side-split", "split", ["q"], [("side-bag", 3), ("side-zero", 0)])],
        [_row(12, "mix-M", "mix", [["q", "s"], ["u"]], [("M", 6)])], [("M", 6)],
    ),
    "reverse": (
        [("prefix-raw", 3), ("root", 6)],
        [("day", "prefix", "split", ["prefix-raw"], [("carried", 3), ("old-zero", 0)])],
        [
            _row(2, "mix-M", "mix", [["mid"], ["carried"]], [("M", 7)]),
            _row(8, "split-root", "split", [["root"]], [("mid", 4), ("tail", 2), ("zero", 0)]),
        ],
        [("M", 7), ("old-zero", 0), ("tail", 2), ("zero", 0)],
    ),
    "cycle": (
        [("u", 0), ("v", 0)], [],
        [
            _row(2, "mix-X", "mix", [["Y"], ["u"]], [("X", 3)]),
            _row(8, "mix-Y", "mix", [["X"], ["v"]], [("Y", 3)]),
        ], [],
    ),
}


def prepare_case(db, name):
    """Seed an empty V1 source and return the arguments for its paper form."""
    batches, prefix, rows, observations = deepcopy(_CASES[name])
    for batch_id, grams in batches:
        receive_batch(db, batch_id, grams, f"ticket-{batch_id}")
    open_segment(db, "day")
    for segment_id, operation_id, kind, inputs, outputs in prefix:
        open_segment(db, segment_id)
        record_operation(db, segment_id, operation_id, kind, inputs, outputs)
    return dict(segment_id="day", inventory_id=f"inventory-{name}", paper_id=f"paper-{name}",
                rows=rows, observations=observations)


@contextmanager
def fail_seal_write(db):
    db.execute("""CREATE TEMP TRIGGER paper_example_fail_seal
                  BEFORE UPDATE OF closed_sequence ON main.operation_segments
                  BEGIN SELECT RAISE(ABORT, "paper seal write failed"); END""")
    try:
        yield
    finally:
        db.execute("DROP TRIGGER temp.paper_example_fail_seal")


def main():
    initial_bags = {}
    for name in _CASES:
        with closing(open_source(":memory:")) as db:
            args = prepare_case(db, name)
            initial_bags[name] = replay_segment(db, args["segment_id"]).bags
            if not get_segment(db, args["segment_id"]).is_open or read_inventory(db, args["inventory_id"]):
                raise AssertionError(f"{name} must start with an unsealed segment")
    if initial_bags != {"ambiguous": (), "unique": (), "cross-segment": (), "cycle": (),
                        "reverse": (("carried", 3), ("old-zero", 0))}:
        raise AssertionError(initial_bags)

    with closing(open_source(":memory:")) as db:
        args = prepare_case(db, "cross-segment")
        segment_id = args["segment_id"]
        inventory_id = args["inventory_id"]
        record = record_operation(db, segment_id, "v1-known-mix", "mix", ["s", "u"], [("M", 6)])
        with fail_seal_write(db):
            try:
                close_inventory(db, segment_id, args["observations"], inventory_id=inventory_id)
            except sqlite3.IntegrityError as exc:
                if str(exc) != "paper seal write failed":
                    raise
            else:
                raise AssertionError("seal trigger did not fire")
        if read_inventory(db, inventory_id) or not get_segment(db, segment_id).is_open:
            raise AssertionError("failed V1 seal changed inventory status")
        if load_operation_log(db, segment_id).operations != (record,):
            raise AssertionError("the previously committed V1 operation must survive")
        close_inventory(db, segment_id, args["observations"], inventory_id=inventory_id)
        piece = issue_totals(db, inventory_id)
        if piece.bags != (("M", 6),):
            raise AssertionError(piece)
    print(json.dumps({"initial_bags": initial_bags, "v1_issued_bags": piece.bags}, sort_keys=True))


if __name__ == "__main__":
    main()
