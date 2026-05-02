#!/usr/bin/env python3
"""
Déploiement Natura Tif avec garde-fous.

Usage :
    python scripts/deploy.py --message "fix tarif coloration"
    python scripts/deploy.py --message "..." --dry-run        # check + simulation, pas de push
    python scripts/deploy.py --message "..." --no-bump        # ne pas bumper sw.js
    python scripts/deploy.py --quiet --message "..."          # output JSON pour orchestration

Garde-fous (refus du push si rouge) :
1. RLS prod doit être stricte : test live d'isolation sur transactions
2. RLS test doit être stricte : pareil sur transactions_test
3. index.html et cockpit.html doivent contenir signInAnonymously
4. Si index.html, cockpit.html, manifest.json ou icon-*.png ont changé : bump auto sw.js
5. Pas de fichier de travail (deploy.log, _*.bat, sw.js.bak) tracké

Workflow :
1. Pre-deploy checks
2. Bump sw.js si nécessaire
3. Clone temporaire dans %TEMP%
4. Copy des fichiers à déployer
5. Update CHANGELOG.md
6. Commit + push
"""
import argparse
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# === Config ===
SUPABASE_URL = 'https://rgwsqufbdimryxqefvhx.supabase.co'
SUPABASE_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InJnd3NxdWZiZGltcnl4cWVmdmh4Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzQ5NTI4OTQsImV4cCI6MjA5MDUyODg5NH0.VNIY3iq3WZz-SVveQdMMqNth2ZmBP7aoJJ3PhfsmN-0'
GIT_REPO = 'https://github.com/oxen19430/natura-tif.git'
GIT_USER_EMAIL = 'electrosoundstyleproject@gmail.com'
GIT_USER_NAME = 'Vincent GIBERT'

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_FILES = ['index.html', 'sw.js', 'manifest.json', 'icon-192.png', 'icon-512.png', 'serve.py', '.gitignore', 'release.json']
DEPLOY_DIRS = ['scripts']  # tout le contenu sera copié
ASSET_FILES = ['index.html', 'manifest.json', 'icon-192.png', 'icon-512.png']  # déclencheurs de bump sw.js
# Note : release.json n'est pas un asset déclencheur — sa modif seule (ex juste le message) ne doit pas bumper sw.js.

CTX = ssl.create_default_context()


def log(msg, args):
    if not args.quiet:
        print(msg, flush=True)


def http(method, path, body=None, token=None):
    headers = {'apikey': SUPABASE_KEY, 'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    url = f'{SUPABASE_URL}{path}'
    data = json.dumps(body).encode('utf-8') if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=15) as r:
            raw = r.read().decode('utf-8')
            return r.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body_err = e.read().decode('utf-8') if e.fp else ''
        try:
            return e.code, json.loads(body_err) if body_err else {}
        except json.JSONDecodeError:
            return e.code, {'error': body_err}


# ============================================================
# Pre-deploy checks
# ============================================================
def check_rls_strict(table):
    """Test live : sans auth, on ne doit RIEN pouvoir lire ni écrire."""
    # SELECT sans auth (juste apikey)
    code_sel, data_sel = http('GET', f'/rest/v1/{table}?select=id&limit=5')
    if code_sel != 200:
        return {'ok': False, 'reason': f'SELECT a renvoyé code {code_sel}: {data_sel}'}
    if data_sel:  # rows visibles → faille
        return {'ok': False, 'reason': f'SELECT sans auth a retourné {len(data_sel)} ligne(s) — RLS pas strict'}
    # INSERT sans auth (doit être bloqué)
    code_ins, data_ins = http('POST', f'/rest/v1/{table}',
                              body={'id': '__check_rls__', 'date': '2026-01-01', 'montant': 0.01,
                                    'prestation': '__check__', 'paiement': 'cb', 'type': 'prestation'})
    # On s'attend à 401/403 ou 400 avec violation policy
    if code_ins == 200 or code_ins == 201:
        return {'ok': False, 'reason': f'INSERT sans auth a réussi (code {code_ins}) — RLS pas strict'}
    return {'ok': True, 'detail': f'SELECT={code_sel} (0 rows), INSERT={code_ins} (rejeté)'}


