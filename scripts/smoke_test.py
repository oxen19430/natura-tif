#!/usr/bin/env python3
"""
Smoke test post-déploiement Natura Tif.

Vérifie en quelques secondes que la prod servie par GitHub Pages est saine :
- 4 pages HTML accessibles (index, cockpit, admin, analytics)
- Chacune a la condition IS_TEST whitelist sur oxen19430.github.io
- Le bandeau MODE TEST a bien style="display:none" (pas de !important réintroduit)
- sw.js prod = version locale (donc le déploiement a bien propagé)
- Supabase auth anonyme + select transactions → OK

Usage :
    python3 scripts/smoke_test.py                # full, sortie texte
    python3 scripts/smoke_test.py --quiet        # sortie JSON pour orchestration
    python3 scripts/smoke_test.py --no-supabase  # skip le check Supabase

Codes de sortie : 0 (OK), 1 (rouge — vraie défaillance), 2 (jaune — warning).
"""
import argparse
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# === Config ===
PROD_BASE = 'https://oxen19430.github.io/natura-tif'
SUPABASE_URL = 'https://rgwsqufbdimryxqefvhx.supabase.co'
SUPABASE_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InJnd3NxdWZiZGltcnl4cWVmdmh4Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzQ5NTI4OTQsImV4cCI6MjA5MDUyODg5NH0.VNIY3iq3WZz-SVveQdMMqNth2ZmBP7aoJJ3PhfsmN-0'
HOSTNAME_PROD = 'oxen19430.github.io'

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PAGES = ['index.html', 'cockpit.html', 'admin.html', 'analytics.html']

CTX = ssl.create_default_context()


def fetch(url, timeout=15):
    """Récupère le contenu d'une URL. Lève en cas d'erreur HTTP ou réseau."""
    req = urllib.request.Request(url, headers={'User-Agent': 'natura-tif-smoke-test/1'})
    with urllib.request.urlopen(req, context=CTX, timeout=timeout) as resp:
        return resp.status, resp.read().decode('utf-8', errors='replace')


def get_local_sw_version():
    """Lit la version courante de sw.js en local (CACHE_NAME = 'natura-tif-vN')."""
    sw = PROJECT_ROOT / 'sw.js'
    if not sw.exists():
        return None
    m = re.search(r"natura-tif-v(\d+)", sw.read_text(encoding='utf-8'))
    return int(m.group(1)) if m else None


def get_prod_sw_version():
    """Lit la version de sw.js servie par GitHub Pages."""
    try:
        _, body = fetch(f'{PROD_BASE}/sw.js')
        m = re.search(r"natura-tif-v(\d+)", body)
        return int(m.group(1)) if m else None
    except Exception:
        return None


def check_page(name):
    """Vérifie qu'une page HTML est saine en prod."""
    issues = []
    url = f'{PROD_BASE}/{name}'
    try:
        status, body = fetch(url)
    except urllib.error.HTTPError as e:
        return {'name': name, 'ok': False, 'fatal': f'HTTP {e.code}', 'url': url}
    except Exception as e:
        return {'name': name, 'ok': False, 'fatal': f'fetch error : {e}', 'url': url}

    if status != 200:
        issues.append(f'HTTP {status} (attendu 200)')

    # 1. Condition IS_TEST whitelist (avec fallback sur la version inline du cockpit/admin/analytics)
    has_whitelist_const = f"hostname !== '{HOSTNAME_PROD}'" in body
    has_whitelist_inline = f"location.hostname!=='{HOSTNAME_PROD}'" in body
    if not (has_whitelist_const or has_whitelist_inline):
        issues.append(f"condition IS_TEST whitelist sur {HOSTNAME_PROD} introuvable")

    # 2. Bandeau MODE TEST présent dans le HTML mais avec style="display:none" et SANS !important
    if 'id="test-banner"' in body:
        if 'display: flex !important' in body or 'display:flex !important' in body:
            issues.append("règle CSS '#test-banner { display: flex !important }' présente — c'est le bug du 30 avril, à supprimer")
        if 'style="display:none' not in body:
            issues.append("bandeau test-banner sans style='display:none' inline — il pourrait s'afficher avant que init() le cache")

    return {
        'name': name,
        'ok': len(issues) == 0,
        'url': url,
        'issues': issues,
    }


