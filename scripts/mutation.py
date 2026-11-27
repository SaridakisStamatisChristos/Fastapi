"""Curated behavioral mutants, isolated from the working tree."""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
MUTANTS = [
    ('allow_extra', "extra='forbid'", "extra='ignore'", 'test_invalid'),
    ('allow_empty', 'min_length=1, max_length=200', 'min_length=0, max_length=200', 'test_invalid'),
    ('allow_long', 'max_length=200', 'max_length=201', 'test_invalid'),
    ('disable_strip', 'str_strip_whitespace=True', 'str_strip_whitespace=False', 'test_crud'),
    ('reverse_order', 'ORDER BY id LIMIT', 'ORDER BY id DESC LIMIT', 'test_pagination_concurrency'),
    ('ignore_offset', '(limit, offset)', '(limit, 0)', 'test_pagination_concurrency'),
    ('wrong_update', '(body.title, body.done, id)', '(body.title, False, id)', 'test_crud'),
    ('delete_missing_success', "if not db.execute('DELETE FROM tasks WHERE id=?', (id,)).rowcount:", "if db.execute('DELETE FROM tasks WHERE id=?', (id,)).rowcount < 0:", 'test_crud'),
    ('runtime_recreate', "else '?mode=rw'", "else '?mode=rwc'", 'test_missing_database_not_recreated'),
    ('readiness_read_only', "db.execute('BEGIN IMMEDIATE')\n                db.execute('SELECT id, title, done FROM tasks LIMIT 1').fetchone()\n                db.execute('UPDATE tasks SET done=done WHERE 0')", "db.execute('SELECT id, title, done FROM tasks LIMIT 1').fetchone()", 'test_readiness_write_lock'),
    ('wrong_db_status', "JSONResponse(status_code=503", "JSONResponse(status_code=500", 'test_trigger_failure_atomicity'),
    ('omit_put_contract', "@api.put('/tasks/{id}', response_model=Task, responses=DB_ERROR | VALIDATION | NOT_FOUND)", "@api.put('/tasks/{id}', response_model=Task)", 'test_openapi_error_contract'),
]

def main():
    source = (ROOT / 'app.py').read_text(encoding='utf-8')
    results = []
    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        shutil.copytree(ROOT / 'tests', work / 'tests', ignore=shutil.ignore_patterns('__pycache__'))
        for name, old, new, selector in MUTANTS:
            if source.count(old) != 1:
                raise RuntimeError(f'{name}: expected exactly one mutation site')
            mutated = source.replace(old, new, 1)
            compile(mutated, 'app.py', 'exec')
            for label, code in [('baseline', source), ('mutant', mutated)]:
                (work / 'app.py').write_text(code, encoding='utf-8')
                shutil.rmtree(work / '__pycache__', ignore_errors=True)
                report = work / 'result.xml'
                report.unlink(missing_ok=True)
                run = subprocess.run([sys.executable, '-m', 'pytest', '-q', '-k', selector, '--junitxml', str(report)], cwd=work, capture_output=True, text=True, timeout=60)
                if not report.exists():
                    raise RuntimeError(run.stdout + run.stderr)
                cases = ET.parse(report).findall('.//testcase')
                failures = [case for case in cases if case.find('failure') is not None]
                errors = [case for case in cases if case.find('error') is not None]
                if not cases or errors or run.returncode not in (0, 1):
                    raise RuntimeError(run.stdout + run.stderr)
                if label == 'baseline' and (run.returncode or failures):
                    raise RuntimeError('Baseline failed: ' + run.stdout)
                if label == 'mutant':
                    state = 'killed' if run.returncode == 1 and failures else 'survived'
                    results.append({'mutant': name, 'state': state, 'failed_tests': [case.attrib['name'] for case in failures]})
                    print(name, state, flush=True)
    killed = sum(r['state'] == 'killed' for r in results)
    output = {'killed': killed, 'total': len(results), 'results': results}
    (ROOT / 'mutation-report.json').write_text(json.dumps(output, indent=2)+'\n', encoding='utf-8')
    return 0 if killed == len(results) else 1

if __name__ == '__main__':
    sys.exit(main())
