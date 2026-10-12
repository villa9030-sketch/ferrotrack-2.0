"""Fogli con piu' pezzi diversi: dal disegno unico ai pezzi per Lantek.

Stefano (12/10/2026): certi clienti mandano UN disegno con DENTRO piu' pezzi
piani diversi da tagliare (a volte ripetuti). Prima col tasto P ("ci sono piu'
pezzi") il disegno restava fuori da Lantek e lo divideva lui in Lantek.
Adesso P apre "Pezzi del foglio": FerroTrack trova tutti i pezzi del foglio
(preventivi/tipo_disegno.pezzi_del_foglio_da_file, letto SOLO su richiesta:
il classificatore del tipo di disegno resta spento), il laser li controlla
(toglie una vista, ne aggiunge uno cliccandone il bordo, corregge codici e
quantita') e conferma. Ogni pezzo diventa una riga per Lantek col suo codice,
il suo DXF pulito (LANTEK/<lamiera>/<codice>.dxf) e la sua quantita'; il
foglio non parte piu'.

  GET    /api/orders/<id>/pezzi/<pz>/pezzi-foglio   i pezzi proposti (o confermati)
  POST   /api/orders/<id>/pezzi/<pz>/pezzi-foglio   conferma {pezzi: [...]}
  DELETE /api/orders/<id>/pezzi/<pz>/pezzi-foglio   annulla: il disegno torna da controllare

Coordinate in mm, y in su (come il CAD e pick_candidates). Le funzioni di
app.py si importano DENTRO le funzioni (app.py importa questo file).
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import threading

from flask import Blueprint, jsonify, request

from .accesso import richiede

logger = logging.getLogger(__name__)

bp_pezzi_foglio = Blueprint('pezzi_foglio', __name__)

_CACHE: dict = {}
_LOCK = threading.Lock()
_MAX_PEZZI = 60
# un codice e' anche il nome del file per Lantek: niente \ / : * ? " < > |
_RX_CODICE = re.compile(r'^[A-Za-z0-9](?:[A-Za-z0-9._\- ]{0,58}[A-Za-z0-9])?$')


def _app():
    import importlib
    return importlib.import_module(__package__ + '.app')


def _errore(msg, stato=400, codice=None):
    corpo = {'success': False, 'error': msg}
    if codice:
        corpo['codice'] = codice
    return jsonify(corpo), stato


def _cfg() -> dict:
    from .database import ConfigManager
    return (ConfigManager.load_config() or {}).get('dxf_detection', {}) or {}


# ---------------------------------------------------------------------------
# Lettura del foglio e proposte
# ---------------------------------------------------------------------------
def pezzi_letti(percorso: str) -> list:
    """I pezzi del foglio (tipo_disegno.pezzi_del_foglio_da_file), in cache per
    (percorso, data del file)."""
    try:
        mt = os.path.getmtime(percorso)
    except OSError:
        mt = 0
    k = (percorso, mt)
    with _LOCK:
        if k in _CACHE:
            return _CACHE[k]
    from .preventivi.tipo_disegno import pezzi_del_foglio_da_file
    pezzi = pezzi_del_foglio_da_file(percorso, _cfg())
    with _LOCK:
        if len(_CACHE) > 30:
            _CACHE.clear()
        _CACHE[k] = pezzi
    return pezzi


_RX_MATERIALE = re.compile(r'^(S\d{3}|DC0\d|DD1\d|AISI|INOX|FE\d{3}|AL\d|UNI|EN\d|ISO|DIN|RAL|SP\d|PIASTRA|LAMIERA)', re.I)


def _forma(s: str) -> str:
    """'13PA00434-00' -> 'AAAA' per lettere/cifre: 99AA99999-99."""
    return re.sub(r'[A-Z]', 'A', re.sub(r'\d', '9', s.upper()))


def _sembra_codice(s: str, codice_foglio: str) -> bool:
    """Un testo del disegno che sembra il codice di un pezzo: lettere e
    cifre, niente spazi, almeno 2 cifre e una lettera, non un materiale; e
    somiglia al codice del foglio (stesso inizio o stessa forma)."""
    t = s.strip().upper()
    if not 4 <= len(t) <= 40 or not _RX_CODICE.match(t) or ' ' in t:
        return False
    if sum(ch.isdigit() for ch in t) < 2 or not any(ch.isalpha() for ch in t) or _RX_MATERIALE.match(t):
        return False
    cf = (codice_foglio or '').strip().upper()
    if not cf or t == cf:
        return False
    return t[:4] == cf[:4] or _forma(t) == _forma(cf) or _forma(t.split('-')[0]) == _forma(cf.split('-')[0])


def codici_dai_testi(pezzi: list, testi: list, codice_foglio: str) -> dict:
    """{indice del pezzo: codice} per i pezzi che hanno, dentro o subito
    accanto, una scritta che sembra un codice. Ogni scritta va al pezzo piu'
    vicino; ogni pezzo prende la scritta piu' vicina; doppioni scartati."""
    cand = []
    for t in testi or []:
        s = str(t.get('s') or '')
        if not _sembra_codice(s, codice_foglio):
            continue
        try:
            x, y = float(t['x']), float(t['y'])
        except (KeyError, TypeError, ValueError):
            continue
        best = None
        for i, p in enumerate(pezzi):
            x0, y0, x1, y1 = p['bbox']
            dx = max(x0 - x, 0, x - x1)
            dy = max(y0 - y, 0, y - y1)
            d = (dx * dx + dy * dy) ** 0.5
            vicino = 0.25 * max(x1 - x0, y1 - y0) + 15.0
            if d <= vicino and (best is None or d < best[0]):
                best = (d, i)
        if best:
            cand.append((best[0], best[1], s.strip().upper()))
    out, usati = {}, set()
    for d, i, s in sorted(cand):
        if i in out or s in usati:
            continue
        out[i] = s
        usati.add(s)
    # lo stesso codice letto per due pezzi diversi: non e' un codice di pezzo
    return out


