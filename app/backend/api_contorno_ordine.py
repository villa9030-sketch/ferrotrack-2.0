"""Contorno di un pezzo scelto a mano nel CAD, sul disegno DELL'ORDINE.

Un ordine caricato dall'ufficio ("pacchetto") ha i pezzi in un preventivo
nascosto gia' accettato: i disegni non stanno piu' nella cartella del
preventivo ma in uploads/drawings/<ordine>/. Quando il riconoscimento ha
preso il contorno sbagliato (es. la cornice del foglio), dal laser si apre lo
stesso CAD (frontend/dxf-editor.html?ordine=...) e si sceglie quello giusto.

Endpoint (laser e ufficio):
  GET  /api/orders/<ordine>/pezzi/<pezzo>/cad/geometry-json
  POST /api/orders/<ordine>/pezzi/<pezzo>/cad/pick-candidates   {x, y}
  POST /api/orders/<ordine>/pezzi/<pezzo>/cad/trace-waypoints   {points}
  POST /api/orders/<ordine>/pezzi/<pezzo>/contorno              {outer_xy, holes_xy}

Le prime tre sono le stesse letture del CAD del preventivo, sul file
dell'ordine. L'ultima ricalcola qui area/perimetro/fori dal contorno scelto,
aggiorna il pezzo (solo quella riga: il preventivo accettato non si
riscrive) e rifa' il DXF pulito per Lantek.

Le funzioni di app.py si importano DENTRO le rotte: app.py importa questo
file per registrarlo, e al contrario si avrebbe un import circolare.
"""
import json
import logging
import math
import os
import shutil
import tempfile
from datetime import datetime

from flask import Blueprint, jsonify, request, Response

from .accesso import richiede

logger = logging.getLogger(__name__)

bp_contorno_ordine = Blueprint('contorno_ordine', __name__)

_MAX_PUNTI = 200000        # punti di un contorno (i cerchi arrivano gia' spezzati)
_MAX_FORI = 5000
_MAX_WAYPOINT = 1000


def _errore(msg, stato=400, codice=None):
    corpo = {'success': False, 'error': msg}
    if codice:
        corpo['codice'] = codice
    return jsonify(corpo), stato


def _pezzo_ordine(order_id, articolo_id):
    """(ordine, pezzo, percorso del DXF dell'ordine, None) oppure
    (None, None, None, risposta d'errore)."""
    from .app import _ordine_esistente, _articolo_ordine
    order = _ordine_esistente(order_id)
    if not order:
        return None, None, None, _errore('Ordine non trovato', 404, 'non_trovato')
    if not order.preventivo_id_origine:
        return None, None, None, _errore("Quest'ordine non ha pezzi col disegno", 404)
    art, percorso = _articolo_ordine(order, articolo_id)
    if not art:
        return None, None, None, _errore("Pezzo non trovato in quest'ordine", 404)
    if not percorso:
        return None, None, None, _errore("Il disegno del pezzo non c'è fra i disegni dell'ordine", 404)
    if not percorso.lower().endswith('.dxf'):
        return None, None, None, _errore('Il disegno del pezzo non è un DXF: il contorno si sceglie solo sui DXF')
    return order, art, percorso, None


def _cfg_riconoscimento():
    from .app import ConfigManager
    return (ConfigManager.load_config() or {}).get('dxf_detection', {})


def _num(v):
    x = float(v)
    if not math.isfinite(x):
        raise ValueError('numero non valido')
    return x


def _punti(lista, minimo):
    """[[x, y], ...] in float, o ValueError."""
    if not isinstance(lista, list) or len(lista) < minimo or len(lista) > _MAX_PUNTI:
        raise ValueError('punti mancanti')
    out = []
    for p in lista:
        if not isinstance(p, (list, tuple)) or len(p) < 2:
            raise ValueError('punto non valido')
        out.append([_num(p[0]), _num(p[1])])
    return out


# ── Letture per il CAD (come quelle del preventivo) ─────────────────────────

