"""Il PACCO DI TAGLIO della pagina laser (16/10/2026).

Stefano al laser (memoria "come lavora il laser"): mette insieme "tutti quelli
che posso" con gli stessi spessori, dentro ci deve stare l'ordine che scade
per primo; controlla i disegni mentre importa il pacco in Lantek; taglia; poi
l'ufficio deve saperlo (oggi a voce). Il pacco e' un oggetto con i suoi stati:

    proposto -> confermato -> in_lantek -> tagliato        (annullato = sciolto)

  GET  /api/pacchi                       i pacchi aperti (e i tagliati delle ultime 24 ore)
  POST /api/pacchi                       {"ordini": [...]}: crea un pacco PROPOSTO
  GET  /api/pacchi/<id>                  il pacco col riepilogo per lamiera (pezzi, kg, ore)
  POST /api/pacchi/<id>/ordini           {"aggiungi": [...], "togli": [...]}
  POST /api/pacchi/<id>/stato            {"stato": "confermato" | "proposto" | "in_lantek" | "annullato"}
  POST /api/pacchi/<id>/tagliato         {"chiave": "FERRO|3"} una lamiera, senza chiave tutte;
                                         {"annulla": true, "fatto": {...}} rimette com'era

Regole:
  - un ordine sta in UN solo pacco aperto; si sta nel pacco per ORDINE intero
    (Lantek riceve l'ordine intero e il "taglio fatto" e' per ordine): il pacco
    mostra tutte le lamiere dei suoi ordini, anche quelle di un altro spessore;
  - niente parte da solo: il pacco nasce PROPOSTO e lo conferma lui. "Manda a
    Lantek" resta quello di sempre, ordine per ordine (/lantek-invia): qui si
    dice solo quando il pacco e' tutto in Lantek;
  - "Segna tagliato" (una lamiera o tutto) segna le lamiere degli ordini del
    pacco (lamiere_tagliate, come dal pannello dell'ordine); quando un ordine
    ha tutte le lamiere tagliate diventa "taglio fatto" (lo stesso di sempre),
    e l'ufficio lo vede in "Forse finiti" e "Tagliati oggi". Un ordine con
    codici ancora "da correggere in Lantek" NON diventa taglio fatto: quei
    pezzi non sono ancora tagliati;
  - ogni passo si annulla; tutto va nello storico (audit).
Un ordine si puo' sempre mandare e tagliare da solo, come prima.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request

from .accesso import richiede

logger = logging.getLogger(__name__)

bp_pacchi = Blueprint('pacchi_taglio', __name__)

STATI = ('proposto', 'confermato', 'in_lantek', 'tagliato', 'annullato')
APERTI = ('proposto', 'confermato', 'in_lantek')
PAROLE = {'proposto': 'Proposto', 'confermato': 'Confermato', 'in_lantek': 'In Lantek',
          'tagliato': 'Tagliato', 'annullato': 'Sciolto'}
# i passaggi che si fanno con /stato (il "tagliato" si fa con /tagliato)
PASSAGGI = {
    ('proposto', 'confermato'), ('confermato', 'proposto'),
    ('confermato', 'in_lantek'), ('in_lantek', 'confermato'),
    ('proposto', 'annullato'), ('confermato', 'annullato'), ('in_lantek', 'annullato'),
    ('annullato', 'proposto'),
}
# chi entra in un pacco: gli ordini del laser da mandare o gia' in Lantek
AMMESSI = ('coda', 'in_lantek')

# Le scritture sui pacchi una alla volta: "un ordine in un solo pacco aperto"
# si controlla e si scrive senza che un'altra postazione passi in mezzo.
_LOCK = threading.Lock()


def _app():
    import importlib
    return importlib.import_module(__package__ + '.app')


def _iso(dt):
    return dt.isoformat() + 'Z' if dt else None


def _errore(testo, http=400, **extra):
    return jsonify({'success': False, 'error': testo, **extra}), http


# ---------------------------------------------------------------------------
# Lettura
# ---------------------------------------------------------------------------
def ordini_di(session, pacco_ids) -> dict:
    """{pacco_id: [order_id, ...]} nell'ordine in cui sono stati messi."""
    from .models import PaccoOrdine
    ids = [i for i in pacco_ids if i]
    out = {i: [] for i in ids}
    if not ids:
        return out
    for r in (session.query(PaccoOrdine).filter(PaccoOrdine.pacco_id.in_(ids))
              .order_by(PaccoOrdine.posizione, PaccoOrdine.aggiunto_il).all()):
        out.setdefault(r.pacco_id, []).append(r.order_id)
    return out