def _qta_foglio(order, art: dict) -> int:
    """Quanti fogli chiede l'ordine (come la distinta: quantita' x lotto o x
    assiemi)."""
    from .database import PreventivoManager

    def q(v):
        try:
            n = int(round(float(v)))
            return n if n > 0 else 1
        except (TypeError, ValueError):
            return 1
    prev = PreventivoManager.get(order.preventivo_id_origine, include_children=True) or {}
    cod_ass = art.get('codice_assieme')
    if cod_ass:
        molt = next((q(a.get('qty')) for a in prev.get('assiemi') or [] if a.get('codice_assieme') == cod_ass), 1)
    else:
        molt = q(prev.get('quantita'))
    return q(art.get('quantita')) * molt


def _codici_ordine(order, aid) -> set:
    """I codici delle altre righe dell'ordine (minuscolo), senza il foglio e
    i suoi pezzi."""
    A = _app()
    out = set()
    for r in A._distinta_ordine(order)[0]:
        if str(r.get('articolo_id') or '') == str(aid) or str((r.get('foglio') or {}).get('articolo_id') or '') == str(aid):
            continue
        if r.get('codice'):
            out.add(str(r['codice']).strip().lower())
    return out


def _foglio(order_id, aid):
    """(ordine, pezzo, percorso del DXF) o (None, None, None, errore)."""
    from .api_contorno_ordine import _pezzo_ordine
    order, art, percorso, err = _pezzo_ordine(order_id, aid)
    return order, art, percorso, err


