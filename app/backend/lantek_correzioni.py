"""Codici "da correggere in Lantek": non si dimenticano (Stefano, 12/10/2026).

In "Manda a Lantek" un codice c'e' gia' in Lantek con spessore o materiale
diverso dall'ordine. Se chi conferma sceglie il valore dell'ORDINE, il codice
resta fuori dall'invio e va corretto a mano in Lantek. Prima l'ordine poteva
sembrare fatto e il codice restava li'. Ora:

  - si scrive qui (tabella lantek_correzioni) cosa deve diventare, chi e quando;
  - l'ordine resta rosso "N da correggere in Lantek" e l'elenco lo ricorda;
  - FerroTrack rilegge Lantek (SOLA LETTURA, pezzi_in_lantek con la sua cache)
    a ogni riassunto dell'ordine e su "Ricontrolla": quando Lantek coincide
    il codice e' "corretto", pronto da mandare (sempre con la conferma), e
    arriva l'avviso alla postazione laser;
  - chi l'ha deciso puo' cambiare idea: "usa il valore di Lantek" e il codice
    si manda subito cosi' com'e';
  - quando il codice ha il suo ordine di produzione in Lantek e' chiuso.

Quantita' cambiate DOPO l'invio (13/10/2026, DECA A 001221 BIS): un pezzo
d'assieme mandato con 1 e poi corretto a 2 per assieme x 3 assiemi = 6.
Cosa ha Lantek si sa dagli ordini di produzione di quell'ordine (sola
lettura) o, se Lantek non risponde, dai file mandati all'XML Importer
(uploads/lantek_import/Importer log/*.xml). Finche' non coincide l'ordine
dice "In Lantek: X ha 1, ora servono 6 — correggi l'ordine di produzione";
chi lo corregge a mano lo segna ("Corretto in Lantek", esito
'quantita_corretta' per quel numero).
"""
from __future__ import annotations

import logging
from datetime import datetime

logger = logging.getLogger(__name__)

TOLLERANZA_MM = 0.01


def _mm(v) -> str:
    return ('%g' % float(v)).replace('.', ',') + ' mm'


def _stesso_materiale(lantek, voluto) -> bool:
    """Come lantek.righe_quantita: "INOX" va bene anche per "INOX 304"."""
    a, b = str(lantek or '').strip().upper(), str(voluto or '').strip().upper()
    return bool(a and b and (a == b or a.endswith(b)))


def coincide(rec: dict, pezzo: dict | None) -> bool | None:
    """Lantek ha adesso i valori voluti? None = non si sa (Lantek non letto,
    o il codice non c'e' piu')."""
    if not pezzo or not pezzo.get('esiste'):
        return None
    if rec.get('spessore_voluto') is not None:
        sp = pezzo.get('spessore')
        if sp is None or abs(float(sp) - float(rec['spessore_voluto'])) > TOLLERANZA_MM:
            return False
    if rec.get('materiale_voluto') and not _stesso_materiale(pezzo.get('materiale'), rec['materiale_voluto']):
        return False
    return True


def _diz(r) -> dict:
    return {'id': r.id, 'order_id': r.order_id, 'codice': r.codice,
            'spessore_voluto': r.spessore_voluto, 'materiale_voluto': r.materiale_voluto,
            'spessore_lantek': r.spessore_lantek, 'materiale_lantek': r.materiale_lantek,
            'creato_il': r.creato_il.isoformat() + 'Z' if r.creato_il else None, 'creato_da': r.creato_da,
            'corretto_il': r.corretto_il.isoformat() + 'Z' if r.corretto_il else None,
            'chiuso_il': r.chiuso_il.isoformat() + 'Z' if r.chiuso_il else None,
            'chiuso_da': r.chiuso_da, 'esito': r.esito,
            'quantita_voluta': getattr(r, 'quantita_voluta', None),
            'quantita_lantek': getattr(r, 'quantita_lantek', None)}


def descrivi(rec: dict) -> str:
    """"spessore 3 mm (in Lantek 2 mm)" — cosa va scritto in Lantek."""
    parti = []
    if rec.get('spessore_voluto') is not None:
        parti.append(f"spessore {_mm(rec['spessore_voluto'])}"
                     + (f" (in Lantek {_mm(rec['spessore_lantek'])})" if rec.get('spessore_lantek') is not None else ''))
    if rec.get('materiale_voluto'):
        parti.append(f"materiale {rec['materiale_voluto']}"
                     + (f" (in Lantek {rec['materiale_lantek']})" if rec.get('materiale_lantek') else ''))
    return ', '.join(parti)


