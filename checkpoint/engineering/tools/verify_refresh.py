"""Re-run offline suites into a fresh evidence directory, preserving old reports."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def positive_seconds(value):
    seconds = int(value)
    if seconds <= 0:
        raise argparse.ArgumentTypeError('timeout must be a positive number of seconds')
    return seconds


def run_check(name, command, *, out, env, timeout_seconds):
    started = time.monotonic()
    timed_out = False
    try:
        result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True,
                                text=True, encoding='utf-8', timeout=timeout_seconds)
        stdout, stderr, exit_code = result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired as error:
        # TimeoutExpired may carry bytes even when text=True was requested.
        def captured_text(value):
            return value.decode('utf-8', errors='replace') if isinstance(value, bytes) else (value or '')
        stdout, stderr = captured_text(error.stdout), captured_text(error.stderr)
        exit_code, timed_out = 124, True
    log = stdout + '\n' + stderr
    if timed_out:
        log += '\n' + json.dumps({'timeout': True, 'timeout_seconds': timeout_seconds,
                                  'exit_code': exit_code, 'captured_output_may_be_partial': True}) + '\n'
    (out / (name + '.log')).write_text(log, encoding='utf-8')
    count = re.search(r'Ran (\d+) tests?', stderr)
    return {'check': name, 'exit_code': exit_code, 'timeout': timed_out,
            'timeout_seconds': timeout_seconds, 'elapsed_seconds': round(time.monotonic() - started, 3),
            'unit_tests': int(count.group(1)) if count else 0, 'log': name + '.log'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', required=True)
    parser.add_argument('--timeout-seconds', type=positive_seconds, default=240,
                        help='Maximum time per check; default 240 seconds. Use 600 for a longer diagnostic run.')
    args = parser.parse_args()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=False)
    env = {**os.environ, 'PYTHONUTF8': '1', 'DEEPSEEK_API_KEY': ''}
    jobs = [
        ('runtime', ['-m', 'unittest', 'discover', '-s', 'reference/runtime_reference/tests', '-v']),
        ('capabilities', ['-m', 'unittest', 'discover', '-s', 'reference/capability_runtime/tests', '-v']),
        ('transactions', ['-m', 'unittest', 'discover', '-s', 'reference/transaction_reference/tests', '-v']),
        ('composition', ['-m', 'unittest', 'discover', '-s', 'reference/qa', '-p', 'test_reference_composition.py', '-v']),
        ('contracts', ['reference/qa/validate_contracts.py']),
        ('application_contracts', ['-c', "import json,yaml; from pathlib import Path; from jsonschema import Draft202012Validator; from openapi_spec_validator import validate; s=json.loads(Path('schemas/commerce.schema.json').read_text(encoding='utf-8')); Draft202012Validator.check_schema(s); a=json.loads(Path('schemas/application.openapi.json').read_text(encoding='utf-8')); validate(a); y=yaml.safe_load(Path('agent_skill_contracts.yaml').read_text(encoding='utf-8')); print(json.dumps({'commerce_definitions':len(s['$defs']),'application_paths':len(a['paths']),'yaml_valid':isinstance(y,dict)}))"]),
        ('application', ['-m', 'unittest', 'discover', '-s', 'tests', '-v']),
        ('public_fact_audit', ['tools/audit_public_facts.py', '--out', str(out / 'public_fact_audit.json')]),
        ('transaction_scenarios', ['tools/run_transaction.py', '--out', str(out / 'transaction_run.json')]),
        ('benchmark_empty', ['tools/summarize_benchmark.py', '--input', 'data/benchmark_observations.jsonl', '--out', str(out / 'benchmark_summary.json')]),
    ]
    jobs += [('offline_' + s, ['tools/run_agent.py', '--mode', 'offline', '--scenario', s,
                              '--out', str(out / ('agent_offline_' + s + '.json'))])
             for s in ('shipping_increase', 'both_unavailable', 'no_change', 'injection')]
    results = []
    for name, command in jobs:
        record = run_check(name, [sys.executable, *command], out=out, env=env,
                           timeout_seconds=args.timeout_seconds)
        results.append(record)
        print(json.dumps(record), flush=True)
    node = shutil.which('node')
    if node:
        results.append(run_check('javascript_syntax', [node, '--check', 'app/web/app.js'],
                                 out=out, env=env, timeout_seconds=args.timeout_seconds))
    else:
        (out / 'javascript_syntax.log').write_text('node unavailable', encoding='utf-8')
        results.append({'check': 'javascript_syntax', 'exit_code': 127, 'timeout': False,
                        'timeout_seconds': args.timeout_seconds, 'unit_tests': 0,
                        'log': 'javascript_syntax.log'})
    files = [p for p in ROOT.rglob('*') if p.is_file() and p.suffix in {'.py', '.js', '.html', '.css', '.json'}
             and not any(x in p.parts for x in ('.venv', '__pycache__', 'evidence'))]
    report = {'checked_at_utc': datetime.now(timezone.utc).isoformat(),
              'status': 'passed' if all(x['exit_code'] == 0 for x in results) else 'failed',
              'unit_tests': sum(x['unit_tests'] for x in results), 'checks': results,
              'timeout_seconds_per_check': args.timeout_seconds,
              'python': sys.version, 'platform': platform.platform(), 'sqlite': sqlite3.sqlite_version,
              'dependencies': {x.metadata['Name']: x.version for x in importlib.metadata.distributions()},
              'live_model_calls': 0, 'real_payments': 0, 'official_psp_verified': False,
              'stage_one_complete': False,
              'scope': 'Fresh offline suites and synthetic scenarios only; storage incident, live model, merchant and PSP gates remain separate.',
              'source_sha256': {str(p.relative_to(ROOT)).replace('\\', '/'): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}}
    (out / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({k: report[k] for k in ('status', 'unit_tests', 'live_model_calls', 'stage_one_complete')}))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