def pacchi_aperti_degli_ordini(session, order_ids, tranne=None) -> dict:
    """{order_id: (pacco_id, numero)} per gli ordini gia' in un pacco aperto."""
    from .models import PaccoOrdine, PaccoTaglio
    ids = [i for i in order_ids if i]
    if not ids:
        return {}
    q = (session.query(PaccoOrdine.order_id, PaccoTaglio.id, PaccoTaglio.numero)
         .join(PaccoTaglio, PaccoTaglio.id == PaccoOrdine.pacco_id)
         .filter(PaccoOrdine.order_id.in_(ids), PaccoTaglio.stato.in_(APERTI)))
    if tranne:
        q = q.filter(PaccoTaglio.id != tranne)
    return {oid: (pid, num) for oid, pid, num in q.all()}


def _dict(p, ordini) -> dict:
    return {'id': p.id, 'numero': p.numero, 'nome': f'Pacco {p.numero}', 'stato': p.stato,
            'parola': PAROLE.get(p.stato, p.stato), 'aperto': p.stato in APERTI,
            'ordini': list(ordini), 'note': p.note or '',
            'creato_il': _iso(p.creato_il), 'creato_da': p.creato_da,
            'aggiornato_il': _iso(p.aggiornato_il), 'aggiornato_da': p.aggiornato_da,
            'confermato_il': _iso(p.confermato_il), 'in_lantek_il': _iso(p.in_lantek_il),
            'tagliato_il': _iso(p.tagliato_il)}


def _carica(session, pacco_id):
    from .models import PaccoTaglio
    return session.query(PaccoTaglio).filter(PaccoTaglio.id == pacco_id).first()


def _ordini_obj(session, ids) -> dict:
    from .models import Order
    if not ids:
        return {}
    return {o.id: o for o in session.query(Order).filter(Order.id.in_(ids)).all()}


def _analisi(o, fresca=False) -> dict:
    """Il riassunto della pagina laser per l'ordine (cache di api_laser)."""
    from . import api_laser as _al
    try:
        return _al._analisi_in_cache(o, forza=fresca) or {}
    except Exception:
        logger.warning('analisi laser di %s non letta per il pacco', getattr(o, 'id', '?'), exc_info=True)
        return {}


def _chiavi_lamiere(a: dict) -> list:
    return [g['chiave'] for g in (a.get('lamiere') or []) if g.get('chiave')]


def _densita(materiale, mat_cfg) -> float:
    from .preventivi import laser_cost_estimator as _le
    try:
        _c, m = _le._resolve_material(str(materiale or '').replace(' ', '_').upper(), mat_cfg)
        return float((m or {}).get('densita_kg_dm3') or 7.85)
    except Exception:
        return 7.85