@bp_contorno_ordine.route('/api/orders/<order_id>/pezzi/<articolo_id>/cad/geometry-json', methods=['GET'])
@richiede('laser', 'ufficio')
def cad_geometry_json(order_id, articolo_id):
    """Geometria del DXF del pezzo come polilinee in mm, per il CAD."""
    try:
        from .app import _get_dxf_geometry_cached
        _o, _a, percorso, err = _pezzo_ordine(order_id, articolo_id)
        if err:
            corpo, stato = err
            return jsonify({'error': corpo.get_json().get('error')}), stato
        payload = _get_dxf_geometry_cached(percorso, _cfg_riconoscimento())
        resp = Response(payload, mimetype='application/json')
        resp.headers['Cache-Control'] = 'private, max-age=3600'
        return resp
    except Exception:
        logger.exception('geometry-json del disegno dell\'ordine fallito')
        return jsonify({'error': 'Il disegno non si legge: prova a riaprirlo o avvisa Stefano'}), 500


@bp_contorno_ordine.route('/api/orders/<order_id>/pezzi/<articolo_id>/cad/pick-candidates', methods=['POST'])
@richiede('laser', 'ufficio')
def cad_pick_candidates(order_id, articolo_id):
    """Dal clic, i contorni chiusi possibili (il piu' probabile per primo)."""
    try:
        data = request.get_json(silent=True) or {}
        try:
            x, y = _num(data.get('x')), _num(data.get('y'))
        except (TypeError, ValueError):
            return _errore('Servono le coordinate del clic (x, y in mm)')
        _o, _a, percorso, err = _pezzo_ordine(order_id, articolo_id)
        if err:
            return err
        from .preventivi.pick_part import pick_candidates
        return jsonify(pick_candidates(percorso, x, y, _cfg_riconoscimento())), 200
    except Exception:
        logger.exception('pick-candidates sul disegno dell\'ordine fallito')
        return _errore('Contorno non trovato per un errore nel leggere il disegno: riprova', 500)


@bp_contorno_ordine.route('/api/orders/<order_id>/pezzi/<articolo_id>/cad/trace-waypoints', methods=['POST'])
@richiede('laser', 'ufficio')
def cad_trace_waypoints(order_id, articolo_id):
    """Contorno guidato: segue il disegno fra i punti cliccati e chiude."""
    try:
        data = request.get_json(silent=True) or {}
        try:
            punti = _punti(data.get('points'), 2)
        except (TypeError, ValueError):
            return _errore('Servono almeno 2 punti lungo il contorno')
        if len(punti) > _MAX_WAYPOINT:
            return _errore('Troppi punti: ne bastano pochi lungo il contorno')
        _o, _a, percorso, err = _pezzo_ordine(order_id, articolo_id)
        if err:
            return err
        from .preventivi.pick_part import trace_contour_waypoints
        return jsonify(trace_contour_waypoints(percorso, punti, _cfg_riconoscimento())), 200
    except Exception:
        logger.exception('trace-waypoints sul disegno dell\'ordine fallito')
        return _errore('Contorno guidato non riuscito per un errore nel leggere il disegno: riprova', 500)


# ── Conferma del contorno ───────────────────────────────────────────────────

def _geometria_dal_contorno(outer_xy, holes_xy):
    """Area netta, perimetro di taglio, inneschi e ingombro dal contorno scelto,
    con lo stesso calcolo del CAD (pick_part). None se il contorno non e' un
    poligono valido."""
    from shapely.geometry import Polygon
    from .preventivi.pick_part import _geometry_from_outer
    grezzo = Polygon(outer_xy)
    outer = grezzo.buffer(0)
    if outer.is_empty or outer.geom_type != 'Polygon' or outer.area <= 0:
        return None
    # Un contorno che si incrocia (a farfalla) "riparato" da buffer(0) perde un
    # pezzo: l'area non torna con quella dei punti mandati -> non valido.
    # Le piccole imperfezioni dei contorni veri (punti doppi) restano ammesse.
    if not grezzo.is_valid and abs(outer.area - abs(grezzo.area)) > 0.01 * outer.area:
        return None
    fori = []
    for h in holes_xy:
        fp = Polygon(h).buffer(0)
        if not fp.is_empty and fp.geom_type == 'Polygon' and fp.area > 0:
            fori.append(fp)
    geo = _geometry_from_outer(outer, fori)
    return geo if geo.get('success') else None


def _dentro_al_disegno(percorso, geo_bounds) -> bool:
    """Il contorno sta nel foglio di questo DXF? (contro un contorno preso da
    un altro disegno)."""
    try:
        from .app import _get_dxf_geometry_cached
        ext = (json.loads(_get_dxf_geometry_cached(percorso, _cfg_riconoscimento())) or {}).get('extents')
        if not ext or len(ext) < 4:
            return True
        tol = 1.0 + 0.01 * max(ext[2] - ext[0], ext[3] - ext[1])
        x1, y1, x2, y2 = geo_bounds
        return (x1 >= ext[0] - tol and y1 >= ext[1] - tol and x2 <= ext[2] + tol and y2 <= ext[3] + tol)
    except Exception:
        logger.warning('estensione del disegno non letta per %s', percorso, exc_info=True)
        return True


