"""Servizio CONTROLLO MANCANZE sulle dichiarazioni ore.

Cosa distingue (requisito 4):
  'mancante' -> nessuna dichiarazione per quel giorno
  'sotto'    -> ore dichiarate inferiori a quelle attese
  'sopra'    -> ore superiori alle attese (da verificare, MAI cancellate)

Principi implementati:
- NON si assume 8 ore per tutti e tutti i giorni: le attese vengono dalla
  configurazione per operaio (`ore_attese`) e dalle eccezioni giornaliere
  (`eccezioni_giorno`: assenza / giornata ridotta / festivo).
- Una giornata NON dichiarata non equivale a una dichiarata con zero ore.
- Le notifiche sono INTERNE all'applicazione (nessuna email/SMS) e non vengono
  duplicate a ogni avvio: ogni anomalia si notifica una sola volta.
- I controlli arretrati vengono recuperati dopo un periodo di spegnimento
  ripassando una finestra di giorni indietro (idempotente).
- Quando i dati diventano completi l'anomalia si risolve DA SOLA.
"""
import logging
import uuid
from datetime import date, datetime, timedelta

from .database import get_session
from .models import User
from .models_ore import (
    AnomaliaOre, EccezioneGiorno, GiornataOre, OreAttese, RigaOre,
)
from .ore_service import oggi_locale, parse_data

logger = logging.getLogger(__name__)

# Finestra di recupero: a ogni controllo si ripassano gli ultimi N giorni.
# Cosi' dopo uno spegnimento prolungato gli arretrati rientrano da soli, senza
# bisogno di conservare un "ultimo controllo" che potrebbe disallinearsi.
GIORNI_RECUPERO = 30


# ---------------------------------------------------------------------------
# Ore attese
# ---------------------------------------------------------------------------
def _config_operai(session):
    return {a.operatore_id: a for a in session.query(OreAttese).all()}


def _eccezioni(session, dal, al):
    rows = session.query(EccezioneGiorno).filter(
        EccezioneGiorno.data >= dal, EccezioneGiorno.data <= al).all()
    return {(e.operatore_id, e.data): e for e in rows}


def _minuti_attesi(cfg, eccezione, giorno: date):
    """Minuti attesi per (operaio, giorno). None = giornata NON dovuta.

    Ordine: eccezione del giorno > giorni lavorativi configurati > default.
    """
    if cfg is None or not cfg.tenuto_alla_compilazione:
        return None
    if eccezione is not None:
        tipo = (eccezione.tipo or '').lower()
        if tipo in ('assenza', 'festivo'):
            return None                     # giornata non dovuta
        if tipo == 'ridotta':
            m = eccezione.minuti_attesi
            return int(m) if m is not None else int(cfg.minuti_attesi or 0)
    giorni = cfg.giorni_settimana or [1, 2, 3, 4, 5]
    try:
        giorni = [int(g) for g in giorni]
    except Exception:
        giorni = [1, 2, 3, 4, 5]
    if giorno.isoweekday() not in giorni:
        return None                          # giorno non lavorativo
    return int(cfg.minuti_attesi or 0)


def minuti_attesi(operatore_id: str, giorno) -> int | None:
    """Versione pubblica (una sola coppia operaio/giorno)."""
    g = parse_data(giorno)
    if not g:
        return None
    session = get_session()
    try:
        cfg = session.query(OreAttese).filter(
            OreAttese.operatore_id == operatore_id).first()
        ecc = session.query(EccezioneGiorno).filter(
            EccezioneGiorno.operatore_id == operatore_id,
            EccezioneGiorno.data == g).first()
        return _minuti_attesi(cfg, ecc, g)
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Valutazione
# ---------------------------------------------------------------------------
def _minuti_dichiarati(session, operatore_id, giorno):
    """(dichiarata, minuti). `dichiarata=False` se la giornata non esiste."""
    g = session.query(GiornataOre).filter(
        GiornataOre.operatore_id == operatore_id,
        GiornataOre.data == giorno).first()
    if g is None:
        return False, 0
    tot = session.query(RigaOre).filter(RigaOre.giornata_id == g.id).all()
    return True, sum(int(r.minuti or 0) for r in tot)


def _tipo_anomalia(dichiarata, minuti, attesi):
    """Ritorna il tipo di anomalia oppure None se tutto a posto."""
    if attesi is None:
        return None                      # giornata non dovuta: nessuna anomalia
    if not dichiarata:
        return 'mancante'                # diverso da "dichiarata a zero"
    if minuti < attesi:
        return 'sotto'
    if minuti > attesi:
        return 'sopra'
    return None