def riepilogo_lamiere(session, ordini: list) -> list:
    """Il pacco per lamiera (materiale + spessore): quali ordini, pezzi, kg e
    minuti di laser, e se e' segnata tagliata in ogni ordine.

    Lamiere e pezzi sono quelli della pagina laser (api_laser.analisi, la
    stessa chiave): kg e minuti si sommano dalla distinta degli ordini. I
    minuti sono quelli del carico del laser (backend/carico_laser.py: taglio
    tarato su Lantek + carico lamiere + scarico pezzi), contati sui pezzi di
    TUTTI gli ordini del pacco insieme: la stessa lamiera si carica una volta."""
    A = _app()
    from . import api_laser as _al
    from . import carico_laser as _cl
    from .database import ConfigManager
    ids = [o.id for o in ordini]
    tg = _al.lamiere_tagliate(ids)
    per: dict = {}
    for o in ordini:
        a = _analisi(o)
        for g in a.get('lamiere') or []:
            k = g.get('chiave')
            if not k:
                continue
            x = per.setdefault(k, {'chiave': k, 'materiale': g.get('materiale'), 'spessore': g.get('spessore'),
                                   'n_codici': 0, 'n_pezzi': 0, 'kg': 0.0, 'kg_mancanti': 0,
                                   'pezzi_tempo': [], 'tempo_mancante': 0, 'ordini': []})
            x['n_codici'] += int(g.get('n_codici') or 0)
            x['n_pezzi'] += int(g.get('n_pezzi') or 0)
            t = (tg.get(o.id) or {}).get(k)
            x['ordini'].append({'id': o.id, 'n_pezzi': int(g.get('n_pezzi') or 0),
                                'tagliata': bool(t) or bool(getattr(o, 'taglio_completato', False)),
                                'tagliata_il': (t or {}).get('il')})
    if not per:
        return []
    # kg e minuti dalla distinta (tempi di Lantek letti tutti insieme)
    try:
        A._precarica_tempi_lantek(session, ordini)
    except Exception:
        logger.warning('tempi Lantek non precaricati per il pacco', exc_info=True)
    try:
        mat_cfg = (((ConfigManager.load_config() or {}).get('laser_config') or {}).get('materiali')
                   or A._laser_estimator.DEFAULT_LASER_CONFIG['materiali'])
    except Exception:
        mat_cfg = {}
    for o in ordini:
        try:
            righe = A._distinta_lantek_con_esclusi(o)[0]
        except Exception:
            logger.warning('distinta di %s non letta per il pacco', o.id, exc_info=True)
            continue
        for r in righe:
            if r.get('tipo') != 'lamiera' or not r.get('codice'):
                continue
            x = per.get(_al.chiave_lamiera(r.get('materiale'), r.get('spessore_mm')))
            if x is None:
                continue
            q = int(r.get('quantita') or 0) or 1
            try:
                area, sp = float(r.get('area_dm2') or 0), float(r.get('spessore_mm') or 0)
            except (TypeError, ValueError):
                area, sp = 0.0, 0.0
            if area > 0 and sp > 0:
                x['kg'] += area * sp / 100.0 * _densita(r.get('materiale'), mat_cfg) * q
            else:
                x['kg_mancanti'] += 1
            if r.get('tempo_min') is None:
                x['tempo_mancante'] += 1
            x['pezzi_tempo'].append({'quantita': q, 'w_mm': r.get('bbox_w_mm'), 'h_mm': r.get('bbox_h_mm'),
                                     'tempo_min': r.get('tempo_min')})
    cal = A._cal_config()
    formati = A._formati_lamiera()
    par = {'margine_mm': 10, 'distanza_mm': 5}
    out = []
    for x in per.values():
        pz = x.pop('pezzi_tempo')
        if pz:
            x['min'] = _cl.durata_ordine([pz], None, cal, formati, par)['min']
            x['n_fogli'] = _cl.lamiere(pz, formati, par)
        else:
            x['min'], x['n_fogli'] = None, None
        x['kg'] = round(x['kg'], 1) if x['kg'] else None
        x['n_ordini'] = len(x['ordini'])
        x['tagliata'] = all(y['tagliata'] for y in x['ordini'])
        out.append(x)
    out.sort(key=lambda x: (str(x['materiale'] or '~').upper(), _al._num(x['spessore'])))
    return out


