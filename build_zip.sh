#!/bin/sh
# Rebuild the distributable package and verify it before handing it over.
# Verification is part of the build: a zip that has not been extracted and
# imported from scratch has not been shown to work.
set -e
cd "$(dirname "$0")"
rm -rf golddesk/__pycache__ aurum_v2.zip
zip -qr aurum_v2.zip golddesk acceptance.py ambiguity.py run_backtest.py \
    export_mt5.py test_integration.py capture_proof.py signals_capture.py signals_evidence.py macro_vintage.py probability_eval.py horizon_stack.py \
    AURUM_V2_INTEGRATION_AUDIT.md \
    -x "*__pycache__*"

tmp=$(mktemp -d)
unzip -q aurum_v2.zip -d "$tmp"
( cd "$tmp" && python3 -c "
import glob, importlib, os, sys
from pathlib import Path
sys.path.insert(0, '.')
mods = sorted(os.path.basename(f)[:-3] for f in glob.glob('golddesk/*.py')
              if not f.endswith('__init__.py'))
bad = []
for m in mods:
    try:
        importlib.import_module('golddesk.' + m)
    except Exception as e:
        bad.append(f'{m}: {type(e).__name__}: {e}')
from golddesk.constitution import REGISTRY, verify_no_silent_restrictions
from golddesk.drift_audit import undeclared_thresholds
ok, _ = verify_no_silent_restrictions(Path('golddesk'))
thr = undeclared_thresholds(Path('golddesk'))
print(f'modules imported      : {len(mods)}  failures={len(bad)}')
for b in bad:
    print('   ', b)
print(f'silent restrictions   : {\"PASS\" if ok else \"FAIL\"}')
print(f'undeclared thresholds : {len(thr)}')
print(f'registry              : {len(REGISTRY)} restrictions')
if bad or not ok or thr:
    raise SystemExit('BUILD FAILED — not fit to send')
print('BUILD OK')
" )
rm -rf "$tmp"
ls -lh aurum_v2.zip
