from contextlib import closing
from dataclasses import asdict
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from exchange.format import encode_totals
from exchange.issue import issue_totals
from exchange.receive import receive_totals
from handling.operations import record_operation
from handling.segments import open_segment
from intake.imports import import_weighings
from inventory.close import close_from_paper, close_inventory
from inventory.readback import read_inventory, read_paper_evidence
from paper_examples import prepare_case
from review.publish import review_total
from review.readback import read_review
from storage import open_source, open_station


def _print(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def run_workday(samples: Path, directory: Path) -> None:
    day = json.loads((samples / 'workday.json').read_text(encoding='utf-8'))
    station_path = directory / 'station.sqlite3'
    with closing(open_source(directory / 'source.sqlite3')) as source:
        imported = import_weighings(source, samples / 'weighings.json')
        refused = [entry.error for entry in imported if entry.error is not None]
        if refused:
            raise ValueError(f'sample weighings refused: {refused}')
        open_segment(source, day['segment_id'])
        for operation in day['operations']:
            record_operation(source, day['segment_id'], **operation)
        closed = close_inventory(
            source, day['segment_id'], day['observations'], inventory_id=day['inventory_id'],
        )
        if closed.snapshot is None:
            raise ValueError(f'sample inventory differences: {closed.difference}')
        piece = issue_totals(source, closed.snapshot.inventory_id)
        body = encode_totals(piece)

    with closing(open_station(station_path)) as station:
        accepted = receive_totals(station, piece.piece_id, piece.inventory_id, body)
        signed = review_total(station, accepted.piece_id, **day['request'])
    with closing(open_station(station_path)) as station:
        stored = read_review(station, signed.review_id)
        if stored != signed:
            raise RuntimeError('reopened review differs from the signed review')
        uses = [(use.bag_id, use.net_grams) for use in stored.allocation.uses]
        if uses != [('bag-A', 6), ('bag-B', 2)] or stored.allocation.status != 'satisfied':
            raise RuntimeError('sample review did not take the expected eight grams')
        _print(asdict(stored))


def run_paper(directory: Path) -> None:
    source_path = directory / 'paper-source.sqlite3'
    station_path = directory / 'paper-station.sqlite3'
    with closing(open_source(source_path)) as source:
        args = prepare_case(source, 'cross-segment')
        result = close_from_paper(source, **args)
        if result.status != 'unique' or result.snapshot is None:
            raise RuntimeError(f'paper case must be unique, got {result.status}')
        _print({
            'step': 'paper-closed',
            'paper_id': result.evidence.paper_id,
            'rows': [
                {'line_no': entry.line_no, 'inputs': list(entry.operation.inputs),
                 'outputs': [[port.node_id, port.net_grams] for port in entry.operation.outputs]}
                for entry in result.evidence.rows
            ],
            'bags': [list(bag) for bag in result.snapshot.bags],
        })
    with closing(open_source(source_path)) as source:
        reopened = read_inventory(source, args['inventory_id'])
        evidence = read_paper_evidence(source, args['inventory_id'])
        if reopened != result.snapshot or evidence != result.evidence:
            raise RuntimeError('reopened paper ledger differs from the sealed result')
        piece = issue_totals(source, args['inventory_id'])
        body = encode_totals(piece)
        _print({
            'step': 'paper-reopened',
            'inventory_id': reopened.inventory_id,
            'bags': [list(bag) for bag in reopened.bags],
            'evidence_rows': [entry.line_no for entry in evidence.rows],
            'evidence_matches': evidence == result.evidence,
            'piece_id': piece.piece_id,
            'v1_body': body,
        })

    with closing(open_station(station_path)) as station:
        accepted = receive_totals(station, piece.piece_id, piece.inventory_id, body)
        signed = review_total(station, accepted.piece_id, ['M'], 6)
        if signed.allocation.status != 'satisfied':
            raise RuntimeError('paper review did not satisfy six grams')
        _print({
            'step': 'paper-review',
            'status': signed.allocation.status,
            'uses': [[use.bag_id, use.net_grams] for use in signed.allocation.uses],
        })
    with closing(open_station(station_path)) as station:
        restored = read_review(station, signed.review_id)
        if restored != signed:
            raise RuntimeError('reopened station review differs from the signed review')
        _print({
            'step': 'paper-review-reopened',
            'review_id': restored.review_id,
            'receipt_no': restored.receipt.receipt_no,
            'uses': [[use.bag_id, use.net_grams] for use in restored.allocation.uses],
        })


def main() -> None:
    samples = Path(__file__).resolve().parent / 'samples'
    with TemporaryDirectory(prefix='lanxi-demo-') as temporary:
        directory = Path(temporary)
        run_workday(samples, directory)
        run_paper(directory)


if __name__ == '__main__':
    main()
