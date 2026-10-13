"""Pagina laser (frontend/laser.html): cosa serve per lavorare un ordine.

Stefano (09/10/2026): la pagina del laser era "caotica" e "saltava da una
schermata all'altra". Qui sta la logica che la pagina nuova mostra, in un
posto solo, cosi' l'elenco degli ordini e l'ordine aperto dicono sempre la
stessa cosa:

  GET  /api/laser/ordini                      un riassunto per ogni ordine del laser
  GET  /api/orders/<id>/laser                 l'ordine aperto: pezzi, Lantek, blocchi
  (tutti e due col "taglia entro": backend/taglia_entro.py)
  GET  /api/orders/<id>/pezzi/<pz>/lettura    il foglio intero del disegno, col
                                              contorno del motore, e i suoi dubbi
  GET  /api/orders/<id>/pezzi/<pz>/foglio.svg lo stesso foglio come immagine
  POST /api/orders/<id>/pezzi/<pz>/giusto     "si', e' il pezzo giusto"
  POST /api/orders/<id>/pezzi/<pz>/decisione  va sviluppato / piu' pezzi / non da laser
                                              (con N: cosa procurare, backend/procura.py)
  GET  /api/orders/<id>/pezzi/<pz>/procura-proposta  cosa proporre col tasto N
  POST /api/orders/<id>/pezzi/<pz>/per-assieme  pezzi per assieme scritti a mano
  POST /api/orders/<id>/assiemi/<codice>/quantita  quanti assiemi (con lo storico)
                                              (backend/assiemi_ordine.py)

Lantek si legge e basta (lantek.py, sola lettura): niente qui manda niente a
Lantek, lo fa solo /lantek-invia dopo la conferma.
Le funzioni di app.py si importano DENTRO le funzioni (app.py importa questo
file per registrarlo).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta

from flask import Blueprint, Response, jsonify, request

from .accesso import richiede

logger = logging.getLogger(__name__)

bp_laser = Blueprint('laser_pagina', __name__)

# riassunto per ordine: {order_id: (istante, dict)}
_CACHE: dict = {}
_LOCK = threading.Lock()
DURATA_CACHE_S = 45

# disegno del foglio e lettura del motore, per (percorso, data del file)
_FOGLI: dict = {}
_FOGLI_LOCK = threading.Lock()
_FOGLI_MAX = 48


def _app():
    """Il modulo backend.app (il pacchetto 'backend' esporta col nome 'app'
    l'oggetto Flask: "from . import app" darebbe quello)."""
    import importlib
    return importlib.import_module(__package__ + '.app')


def invalida(order_id=None) -> None:
    """L'ordine e' cambiato (decisione, invio, smistamento...): si rilegge."""
    with _LOCK:
        if order_id is None:
            _CACHE.clear()
        else:
            _CACHE.pop(order_id, None)


# ---------------------------------------------------------------------------
# Stato dell'ordine
# ---------------------------------------------------------------------------
def stato_laser(o) -> str:
    """smistare | coda (da mettere in Lantek) | in_lantek | tagliato | escluso"""
    if getattr(o, 'taglio_completato', False):
        return 'tagliato'
    richiesto = getattr(o, 'taglio_richiesto', None)
    if richiesto is None:
        return 'smistare'
    if not richiesto:
        return 'escluso'
    return 'in_lantek' if getattr(o, 'importato_lantek_il', None) else 'coda'


def ordini_del_laser(session) -> list:
    """Gli ordini che la pagina laser mostra: vivi, che passano (o forse
    passano) dal laser, e quelli tagliati nelle ultime 24 ore (per rimediare
    a uno sbaglio)."""
    from .models import Order
    from .ordini_service import STATI_ARCHIVIO
    ordini = session.query(Order).filter(
        Order.is_deleted == False,  # noqa: E712
        (Order.status.is_(None)) | (~Order.status.in_(STATI_ARCHIVIO)),
    ).all()
    limite = datetime.utcnow() - timedelta(hours=24)
    out = []
    for o in ordini:
        st = stato_laser(o)
        if st == 'escluso':
            continue
        if st == 'tagliato' and not (o.data_taglio_completato and o.data_taglio_completato >= limite):
            continue
        out.append(o)
    return out


# Cosa fare per ogni motivo per cui un codice nuovo non si puo' creare
def _cosa_fare(motivo: str, codice_ft: str) -> str:
    m = (motivo or '').lower()
    if re.search(r'\s\(\d+\)$', str(codice_ft or '')):
        return "Correggi il codice nell'ordine (togli « (2)»), poi premi Ricontrolla."
    if 'manca il disegno' in m:
        return ('Importa il disegno dal MES (Importa → Files DXF) e premi Ricontrolla, '
                'oppure tienilo fuori da Lantek.')
    if 'da preparare' in m:
        return 'Riapri il disegno e scegli il contorno sul foglio (tasto S), oppure tienilo fuori da Lantek.'
    if 'revisioni' in m:
        return "Scrivi nell'ordine la revisione giusta del codice, poi premi Ricontrolla."
    if 'materiale' in m:
        return 'Scegli qui il materiale guardando il disegno: lo creo io in Lantek.'
    if 'spessore' in m:
        return 'Scrivi qui lo spessore guardando il disegno: lo creo io in Lantek.'
    if 'questo pc' in m:
        return 'Importalo dal MES (Importa → Files DXF), poi premi Ricontrolla.'
    return 'Importalo dal MES, poi premi Ricontrolla.'


_RX_ASSIEME = re.compile(r'SA\d{3,}', re.I)
_RX_DIFF = re.compile(r"^(spessore|materiale): l'ordine dice (.+?), in Lantek e' (.+)$")


def _differenza(avviso: str) -> dict:
    m = _RX_DIFF.match(avviso or '')
    if m:
        return {'tipo': m.group(1), 'ordine': m.group(2), 'lantek': m.group(3), 'testo': avviso}
    if 'revisione' in (avviso or ''):
        return {'tipo': 'revisione', 'ordine': None, 'lantek': None,
                'testo': avviso.replace("c'e'", "c'è").replace("piu'", 'più')}
    return {'tipo': 'altro', 'ordine': None, 'lantek': None, 'testo': avviso}


def analisi(order, completa: bool = False, con_lantek: bool = True) -> dict:
    """Tutto quello che la pagina laser deve sapere di un ordine.

    completa=False: il riassunto per l'elenco (conteggi, blocchi).
    completa=True: anche i pezzi uno per uno, le differenze con Lantek e cosa
    partirebbe con "Manda a Lantek"."""
    A = _app()
    from . import lantek as _lt
    stato = stato_laser(order)
    righe_dist, righe_pdf, esclusi = A._distinta_lantek_con_esclusi(order)
    # le righe tubolari della distinta finiscono da sole in "Da procurare";
    # le voci dei pezzi "non da laser" (esclusi) seguono le loro quantita'
    if righe_dist and not righe_pdf:
        from . import procura as _pc
        _pc.sincronizza_righe(order.id, righe_dist + [x['_riga'] for x in esclusi if x.get('_riga')])
    lam = [r for r in righe_dist if r.get('tipo') == 'lamiera' and r.get('codice')]
    tutte_lam = lam + [x['_riga'] for x in esclusi if x.get('_riga')]
    altri = [r for r in righe_dist if r.get('tipo') != 'lamiera']

    q, lt = None, {'disponibile': False, 'errore': None}
    nuovi_si, nuovi_no, procesos = [], [], False
    pdf = None
    # "taglia entro": quanto prima della consegna, dalle lavorazioni della
    # distinta intera (anche i pezzi tenuti fuori da Lantek). Qui solo cosa
    # c'e' da fare: i giorni si contano dopo, coi numeri del calendario.
    from . import taglia_entro as _te
    dopo = _te.lavorazioni_ordine([] if righe_pdf else righe_dist + [x['_riga'] for x in esclusi if x.get('_riga')],
                                  'pdf' if righe_pdf else ('distinta' if (righe_dist or esclusi) else 'nessuna'),
                                  getattr(order, 'note', '') or '')
    if con_lantek and (lam or righe_pdf):
        q = A._quantita_lantek(order, righe_dist)
        lt = q['lantek']
        if lt.get('disponibile'):
            procesos = _lt.procesos_disponibile()
            if procesos:
                nuovi_si, nuovi_no = A._nuovi_per_lantek(order, q, righe_dist)
        if righe_pdf:
            from . import ordine_pdf_lantek as _opl
            codici_pdf = [r['codice'] for r in righe_pdf]
            pdf = _opl.stato_righe(
                righe_pdf, _opl.abbinamenti(codici_pdf), _lt.pezzi_in_lantek(codici_pdf),
                _lt.pezzi_dell_ordine(q['commessa']) if lt.get('disponibile') else None)
    da_abbinare = {str(x['codice']).lower() for x in ((pdf or {}).get('da_abbinare') or [])}

    per_q = {str(r['codice_ft']).strip().lower(): r for r in (q or {}).get('righe') or []}
    no_per = {str(x['codice']).strip().lower(): x for x in nuovi_no}
    si_cod = {str(x['codice']).strip().lower() for x in nuovi_si}

    pezzi, bloccati, visti = [], [], set()
    n_controllare = 0
    for r in tutte_lam:
        cod = str(r['codice']).strip()
        x = per_q.get(cod.lower())
        mt = r.get('motore') or {'stato': 'sicuro', 'motivi': [], 'decisione': None}
        dec = mt.get('decisione')
        escluso = dec in A._ESCLUSE_LASER
        if escluso:
            lt_stato = None
        elif x:
            lt_stato = x['stato']                     # in_lantek | nuovo | sconosciuto
        else:
            lt_stato = 'sconosciuto'
        cod_lt = (x or {}).get('codice') or cod
        no = no_per.get(cod_lt.lower())
        # il disegno serve solo per creare un codice nuovo in Lantek, e solo
        # finche' l'ordine non e' in Lantek (dopo, controllarlo non serve piu')
        serve_disegno = (lt_stato in ('nuovo', 'sconosciuto') and cod.lower() not in da_abbinare
                         and stato in ('smistare', 'coda'))
        perche = list(mt.get('motivi') or [])
        da_controllare = bool(serve_disegno and mt.get('stato') == 'verificare' and r.get('disegno'))
        da_preparare = bool(no and 'da preparare' in no.get('motivo', ''))
        if serve_disegno and da_preparare and mt.get('stato') not in ('deciso',) and r.get('disegno'):
            da_controllare = True
            if not perche:
                perche = ['Manca il disegno pulito per Lantek: conferma il contorno']
        if da_controllare and not perche:
            perche = ['Il motore non è sicuro della lettura']
        # codice d'assieme senza disegno (DECA: ...SA0233...): non va al laser,
        # i suoi pezzi sono le altre righe. Non blocca l'ordine.
        assieme = bool(lt_stato == 'nuovo' and not r.get('disegno') and not r.get('foglio')
                       and _RX_ASSIEME.search(cod))
        # pezzo di un foglio con piu' pezzi: i dati (spessore, materiale) si
        # scrivono sul foglio, e il disegno da guardare e' il foglio
        aid_dati = r.get('articolo_id') or (r.get('foglio') or {}).get('articolo_id')
        blocco = None
        if r.get('qta_da_confermare') and not escluso and stato in ('smistare', 'coda'):
            # componente d'assieme senza riga nell'ordine: quanti per assieme
            # non lo dice nessuno, non si manda a Lantek un numero inventato
            blocco = {'motivo': 'pezzi per assieme da confermare',
                      'cosa': f"L'ordine non dice quanti {cod} servono per ogni assieme "
                              f"{r.get('assieme') or ''}: scrivilo qui (Invio salva).",
                      'campo': 'pz_assieme', 'articolo_id': r.get('articolo_id'),
                      'assieme': r.get('assieme'), 'disegno': bool(r.get('disegno')),
                      'qta_assieme': (int(r.get('quantita') or 0) // max(1, int(r.get('qta_per') or 1)))
                      if r.get('qta_per') else None}
        elif lt_stato == 'nuovo' and not escluso and not assieme and cod.lower() not in da_abbinare                 and not da_controllare and stato in ('smistare', 'coda'):
            if not procesos and lt.get('disponibile'):
                blocco = {'motivo': 'su questo PC non posso creare pezzi nuovi in Lantek',
                          'cosa': _cosa_fare('questo pc', cod)}
            elif no and not no.get('verifica'):
                blocco = {'motivo': no['motivo'], 'cosa': _cosa_fare(no['motivo'], cod)}
                mot = str(no['motivo']).lower()
                campo = 'spessore' if 'spessore' in mot else 'materiale' if 'materiale' in mot else None
                if campo and aid_dati:
                    # lo decide il laser qui, guardando il disegno (non nel MES)
                    blocco.update({'campo': campo, 'articolo_id': aid_dati,
                                   'disegno': bool(r.get('disegno') or (r.get('foglio') or {}).get('disegno')),
                                   'materiale': (x or {}).get('materiale') or r.get('materiale'),
                                   'spessore': (x or {}).get('spessore') or r.get('spessore_mm')})
                    if campo == 'spessore' and completa and blocco['disegno']:
                        # la lettura migliore del motore, gia' scelta: il
                        # laser guarda e conferma (Invio)
                        try:
                            art_sp, perc_sp = A._articolo_ordine(order, aid_dati)
                            prop = proposta_spessore(perc_sp, art_sp)
                        except Exception:
                            logger.warning('spessore proposto non letto per %s', cod, exc_info=True)
                            prop = None
                        if prop:
                            blocco['proposta'] = prop
            elif no and no.get('verifica') and mt.get('stato') == 'deciso':
                blocco = {'motivo': no['motivo'], 'cosa': 'Riapri il disegno e scegli il contorno (tasto S).'}
        chiave = cod.lower()
        nuovo_codice = chiave not in visti
        visti.add(chiave)
        if da_controllare and nuovo_codice:
            n_controllare += 1
        if blocco and nuovo_codice:
            bloccati.append({'codice': cod, **blocco})
        if completa:
            avvisi = [a for a in ((x or {}).get('avvisi') or [])
                      if not re.match(r"^non ancora in Lantek|.*scegli quella giusta", a)]
            pezzi.append({
                'codice': cod, 'codice_lantek': cod_lt if cod_lt != cod else None,
                'articolo_id': r.get('articolo_id'), 'quantita': r.get('quantita'),
                'materiale': (x or {}).get('materiale') or r.get('materiale'),
                'spessore': (x or {}).get('spessore') if (x or {}).get('spessore') is not None else r.get('spessore_mm'),
                'materiale_ordine': r.get('materiale'), 'spessore_ordine': r.get('spessore_mm'),
                'lavorazioni': [lv for lv in (r.get('lavorazioni') or []) if not re.match(r'^taglio( laser)?$', str(lv), re.I)],
                'assieme': r.get('assieme'), 'disegno': r.get('disegno'),
                'qta_per': r.get('qta_per'), 'qta_da_confermare': bool(r.get('qta_da_confermare')),
                'misure': [r.get('bbox_w_mm'), r.get('bbox_h_mm')] if r.get('bbox_w_mm') else None,
                'lantek': lt_stato, 'in_produzione': (x or {}).get('in_produzione') or 0,
                'gia_fatti': (x or {}).get('gia_fatti') or 0,
                'crea': cod_lt.lower() in si_cod,
                'motore': mt.get('stato'), 'decisione': dec, 'perche': perche[:3],
                'da_controllare': da_controllare, 'blocco': blocco,
                'escluso': A._ESCLUSE_LASER.get(dec) if escluso else None,
                'da_abbinare': cod.lower() in da_abbinare, 'assieme_senza_disegno': assieme,
                'avvisi': avvisi,
                'foglio': r.get('foglio'),
            })

    invio_codici = [r['codice'] for r in _lt.righe_per_xml(q['righe'])] if q else []
    # codici "da correggere in Lantek" (lantek_correzioni.py): finche' Lantek
    # non coincide non partono, e l'ordine non e' "tutto in Lantek"
    from . import lantek_correzioni as _lc
    in_prod = ({str(r['codice']).strip().upper() for r in q['righe'] if r.get('in_produzione')}
               if q and lt.get('disponibile') else None)
    try:
        corr = _lc.stato(order.id, in_prod)
    except Exception:
        logger.warning('correzioni Lantek non lette per %s', order.id, exc_info=True)
        corr = {'correzioni': [], 'n_correggere': 0, 'n_corretti': 0}
    # quantita' cambiate dopo l'invio (pezzi per assieme, assiemi): Lantek ha
    # ancora il numero vecchio, va corretto l'ordine di produzione
    try:
        commessa = (q or {}).get('commessa') or _lt.commessa_lantek(
            A._numero_per_cartelle(order) or getattr(order, 'numero_ordine', '') or '')
        inviate = _lc.quantita_inviate(commessa, os.path.join(A.UPLOAD_FOLDER, 'lantek_import'))
        lt_ord = _lt.pezzi_dell_ordine(commessa) if lt.get('disponibile') else None
        sospesi = {str(r.get('codice') or '').strip().upper() for r in lam if r.get('qta_da_confermare')}
        ora_q = [x for x in ((q or {}).get('righe') or _lt.righe_quantita(lam, None))
                 if str(x.get('codice_ft') or x.get('codice') or '').strip().upper() not in sospesi]
        cambiate = _lc.quantita_cambiate(order.id, ora_q, inviate, lt_ord) if (inviate or lt_ord) else []
    except Exception:
        logger.warning("quantita' mandate a Lantek non lette per %s", order.id, exc_info=True)
        cambiate = []
    if cambiate:
        corr = {**corr, 'correzioni': list(corr['correzioni']) + cambiate,
                'n_correggere': corr['n_correggere'] + len(cambiate)}
    fermi = {c['codice'].strip().upper() for c in corr['correzioni'] if c['stato'] == 'da_correggere'}
    da_abbinare_n = len((pdf or {}).get('da_abbinare') or [])
    n_codici = len({str(r['codice']).strip().lower() for r in tutte_lam})
    out = {
        'id': order.id, 'stato': stato,
        'origine': 'pdf' if righe_pdf else ('distinta' if tutte_lam else 'nessuna'),
        'n_codici': n_codici,
        'n_pezzi': sum(int(r.get('quantita') or 0) for r in tutte_lam),
        'n_controllare': n_controllare, 'n_abbinare': da_abbinare_n,
        'n_esclusi': len(esclusi),
        'bloccati': bloccati,
        'materiali_lantek': (sorted(_lt.materiali_lantek_noti() or [])
                             if any(b.get('campo') == 'materiale' for b in bloccati) else []),
        'lantek': {'letto': q is not None, 'disponibile': bool(lt.get('disponibile')),
                   'errore': lt.get('errore'), 'crea_pezzi': bool(procesos),
                   'invio_automatico': bool(q is not None and _lt.xmlimporter_disponibile())},
        'n_invio': len([c for c in invio_codici if str(c).strip().upper() not in fermi]) + len(nuovi_si),
        'n_in_produzione': sum(1 for r in (q or {}).get('righe') or [] if r.get('in_produzione')),
        'dopo_laser': dopo,
        'n_qta_da_confermare': len({str(r.get('codice')).lower() for r in lam if r.get('qta_da_confermare')}),
        'correzioni': corr['correzioni'],
        'n_correggere': corr['n_correggere'], 'n_corretti': corr['n_corretti'],
    }
    out['pronto'] = bool(out['lantek']['disponibile'] and not n_controllare and not bloccati
                         and not da_abbinare_n and out['n_invio'])
    # niente da mandare e niente in sospeso: i codici hanno gia' l'ordine di
    # produzione in Lantek (o sono tenuti fuori)
    out['tutto_in_lantek'] = bool(out['lantek']['disponibile'] and not out['n_invio'] and not n_controllare
                                  and not bloccati and not da_abbinare_n and n_codici
                                  and not corr['n_correggere'])
    if completa:
        righe_q = {r['codice']: r for r in (q or {}).get('righe') or []}
        from .assiemi_ordine import assiemi_ordine
        out.update({
            'pezzi': pezzi,
            # gli assiemi (riga dell'ordine, quanti) coi loro pezzi sotto
            'assiemi': assiemi_ordine(order, righe_dist + [x['_riga'] for x in esclusi if x.get('_riga')]),
            'altri': [{'codice': r.get('codice'), 'tipo': r.get('tipo'), 'quantita': r.get('quantita'),
                       'descrizione': r.get('descrizione')} for r in altri],
            'esclusi': [{k: v for k, v in e.items() if k != '_riga'} for e in esclusi],
            'commessa': (q or {}).get('commessa'), 'cliente_lantek': (q or {}).get('cliente_lantek'),
            'consegna_lantek': (q or {}).get('consegna'),
            'invio': {
                'codici': invio_codici,
                'nuovi': [x['codice'] for x in nuovi_si],
                'da_mandare': [{'codice': r['codice'], 'quantita': r['quantita'],
                                'materiale': r.get('materiale'), 'spessore': r.get('spessore'),
                                'gia_fatti': r.get('gia_fatti') or 0, 'gia_fatti_il': r.get('gia_fatti_il')}
                               for r in _lt.righe_per_xml((q or {}).get('righe') or [])],
                'nuovi_da_creare': [{k: x[k] for k in ('codice', 'materiale', 'spessore', 'quantita')} for x in nuovi_si],
            },
            'differenze': [{'codice': c, **_differenza(a)}
                           for c in invio_codici for a in (righe_q.get(c) or {}).get('avvisi') or []
                           if not a.startswith('non ancora in Lantek')],
            'pdf': pdf,
            # chi ha gia' scelto "va bene Lantek" per un codice da correggere
            'usa_lantek': [c for c in _lc.usa_lantek(order.id) if c in invio_codici],
        })
    return out


def _procura_ordine(order_id) -> dict:
    """Le voci "da procurare" non arrivate di un ordine (procura.riassunto_per_ordini)."""
    try:
        from . import procura as _pc
        return _pc.riassunto_per_ordini([order_id]).get(order_id) or {}
    except Exception:
        logger.warning('da procurare non letto per %s', order_id, exc_info=True)
        return {}


def con_taglia_entro(order, a: dict, cal=None, procura: dict | None = None) -> dict:
    """Il riassunto con il "taglia entro" dell'ordine. Si calcola ADESSO (non
    sta nella cache): la data scelta a mano e la consegna si vedono subito.
    procura: le voci "da procurare" non arrivate dell'ordine (None = si
    leggono qui): laser tubo e meccanica dal fornitore contano come
    lavorazione esterna (+5); servono anche al chip "Da procurare (N)"."""
    from . import taglia_entro as _te
    cal = cal if cal is not None else _app()._cal_config()
    flag = getattr(order, 'lavorazione_esterna', None)
    nota = getattr(order, 'lavorazione_esterna_nota', None) or ''
    pc = procura if procura is not None else _procura_ordine(order.id)
    # la spunta "Lavorazione esterna" vale subito (non sta nella cache)
    ant = (_te.anticipo_da(_te.con_procura(_te.con_esterna(a['dopo_laser'], flag, nota), pc.get('esterne')), cal)
           if a.get('dopo_laser') or flag is not None or pc.get('esterne') else None)
    te = _te.taglia_entro(getattr(order, 'data_consegna', None), ant, cal,
                          getattr(order, 'data_taglio_pianificata', None))
    return {**a, 'taglia_entro': te,
            'lavorazione_esterna': {'valore': flag, 'nota': nota},
            'procura': {'n': pc.get('n') or 0, 'n_da_ordinare': pc.get('n_da_ordinare') or 0,
                        'n_ordinati': pc.get('n_ordinati') or 0, 'n_scaduti': pc.get('n_scaduti') or 0}}


@bp_laser.route('/api/orders/<order_id>/lavorazione-esterna', methods=['POST'])
@richiede('laser', 'ufficio')
def api_lavorazione_esterna(order_id):
    """Dopo il laser l'ordine va fuori (zincatura, verniciatura...)? Lo dice
    una persona e il "taglia entro" se ne fida (backend/taglia_entro.py).

    Corpo: {"valore": true | false | null, "nota": "zincatura"} (null = non
    detto: torna la deduzione dalle note). Risposta: {success, valore, nota}"""
    try:
        corpo = request.get_json(silent=True) or {}
        if 'valore' not in corpo:
            return jsonify({'success': False, 'error': 'Manca il valore (sì, no o non detto)'}), 400
        valore = corpo.get('valore')
        if not (valore is None or isinstance(valore, bool)):
            return jsonify({'success': False, 'error': 'Valore non valido: true, false o null'}), 400
        nota = str(corpo.get('nota') or '').strip()[:60] if valore else ''
        r = salva_lavorazione_esterna(order_id, valore, nota)
        if not r.get('success'):
            return jsonify(r), 404 if r.get('codice') == 'non_trovato' else 400
        return jsonify(r), 200
    except Exception as e:
        logger.exception('lavorazione esterna non salvata')
        return jsonify({'success': False, 'error': str(e)[:200]}), 500


def salva_lavorazione_esterna(order_id, valore, nota='') -> dict:
    """Scrive la spunta "Lavorazione esterna" sull'ordine, con l'audit.
    valore True / False / None (non detto)."""
    A = _app()
    from .database import get_session
    from .models import Order
    session = get_session()
    try:
        o = session.query(Order).filter(Order.id == order_id).first()
        if not o or o.is_deleted:
            return {'success': False, 'codice': 'non_trovato', 'error': 'Ordine non trovato'}
        prima = (o.lavorazione_esterna, o.lavorazione_esterna_nota)
        o.lavorazione_esterna = valore
        o.lavorazione_esterna_nota = (nota or None) if valore else None
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    testo = lambda v, n: 'sì' + (f' ({n})' if n else '') if v else 'no' if v is False else 'non detto'  # noqa: E731
    A._audit('ORDINE_LAV_ESTERNA', 'orders', order_id,
             f'lavorazione esterna: {testo(*prima)} -> {testo(valore, nota)}')
    invalida(order_id)
    return {'success': True, 'valore': valore, 'nota': (nota or None) if valore else None}


def _analisi_in_cache(order, forza=False) -> dict:
    ora = time.time()
    with _LOCK:
        c = _CACHE.get(order.id)
    if c and not forza and ora - c[0] < DURATA_CACHE_S:
        return c[1]
    st = stato_laser(order)
    a = analisi(order, completa=False, con_lantek=st in ('smistare', 'coda'))
    with _LOCK:
        if len(_CACHE) > 400:
            _CACHE.clear()
        _CACHE[order.id] = (ora, a)
    return a


@bp_laser.route('/api/laser/ordini', methods=['GET'])
@richiede('laser', 'ufficio')
def api_laser_ordini():
    """Un riassunto per ogni ordine della pagina laser (vedi analisi()).
    ?fresco=1 rilegge Lantek; ?ordine=<id> solo quell'ordine, sempre fresco."""
    from .database import get_session
    from .models import Order
    try:
        fresco = request.args.get('fresco') in ('1', 'true', 'si')
        solo = request.args.get('ordine')
        if fresco:
            from . import lantek as _lt
            _lt._svuota_cache()
        session = get_session()
        try:
            if solo:
                o = session.query(Order).filter(Order.id == solo).first()
                ordini = [o] if o else []
            else:
                ordini = ordini_del_laser(session)
        finally:
            session.close()
        out = {}
        cal = _app()._cal_config()
        from . import procura as _pc
        try:
            _pc.sincronizza_tutti()
            proc = _pc.riassunto_per_ordini([o.id for o in ordini])
        except Exception:
            logger.warning('da procurare non letto', exc_info=True)
            proc = {}
        for o in ordini:
            try:
                out[o.id] = _analisi_in_cache(o, forza=fresco or bool(solo))
            except Exception as e:
                logger.exception('analisi laser di %s fallita', o.id)
                out[o.id] = {'id': o.id, 'stato': stato_laser(o), 'errore': str(e)[:200]}
            out[o.id] = con_taglia_entro(o, out[o.id], cal, proc.get(o.id) or {})
        return jsonify({'success': True, 'ordini': out}), 200
    except Exception as e:
        logger.exception('elenco laser fallito')
        return jsonify({'success': False, 'error': str(e)}), 500


@bp_laser.route('/api/orders/<order_id>/laser', methods=['GET'])
@richiede('laser', 'ufficio')
def api_ordine_laser(order_id):
    """L'ordine aperto nella pagina laser: pezzi, Lantek, blocchi, invio.
    ?fresco=1 rilegge Lantek adesso ("Ricontrolla")."""
    A = _app()
    try:
        order = A._ordine_esistente(order_id)
        if not order:
            return A._non_trovato_ordine()
        if request.args.get('fresco') in ('1', 'true', 'si'):
            from . import lantek as _lt
            _lt._svuota_cache()
        a = analisi(order, completa=True, con_lantek=True)
        # anche l'elenco vede subito lo stesso stato
        ridotto = {k: v for k, v in a.items() if k not in ('pezzi', 'altri', 'esclusi', 'invio', 'differenze',
                                                          'pdf', 'commessa', 'cliente_lantek', 'consegna_lantek',
                                                          'usa_lantek')}
        with _LOCK:
            _CACHE[order.id] = (time.time(), ridotto)
        from . import procura as _pc
        return jsonify({'success': True, **con_taglia_entro(order, a),
                        'procura_voci': _pc.elenco('tutti', ordine=order.id)}), 200
    except Exception as e:
        logger.exception('ordine laser fallito')
        return jsonify({'success': False, 'error': str(e)}), 500


@bp_laser.route('/api/orders/<order_id>/lantek-correzioni', methods=['POST'])
@richiede('laser', 'ufficio')
def api_lantek_correzione(order_id):
    """Un codice "da correggere in Lantek": chi l'ha deciso cambia idea e va
    bene il valore che c'e' in Lantek. La correzione si chiude e il codice
    si puo' mandare subito (sempre con la conferma di "Manda a Lantek").

    Corpo: {"codice": "11PCOPA0025-01", "azione": "usa_lantek"}"""
    A = _app()
    from . import lantek_correzioni as _lc
    try:
        if not A._ordine_esistente(order_id):
            return A._non_trovato_ordine()
        corpo = request.get_json(silent=True) or {}
        codice = str(corpo.get('codice') or '').strip()
        if corpo.get('azione') == 'quantita_corretta' and codice:
            # quantita' cambiata dopo l'invio: corretta a mano in Lantek
            try:
                qv = float(corpo.get('quantita'))
                ql = float(corpo['quantita_lantek']) if corpo.get('quantita_lantek') is not None else None
            except (TypeError, ValueError):
                return jsonify({'success': False, 'error': "Quantità non valida"}), 400
            rec = _lc.segna_quantita_corretta(order_id, codice, qv, ql, A._chi_nome())
            A._audit('LANTEK_QUANTITA_CORRETTA', 'orders', order_id,
                     f"{codice}: quantità corretta a mano in Lantek ({'%g' % ql if ql is not None else '?'} -> {qv:g})")
            invalida(order_id)
            return jsonify({'success': True, 'codice': rec['codice']}), 200
        if corpo.get('azione') != 'usa_lantek' or not codice:
            return jsonify({'success': False, 'error': 'Serve il codice e azione "usa_lantek"'}), 400
        chiuse = _lc.chiudi(order_id, [codice], 'usa_lantek', A._chi_nome())
        if not chiuse:
            return jsonify({'success': False, 'codice': 'non_trovato',
                            'error': f'{codice} non è fra i codici da correggere di quest\'ordine'}), 404
        A._audit('LANTEK_USA_LANTEK', 'orders', order_id,
                 f"{codice}: va bene com'è in Lantek (non più da correggere: era {_lc.descrivi(chiuse[0])})")
        invalida(order_id)
        return jsonify({'success': True, 'codice': chiuse[0]['codice']}), 200
    except Exception as e:
        logger.exception('correzione Lantek non salvata')
        return jsonify({'success': False, 'error': str(e)[:200]}), 500


# ---------------------------------------------------------------------------
# Il foglio del disegno, grande, col contorno del motore
# ---------------------------------------------------------------------------
def _cfg_riconoscimento() -> dict:
    from .database import ConfigManager
    return (ConfigManager.load_config() or {}).get('dxf_detection', {}) or {}


def _lettura_motore(percorso: str) -> dict:
    """detect_pezzo_geometry_v3 sul disegno (sola lettura), in cache."""
    try:
        mt = os.path.getmtime(percorso)
    except OSError:
        mt = 0
    chiave = ('det', percorso, mt)
    with _FOGLI_LOCK:
        if chiave in _FOGLI:
            return _FOGLI[chiave]
    from .preventivi.dxf_polygon_detector_v3 import detect_pezzo_geometry_v3
    try:
        r = detect_pezzo_geometry_v3(percorso, _cfg_riconoscimento()) or {}
    except Exception as e:
        logger.warning('lettura del motore fallita per %s: %s', percorso, e)
        r = {'warnings': [], 'candidates': []}
    with _FOGLI_LOCK:
        while len(_FOGLI) >= _FOGLI_MAX:
            _FOGLI.pop(next(iter(_FOGLI)))
        _FOGLI[chiave] = r
    return r


# Da dove viene lo spessore letto sul disegno, in parole del laser
_FONTI_SPESSORE = {
    'testo': 'scritto nel cartiglio', 'cella': 'cartiglio', 'cartiglio_tabellare': 'cartiglio',
    'cartiglio_descrizione': 'descrizione nel cartiglio', 'piatto': 'descrizione del piatto',
    'vista_laterale': 'vista di fianco', 'peso_area': 'peso del cartiglio / area', 'filename': 'nome del file',
}


def _fonte_breve(src) -> str:
    s = str(src or '').split('+')[0]
    return _FONTI_SPESSORE.get(s, s.replace('_', ' '))


def lettura_spessore(percorso: str, art: dict | None = None) -> dict:
    """La lettura dello spessore del motore sul disegno (dxf_scanner: testo
    del cartiglio, vista di fianco, peso/area, nome file...), come al
    caricamento dell'ordine: prima la cache dei disegni gia' letti
    (dxf_cache), altrimenti la si rifa' qui. In cache per (percorso, data).

    {spessore_mm, confidence, source, fonti, confermato_da} (spessore_mm None
    se il disegno non lo dice)."""
    try:
        mt = os.path.getmtime(percorso)
    except OSError:
        mt = 0
    chiave = ('sp', percorso, mt)
    with _FOGLI_LOCK:
        if chiave in _FOGLI:
            return _FOGLI[chiave]
    from .preventivi import dxf_cache, dxf_scanner
    cfg = _cfg_riconoscimento()
    r = None
    try:
        h = dxf_cache.hash_file(percorso)
        nomi = {os.path.basename(percorso), os.path.basename(str((art or {}).get('dxf_filename') or ''))}
        for nome in [n for n in nomi if n]:
            c = dxf_cache.get(dxf_cache.chiave_cache(h, nome, cfg))
            sp = ((c or {}).get('payload') or {}).get('spessore')
            if isinstance(sp, dict) and sp.get('spessore_mm'):
                r = sp
                break
    except Exception as e:      # noqa: BLE001
        logger.warning('cache dello spessore non letta per %s: %s', percorso, e)
    if r is None:
        try:
            det = _lettura_motore(percorso)
            cart = dxf_scanner.estrai_materiale_da_cartiglio(percorso) or {}
            mat = (art or {}).get('materiale') or (cart.get('materiale') if (cart.get('confidence') or 0) >= 0.5 else None)
            dim = dxf_scanner.estrai_dimensioni_da_descrizione_cartiglio(percorso) or {}
            altre = ([{'spessore_mm': dim['spessore_mm'], 'confidence': dim.get('confidence') or 0,
                       'source': dim.get('source') or 'cartiglio_descrizione', 'warnings': [], 'details': {}}]
                     if dim.get('spessore_mm') else [])
            # l'area del contorno confermato dal laser vale piu' di quella letta
            area = ((art or {}).get('area_dm2') if (art or {}).get('geometria_manuale_confermata')
                    else det.get('area_dm2'))
            r = dxf_scanner.estrai_spessore_da_cartiglio(
                percorso, area_dm2=area, materiale=mat,
                area_incerta=bool(det.get('needs_manual_select')) and not (art or {}).get('geometria_manuale_confermata'),
                vista=det.get('spessore_vista'), altre_fonti=altre)
        except Exception as e:      # noqa: BLE001
            logger.warning('spessore non letto per %s: %s', percorso, e)
            r = {'spessore_mm': None, 'confidence': 0.0, 'source': 'none', 'fonti': []}
    with _FOGLI_LOCK:
        while len(_FOGLI) >= _FOGLI_MAX:
            _FOGLI.pop(next(iter(_FOGLI)))
        _FOGLI[chiave] = r
    return r


def proposta_spessore(percorso: str | None, art: dict | None = None) -> dict | None:
    """Lo spessore da proporre su una riga bloccata per "spessore mancante":
    la lettura migliore del motore, arrotondata agli spessori a magazzino.
    {spessore_mm, fonte: "vista di fianco", fonti: [...], sicura} o None."""
    if not percorso or not str(percorso).lower().endswith('.dxf'):
        return None
    r = lettura_spessore(percorso, art)
    v = r.get('spessore_mm')
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if not SPESSORE_MIN_MM <= v <= SPESSORE_MAX_MM:
        return None
    from .preventivi.pick_part import _snap_stock
    s = _snap_stock(v)
    if abs(s - v) > 0.15 * v:            # lontano da uno spessore a magazzino: niente proposta
        return None
    conf = float(r.get('confidence') or 0)
    fonti = [_fonte_breve(x) for x in (r.get('confermato_da') or [r.get('source')])]
    return {'spessore_mm': s, 'fonte': ' + '.join(list(dict.fromkeys(f for f in fonti if f))[:2]),
            'sicura': conf >= 0.7, 'confidenza': round(conf, 2),
            'altre': sorted({float(x['spessore_mm']) for x in r.get('alternative') or []
                             if x.get('spessore_mm') and abs(float(x['spessore_mm']) - s) > 0.05})[:3]}


def _f(v: float) -> str:
    return ('%.2f' % v).rstrip('0').rstrip('.') if abs(v) < 1e7 else '0'


def _d_poli(punti, chiudi=False) -> str:
    """Punti [x, y] (mm, y in su) -> comandi di un path SVG (y in giu')."""
    if not punti or len(punti) < 2:
        return ''
    out = ['M' + _f(punti[0][0]) + ' ' + _f(-punti[0][1])]
    out += ['L' + _f(x) + ' ' + _f(-y) for x, y in punti[1:]]
    if chiudi:
        out.append('Z')
    return ''.join(out)


def svg_foglio(gj: dict, contorno: dict | None) -> tuple:
    """SVG del foglio intero nelle coordinate del disegno in mm, con l'asse y
    girato: un punto (u, v) dell'SVG e' il punto (u, -v) del disegno. Cosi' un
    clic si riporta al disegno con la sola matrice dello schermo
    (getScreenCTM().inverse()) e poi y = -v (data-y-invertita="1").

    Ritorna (svg, vista) con vista = [x0, y0, x1, y1] del disegno in mm."""
    from xml.sax.saxutils import escape
    ext = gj.get('extents') or [0, 0, 100, 100]
    x0, y0, x1, y1 = [float(v) for v in ext]
    if contorno and contorno.get('outer'):
        xs = [p[0] for p in contorno['outer']]
        ys = [p[1] for p in contorno['outer']]
        x0, y0, x1, y1 = min(x0, min(xs)), min(y0, min(ys)), max(x1, max(xs)), max(y1, max(ys))
    w, h = max(x1 - x0, 1.0), max(y1 - y0, 1.0)
    m = 0.03 * max(w, h)
    vb = (x0 - m, -(y1 + m), w + 2 * m, h + 2 * m)
    geo = ''.join(_d_poli(p['pts']) for p in gj.get('polylines') or [] if p.get('kind') != 'annot')
    ann = ''.join(_d_poli(p['pts']) for p in gj.get('polylines') or [] if p.get('kind') == 'annot')
    parti = [f'<svg xmlns="http://www.w3.org/2000/svg" class="lz-disegno lz-foglio-svg" '
             f'viewBox="{_f(vb[0])} {_f(vb[1])} {_f(vb[2])} {_f(vb[3])}" preserveAspectRatio="xMidYMid meet" '
             f'data-y-invertita="1" data-x0="{_f(x0)}" data-y0="{_f(y0)}" data-x1="{_f(x1)}" data-y1="{_f(y1)}">']
    if ann:
        parti.append(f'<path class="fg-annot" d="{ann}" vector-effect="non-scaling-stroke"/>')
    if geo:
        parti.append(f'<path class="fg-geo" d="{geo}" vector-effect="non-scaling-stroke"/>')
    testi = []
    for t in (gj.get('texts') or [])[:1500]:
        try:
            x, y, hh = float(t['x']), float(t['y']), max(float(t.get('h') or 2.5), 0.1)
        except (TypeError, ValueError, KeyError):
            continue
        rot = float(t.get('rot') or 0)
        tr = f' transform="rotate({_f(-rot)} {_f(x)} {_f(-y)})"' if rot else ''
        base = {'hanging': 'hanging', 'middle': 'central'}.get(t.get('baseline'), 'auto')
        testi.append(f'<text x="{_f(x)}" y="{_f(-y)}" font-size="{_f(hh)}" text-anchor="{t.get("anchor") or "start"}" '
                     f'dominant-baseline="{base}"{tr}>{escape(str(t.get("s") or ""))}</text>')
    if testi:
        parti.append('<g class="fg-testi">' + ''.join(testi) + '</g>')
    if contorno and contorno.get('outer'):
        d = _d_poli(contorno['outer'], True) + ''.join(_d_poli(f, True) for f in contorno.get('holes') or [])
        parti.append(f'<path class="fg-pezzo" d="{d}" fill-rule="evenodd" vector-effect="non-scaling-stroke"/>')
    parti.append('<g class="fg-scelte"></g></svg>')
    return ''.join(parti), [x0, y0, x1, y1]


def _contorno_motore(gj: dict, det: dict) -> dict | None:
    """Il contorno scelto dal motore, in mm come il foglio (il motore lo da'
    nelle unita' del disegno: x scala_unita_mm)."""
    sel = next((c for c in det.get('candidates') or [] if c.get('is_selected')), None)
    if not sel or not sel.get('geometry'):
        return None
    k = float(gj.get('scala_unita_mm') or 1.0)
    return {'outer': [[float(x) * k, float(y) * k] for x, y in sel['geometry']], 'holes': []}


_SUGGERIMENTI = {'DA_SVILUPPARE': 'sviluppo', 'PIU_PEZZI': 'piu_pezzi', 'NON_LASER': 'non_laser'}


def _pezzi_foglio_confermati(art) -> bool:
    """Il foglio ha gia' i pezzi confermati (api_pezzi_foglio): altre
    decisioni sul foglio lascerebbero in giro i loro DXF."""
    return bool(art and art.get('pezzi_foglio')
                and (art.get('decisione_laser') or {}).get('scelta') == 'piu_pezzi_confermati')


def _errore_pezzi_foglio():
    return jsonify({'success': False, 'codice': 'pezzi_foglio',
                    'error': "Questo foglio ha gia' i pezzi confermati: annulla prima i pezzi del foglio"}), 409


def _pezzo(order_id, articolo_id):
    """(ordine, pezzo, percorso del DXF) o una risposta d'errore."""
    A = _app()
    order = A._ordine_esistente(order_id)
    if not order:
        return None, None, None, A._non_trovato_ordine()
    art, percorso = A._articolo_ordine(order, articolo_id)
    if not art:
        return None, None, None, (jsonify({'success': False, 'error': "Pezzo non trovato in quest'ordine"}), 404)
    if not percorso:
        return order, art, None, (jsonify({'success': False, 'codice': 'senza_disegno',
                                           'error': "Il disegno del pezzo non c'è fra i disegni dell'ordine"}), 404)
    if not percorso.lower().endswith('.dxf'):
        return order, art, None, (jsonify({'success': False, 'codice': 'non_dxf',
                                           'error': 'Il disegno non è un DXF: aprilo dalla scheda Disegni'}), 415)
    return order, art, percorso, None


def _sposta_dxf_lantek(order, art, percorso, materiale, spessore) -> int:
    """Cambiati spessore o materiale: i DXF per Lantek del pezzo (e dei pezzi del
    foglio) vanno nella cartella della lamiera giusta (LANTEK/<lamiera>/).
    Ritorna quanti file ha spostato."""
    import glob
    import shutil
    A = _app()
    cart = os.path.join(A.DRAWINGS_FOLDER, order.id, 'LANTEK')
    dest_dir = os.path.join(cart, A._nome_lamiera(materiale, spessore))
    nomi = []
    if percorso:
        nomi.append(A._nome_base_disegno(os.path.basename(percorso)))
    for pz in art.get('pezzi_foglio') or []:
        if isinstance(pz, dict) and pz.get('codice'):
            nomi.append(str(pz['codice']) + '.dxf')
    n = 0
    for nome in nomi:
        for f in glob.glob(os.path.join(cart, '*', nome)):
            if A.CARTELLA_DA_PREPARARE in f or os.path.dirname(f) == dest_dir:
                continue
            os.makedirs(dest_dir, exist_ok=True)
            shutil.move(f, os.path.join(dest_dir, nome))
            n += 1
    return n


def _contorno_salvato(order, art, percorso) -> dict | None:
    """Il contorno scelto a mano (o confermato) per questo pezzo, in mm come il
    foglio: dal campo salvato o, per i pezzi salvati prima, dal DXF per Lantek
    scritto dal contorno scelto (polilinee chiuse sul TAGLIO)."""
    cs = art.get('contorno_scelto')
    if isinstance(cs, dict) and cs.get('outer'):
        return {'outer': cs['outer'], 'holes': cs.get('holes') or []}
    dec = (art.get('decisione_laser') or {}).get('scelta')
    if dec not in ('scelto', 'giusto'):
        return None
    try:
        import glob
        import ezdxf
        A = _app()
        nome = A._nome_base_disegno(os.path.basename(percorso))
        cart = os.path.join(A.DRAWINGS_FOLDER, order.id, 'LANTEK')
        for f in glob.glob(os.path.join(cart, '*', nome)):
            if A.CARTELLA_DA_PREPARARE in f:
                continue
            doc = ezdxf.readfile(f)
            anelli = []
            for e in doc.modelspace().query('LWPOLYLINE[layer=="TAGLIO"]'):
                if e.closed:
                    anelli.append([[float(x), float(y)] for x, y, *_ in e.get_points()])
            if not anelli:
                return None
            from shapely.geometry import Polygon
            anelli.sort(key=lambda a: -Polygon(a).area if len(a) >= 3 else 0)
            # solo se sta nel foglio (i puliti copiati dal disegno possono essere spostati)
            gj = json.loads(A._get_dxf_geometry_cached(percorso, _cfg_riconoscimento()) or '{}')
            bb = gj.get('extents')
            xs = [p[0] for p in anelli[0]]
            ys = [p[1] for p in anelli[0]]
            if bb and len(bb) == 4 and not (bb[0] - 1 <= min(xs) and max(xs) <= bb[2] + 1
                                            and bb[1] - 1 <= min(ys) and max(ys) <= bb[3] + 1):
                return None
            return {'outer': anelli[0], 'holes': anelli[1:]}
    except Exception:
        logger.warning('contorno salvato non letto per %s', percorso, exc_info=True)
    return None


def _foglio(percorso: str) -> dict:
    """{svg, vista, contorno, avvisi, confidenza} del foglio, in cache."""
    try:
        mt = os.path.getmtime(percorso)
    except OSError:
        mt = 0
    chiave = ('foglio', percorso, mt)
    with _FOGLI_LOCK:
        if chiave in _FOGLI:
            return _FOGLI[chiave]
    A = _app()
    gj = json.loads(A._get_dxf_geometry_cached(percorso, _cfg_riconoscimento()) or '{}')
    if gj.get('error'):
        raise ValueError(gj['error'])
    det = _lettura_motore(percorso)
    contorno = _contorno_motore(gj, det)
    svg, vista = svg_foglio(gj, contorno)
    r = {'svg': svg, 'vista': vista, 'contorno': contorno,
         'avvisi': list(det.get('warnings') or []) + list(gj.get('warnings') or []),
         'confidenza': det.get('confidence'),
         # classificatore del tipo di disegno (motore/riconoscimento): solo un
         # suggerimento, decide sempre il laser col suo tasto
         'suggerimento': _SUGGERIMENTI.get(str(det.get('tipo_disegno') or '').upper()),
         'misure_motore': [det.get('bbox_width_mm'), det.get('bbox_height_mm')]}
    with _FOGLI_LOCK:
        while len(_FOGLI) >= _FOGLI_MAX:
            _FOGLI.pop(next(iter(_FOGLI)))
        _FOGLI[chiave] = r
    return r


# ---------------------------------------------------------------------------
# La riga dell'ordine del cliente, accanto al disegno
# ---------------------------------------------------------------------------
_RIGHE_PDF: dict = {}


def _pdf_ordine(order) -> str | None:
    """Il PDF dell'ordine del cliente allegato all'ordine (il primo)."""
    from .database import get_session
    from .models import OrderFile
    s = get_session()
    try:
        f = (s.query(OrderFile).filter(OrderFile.order_id == order.id, OrderFile.file_type == 'PDF')
             .order_by(OrderFile.upload_date).first())
        return f.filepath if f and f.filepath and os.path.isfile(f.filepath) else None
    finally:
        s.close()


def righe_ordine_cliente(percorso: str | None) -> dict:
    """Le righe del PDF d'ordine come le ha scritte il cliente, per codice
    (ordine_codici.chiave: senza revisione). Lettura esatta, senza AI
    (Ordine Fornitore DECA, ordini prezzati Poliform/B&B...); {} se il formato
    non si legge. In cache finche' il file non cambia.

    {chiave: [{pos, codice, quantita, descrizione, commessa, materiale, spessore_mm}]}"""
    if not percorso:
        return {}
    try:
        st = os.stat(percorso)
    except OSError:
        return {}
    k = (os.path.normcase(os.path.abspath(percorso)), st.st_mtime, st.st_size)
    with _FOGLI_LOCK:
        if k in _RIGHE_PDF:
            return _RIGHE_PDF[k]
    from .preventivi.ordine_codici import chiave
    righe = []
    try:
        with open(percorso, 'rb') as fh:
            dati = fh.read()
        from .preventivi.ordine_testo import leggi_ordine_fornitore
        d = leggi_ordine_fornitore(dati)
        if d and d.get('articoli'):
            for i, a in enumerate(d['articoli']):
                righe.append({'pos': i + 1, 'codice': a.get('codice'), 'quantita': a.get('_qta_letta') or a.get('quantita'),
                              'descrizione': a.get('descrizione') or '', 'commessa': a.get('_commessa'),
                              'materiale': a.get('materiale'), 'spessore_mm': a.get('spessore_mm')})
        else:
            from .preventivi.ordine_prezzi import leggi_ordine_prezzato
            p = leggi_ordine_prezzato(dati)
            for i, r in enumerate((p or {}).get('righe') or []):
                righe.append({'pos': r.get('pos') or i + 1, 'codice': r.get('codice'), 'quantita': r.get('quantita'),
                              'descrizione': ' · '.join(x for x in (r.get('descrizione'), r.get('dettaglio')) if x),
                              'commessa': None, 'materiale': None, 'spessore_mm': None})
    except Exception:
        logger.warning('righe del PDF d\'ordine non lette: %s', percorso, exc_info=True)
        righe = []
    out = {}
    for r in righe:
        if r.get('codice'):
            out.setdefault(chiave(r['codice']), []).append(r)
    with _FOGLI_LOCK:
        if len(_RIGHE_PDF) > 60:
            _RIGHE_PDF.clear()
        _RIGHE_PDF[k] = out
    return out


def riga_ordine_di(order, art: dict | None) -> dict | None:
    """La riga dell'ordine del cliente di un pezzo, per il controllo a tutto
    schermo: {codice, righe: [{pos, quantita, descrizione, commessa}],
    materiale, spessore_mm, file}. Dal PDF d'ordine; se non si legge, dalle
    righe salvate al caricamento (extra 'ordine': quantita' e commessa).
    None se non si trova (la pagina allora non mostra niente)."""
    if not art:
        return None
    from .preventivi.ordine_codici import chiave
    pdf = _pdf_ordine(order)
    per = righe_ordine_cliente(pdf)
    trovate = []
    for c in (art.get('codice'), art.get('dxf_filename')):
        if c and chiave(c) in per:
            trovate = per[chiave(c)]
            break
    if trovate:
        r0 = trovate[0]
        mat = next((r['materiale'] for r in trovate if r.get('materiale')), None)
        sp = next((r['spessore_mm'] for r in trovate if r.get('spessore_mm')), None)
        return {'codice': r0['codice'], 'file': os.path.basename(pdf),
                'righe': [{k: r.get(k) for k in ('pos', 'quantita', 'descrizione', 'commessa')} for r in trovate[:6]],
                'materiale': mat, 'spessore_mm': sp}
    od = art.get('ordine') if isinstance(art.get('ordine'), dict) else {}
    salvate = [r for r in od.get('righe') or [] if isinstance(r, dict) and r.get('qta')]
    if salvate:
        return {'codice': art.get('codice'), 'file': od.get('file') or None,
                'righe': [{'pos': None, 'quantita': r.get('qta'), 'descrizione': None,
                           'commessa': r.get('commessa')} for r in salvate[:6]],
                'materiale': None, 'spessore_mm': None}
    return None


@bp_laser.route('/api/orders/<order_id>/pezzi/<articolo_id>/lettura', methods=['GET'])
@richiede('laser', 'ufficio')
def api_pezzo_lettura(order_id, articolo_id):
    """Il foglio intero del disegno (SVG in mm, vedi svg_foglio) col contorno
    del motore in verde, e i suoi dubbi in frasi corte ("Perche' te lo chiedo")."""
    A = _app()
    try:
        order, art, percorso, err = _pezzo(order_id, articolo_id)
        if err:
            return err
        f = _foglio(percorso)
        # contorno scelto (o confermato) a mano: si mostra quello, non il motore
        tuo = _contorno_salvato(order, art, percorso)
        if tuo:
            gj = json.loads(A._get_dxf_geometry_cached(percorso, _cfg_riconoscimento()) or '{}')
            svg_tuo, vista_tuo = svg_foglio(gj, tuo)
            f = {**f, 'svg': svg_tuo, 'vista': vista_tuo, 'contorno': tuo}
        mt = A._stato_motore(art)
        perche = list(mt.get('motivi') or [])
        for b in A.motivi_brevi(f['avvisi']):
            if b not in perche:
                perche.append(b)
        try:
            riga = riga_ordine_di(order, art)
        except Exception:
            logger.warning('riga dell\'ordine non letta per %s', art.get('codice'), exc_info=True)
            riga = None
        return jsonify({'riga_ordine': riga,'success': True, 'codice': art.get('codice'), 'svg': f['svg'], 'vista': f['vista'],
                        'ha_contorno': bool(f['contorno']), 'contorno_tuo': bool(tuo), 'perche': perche[:3],
                        'suggerimento': f.get('suggerimento'),
                        'motore': mt.get('stato'), 'decisione': mt.get('decisione'),
                        'misure': [art.get('bbox_w_mm'), art.get('bbox_h_mm')]}), 200
    except Exception as e:
        logger.exception('lettura del pezzo fallita')
        return jsonify({'success': False, 'error': 'Il disegno non si legge: ' + str(e)[:160]}), 500


@bp_laser.route('/api/orders/<order_id>/pezzi/<articolo_id>/foglio.svg', methods=['GET'])
@richiede('laser', 'ufficio')
def api_pezzo_foglio_svg(order_id, articolo_id):
    """Il foglio intero come immagine (stesso SVG di /lettura). La
    trasformazione disegno <-> SVG: x uguale, y girata (data-y-invertita)."""
    try:
        _order, _art, percorso, err = _pezzo(order_id, articolo_id)
        if err:
            return err
        f = _foglio(percorso)
        stile = ('<style>.fg-geo{fill:none;stroke:#59616d}.fg-annot{fill:none;stroke:#b8bec7}'
                 '.fg-testi{fill:#8a929e;font-family:sans-serif}'
                 '.fg-pezzo{fill:#16a34a;fill-opacity:.14;stroke:#15803d;stroke-width:2.5}</style>')
        svg = f['svg'].replace('>', '>' + stile, 1)
        resp = Response(svg, mimetype='image/svg+xml; charset=utf-8')
        resp.headers['Cache-Control'] = 'private, max-age=300'
        resp.headers['X-Trasforma'] = 'x=u; y=-v (mm)'
        return resp
    except Exception as e:
        logger.exception('foglio del pezzo fallito')
        return jsonify({'success': False, 'error': str(e)[:200]}), 500


def _disegno_pulito_pronto(order, art) -> bool:
    """C'e' gia' il DXF pulito per Lantek di questo pezzo (cartella LANTEK
    dell'ordine, fuori da _DA PREPARARE)?"""
    A = _app()
    righe = A._distinta_ordine(order)[0]
    cod = str(art.get('codice') or '').strip()
    dati = {}
    for r in righe:
        if r.get('tipo') != 'lamiera' or str(r.get('codice') or '').strip().lower() != cod.lower():
            continue
        dati[cod.lower()] = (cod, [])
        if r.get('disegno'):
            dati[A._nome_base_disegno(r['disegno']).rsplit('.', 1)[0].lower()] = (cod, [])
    if not dati:
        return False
    for _percorso, arc in A._struttura_zip('X', os.path.join(A.DRAWINGS_FOLDER, order.id),
                                           A._disegni_ordine(order), righe):
        v = A._voce_per_lantek(arc, dati)
        if v and v[0].rsplit('/', 1)[-1][:-4].lower() == cod.lower():
            return True
    return False


def _contorno_preciso(percorso: str, det: dict, gj: dict):
    """Il contorno del motore con i fori, preciso (non i 200 punti del
    disegno): si "clicca" sul suo bordo con lo stesso strumento del CAD e si
    prende il contorno che ha la stessa area. None se non torna."""
    from .preventivi.pick_part import pick_candidates
    sel = next((c for c in det.get('candidates') or [] if c.get('is_selected')), None)
    if not sel or not sel.get('geometry'):
        return None
    k = float(gj.get('scala_unita_mm') or 1.0)
    area = float(sel.get('area_dm2') or 0)
    punti = sel['geometry']
    for i in (0, len(punti) // 3, (2 * len(punti)) // 3):
        x, y = float(punti[i][0]) * k, float(punti[i][1]) * k
        r = pick_candidates(percorso, x, y, _cfg_riconoscimento())
        for c in (r or {}).get('candidates') or []:
            lorda = float(c.get('area_lorda_dm2') or c.get('area_dm2') or 0)
            if area > 0 and abs(lorda - area) / area < 0.02:
                return c.get('outer_xy'), c.get('holes_xy') or []
    return None


@bp_laser.route('/api/orders/<order_id>/pezzi/<articolo_id>/giusto', methods=['POST'])
@richiede('laser', 'ufficio')
def api_pezzo_giusto(order_id, articolo_id):
    """"Si', e' il pezzo giusto" dal controllo dei disegni.

    Se il DXF pulito per Lantek c'e' gia' (o il codice e' gia' in Lantek) e'
    la conferma di sempre (/conferma). Se manca, si salva il contorno del
    motore come se l'avesse scelto il laser: cosi' il DXF pulito si crea e il
    pezzo puo' partire. Corpo: {serve_pulito?: bool} (lo sa la pagina: pezzo
    nuovo per Lantek)."""
    A = _app()
    try:
        order, art, percorso, err = _pezzo(order_id, articolo_id)
        if err and not (order and art):
            return err
        if _pezzi_foglio_confermati(art):
            return _errore_pezzi_foglio()
        corpo = request.get_json(silent=True) or {}
        if corpo.get('serve_pulito') and percorso and not _disegno_pulito_pronto(order, art):
            f = _foglio(percorso)
            gj = json.loads(A._get_dxf_geometry_cached(percorso, _cfg_riconoscimento()) or '{}')
            preciso = _contorno_preciso(percorso, _lettura_motore(percorso), gj)
            if preciso and preciso[0]:
                from .api_contorno_ordine import salva_contorno
                esito, stato = salva_contorno(order_id, articolo_id, preciso[0], preciso[1],
                                              dal_motore=True, pagina='laser')
                return jsonify({**esito, 'modo': 'contorno'}), stato
            if not f.get('contorno'):
                return jsonify({'success': False, 'codice': 'senza_contorno',
                                'error': 'Il motore non ha un contorno da confermare: sceglilo tu sul disegno (S)'}), 409
            return jsonify({'success': False, 'codice': 'contorno_non_preciso',
                            'error': 'Non riesco a ricostruire il contorno con i fori: sceglilo tu sul disegno (S)'}), 409
        if not order.preventivo_id_origine:
            return jsonify({'success': False, 'error': "Quest'ordine non ha pezzi da verificare"}), 404
        chi = A._chi_nome()
        esito = A.PreventivoManager.conferma_controllo_articolo(order.preventivo_id_origine, articolo_id, chi)
        if not esito.get('success'):
            return jsonify({'success': False, 'error': esito.get('error') or 'non riuscito'}), 400
        A.PreventivoManager.decisione_laser_articolo(order.preventivo_id_origine, articolo_id, 'giusto', chi)
        A._registra_decisione_laser(order, articolo_id, 'contorno_confermato', 'giusto', chi)
        A._audit('PEZZO_VERIFICATO', 'orders', order_id,
                 f"Disegno del pezzo {art.get('codice') or articolo_id} confermato giusto (laser)")
        invalida(order_id)
        return jsonify({'success': True, 'modo': 'conferma'}), 200
    except Exception as e:
        logger.exception('conferma del pezzo fallita')
        return jsonify({'success': False, 'error': str(e)[:200]}), 500


_SCELTE = {'sviluppo': 'va sviluppato', 'piu_pezzi': "ci sono più pezzi: da fare a mano in Lantek",
           'non_laser': "non è da laser"}


def _procura_dal_corpo(corpo: dict):
    """{tipo, profilo, lunghezza_mm, nota} mandato col tasto N, controllato.
    (dati, None) o (None, errore); (None, None) se non c'e'."""
    from . import procura as _pc
    pr = corpo.get('procura')
    if pr is None:
        return None, None
    if not isinstance(pr, dict) or pr.get('tipo') not in _pc.TIPI:
        return None, 'Cosa procurare? tubo, laser_tubo, meccanica, commerciale o altro'
    lung = pr.get('lunghezza_mm')
    if lung not in (None, ''):
        try:
            lung = round(float(str(lung).replace(',', '.')), 1)
        except (TypeError, ValueError):
            return None, 'Lunghezza non valida: scrivi i mm'
        if not 1 <= lung <= 24000:
            return None, 'Lunghezza fuori misura (1-24000 mm)'
    else:
        lung = None
    nota = str(pr.get('nota') or '').strip()[:200]
    if pr['tipo'] == 'altro' and not nota:
        return None, "Per \"Altro\" scrivi una nota corta: cos'è"
    profilo = str(pr.get('profilo') or '').strip()[:80] or None
    if pr['tipo'] in ('tubo', 'laser_tubo') and not profilo:
        # quale tubo: senza profilo non si puo' ordinare (ne' fare le barre)
        return None, 'Quale tubo? Scrivi il profilo (es. Tubo quadro 50x50 sp.2mm)'
    if pr['tipo'] in ('tubo', 'laser_tubo') and lung is None:
        return None, 'Scrivi la lunghezza del pezzo di tubo (mm)'
    per = pr.get('per_assieme')
    if per not in (None, ''):
        try:
            per = float(str(per).replace(',', '.'))
            if per != int(per) or not 1 <= per <= 10000:
                raise ValueError
            per = int(per)
        except (TypeError, ValueError):
            return None, 'Pezzi non validi: serve un numero intero da 1 in su'
    else:
        per = None
    return {'tipo': pr['tipo'], 'profilo': profilo, 'lunghezza_mm': lung, 'nota': nota or None,
            'materiale': str(pr.get('materiale') or '').strip()[:40] or None,
            'descrizione': str(pr.get('descrizione') or '').strip()[:200] or None,
            'per_assieme': per}, None


def _quantita_distinta(order, articolo_id) -> int:
    """Pezzi in tutto del pezzo nell'ordine (quantita' x assieme, come la distinta)."""
    try:
        for r in _app()._distinta_ordine(order)[0]:
            if r.get('tipo') == 'lamiera' and str(r.get('articolo_id')) == str(articolo_id):
                return max(1, int(r.get('quantita') or 1))
    except Exception:
        logger.warning("quantita' del pezzo non letta", exc_info=True)
    return 1


def _testi_riga(order, art) -> list:
    """Le descrizioni della riga dell'ordine del cliente del pezzo."""
    try:
        riga = riga_ordine_di(order, art)
        return [str(r['descrizione']) for r in (riga or {}).get('righe') or [] if r.get('descrizione')]
    except Exception:
        logger.warning("riga dell'ordine non letta per %s", (art or {}).get('codice'), exc_info=True)
        return []


def _profilo_cartiglio(percorso) -> str | None:
    if not (percorso and str(percorso).lower().endswith('.dxf')):
        return None
    try:
        from .preventivi.verifica_coerenza import descrizione_profilo
        return descrizione_profilo(percorso)
    except Exception:
        return None


@bp_laser.route('/api/orders/<order_id>/pezzi/<articolo_id>/procura-proposta', methods=['GET'])
@richiede('laser', 'ufficio')
def api_pezzo_procura_proposta(order_id, articolo_id):
    """Cosa proporre quando il laser dice "non e' da laser" (tasto N): il
    profilo e la lunghezza letti nella riga dell'ordine del cliente o nel
    cartiglio ("Tubo 40x40x2 Lg. 795", "Angolare 50x5"...), o una sezione di
    tubo disegnata. Solo un suggerimento: decide il tasto.
    {success, proposta: {tipo, profilo, lunghezza_mm, fonte, testo, sezione,
     alternativa}, attuale: la voce gia' salvata o null, quantita}"""
    A = _app()
    from . import procura as _pc
    try:
        order = A._ordine_esistente(order_id)
        if not order:
            return A._non_trovato_ordine()
        art, percorso = A._articolo_ordine(order, articolo_id)
        if not art:
            return jsonify({'success': False, 'error': "Pezzo non trovato in quest'ordine"}), 404
        testi = _testi_riga(order, art)
        cart = _profilo_cartiglio(percorso)
        prop = _pc.proposta(testi, cart, percorso, [art.get('bbox_w_mm'), art.get('bbox_h_mm')])
        attuale = next((v for v in _pc.elenco('tutti', ordine=order.id) if v['chiave'] == f'art:{articolo_id}'), None)
        from .assiemi_ordine import da_confermare
        cod_ass = art.get('codice_assieme') or None
        qta_ass = None
        if cod_ass:
            prev = A.PreventivoManager.get(order.preventivo_id_origine, include_children=True) or {}
            qta_ass = next((int(x.get('qty') or 1) for x in prev.get('assiemi') or []
                            if x.get('codice_assieme') == cod_ass), 1)
        return jsonify({'success': True, 'proposta': prop, 'attuale': attuale,
                        'quantita': _quantita_distinta(order, articolo_id),
                        # pezzi per assieme (o del pezzo sciolto) e da dove
                        'assieme': cod_ass, 'qta_assieme': qta_ass,
                        'per_assieme': int(art.get('quantita') or 1),
                        'qta_da_confermare': da_confermare(art),
                        'materiale': art.get('materiale'),
                        'descrizione': (testi[0] if testi else None) or cart,
                        'profili': _pc.nomi_profili()}), 200
    except Exception as e:
        logger.exception('proposta da procurare fallita')
        return jsonify({'success': False, 'error': str(e)[:200]}), 500


@bp_laser.route('/api/orders/<order_id>/pezzi/<articolo_id>/decisione', methods=['POST'])
@richiede('laser', 'ufficio')
def api_pezzo_decisione(order_id, articolo_id):
    """Decisione del laser su un pezzo che non si manda a Lantek cosi' com'e'.

    Corpo: {"scelta": "sviluppo" | "piu_pezzi" | "non_laser"} oppure
    {"scelta": null} per toglierla. Il pezzo resta fuori da "Manda a Lantek"
    col suo motivo; finisce anche nel registro degli esempi del motore.

    Con "non_laser" si dice anche cosa procurare (tasto N, poi T/L/M/C/A):
    "procura": {"tipo": "tubo" | "laser_tubo" | "meccanica" | "commerciale" |
    "altro", "profilo": "Tubo quadro 40x40 sp.2mm", "lunghezza_mm": 795,
    "nota": "..."} -> la voce in "Da procurare" (backend/procura.py). Tolta o
    cambiata la decisione, la voce si toglie (se non e' gia' ordinata)."""
    A = _app()
    try:
        corpo = request.get_json(silent=True) or {}
        scelta = corpo.get('scelta')
        if scelta is not None and scelta not in _SCELTE:
            return jsonify({'success': False, 'error': 'Scelta non valida (sviluppo, piu_pezzi, non_laser)'}), 400
        procura, err_pr = _procura_dal_corpo(corpo) if scelta == 'non_laser' else (None, None)
        if err_pr:
            return jsonify({'success': False, 'error': err_pr}), 400
        order = A._ordine_esistente(order_id)
        if not order:
            return A._non_trovato_ordine()
        if not order.preventivo_id_origine:
            return jsonify({'success': False, 'error': "Quest'ordine non ha pezzi con la distinta"}), 404
        art, _percorso = A._articolo_ordine(order, articolo_id)
        if not art:
            return jsonify({'success': False, 'error': "Pezzo non trovato in quest'ordine"}), 404
        if _pezzi_foglio_confermati(art):
            return _errore_pezzi_foglio()
        chi = A._chi_nome()
        from . import assiemi_ordine as _ao
        if procura and _ao.da_confermare(art) and not procura.get('per_assieme'):
            return jsonify({'success': False, 'codice': 'qta_da_confermare',
                            'error': "Quanti ne servono per ogni assieme? L'ordine non lo dice: scrivilo"}), 400
        if procura and procura.get('per_assieme') and (procura['per_assieme'] != int(art.get('quantita') or 0)
                                                        or _ao.da_confermare(art)):
            pa = _ao.imposta_per_assieme(order, articolo_id, procura['per_assieme'], chi)
            if not pa.get('success'):
                return jsonify({'success': False, 'error': pa.get('error') or 'non salvato'}), 400
            _audit_per_assieme(A, order_id, pa)
        esito = A.PreventivoManager.decisione_laser_articolo(order.preventivo_id_origine, articolo_id, scelta, chi)
        if not esito.get('success'):
            return jsonify({'success': False, 'error': esito.get('error') or 'non riuscito'}), 400
        A._registra_decisione_laser(order, articolo_id, 'decisione_laser' if scelta else 'decisione_annullata',
                                    scelta, chi)
        A._audit('PEZZO_DECISIONE', 'orders', order_id,
                 f"Pezzo {art.get('codice') or articolo_id}: "
                 + (_SCELTE[scelta] + ' (fuori da Lantek)' if scelta else 'decisione tolta'))
        from . import procura as _pc
        voce = None
        if scelta == 'non_laser' and procura:
            testi = _testi_riga(order, art)
            voce = _pc.classifica(order_id, articolo_id, tipo=procura['tipo'], codice=art.get('codice'),
                                  descrizione=procura.get('descrizione') or (testi[0] if testi else None)
                                  or _profilo_cartiglio(_percorso),
                                  profilo=procura['profilo'], lunghezza_mm=procura['lunghezza_mm'],
                                  quantita=_quantita_distinta(order, articolo_id),
                                  materiale=procura.get('materiale') or art.get('materiale'),
                                  nota=procura['nota'], chi=chi)
            det = ', '.join(x for x in (procura['profilo'] or '',
                                        f"{procura['lunghezza_mm']:g} mm" if procura['lunghezza_mm'] else '',
                                        procura['nota'] or '') if x)
            A._audit('PROCURA_CLASSIFICA', 'orders', order_id,
                     f"{art.get('codice') or articolo_id}: da procurare come {_pc.TIPI[procura['tipo']]}"
                     + (f' ({det})' if det else '') + f" · {voce['quantita']} pz")
        elif scelta != 'non_laser':
            via = _pc.togli_classificato(order_id, articolo_id)
            if via and via.get('tolta'):
                A._audit('PROCURA_TOLTA', 'orders', order_id,
                         f"{art.get('codice') or articolo_id}: tolto da \"Da procurare\" (decisione cambiata)")
        invalida(order_id)
        return jsonify({'success': True, 'scelta': scelta, 'prima': esito.get('prima'), 'procura': voce}), 200
    except Exception as e:
        logger.exception('decisione sul pezzo fallita')
        return jsonify({'success': False, 'error': str(e)[:200]}), 500


SPESSORE_MIN_MM, SPESSORE_MAX_MM = 0.3, 50.0


@bp_laser.route('/api/orders/<order_id>/pezzi/<articolo_id>/dati', methods=['POST'])
@richiede('laser', 'ufficio')
def api_pezzo_dati(order_id, articolo_id):
    """Spessore e/o materiale di un pezzo nuovo, decisi dal laser guardando il
    disegno (il cartiglio non li dice): servono per crearlo in Lantek.

    Corpo: {"spessore_mm": 3} e/o {"materiale": "INOX"} (un materiale che
    Lantek conosce). Si scrivono sulla riga del pezzo; finiscono anche nel
    registro degli esempi del motore."""
    A = _app()
    try:
        corpo = request.get_json(silent=True) or {}
        sp, mat = corpo.get('spessore_mm'), corpo.get('materiale')
        if sp is None and not mat:
            return jsonify({'success': False, 'error': 'Scrivi lo spessore o scegli il materiale'}), 400
        if sp is not None:
            try:
                sp = round(float(str(sp).replace(',', '.')), 2)
            except (TypeError, ValueError):
                return jsonify({'success': False, 'error': 'Spessore non valido: scrivi un numero in mm'}), 400
            if not SPESSORE_MIN_MM <= sp <= SPESSORE_MAX_MM:
                return jsonify({'success': False,
                                'error': f'Spessore fuori misura: tra {SPESSORE_MIN_MM:g} e {SPESSORE_MAX_MM:g} mm'}), 400
        if mat:
            mat = str(mat).strip().upper()
            from . import lantek as _lt
            noti = {str(m).upper() for m in (_lt.materiali_lantek_noti() or [])}
            if noti and mat not in noti:
                return jsonify({'success': False, 'error': f'Materiale {mat} non presente in Lantek'}), 400
        order = A._ordine_esistente(order_id)
        if not order:
            return A._non_trovato_ordine()
        if not order.preventivo_id_origine:
            return jsonify({'success': False, 'error': "Quest'ordine non ha pezzi con la distinta"}), 404
        art, _percorso = A._articolo_ordine(order, articolo_id)
        if not art:
            return jsonify({'success': False, 'error': "Pezzo non trovato in quest'ordine"}), 404
        from sqlalchemy import text as _text
        from .database import get_session
        session = get_session()
        try:
            campi, val = [], {'i': articolo_id, 'p': order.preventivo_id_origine}
            if sp is not None:
                campi.append('spessore_mm = :sp')
                val['sp'] = sp
            if mat:
                campi.append('materiale = :mat')
                val['mat'] = mat
            n = session.execute(_text(f"UPDATE preventivo_articoli SET {', '.join(campi)} "
                                      'WHERE id = :i AND preventivo_id = :p'), val).rowcount
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
        if not n:
            return jsonify({'success': False, 'error': 'Pezzo non trovato'}), 404
        chi = A._chi_nome()
        scelta = ', '.join(x for x in (f'{sp:g} mm' if sp is not None else '', mat or '') if x)
        A._registra_decisione_laser(order, articolo_id, 'dati_scelti_dal_laser', scelta, chi)
        A._audit('PEZZO_DATI', 'orders', order_id,
                 f"Pezzo {art.get('codice') or articolo_id}: "
                 + (f"spessore {art.get('spessore_mm')} -> {sp:g} mm " if sp is not None else '')
                 + (f"materiale {art.get('materiale')} -> {mat}" if mat else '') + ' (dal laser, guardando il disegno)')
        try:
            from . import lantek as _lt
            _lt._svuota_cache()
        except Exception:
            pass
        try:
            _sposta_dxf_lantek(order, art, _percorso, mat or art.get('materiale'),
                               sp if sp is not None else art.get('spessore_mm'))
        except Exception:
            logger.warning('DXF per Lantek non spostato nella cartella del nuovo spessore', exc_info=True)
        invalida(order_id)
        return jsonify({'success': True, 'spessore_mm': sp, 'materiale': mat}), 200
    except Exception as e:
        logger.exception('dati del pezzo non salvati')
        return jsonify({'success': False, 'error': str(e)[:200]}), 500


# ---------------------------------------------------------------------------
# Assiemi: quanti assiemi e pezzi per assieme (backend/assiemi_ordine.py)
# ---------------------------------------------------------------------------
def _audit_per_assieme(A, order_id, pa: dict) -> None:
    A._audit('PEZZO_PER_ASSIEME', 'orders', order_id,
             f"{pa.get('codice')}: pezzi " + (f"per assieme {pa.get('assieme')}" if pa.get('assieme') else '')
             + f" {pa.get('prima')} -> {pa.get('dopo')}" + (' (confermati: l\'ordine non li diceva)'
                                                         if pa.get('era_da_confermare') else ''))


@bp_laser.route('/api/orders/<order_id>/pezzi/<articolo_id>/per-assieme', methods=['POST'])
@richiede('laser', 'ufficio')
def api_pezzo_per_assieme(order_id, articolo_id):
    """Quanti pezzi servono per ogni assieme (o del pezzo sciolto), scritti dal
    laser guardando il disegno: la quantita' del pezzo diventa questo x
    assiemi, per Lantek e per "Da procurare". Corpo: {"pezzi": 2}.
    Risposta: {success, prima, dopo, quantita (in tutto)}"""
    A = _app()
    from . import assiemi_ordine as _ao
    try:
        order = A._ordine_esistente(order_id)
        if not order:
            return A._non_trovato_ordine()
        corpo = request.get_json(silent=True) or {}
        pa = _ao.imposta_per_assieme(order, articolo_id, corpo.get('pezzi'), A._chi_nome())
        if not pa.get('success'):
            return jsonify({'success': False, 'error': pa.get('error')}), 404 if pa.get('codice_errore') == 'non_trovato' else 400
        _audit_per_assieme(A, order_id, pa)
        _ao.dopo_cambio(order)
        return jsonify({'success': True, 'prima': pa['prima'], 'dopo': pa['dopo'],
                        'quantita': _quantita_distinta(order, articolo_id)}), 200
    except Exception as e:
        logger.exception('pezzi per assieme non salvati')
        return jsonify({'success': False, 'error': str(e)[:200]}), 500


@bp_laser.route('/api/orders/<order_id>/assiemi/<path:codice>/quantita', methods=['POST'])
@richiede('laser', 'ufficio')
def api_assieme_quantita(order_id, codice):
    """Quanti assiemi ordina il cliente (la riga dell'ordine). Tutti i suoi
    pezzi si ricalcolano (pezzi per assieme x assiemi). Corpo: {"quantita": 3}."""
    A = _app()
    from . import assiemi_ordine as _ao
    try:
        order = A._ordine_esistente(order_id)
        if not order:
            return A._non_trovato_ordine()
        corpo = request.get_json(silent=True) or {}
        r = _ao.imposta_qta_assieme(order, codice, corpo.get('quantita'))
        if not r.get('success'):
            return jsonify({'success': False, 'error': r.get('error')}), 404 if r.get('codice_errore') == 'non_trovato' else 400
        A._audit('ASSIEME_QUANTITA', 'orders', order_id, f"Assieme {codice}: quantità {r['prima']} -> {r['dopo']}")
        _ao.dopo_cambio(order)
        return jsonify({'success': True, 'prima': r['prima'], 'dopo': r['dopo']}), 200
    except Exception as e:
        logger.exception("quantità dell'assieme non salvata")
        return jsonify({'success': False, 'error': str(e)[:200]}), 500