@bp_pezzi_foglio.route('/api/orders/<order_id>/pezzi/<articolo_id>/pezzi-foglio', methods=['GET'])
@richiede('laser', 'ufficio')
def api_pezzi_foglio(order_id, articolo_id):
    """I pezzi del foglio, numerati in ordine di lettura, coi codici e le
    quantita' proposti. Se gia' confermati: quelli confermati.

    {success, confermati, codice_foglio, qta_foglio, pezzi: [{n, codice,
     codice_da: 'disegno'|'proposto'|'confermato', outer_xy, holes_xy, copie,
     qta_per_foglio, qta, misure: [w, h], area_dm2, perimetro_taglio_m,
     n_forature}], altri_codici: [...]}"""
    try:
        order, art, percorso, err = _foglio(order_id, articolo_id)
        if err:
            return err
        qf = _qta_foglio(order, art)
        cod_f = str(art.get('codice') or '').strip()
        altri = sorted(_codici_ordine(order, articolo_id))
        salvati = art.get('pezzi_foglio') if isinstance(art.get('pezzi_foglio'), list) else []
        if salvati and (art.get('decisione_laser') or {}).get('scelta') == 'piu_pezzi_confermati':
            pezzi = [{'n': i, 'codice': p.get('codice'), 'codice_da': 'confermato', 'outer_xy': p.get('outer_xy'),
                      'holes_xy': p.get('holes_xy') or [], 'copie': [], 'qta_per_foglio': p.get('qta_per_foglio') or 1,
                      'qta': p.get('qta') or 1, 'misure': p.get('bbox'), 'area_dm2': p.get('area_dm2'),
                      'perimetro_taglio_m': p.get('perimetro_taglio_m'), 'n_forature': p.get('n_forature')}
                     for i, p in enumerate(salvati, 1)]
            return jsonify({'success': True, 'confermati': True, 'codice_foglio': cod_f, 'qta_foglio': qf,
                            'pezzi': pezzi, 'altri_codici': altri}), 200
        letti = pezzi_letti(percorso)
        testi = []
        try:
            import json as _json
            gj = _json.loads(_app()._get_dxf_geometry_cached(percorso, _cfg()) or '{}')
            testi = gj.get('texts') or []
        except Exception:
            logger.warning('testi del foglio non letti: %s', percorso, exc_info=True)
        dai_testi = codici_dai_testi(letti, testi, cod_f)
        pezzi, usati = [], set(altri)
        for i, p in enumerate(letti):
            cod = dai_testi.get(i)
            da = 'disegno'
            if not cod or cod.lower() in usati:
                k = len(pezzi) + 1
                cod = f'{cod_f}-{k}'
                while cod.lower() in usati:
                    k += 1
                    cod = f'{cod_f}-{k}'
                da = 'proposto'
            usati.add(cod.lower())
            pezzi.append({'n': i + 1, 'codice': cod, 'codice_da': da, 'outer_xy': p['contorno'],
                          'holes_xy': p['fori'], 'copie': p.get('copie') or [], 'qta_per_foglio': p['quantita'],
                          'qta': p['quantita'] * qf, 'misure': [p['bbox_width_mm'], p['bbox_height_mm']],
                          'area_dm2': p['area_dm2'], 'perimetro_taglio_m': p['perimetro_taglio_m'],
                          'n_forature': p['n_forature']})
        return jsonify({'success': True, 'confermati': False, 'codice_foglio': cod_f, 'qta_foglio': qf,
                        'pezzi': pezzi, 'altri_codici': altri}), 200
    except Exception as e:
        logger.exception('pezzi del foglio non letti')
        return _errore('Pezzi del foglio non letti: ' + str(e)[:160], 500)


# ---------------------------------------------------------------------------
# File per Lantek
# ---------------------------------------------------------------------------
def _cartella_lantek(order) -> str:
    A = _app()
    return os.path.join(A.DRAWINGS_FOLDER, order.id, 'LANTEK')


