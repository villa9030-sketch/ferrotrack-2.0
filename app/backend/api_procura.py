"""Da procurare (pagina laser): le rotte. La logica sta in procura.py.

  GET  /api/procura                     le voci (filtri stato, tipo, ordine), i
                                        tubi in barre, i conteggi
  GET  /api/procura/conteggio           quante voci non ancora arrivate (bottone)
  POST /api/procura/stato               segna ordinato / arrivato / da ordinare
  POST /api/procura/annulla             annulla l'ultimo cambio di stato
  POST /api/procura/richiesta-offerta   ZIP: PDF della richiesta + disegni
  POST /api/procura/<id>                corregge profilo, lunghezza, materiale, quantita'
                                        (pezzi per assieme), tipo, nota
  GET  /api/procura/profili             i profili del catalogo (scelta scrivendo)
  GET  /api/orders/<id>/procura         le voci di un ordine

Le voci le vedono e le cambiano il laser, l'Ufficio (amministrazione: e'
li' che si ordina, laser-procura.js anche in impiegata.html) e un
amministratore col PIN; non il tablet (lui vede solo "mancano N pezzi da
procurare"). Ogni cambio va nello storico, con chi l'ha fatto. Le voci
nuove arrivano anche come avviso alla campanella dell'Ufficio, uno per
ordine (procura.avvisa_ufficio).
Le funzioni di app.py si importano DENTRO le funzioni (app.py importa questo
file per registrarlo).
"""
from __future__ import annotations

import logging
from datetime import date, datetime

from flask import Blueprint, Response, jsonify, request

from .accesso import ADMIN, richiede

logger = logging.getLogger(__name__)

bp_procura = Blueprint('procura', __name__)


def _app():
    import importlib
    return importlib.import_module(__package__ + '.app')


def _invalida(order_ids) -> None:
    """Il riassunto della pagina laser (chip, taglia entro) si rilegge."""
    try:
        from . import api_laser
        for oid in set(order_ids):
            api_laser.invalida(oid)
    except Exception:            # noqa: BLE001
        pass


def _data_it(iso) -> str:
    try:
        return datetime.strptime(str(iso)[:10], '%Y-%m-%d').strftime('%d/%m/%Y')
    except (TypeError, ValueError):
        return ''


def _descr_voce(v: dict) -> str:
    cod = v.get('codice') or v.get('profilo') or v.get('descrizione') or v['id'][:8]
    mat = ' (materiale)' if v.get('parte') == 'materiale' else ''
    return f"{cod} x{v.get('quantita')}{mat}"