# ---------------------------------------------------------------------------
# Scrittura
# ---------------------------------------------------------------------------
def registra(order_id: str, righe_q: list, codici, chi: str = '') -> list:
    """Chi ha mandato a Lantek ha tenuto fuori questi codici scegliendo il
    valore dell'ordine: si scrivono "da correggere". righe_q = le righe di
    lantek.righe_quantita (codice di Lantek, valori di Lantek e dell'ordine,
    avvisi). Solo i codici con una differenza vera di spessore o materiale;
    se ce n'e' gia' uno aperto per lo stesso codice si aggiorna. Ritorna le
    correzioni aperte scritte (dict)."""
    from .database import get_session
    from .models import LantekCorrezione
    per = {str(r.get('codice') or '').strip().upper(): r for r in righe_q or []}
    session = get_session()
    out = []
    try:
        for c in sorted({str(c).strip() for c in codici or [] if str(c).strip()}):
            r = per.get(c.upper())
            if not r:
                continue
            avvisi = r.get('avvisi') or []
            diff_sp = any(str(a).startswith('spessore:') for a in avvisi) and r.get('spessore_ordine') is not None
            diff_mat = any(str(a).startswith('materiale:') for a in avvisi) and r.get('materiale_ordine')
            if not (diff_sp or diff_mat):
                continue
            rec = session.query(LantekCorrezione).filter(
                LantekCorrezione.order_id == order_id, LantekCorrezione.codice == r['codice'],
                LantekCorrezione.chiuso_il.is_(None)).first()
            if rec is None:
                rec = LantekCorrezione(order_id=order_id, codice=r['codice'],
                                       creato_il=datetime.utcnow(), creato_da=chi or None)
                session.add(rec)
            rec.spessore_voluto = float(r['spessore_ordine']) if diff_sp else None
            rec.materiale_voluto = str(r['materiale_ordine']) if diff_mat else None
            rec.spessore_lantek = float(r['spessore']) if diff_sp and r.get('spessore') is not None else None
            rec.materiale_lantek = str(r['materiale']) if diff_mat and r.get('materiale') else None
            rec.corretto_il = None
            session.flush()
            out.append(_diz(rec))
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    return out


def chiudi(order_id: str, codici, esito: str, chi: str = '') -> list:
    """Chiude le correzioni aperte di questi codici (esito 'mandato' o
    'usa_lantek'). Ritorna quelle chiuse (dict)."""
    from .database import get_session
    from .models import LantekCorrezione
    voluti = {str(c).strip().upper() for c in codici or [] if str(c).strip()}
    if not voluti:
        return []
    session = get_session()
    out = []
    try:
        for rec in session.query(LantekCorrezione).filter(
                LantekCorrezione.order_id == order_id, LantekCorrezione.chiuso_il.is_(None)).all():
            if rec.codice.strip().upper() in voluti:
                rec.chiuso_il = datetime.utcnow()
                rec.chiuso_da = chi or None
                rec.esito = esito
                out.append(_diz(rec))
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    return out


# ---------------------------------------------------------------------------
# Lettura
# ---------------------------------------------------------------------------
def aperte(order_id: str) -> list:
    from .database import get_session
    from .models import LantekCorrezione
    session = get_session()
    try:
        return [_diz(r) for r in session.query(LantekCorrezione).filter(
            LantekCorrezione.order_id == order_id, LantekCorrezione.chiuso_il.is_(None)
        ).order_by(LantekCorrezione.creato_il, LantekCorrezione.codice).all()]
    finally:
        session.close()


def usa_lantek(order_id: str) -> list:
    """I codici per cui chi conferma ha cambiato idea ("va bene Lantek"):
    nella conferma dopo la scelta e' gia' fatta."""
    from .database import get_session
    from .models import LantekCorrezione
    session = get_session()
    try:
        return sorted({r.codice for r in session.query(LantekCorrezione).filter(
            LantekCorrezione.order_id == order_id, LantekCorrezione.esito == 'usa_lantek').all()})
    finally:
        session.close()


def _avvisa_corretto(order_id: str, rec: dict) -> None:
    """Avviso alla postazione laser, una volta sola (corretto_il appena scritto)."""
    try:
        from .accesso import STAZIONI
        from .database import AuditManager, NotificationManager, get_session
        from .models import Order
        import importlib
        s = get_session()
        try:
            o = s.query(Order).filter(Order.id == order_id).first()
            numero = ''
            if o:
                try:      # il numero dell'ordine del cliente (1252), non PREV-...
                    numero = importlib.import_module(__package__ + '.app')._numero_per_cartelle(o) or ''
                except Exception:
                    numero = o.numero_ordine or ''
            chi = ((o.cliente or '') + ' ' + numero).strip() if o else ''
        finally:
            s.close()
        NotificationManager.create_notification(
            user_id=STAZIONI['laser']['utente'], order_id=order_id,
            title='Corretto in Lantek',
            message=f"{rec['codice']} corretto in Lantek: pronto da mandare" + (f' ({chi})' if chi else ''),
            notification_type='alert', notification_category='attiva')
        AuditManager.log(user_name='FerroTrack', action='LANTEK_CORRETTO', entity_type='orders',
                         entity_id=order_id, detail=f"{rec['codice']}: in Lantek ora {descrivi(rec)}"[:2000])
    except Exception:
        logger.warning('avviso "corretto in Lantek" non mandato', exc_info=True)