@bp_pacchi.route('/api/pacchi', methods=['GET'])
@richiede('laser', 'ufficio')
def api_pacchi():
    """I pacchi aperti, poi quelli tagliati nelle ultime 24 ore (?giorni=N per
    vederne di piu'). Senza riepilogo: quello lo da' /api/pacchi/<id>."""
    from .database import get_session
    from .models import PaccoTaglio
    try:
        giorni = max(1, min(60, int(request.args.get('giorni') or 1)))
    except ValueError:
        giorni = 1
    limite = datetime.utcnow() - timedelta(days=giorni)
    session = get_session()
    try:
        pp = session.query(PaccoTaglio).filter(
            (PaccoTaglio.stato.in_(APERTI))
            | ((PaccoTaglio.stato == 'tagliato') & (PaccoTaglio.tagliato_il >= limite))).all()
        oo = ordini_di(session, [p.id for p in pp])
        pp.sort(key=lambda p: (0 if p.stato in APERTI else 1, -(p.numero or 0)))
        return jsonify({'success': True, 'pacchi': [_dict(p, oo.get(p.id, [])) for p in pp]}), 200
    finally:
        session.close()


@bp_pacchi.route('/api/pacchi/<pacco_id>', methods=['GET'])
@richiede('laser', 'ufficio')
def api_pacco(pacco_id):
    from .database import get_session
    session = get_session()
    try:
        p = _carica(session, pacco_id)
        if not p:
            return _errore('Pacco non trovato', 404)
        ids = ordini_di(session, [p.id]).get(p.id, [])
        oo = _ordini_obj(session, ids)
        ordini = [oo[i] for i in ids if i in oo]
        try:
            lam = riepilogo_lamiere(session, ordini)
        except Exception:
            logger.exception('riepilogo del pacco %s non riuscito', pacco_id)
            lam = []
        return jsonify({'success': True, 'pacco': _dict(p, ids), 'lamiere': lam,
                        'mancanti': [i for i in ids if i not in oo or oo[i].is_deleted]}), 200
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Scrittura
# ---------------------------------------------------------------------------
def _controlla_ordini(session, ids, tranne=None):
    """(ordini, errore) per mettere `ids` in un pacco: ci sono, sono del laser
    (da mandare o in Lantek) e non stanno gia' in un altro pacco aperto."""
    from . import api_laser as _al
    oo = _ordini_obj(session, ids)
    mancano = [i for i in ids if i not in oo or oo[i].is_deleted]
    if mancano:
        return None, _errore('Ordine non trovato', 404, ordini=mancano)
    fuori = [i for i in ids if _al.stato_laser(oo[i]) not in AMMESSI]
    if fuori:
        nomi = ', '.join(_numero(oo[i]) for i in fuori[:4])
        return None, _errore(f'Nel pacco vanno gli ordini da mandare a Lantek o già in Lantek, da tagliare: {nomi} no',
                             409, codice='non_ammesso', ordini=fuori)
    gia = pacchi_aperti_degli_ordini(session, ids, tranne=tranne)
    if gia:
        testo = '; '.join(f'{_numero(oo[i])} è già nel pacco {n}' for i, (_p, n) in list(gia.items())[:4])
        return None, _errore(testo, 409, codice='in_altro_pacco',
                             ordini={i: {'pacco': pid, 'numero': n} for i, (pid, n) in gia.items()})
    return [oo[i] for i in ids], None


def _numero(o) -> str:
    return (getattr(o, 'numero_ordine', None) or str(getattr(o, 'id', ''))[:8]).strip()


def _ids(v) -> list:
    out = []
    for x in v or []:
        x = str(x or '').strip()
        if x and x not in out:
            out.append(x)
    return out[:200]