@bp_procura.route('/api/procura', methods=['GET'])
@richiede('laser', 'ufficio', ADMIN)
def api_procura_elenco():
    """{voci, gruppi (tubi per profilo + materiale, in barre), conteggi, ordini,
    tipi, config}. ?stato=da_ordinare (default) | ordinato | arrivato | aperti
    | tutti; ?tipo=tubo...; ?ordine=<id>; ?fresco=1 rilegge subito le righe
    tubolari delle distinte."""
    from . import procura as pc
    try:
        pc.sincronizza_tutti(forza=request.args.get('fresco') in ('1', 'true'))
        stato = request.args.get('stato') or 'da_ordinare'
        tipo = request.args.get('tipo') or None
        ordine = request.args.get('ordine') or None
        tutte = pc.elenco('tutti')
        # una voce c'e' se il suo stato O quello del suo materiale (laser
        # tubo: barre comprate a parte) e' nel filtro; tipo "tubo" = anche il
        # materiale dei laser tubo, che va nelle barre
        ok_st = pc.stato_corrisponde
        voci = [v for v in tutte if (ok_st(v['stato'], stato) or ok_st(pc.stato_materiale(v), stato))
                and (not tipo or v['tipo'] == tipo or (tipo == 'tubo' and v['tipo'] == 'laser_tubo'
                                                       and pc.compra_materiale(v)))
                and (not ordine or v['order_id'] == ordine)]
        dis = pc.disegni_disponibili(voci)
        for v in voci:
            v['n_disegni'] = dis.get(v['id'], 0)
        cfg = pc.config()
        # il materiale di un laser tubo conta come una voce in piu'
        stati_tutti = [v['stato'] for v in tutte] + [v['acquisto']['stato'] for v in tutte
                                                    if v['tipo'] == 'laser_tubo' and v.get('acquisto')]
        conteggi = {s: stati_tutti.count(s) for s in pc.STATI}
        conteggi['aperti'] = conteggi['da_ordinare'] + conteggi['ordinato']
        conteggi['scaduti'] = (sum(1 for v in tutte if v['scaduta'])
                               + sum(1 for v in tutte if v['tipo'] == 'laser_tubo' and (v.get('acquisto') or {}).get('scaduta')))
        ordini = {}
        for v in tutte:
            if v['stato'] != 'arrivato' or pc.stato_corrisponde(pc.stato_materiale(v), 'aperti') or v['order_id'] == ordine:
                ordini.setdefault(v['order_id'], v['ordine'])
        # barre: il materiale da comprare il cui stato e' nel filtro
        materiale = [v for v in voci if ok_st(pc.stato_materiale(v), stato)]
        return jsonify({'success': True, 'voci': voci, 'gruppi': pc.gruppi_tubi(materiale, cfg),
                        'conteggi': conteggi, 'ordini': sorted(ordini.values(), key=lambda o: o['etichetta'].lower()),
                        'tipi': pc.TIPI_BREVI, 'config': cfg, 'filtri': {'stato': stato, 'tipo': tipo, 'ordine': ordine}}), 200
    except Exception as e:
        logger.exception('elenco da procurare fallito')
        return jsonify({'success': False, 'error': str(e)[:200]}), 500


@bp_procura.route('/api/procura/conteggio', methods=['GET'])
@richiede('laser', 'ufficio', ADMIN)
def api_procura_conteggio():
    """{aperti: non ancora arrivate, da_ordinare, scaduti}."""
    from . import procura as pc
    try:
        pc.sincronizza_tutti()
        r = pc.riassunto_per_ordini()
        return jsonify({'success': True, 'aperti': sum(x['n'] for x in r.values()),
                        'da_ordinare': sum(x['n_da_ordinare'] for x in r.values()),
                        'scaduti': sum(x['n_scaduti'] for x in r.values())}), 200
    except Exception as e:
        logger.exception('conteggio da procurare fallito')
        return jsonify({'success': False, 'error': str(e)[:200]}), 500


@bp_procura.route('/api/orders/<order_id>/procura', methods=['GET'])
@richiede('laser', 'ufficio', ADMIN)
def api_ordine_procura(order_id):
    from . import procura as pc
    A = _app()
    try:
        if not A._ordine_esistente(order_id):
            return A._non_trovato_ordine()
        return jsonify({'success': True, 'voci': pc.elenco('tutti', ordine=order_id)}), 200
    except Exception as e:
        logger.exception('da procurare dell\'ordine fallito')
        return jsonify({'success': False, 'error': str(e)[:200]}), 500


def _ids(corpo, campo='ids') -> list:
    ids = corpo.get(campo)
    if not isinstance(ids, list):
        return []
    return [str(x) for x in ids if isinstance(x, (str, int)) and str(x).strip()][:500]


