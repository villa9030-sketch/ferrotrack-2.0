"""Assiemi negli ordini: quanti assiemi e quanti pezzi per assieme (13/10/2026).

Il caso vero (DECA A 001221 BIS): nell'ordine la riga 26T30SA0233-00
"Struttura x MTR" x2 + x1 = 3 assiemi; nel pacchetto la sottocartella
26T30SA0233-00 con i disegni dei componenti, che nell'ordine non hanno una
riga loro. Prima l'assieme nasceva x1 e ogni componente x1, segnato
"controllato": tre lamiere mandate a Lantek con 1 invece di 3 x (pezzi per
assieme), e "Da procurare" con 1.

Ora:
  - la quantita' dell'assieme e' quella della sua riga nell'ordine
    (rfq_importer: righe ripetute sommate);
  - i pezzi per assieme di un componente senza riga si leggono nella
    distinta del PDF dell'assieme (preventivi/distinte_pdf.py) se c'e';
    altrimenti il componente resta "pezzi per assieme da confermare"
    (riga dell'ordine 'qta_da_confermare'): bloccato per Lantek finche' il
    laser non scrive il numero;
  - dalla pagina laser si cambiano i pezzi per assieme e la quantita'
    dell'assieme (con lo storico); la distinta, Lantek e "Da procurare" si
    ricalcolano da li' (quantita' = pezzi per assieme x assiemi).

Sul preventivo ACCETTATO dietro l'ordine si scrivono solo questi numeri
(come la decisione del laser): e' il lavoro sull'ordine, non l'offerta.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime

logger = logging.getLogger(__name__)

# Stato della riga d'ordine di un componente senza quantita' per assieme
DA_CONFERMARE = 'qta_da_confermare'
QTA_MAX = 10000


def _chiave(c) -> str:
    """'26CCSA0038-00.dxf' -> '26CCSA0038' (come le distinte)."""
    from .preventivi.distinte_pdf import chiave_codice
    return chiave_codice(c)


# ---------------------------------------------------------------------------
# Caricamento del pacchetto
# ---------------------------------------------------------------------------
def distinta_assieme(prev_dir: str, result, cod_ass: str) -> dict:
    """La distinta di un assieme: {chiave codice: {qta, testo, dubbio}}.

    Dalla tabella dei componenti del PDF d'insieme (salvato nella cartella
    del preventivo) e da quella scritta nel DXF d'insieme del pacchetto
    (SolidWorks la mette in un blocco di testi). Le due di solito
    coincidono; il PDF a volte taglia i codici, il DXF e' esatto: si unisce,
    e se dicono quantita' diverse lo stesso codice e' "dubbio" (lo conferma
    il laser). Codici della tabella senza revisione (191700045 = il disegno
    191700045-00)."""
    from .preventivi.distinte_pdf import distinte_per_codici, leggi_distinta_dxf
    k_ass = _chiave(cod_ass)
    fonti = []
    try:
        d = distinte_per_codici(prev_dir, [cod_ass], profondita=0).get(k_ass) if prev_dir else None
        if d:
            fonti.append(d)
    except Exception:
        logger.warning("distinta PDF dell'assieme %s non letta", cod_ass, exc_info=True)
    dxf_map = getattr(result, 'dxf_map', None) or {}
    masters = getattr(result, 'master_dxf', None) or {}
    for base, dati in dxf_map.items():
        if masters.get(base) == cod_ass or _chiave(base) == k_ass:
            try:
                d = leggi_distinta_dxf(dati)
            except Exception:
                d = None
                logger.warning("distinta DXF dell'assieme %s non letta", cod_ass, exc_info=True)
            if d:
                fonti.append(d)
            break
    out = {}
    for d in fonti:
        # nella stessa tabella lo stesso codice su piu' righe si somma
        questa = {}
        for r in d.get('righe') or []:
            k = _chiave(r.get('codice'))
            try:
                q = int(round(float(r.get('qta') or 0)))
            except (TypeError, ValueError):
                q = 0
            if not k or q <= 0 or k == k_ass:
                continue
            testo = ' - '.join(x for x in (r.get('descrizione'), r.get('descrizione_agg')) if x)
            y = questa.setdefault(k, {'qta': 0, 'testo': testo[:200]})
            y['qta'] += q
        # fra PDF e DXF: se dicono quantita' diverse il codice e' dubbio
        for k, y in questa.items():
            x = out.get(k)
            if x is None:
                out[k] = {'qta': y['qta'], 'testo': y['testo'], 'dubbio': False}
                continue
            if x['qta'] != y['qta']:
                x['dubbio'] = True
            if not x['testo'] and y['testo']:
                x['testo'] = y['testo']
    return out


def quantita_da_distinte(prev_dir: str, result) -> int:
    """I pezzi per assieme letti nella distinta del disegno d'insieme (PDF o
    DXF del pacchetto) invece di 1 messo in silenzio.

      - componente 'per_assieme' (nella cartella dell'assieme, senza riga
        nell'ordine): prende la quantita' della distinta (qta_fonte
        'distinta');
      - disegno che l'ordine non cita ('non_trovato', o rimasto senza
        articolo nel pacchetto) ma che sta nella distinta di un assieme
        dell'ordine: diventa un componente di quell'assieme (DECA A 001221
        BIS: i componenti erano fuori dalla cartella dell'assieme);
      - un pezzo con la sua riga nell'ordine resta com'e' (ordinato a parte).
    Se le due distinte non concordano il pezzo resta "da confermare".
    Ritorna quanti pezzi hanno preso la quantita' dalla distinta."""
    from .preventivi.rfq_importer import ArticoloRFQ
    assiemi = list(getattr(result, 'assiemi', None) or [])
    if not assiemi:
        return 0
    articoli = getattr(result, 'articoli', None)
    if articoli is None:
        return 0
    trovati = 0
    # lo stesso codice puo' stare in piu' assiemi (DECA A 001252: 25CCPA0026
    # in 4 spalle): ogni distinta deve trovare il pezzo del SUO assieme.
    # Prima si teneva solo il primo e negli altri assiemi il pezzo restava
    # "da confermare" anche con la distinta letta.
    per_chiave = {}
    for a in articoli:
        if not getattr(a, 'e_master', False):
            per_chiave.setdefault(_chiave(a.codice), []).append(a)
    liberi = {_chiave(d): d for d in (getattr(result, 'dxf_no_match', None) or [])}
    for cod_ass in assiemi:
        dist = distinta_assieme(prev_dir, result, cod_ass)
        for k, riga in dist.items():
            cand = per_chiave.get(k) or []
            a = (next((x for x in cand if x.codice_assieme == cod_ass), None)
                 or next((x for x in cand if x.codice_assieme is None), None))
            if a is None and k in liberi:
                # disegno del pacchetto che l'ordine non cita: e' di quest'assieme
                base = liberi.pop(k)
                a = ArticoloRFQ(codice=os.path.splitext(base)[0], quantita=1, matched_dxf=base,
                                codice_assieme=cod_ass, descrizione='componente assieme (dalla distinta)',
                                qta_fonte='per_assieme', abbinamento='esatto')
                articoli.append(a)
                per_chiave.setdefault(k, []).append(a)
                try:
                    result.dxf_no_match.remove(base)
                except (AttributeError, ValueError):
                    pass
            if a is None:
                continue
            fonte = getattr(a, 'qta_fonte', 'pdf')
            if a.codice_assieme not in (None, cod_ass) or fonte not in ('per_assieme', 'non_trovato'):
                continue                  # ha la sua riga nell'ordine, o e' di un altro assieme
            a.codice_assieme = cod_ass
            try:
                result.dxf_no_match.remove(a.matched_dxf)     # non e' piu' "fuori ordine"
            except (AttributeError, ValueError):
                pass
            if riga['testo'] and (not a.descrizione or a.descrizione.startswith('componente assieme')):
                a.descrizione = riga['testo']
            if riga['dubbio']:
                a.qta_fonte = 'per_assieme'
                continue
            a.quantita = riga['qta']
            a.qta_fonte = 'distinta'
            trovati += 1
    comp = [a for a in articoli if getattr(a, 'qta_fonte', None) == 'per_assieme']
    if comp and hasattr(result, 'warnings'):
        result.warnings.append(
            f"{len(comp)} pezzi d'assieme senza quantita' (ne' nell'ordine ne' nella distinta): "
            "quanti per assieme lo conferma il laser (fino ad allora non vanno a Lantek)")
    if trovati and hasattr(result, 'warnings'):
        result.warnings.append(f"{trovati} pezzi d'assieme con la quantita' letta nella distinta del disegno d'insieme")
    return trovati


# ---------------------------------------------------------------------------
# Lettura per la pagina laser
# ---------------------------------------------------------------------------
def da_confermare(art: dict) -> bool:
    od = art.get('ordine') if isinstance(art.get('ordine'), dict) else {}
    return od.get('stato') == DA_CONFERMARE


def descrizioni_assiemi(order, codici) -> dict:
    """{codice assieme: descrizione} dalla riga dell'ordine del cliente."""
    out = {}
    if not codici:
        return out
    try:
        from .api_laser import _pdf_ordine, righe_ordine_cliente
        from .preventivi.ordine_codici import chiave
        per = righe_ordine_cliente(_pdf_ordine(order))
        for c in codici:
            rr = per.get(chiave(c)) or []
            d = next((str(r['descrizione']).strip() for r in rr if r.get('descrizione')), None)
            if d:
                out[c] = d[:200]
    except Exception:
        logger.warning("descrizioni degli assiemi non lette", exc_info=True)
    return out


def assiemi_ordine(order, righe: list) -> list:
    """Gli assiemi dell'ordine per la pagina laser: [{codice, descrizione,
    quantita, n_componenti, n_da_confermare}]. righe = la distinta."""
    ass = [r for r in righe or [] if r.get('tipo') == 'assieme' and r.get('codice')]
    if not ass:
        return []
    desc = descrizioni_assiemi(order, [r['codice'] for r in ass])
    out = []
    for r in ass:
        comp = [x for x in righe if x.get('tipo') != 'assieme' and x.get('assieme') == r['codice']]
        out.append({'codice': r['codice'], 'descrizione': desc.get(r['codice']),
                    'quantita': int(r.get('quantita') or 1), 'n_componenti': len(comp),
                    'n_da_confermare': sum(1 for x in comp if x.get('qta_da_confermare'))})
    return out


# ---------------------------------------------------------------------------
# Scrittura (sul preventivo dietro l'ordine, anche se accettato)
# ---------------------------------------------------------------------------
def _intero(v, minimo=1):
    try:
        f = float(str(v).replace(',', '.'))
    except (TypeError, ValueError):
        return None
    if f != int(f) or not minimo <= f <= QTA_MAX:
        return None
    return int(f)


def imposta_per_assieme(order, articolo_id, valore, chi: str = '') -> dict:
    """Pezzi per assieme (o quantita' del pezzo sciolto) scritti a mano.
    La riga d'ordine 'qta_da_confermare' diventa 'controllato' (chi, quando).
    {success, prima, dopo, codice, assieme, era_da_confermare} o {error}."""
    from sqlalchemy import text as _text
    from .database import PreventivoManager, get_session
    n = _intero(valore)
    if n is None:
        return {'error': f"Pezzi non validi: serve un numero intero da 1 a {QTA_MAX}"}
    pid = getattr(order, 'preventivo_id_origine', None)
    if not pid:
        return {'error': "Quest'ordine non ha pezzi con la distinta"}
    s = get_session()
    try:
        riga = s.execute(_text('SELECT codice, quantita, codice_assieme, extra_campi FROM preventivo_articoli '
                               'WHERE id = :i AND preventivo_id = :p'), {'i': str(articolo_id), 'p': pid}).fetchone()
        if riga is None:
            return {'error': "Pezzo non trovato in quest'ordine", 'codice_errore': 'non_trovato'}
        codice, prima, cod_ass, extra_j = riga
        try:
            extra = json.loads(extra_j or '{}') or {}
        except (TypeError, ValueError):
            extra = {}
        od = extra.get('ordine') if isinstance(extra.get('ordine'), dict) else None
        era = bool(od and od.get('stato') == DA_CONFERMARE)
        if od is not None:
            od['stato'] = 'controllato' if era else od.get('stato')
            od['qta_da'] = (chi or 'laser')[:60]
            od['qta_il'] = datetime.utcnow().isoformat(timespec='seconds')
            extra['ordine'] = od
            riga_n = extra.get('riga')
            extra = PreventivoManager._extra_articolo(extra)
            if isinstance(riga_n, int):
                extra['riga'] = riga_n
            s.execute(_text('UPDATE preventivo_articoli SET quantita = :q, extra_campi = :j WHERE id = :i'),
                      {'q': n, 'j': json.dumps(extra, ensure_ascii=False), 'i': str(articolo_id)})
        else:
            s.execute(_text('UPDATE preventivo_articoli SET quantita = :q WHERE id = :i'),
                      {'q': n, 'i': str(articolo_id)})
        s.commit()
    except Exception as e:
        s.rollback()
        logger.exception('pezzi per assieme non salvati')
        return {'error': str(e)[:200]}
    finally:
        s.close()
    return {'success': True, 'prima': int(prima or 0), 'dopo': n, 'codice': codice, 'assieme': cod_ass,
            'era_da_confermare': era}


def imposta_qta_assieme(order, codice_assieme, valore) -> dict:
    """Quanti assiemi ordina il cliente. {success, prima, dopo} o {error}."""
    from sqlalchemy import text as _text
    from .database import get_session
    n = _intero(valore)
    if n is None:
        return {'error': f"Quantità non valida: serve un numero intero da 1 a {QTA_MAX}"}
    pid = getattr(order, 'preventivo_id_origine', None)
    if not pid:
        return {'error': "Quest'ordine non ha la distinta"}
    s = get_session()
    try:
        r = s.execute(_text('SELECT id, qty FROM preventivo_assiemi WHERE preventivo_id = :p AND codice_assieme = :c'),
                      {'p': pid, 'c': str(codice_assieme)}).fetchall()
        if not r:
            return {'error': "Assieme non trovato in quest'ordine", 'codice_errore': 'non_trovato'}
        prima = int(r[0][1] or 1)
        s.execute(_text('UPDATE preventivo_assiemi SET qty = :q WHERE preventivo_id = :p AND codice_assieme = :c'),
                  {'q': n, 'p': pid, 'c': str(codice_assieme)})
        s.commit()
    except Exception as e:
        s.rollback()
        logger.exception("quantità dell'assieme non salvata")
        return {'error': str(e)[:200]}
    finally:
        s.close()
    return {'success': True, 'prima': prima, 'dopo': n}


def dopo_cambio(order) -> None:
    """Le quantita' sono cambiate: "Da procurare" si riallinea subito alla
    distinta e la pagina laser rilegge l'ordine."""
    import importlib
    try:
        A = importlib.import_module(__package__ + '.app')
        from . import procura as _pc
        _pc.sincronizza_righe(order.id, A._distinta_ordine(order)[0])
    except Exception:
        logger.warning('da procurare non riallineato per %s', getattr(order, 'id', '?'), exc_info=True)
    try:
        from . import api_laser
        api_laser.invalida(order.id)
    except Exception:
        pass

