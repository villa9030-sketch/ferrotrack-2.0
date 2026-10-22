"""Aggiungere a un ordine GIA' caricato gli assiemi (o i disegni) che mancano.

Il caso vero (DECA A 001252, 22/10/2026): l'ordine e' stato caricato col
pacchetto senza le 7 sottocartelle degli assiemi. Gli assiemi sono rimasti
righe senza disegno e i loro 45 componenti non sono mai arrivati al laser ne'
a Lantek, mentre 27 codici erano gia' stati mandati a Lantek.

Due passi, come il pacchetto d'ordine (controllo umano):
  1. analizza(): il pacchetto (cartella o ZIP, con le sottocartelle) passa
     per la STESSA strada dell'import (rfq_importer.process_rfq_package +
     app._crea_preventivo_da_pacchetto: analisi dei DXF fuori dal server,
     pulito per Lantek, verifica dei pezzi, distinte degli assiemi), ma solo
     per gli assiemi che l'ordine non ha ancora. I pezzi finiscono in un
     preventivo tecnico nascosto (BOZZA) e si mostra il piano: cosa si
     aggiunge, cosa e' gia' nell'ordine (non si duplica), cosa resta bloccato
     (pezzi per assieme da confermare).
  2. applica(): i pezzi passano all'ordine (al preventivo tecnico dietro
     l'ordine, con le righe gia' esistenti intatte), i disegni nella cartella
     dell'ordine (LANTEK/<lamiera>), le righe-assieme senza disegno diventano
     assiemi veri con la quantita' dell'ordine. Il preventivo d'appoggio si
     cancella.

Mai doppioni: un codice gia' nell'ordine (anche gia' mandato a Lantek) non
si aggiunge una seconda volta; il piano lo elenca ("gia' nell'ordine").
"""
from __future__ import annotations

import importlib
import json
import logging
import os
import re
import threading

logger = logging.getLogger(__name__)

PIANO_FILE = '_aggiunta_ordine.json'
_LOCK = threading.Lock()


class Errore(Exception):
    def __init__(self, codice, messaggio, stato=400):
        super().__init__(messaggio)
        self.codice, self.messaggio, self.stato = codice, messaggio, stato


def _app():
    return importlib.import_module(__package__ + '.app')


def chiave(c) -> str:
    """'25NDSPA1979-00.DXF' / '25NDSPA1979' -> '25NDSPA1979' (senza
    estensione del disegno, copia "(2)" e revisione finale)."""
    b = os.path.basename(str(c or '')).strip().upper()
    b = re.sub(r'\.(DXF|DWG|PDF|STEP|STP)$', '', b)
    b = re.sub(r'\s*\(\d+\)$', '', b)
    return re.sub(r'[-_]\d{2}$', '', b)


def zip_da_cartella(cartella: str) -> bytes:
    """Una cartella del disco come lo ZIP di una cartella trascinata (la
    cartella stessa in cima: le sottocartelle sono gli assiemi)."""
    import io
    import zipfile
    cartella = os.path.abspath(cartella)
    radice = os.path.basename(cartella.rstrip('\\/')) or 'pacchetto'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for root, _dirs, files in os.walk(cartella):
            for f in sorted(files):
                if f.lower() in ('thumbs.db', 'desktop.ini') or f.startswith('~$'):
                    continue
                p = os.path.join(root, f)
                z.write(p, radice + '/' + os.path.relpath(p, cartella).replace(os.sep, '/'))
    return buf.getvalue()


def _righe_ordine(pid):
    """Le righe del preventivo dietro l'ordine, nel loro ordine (extra 'riga')."""
    from sqlalchemy import text as _t
    from .database import get_session
    s = get_session()
    try:
        out = []
        for r in s.execute(_t('SELECT id, codice, quantita, codice_assieme, dxf_filename, extra_campi '
                              'FROM preventivo_articoli WHERE preventivo_id = :p'), {'p': pid}).fetchall():
            try:
                ex = json.loads(r[5] or '{}') or {}
            except (TypeError, ValueError):
                ex = {}
            out.append({'id': r[0], 'codice': r[1], 'quantita': r[2], 'codice_assieme': r[3],
                        'dxf_filename': r[4], 'extra': ex})
        out.sort(key=lambda x: (x['extra'].get('riga') if isinstance(x['extra'].get('riga'), int) else 10 ** 6,
                                x['codice'] or ''))
        assiemi = {row[0]: row[1] for row in s.execute(
            _t('SELECT codice_assieme, qty FROM preventivo_assiemi WHERE preventivo_id = :p'), {'p': pid}).fetchall()}
        return out, assiemi
    finally:
        s.close()