@bp_procura.route('/api/procura/stato', methods=['POST'])
@richiede('laser', 'ufficio', ADMIN)
def api_procura_stato():
    """Corpo: {ids: [...], ids_materiale: [...], stato: "ordinato" | "arrivato" |
    "da_ordinare", fornitore: "Rossi tubi" (con ordinato), data_prevista:
    "2026-10-20"}. ids_materiale: il materiale da comprare (barre), per un
    laser tubo a parte dal taglio (ids).
    Risposta: {success, n, prima: [foto per annullare]}"""
    from . import procura as pc
    A = _app()
    try:
        corpo = request.get_json(silent=True) or {}
        ids, stato = _ids(corpo), corpo.get('stato')
        ids_mat = _ids(corpo, 'ids_materiale')
        if not ids and not ids_mat:
            return jsonify({'success': False, 'error': 'Scegli almeno una voce'}), 400
        if stato not in pc.STATI:
            return jsonify({'success': False, 'error': 'Stato non valido (da_ordinare, ordinato, arrivato)'}), 400
        fornitore = str(corpo.get('fornitore') or '').strip()[:80]
        prevista = corpo.get('data_prevista') or None
        if stato == 'ordinato':
            if not fornitore:
                return jsonify({'success': False, 'error': 'Scrivi a chi l\'hai ordinato (fornitore)'}), 400
            if prevista:
                try:
                    d = datetime.strptime(str(prevista)[:10], '%Y-%m-%d').date()
                except ValueError:
                    return jsonify({'success': False, 'error': 'Data prevista non valida'}), 400
                if not date(2020, 1, 1) <= d <= date(2100, 1, 1):
                    return jsonify({'success': False, 'error': 'Data prevista fuori misura'}), 400
        chi = A._chi_nome()
        cambi = pc.cambia_stato(ids, stato, fornitore=fornitore or None, data_prevista=prevista, chi=chi,
                                ids_materiale=ids_mat)
        if not cambi:
            return jsonify({'success': False, 'codice': 'non_trovato', 'error': 'Voci non trovate'}), 404
        per_ordine = {}
        for prima, dopo in cambi:
            per_ordine.setdefault(dopo['order_id'], []).append((prima, dopo))
        testo = {'ordinato': 'ordinato', 'arrivato': 'arrivato', 'da_ordinare': 'di nuovo da ordinare'}[stato]
        for oid, cc in per_ordine.items():
            extra = ''
            if stato == 'ordinato':
                extra = f' a {fornitore}' + (f', previsto il {_data_it(prevista)}' if prevista else '')
            A._audit('PROCURA_STATO', 'orders', oid,
                     f"Da procurare {testo}{extra}: " + ', '.join(_descr_voce(d) for _p, d in cc)
                     + ' (prima: ' + ', '.join(sorted({p['stato'] for p, _d in cc})) + ')')
        _invalida(per_ordine)
        return jsonify({'success': True, 'n': len(cambi), 'prima': [p for p, _d in cambi]}), 200
    except Exception as e:
        logger.exception('stato da procurare non salvato')
        return jsonify({'success': False, 'error': str(e)[:200]}), 500


@bp_procura.route('/api/procura/annulla', methods=['POST'])
@richiede('laser', 'ufficio', ADMIN)
def api_procura_annulla():
    """Corpo: {prima: [...]} come l'ha data /api/procura/stato."""
    from . import procura as pc
    A = _app()
    try:
        corpo = request.get_json(silent=True) or {}
        foto = corpo.get('prima')
        if not isinstance(foto, list) or not foto:
            return jsonify({'success': False, 'error': 'Niente da annullare'}), 400
        fatti = pc.ripristina(foto[:500], A._chi_nome())
        if not fatti:
            return jsonify({'success': False, 'codice': 'non_trovato', 'error': 'Voci non trovate'}), 404
        per_ordine = {}
        for prima, dopo in fatti:
            per_ordine.setdefault(dopo['order_id'], []).append((prima, dopo))
        for oid, cc in per_ordine.items():
            A._audit('PROCURA_ANNULLA', 'orders', oid,
                     'Da procurare, annullato: ' + ', '.join(f"{_descr_voce(d)} {p['stato']} -> {d['stato']}" for p, d in cc))
        _invalida(per_ordine)
        return jsonify({'success': True, 'n': len(fatti)}), 200
    except Exception as e:
        logger.exception('annulla da procurare fallito')
        return jsonify({'success': False, 'error': str(e)[:200]}), 500


