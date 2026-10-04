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

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', required=True)
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
        result = subprocess.run([sys.executable, *command], cwd=ROOT, env=env,
                                capture_output=True, text=True, encoding='utf-8', timeout=240)
        (out / (name + '.log')).write_text(result.stdout + '\n' + result.stderr, encoding='utf-8')
        count = re.search(r'Ran (\d+) tests?', result.stderr)
        record = {'check': name, 'exit_code': result.returncode,
                  'unit_tests': int(count.group(1)) if count else 0, 'log': name + '.log'}
        results.append(record)
        print(json.dumps(record), flush=True)
    node = shutil.which('node')
    js_result = subprocess.run([node, '--check', 'app/web/app.js'], cwd=ROOT,
                               capture_output=True, text=True, encoding='utf-8') if node else None
    (out / 'javascript_syntax.log').write_text(js_result.stdout + js_result.stderr if js_result else 'node unavailable', encoding='utf-8')
    results.append({'check': 'javascript_syntax', 'exit_code': js_result.returncode if js_result else 127,
                    'unit_tests': 0, 'log': 'javascript_syntax.log'})
    files = [p for p in ROOT.rglob('*') if p.is_file() and p.suffix in {'.py', '.js', '.html', '.css', '.json'}
             and not any(x in p.parts for x in ('.venv', '__pycache__', 'evidence'))]
    report = {'checked_at_utc': datetime.now(timezone.utc).isoformat(),
              'status': 'passed' if all(x['exit_code'] == 0 for x in results) else 'failed',
              'unit_tests': sum(x['unit_tests'] for x in results), 'checks': results,
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
