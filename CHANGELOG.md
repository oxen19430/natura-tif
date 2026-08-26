# Changelog Natura Tif

Historique des déploiements en prod (`oxen19430.github.io/natura-tif`).

## 2026-04-30 09:37 CEST — sw.js v12

**auto-refresh cockpit + admin sur visibilitychange et online (anti-rebond 10s)**

Fichiers déployés :
- `.gitignore`
- `cockpit.html`

## 2026-04-30 10:30 CEST — sw.js v13

**redeploy v12**

Fichiers déployés :
- `.gitignore`
- `cockpit.html`
- `sw.js`

## 2026-04-30 11:30 CEST — sw.js v14

**fix bandeau MODE TEST + whitelist prod**

Fichiers déployés :
- `cockpit.html`
- `index.html`

## 2026-04-30 14:52 CEST — sw.js v15

**deploy.py: rollback + timeout 300s + admin/analytics dans DEPLOY_FILES + auto-refresh admin**

Fichiers déployés :
- `admin.html`
- `analytics.html`
- `scripts/deploy.py`

## 2026-05-02 10:34 CEST — sw.js v16

**test diagnostic**

Fichiers déployés :
- `index.html`
- `release.json`
- `scripts/deploy.py`
- `scripts/smoke_test.py`
- `serve.py`
- `sw.js`

## 2026-08-26 21:29 UTC+0200 — sw.js v16

**gitattributes LF, garde-fou cockpit retire, dry-run sans effet de bord**

Fichiers déployés :
- `.gitattributes`
- `.gitignore`
- `scripts/deploy.py`
- `serve.py`

## 2026-08-26 21:34 UTC+0200 — sw.js v16

**smoke test : retrait des pages cockpit/admin/analytics supprimees en mai**

Fichiers déployés :
- `scripts/smoke_test.py`

## 2026-08-26 21:58 UTC+0200 — sw.js v18

**compteur clients : compte les clientes et non les lignes (client_no) + sw.js ne cache plus l'API Supabase**

Fichiers déployés :
- `.gitignore`
- `index.html`
- `sw.js`

## 2026-08-26 22:29 UTC+0200 — sw.js v19

**sauvegarde sur fichier : export complet de la caisse sans passer par internet**

Fichiers déployés :
- `.gitignore`
- `index.html`
