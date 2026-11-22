"""Interpret a paper form and register the single unique reading (V1 rules)."""
from dataclasses import dataclass
from itertools import product
import json
import sqlite3

from handling.balance import OutputPort, check_node_identities, parse_output_port
from handling.operations import OperationRecord, register_operation
from handling.replay import replay_segment
from handling.segments import require_open_segment, seal_segment
from intake.weighing import parse_identifier, record_rows
from inventory.observations import read_observations
from inventory.reconcile import compare_bag_ledger
from inventory.snapshot import InventorySnapshot, save_snapshot
from storage import decimal_integer, write_transaction


_ROW_FIELDS = {'line_no', 'operation_id', 'kind', 'input_candidates', 'outputs'}


@dataclass(frozen=True)
class PaperLineEvidence:
    line_no: int
    operation: OperationRecord


@dataclass(frozen=True)
class PaperEvidence:
    paper_id: str
    rows: tuple[PaperLineEvidence, ...]


@dataclass(frozen=True)
class PaperCloseResult:
    status: str
    snapshot: InventorySnapshot | None
    evidence: PaperEvidence | None


@dataclass(frozen=True)
class _PaperRow:
    line_no: int
    operation_id: str
    kind: str
    candidates: tuple[tuple[str, ...], ...]
    outputs: tuple[OutputPort, ...]


@dataclass(frozen=True)
class _Ledger:
    nodes: dict
    used_raws: frozenset
    produced: frozenset

    def consume(self, name):
        nodes = dict(self.nodes)
        if name in nodes:
            del nodes[name]
            return _Ledger(nodes, self.used_raws, self.produced)
        return _Ledger(nodes, self.used_raws | {name}, self.produced)

    def produce(self, node, grams):
        nodes = dict(self.nodes)
        nodes[node] = grams
        return _Ledger(nodes, self.used_raws, self.produced | {node})


@dataclass(frozen=True)
class _Base:
    ledger: _Ledger
    raw_grams: dict
    occupied: frozenset
    segment_names: frozenset
    operation_ids: frozenset


def _read_paper_rows(rows) -> tuple[_PaperRow, ...]:
    if not isinstance(rows, (list, tuple)):
        raise ValueError('paper rows must be a list or tuple')
    parsed = []
    line_numbers = set()
    operation_ids = set()
    for entry in rows:
        if not isinstance(entry, dict) or set(entry) != _ROW_FIELDS:
            raise ValueError(
                'each paper row needs line_no, operation_id, kind, input_candidates and outputs',
            )
        line_no = entry['line_no']
        if type(line_no) is not int or line_no < 1:
            raise ValueError('paper line number must be a positive integer')
        if line_no in line_numbers:
            raise ValueError(f'duplicate paper line {line_no}')
        operation_id = parse_identifier(entry['operation_id'], 'operation ID')
        if operation_id in operation_ids:
            raise ValueError(f'duplicate paper operation {operation_id!r}')
        kind = entry['kind']
        if kind not in ('split', 'mix'):
            raise ValueError('operation kind must be split or mix')
        positions = []
        for position in record_rows(entry['input_candidates'], 'paper input candidates'):
            names = tuple(dict.fromkeys(
                parse_identifier(name, 'paper input')
                for name in record_rows(position, 'a paper input position')
            ))
            if not names:
                raise ValueError('each paper input position needs a candidate')
            positions.append(names)
        outputs = tuple(parse_output_port(row) for row in record_rows(entry['outputs'], 'paper outputs'))
        check_node_identities((), outputs)
        if kind == 'split':
            if len(positions) != 1:
                raise ValueError('a split needs exactly one input position')
            if len(outputs) < 2:
                raise ValueError('a split needs at least two outputs')
        else:
            if len(positions) < 2:
                raise ValueError('a mix needs at least two input positions')
            if len(outputs) != 1:
                raise ValueError('a mix needs exactly one output')
        parsed.append(_PaperRow(line_no, operation_id, kind, tuple(positions), outputs))
        line_numbers.add(line_no)
        operation_ids.add(operation_id)
    return tuple(sorted(parsed, key=lambda row: row.line_no))