def _sposta_foglio_da_preparare(order, art) -> int:
    """Il DXF pulito del FOGLIO (se c'era) non deve partire: va in
    LANTEK/_DA PREPARARE/<lamiera>/ (da li' torna utile se si annulla).
    Ritorna quanti file ha spostato."""
    A = _app()
    radice = _cartella_lantek(order)
    if not os.path.isdir(radice):
        return 0
    basi = {os.path.splitext(A._nome_base_disegno(os.path.basename(art.get('dxf_filename') or '')))[0].lower(),
            str(art.get('codice') or '').strip().lower()} - {''}
    dest_dir = os.path.join(radice, A.CARTELLA_DA_PREPARARE, A._nome_lamiera(art.get('materiale'), art.get('spessore_mm')))
    n = 0
    for base, dirs, files in os.walk(radice):
        rel = os.path.relpath(base, radice).replace(os.sep, '/')
        if rel.split('/')[0] in (A.CARTELLA_DA_PREPARARE, A.CARTELLA_ORIGINALI):
            dirs[:] = []
            continue
        for f in files:
            if f.lower().endswith('.dxf') and os.path.splitext(f)[0].lower() in basi:
                os.makedirs(dest_dir, exist_ok=True)
                dest = os.path.join(dest_dir, f)
                try:
                    if os.path.exists(dest):
                        os.remove(os.path.join(base, f))
                    else:
                        shutil.move(os.path.join(base, f), dest)
                    n += 1
                except OSError:
                    logger.warning('pulito del foglio non spostato: %s', f, exc_info=True)
    return n


def _togli_file(order, rels) -> int:
    """Toglie i DXF dei pezzi scritti da una conferma precedente (percorsi
    relativi alla cartella dell'ordine, solo dentro LANTEK)."""
    A = _app()
    cartella = os.path.realpath(os.path.join(A.DRAWINGS_FOLDER, order.id, 'LANTEK'))
    n = 0
    for rel in rels or []:
        if not rel:
            continue
        p = os.path.realpath(os.path.join(A.DRAWINGS_FOLDER, order.id, rel))
        if not p.startswith(cartella + os.sep) or not p.lower().endswith('.dxf'):
            continue
        try:
            os.remove(p)
            n += 1
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning('DXF del pezzo non tolto: %s', p, exc_info=True)
    return n


def _punti(lista, minimo):
    from .api_contorno_ordine import _punti as _p
    return _p(lista, minimo)