@bp_pacchi.route('/api/pacchi', methods=['POST'])
@richiede('laser')
def api_pacco_crea():
    """Crea un pacco PROPOSTO con gli ordini scelti. Non manda niente."""
    A = _app()
    from .database import get_session
    from .models import PaccoOrdine, PaccoTaglio
    from sqlalchemy import func
    corpo = request.get_json(silent=True) or {}
    ids = _ids(corpo.get('ordini'))
    if not ids:
        return _errore('Scegli almeno un ordine')
    chi = A._chi_nome() or None
    with _LOCK:
        session = get_session()
        try:
            ordini, err = _controlla_ordini(session, ids)
            if err:
                return err
            nomi = ', '.join(_numero(o) for o in ordini)
            numero = (session.query(func.max(PaccoTaglio.numero)).scalar() or 0) + 1
            p = PaccoTaglio(numero=numero, stato='proposto', creato_da=chi, aggiornato_da=chi,
                            note=str(corpo.get('note') or '')[:500] or None)
            session.add(p)
            session.flush()
            for i, oid in enumerate(ids):
                session.add(PaccoOrdine(pacco_id=p.id, order_id=oid, posizione=i, aggiunto_da=chi))
            session.commit()
            out = _dict(p, ids)
        except Exception as e:
            session.rollback()
            logger.exception('pacco non creato')
            return _errore(str(e)[:200], 500)
        finally:
            session.close()
    A._audit('PACCO_CREATO', 'pacchi_taglio', out['id'],
             f"Pacco {out['numero']} proposto con {len(ids)} ordini: " + nomi)
    return jsonify({'success': True, 'pacco': out}), 201


@bp_pacchi.route('/api/pacchi/<pacco_id>/ordini', methods=['POST'])
@richiede('laser')
def api_pacco_ordini(pacco_id):
    """Aggiunge o toglie ordini. Si aggiunge finche' il pacco e' proposto o
    confermato (dopo l'invio a Lantek un ordine nuovo non c'e' in Lantek); si
    toglie finche' e' aperto. L'ultimo ordine non si toglie: si scioglie il pacco."""
    A = _app()
    from .database import get_session
    from .models import PaccoOrdine
    corpo = request.get_json(silent=True) or {}
    agg, tog = _ids(corpo.get('aggiungi')), _ids(corpo.get('togli'))
    if not agg and not tog:
        return _errore('Niente da cambiare')
    chi = A._chi_nome() or None
    with _LOCK:
        session = get_session()
        try:
            p = _carica(session, pacco_id)
            if not p:
                return _errore('Pacco non trovato', 404)
            if p.stato not in APERTI:
                return _errore(f'Il pacco {p.numero} è {PAROLE[p.stato].lower()}: non si cambia più', 409, codice='chiuso')
            if agg and p.stato not in ('proposto', 'confermato'):
                return _errore(f'Il pacco {p.numero} è già in Lantek: fai un pacco nuovo per gli altri ordini',
                               409, codice='gia_in_lantek')
            dentro = ordini_di(session, [p.id]).get(p.id, [])
            agg = [i for i in agg if i not in dentro]
            tog = [i for i in tog if i in dentro]
            if dentro and not [i for i in dentro if i not in tog] and not agg:
                return _errore("È l'ultimo ordine del pacco: per toglierlo sciogli il pacco", 409, codice='ultimo')
            nuovi = []
            if agg:
                nuovi, err = _controlla_ordini(session, agg, tranne=p.id)
                if err:
                    return err
            nomi = {o.id: _numero(o) for o in nuovi}
            nomi.update({i: _numero(o) for i, o in _ordini_obj(session, tog).items()})
            if tog:
                session.query(PaccoOrdine).filter(PaccoOrdine.pacco_id == p.id,
                                                  PaccoOrdine.order_id.in_(tog)).delete(synchronize_session=False)
            base = len(dentro)
            for i, oid in enumerate(agg):
                session.add(PaccoOrdine(pacco_id=p.id, order_id=oid, posizione=base + i, aggiunto_da=chi))
            p.aggiornato_il, p.aggiornato_da = datetime.utcnow(), chi
            session.commit()
            ora = ordini_di(session, [p.id]).get(p.id, [])
            out = _dict(p, ora)
        except Exception as e:
            session.rollback()
            logger.exception('ordini del pacco non cambiati')
            return _errore(str(e)[:200], 500)
        finally:
            session.close()
    parti = []
    if agg:
        parti.append('aggiunti ' + ', '.join(nomi.get(i, i[:8]) for i in agg))
    if tog:
        parti.append('tolti ' + ', '.join(nomi.get(i, i[:8]) for i in tog))
    A._audit('PACCO_ORDINI', 'pacchi_taglio', pacco_id, f"Pacco {out['numero']}: " + '; '.join(parti))
    return jsonify({'success': True, 'pacco': out, 'aggiunti': agg, 'tolti': tog}), 200


