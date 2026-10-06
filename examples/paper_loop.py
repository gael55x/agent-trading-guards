# SPDX-License-Identifier: MIT
"""Single-owner, constructed paper fill; no broker, market data or concurrency."""
import copy
from decimal import Decimal
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orderguard import decide

root = Path(__file__).resolve().parent
limits, snapshot, proposal = [json.loads((root / name).read_text()) for name in
                             ('limits.json', 'snapshot.json', 'proposal.json')]
first = decide(limits, snapshot, proposal, '2026-10-09T12:01:00Z')
assert first['verdict'] == 'ADMIT', first

# This example explicitly assumes a full paper fill at the supplied limit.
# A real caller must refresh authoritative state, handle fees/partial fills,
# and serialize check-then-submit; ADMIT is not an execution receipt.
after = copy.deepcopy(snapshot)
after['cash'] = str(Decimal(after['cash']) - Decimal(first['computed']['order_notional']))
after['positions']['AAA']['qty'] = str(Decimal(after['positions']['AAA']['qty']) + Decimal(proposal['qty']))
after['as_of'] = '2026-10-09T12:01:00Z'
second = decide(limits, after, {**proposal, 'proposal_id': 'original-paper-next', 'qty': '1'},
                '2026-10-09T12:02:00Z')
assert second['verdict'] == 'REFUSE' and second['reasons'] == ['CASH_RESERVE', 'POSITION_LIMIT'], second
print(json.dumps({'scope': 'Original single-owner paper example, assumed full fill; no returns measured',
                  'first': first, 'assumed_fill_snapshot': after, 'next': second}, indent=2))