@bp_procura.route('/api/procura/<voce_id>', methods=['POST'])
@richiede('laser', 'ufficio', ADMIN)
def api_procura_modifica(voce_id):
    """Corregge una voce dopo che e' nata: {profilo?, lunghezza_mm?,
    materiale?, descrizione?, quantita?, per_assieme?, nota?, tipo?}, con lo
    storico. La quantita' di un pezzo dell'ordine segue la distinta: per un
    pezzo d'assieme si scrivono i pezzi PER ASSIEME (per_assieme, sul pezzo:
    quantita' = per assieme x assiemi, come per Lantek); le righe tubolari
    della distinta non si cambiano qui."""
    from . import procura as pc
    from . import assiemi_ordine as ao
    A = _app()
    try:
        corpo = request.get_json(silent=True) or {}
        campi = {k: corpo[k] for k in ('profilo', 'lunghezza_mm', 'quantita', 'nota', 'tipo', 'materiale',
                                        'descrizione', 'materiale_fornito') if k in corpo}
        if 'materiale_fornito' in campi and not isinstance(campi['materiale_fornito'], bool):
            return jsonify({'success': False, 'error': 'materiale_fornito: vero o falso'}), 400
        per = corpo.get('per_assieme')
        if not campi and per in (None, ''):
            return jsonify({'success': False, 'error': 'Niente da cambiare'}), 400
        if 'lunghezza_mm' in campi and campi['lunghezza_mm'] not in (None, ''):
            try:
                v = float(str(campi['lunghezza_mm']).replace(',', '.'))
            except (TypeError, ValueError):
                return jsonify({'success': False, 'error': 'Lunghezza non valida'}), 400
            if not 1 <= v <= 24000:
                return jsonify({'success': False, 'error': 'Lunghezza fuori misura (1-24000 mm)'}), 400
        if 'quantita' in campi:
            try:
                q = int(campi['quantita'])
            except (TypeError, ValueError):
                return jsonify({'success': False, 'error': 'Quantità non valida'}), 400
            if not 1 <= q <= 100000:
                return jsonify({'success': False, 'error': 'Quantità fuori misura'}), 400
        if 'tipo' in campi and campi['tipo'] not in pc.TIPI:
            return jsonify({'success': False, 'error': 'Tipo non valido'}), 400
        voce = next(iter(pc.elenco('tutti', ids=[voce_id])), None)
        if not voce:
            return jsonify({'success': False, 'codice': 'non_trovato', 'error': 'Voce non trovata'}), 404
        tipo_dopo = campi.get('tipo') or voce['tipo']
        if tipo_dopo in ('tubo', 'laser_tubo') and 'profilo' in campi and not str(campi['profilo'] or '').strip():
            return jsonify({'success': False, 'error': 'Il profilo del tubo non si può togliere'}), 400
        order = A._ordine_esistente(voce['order_id'])
        # la quantita' di un pezzo dell'ordine si scrive sul pezzo (distinta)
        if 'quantita' in campi and voce.get('origine') == 'tubolare':
            return jsonify({'success': False, 'codice': 'dalla_distinta',
                            'error': 'Quantità dalla distinta (riga tubolare): si cambia nel preventivo'}), 400
        if 'quantita' in campi and voce.get('assieme'):
            return jsonify({'success': False, 'codice': 'per_assieme',
                            'error': "Pezzo dell'assieme: scrivi i pezzi per assieme, la quantità si calcola"}), 400
        if 'quantita' in campi and voce.get('articolo_id') and order:
            per, campi = campi.pop('quantita'), campi    # pezzo sciolto: quantita' del pezzo
        pa = None
        if per not in (None, ''):
            if not (voce.get('articolo_id') and order):
                return jsonify({'success': False, 'error': "Questa voce non è un pezzo dell'ordine"}), 400
            pa = ao.imposta_per_assieme(order, voce['articolo_id'], per, A._chi_nome())
            if not pa.get('success'):
                return jsonify({'success': False, 'error': pa.get('error')}), 400
            from .api_laser import _audit_per_assieme
            _audit_per_assieme(A, voce['order_id'], pa)
            ao.dopo_cambio(order)
        d = voce
        if campi:
            r = pc.modifica(voce_id, campi, A._chi_nome())
            if not r:
                return jsonify({'success': False, 'codice': 'non_trovato', 'error': 'Voce non trovata'}), 404
            p, d = r['prima'], r['dopo']
            diff = ', '.join(f"{k}: {p.get(k)} -> {d.get(k)}" for k in campi if p.get(k) != d.get(k))
            A._audit('PROCURA_MODIFICA', 'orders', d['order_id'], f"Da procurare {_descr_voce(d)}: {diff or 'nessun cambio'}")
        d = next(iter(pc.elenco('tutti', ids=[voce_id])), d)
        _invalida([d['order_id']])
        return jsonify({'success': True, 'voce': d}), 200
    except Exception as e:
        logger.exception('voce da procurare non modificata')
        return jsonify({'success': False, 'error': str(e)[:200]}), 500