def stato(order_id: str, in_produzione=None) -> dict:
    """Le correzioni aperte dell'ordine, ricontrollate su Lantek adesso
    (pezzi_in_lantek: sola lettura, con la sua cache di un minuto).

    in_produzione: i codici che hanno gia' l'ordine di produzione in Lantek
    per quest'ordine (dal riassunto), se letti: quelle correzioni si chiudono
    ('mandato'). Ritorna {correzioni: [{codice, stato, cosa, ...}],
    n_correggere, n_corretti, lantek_letto}. stato: 'da_correggere' |
    'corretto' (Lantek coincide: si puo' mandare)."""
    out = {'correzioni': [], 'n_correggere': 0, 'n_corretti': 0, 'lantek_letto': False}
    recs = aperte(order_id)
    if not recs:
        return out
    if in_produzione:
        prod = {str(c).strip().upper() for c in in_produzione}
        fatti = [r['codice'] for r in recs if r['codice'].strip().upper() in prod]
        if fatti:
            chiudi(order_id, fatti, 'mandato', 'FerroTrack')
            recs = [r for r in recs if r['codice'] not in fatti]
            if not recs:
                return out
    from . import lantek as _lt
    try:
        info = _lt.pezzi_in_lantek([r['codice'] for r in recs]) or {}
    except Exception:
        logger.warning('Lantek non letto per le correzioni', exc_info=True)
        info = {}
    letto = bool(info.get('disponibile'))
    out['lantek_letto'] = letto
    pezzi = info.get('pezzi') or {}
    cambi = {}
    for r in recs:
        ok = coincide(r, pezzi.get(r['codice'])) if letto else None
        if ok and not r['corretto_il']:
            cambi[r['id']] = datetime.utcnow()
            r['corretto_il'] = cambi[r['id']].isoformat() + 'Z'
        elif ok is False and r['corretto_il']:
            # ricambiato in Lantek: di nuovo da correggere
            cambi[r['id']] = None
            r['corretto_il'] = None
        # finche' non si sa (Lantek non risponde) resta com'era
        st = 'corretto' if (ok or (ok is None and r['corretto_il'])) else 'da_correggere'
        p = pezzi.get(r['codice']) or {}
        out['correzioni'].append({
            'codice': r['codice'], 'stato': st, 'cosa': descrivi(r),
            'spessore_voluto': r['spessore_voluto'], 'materiale_voluto': r['materiale_voluto'],
            'spessore_lantek': p.get('spessore') if letto and p.get('esiste') else r['spessore_lantek'],
            'materiale_lantek': p.get('materiale') if letto and p.get('esiste') else r['materiale_lantek'],
            'creato_il': r['creato_il'], 'creato_da': r['creato_da'], 'corretto_il': r['corretto_il']})
    if cambi:
        from .database import get_session
        from .models import LantekCorrezione
        session = get_session()
        avvisare = set()
        try:
            for rid, quando in cambi.items():
                rec = session.get(LantekCorrezione, rid)
                if rec is None or rec.chiuso_il is not None:
                    continue
                # l'avviso una volta sola, anche con due letture insieme
                if quando is not None and rec.corretto_il is None:
                    avvisare.add(rid)
                if quando is None or rec.corretto_il is None:
                    rec.corretto_il = quando
            session.commit()
        except Exception:
            session.rollback()
            avvisare = set()
            logger.warning('stato delle correzioni non salvato', exc_info=True)
        finally:
            session.close()
        for r in recs:
            if r['id'] in avvisare:
                _avvisa_corretto(order_id, r)
    out['n_correggere'] = sum(1 for c in out['correzioni'] if c['stato'] == 'da_correggere')
    out['n_corretti'] = sum(1 for c in out['correzioni'] if c['stato'] == 'corretto')
    return out


# ---------------------------------------------------------------------------
# Quantita' cambiate dopo l'invio
# ---------------------------------------------------------------------------
_INVIATE: dict = {}


def _base(c) -> str:
    import re
    return re.sub(r'-\d{2}$', '', str(c or '').strip().upper())