def _stato_ordine(order):
    """(righe esistenti, assiemi esistenti, chiavi gia' mandate a Lantek)."""
    from . import lantek_invii
    pid = getattr(order, 'preventivo_id_origine', None)
    if not pid:
        raise Errore('senza_distinta', "Quest'ordine non ha la distinta dei pezzi (non e' nato da un "
                                       "pacchetto o da un preventivo): niente a cui aggiungere i disegni.")
    righe, assiemi = _righe_ordine(pid)
    mandati = {chiave(c) for c in lantek_invii.mandati(order.id)}
    return pid, righe, assiemi, mandati


def _e_riga_assieme(r, chiavi_ass) -> bool:
    """Una riga dell'ordine che e' in realta' un assieme rimasto senza
    cartella: codice dell'assieme, nessun disegno, non dentro un assieme."""
    return (chiave(r['codice']) in chiavi_ass and not r.get('dxf_filename')
            and not r.get('codice_assieme'))


def analizza(order_id: str, zip_bytes: bytes, nome_file: str = '', utente_id=None) -> dict:
    """Passo 1: legge il pacchetto e prepara i pezzi da aggiungere (nel
    preventivo tecnico d'appoggio). Ritorna il piano. Non tocca l'ordine."""
    from .preventivi import rfq_importer
    A = _app()
    order = A._ordine_esistente(order_id)
    if not order:
        raise Errore('ordine_non_trovato', 'Ordine non trovato.', 404)
    pid, righe, assiemi_es, mandati = _stato_ordine(order)

    result = rfq_importer.process_rfq_package(zip_bytes, tollerante=True)
    if not result.success:
        raise Errore('pacchetto_illeggibile', result.error or 'Non riesco a leggere il pacchetto.')

    # assiemi che l'ordine ha gia' (record assieme o componenti)
    gia_ass = {chiave(c) for c in assiemi_es} | {chiave(r['codice_assieme']) for r in righe if r.get('codice_assieme')}
    nuovi_ass = [c for c in (result.assiemi or []) if chiave(c) not in gia_ass]
    chiavi_nuovi_ass = {chiave(c) for c in nuovi_ass}
    righe_ass = {chiave(r['codice']): r for r in righe if _e_riga_assieme(r, chiavi_nuovi_ass)}
    esistenti = {}
    for r in righe:
        if r['id'] not in {x['id'] for x in righe_ass.values()}:
            esistenti.setdefault(chiave(r['codice']), r)
            if r.get('dxf_filename'):
                esistenti.setdefault(chiave(r['dxf_filename']), r)

    tenuti, uniti, fuori = [], [], []
    gia_presenti = 0
    for a in result.articoli:
        k = chiave(a.codice)
        if a.codice_assieme in nuovi_ass:
            if getattr(a, 'e_master', False) or k == chiave(a.codice_assieme):
                tenuti.append(a)          # disegno d'insieme: la pipeline lo salta
                continue
        elif a.codice_assieme or not a.matched_dxf:
            continue                       # assieme gia' nell'ordine, o riga senza disegno
        elif getattr(a, 'qta_fonte', '') == 'non_trovato':
            if k not in esistenti:
                fuori.append(a.codice)     # disegno che l'ordine non chiede: non si aggiunge
            continue
        if k in esistenti or (a.matched_dxf and chiave(a.matched_dxf) in esistenti):
            e = esistenti.get(k) or esistenti.get(chiave(a.matched_dxf))
            if not a.codice_assieme:
                gia_presenti += 1      # pezzo sciolto gia' caricato: e' quello
                continue
            uniti.append({'codice': a.codice, 'assieme': a.codice_assieme, 'gia_come': e['codice'],
                          'gia_in_assieme': e.get('codice_assieme'), 'quantita_ordine': e.get('quantita'),
                          'mandato_lantek': k in mandati})
            continue
        tenuti.append(a)
    if not nuovi_ass and not any(not a.codice_assieme for a in tenuti):
        raise Errore('niente_da_aggiungere',
                     "Nel pacchetto non c'e' niente che manchi all'ordine: gli assiemi e i disegni "
                     "ci sono gia'.", 409)

    result.articoli = tenuti
    result.assiemi = nuovi_ass
    result.dxf_no_match = []
    # quanti assiemi: la riga dell'ordine gia' caricata (quella vera del cliente)
    qa = dict(getattr(result, 'qta_assiemi', None) or {})
    for c in nuovi_ass:
        r = righe_ass.get(chiave(c))
        if r and int(r.get('quantita') or 0) > 0:
            qa[c] = int(r['quantita'])
    result.qta_assiemi = qa

    esito = A._crea_preventivo_da_pacchetto(result, zip_bytes, nome_file, creato_da=utente_id or None,
                                            solo_tecnico=True)
    if not esito:
        raise Errore('errore_interno', 'Non riesco a preparare i pezzi da aggiungere.', 500)
    tid = esito['preventivo_id']
    dati = {'order_id': order_id, 'preventivo_ordine': pid, 'uniti': uniti, 'fuori_ordine': fuori,
            'gia_presenti': gia_presenti,
            'righe_assieme': {c: righe_ass[chiave(c)]['id'] for c in nuovi_ass if chiave(c) in righe_ass},
            'avvisi': list(result.warnings or [])}
    with open(os.path.join(A._cartella_preventivo(tid), PIANO_FILE), 'w', encoding='utf-8') as fp:
        json.dump(dati, fp, ensure_ascii=False)
    return piano(order_id, tid)