def _aggiorna_pezzo(preventivo_id, articolo_id, geo, chi, scelta='scelto') -> dict:
    """Scrive la geometria scelta a mano su UNA riga del pezzo (colonne +
    campi extra), come conferma_controllo_articolo: il preventivo accettato
    non si riscrive (replace_articoli e' bloccato, giustamente)."""
    from sqlalchemy import text as _text
    from .database import get_session, PreventivoManager
    session = get_session()
    try:
        if not PreventivoManager._colonna_extra_pronta(session):
            return {'error': 'Archivio non pronto (campi extra del pezzo)'}
        riga = session.execute(_text(
            'SELECT extra_campi FROM preventivo_articoli WHERE id = :i AND preventivo_id = :p'),
            {'i': articolo_id, 'p': preventivo_id}).fetchone()
        if riga is None:
            return {'error': 'Pezzo non trovato'}
        try:
            extra = json.loads(riga[0] or '{}') or {}
        except (TypeError, ValueError):
            extra = {}
        quando = datetime.utcnow().isoformat(timespec='seconds')
        motivo = (f"contorno del motore confermato ({chi or 'operatore'})" if scelta == 'giusto'
                  else f"contorno scelto a mano ({chi or 'operatore'})")
        extra['bbox_w_mm'] = geo['bbox_width_mm']
        extra['bbox_h_mm'] = geo['bbox_height_mm']
        # Il contorno ora e' quello giusto: non serve piu' rivederlo
        extra['dxf_needs_verify'] = False
        ev = extra.get('esito_verifica') if isinstance(extra.get('esito_verifica'), dict) else {}
        ev['motivi'] = (list(ev.get('motivi') or []) + [motivo])[-8:]
        ev['stato'] = 'confermato'
        ev['quando'] = quando
        ev['fonti'] = list(dict.fromkeys(list(ev.get('fonti') or []) + ['cad']))[:6]
        extra['esito_verifica'] = ev
        ca = extra.get('contorno_auto')
        if isinstance(ca, dict):
            ca['stato'] = 'confermato'
        ab = extra.get('abbinamento')
        if isinstance(ab, dict):
            ab['confermato'] = True
        # decisione del laser (pagina laser): il contorno ora e' deciso
        extra['decisione_laser'] = {'scelta': scelta, 'chi': chi or None, 'quando': quando}
        riga_n = extra.get('riga')
        extra = PreventivoManager._extra_articolo(extra)
        if isinstance(riga_n, int):
            extra['riga'] = riga_n
        session.execute(_text(
            'UPDATE preventivo_articoli SET area_dm2 = :a, perimetro_taglio_m = :pm, n_forature = :nf, '
            "geometry_source = 'manual-click', geometria_manuale_confermata = 1, area_stimata_piega = 0, "
            'extra_campi = :j WHERE id = :i AND preventivo_id = :p'),
            {'a': geo['area_dm2'], 'pm': geo['perimetro_taglio_m'], 'nf': int(geo['n_forature']),
             'j': json.dumps(extra, ensure_ascii=False), 'i': articolo_id, 'p': preventivo_id})
        session.commit()
        return {'success': True}
    except Exception as e:
        session.rollback()
        logger.exception('pezzo dell\'ordine non aggiornato')
        return {'error': f'Pezzo non salvato: {e}'}
    finally:
        session.close()


def _togli_da_preparare(cartella_lantek, nome) -> int:
    """Toglie da LANTEK/_DA PREPARARE/<lamiera>/ le copie di quel disegno
    (ora c'e' il pulito pronto). Le cartelle rimaste vuote si tolgono."""
    from .app import CARTELLA_DA_PREPARARE
    radice = os.path.join(cartella_lantek, CARTELLA_DA_PREPARARE)
    tolti = 0
    if not os.path.isdir(radice):
        return 0
    for base, _dirs, files in os.walk(radice, topdown=False):
        for f in files:
            if f.lower() == nome.lower():
                try:
                    os.remove(os.path.join(base, f))
                    tolti += 1
                except OSError:
                    logger.warning('copia da preparare non tolta: %s', os.path.join(base, f), exc_info=True)
        try:
            if not os.listdir(base):
                os.rmdir(base)
        except OSError:
            pass
    return tolti