def check_sw_version():
    """Compare la version sw.js locale et celle servie par GitHub Pages."""
    local = get_local_sw_version()
    prod = get_prod_sw_version()
    issues = []
    if local is None:
        issues.append('version locale sw.js illisible')
    if prod is None:
        issues.append('version prod sw.js inaccessible (404 ?)')
    if local is not None and prod is not None and local != prod:
        issues.append(f'desync : local v{local} ≠ prod v{prod}. Le push n\'est probablement pas passé.')
    return {
        'name': 'sw.js version',
        'ok': len(issues) == 0,
        'local_version': local,
        'prod_version': prod,
        'issues': issues,
    }


def check_supabase():
    """Vérifie auth anonyme + select 1 ligne sur transactions (la lecture publique passe avec la clé anon)."""
    issues = []
    try:
        req = urllib.request.Request(
            f'{SUPABASE_URL}/rest/v1/transactions?select=id&limit=1',
            headers={
                'apikey': SUPABASE_KEY,
                'Authorization': f'Bearer {SUPABASE_KEY}',
                'User-Agent': 'natura-tif-smoke-test/1',
            }
        )
        with urllib.request.urlopen(req, context=CTX, timeout=15) as resp:
            if resp.status != 200:
                issues.append(f'HTTP {resp.status} sur /rest/v1/transactions')
    except urllib.error.HTTPError as e:
        issues.append(f'HTTP {e.code} : {e.read().decode("utf-8", errors="replace")[:200]}')
    except Exception as e:
        issues.append(f'erreur : {e}')
    return {
        'name': 'Supabase /rest/v1/transactions',
        'ok': len(issues) == 0,
        'issues': issues,
    }


def main():
    parser = argparse.ArgumentParser(description='Smoke test post-déploiement Natura Tif')
    parser.add_argument('--quiet', action='store_true', help='Sortie JSON pour orchestration')
    parser.add_argument('--no-supabase', action='store_true', help='Skip le test Supabase (utile en CI sans clé)')
    parser.add_argument('--wait', type=int, default=0, help='Attente en secondes avant de tester (utile post-deploy pour propagation GitHub Pages)')
    args = parser.parse_args()

    if args.wait > 0 and not args.quiet:
        print(f'Attente {args.wait}s pour propagation...', flush=True)
        time.sleep(args.wait)
    elif args.wait > 0:
        time.sleep(args.wait)

    if not args.quiet:
        print('== Smoke test Natura Tif ==', flush=True)
        print(f'Base prod : {PROD_BASE}', flush=True)
        print('', flush=True)

    results = []
    for page in PAGES:
        r = check_page(page)
        results.append(r)
        if not args.quiet:
            mark = '✓' if r['ok'] else '✗'
            print(f'  {mark} {r["name"]}', flush=True)
            for issue in r.get('issues', []):
                print(f'      → {issue}', flush=True)
            if r.get('fatal'):
                print(f'      → {r["fatal"]}', flush=True)

    sw_check = check_sw_version()
    results.append(sw_check)
    if not args.quiet:
        mark = '✓' if sw_check['ok'] else '✗'
        print(f'  {mark} {sw_check["name"]} (local v{sw_check["local_version"]} / prod v{sw_check["prod_version"]})', flush=True)
        for issue in sw_check.get('issues', []):
            print(f'      → {issue}', flush=True)

    if not args.no_supabase:
        sb_check = check_supabase()
        results.append(sb_check)
        if not args.quiet:
            mark = '✓' if sb_check['ok'] else '✗'
            print(f'  {mark} {sb_check["name"]}', flush=True)
            for issue in sb_check.get('issues', []):
                print(f'      → {issue}', flush=True)

    failures = [r for r in results if not r['ok']]
    out = {
        'ok': len(failures) == 0,
        'checks_total': len(results),
        'checks_failed': len(failures),
        'results': results,
    }

    if args.quiet:
        print(json.dumps(out))
    else:
        print('', flush=True)
        if out['ok']:
            print(f'✓ Smoke test OK ({len(results)} checks)', flush=True)
        else:
            print(f'✗ {len(failures)}/{len(results)} checks rouges', flush=True)
            print('  → Inspecte les détails ci-dessus.', flush=True)

    return 0 if out['ok'] else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:
        if '--quiet' in sys.argv:
            print(json.dumps({'ok': False, 'fatal': str(e)}))
        else:
            print(f'\n❌ ERREUR : {e}')
        sys.exit(1)