@bp_procura.route('/api/procura/profili', methods=['GET'])
@richiede('laser', 'ufficio', ADMIN)
def api_procura_profili():
    """I profili del catalogo, per scegliere scrivendo."""
    from . import procura as pc
    return jsonify({'success': True, 'profili': pc.nomi_profili()}), 200


@bp_procura.route('/api/procura/richiesta-offerta', methods=['POST'])
@richiede('laser', 'ufficio', ADMIN)
def api_procura_richiesta():
    """Corpo: {ids: [...]} -> ZIP "Richiesta offerta AAAA-MM-GG.zip": il PDF
    (titolo, data, tubi in barre, tabella dei pezzi) e in disegni/ i file dei
    pezzi scelti (DXF, PDF, STEP col loro codice). Mai il PDF dell'ordine del
    cliente. Nessun fornitore da scegliere: la richiesta va a chi si vuole."""
    from . import procura as pc
    from .database import ConfigManager, get_session
    from .models import Order
    A = _app()
    try:
        corpo = request.get_json(silent=True) or {}
        ids = _ids(corpo)
        ids_mat = _ids(corpo, 'ids_materiale')
        if not ids and not ids_mat:
            return jsonify({'success': False, 'error': 'Scegli almeno una voce'}), 400
        # pezzi (con i disegni): le voci scelte, e i tubi T scelti come
        # materiale (si tagliano in casa: la lista serve); il materiale di un
        # laser tubo va solo nelle barre (al laser tubo vanno i disegni)
        con_mat = pc.elenco('tutti', ids=ids + ids_mat)
        voci = [v for v in con_mat if v['id'] in ids or (v['id'] in ids_mat and v['tipo'] == 'tubo')]
        materiale = [v for v in con_mat if (v['id'] in ids_mat and pc.compra_materiale(v))
                     or (v['id'] in ids and v['tipo'] == 'tubo')]
        if not con_mat:
            return jsonify({'success': False, 'codice': 'non_trovato', 'error': 'Voci non trovate'}), 404
        s = get_session()
        try:
            ordini = {o.id: o for o in s.query(Order).filter(Order.id.in_({v['order_id'] for v in con_mat})).all()}
        finally:
            s.close()
        azienda = (ConfigManager.load_config() or {}).get('azienda') or {}
        quando = datetime.now()
        dati, nomi = pc.zip_richiesta(voci, pc.gruppi_tubi(materiale), ordini, azienda, quando)
        per_ordine = {}
        for v in con_mat:
            per_ordine.setdefault(v['order_id'], []).append(v)
        for oid, vv in per_ordine.items():
            A._audit('PROCURA_RICHIESTA', 'orders', oid,
                     "Richiesta d'offerta preparata: " + ', '.join(_descr_voce(v) for v in vv)
                     + f' ({len(nomi) - 1} disegni nello ZIP)')
        nome = quando.strftime('Richiesta offerta %Y-%m-%d.zip')
        r = Response(dati, mimetype='application/zip')
        r.headers['Content-Disposition'] = f'attachment; filename="{nome}"'
        r.headers['X-File-Zip'] = str(len(nomi))
        return r
    except Exception as e:
        logger.exception('richiesta d\'offerta non preparata')
        return jsonify({'success': False, 'error': str(e)[:200]}), 500