def non_in_lantek(ordini: list) -> list:
    """Gli ordini del pacco che NON sono ancora in Lantek: [{id, perche}].
    In Lantek = segnato "In Lantek" (o gia' tagliato), oppure partito tutto
    tranne i codici tenuti fuori "da correggere in Lantek" (restano rossi
    nella pagina laser finche' non sono a posto)."""
    from . import api_laser as _al
    out = []
    for o in ordini:
        st = _al.stato_laser(o)
        if st in ('in_lantek', 'tagliato'):
            continue
        if st == 'coda':
            a = _analisi(o, fresca=True)
            if a.get('n_correggere') and not a.get('n_invio') and not a.get('errore'):
                continue
            out.append({'id': o.id, 'perche': 'da mandare a Lantek'})
        else:
            out.append({'id': o.id, 'perche': 'non è più fra quelli da tagliare'})
    return out


@bp_pacchi.route('/api/pacchi/<pacco_id>/stato', methods=['POST'])
@richiede('laser')
def api_pacco_stato(pacco_id):
    """Un passo del pacco (o il suo annullamento): vedi PASSAGGI.
    in_lantek solo quando tutti gli ordini sono in Lantek (non_in_lantek)."""
    A = _app()
    from .database import get_session
    corpo = request.get_json(silent=True) or {}
    nuovo = str(corpo.get('stato') or '').strip()
    if nuovo not in STATI:
        return _errore('Stato sconosciuto')
    chi = A._chi_nome() or None
    with _LOCK:
        session = get_session()
        try:
            p = _carica(session, pacco_id)
            if not p:
                return _errore('Pacco non trovato', 404)
            prima = p.stato
            if prima == nuovo:
                return jsonify({'success': True, 'pacco': _dict(p, ordini_di(session, [p.id]).get(p.id, [])),
                                'invariato': True}), 200
            if (prima, nuovo) not in PASSAGGI:
                return _errore(f'Da {PAROLE[prima].lower()} non si passa a {PAROLE[nuovo].lower()}', 409,
                               codice='passaggio')
            ids = ordini_di(session, [p.id]).get(p.id, [])
            if nuovo in APERTI and not ids:
                return _errore('Il pacco è vuoto: aggiungi un ordine', 409, codice='vuoto')
            if prima == 'annullato':
                # si rimette su solo se i suoi ordini sono ancora liberi e al laser
                _o, err = _controlla_ordini(session, ids, tranne=p.id)
                if err:
                    return err
            if nuovo == 'in_lantek':
                oo = _ordini_obj(session, ids)
                manca = non_in_lantek([oo[i] for i in ids if i in oo])
                if manca:
                    nomi = ', '.join(_numero(oo[x['id']]) for x in manca[:4])
                    return _errore(f'Non sono ancora tutti in Lantek: {nomi}', 409, codice='non_in_lantek',
                                   ordini=manca)
            ora = datetime.utcnow()
            p.stato, p.aggiornato_il, p.aggiornato_da = nuovo, ora, chi
            if nuovo == 'confermato' and prima == 'proposto':
                p.confermato_il = ora
            if nuovo == 'in_lantek':
                p.in_lantek_il = ora
            if nuovo == 'proposto':
                p.confermato_il = None
            if nuovo == 'confermato' and prima == 'in_lantek':
                p.in_lantek_il = None
            session.commit()
            out = _dict(p, ids)
        except Exception as e:
            session.rollback()
            logger.exception('stato del pacco non cambiato')
            return _errore(str(e)[:200], 500)
        finally:
            session.close()
    A._audit('PACCO_STATO', 'pacchi_taglio', pacco_id,
             f"Pacco {out['numero']}: {PAROLE[prima]} -> {PAROLE[nuovo]}")
    return jsonify({'success': True, 'pacco': out, 'prima': prima}), 200


