# SPDX-License-Identifier: MIT
"""Codex-owned CLI checks fixed before lead source arrives; original data only."""
from pathlib import Path
from tempfile import TemporaryDirectory
import copy
import hashlib
import json
import subprocess
import sys


def main():
    target = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parent
    tool = target / 'factoraudit.py'
    results = []
    with TemporaryDirectory() as temp:
        root = Path(temp)
        data = {
            'training.csv': 'date,value\n2019-12-30,1\n2019-12-31,2\n',
            'scores.csv': 'date,value\n2020-01-02,0.1\n2020-12-31,0.2\n',
            'next_training.csv': 'date,value\n2020-12-30,3\n2020-12-31,4\n',
            'next_scores.csv': 'date,value\n2021-01-04,0.3\n2021-12-31,0.4\n',
            'factor.json': '{"factor":"original_simple_momentum","lookback":20}\n',
        }
        for name, text in data.items():
            (root / name).write_text(text)
        sha = lambda name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        inputs = [{'id': name.removesuffix('.csv'), 'path': name, 'sha256': sha(name), 'date_column': 'date'} for name in data if name.endswith('.csv')]
        first = {'candidate_id': 'fold-2020', 'family': 'momentum', 'spec_path': 'factor.json', 'spec_sha256': sha('factor.json'), 'visible_through': '2019-12-31', 'inputs': ['training'], 'score_input': 'scores', 'score_window': {'start': '2020-01-02', 'end': '2020-12-31'}}
        second = {**first, 'candidate_id': 'fold-2021', 'visible_through': '2020-12-31', 'inputs': ['next_training'], 'score_input': 'next_scores', 'score_window': {'start': '2021-01-04', 'end': '2021-12-31'}}
        valid = {'campaign_id': 'original-walk-forward', 'inputs': inputs, 'candidates': [first, second]}

        def run(name, manifest, code, fail_rule=None):
            p = root / 'manifest.json'
            p.write_text(json.dumps(manifest))
            proc = subprocess.run([sys.executable, str(tool), 'audit', '--manifest', str(p), '--root', str(root)], capture_output=True, text=True, timeout=15)
            assert proc.returncode == code, (name, proc.returncode, proc.stdout, proc.stderr)
            response = json.loads(proc.stdout)
            assert 'Traceback' not in proc.stderr, (name, proc.stderr)
            if fail_rule:
                def entries(value):
                    if isinstance(value, dict):
                        yield value
                        for item in value.values(): yield from entries(item)
                    elif isinstance(value, list):
                        for item in value: yield from entries(item)
                assert any(item.get('rule') == fail_rule and item.get('status') == 'FAIL' for item in entries(response)), (name, response)
            results.append({'case': name, 'exit_code': code, 'expected_failure_rule': fail_rule, 'pass': True})

        run('same_spec_two_legitimate_historical_folds', valid, 0)
        leak = copy.deepcopy(valid); leak['candidates'][0]['inputs'].append('scores')
        run('declared_construction_contains_future_data', leak, 1, 'INPUT_VISIBILITY')
        overlap = copy.deepcopy(valid); overlap['candidates'][0]['score_window']['start'] = '2019-12-31'
        run('declared_window_includes_visibility_day', overlap, 1, 'WINDOW')
        outside = copy.deepcopy(valid); outside['candidates'][0]['score_window']['end'] = '2020-06-30'
        run('recorded_score_outside_declared_window', outside, 1, 'SCORE_DATES')
        repeat = copy.deepcopy(valid); repeat['candidates'].append({**copy.deepcopy(first), 'candidate_id': 'same-trial-other-id'})
        run('same_exact_trial_other_id', repeat, 1, 'DUPLICATE_TRIAL')
        (root / 'training.csv').write_text(data['training.csv'] + '2019-12-31,99\n')
        run('changed_input_bytes', valid, 1, 'HASH')
        (root / 'training.csv').write_text(data['training.csv'])
        escape = copy.deepcopy(valid); escape['candidates'][0]['spec_path'] = '../factor.json'
        run('path_escapes_campaign', escape, 3)
        missing = copy.deepcopy(valid); missing['candidates'][0]['score_input'] = 'unknown-file-id'
        run('unknown_scoring_input', missing, 3)
        duplicate_id = copy.deepcopy(valid); duplicate_id['candidates'].append(copy.deepcopy(first))
        run('repeated_candidate_id', duplicate_id, 1, 'DUPLICATE_ID')
        # Same bytes under another input ID must not hide an exact repeated trial.
        alias = copy.deepcopy(valid)
        alias['inputs'].append({**alias['inputs'][0], 'id': 'training-alias'})
        alias['candidates'].append({**copy.deepcopy(first), 'candidate_id': 'aliased-repeat', 'inputs': ['training-alias']})
        run('alias_cannot_hide_repeat', alias, 1, 'DUPLICATE_TRIAL')
        for name, text in [('invalid_calendar_day', 'date,value\n2019-02-30,1\n'),
                           ('duplicate_headers', 'date,date\n2019-12-31,1\n'),
                           ('missing_date_column', 'wrong,value\n2019-12-31,1\n'),
                           ('wrong_row_width', 'date,value\n2019-12-31\n'),
                           ('empty_data', 'date,value\n')]:
            (root / 'training.csv').write_text(text)
            run(name, valid, 3)
        (root / 'training.csv').write_text(data['training.csv'])
        with TemporaryDirectory() as outside:
            external = Path(outside) / 'factor.json'; external.write_text(data['factor.json'])
            (root / 'escape-link.json').symlink_to(external)
            symlink = copy.deepcopy(valid); symlink['candidates'][0]['spec_path'] = 'escape-link.json'
            run('symlink_escapes_campaign', symlink, 3)
        (root / 'not-a-file').mkdir()
        directory = copy.deepcopy(valid); directory['candidates'][0]['spec_path'] = 'not-a-file'
        run('nonregular_spec', directory, 3)
        p = root / 'manifest.json'; p.write_text(json.dumps(valid))
        proc = subprocess.run([sys.executable, str(tool), 'audit', '--manifest', str(p), '--root', str(root), '--max-bytes', '1'], capture_output=True, text=True, timeout=15)
        assert proc.returncode == 3 and json.loads(proc.stdout)['overall'] == 'MALFORMED', proc.stdout
        results.append({'case': 'per_file_byte_cap', 'exit_code': 3, 'pass': True})
        # Malformed duplicate JSON keys cannot pass a plain dict serialization.
        p = root / 'manifest.json'; p.write_text('{"campaign_id":"first","campaign_id":"second","inputs":[],"candidates":[]}')
        proc = subprocess.run([sys.executable, str(tool), 'audit', '--manifest', str(p), '--root', str(root)], capture_output=True, text=True, timeout=15)
        assert proc.returncode == 3 and isinstance(json.loads(proc.stdout), dict), proc.stdout
        results.append({'case': 'duplicate_json_keys', 'exit_code': 3, 'pass': True})
    print(json.dumps({'independent_cases': results, 'limitations': 'Original declared data and deterministic injected errors only; no trading performance, omitted trials or trusted knowledge timestamps measured.'}, indent=2))


if __name__ == '__main__':
    main()