def _pulito_per_lantek(order, art, percorso, outer_xy, holes_xy, geo) -> dict:
    """Rifa' il DXF pulito per Lantek dal contorno scelto, lo controlla e, se e'
    pronto, lo mette in uploads/drawings/<ordine>/LANTEK/<lamiera>/ (dove lo
    cercano zip, cartella condivisa e "Manda a Lantek"), togliendo la copia da
    preparare. Se non e' pronto non si tocca niente.

    {'pronto': bool, 'motivo': str, 'file': percorso relativo o None}"""
    from .app import DRAWINGS_FOLDER, _nome_lamiera, _nome_base_disegno
    from .preventivi import dxf_cleanup as _dxc
    nome = _nome_base_disegno(os.path.basename(percorso))
    tmp = tempfile.mkdtemp(prefix='ft_contorno_')
    try:
        pulito = os.path.join(tmp, nome)
        rp = _dxc.pulito_da_contorno(percorso, pulito, outer_xy, holes_xy, _cfg_riconoscimento())
        if not rp.get('success'):
            return {'pronto': False, 'file': None,
                    'motivo': 'DXF pulito non creato (' + str(rp.get('error') or 'motivo sconosciuto')
                              + '): il pezzo resta da preparare in Lantek'}
        v = _dxc.verifica_lantek(pulito, {'bbox_w_mm': geo['bbox_width_mm'], 'bbox_h_mm': geo['bbox_height_mm'],
                                          'area_dm2': geo['area_dm2']})
        if v.get('stato') != 'pronto':
            return {'pronto': False, 'file': None,
                    'motivo': 'DXF pulito da guardare (' + '; '.join(v.get('motivi') or ['?'])
                              + '): il pezzo resta da preparare in Lantek'}
        cartella_lantek = os.path.join(DRAWINGS_FOLDER, order.id, 'LANTEK')
        lamiera = _nome_lamiera(art.get('materiale'), art.get('spessore_mm'))
        dest_dir = os.path.join(cartella_lantek, lamiera)
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, nome)
        shutil.copyfile(pulito, dest)
        tolti = _togli_da_preparare(cartella_lantek, nome)
        rel = '/'.join(['LANTEK', lamiera, nome])
        return {'pronto': True, 'file': rel,
                'motivo': f'pronto per Lantek in {rel}' + (' (tolto da _DA PREPARARE)' if tolti else '')}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def salva_contorno(order_id, articolo_id, outer_xy, holes_xy, dal_motore=False, pagina='cad'):
    """Salva il contorno di un pezzo dell'ordine: ricalcola area/perimetro/fori,
    aggiorna la riga del pezzo, rifa' il DXF pulito per Lantek, registra
    l'esempio e l'audit. outer_xy/holes_xy in mm, gia' validati (_punti).

    dal_motore=True: il laser conferma il contorno proposto dal motore (pagina
    laser, "Si', e' il pezzo giusto" su un pezzo senza DXF pulito): stesso
    salvataggio, ma nel registro e' una conferma, non una correzione.

    Ritorna (corpo, stato_http)."""
    from .app import _audit, _chi_nome, _invalida_analisi_laser
    order, art, percorso, err = _pezzo_ordine(order_id, articolo_id)
    if err:
        corpo, stato = err
        return corpo.get_json(), stato
    geo = _geometria_dal_contorno(outer_xy, holes_xy)
    if not geo or not geo.get('area_dm2') or geo['area_dm2'] <= 0:
        return {'success': False, 'error': 'Il contorno scelto non è chiuso o si incrocia: scegline un altro'}, 400
    xs = [p[0] for p in outer_xy]
    ys = [p[1] for p in outer_xy]
    if not _dentro_al_disegno(percorso, (min(xs), min(ys), max(xs), max(ys))):
        return {'success': False,
                'error': 'Il contorno non sta nel disegno di questo pezzo: riapri il CAD dal pezzo giusto'}, 400

    chi = _chi_nome()
    nome = os.path.basename(percorso)
    prima = (art.get('area_dm2'), art.get('perimetro_taglio_m'), art.get('n_forature'))
    esito = _aggiorna_pezzo(order.preventivo_id_origine, articolo_id, geo, chi,
                            scelta='giusto' if dal_motore else 'scelto')
    if not esito.get('success'):
        return {'success': False, 'error': esito.get('error') or 'Pezzo non salvato'}, 500

    try:
        lt = _pulito_per_lantek(order, art, percorso, outer_xy, holes_xy, geo)
    except Exception as e:
        logger.exception('pulito per Lantek dal contorno non preparato')
        lt = {'pronto': False, 'file': None,
              'motivo': f'DXF pulito non preparato ({e}): il pezzo resta da preparare in Lantek'}

    from .preventivi.registro_esempi import registra
    registra('contorno_confermato' if dal_motore else 'contorno_scelto_a_mano', percorso,
             codice=art.get('codice'), chi=chi,
             motore={'area_dm2': prima[0], 'perimetro_taglio_m': prima[1], 'n_forature': prima[2],
                     'spessore_mm': art.get('spessore_mm'), 'materiale': art.get('materiale')},
             decisione={'area_dm2': geo['area_dm2'], 'perimetro_taglio_m': geo['perimetro_taglio_m'],
                        'n_forature': geo['n_forature'], 'bbox': [geo['bbox_width_mm'], geo['bbox_height_mm']],
                        'outer_xy': outer_xy, 'holes_xy': holes_xy,
                        'scelta': 'giusto' if dal_motore else 'scelto'},
             contesto={'ordine': order_id, 'articolo_id': articolo_id, 'pagina': pagina or 'cad'})
    _audit('PEZZO_CONTORNO', 'orders', order_id,
           f"Pezzo {art.get('codice') or articolo_id} ({nome}): "
           f"{'contorno del motore confermato' if dal_motore else 'contorno scelto a mano'}. "
           f"Area {prima[0]} -> {geo['area_dm2']:.4f} dm2, perimetro {prima[1]} -> "
           f"{geo['perimetro_taglio_m']:.3f} m, inneschi {prima[2]} -> {geo['n_forature']}. "
           f"Lantek: {lt['motivo']}")
    _invalida_analisi_laser(order_id)
    return {'success': True,
            'area_dm2': round(geo['area_dm2'], 4),
            'perimetro_taglio_m': round(geo['perimetro_taglio_m'], 4),
            'n_forature': int(geo['n_forature']),
            'bbox_w_mm': round(geo['bbox_width_mm'], 2),
            'bbox_h_mm': round(geo['bbox_height_mm'], 2),
            'pronto_lantek': bool(lt['pronto']),
            'motivo': lt['motivo'],
            'file_lantek': lt.get('file')}, 200