def _load_base(db: sqlite3.Connection, segment_id: str) -> _Base:
    nodes = dict(replay_segment(db, segment_id).bags)
    used_raws = frozenset(batch for (batch,) in db.execute('SELECT batch_id FROM raw_uses'))
    occupied = frozenset(
        node for (node,) in db.execute('SELECT node_id FROM material_nodes WHERE segment_id = ?', (segment_id,))
    ) | frozenset(batch for (batch,) in db.execute('SELECT batch_id FROM raw_batches'))
    raw_grams = {
        batch: decimal_integer(grams)
        for batch, grams in db.execute('SELECT batch_id, net_grams FROM raw_batches')
    }
    operation_ids = frozenset(
        key for (key,) in db.execute('SELECT operation_id FROM operations WHERE segment_id = ?', (segment_id,))
    )
    segment_names = frozenset(
        node for (node,) in db.execute('SELECT node_id FROM material_nodes WHERE segment_id = ?', (segment_id,))
    )
    return _Base(_Ledger(nodes, used_raws, frozenset()), raw_grams, occupied, segment_names, operation_ids)


def _choices(row: _PaperRow):
    """Yield each distinct input name set once; same-set orderings collapse.

    Readings come out in a stable (code point) order so registered inputs are
    reproducible across runs.
    """
    seen = set()
    for combo in product(*row.candidates):
        if len(set(combo)) != len(combo):
            continue
        key = frozenset(combo)
        if key in seen:
            continue
        seen.add(key)
        yield tuple(sorted(key))


def _step(row: _PaperRow, names, ledger: _Ledger, base: _Base):
    """Return the ledger after this reading, or None if it cannot run next."""
    if row.operation_id in base.operation_ids:
        return None
    for output in row.outputs:
        if output.node_id in base.occupied or output.node_id in ledger.produced or output.node_id in names:
            return None
    grams = []
    for name in names:
        if name in ledger.nodes:
            grams.append(ledger.nodes[name])
        elif name in base.segment_names or name in ledger.produced:
            return None
        elif name in base.raw_grams and name not in ledger.used_raws:
            grams.append(base.raw_grams[name])
        else:
            return None
    if sum(grams) != sum(output.net_grams for output in row.outputs):
        return None
    result = ledger
    for name in names:
        result = result.consume(name)
    for output in row.outputs:
        result = result.produce(output.node_id, output.net_grams)
    return result


def _plan(rows, assignment, base: _Base):
    """Return one legal execution order for a fixed assignment, or None.

    Order matters only for availability, so search orders directly rather than
    assuming any single greedy order works.
    """
    memo = set()

    def signature(ledger: _Ledger):
        return (tuple(sorted(ledger.nodes.items())), ledger.used_raws, ledger.produced)

    def search(remaining, ledger, order):
        if not remaining:
            return order
        key = (remaining, signature(ledger))
        if key in memo:
            return None
        memo.add(key)
        for index in sorted(remaining):
            following = _step(rows[index], assignment[index], ledger, base)
            if following is None:
                continue
            found = search(remaining - {index}, following, order + [index])
            if found is not None:
                return found
        return None

    return search(frozenset(assignment), base.ledger, [])


def _decide(rows, base: _Base, observed):
    target = {bag.bag_id: bag.net_grams for bag in observed}
    option_sets = [tuple(_choices(row)) for row in rows]
    if any(not options for options in option_sets):
        return 'no_solution', None
    readings = {}
    assignment = {}

    def walk(index):
        if len(readings) > 1:
            return
        if index == len(rows):
            plan = _plan(rows, assignment, base)
            if plan is None:
                return
            ledger = base.ledger
            for step in plan:
                ledger = _step(rows[step], assignment[step], ledger, base)
            if ledger.nodes == target:
                key = frozenset((position, names) for position, names in assignment.items())
                readings.setdefault(key, (tuple(plan), tuple(
                    assignment[index] for index in range(len(rows))
                )))
            return
        for names in option_sets[index]:
            assignment[index] = names
            walk(index + 1)
            if len(readings) > 1:
                return
        del assignment[index]

    walk(0)
    if not readings:
        return 'no_solution', None
    if len(readings) > 1:
        return 'ambiguous', None
    ((plan, chosen),) = readings.values()
    return 'unique', (plan, chosen)