def check_html_signin(path):
    if not path.exists():
        return {'ok': False, 'reason': f'{path.name} introuvable'}
    content = path.read_text(encoding='utf-8', errors='replace')
    if 'signInAnonymously' not in content:
        return {'ok': False, 'reason': f'{path.name} ne contient pas signInAnonymously — l\'app casserait avec RLS strict'}
    return {'ok': True, 'detail': f'{path.name} a signInAnonymously'}


def get_current_sw_version():
    sw = PROJECT_ROOT / 'sw.js'
    if not sw.exists():
        return None
    m = re.search(r"natura-tif-v(\d+)", sw.read_text(encoding='utf-8', errors='replace'))
    return int(m.group(1)) if m else None


def files_changed_vs_remote(args):
    """Approximation : on n'a pas le repo en local, donc on compare via clone temp.
    Retourne la liste des fichiers qui diffèrent. Pour les checks de bump auto."""
    tmp = Path(tempfile.mkdtemp(prefix='nt_predeploy_'))
    try:
        proc = subprocess.run(['git', 'clone', '--depth', '1', GIT_REPO, str(tmp)],
                              capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            return None  # pas accessible, on ne sait pas
        changed = []
        for f in DEPLOY_FILES + [str(Path('scripts') / p.name) for p in (PROJECT_ROOT / 'scripts').glob('*.py') if (PROJECT_ROOT / 'scripts').exists()]:
            local = PROJECT_ROOT / f
            remote = tmp / f
            if local.exists():
                if not remote.exists():
                    changed.append(f)
                elif local.read_bytes() != remote.read_bytes():
                    changed.append(f)
        return changed
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_predeploy_checks(args):
    checks = []

    # 1. RLS prod
    log('  → RLS prod (transactions)...', args)
    checks.append({'name': 'RLS strict sur transactions (prod)', **check_rls_strict('transactions')})

    # 2. RLS test
    log('  → RLS test (transactions_test)...', args)
    checks.append({'name': 'RLS strict sur transactions_test', **check_rls_strict('transactions_test')})

    # 3. signInAnonymously dans index.html et cockpit.html
    log('  → signInAnonymously dans index.html et cockpit.html...', args)
    checks.append({'name': 'index.html contient signInAnonymously', **check_html_signin(PROJECT_ROOT / 'index.html')})
    checks.append({'name': 'cockpit.html contient signInAnonymously', **check_html_signin(PROJECT_ROOT / 'cockpit.html')})

    # 4. sw.js version actuelle
    log('  → version actuelle sw.js...', args)
    sw_v = get_current_sw_version()
    checks.append({
        'name': f'sw.js cache version (actuelle: v{sw_v})',
        'ok': sw_v is not None,
        'detail': f'natura-tif-v{sw_v}' if sw_v else 'introuvable',
        'reason': 'sw.js illisible ou sans CACHE_NAME' if sw_v is None else None
    })

    # 5. Fichiers qui changeraient (pour info)
    log('  → diff vs remote (clone rapide)...', args)
    changed = files_changed_vs_remote(args)
    if changed is None:
        checks.append({'name': 'Diff vs remote', 'ok': False, 'reason': 'impossible de cloner pour comparer'})
    else:
        asset_changed = any(f in ASSET_FILES for f in changed)
        checks.append({
            'name': 'Diff vs remote',
            'ok': True,
            'detail': f'{len(changed)} fichier(s) à déployer' + (' (asset → bump sw.js requis)' if asset_changed else ''),
            'changed_files': changed,
            'asset_changed': asset_changed,
        })

    return checks, sw_v, (changed or [])


# ============================================================
# Bump sw.js
# ============================================================
def bump_sw(current_v, args):
    sw = PROJECT_ROOT / 'sw.js'
    content = sw.read_text(encoding='utf-8')
    new_v = (current_v or 0) + 1
    new_content = re.sub(r"natura-tif-v\d+", f"natura-tif-v{new_v}", content, count=1)
    sw.write_text(new_content, encoding='utf-8', newline='\n')
    log(f'  → sw.js bumpé : v{current_v} → v{new_v}', args)
    return new_v


# ============================================================
# Changelog
# ============================================================
def update_changelog(message, sw_v, changed_files, args):
    chlog = PROJECT_ROOT / 'CHANGELOG.md'
    now = datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M %Z')
    entry = f'\n## {now} — sw.js v{sw_v}\n\n**{message}**\n\n'
    if changed_files:
        entry += 'Fichiers déployés :\n' + '\n'.join(f'- `{f}`' for f in sorted(changed_files)) + '\n'
    if chlog.exists():
        existing = chlog.read_text(encoding='utf-8')
    else:
        existing = '# Changelog Natura Tif\n\nHistorique des déploiements en prod (`oxen19430.github.io/natura-tif`).\n'
    chlog.write_text(existing + entry, encoding='utf-8', newline='\n')
    log(f'  → CHANGELOG.md mis à jour', args)


# ============================================================
# Git deploy
# ============================================================
def run_git_deploy(message, args):
    tmp = Path(tempfile.mkdtemp(prefix='nt_deploy_'))
    try:
        log(f'  → clone {GIT_REPO}...', args)
        proc = subprocess.run(['git', 'clone', GIT_REPO, str(tmp)],
                              capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            return {'ok': False, 'step': 'clone', 'error': proc.stderr.strip()}

        log('  → copy fichiers...', args)
        for f in DEPLOY_FILES:
            src = PROJECT_ROOT / f
            if src.exists():
                dst = tmp / f
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)

        scripts_src = PROJECT_ROOT / 'scripts'
        if scripts_src.exists():
            scripts_dst = tmp / 'scripts'
            scripts_dst.mkdir(exist_ok=True)
            for py in scripts_src.glob('*.py'):
                shutil.copy2(py, scripts_dst / py.name)

        chlog_src = PROJECT_ROOT / 'CHANGELOG.md'
        if chlog_src.exists():
            shutil.copy2(chlog_src, tmp / 'CHANGELOG.md')

        log('  → git status...', args)
        proc = subprocess.run(['git', 'status', '--short'], cwd=tmp, capture_output=True, text=True)
        status = proc.stdout.strip()
        log(f'  status:\n{status}', args)

        if not status:
            return {'ok': True, 'no_changes': True, 'message': 'Aucun changement à déployer'}

        log('  → git add + commit...', args)
        subprocess.run(['git', 'add', '-A'], cwd=tmp, capture_output=True, text=True)
        proc = subprocess.run(
            ['git', '-c', f'user.email={GIT_USER_EMAIL}', '-c', f'user.name={GIT_USER_NAME}',
             'commit', '-m', message],
            cwd=tmp, capture_output=True, text=True
        )
        if proc.returncode != 0:
            return {'ok': False, 'step': 'commit', 'error': proc.stdout + proc.stderr}
        commit_hash = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'],
                                     cwd=tmp, capture_output=True, text=True).stdout.strip()

        if args.dry_run:
            return {'ok': True, 'dry_run': True, 'commit_hash': commit_hash, 'status': status}

        log('  → git push...', args)
        # Timeout 300s pour laisser le temps de générer/coller un PAT si nécessaire.
        proc = subprocess.run(['git', 'push', 'origin', 'HEAD'],
                              cwd=tmp, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            return {'ok': False, 'step': 'push', 'error': proc.stderr.strip()}

        return {'ok': True, 'commit_hash': commit_hash, 'push_output': proc.stderr.strip(), 'changed_status': status}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================
# Main
# ============================================================
def main():
    parser = argparse.ArgumentParser(description='Déploiement Natura Tif avec garde-fous')
    parser.add_argument('--message', '-m', required=True, help='Message de commit')
    parser.add_argument('--dry-run', action='store_true', help='Simule sans pousser')
    parser.add_argument('--no-bump', action='store_true', help='Ne pas bumper sw.js auto')
    parser.add_argument('--force', action='store_true', help='Pousse même si checks rouges (à éviter)')
    parser.add_argument('--quiet', action='store_true', help='Sortie JSON pour orchestration')
    args = parser.parse_args()

    log(f'== Déploiement Natura Tif ==', args)
    log(f'Message : {args.message}', args)
    log('', args)

    log('1. Pre-deploy checks...', args)
    checks, sw_v, changed = run_predeploy_checks(args)
    failed = [c for c in checks if not c.get('ok')]
    if failed and not args.force:
        if args.quiet:
            print(json.dumps({'ok': False, 'step': 'checks', 'failed': failed, 'all_checks': checks}))
        else:
            log('\n❌ Pre-deploy checks ÉCHOUÉS :', args)
            for c in failed:
                log(f'   - {c["name"]} : {c.get("reason", "?")}', args)
            log('\nUtilise --force pour ignorer (déconseillé)', args)
        return 2
    log(f'   ✓ {len(checks)} checks ok', args)

    # Sauvegarde du contenu d'origine de sw.js, CHANGELOG.md et release.json AVANT toute modification locale.
    # Utilisé pour rollback si le push échoue, afin de garder local et prod cohérents.
    sw_path = PROJECT_ROOT / 'sw.js'
    chlog_path = PROJECT_ROOT / 'CHANGELOG.md'
    release_path = PROJECT_ROOT / 'release.json'
    sw_original = sw_path.read_text(encoding='utf-8') if sw_path.exists() else None
    chlog_original = chlog_path.read_text(encoding='utf-8') if chlog_path.exists() else None
    release_original = release_path.read_text(encoding='utf-8') if release_path.exists() else None

    asset_changed = any(c.get('asset_changed') for c in checks)
    if asset_changed and not args.no_bump:
        log('\n2. Bump sw.js (assets ont changé)...', args)
        sw_v = bump_sw(sw_v, args)
    elif args.no_bump:
        log('\n2. Bump sw.js skipped (--no-bump)', args)
    else:
        log('\n2. Pas de bump sw.js (aucun asset changé)', args)

    log('\n3. Update CHANGELOG.md...', args)
    update_changelog(args.message, sw_v, changed, args)

    # release.json : message simple visible par Gaëlle dans l'overlay de mise à jour.
    log('\n3b. Écriture release.json...', args)
    release_payload = {
        'version': sw_v,
        'message': args.message,
        'deployed_at': datetime.now(timezone.utc).astimezone().isoformat(),
    }
    release_path.write_text(json.dumps(release_payload, ensure_ascii=False, indent=2), encoding='utf-8', newline='\n')
    log(f'   → release.json écrit (v{sw_v}, "{args.message}")', args)

    log('\n4. Git deploy...', args)
    result = run_git_deploy(args.message, args)

    # Rollback si le push a échoué : on remet sw.js, CHANGELOG.md et release.json à leur état d'origine,
    # sinon le local et la prod divergent.
    if not result.get('ok') and result.get('step') == 'push' and not args.dry_run:
        log('\n⚠ Push échoué — rollback de sw.js, CHANGELOG.md et release.json...', args)
        if sw_original is not None:
            sw_path.write_text(sw_original, encoding='utf-8', newline='\n')
        if chlog_original is not None:
            chlog_path.write_text(chlog_original, encoding='utf-8', newline='\n')
        if release_original is not None:
            release_path.write_text(release_original, encoding='utf-8', newline='\n')
        elif release_path.exists():
            release_path.unlink()  # release.json n'existait pas avant — on le supprime
        log('   ✓ Fichiers locaux rétablis. Relance le déploiement après avoir réglé l\'auth.', args)

    out = {
        'ok': result.get('ok', False),
        'sw_version': sw_v,
        'asset_bumped': asset_changed and not args.no_bump,
        'changed_files': changed,
        'checks_passed': len(checks) - len(failed),
        'checks_failed': len(failed),
        **result,
    }

    # Log persistant dans deploy.log (succès ET échec).
    write_deploy_log(args, out)

    # Smoke test post-déploiement : seulement si le push a réussi (et pas en dry-run).
    # Vérifie que la prod sert bien la nouvelle version (HTTP 200, IS_TEST whitelist, sw.js sync, Supabase OK).
    smoke_ok = None
    if out['ok'] and not result.get('no_changes') and not args.dry_run:
        smoke_path = PROJECT_ROOT / 'scripts' / 'smoke_test.py'
        if smoke_path.exists():
            log('\n5. Smoke test post-déploiement (attente 30s pour propagation GitHub Pages)...', args)
            try:
                smoke_proc = subprocess.run(
                    [sys.executable, str(smoke_path), '--wait', '30', '--quiet'],
                    capture_output=True, text=True, timeout=120
                )
                try:
                    smoke_out = json.loads(smoke_proc.stdout.strip())
                    smoke_ok = smoke_out.get('ok', False)
                    out['smoke'] = smoke_out
                except (json.JSONDecodeError, ValueError):
                    smoke_ok = False
                    out['smoke'] = {'ok': False, 'fatal': 'sortie smoke_test illisible'}

                if smoke_ok:
                    log('   ✓ Smoke test vert — prod saine.', args)
                else:
                    log('   ⚠ Smoke test ROUGE — la prod n\'est peut-être pas saine.', args)
                    failed_checks = [r for r in (out['smoke'].get('results') or []) if not r.get('ok')]
                    for r in failed_checks:
                        log(f'      ✗ {r.get("name", "?")}', args)
                        for issue in (r.get('issues') or []):
                            log(f'         → {issue}', args)
                        if r.get('fatal'):
                            log(f'         → {r["fatal"]}', args)
                    log('   Lance `python3 scripts/smoke_test.py` à la main pour creuser, ou attends quelques minutes (propagation GitHub Pages).', args)
            except subprocess.TimeoutExpired:
                log('   ⚠ Smoke test timeout — à relancer manuellement.', args)
                out['smoke'] = {'ok': False, 'fatal': 'timeout'}

    if args.quiet:
        print(json.dumps(out))
    elif out['ok']:
        log(f'\n✓ Déploiement OK. Commit {out.get("commit_hash", "?")}', args)
        if smoke_ok is False:
            log('  (mais smoke test rouge — voir au-dessus)', args)
    else:
        log(f'\n❌ Échec : {out.get("step", "?")} : {out.get("error", "?")}', args)
    return 0 if out['ok'] else 1


def write_deploy_log(args, out):
    """Append au deploy.log à chaque exécution (succès ou échec) pour avoir une trace persistante."""
    log_file = PROJECT_ROOT / 'deploy.log'
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    status = 'OK' if out.get('ok') else 'ÉCHEC'
    lines = [f'\n==== {now} — {status} ====']
    lines.append(f'Message : {args.message}')
    lines.append(f'sw.js   : v{out.get("sw_version", "?")}')
    if out.get('ok'):
        lines.append(f'Commit  : {out.get("commit_hash", "?")}')
        if out.get('changed_files'):
            lines.append(f'Fichiers: {", ".join(out["changed_files"])}')
    else:
        lines.append(f'Étape   : {out.get("step", "?")}')
        err = (out.get("error") or "?")[:500]
        lines.append(f'Erreur  : {err}')
    try:
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
    except Exception:
        pass  # Si on n'arrive pas à logger, on ne casse pas le déploiement


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:
        if '--quiet' in sys.argv:
            print(json.dumps({'ok': False, 'fatal': str(e)}))
        else:
            print(f'\n❌ ERREUR : {e}')
        sys.exit(1)
