"""Copia automatica degli abbinamenti Lantek (tabella assiemi_lantek).

Gli abbinamenti "riga dell'ordine del cliente -> pezzi Lantek" li fa Stefano
UNA volta (ordini solo PDF, Poliform/B&B/assiemi DECA) e servono per tutte le
forniture dopo: non si devono perdere se il database viene ripulito o rifatto.
Prima la copia si faceva a mano (tools/abbinamenti_lantek.py esporta); dal
15/10/2026 la fa l'app:

- ogni volta che un abbinamento si crea, cambia o si toglie (ordine_pdf_lantek);
- all'avvio, se il file manca o non corrisponde piu' alla tabella;
- all'avvio, se la tabella e' VUOTA ma il file ha degli abbinamenti, NON si
  sovrascrive niente: si scrive nel log e si avvisa l'amministrazione, che li
  rimette con "Ripristina" (Admin -> Backup) o con il tool.

Stessi file e stesso formato del tool: dati_clienti/abbinamenti_lantek.json
({tabella, colonne, righe}) e .csv (separatore ';', UTF-8 con BOM per Excel).
Scrittura atomica (file temporaneo + os.replace). Se una nuova copia ha meno
abbinamenti della precedente (uno tolto, o tabella rifatta a meta'), la copia
di prima resta in dati_clienti/storico_abbinamenti/ (le ultime 30).
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import threading
import uuid
from datetime import datetime

logger = logging.getLogger(__name__)

_APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# FERROTRACK_DATI_CLIENTI: un'altra cartella (istanze di prova su una copia)
CARTELLA = os.environ.get('FERROTRACK_DATI_CLIENTI') or os.path.join(_APP, 'dati_clienti')
NOME = 'abbinamenti_lantek'
TIPO_AVVISO = 'abbinamenti_lantek_ripristino'
STORICO_MAX = 30
_LOCK = threading.Lock()


def percorso_json() -> str:
    return os.path.join(CARTELLA, NOME + '.json')


def percorso_csv() -> str:
    return os.path.join(CARTELLA, NOME + '.csv')


def _colonne() -> list[str]:
    from .models import AssiemeLantek
    return [c.name for c in AssiemeLantek.__table__.columns]


def _valore(v):
    # come il tool (json default=str): le date diventano "2026-10-07 12:00:00"
    if isinstance(v, datetime):
        return str(v)
    return v


def righe_tabella() -> list[dict]:
    """Gli abbinamenti nel database, nello stesso ordine del tool."""
    from .models import get_session, AssiemeLantek
    cols = _colonne()
    s = get_session()
    try:
        q = s.query(AssiemeLantek).order_by(AssiemeLantek.cliente, AssiemeLantek.codice,
                                            AssiemeLantek.pezzo_lantek)
        return [{c: _valore(getattr(a, c)) for c in cols} for a in q.all()]
    finally:
        s.close()


def leggi_file() -> dict | None:
    """Il contenuto della copia ({tabella, colonne, righe}), None se manca o e' illeggibile."""
    try:
        with open(percorso_json(), encoding='utf-8') as f:
            dati = json.load(f)
        return dati if isinstance(dati, dict) and isinstance(dati.get('righe'), list) else None
    except FileNotFoundError:
        return None
    except Exception:
        logger.warning('copia degli abbinamenti Lantek illeggibile: %s', percorso_json(), exc_info=True)
        return None


def _chiave(r: dict) -> tuple:
    return (str(r.get('codice') or '').upper(), r.get('pezzo_lantek') or '')


def _scrivi_atomico(percorso: str, testo: str, encoding: str) -> None:
    tmp = f'{percorso}.{uuid.uuid4().hex[:8]}.tmp'
    try:
        with open(tmp, 'w', encoding=encoding, newline='') as f:
            f.write(testo)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, percorso)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _metti_da_parte(vecchia: dict) -> None:
    """La copia di prima nello storico (le ultime STORICO_MAX)."""
    cart = os.path.join(CARTELLA, 'storico_abbinamenti')
    os.makedirs(cart, exist_ok=True)
    nome = f"{NOME}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
    _scrivi_atomico(os.path.join(cart, nome),
                    json.dumps(vecchia, ensure_ascii=False, indent=1, default=str), 'utf-8')
    vecchie = sorted(f for f in os.listdir(cart) if f.startswith(NOME + '_') and f.endswith('.json'))
    for f in vecchie[:-STORICO_MAX]:
        try:
            os.remove(os.path.join(cart, f))
        except OSError:
            pass


def esporta(motivo: str = '') -> int:
    """Scrive la copia (json + csv) dalla tabella. Ritorna quante righe."""
    with _LOCK:
        cols = _colonne()
        righe = righe_tabella()
        os.makedirs(CARTELLA, exist_ok=True)
        vecchia = leggi_file()
        if vecchia and {_chiave(r) for r in vecchia['righe']} - {_chiave(r) for r in righe}:
            _metti_da_parte(vecchia)
        _scrivi_atomico(percorso_json(),
                        json.dumps({'tabella': 'assiemi_lantek', 'colonne': cols, 'righe': righe},
                                   ensure_ascii=False, indent=1, default=str), 'utf-8')
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=cols, delimiter=';', lineterminator='\r\n')
        w.writeheader()
        w.writerows(righe)
        _scrivi_atomico(percorso_csv(), buf.getvalue(), 'utf-8-sig')
    logger.info('abbinamenti Lantek: copia aggiornata (%d righe)%s', len(righe),
                f' — {motivo}' if motivo else '')
    return len(righe)


def esporta_sicuro(motivo: str = '') -> None:
    """esporta() che non fa mai fallire chi l'ha chiamata (il salvataggio
    dell'abbinamento e' gia' fatto: la copia e' in piu')."""
    try:
        esporta(motivo)
    except Exception:
        logger.warning('copia degli abbinamenti Lantek non aggiornata (%s)', motivo, exc_info=True)