def _register(db, segment_id, rows, plan, chosen):
    records = {}
    for index in plan:
        row = rows[index]
        records[index] = register_operation(
            db, segment_id, row.operation_id, row.kind, list(chosen[index]), row.outputs,
        )
    return records


def _store_evidence(db, inventory_id, paper_id, rows, records) -> PaperEvidence:
    db.execute(
        'INSERT INTO paper_evidence(inventory_id, paper_id) VALUES (?, ?)',
        (inventory_id, paper_id),
    )
    entries = []
    for index, row in enumerate(rows):
        record = records[index]
        db.execute(
            '''INSERT INTO paper_evidence_rows(inventory_id, line_no, operation_id, sequence)
               VALUES (?, ?, ?, ?)''',
            (inventory_id, row.line_no, record.operation_id, record.sequence),
        )
        entries.append(PaperLineEvidence(row.line_no, record))
    return PaperEvidence(paper_id, tuple(entries))


def close_from_paper(
    db: sqlite3.Connection, segment_id, inventory_id, paper_id, rows, observations,
) -> PaperCloseResult:
    segment_id = parse_identifier(segment_id, 'segment ID')
    inventory_id = parse_identifier(inventory_id, 'inventory ID')
    paper_id = parse_identifier(paper_id, 'paper ID')
    parsed = _read_paper_rows(rows)
    observed = read_observations(observations)
    with write_transaction(db):
        require_open_segment(db, segment_id)
        if db.execute('SELECT 1 FROM inventories WHERE inventory_id = ?', (inventory_id,)).fetchone():
            raise ValueError(f'inventory {inventory_id!r} already exists')
        base = _load_base(db, segment_id)
        status, decision = _decide(parsed, base, observed)
        if status != 'unique':
            return PaperCloseResult(status, None, None)
        plan, chosen = decision
        records = _register(db, segment_id, parsed, plan, chosen)
        fresh = replay_segment(db, segment_id)
        if not compare_bag_ledger(fresh, observed).matches:
            raise ValueError('paper reading does not match the observed bags')
        snapshot = InventorySnapshot(inventory_id, segment_id, fresh.through_sequence, fresh.bags)
        save_snapshot(db, snapshot)
        evidence = _store_evidence(db, inventory_id, paper_id, parsed, records)
        seal_segment(db, segment_id, fresh.through_sequence)
        return PaperCloseResult('unique', snapshot, evidence)


def read_paper_evidence(db: sqlite3.Connection, inventory_id) -> PaperEvidence | None:
    key = parse_identifier(inventory_id, 'inventory ID')
    row = db.execute('SELECT paper_id FROM paper_evidence WHERE inventory_id = ?', (key,)).fetchone()
    if row is None:
        return None
    inventory = db.execute('SELECT segment_id FROM inventories WHERE inventory_id = ?', (key,)).fetchone()
    if inventory is None:
        raise sqlite3.DatabaseError(f'inventory {key!r} has evidence but no sealed ledger')
    entries = tuple(
        PaperLineEvidence(
            line_no,
            OperationRecord(
                sequence, operation_id, kind, tuple(json.loads(inputs)),
                tuple(OutputPort(node, decimal_integer(grams)) for node, grams in json.loads(outputs)),
            ),
        )
        for line_no, sequence, operation_id, kind, inputs, outputs in db.execute(
            '''SELECT r.line_no, o.sequence, o.operation_id, o.kind, o.input_nodes, o.output_ports
               FROM paper_evidence_rows AS r
               JOIN operations AS o ON o.segment_id = ? AND o.sequence = r.sequence
               WHERE r.inventory_id = ? ORDER BY r.line_no''',
            (inventory[0], key),
        )
    )
    return PaperEvidence(row[0], entries)