@bp_pacchi.route('/api/pacchi/<pacco_id>/tagliato', methods=['POST'])
@richiede('laser')
def api_pacco_tagliato(pacco_id):
    """"Segna tagliato" di una lamiera del pacco ({"chiave"}) o di tutto.

    Segna la lamiera in ogni ordine del pacco che la ha (lamiere_tagliate);
    l'ordine con tutte le lamiere tagliate diventa "taglio fatto" (lo stesso
    di sempre: l'ufficio lo vede in "Forse finiti"); il pacco con tutti gli
    ordini tagliati diventa tagliato. Risponde con "fatto" (cosa ha cambiato):
    {"annulla": true, "fatto": ...} rimette esattamente com'era."""
    A = _app()
    from . import api_laser as _al
    from .database import OrderManager, get_session
    from .models import LamieraTagliata
    corpo = request.get_json(silent=True) or {}
    chi_nome, chi_id = A._chi_nome() or None, A._chi()
    annulla = bool(corpo.get('annulla'))
    chiave = str(corpo.get('chiave') or '').strip()[:80] or None
    if chiave and '|' not in chiave:
        return _errore('Lamiera sconosciuta')
    with _LOCK:
        session = get_session()
        try:
            p = _carica(session, pacco_id)
            if not p:
                return _errore('Pacco non trovato', 404)
            ids = ordini_di(session, [p.id]).get(p.id, [])
            oo = _ordini_obj(session, ids)
            nomi = {i: _numero(o) for i, o in oo.items()}
            if annulla:
                return _annulla_taglio(session, p, ids, oo, corpo.get('fatto') or {}, chi_nome, chi_id)
            if p.stato not in ('confermato', 'in_lantek'):
                return _errore(f'Il pacco {p.numero} è {PAROLE[p.stato].lower()}: prima mettilo in Lantek', 409,
                               codice='non_in_lantek')
            gia = {(r.order_id, r.chiave) for r in session.query(LamieraTagliata)
                   .filter(LamieraTagliata.order_id.in_(ids or ['-'])).all()}
            fatto = {'lamiere': [], 'ordini': [], 'pacco_prima': p.stato}
            lam_ord, trattenuti = {}, []
            senza_disegni, confermati_senza = [], []
            for oid in ids:
                o = oo.get(oid)
                if not o or o.is_deleted or o.taglio_completato:
                    continue
                a = _analisi(o)
                ks = _chiavi_lamiere(a)
                lam_ord[oid] = (ks, a)
                for k in ks:
                    if (chiave is None or k == chiave) and (oid, k) not in gia:
                        session.add(LamieraTagliata(order_id=oid, chiave=k, tagliata_da=chi_nome))
                        gia.add((oid, k))
                        fatto['lamiere'].append([oid, k])
            if chiave and not fatto['lamiere'] and not any(chiave in ks for ks, _a in lam_ord.values()):
                session.rollback()
                return _errore('Nessun ordine del pacco ha questa lamiera da tagliare', 409, codice='niente')
            session.commit()
            # gli ordini con tutte le lamiere tagliate: "taglio fatto"
            for oid, (ks, a) in lam_ord.items():
                tutte = all((oid, k) in gia for k in ks) if ks else chiave is None
                if not tutte:
                    continue
                if a.get('n_correggere'):
                    trattenuti.append(oid)          # codici da correggere: non e' tutto tagliato
                    continue
                # assiemi senza componenti / righe senza disegno (DECA A 001252):
                # l'ordine non e' tagliato tutto, salvo conferma esplicita
                manc = [str(x.get('codice')) for x in (a.get('mancano_disegni') or [])]
                if manc and not set(manc) <= {str(c) for c in (corpo.get('conferma_mancanti') or [])}:
                    senza_disegni.append({'id': oid, 'numero': nomi.get(oid, oid[:8]), 'codici': manc})
                    continue
                if manc:
                    confermati_senza.append(f"{nomi.get(oid, oid[:8])}: {', '.join(manc)}")
                r = OrderManager.mark_laser_done(oid, user_id=chi_id)
                if r.get('success'):
                    fatto['ordini'].append(oid)
            for oid in set([x[0] for x in fatto['lamiere']] + fatto['ordini']):
                A._invalida_analisi_laser(oid)
            # il pacco e' tagliato quando ogni ordine ha tutte le lamiere tagliate
            session.expire_all()
            oo = _ordini_obj(session, ids)
            finito = all(
                (oo.get(i) is None or oo[i].is_deleted or oo[i].taglio_completato
                 or (i in lam_ord and lam_ord[i][0] and all((i, k) in gia for k in lam_ord[i][0])))
                for i in ids)
            p = _carica(session, pacco_id)
            # un ordine lasciato da tagliare per i disegni mancanti tiene aperto il pacco
            finito = finito and not senza_disegni
            if finito and p.stato != 'tagliato':
                p.stato, p.tagliato_il = 'tagliato', datetime.utcnow()
                p.aggiornato_il, p.aggiornato_da = datetime.utcnow(), chi_nome
                session.commit()
            out = _dict(p, ids)
        except Exception as e:
            session.rollback()
            logger.exception('taglio del pacco non segnato')
            return _errore(str(e)[:200], 500)
        finally:
            session.close()
    A._audit('PACCO_TAGLIATO', 'pacchi_taglio', pacco_id,
             f"Pacco {out['numero']}: " + (f"lamiera {chiave.replace('|', ' ')} tagliata" if chiave else 'tutto tagliato')
             + (f"; taglio fatto: {', '.join(nomi.get(i, i[:8]) for i in fatto['ordini'])}" if fatto['ordini'] else '')
             + (f"; restano da correggere in Lantek: {', '.join(nomi.get(i, i[:8]) for i in trattenuti)}"
                if trattenuti else '')
             + ('; pacco tagliato' if out['stato'] == 'tagliato' else '')
             + (f"; NON segnati taglio fatto, mancano i disegni: {', '.join(x['numero'] for x in senza_disegni)}"
                if senza_disegni else '')
             + (f"; taglio fatto CONFERMATO anche se mancano i disegni ({'; '.join(confermati_senza)})"
                if confermati_senza else ''))
    return jsonify({'success': True, 'pacco': out, 'fatto': fatto, 'trattenuti': trattenuti,
                    'mancano_disegni': senza_disegni,
                    'lamiere_tagliate': _al.lamiere_tagliate(ids)}), 200


