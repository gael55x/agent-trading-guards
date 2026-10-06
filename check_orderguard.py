# SPDX-License-Identifier: MIT
"""Codex's pre-implementation accounting oracle; imports only the reviewed target."""
from fractions import Fraction as F
from pathlib import Path
import copy
import importlib.util
import json
import random
import sys
import subprocess
from tempfile import TemporaryDirectory


def cents(n):
    return f'{n // 100}.{n % 100:02d}'


def reference(limits, snap, proposal):
    """Original contract arithmetic, exact rationals; no target helpers used."""
    order = F(proposal['qty']) * F(proposal['limit_price'])
    pending = sum((F(p['qty']) * F(p['limit_price']) for p in snap['pending'] if p['side'] == 'buy'), F(0))
    held = sum((F(p['qty']) * F(p['mark']) for p in snap['positions'].values()), F(0))
    exposure = held + pending + order
    cash = F(snap['cash']) - pending - order
    rules = {
        'ORDER_NOTIONAL': order > F(limits['max_order_notional']),
        'POSITION_LIMIT': exposure > F(limits['symbols']['AAA']['max_position_notional']),
        'GROSS_LIMIT': exposure > F(limits['max_gross_notional']),
        'CASH_RESERVE': cash < F(limits['min_cash_reserve']),
    }
    return sorted(k for k, violated in rules.items() if violated), {'order_notional': order, 'pending_buy_notional': pending, 'symbol_exposure_after': exposure, 'gross_after': exposure, 'cash_after': cash}