def stato() -> dict:
    """Quanti abbinamenti in tabella e nella copia, e se c'e' da ripristinare."""
    righe = righe_tabella()
    dati = leggi_file()
    nel_file = (dati or {}).get('righe') or []
    mancanti = {_chiave(r) for r in nel_file} - {_chiave(r) for r in righe}
    agg = None
    if os.path.exists(percorso_json()):
        agg = datetime.fromtimestamp(os.path.getmtime(percorso_json())).isoformat(timespec='seconds')
    return {'n_tabella': len(righe), 'n_file': len(nel_file), 'file': percorso_json(),
            'file_esiste': dati is not None, 'file_aggiornato_il': agg,
            'n_mancanti': len(mancanti),
            'da_ripristinare': not righe and bool(nel_file)}


def ripristina(chi: str = '') -> int:
    """Rimette nel database gli abbinamenti della copia che mancano (stesso
    codice + pezzo Lantek: non duplica). Come `tools/abbinamenti_lantek.py importa`."""
    from .models import get_session, AssiemeLantek
    dati = leggi_file()
    if not dati:
        raise FileNotFoundError(percorso_json())
    cols = set(_colonne())
    s = get_session()
    n = 0
    try:
        esistenti = {(a.codice.upper(), a.pezzo_lantek or '') for a in s.query(AssiemeLantek).all()}
        for r in dati['righe']:
            if not isinstance(r, dict) or not r.get('codice') or _chiave(r) in esistenti:
                continue
            v = {k: r.get(k) for k in cols if k in r}
            v['codice'] = str(v['codice']).upper()
            v['id'] = v.get('id') or str(uuid.uuid4())
            if s.get(AssiemeLantek, v['id']) is not None:
                v['id'] = str(uuid.uuid4())
            creato = v.get('creato_il')
            if isinstance(creato, str):
                try:
                    v['creato_il'] = datetime.fromisoformat(creato)
                except ValueError:
                    v['creato_il'] = None
            v['quantita'] = int(v.get('quantita') or 1)
            v['non_laser'] = bool(v.get('non_laser'))
            if chi and not v.get('creato_da'):
                v['creato_da'] = chi
            s.add(AssiemeLantek(**v))
            esistenti.add(_chiave(r))
            n += 1
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
    if n:
        _chiudi_avvisi()
    esporta_sicuro(f'ripristino di {n} abbinamenti')
    return n


def _destinatari() -> list[str]:
    from .models import get_session, User, RUOLI_UFFICIO
    s = get_session()
    try:
        attivi = s.query(User).filter(User.is_active == True)   # noqa: E712
        admin = [u.id for u in attivi.filter(User.e_admin == True).all()]   # noqa: E712
        return admin or [u.id for u in attivi.filter(User.role.in_(RUOLI_UFFICIO)).all()]
    finally:
        s.close()


def _avviso_aperto() -> bool:
    from .models import get_session, Notification
    s = get_session()
    try:
        return s.query(Notification.id).filter(Notification.notification_type == TIPO_AVVISO,
                                               Notification.is_deleted == False,   # noqa: E712
                                               Notification.is_read == False).first() is not None   # noqa: E712
    finally:
        s.close()


def _chiudi_avvisi() -> None:
    from .models import get_session, Notification
    s = get_session()
    try:
        s.query(Notification).filter(Notification.notification_type == TIPO_AVVISO,
                                     Notification.is_read == False).update(   # noqa: E712
            {Notification.is_read: True}, synchronize_session=False)
        s.commit()
    except Exception:
        s.rollback()
    finally:
        s.close()


def avvio() -> str:
    """Controllo all'avvio dell'app. Ritorna cosa ha fatto:
    'esportato' | 'gia_allineato' | 'da_ripristinare' | 'niente'."""
    righe = righe_tabella()
    dati = leggi_file()
    nel_file = (dati or {}).get('righe') or []
    if not righe:
        if nel_file:
            # Tabella vuota ma copia piena: database ripulito o rifatto. Non si
            # sovrascrive la copia: lo decide l'amministrazione.
            logger.warning('abbinamenti Lantek: la tabella e\' VUOTA ma la copia %s ne ha %d. '
                           'Non la sovrascrivo: rimettili da Admin -> Backup -> "Ripristina" '
                           '(o tools/abbinamenti_lantek.py importa).', percorso_json(), len(nel_file))
            if not _avviso_aperto():
                from .database import NotificationManager
                for uid in _destinatari():
                    NotificationManager.create_notification(
                        user_id=uid, order_id=None,
                        title='Abbinamenti Lantek da ripristinare',
                        message=(f'Nel database non c\'è nessun abbinamento Lantek, ma la copia ne ha '
                                 f'{len(nel_file)}. Per rimetterli: Admin → Backup → "Ripristina gli abbinamenti".'),
                        notification_type=TIPO_AVVISO, notification_category='attiva')
            return 'da_ripristinare'
        return 'niente'
    if dati is not None and [_normalizza(r) for r in nel_file] == [_normalizza(r) for r in righe]:
        return 'gia_allineato'
    esporta('avvio: copia mancante o non aggiornata')
    return 'esportato'


def _normalizza(r: dict) -> dict:
    return {k: (None if v is None else str(v)) for k, v in r.items()}


def avvio_sicuro() -> None:
    try:
        esito = avvio()
        if esito != 'gia_allineato':
            logger.info('abbinamenti Lantek all\'avvio: %s', esito)
    except Exception:
        logger.warning('controllo degli abbinamenti Lantek all\'avvio non riuscito', exc_info=True)