def _leggi_dati(tid):
    A = _app()
    try:
        with open(os.path.join(A._cartella_preventivo(tid), PIANO_FILE), encoding='utf-8') as fp:
            return json.load(fp)
    except (OSError, ValueError):
        return None


def piano(order_id: str, tid: str) -> dict:
    """Il piano da mostrare: assiemi con i loro pezzi (per assieme, totale,
    materiale, spessore, stato), pezzi gia' nell'ordine, bloccati."""
    from .database import PreventivoManager
    tec = PreventivoManager.get_tecnico(tid)
    dati = _leggi_dati(tid)
    if not tec or not dati or dati.get('order_id') != order_id:
        raise Errore('aggiunta_non_trovata', "Analisi non trovata: ricarica i disegni.", 404)
    qta_ass = {a['codice_assieme']: int(a.get('qty') or 1) for a in tec.get('assiemi') or []}
    per_ass = {c: [] for c in qta_ass}
    sciolti, bloccati = [], []
    for a in tec.get('articoli') or []:
        od = a.get('ordine') if isinstance(a.get('ordine'), dict) else {}
        da_conf = od.get('stato') == 'qta_da_confermare'
        ca = a.get('codice_assieme')
        x = {'codice': a.get('codice'), 'per_assieme': int(a.get('quantita') or 0),
             'totale': int(a.get('quantita') or 0) * (qta_ass.get(ca) or 1) if ca else int(a.get('quantita') or 0),
             'materiale': a.get('materiale'), 'spessore_mm': a.get('spessore_mm'),
             'disegno': a.get('dxf_filename'), 'da_confermare': da_conf,
             'verifica': ((a.get('esito_verifica') or {}).get('stato')
                          if isinstance(a.get('esito_verifica'), dict) else None)}
        if da_conf:
            bloccati.append(a.get('codice'))
        if ca:
            per_ass.setdefault(ca, []).append(x)
        else:
            sciolti.append(x)
    return {'tecnico_id': tid, 'order_id': order_id,
            'assiemi': [{'codice': c, 'quantita': qta_ass.get(c) or 1, 'da_riga': c in (dati.get('righe_assieme') or {}),
                         'pezzi': per_ass.get(c) or []} for c in qta_ass],
            'sciolti': sciolti, 'uniti': dati.get('uniti') or [], 'bloccati': bloccati,
            'fuori_ordine': dati.get('fuori_ordine') or [], 'avvisi': dati.get('avvisi') or [],
            'gia_presenti': dati.get('gia_presenti') or 0,
            'n_nuovi': sum(len(v) for v in per_ass.values()) + len(sciolti)}


def scarta(order_id: str, tid: str) -> bool:
    from .database import PreventivoManager
    dati = _leggi_dati(tid)
    if not dati or dati.get('order_id') != order_id:
        return False
    ok = PreventivoManager.elimina_tecnico(tid)
    if ok:
        _app()._cleanup_preventivo_files(tid)
    return ok


def applica(order_id: str, tid: str, chi: str = '', esporta: bool = True) -> dict:
    """Passo 2: i pezzi preparati entrano nell'ordine. Ritorna il resoconto."""
    with _LOCK:
        return _applica(order_id, tid, chi, esporta)