def main():
    target = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location('orderguard', target / 'orderguard.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    sys.path.insert(0, str(target))
    spec.loader.exec_module(module)
    rng = random.Random(20261009)
    seed = 20261009
    admitted = refused = confidence_checks = 0
    for index in range(2000):
        limits = {
            'max_order_notional': cents(rng.randrange(1, 100000)),
            'max_gross_notional': cents(rng.randrange(1, 200000)),
            'min_cash_reserve': cents(rng.randrange(0, 30000)),
            'max_snapshot_age_s': '300',
            'symbols': {'AAA': {'max_position_notional': cents(rng.randrange(1, 200000))}},
        }
        snapshot = {
            'as_of': '2026-10-09T12:00:00Z', 'cash': cents(rng.randrange(1, 200000)),
            'positions': {'AAA': {'qty': cents(rng.randrange(0, 1000)), 'mark': cents(rng.randrange(1, 20000))}},
            'pending': [{'side': 'buy', 'symbol': 'AAA', 'qty': cents(rng.randrange(1, 100)), 'limit_price': cents(rng.randrange(1, 20000))}],
        }
        proposal = {'proposal_id': f'original-{index}', 'symbol': 'AAA', 'side': 'buy', 'qty': cents(rng.randrange(1, 1000)), 'limit_price': cents(rng.randrange(1, 20000))}
        before = copy.deepcopy((limits, snapshot, proposal))
        expected_reasons, expected_values = reference(limits, snapshot, proposal)
        decision = module.decide(limits, snapshot, proposal, '2026-10-09T12:02:00Z')
        assert decision['reasons'] == expected_reasons, (index, decision, expected_reasons)
        assert decision['verdict'] == ('REFUSE' if expected_reasons else 'ADMIT'), (index, decision)
        for key, expected in expected_values.items():
            assert F(decision['computed'][key]) == expected, (index, key, decision, expected)
        assert (limits, snapshot, proposal) == before, 'input mutation'
        high = module.decide(limits, snapshot, {**proposal, 'confidence': '1'}, '2026-10-09T12:02:00Z')
        low = module.decide(limits, snapshot, {**proposal, 'confidence': '0'}, '2026-10-09T12:02:00Z')
        assert (high['verdict'], high['reasons'], high['computed']) == (low['verdict'], low['reasons'], low['computed']) == (decision['verdict'], decision['reasons'], decision['computed'])
        confidence_checks += 2
        admitted += not expected_reasons
        refused += bool(expected_reasons)
    print(json.dumps({'oracle': 'independent fractions.Fraction accounting, no target arithmetic helpers', 'seed': seed, 'cases': 2000, 'admitted': admitted, 'refused': refused, 'confidence_checks': confidence_checks, 'inputs_unchanged': True, 'boundary_cases': boundaries(module, target), 'limitations': 'Original one-symbol synthetic accounting cases; no market returns, execution concurrency, quote accuracy or trading quality measured.'}, indent=2))



def boundaries(module, target):
    limits = {'max_order_notional':'3000','max_gross_notional':'20000','min_cash_reserve':'1000','max_snapshot_age_s':'300','symbols':{'AAA':{'max_position_notional':'8000'},'BBB':{'max_position_notional':'4000'}}}
    snap = {'as_of':'2026-10-09T12:00:00Z','cash':'4000','positions':{'AAA':{'qty':'20','mark':'250'},'BBB':{'qty':'10','mark':'100'}},'pending':[{'side':'buy','symbol':'AAA','qty':'9','limit_price':'110'},{'side':'sell','symbol':'AAA','qty':'5','limit_price':'260'}]}
    buy = {'proposal_id':'boundary','side':'buy','symbol':'AAA','qty':'20','limit_price':'100.5','confidence':'1'}
    now = '2026-10-09T12:05:00Z'
    results = []
    def expect(name, snapshot, proposal, reasons, values=None, clock=now, policy=limits):
        report = module.decide(policy,snapshot,proposal,clock)
        assert report['reasons'] == sorted(reasons), (name, report)
        assert report['verdict'] == ('REFUSE' if reasons else 'ADMIT'), (name,report)
        for key,value in (values or {}).items(): assert F(report['computed'][key]) == F(value), (name,report)
        results.append({'case':name,'verdict':report['verdict'],'pass':True})
    expect('pending_cash_exact_age_exact',snap,buy,[],{'cash_after':'1000','symbol_exposure_after':'8000','gross_after':'9000'})
    expect('one_price_step_above',snap,{**buy,'limit_price':'100.51'},['CASH_RESERVE','POSITION_LIMIT'],{'cash_after':'999.8','symbol_exposure_after':'8000.2'})
    expect('stale_by_one_second',snap,buy,['STALE_SNAPSHOT'],clock='2026-10-09T12:05:01Z')
    sell={**buy,'side':'sell','qty':'15','limit_price':'250'}
    restrictive={**limits,'max_order_notional':'1','max_gross_notional':'1','symbols':{k:{'max_position_notional':'1'} for k in limits['symbols']}}
    expect('over_limit_position_can_reduce',snap,sell,[],{'sellable':'15'},policy=restrictive)
    expect('pending_sell_reservation',snap,{**sell,'qty':'15.000001'},['SELL_EXCEEDS_SELLABLE'])
    expect('pending_sell_is_not_cash',snap,{**buy,'qty':'12','limit_price':'250'},['CASH_RESERVE','POSITION_LIMIT'],{'cash_after':'10','symbol_exposure_after':'8990'})
    unknown=copy.deepcopy(snap);unknown['positions']['CCC']={'qty':'1','mark':'1'}
    expect('untracked_exposure_refused',unknown,buy,['UNTRACKED_EXPOSURE'])
    oversold=copy.deepcopy(snap);oversold['pending'][1]['qty']='21'
    expect('inconsistent_pending_sell_refused',oversold,buy,['INVALID_PENDING_POSITION'])
    other=copy.deepcopy(snap);other['positions']['BBB']['mark']='500'
    expect('other_symbol_cap_also_checked',other,buy,['POSITION_LIMIT'])
    fractional={'as_of':snap['as_of'],'cash':'1','positions':{},'pending':[]}
    fractional_limits={**limits,'min_cash_reserve':'0.7','max_gross_notional':'0.3','symbols':{'AAA':{'max_position_notional':'0.3'}}}
    expect('decimal_exact',fractional,{**buy,'qty':'3','limit_price':'0.1'},[],{'cash_after':'0.7','symbol_exposure_after':'0.3'},policy=fractional_limits)
    for name,bad in [('JSON_number',{**buy,'qty':20}),('negative',{**buy,'qty':'-1'}),('NaN',{**buy,'qty':'NaN'}),('exponent',{**buy,'qty':'1e2'}),('precision',{**buy,'qty':'0.0000000000001'}),('unknown_field',{**buy,'extra':'ignored?'}),('confidence_outside_range',{**buy,'confidence':'1.01'})]:
        try: module.decide(limits,snap,bad,now)
        except module.Malformed: results.append({'case':name,'verdict':'MALFORMED','pass':True})
        else: raise AssertionError(name)
    try: module.decide(limits,snap,buy,'2026-10-09T11:59:59Z')
    except module.Malformed: results.append({'case':'future_snapshot','verdict':'MALFORMED','pass':True})
    else: raise AssertionError('future snapshot')
    with TemporaryDirectory() as temp:
        root=Path(temp)
        for name,value in [('limits',limits),('snapshot',snap),('proposal',buy)]: (root/(name+'.json')).write_text(json.dumps(value))
        cmd=[sys.executable,str(target/'orderguard.py'),'check','--limits',str(root/'limits.json'),'--snapshot',str(root/'snapshot.json'),'--proposal',str(root/'proposal.json'),'--now',now]
        for name,text,code in [('CLI_admit',json.dumps(buy),0),('CLI_refuse',json.dumps({**buy,'limit_price':'100.51'}),2),('CLI_duplicate_key','{"proposal_id":"a","proposal_id":"b"}',3),('CLI_bad_JSON','{',3)]:
            (root/'proposal.json').write_text(text)
            proc=subprocess.run(cmd,capture_output=True,text=True,timeout=10)
            assert proc.returncode==code and not proc.stderr,(name,proc.stdout,proc.stderr)
            json.loads(proc.stdout);results.append({'case':name,'exit_code':code,'pass':True})
        (root/'proposal.json').unlink();(root/'proposal.json').mkdir()
        proc=subprocess.run(cmd,capture_output=True,text=True,timeout=10)
        assert proc.returncode==3 and json.loads(proc.stdout)['verdict']=='MALFORMED',proc.stdout
        results.append({'case':'CLI_nonregular_file','exit_code':3,'pass':True})
    return results

if __name__ == '__main__':
    main()