def _applica(session, operatore_id, giorno, tipo, minuti, attesi):
    """Crea/aggiorna/risolve l'anomalia. Ritorna 'creata'|'aggiornata'|'risolta'|None."""
    a = session.query(AnomaliaOre).filter(
        AnomaliaOre.operatore_id == operatore_id,
        AnomaliaOre.data == giorno).first()

    if tipo is None:
        if a is not None and a.stato == 'aperta':
            a.stato = 'risolta'
            a.risolta_il = datetime.utcnow()
            a.minuti_dichiarati = minuti
            return 'risolta'
        return None

    if a is None:
        session.add(AnomaliaOre(
            id=str(uuid.uuid4()), operatore_id=operatore_id, data=giorno,
            tipo=tipo, minuti_dichiarati=minuti, minuti_attesi=attesi or 0,
            stato='aperta', rilevata_il=datetime.utcnow(), notificata=False,
        ))
        return 'creata'

    cambiata = (a.tipo != tipo or a.stato != 'aperta'
                or a.minuti_dichiarati != minuti or a.minuti_attesi != (attesi or 0))
    a.tipo = tipo
    a.minuti_dichiarati = minuti
    a.minuti_attesi = attesi or 0
    if a.stato != 'aperta':
        # Si riapre: e' di nuovo anomala. Va rinotificata.
        a.stato = 'aperta'
        a.risolta_il = None
        a.notificata = False
    return 'aggiornata' if cambiata else None


def rivaluta_giornata(operatore_id: str, giorno) -> dict:
    """Ricontrolla UNA giornata. Chiamata dopo ogni salvataggio: se i dati sono
    diventati completi l'anomalia si risolve automaticamente."""
    g = parse_data(giorno)
    if not g:
        return {'error': 'data non valida'}
    session = get_session()
    try:
        cfg = session.query(OreAttese).filter(
            OreAttese.operatore_id == operatore_id).first()
        ecc = session.query(EccezioneGiorno).filter(
            EccezioneGiorno.operatore_id == operatore_id,
            EccezioneGiorno.data == g).first()
        attesi = _minuti_attesi(cfg, ecc, g)
        dichiarata, minuti = _minuti_dichiarati(session, operatore_id, g)
        tipo = _tipo_anomalia(dichiarata, minuti, attesi)
        esito = _applica(session, operatore_id, g, tipo, minuti, attesi)
        session.commit()
        return {'success': True, 'tipo': tipo, 'esito': esito}
    except Exception as e:
        session.rollback()
        logger.exception('rivaluta_giornata fallita: %s', e)
        return {'error': 'errore interno'}
    finally:
        session.close()


def controlla_periodo(dal=None, al=None) -> dict:
    """Controlla tutte le giornate dovute nel periodo.

    Default: dagli ultimi GIORNI_RECUPERO giorni fino a IERI (oggi e' ancora in
    corso: segnalarlo sarebbe un falso allarme). Idempotente: rieseguirlo non
    duplica anomalie ne' notifiche.
    """
    oggi = oggi_locale()
    a_giorno = parse_data(al) or (oggi - timedelta(days=1))
    da_giorno = parse_data(dal) or (a_giorno - timedelta(days=GIORNI_RECUPERO - 1))
    if da_giorno > a_giorno:
        return {'creata': 0, 'aggiornata': 0, 'risolta': 0, 'giorni': 0}

    session = get_session()
    esiti = {'creata': 0, 'aggiornata': 0, 'risolta': 0}
    try:
        cfgs = _config_operai(session)
        if not cfgs:
            return {**esiti, 'giorni': 0, 'nota': 'nessun operaio configurato'}
        ecc = _eccezioni(session, da_giorno, a_giorno)
        attivi = {u.id for u in session.query(User).filter(
            User.is_active == True).all()}  # noqa: E712

        giorno = da_giorno
        n_giorni = 0
        while giorno <= a_giorno:
            n_giorni += 1
            for op_id, cfg in cfgs.items():
                if op_id not in attivi:
                    continue
                attesi = _minuti_attesi(cfg, ecc.get((op_id, giorno)), giorno)
                if attesi is None:
                    continue
                dichiarata, minuti = _minuti_dichiarati(session, op_id, giorno)
                tipo = _tipo_anomalia(dichiarata, minuti, attesi)
                r = _applica(session, op_id, giorno, tipo, minuti, attesi)
                if r in esiti:
                    esiti[r] += 1
            giorno += timedelta(days=1)
        session.commit()
        return {**esiti, 'giorni': n_giorni,
                'dal': da_giorno.isoformat(), 'al': a_giorno.isoformat()}
    except Exception as e:
        session.rollback()
        logger.exception('controlla_periodo fallita: %s', e)
        return {'error': 'errore interno'}
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Notifiche interne (mai duplicate)
# ---------------------------------------------------------------------------
_TESTO = {
    'mancante': 'non ha dichiarato le ore',
    'sotto': 'ha dichiarato meno ore del previsto',
    'sopra': 'ha dichiarato piu\' ore del previsto',
}