@bp_pezzi_foglio.route('/api/orders/<order_id>/pezzi/<articolo_id>/pezzi-foglio', methods=['POST'])
@richiede('laser', 'ufficio')
def api_conferma_pezzi_foglio(order_id, articolo_id):
    """Il laser conferma i pezzi del foglio.

    Corpo: {pezzi: [{codice, outer_xy, holes_xy, qta_per_foglio, qta}]}
    (mm). Area, perimetro e fori si ricalcolano qui. Codici unici nell'ordine
    e validi come nome di file. Si scrive un DXF pulito per pezzo in
    LANTEK/<lamiera>/<codice>.dxf (tutti controllati prima di scriverne uno);
    il pulito del foglio va in _DA PREPARARE. Risposta: {success, pezzi:
    [{codice, qta, file}], n_pezzi}"""
    A = _app()
    try:
        from .api_contorno_ordine import _geometria_dal_contorno, _dentro_al_disegno, _pulito_dal_contorno_scelto
        from .preventivi import dxf_cleanup as _dxc
        corpo = request.get_json(silent=True) or {}
        lista = corpo.get('pezzi')
        if not isinstance(lista, list) or not lista:
            return _errore('Nessun pezzo da confermare')
        if len(lista) > _MAX_PEZZI:
            return _errore(f'Troppi pezzi (al massimo {_MAX_PEZZI})')
        order, art, percorso, err = _foglio(order_id, articolo_id)
        if err:
            return err
        altri = _codici_ordine(order, articolo_id)
        pezzi, visti = [], set()
        for i, p in enumerate(lista, 1):
            p = p if isinstance(p, dict) else {}
            cod = str(p.get('codice') or '').strip()
            if not _RX_CODICE.match(cod):
                return _errore(f'Pezzo {i}: codice "{cod}" non valido (lettere, cifre, - . _; niente \\ / : * ? " < > |)',
                               codice='codice_non_valido')
            if cod.lower() in visti:
                return _errore(f'Il codice {cod} e\' usato per due pezzi: ogni pezzo vuole il suo', codice='codice_doppio')
            if cod.lower() in altri:
                return _errore(f'Il codice {cod} c\'e\' gia\' in quest\'ordine: scegline un altro', codice='codice_doppio')
            visti.add(cod.lower())
            try:
                esterno = _punti(p.get('outer_xy'), 3)
                fori = [_punti(h, 3) for h in (p.get('holes_xy') or [])[:5000]]
                qpf = int(p.get('qta_per_foglio') or 1)
                qta = int(p.get('qta') or 0)
            except (TypeError, ValueError):
                return _errore(f'Pezzo {cod}: contorno o quantita\' non validi')
            if not (1 <= qpf <= 9999 and 1 <= qta <= 999999):
                return _errore(f'Pezzo {cod}: quantita\' fuori misura')
            geo = _geometria_dal_contorno(esterno, fori)
            if not geo or not geo.get('area_dm2'):
                return _errore(f'Pezzo {cod}: il contorno non e\' chiuso o si incrocia')
            xs = [q[0] for q in esterno]
            ys = [q[1] for q in esterno]
            if not _dentro_al_disegno(percorso, (min(xs), min(ys), max(xs), max(ys))):
                return _errore(f'Pezzo {cod}: il contorno non sta in questo disegno')
            pezzi.append({'codice': cod, 'outer_xy': esterno, 'holes_xy': fori, 'qta_per_foglio': qpf, 'qta': qta,
                          'area_dm2': round(geo['area_dm2'], 4), 'perimetro_taglio_m': round(geo['perimetro_taglio_m'], 4),
                          'n_forature': int(geo['n_forature']),
                          'bbox': [round(geo['bbox_width_mm'], 2), round(geo['bbox_height_mm'], 2)]})

        # 1. tutti i DXF in una cartella temporanea, controllati come il pulito
        lamiera = A._nome_lamiera(art.get('materiale'), art.get('spessore_mm'))
        tmp = tempfile.mkdtemp(prefix='ft_pezzi_foglio_')
        try:
            for p in pezzi:
                dest = os.path.join(tmp, p['codice'] + '.dxf')
                r = _pulito_dal_contorno_scelto(dest, p['outer_xy'], p['holes_xy'])
                v = (_dxc.verifica_lantek(dest, {'bbox_w_mm': p['bbox'][0], 'bbox_h_mm': p['bbox'][1],
                                                 'area_dm2': p['area_dm2']}) if r.get('success') else {})
                if v.get('stato') != 'pronto':
                    motivo = r.get('error') if not r.get('success') else '; '.join(v.get('motivi') or ['?'])
                    return _errore(f"Pezzo {p['codice']}: DXF per Lantek non pronto ({motivo})", codice='dxf_non_pronto')
            # 2. al loro posto: via i DXF della conferma di prima, dentro i nuovi
            prima = art.get('pezzi_foglio') if isinstance(art.get('pezzi_foglio'), list) else []
            _togli_file(order, [x.get('file') for x in prima if isinstance(x, dict)])
            dest_dir = os.path.join(_cartella_lantek(order), lamiera)
            os.makedirs(dest_dir, exist_ok=True)
            for p in pezzi:
                shutil.copyfile(os.path.join(tmp, p['codice'] + '.dxf'), os.path.join(dest_dir, p['codice'] + '.dxf'))
                p['file'] = '/'.join(['LANTEK', lamiera, p['codice'] + '.dxf'])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        try:
            spostati = _sposta_foglio_da_preparare(order, art)
        except OSError:
            # non blocca la conferma: il foglio non e' piu' nella distinta,
            # il suo pulito resta dov'e' ma non parte
            logger.warning('pulito del foglio non spostato in _DA PREPARARE', exc_info=True)
            spostati = 0

        chi = A._chi_nome()
        esito = A.PreventivoManager.pezzi_foglio_articolo(order.preventivo_id_origine, articolo_id, pezzi, chi)
        if not esito.get('success'):
            _togli_file(order, [p['file'] for p in pezzi])
            return _errore(esito.get('error') or 'Pezzi non salvati', 500)

        # registro degli esempi del motore: cosa aveva letto e cosa ha confermato il laser
        try:
            from .preventivi.registro_esempi import registra
            try:
                letti = pezzi_letti(percorso)
            except Exception:
                letti = []
            registra('pezzi_del_foglio', percorso, codice=art.get('codice'), chi=chi,
                     motore={'pezzi': [{'bbox': p['bbox'], 'quantita': p['quantita'], 'area_dm2': p['area_dm2']}
                                       for p in letti]},
                     decisione={'pezzi': [{k: p[k] for k in ('codice', 'outer_xy', 'holes_xy', 'qta_per_foglio', 'qta',
                                                             'area_dm2', 'perimetro_taglio_m', 'n_forature', 'bbox')}
                                          for p in pezzi]},
                     contesto={'ordine': order.id, 'articolo_id': articolo_id, 'pagina': 'laser'})
        except Exception:
            logger.warning('pezzi del foglio non registrati negli esempi', exc_info=True)
        A._audit('PEZZI_DEL_FOGLIO', 'orders', order_id,
                 f"Foglio {art.get('codice') or articolo_id}: {len(pezzi)} pezzi confermati ("
                 + ', '.join(f"{p['codice']} x{p['qta']}" for p in pezzi) + ')'
                 + (f'; pulito del foglio in _DA PREPARARE ({spostati})' if spostati else ''))
        _dopo(order_id)
        return jsonify({'success': True, 'n_pezzi': len(pezzi),
                        'pezzi': [{'codice': p['codice'], 'qta': p['qta'], 'file': p['file']} for p in pezzi]}), 200
    except Exception as e:
        logger.exception('pezzi del foglio non confermati')
        return _errore('Pezzi non confermati per un errore imprevisto: ' + str(e)[:160], 500)


