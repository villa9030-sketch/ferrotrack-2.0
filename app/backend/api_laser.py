"""Pagina laser (frontend/laser.html): cosa serve per lavorare un ordine.

Stefano (09/10/2026): la pagina del laser era "caotica" e "saltava da una
schermata all'altra". Qui sta la logica che la pagina nuova mostra, in un
posto solo, cosi' l'elenco degli ordini e l'ordine aperto dicono sempre la
stessa cosa:

  GET  /api/laser/ordini                      un riassunto per ogni ordine del laser
  GET  /api/orders/<id>/laser                 l'ordine aperto: pezzi, Lantek, blocchi
  GET  /api/orders/<id>/pezzi/<pz>/lettura    il foglio intero del disegno, col
                                              contorno del motore, e i suoi dubbi
  GET  /api/orders/<id>/pezzi/<pz>/foglio.svg lo stesso foglio come immagine
  POST /api/orders/<id>/pezzi/<pz>/giusto     "si', e' il pezzo giusto"
  POST /api/orders/<id>/pezzi/<pz>/decisione  va sviluppato / piu' pezzi / non da laser

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
        return 'Importalo dal MES scegliendo il materiale, poi premi Ricontrolla.'
    if 'spessore' in m:
        return 'Importalo dal MES indicando lo spessore, poi premi Ricontrolla.'
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
    lam = [r for r in righe_dist if r.get('tipo') == 'lamiera' and r.get('codice')]
    tutte_lam = lam + [x['_riga'] for x in esclusi if x.get('_riga')]
    altri = [r for r in righe_dist if r.get('tipo') != 'lamiera']

    q, lt = None, {'disponibile': False, 'errore': None}
    nuovi_si, nuovi_no, procesos = [], [], False
    pdf = None
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
        assieme = bool(lt_stato == 'nuovo' and not r.get('disegno') and _RX_ASSIEME.search(cod))
        blocco = None
        if lt_stato == 'nuovo' and not escluso and not assieme and cod.lower() not in da_abbinare                 and not da_controllare and stato in ('smistare', 'coda'):
            if not procesos and lt.get('disponibile'):
                blocco = {'motivo': 'su questo PC non posso creare pezzi nuovi in Lantek',
                          'cosa': _cosa_fare('questo pc', cod)}
            elif no and not no.get('verifica'):
                blocco = {'motivo': no['motivo'], 'cosa': _cosa_fare(no['motivo'], cod)}
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
                'misure': [r.get('bbox_w_mm'), r.get('bbox_h_mm')] if r.get('bbox_w_mm') else None,
                'lantek': lt_stato, 'in_produzione': (x or {}).get('in_produzione') or 0,
                'gia_fatti': (x or {}).get('gia_fatti') or 0,
                'crea': cod_lt.lower() in si_cod,
                'motore': mt.get('stato'), 'decisione': dec, 'perche': perche[:3],
                'da_controllare': da_controllare, 'blocco': blocco,
                'escluso': A._ESCLUSE_LASER.get(dec) if escluso else None,
                'da_abbinare': cod.lower() in da_abbinare, 'assieme_senza_disegno': assieme,
                'avvisi': avvisi,
            })

    invio_codici = [r['codice'] for r in _lt.righe_per_xml(q['righe'])] if q else []
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
        'lantek': {'letto': q is not None, 'disponibile': bool(lt.get('disponibile')),
                   'errore': lt.get('errore'), 'crea_pezzi': bool(procesos),
                   'invio_automatico': bool(q is not None and _lt.xmlimporter_disponibile())},
        'n_invio': len(invio_codici) + len(nuovi_si),
        'n_in_produzione': sum(1 for r in (q or {}).get('righe') or [] if r.get('in_produzione')),
    }
    out['pronto'] = bool(out['lantek']['disponibile'] and not n_controllare and not bloccati
                         and not da_abbinare_n and out['n_invio'])
    # niente da mandare e niente in sospeso: i codici hanno gia' l'ordine di
    # produzione in Lantek (o sono tenuti fuori)
    out['tutto_in_lantek'] = bool(out['lantek']['disponibile'] and not out['n_invio'] and not n_controllare
                                  and not bloccati and not da_abbinare_n and n_codici)
    if completa:
        righe_q = {r['codice']: r for r in (q or {}).get('righe') or []}
        out.update({
            'pezzi': pezzi,
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
        })
    return out


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
        for o in ordini:
            try:
                out[o.id] = _analisi_in_cache(o, forza=fresco or bool(solo))
            except Exception as e:
                logger.exception('analisi laser di %s fallita', o.id)
                out[o.id] = {'id': o.id, 'stato': stato_laser(o), 'errore': str(e)[:200]}
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
                                                          'pdf', 'commessa', 'cliente_lantek', 'consegna_lantek')}
        with _LOCK:
            _CACHE[order.id] = (time.time(), ridotto)
        return jsonify({'success': True, **a}), 200
    except Exception as e:
        logger.exception('ordine laser fallito')
        return jsonify({'success': False, 'error': str(e)}), 500


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
        mt = A._stato_motore(art)
        perche = list(mt.get('motivi') or [])
        for b in A.motivi_brevi(f['avvisi']):
            if b not in perche:
                perche.append(b)
        return jsonify({'success': True, 'codice': art.get('codice'), 'svg': f['svg'], 'vista': f['vista'],
                        'ha_contorno': bool(f['contorno']), 'perche': perche[:3],
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


@bp_laser.route('/api/orders/<order_id>/pezzi/<articolo_id>/decisione', methods=['POST'])
@richiede('laser', 'ufficio')
def api_pezzo_decisione(order_id, articolo_id):
    """Decisione del laser su un pezzo che non si manda a Lantek cosi' com'e'.

    Corpo: {"scelta": "sviluppo" | "piu_pezzi" | "non_laser"} oppure
    {"scelta": null} per toglierla. Il pezzo resta fuori da "Manda a Lantek"
    col suo motivo; finisce anche nel registro degli esempi del motore."""
    A = _app()
    try:
        corpo = request.get_json(silent=True) or {}
        scelta = corpo.get('scelta')
        if scelta is not None and scelta not in _SCELTE:
            return jsonify({'success': False, 'error': 'Scelta non valida (sviluppo, piu_pezzi, non_laser)'}), 400
        order = A._ordine_esistente(order_id)
        if not order:
            return A._non_trovato_ordine()
        if not order.preventivo_id_origine:
            return jsonify({'success': False, 'error': "Quest'ordine non ha pezzi con la distinta"}), 404
        art, _percorso = A._articolo_ordine(order, articolo_id)
        if not art:
            return jsonify({'success': False, 'error': "Pezzo non trovato in quest'ordine"}), 404
        chi = A._chi_nome()
        esito = A.PreventivoManager.decisione_laser_articolo(order.preventivo_id_origine, articolo_id, scelta, chi)
        if not esito.get('success'):
            return jsonify({'success': False, 'error': esito.get('error') or 'non riuscito'}), 400
        A._registra_decisione_laser(order, articolo_id, 'decisione_laser' if scelta else 'decisione_annullata',
                                    scelta, chi)
        A._audit('PEZZO_DECISIONE', 'orders', order_id,
                 f"Pezzo {art.get('codice') or articolo_id}: "
                 + (_SCELTE[scelta] + ' (fuori da Lantek)' if scelta else 'decisione tolta'))
        invalida(order_id)
        return jsonify({'success': True, 'scelta': scelta, 'prima': esito.get('prima')}), 200
    except Exception as e:
        logger.exception('decisione sul pezzo fallita')
        return jsonify({'success': False, 'error': str(e)[:200]}), 500