def notifica_anomalie(limite: int = 50) -> int:
    """Notifica all'IMPIEGATA le anomalie aperte non ancora notificate.

    Una anomalia si notifica UNA SOLA VOLTA (flag `notificata`): riavviare
    l'applicazione o rieseguire il controllo non produce doppioni.
    """
    from .database import NotificationManager
    session = get_session()
    inviate = 0
    try:
        destinatari = [u.id for u in session.query(User).filter(
            User.is_active == True,          # noqa: E712
            User.role == 'Impiegata').all()]
        if not destinatari:
            return 0
        nomi = {u.id: u.name for u in session.query(User).all()}
        aperte = session.query(AnomaliaOre).filter(
            AnomaliaOre.stato == 'aperta',
            AnomaliaOre.notificata == False,  # noqa: E712
        ).order_by(AnomaliaOre.data.desc()).limit(limite).all()

        da_notificare = []
        for a in aperte:
            a.notificata = True
            da_notificare.append({
                'operatore': nomi.get(a.operatore_id, a.operatore_id),
                'data': a.data,
                'tipo': a.tipo,
                'dich': a.minuti_dichiarati,
                'att': a.minuti_attesi,
            })
        session.commit()
    except Exception as e:
        session.rollback()
        logger.exception('notifica_anomalie (marcatura) fallita: %s', e)
        return 0
    finally:
        session.close()

    for n in da_notificare:
        dettaglio = ''
        if n['tipo'] in ('sotto', 'sopra'):
            dettaglio = f" ({n['dich'] / 60:.1f}h invece di {n['att'] / 60:.1f}h)".replace('.', ',')
        for uid in destinatari:
            try:
                NotificationManager.create_notification(
                    user_id=uid, order_id=None,
                    title='Ore da controllare',
                    message=f"{n['operatore']}: {_TESTO.get(n['tipo'], n['tipo'])}"
                            f" il {n['data'].strftime('%d/%m/%Y')}{dettaglio}.",
                    notification_type='ore_anomalia',
                    notification_category='attiva',
                )
                inviate += 1
            except Exception:
                logger.warning('notifica anomalia non inviata a %s', uid)
    return inviate


def controlla_e_notifica() -> dict:
    """Passo completo per il thread di vigilanza: controlla e poi notifica."""
    esiti = controlla_periodo()
    if esiti.get('error'):
        return esiti
    esiti['notifiche'] = notifica_anomalie()
    return esiti