def _applica(order_id, tid, chi, esporta):
    from sqlalchemy import text as _t
    from .database import PreventivoManager, get_session
    A = _app()
    order = A._ordine_esistente(order_id)
    if not order:
        raise Errore('ordine_non_trovato', 'Ordine non trovato.', 404)
    tec = PreventivoManager.get_tecnico(tid)
    dati = _leggi_dati(tid)
    if not tec or not dati or dati.get('order_id') != order_id or tec.get('status') != 'BOZZA':
        raise Errore('aggiunta_non_trovata', "Analisi non trovata o gia' usata: ricarica i disegni.", 404)
    pid, righe, assiemi_es, mandati = _stato_ordine(order)
    if pid != dati.get('preventivo_ordine'):
        raise Errore('cambiato', "L'ordine e' cambiato: ricarica i disegni.", 409)

    # di nuovo, adesso: niente doppioni con quello che l'ordine ha gia'
    nuovi_ass = {a['codice_assieme'] for a in tec.get('assiemi') or []}
    if {chiave(c) for c in nuovi_ass} & ({chiave(c) for c in assiemi_es}
                                          | {chiave(r['codice_assieme']) for r in righe if r.get('codice_assieme')}):
        raise Errore('cambiato', "Un assieme e' gia' stato aggiunto all'ordine: ricarica i disegni.", 409)
    id_righe_ass = {c: i for c, i in (dati.get('righe_assieme') or {}).items()}
    per_id = {r['id']: r for r in righe}
    for c, i in list(id_righe_ass.items()):
        r = per_id.get(i)
        if not r or not _e_riga_assieme(r, {chiave(c)}):
            raise Errore('cambiato', f"La riga dell'assieme {c} e' cambiata: ricarica i disegni.", 409)
    esistenti = set()
    for r in righe:
        if r['id'] in id_righe_ass.values():
            continue
        esistenti.add(chiave(r['codice']))
        if r.get('dxf_filename'):
            esistenti.add(chiave(r['dxf_filename']))
    articoli = tec.get('articoli') or []
    doppi = [a for a in articoli if chiave(a.get('codice')) in esistenti
             or (a.get('dxf_filename') and chiave(a['dxf_filename']) in esistenti)]
    if doppi:
        raise Errore('cambiato', 'Pezzi gia\' nell\'ordine: ' + ', '.join(a['codice'] for a in doppi[:5])
                     + '. Ricarica i disegni.', 409)
    if not articoli and not nuovi_ass:
        raise Errore('niente_da_aggiungere', 'Niente da aggiungere.', 409)

    # 1. disegni nella cartella dell'ordine (originale + LANTEK/<lamiera>),
    #    con le stesse funzioni della conferma del pacchetto
    dxf_stats = A._copy_cleaned_dxf_to_drawings(tid, order_id)

    # 2. i pezzi passano all'ordine, nell'ordine del PDF: i componenti al
    #    posto della riga del loro assieme
    s = get_session()
    try:
        nuovi = sorted(
            s.execute(_t('SELECT id, codice_assieme, extra_campi FROM preventivo_articoli '
                         'WHERE preventivo_id = :t'), {'t': tid}).fetchall(),
            key=lambda r: (json.loads(r[2] or '{}') or {}).get('riga', 10 ** 6))
        per_ass = {}
        for r in nuovi:
            per_ass.setdefault(r[1], []).append(r[0])
        sequenza = []
        messi = set()
        for r in righe:
            ass = next((c for c, i in id_righe_ass.items() if i == r['id']), None)
            if ass is not None:
                for nid in per_ass.get(ass, []):
                    sequenza.append(nid)
                    messi.add(nid)
            else:
                sequenza.append(r['id'])
        for r in nuovi:
            if r[0] not in messi:
                sequenza.append(r[0])
        qta_ass = {}
        for c, i in id_righe_ass.items():
            qta_ass[c] = max(1, int(per_id[i].get('quantita') or 1))
        if id_righe_ass:
            s.execute(_t('DELETE FROM preventivo_articoli WHERE preventivo_id = :p AND id IN ('
                         + ','.join(f':i{n}' for n in range(len(id_righe_ass))) + ')'),
                      {'p': pid, **{f'i{n}': i for n, i in enumerate(id_righe_ass.values())}})
        s.execute(_t('UPDATE preventivo_articoli SET preventivo_id = :p WHERE preventivo_id = :t'),
                  {'p': pid, 't': tid})
        s.execute(_t('UPDATE preventivo_assiemi SET preventivo_id = :p WHERE preventivo_id = :t'),
                  {'p': pid, 't': tid})
        for c, q in qta_ass.items():
            s.execute(_t('UPDATE preventivo_assiemi SET qty = :q WHERE preventivo_id = :p AND codice_assieme = :c'),
                      {'q': q, 'p': pid, 'c': c})
        extra = {r[0]: r[1] for r in s.execute(
            _t('SELECT id, extra_campi FROM preventivo_articoli WHERE preventivo_id = :p'), {'p': pid}).fetchall()}
        for n, aid in enumerate(sequenza):
            if aid not in extra:
                continue
            try:
                ex = json.loads(extra[aid] or '{}') or {}
            except (TypeError, ValueError):
                ex = {}
            if ex.get('riga') != n:
                ex['riga'] = n
                s.execute(_t('UPDATE preventivo_articoli SET extra_campi = :j WHERE id = :i'),
                          {'j': json.dumps(ex, ensure_ascii=False), 'i': aid})
        # il preventivo d'appoggio, ora vuoto
        for tab in ('preventivo_tubolari', 'preventivo_piastre'):
            s.execute(_t(f'DELETE FROM {tab} WHERE preventivo_id = :t'), {'t': tid})
        s.execute(_t('DELETE FROM preventivi WHERE id = :t AND solo_tecnico = 1'), {'t': tid})
        s.commit()
    except Exception:
        s.rollback()
        logger.exception('aggiunta disegni: pezzi non passati all\'ordine %s', order_id)
        raise Errore('errore_interno', "Pezzi non aggiunti all'ordine (niente e' cambiato nella distinta). "
                                       "I disegni copiati nella cartella dell'ordine restano.", 500)
    finally:
        s.close()

    # 3. file d'appoggio: via solo se tutti i disegni sono arrivati
    cartella = os.path.join(A.UPLOAD_FOLDER, 'drawings', order_id)
    attesi = {os.path.basename(a['dxf_filename']) for a in articoli if a.get('dxf_filename')}
    mancanti = sorted(n for n in attesi if not os.path.isfile(os.path.join(cartella, n)))
    if not mancanti:
        A._cleanup_preventivo_files(tid)
    export = None
    if esporta:
        try:
            export = A._esporta_disegni_per_officina(order_id, order.cliente, order.numero_ordine)
        except Exception as e:
            export = {'error': str(e)}
    try:
        from . import assiemi_ordine
        assiemi_ordine.dopo_cambio(order)
    except Exception:
        logger.warning('dopo l\'aggiunta: ricalcolo non riuscito', exc_info=True)
    bloccati = sorted({a['codice'] for a in articoli
                       if isinstance(a.get('ordine'), dict) and a['ordine'].get('stato') == 'qta_da_confermare'})
    return {'success': True, 'aggiunti': len(articoli), 'assiemi': sorted(nuovi_ass),
            'righe_assieme_convertite': sorted(id_righe_ass), 'bloccati': bloccati,
            'uniti': dati.get('uniti') or [], 'lantek_pronti': dxf_stats.get('lantek_pronti'),
            'lantek_da_guardare': [x['codice'] for x in dxf_stats.get('lantek_da_guardare') or []],
            'disegni_mancanti': mancanti, 'export': export}