@bp_pezzi_foglio.route('/api/orders/<order_id>/pezzi/<articolo_id>/pezzi-foglio', methods=['DELETE'])
@richiede('laser', 'ufficio')
def api_annulla_pezzi_foglio(order_id, articolo_id):
    """Annulla i pezzi del foglio: via i loro DXF, il foglio torna come
    prima (da controllare)."""
    A = _app()
    try:
        order = A._ordine_esistente(order_id)
        if not order:
            return A._non_trovato_ordine()
        if not order.preventivo_id_origine:
            return _errore("Quest'ordine non ha pezzi con la distinta", 404)
        art, percorso = A._articolo_ordine(order, articolo_id)
        if not art:
            return _errore("Pezzo non trovato in quest'ordine", 404)
        prima = art.get('pezzi_foglio') if isinstance(art.get('pezzi_foglio'), list) else []
        if not prima:
            return _errore('Questo disegno non ha pezzi del foglio confermati', 409, 'niente_da_annullare')
        chi = A._chi_nome()
        esito = A.PreventivoManager.pezzi_foglio_articolo(order.preventivo_id_origine, articolo_id, None, chi)
        if not esito.get('success'):
            return _errore(esito.get('error') or 'Non annullato', 500)
        tolti = _togli_file(order, [x.get('file') for x in prima if isinstance(x, dict)])
        try:
            from .preventivi.registro_esempi import registra
            registra('pezzi_del_foglio_annullati', percorso, codice=art.get('codice'), chi=chi,
                     decisione={'codici': [x.get('codice') for x in prima]},
                     contesto={'ordine': order.id, 'articolo_id': articolo_id, 'pagina': 'laser'})
        except Exception:
            logger.warning('annullamento non registrato negli esempi', exc_info=True)
        A._audit('PEZZI_DEL_FOGLIO', 'orders', order_id,
                 f"Foglio {art.get('codice') or articolo_id}: pezzi annullati ({', '.join(str(x.get('codice')) for x in prima)}), "
                 f"{tolti} DXF tolti: di nuovo da controllare")
        _dopo(order_id)
        return jsonify({'success': True, 'tolti': tolti, 'codici': [x.get('codice') for x in prima]}), 200
    except Exception as e:
        logger.exception('pezzi del foglio non annullati')
        return _errore('Non annullato: ' + str(e)[:160], 500)


def _dopo(order_id) -> None:
    """La pagina laser e Lantek rileggono l'ordine."""
    A = _app()
    A._invalida_analisi_laser(order_id)
    try:
        from . import lantek as _lt
        _lt._svuota_cache()
    except Exception:
        pass