# ---------------------------------------------------------------------------
# Consultazione per l'impiegata
# ---------------------------------------------------------------------------
def elenco_anomalie(stato: str = 'aperta', dal=None, al=None, limite: int = 200) -> list:
    """Anomalie ordinate per data (piu' recenti prima) e operaio."""
    session = get_session()
    try:
        nomi = {u.id: u.name for u in session.query(User).all()}
        q = session.query(AnomaliaOre)
        if stato in ('aperta', 'risolta'):
            q = q.filter(AnomaliaOre.stato == stato)
        d1, d2 = parse_data(dal), parse_data(al)
        if d1:
            q = q.filter(AnomaliaOre.data >= d1)
        if d2:
            q = q.filter(AnomaliaOre.data <= d2)
        rows = q.order_by(AnomaliaOre.data.desc(),
                          AnomaliaOre.operatore_id.asc()).limit(limite).all()
        confermate = {}
        if rows:
            for op, gg in session.query(
                    GiornataOre.operatore_id, GiornataOre.data).filter(
                    GiornataOre.scostamento_confermato == True,  # noqa: E712
                    GiornataOre.data.in_([a.data for a in rows])).all():
                confermate[(op, gg)] = True
        return [{
            'id': a.id,
            'operatore_id': a.operatore_id,
            'operatore': nomi.get(a.operatore_id, a.operatore_id),
            'data': a.data.isoformat(),
            'tipo': a.tipo,
            'minuti_dichiarati': int(a.minuti_dichiarati or 0),
            'minuti_attesi': int(a.minuti_attesi or 0),
            'stato': a.stato,
            'rilevata_il': a.rilevata_il.isoformat() if a.rilevata_il else None,
            # "l'operaio sapeva": distingue una giornata corta confermata
            # (mezza giornata, permesso) da una dimenticanza o da un errore.
            'confermata_dall_operaio': bool(confermate.get((a.operatore_id, a.data))),
        } for a in rows]
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Configurazione ed eccezioni (gestite dall'impiegata)
# ---------------------------------------------------------------------------
def elenco_configurazione() -> list:
    """Chi e' tenuto a compilare, quanto e in quali giorni."""
    session = get_session()
    try:
        cfgs = {c.operatore_id: c for c in session.query(OreAttese).all()}
        out = []
        for u in session.query(User).filter(User.is_active == True).all():  # noqa: E712
            c = cfgs.get(u.id)
            out.append({
                'operatore_id': u.id, 'operatore': u.name, 'ruolo': u.role,
                'configurato': c is not None,
                'tenuto': bool(c.tenuto_alla_compilazione) if c else False,
                'minuti_attesi': int(c.minuti_attesi) if c else 480,
                'giorni_settimana': (c.giorni_settimana if c else [1, 2, 3, 4, 5]),
            })
        out.sort(key=lambda x: (not x['tenuto'], x['operatore'].lower()))
        return out
    finally:
        session.close()


def salva_configurazione(operatore_id, tenuto, minuti_attesi, giorni_settimana,
                         da: str = '') -> dict:
    """Aggiorna la configurazione di un operaio (solo ufficio)."""
    try:
        minuti = int(minuti_attesi)
    except (TypeError, ValueError):
        return {'error': 'minuti attesi non validi'}
    if minuti < 0 or minuti > 1440:
        return {'error': 'minuti attesi fuori intervallo'}
    giorni = []
    for g in (giorni_settimana or []):
        try:
            gi = int(g)
        except (TypeError, ValueError):
            continue
        if 1 <= gi <= 7:
            giorni.append(gi)
    giorni = sorted(set(giorni))

    session = get_session()
    try:
        if not session.query(User).filter(User.id == operatore_id).first():
            return {'error': 'operatore non valido'}
        c = session.query(OreAttese).filter(
            OreAttese.operatore_id == operatore_id).first()
        if c is None:
            c = OreAttese(id=str(uuid.uuid4()), operatore_id=operatore_id)
            session.add(c)
        c.tenuto_alla_compilazione = bool(tenuto)
        c.minuti_attesi = minuti
        c.giorni_settimana = giorni or [1, 2, 3, 4, 5]
        c.aggiornata_il = datetime.utcnow()
        c.aggiornata_da = da or None
        session.commit()
        return {'success': True}
    except Exception as e:
        session.rollback()
        logger.exception('salva_configurazione fallita: %s', e)
        return {'error': 'errore interno'}
    finally:
        session.close()


def salva_eccezione(operatore_id, giorno, tipo, minuti_attesi=None,
                    nota=None, da: str = '') -> dict:
    """Registra assenza / giornata ridotta / festivo. Rivaluta subito il giorno."""
    g = parse_data(giorno)
    if not g:
        return {'error': 'data non valida'}
    tipo = (tipo or '').strip().lower()
    if tipo not in ('assenza', 'ridotta', 'festivo'):
        return {'error': 'tipo non valido (assenza|ridotta|festivo)'}
    minuti = None
    if tipo == 'ridotta':
        try:
            minuti = int(minuti_attesi)
        except (TypeError, ValueError):
            return {'error': 'per una giornata ridotta servono i minuti attesi'}
        if minuti < 0 or minuti > 1440:
            return {'error': 'minuti attesi fuori intervallo'}

    session = get_session()
    try:
        if not session.query(User).filter(User.id == operatore_id).first():
            return {'error': 'operatore non valido'}
        e = session.query(EccezioneGiorno).filter(
            EccezioneGiorno.operatore_id == operatore_id,
            EccezioneGiorno.data == g).first()
        if e is None:
            e = EccezioneGiorno(id=str(uuid.uuid4()), operatore_id=operatore_id, data=g)
            session.add(e)
        e.tipo = tipo
        e.minuti_attesi = minuti
        e.nota = nota
        e.creata_il = datetime.utcnow()
        e.creata_da = da or None
        session.commit()
    except Exception as ex:
        session.rollback()
        logger.exception('salva_eccezione fallita: %s', ex)
        return {'error': 'errore interno'}
    finally:
        session.close()

    rivaluta_giornata(operatore_id, g)   # l'anomalia puo' sparire subito
    return {'success': True}


def elimina_eccezione(operatore_id, giorno) -> dict:
    g = parse_data(giorno)
    if not g:
        return {'error': 'data non valida'}
    session = get_session()
    try:
        session.query(EccezioneGiorno).filter(
            EccezioneGiorno.operatore_id == operatore_id,
            EccezioneGiorno.data == g).delete(synchronize_session=False)
        session.commit()
    except Exception as e:
        session.rollback()
        logger.exception('elimina_eccezione fallita: %s', e)
        return {'error': 'errore interno'}
    finally:
        session.close()
    rivaluta_giornata(operatore_id, g)
    return {'success': True}
