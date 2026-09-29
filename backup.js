/* Complete, append-only backups. Never restores or modifies cash-register data. */
(function(root) {
  'use strict';
  const KEYS = ['nt_transactions', 'nt_tarifs', 'nt_produits', 'nt_fonds', 'nt_fermetures', 'nt_settings'];
  const TABLE = 'tablet_snapshots';
  const MAX_BYTES = 10 * 1024 * 1024;
  const uuid = c => c.randomUUID();
  async function digest(c, text) {
    return Array.from(new Uint8Array(await c.subtle.digest('SHA-256', new TextEncoder().encode(text))))
      .map(b => b.toString(16).padStart(2, '0')).join('');
  }
  function capture(storage, prefix, mode) {
    const donnees = {};
    for (const key of KEYS) {
      const raw = storage.getItem(prefix + key);
      if (raw !== null) {
        try { donnees[key] = JSON.parse(raw); }
        catch (_) { throw new Error('Une donnée locale est illisible. Exportez une copie sur fichier.'); }
        const array = ['nt_transactions', 'nt_tarifs', 'nt_produits', 'nt_fermetures'].includes(key);
        // Tariffs are grouped by category, rather than stored as a list.
        if (key !== 'nt_tarifs' && array && !Array.isArray(donnees[key])) throw new Error('Format local invalide : ' + key);
        if ((!array || key === 'nt_tarifs') && (!donnees[key] || typeof donnees[key] !== 'object' || Array.isArray(donnees[key]))) throw new Error('Format local invalide : ' + key);
      }
    }
    const tx = donnees.nt_transactions || [];
    const ids = new Set();
    let cents = 0;
    for (const row of tx) {
      if (!row || typeof row.id !== 'string' || !row.id || ids.has(row.id) ||
          !/^\d{4}-\d{2}-\d{2}$/.test(row.date) ||
          !['number', 'string'].includes(typeof row.montant) || String(row.montant).trim() === '' ||
          !Number.isFinite(Number(row.montant))) throw new Error('Une opération locale doit être vérifiée avant la sauvegarde.');
      ids.add(row.id);
      cents += Math.round(Number(row.montant) * 100);
    }
    if (!Number.isSafeInteger(cents)) throw new Error('Total hors limites.');
    const dates = tx.map(t => t.date).sort();
    const source = JSON.stringify({ mode, donnees });
    return { source, donnees, resume: { transactions: tx.length, non_synchronisees: tx.filter(t => !t._synced).length,
      premiere_date: dates[0] || null, derniere_date: dates[dates.length - 1] || null, total_euros: cents / 100 } };
  }
  function createManager(options) {
    const { storage, prefix, mode, crypto: c, client, authenticate } = options;
    const now = options.now || (() => new Date());
    const online = options.online || (() => true);
    const visible = options.visible || (() => true);
    const notify = options.notify || (() => {});
    let busy = false, dirty = true, timer = null, lastError = null;
    function read(key) { try { return JSON.parse(storage.getItem(prefix + key)); } catch (_) { return null; } }
    function write(key, value) { storage.setItem(prefix + key, JSON.stringify(value)); }
    function state(status, message, extra) {
      const result = { status, message, last: read('nt_backup_last'), mode, ...extra };
      notify(result);return result;
    }
    function changed() {
      dirty = true;
      state('pending', 'Des changements restent à sauvegarder.');
      if (!timer) timer = setTimeout(() => { timer = null; run(); }, 30000);
    }
    async function request(builder) {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 30000);
      try {
        const result = await builder.abortSignal(controller.signal);
        if (result.error) throw Object.assign(new Error(result.error.message || 'Erreur de sauvegarde'), { code: result.error.code });
        return result.data;
      } finally { clearTimeout(timeout); }
    }
    async function openSession() {
      const sb = client();
      if (!sb) throw new Error('Sauvegarde en ligne non configurée.');
      if (authenticate) await authenticate();
      const current = await sb.auth.getSession();
      if (current.error) throw current.error;
      let user = current.data?.session?.user;
      if (!user) {
        const signed = await sb.auth.signInAnonymously();
        if (signed.error || !signed.data?.session?.user) throw signed.error || new Error('Connexion à la sauvegarde impossible.');
        user = signed.data.session.user;
      }
      return { sb, user };
    }
    async function session() {
      let timeout;
      try {
        return await Promise.race([openSession(), new Promise((_, reject) => {
          timeout = setTimeout(() => reject(new Error('Connexion à la sauvegarde trop lente.')), 30000);
        })]);
      } finally { clearTimeout(timeout); }
    }
    async function run(force = false) {
      if (busy) return state('working', 'Sauvegarde complète en cours… Gardez l’application ouverte.');
      if (!visible() && !force) return;
      if (!online()) return state('offline', 'Hors ligne. Connectez la tablette puis gardez Natura’tif ouvert.');
      if (!dirty && !force && !lastError) return;
      busy = true;
      state('working', 'Sauvegarde complète en cours… Gardez l’application ouverte.');
      try {
        const shot = capture(storage, prefix, mode);
        const hash = await digest(c, shot.source);
        const { sb, user } = await session();
        let device = read('nt_backup_device');
        if (typeof device !== 'string' || !/^[0-9a-f-]{36}$/i.test(device)) { device = uuid(c);write('nt_backup_device', device); }
        const last = read('nt_backup_last');
        let row = null;
        if (last?.content_hash === hash && last.owner_id === user.id && last.device_id === device) {
          row = await request(sb.from(TABLE).select('*').eq('id', last.id).eq('mode', mode).maybeSingle());
          if (row) {
            const envelope = JSON.parse(row.payload);
            if (row.payload_sha256 !== await digest(c, row.payload) ||
                JSON.stringify({ mode: envelope.mode, donnees: envelope.donnees }) !== shot.source) {
              throw new Error('La copie reçue ne correspond pas aux données locales.');
            }
          }
        }
        if (!row) {
          let attempt = read('nt_backup_attempt');
          if (!attempt || attempt.content_hash !== hash || attempt.owner_id !== user.id) {
            attempt = { id: uuid(c), content_hash: hash, owner_id: user.id, exporte_le: now().toISOString() };
            write('nt_backup_attempt', attempt);
          }
          const payload = JSON.stringify({ format: 'natura-tif-sauvegarde', version: 1, exporte_le: attempt.exporte_le,
            mode, resume: shot.resume, donnees: shot.donnees });
          if (new TextEncoder().encode(payload).byteLength > MAX_BYTES) throw new Error('Copie trop volumineuse. Utilisez l’export sur fichier.');
          try {
            await request(sb.from(TABLE).insert({ id: attempt.id, device_id: device, mode, content_hash: hash, payload }));
          } catch (error) {
            // A previous upload may have succeeded before the connection dropped.
            if (error.code !== '23505') throw error;
          }
          row = await request(sb.from(TABLE).select('*').eq('id', attempt.id).eq('mode', mode).single());
          if (!row || row.payload !== payload || row.payload_sha256 !== await digest(c, payload)) {
            throw new Error('La copie reçue est incomplète ou différente.');
          }
        }
        if (row.owner_id !== user.id || row.device_id !== device || row.mode !== mode || row.content_hash !== hash) {
          throw new Error('La copie reçue ne correspond pas à cette tablette.');
        }
        const verified = { id: row.id, owner_id: user.id, device_id: device, content_hash: hash,
          created_at: row.created_at, verified_at: now().toISOString(), transactions: shot.resume.transactions, total_euros: shot.resume.total_euros };
        write('nt_backup_last', verified);
        storage.removeItem(prefix + 'nt_backup_attempt');
        dirty = capture(storage, prefix, mode).source !== shot.source;
        lastError = null;
        return state(dirty ? 'pending' : 'ok', dirty ? 'Copie vérifiée ; de nouveaux changements restent à sauvegarder.' : 'Sauvegarde complète vérifiée.');
      } catch (error) {
        dirty = true;lastError = error;
        return state('error', 'Sauvegarde à refaire. Gardez l’application ouverte et réessayez.', { detail: String(error.message || error) });
      } finally { busy = false; }
    }
    async function history() {
      const { sb } = await session();
      const device = read('nt_backup_device');
      if (!device) return [];
      return request(sb.from(TABLE).select('id,created_at,transaction_count,total_euros,mode').eq('mode', mode).eq('device_id', device)
        .order('created_at', { ascending: false }).limit(20));
    }
    async function download(id) {
      const { sb } = await session();
      const row = await request(sb.from(TABLE).select('*').eq('id', id).eq('mode', mode).eq('device_id', read('nt_backup_device')).single());
      if (!row || await digest(c, row.payload) !== row.payload_sha256) throw new Error('Copie non vérifiée.');
      return row.payload;
    }
    function start() {
      state('pending', 'Vérification de la sauvegarde complète…');
      run(true);
      const interval = setInterval(() => run(), 60000);
      return () => { clearInterval(interval);if (timer) clearTimeout(timer); };
    }
    return { run, changed, history, download, start };
  }
  root.NaturaBackup = { capture, createManager, digest, KEYS, TABLE };
  if (typeof module !== 'undefined') module.exports = root.NaturaBackup;
})(typeof window !== 'undefined' ? window : globalThis);