def descrivi(p: dict) -> str:
    """Il piano in righe di testo (strumento da riga di comando, audit)."""
    out = []
    for a in p.get('assiemi') or []:
        out.append(f"ASSIEME {a['codice']} x{a['quantita']}"
                   + (' (la riga dell\'ordine diventa l\'assieme)' if a.get('da_riga') else '')
                   + f" - {len(a['pezzi'])} pezzi")
        for x in a['pezzi']:
            out.append(f"   {x['codice']:<18} {x['per_assieme']:>3}/assieme  tot {x['totale']:>3}  "
                       f"{x['materiale'] or '?'} {x['spessore_mm'] if x['spessore_mm'] else '?'} mm"
                       + ('  [PEZZI PER ASSIEME DA CONFERMARE]' if x['da_confermare'] else '')
                       + (f"  verifica:{x['verifica']}" if x.get('verifica') else ''))
    for x in p.get('sciolti') or []:
        out.append(f"PEZZO {x['codice']} x{x['totale']} {x['materiale'] or '?'} {x['spessore_mm'] or '?'} mm")
    for u in p.get('uniti') or []:
        out.append(f"GIA' NELL'ORDINE (non aggiunto): {u['codice']} (assieme {u['assieme']}) = {u['gia_come']}"
                   + (' - gia\' mandato a Lantek' if u.get('mandato_lantek') else ''))
    if p.get('gia_presenti'):
        out.append(f"Pezzi sciolti del pacchetto gia' nell'ordine (lasciati com'erano): {p['gia_presenti']}")
    if p.get('fuori_ordine'):
        out.append('Disegni che l\'ordine non chiede (non aggiunti): ' + ', '.join(p['fuori_ordine']))
    out.append(f"Totale pezzi nuovi: {p.get('n_nuovi')}; bloccati (pezzi per assieme da confermare): "
               f"{len(p.get('bloccati') or [])}")
    return '\n'.join(out)