def _annulla_taglio(session, p, ids, oo, fatto, chi_nome, chi_id):
    """Rimette com'era un "Segna tagliato" del pacco: solo quello che quel
    passo aveva cambiato (lamiere segnate, ordini diventati taglio fatto)."""
    A = _app()
    from . import api_laser as _al
    from .database import OrderManager
    from .models import LamieraTagliata
    lam = [(str(x[0]), str(x[1])) for x in (fatto.get('lamiere') or []) if isinstance(x, (list, tuple)) and len(x) == 2]
    lam = [x for x in lam if x[0] in ids]
    ordini = [str(i) for i in (fatto.get('ordini') or []) if str(i) in ids]
    for oid, k in lam:
        session.query(LamieraTagliata).filter(LamieraTagliata.order_id == oid,
                                              LamieraTagliata.chiave == k).delete(synchronize_session=False)
    prima = fatto.get('pacco_prima')
    if p.stato == 'tagliato' and prima in APERTI:
        p.stato, p.tagliato_il = prima, None
    p.aggiornato_il, p.aggiornato_da = datetime.utcnow(), chi_nome
    session.commit()
    for oid in ordini:
        OrderManager.mark_laser_undone(oid, user_id=chi_id)
    for oid in set([x[0] for x in lam] + ordini):
        A._invalida_analisi_laser(oid)
    out = _dict(p, ids)
    A._audit('PACCO_TAGLIATO', 'pacchi_taglio', p.id,
             f"Pacco {p.numero}: taglio annullato ({len(lam)} lamiere, {len(ordini)} ordini di nuovo da tagliare)")
    return jsonify({'success': True, 'pacco': out, 'annullato': {'lamiere': lam, 'ordini': ordini},
                    'lamiere_tagliate': _al.lamiere_tagliate(ids)}), 200