@bp_contorno_ordine.route('/api/orders/<order_id>/pezzi/<articolo_id>/contorno', methods=['POST'])
@richiede('laser', 'ufficio')
def contorno_pezzo(order_id, articolo_id):
    """Conferma del contorno scelto nel CAD (o sul disegno grande della pagina
    laser) sul disegno dell'ordine.

    Corpo: {outer_xy: [[x,y]...], holes_xy: [[[x,y]...]...], pagina?} in mm,
    come li manda il CAD. Area, perimetro e fori si RICALCOLANO qui (non ci si
    fida dei numeri della pagina).

    Risposta: {success, area_dm2, perimetro_taglio_m, n_forature, bbox_w_mm,
    bbox_h_mm, pronto_lantek, motivo, file_lantek}"""
    try:
        data = request.get_json(silent=True) or {}
        try:
            outer_xy = _punti(data.get('outer_xy'), 3)
        except (TypeError, ValueError):
            return _errore('Contorno esterno mancante o non valido: scegli di nuovo il contorno del pezzo')
        fori_in = data.get('holes_xy') or []
        if not isinstance(fori_in, list) or len(fori_in) > _MAX_FORI:
            return _errore('Fori non validi: scegli di nuovo il contorno del pezzo')
        try:
            holes_xy = [_punti(h, 3) for h in fori_in]
        except (TypeError, ValueError):
            return _errore('Un foro del contorno non è valido: scegli di nuovo il contorno del pezzo')
        corpo, stato = salva_contorno(order_id, articolo_id, outer_xy, holes_xy,
                                      dal_motore=False, pagina=str(data.get('pagina') or 'cad')[:20])
        return jsonify(corpo), stato
    except Exception:
        logger.exception("contorno del pezzo dell'ordine non salvato")
        return _errore('Contorno non salvato per un errore imprevisto: riprova o avvisa Stefano', 500)