def quantita_inviate(commessa: str, cartella: str) -> dict:
    """{CODICE: pezzi} mandati a Lantek con l'XML Importer per quell'ordine
    (SaleOrder = commessa), sommando tutti gli invii. {} se nessuno."""
    import glob
    import os
    import xml.etree.ElementTree as ET
    commessa = str(commessa or '').strip().upper()
    if not commessa or not cartella:
        return {}
    out = {}
    for f in sorted(glob.glob(os.path.join(glob.escape(cartella), 'Importer log', '*.xml'))):
        try:
            k = (f, os.path.getmtime(f))
        except OSError:
            continue
        righe = _INVIATE.get(k)
        if righe is None:
            righe = []
            try:
                for cmd in ET.parse(f).getroot().iter('COMMAND'):
                    campi = {x.get('FldRef'): x.get('FldValue') for x in cmd.iter('FIELD')}
                    try:
                        q = float(str(campi.get('Quantity') or '0').replace(',', '.'))
                    except ValueError:
                        q = 0
                    righe.append((str(campi.get('SaleOrder') or '').strip().upper(),
                                  str(campi.get('Product') or '').strip().upper(), q))
            except Exception:
                logger.warning('file mandato a Lantek non letto: %s', f, exc_info=True)
            if len(_INVIATE) > 500:
                _INVIATE.clear()
            _INVIATE[k] = righe
        for so, prod, q in righe:
            if so == commessa and prod and q:
                out[prod] = out.get(prod, 0) + q
    return out


def corrette_a_mano(order_id: str) -> dict:
    """{CODICE: {quantita' segnate "corretto in Lantek"}}."""
    from .database import get_session
    from .models import LantekCorrezione
    s = get_session()
    try:
        out = {}
        for r in s.query(LantekCorrezione).filter(LantekCorrezione.order_id == order_id,
                                                  LantekCorrezione.esito == 'quantita_corretta').all():
            if r.quantita_voluta is not None:
                out.setdefault(r.codice.strip().upper(), set()).add(float(r.quantita_voluta))
        return out
    finally:
        s.close()


def quantita_cambiate(order_id: str, righe_q: list, inviate: dict, lantek_ordine: dict | None = None) -> list:
    """I codici gia' mandati a Lantek la cui quantita' e' cambiata dopo.

    righe_q: righe come lantek.righe_quantita (codice, quantita' adesso);
    inviate: quantita_inviate(); lantek_ordine: lantek.pezzi_dell_ordine()
    (aperti + fatti) se Lantek risponde: e' lui a dire cosa c'e' davvero.
    [{codice, tipo: 'quantita', stato: 'da_correggere', cosa, quantita_lantek,
      quantita_voluta, fonte}]"""
    if not righe_q or not (inviate or lantek_ordine):
        return []
    fatte = corrette_a_mano(order_id)

    def trova(diz, cod):
        if not diz:
            return None
        if cod in diz:
            return diz[cod]
        b = _base(cod)
        v = [q for c, q in diz.items() if _base(c) == b]
        return sum(v) if v else None

    out = []
    for r in righe_q:
        cod = str(r.get('codice') or '').strip().upper()
        if not cod:
            continue
        voluta = float(r.get('quantita') or 0)
        if lantek_ordine is not None:
            in_lt, fonte = trova(lantek_ordine, cod), 'lantek'
        else:
            in_lt, fonte = trova(inviate, cod), 'invio'
        if not in_lt or abs(float(in_lt) - voluta) < 0.001:
            continue
        if voluta in fatte.get(cod, set()):
            continue                     # gia' corretto a mano per questo numero
        q_lt = int(in_lt) if float(in_lt).is_integer() else in_lt
        q_v = int(voluta) if voluta.is_integer() else voluta
        out.append({'codice': r.get('codice'), 'tipo': 'quantita', 'stato': 'da_correggere',
                    'cosa': f"quantità: in Lantek {q_lt:g}, ora servono {q_v:g}",
                    'testo': f"In Lantek: {r.get('codice')} ha {q_lt:g}, ora servono {q_v:g} — correggi l'ordine di produzione",
                    'quantita_lantek': q_lt, 'quantita_voluta': q_v, 'fonte': fonte,
                    'spessore_voluto': None, 'materiale_voluto': None, 'spessore_lantek': None,
                    'materiale_lantek': None, 'creato_il': None, 'creato_da': None, 'corretto_il': None})
    return out


def segna_quantita_corretta(order_id: str, codice: str, quantita, quantita_lantek=None, chi: str = '') -> dict:
    """"Corretto in Lantek": la quantita' X del codice e' stata sistemata a
    mano nell'ordine di produzione. Resta scritto (chi, quando, da quanto)."""
    from .database import get_session
    from .models import LantekCorrezione
    ora = datetime.utcnow()
    s = get_session()
    try:
        rec = LantekCorrezione(order_id=order_id, codice=str(codice).strip(), creato_il=ora, creato_da=chi or None,
                               corretto_il=ora, chiuso_il=ora, chiuso_da=chi or None, esito='quantita_corretta',
                               quantita_voluta=float(quantita),
                               quantita_lantek=float(quantita_lantek) if quantita_lantek is not None else None)
        s.add(rec)
        s.commit()
        return _diz(rec)
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
