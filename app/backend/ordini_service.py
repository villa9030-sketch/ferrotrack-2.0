"""Ciclo amministrativo degli ORDINI: UNA sola strada dalla fine del lavoro
all'archivio.

L'operaio comunica a voce che ha finito (o il capo preme "Lavoro finito"):
nessuno in officina tocca il resto. E' l'ufficio a registrare i passaggi, e
sono fatti DIVERSI che prima finivano tutti nello stesso stato:

  1. lavorazione completata   -> data_completamento_operativo (+ chi)
  2. DDT registrato           -> ddt_numero / ddt_data
  3. merce consegnata         -> data_consegna_effettiva
  4. fatturato e chiuso       -> numero_fattura / data_fattura + status CHIUSO

La macchina a stati e' questa, senza scorciatoie:

  in lavorazione -> completato ("pronto per DDT") -> DDT registrato
                 -> consegnato (= "da fatturare") -> fatturato e chiuso

Da questi fatti derivano le quattro viste dell'ufficio: ordini aperti,
pronti per DDT (con o senza DDT gia' registrato), consegnati/da fatturare,
archivio. La scheda "Ordini da fatturare" e il suo contatore leggono la
STESSA vista "consegnati": prima erano due strade parallele, e una chiudeva
senza fattura, l'altra senza consegna.

STORICO: la colonna `status` non viene toccata nel suo significato precedente.
Gli ordini gia' in DA_FATTURARE prima di questo intervento non hanno le nuove
date, e verrebbero classificati come "aperti" per errore: per questo `fase()`
guarda ANCHE lo stato storico. Nessun dato viene riscritto per adeguarlo.

DDT: il campo buono e' `ddt_numero`/`ddt_data`. La vecchia scheda di
fatturazione scriveva in `numero_ddt`/`data_ddt`: si legge sempre "il nuovo, o
se manca il vecchio" (vedi `ddt_di`), e chi scrive aggiorna entrambi, cosi'
nessuna schermata vede un DDT diverso da un'altra.

Il DDT e la fattura veri sono emessi da un altro sistema: qui si registra
soltanto il passaggio e il riferimento al documento, non si genera nulla di
fiscale.
"""
import logging
from datetime import datetime

from .database import get_session, AuditManager
from .models import Order, User, Preventivo
from .orario import iso_utc, iso_data, data_calendario

logger = logging.getLogger(__name__)

# Le quattro viste funzionali, nell'ordine in cui un ordine le attraversa.
FASI = ('aperto', 'pronto_ddt', 'consegnato', 'archivio')

ETICHETTE = {
    'aperto': 'Ordini aperti',
    'pronto_ddt': 'Pronti per DDT',
    'consegnato': 'Consegnati / da fatturare',
    'archivio': 'Archivio',
}

# Il passo preciso DENTRO la vista: la vista dice dove guardare, il passo dice
# cosa manca. "Pronti per DDT" contiene sia chi aspetta il DDT sia chi ce l'ha
# gia' e aspetta la consegna; "consegnati" sia chi e' da fatturare sia chi ha
# ancora un residuo da consegnare.
ETICHETTE_PASSO = {
    'in_lavorazione': 'In lavorazione',
    'attesa_ddt': 'Pronto: da preparare il DDT',
    'ddt_registrato': 'DDT registrato: da consegnare',
    'consegna_parziale': 'Consegnato in parte: resta un residuo',
    'da_fatturare': 'Consegnato: da fatturare',
    'chiuso': 'Fatturato e chiuso',
}

# Stati storici che significano "pratica chiusa"
_STATI_ARCHIVIO = ('CHIUSO', 'SPEDITO', 'ARCHIVIATO')
STATI_ARCHIVIO = _STATI_ARCHIVIO
# Stato storico che significava "lavorazione finita, da chiudere in ufficio"
_STATO_LAVORO_FINITO = 'DA_FATTURARE'

# Un riferimento a un documento esterno, non un testo libero.
_MAX_RIFERIMENTO = 60


# ---------------------------------------------------------------------------
# DDT: un campo solo, anche per i dati vecchi
# ---------------------------------------------------------------------------
def ddt_di(order):
    """Numero del DDT: il campo nuovo, o quello della vecchia scheda."""
    return (getattr(order, 'ddt_numero', None)
            or getattr(order, 'numero_ddt', None) or None)


def ddt_data_di(order):
    """Data del DDT, con la stessa regola del numero."""
    return (getattr(order, 'ddt_data', None)
            or getattr(order, 'data_ddt', None) or None)


def imposta_ddt(order, numero, data):
    """Scrive il DDT nel campo buono E in quello vecchio.

    Scriverne uno solo non basta: se si cancellasse il DDT solo dal campo
    nuovo, la lettura "nuovo o vecchio" ritroverebbe il valore vecchio e lo
    farebbe ricomparire.
    """
    numero = (numero or '').strip() or None
    order.ddt_numero = numero
    order.numero_ddt = numero
    order.ddt_data = data if numero else None
    order.data_ddt = data if numero else None


# ---------------------------------------------------------------------------
# Classificazione
# ---------------------------------------------------------------------------
def fase(order) -> str:
    """In quale delle quattro viste ricade l'ordine.

    L'ordine dei controlli va dal fatto piu' avanzato al piu' arretrato: un
    ordine consegnato resta consegnato anche se qualcuno non aveva registrato
    il completamento della lavorazione.

    "Consegnato" vuol dire CONSEGNATO: un DDT registrato da solo non basta,
    perche' questa e' la vista "da fatturare" e si fattura la merce arrivata
    al cliente. L'ordine col solo DDT resta fra i pronti, al passo "DDT
    registrato".
    """
    if (order.status or '') in _STATI_ARCHIVIO:
        return 'archivio'
    if getattr(order, 'data_consegna_effettiva', None):
        return 'consegnato'
    if getattr(order, 'data_completamento_operativo', None) or ddt_di(order):
        return 'pronto_ddt'
    if (order.status or '') == _STATO_LAVORO_FINITO:
        # Ordini chiusi dall'officina PRIMA di questo intervento: la data non
        # c'e', ma la lavorazione era finita davvero. Non vanno fra gli aperti.
        return 'pronto_ddt'
    return 'aperto'


def passo(order) -> str:
    """Il passo della macchina a stati (piu' fine della vista)."""
    f = fase(order)
    if f == 'archivio':
        return 'chiuso'
    if f == 'consegnato':
        return ('consegna_parziale' if getattr(order, 'consegna_parziale', False)
                else 'da_fatturare')
    if f == 'pronto_ddt':
        return 'ddt_registrato' if ddt_di(order) else 'attesa_ddt'
    return 'in_lavorazione'


def _manca_per_chiudere(order):
    """Cosa impedisce di fatturare e chiudere, detto in chiaro. None = niente."""
    if fase(order) == 'archivio':
        return 'Pratica gia\' chiusa.'
    if not getattr(order, 'data_consegna_effettiva', None):
        return 'Manca la consegna: si fattura solo la merce consegnata.'
    if getattr(order, 'consegna_parziale', False):
        return 'Consegna parziale: resta un residuo da consegnare.'
    return None


def stato_taglio(order) -> str:
    """Dove sta l'ordine rispetto al laser, in una parola."""
    if getattr(order, 'taglio_completato', False):
        return 'tagliato'
    richiesto = getattr(order, 'taglio_richiesto', None)
    if richiesto is None:
        return 'da_smistare'
    return 'da_tagliare' if richiesto else 'non_serve'


def filtro_consegnati(query):
    """Filtro SQL equivalente a `fase(o) == 'consegnato'`.

    Lo usa la scheda "Ordini da fatturare" (elenco e contatore): cosi' il
    numero sul badge e la vista "consegnati" non possono piu' divergere.
    """
    return query.filter(
        Order.is_deleted == False,  # noqa: E712
        Order.data_consegna_effettiva.isnot(None),
        (Order.status.is_(None)) | (~Order.status.in_(_STATI_ARCHIVIO)),
    )


def fase_laser(order) -> str:
    """Fase al laser, come i riquadri della pagina laser:
    da_smistare | da_importare | in_lantek | tagliato | non_serve."""
    st = stato_taglio(order)
    if st == 'da_tagliare':
        return 'in_lantek' if getattr(order, 'importato_lantek_il', None) else 'da_importare'
    return st


def _numero_cliente(order, cache: dict | None = None) -> str | None:
    """Numero dell'ordine del CLIENTE (es. 1252) per gli ordini da preventivo."""
    pid = getattr(order, 'preventivo_id_origine', None)
    if not pid:
        return None
    if cache is not None and pid in cache:
        return cache[pid]
    try:
        from sqlalchemy.orm import object_session
        sess = object_session(order)
        p = sess.query(Preventivo.numero_ordine_cliente).filter(Preventivo.id == pid).first() if sess else None
        n = ((p[0] if p else '') or '').strip() or None
    except Exception:
        n = None
    if cache is not None:
        cache[pid] = n
    return n


def _riga(order, nomi: dict, numcli: dict | None = None) -> dict:
    """Dati che servono all'ufficio per decidere, senza fasi produttive.

    Gli istanti (completamento, consegna, taglio, chiusura) escono in UTC con
    la "Z": il browser li mostra nell'ora locale. Le date di calendario
    (consegna prevista, DDT, fattura) escono senza fuso.
    """
    f = fase(order)
    p = passo(order)
    ddt = ddt_di(order)
    ddt_data = iso_data(ddt_data_di(order))
    completato_da = getattr(order, 'completato_operativo_da', None)
    consegnato_da = getattr(order, 'consegna_registrata_da', None)
    chiuso_da = getattr(order, 'chiuso_da', None)
    consegna = iso_utc(getattr(order, 'data_consegna_effettiva', None))
    manca = _manca_per_chiudere(order)
    return {
        'id': order.id,
        'numero_ordine': order.numero_ordine or order.id[:8],
        'numero_ordine_cliente': _numero_cliente(order, numcli),
        'cliente': order.cliente or '',
        'fase': f,
        'fase_etichetta': ETICHETTE[f],
        'passo': p,
        'passo_etichetta': ETICHETTE_PASSO[p],
        'status': order.status,
        'data_ricezione': iso_utc(order.data_ricezione),
        'data_consegna': iso_data(order.data_consegna),
        'note': order.note or '',
        'prezzo_quotato': order.prezzo_quotato,
        'origine': getattr(order, 'origine', None) or 'PDF',
        'preventivo_id_origine': getattr(order, 'preventivo_id_origine', None),
        'lotto_numero': order.lotto_numero or 0,
        'lotto_nome': order.lotto_nome or '',
        'parent_order_id': order.parent_order_id,
        # Unico segnale su dove sia un ordine ancora aperto: al laser o gia'
        # in officina. Lo marca il laser, non blocca nulla, ma all'ufficio
        # serve per rispondere al cliente che chiede "a che punto siamo".
        'taglio_fatto': bool(getattr(order, 'taglio_completato', False)),
        'taglio_richiesto': getattr(order, 'taglio_richiesto', None),
        'taglio_stato': stato_taglio(order),
        'taglio_il': iso_utc(getattr(order, 'data_taglio_completato', None)),
        # Laser in dettaglio (riquadri e storia dell'ordine per l'ufficio)
        'laser_fase': fase_laser(order),
        'smistato_il': iso_utc(getattr(order, 'smistato_il', None)),
        'importato_lantek_il': iso_utc(getattr(order, 'importato_lantek_il', None)),
        'data_taglio_pianificata': iso_data(getattr(order, 'data_taglio_pianificata', None)),
        'durata_laser_manuale_min': getattr(order, 'durata_laser_manuale_min', None),
        'taglio_completato': bool(getattr(order, 'taglio_completato', False)),
        'data_taglio_completato': iso_utc(getattr(order, 'data_taglio_completato', None)),
        # I fatti, distinti
        'completamento': iso_utc(getattr(order, 'data_completamento_operativo', None)),
        'completato_da_id': completato_da,
        'completato_da': nomi.get(completato_da or '', completato_da),
        # DDT unificato: stesso valore con i due nomi, perche' le schermate
        # vecchie leggono ancora numero_ddt/data_ddt.
        'ddt_numero': ddt,
        'ddt_data': ddt_data,
        'numero_ddt': ddt,
        'data_ddt': ddt_data,
        'consegna': consegna,
        'data_consegna_effettiva': consegna,
        'consegna_registrata_da_id': consegnato_da,
        'consegna_registrata_da': nomi.get(consegnato_da or '', consegnato_da),
        'consegna_parziale': bool(getattr(order, 'consegna_parziale', False)),
        'note_consegna': getattr(order, 'note_consegna', None) or '',
        'numero_fattura': getattr(order, 'numero_fattura', None),
        'data_fattura': iso_data(getattr(order, 'data_fattura', None)),
        'note_chiusura': getattr(order, 'note_chiusura', None) or '',
        'data_chiusura_amministrativa': iso_utc(
            getattr(order, 'data_chiusura_amministrativa', None)),
        'chiuso_da_id': chiuso_da,
        'chiuso_da': nomi.get(chiuso_da or '', chiuso_da),
        # Si puo' fatturare e chiudere? Se no, perche'.
        'chiudibile': manca is None,
        'manca_per_chiudere': manca,
    }


def _nomi(session) -> dict:
    return {u.id: u.name for u in session.query(User).all()}


def elenco(fase_richiesta: str = None, cliente: str = None, limite: int = 500) -> dict:
    """Ordini raggruppati per fase, con i conteggi di tutte le viste.

    I conteggi arrivano sempre completi: le linguette devono mostrare il numero
    anche delle viste non aperte.
    """
    session = get_session()
    try:
        nomi = _nomi(session)
        q = session.query(Order).filter(Order.is_deleted == False)  # noqa: E712
        if cliente:
            q = q.filter(Order.cliente == cliente)
        ordini = q.order_by(Order.data_consegna.asc()).all()
        # numeri dei clienti in una richiesta sola
        pids = {o.preventivo_id_origine for o in ordini if getattr(o, 'preventivo_id_origine', None)}
        numcli = {pid: (n or '').strip() or None for pid, n in session.query(
            Preventivo.id, Preventivo.numero_ordine_cliente).filter(Preventivo.id.in_(pids)).all()} if pids else {}
        righe = [_riga(o, nomi, numcli) for o in ordini]

        conteggi = {f: 0 for f in FASI}
        for r in righe:
            conteggi[r['fase']] += 1

        if fase_richiesta in FASI:
            righe = [r for r in righe if r['fase'] == fase_richiesta]
        return {'ordini': righe[:limite], 'conteggi': conteggi,
                'totale': len(righe), 'etichette': ETICHETTE,
                'etichette_passo': ETICHETTE_PASSO}
    finally:
        session.close()


def conta_da_fatturare() -> int:
    """Quanti ordini sono consegnati e non ancora fatturati (badge)."""
    session = get_session()
    try:
        return filtro_consegnati(session.query(Order)).count()
    finally:
        session.close()


def riga_ordine(order_id: str):
    """La riga di un singolo ordine, come nelle viste. None se non esiste."""
    session = get_session()
    try:
        o = _carica(session, order_id)
        return _riga(o, _nomi(session)) if o else None
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Transizioni — riservate all'ufficio (il completamento anche al capo)
# ---------------------------------------------------------------------------
def _carica(session, order_id):
    o = session.query(Order).filter(Order.id == order_id,
                                    Order.is_deleted == False).first()  # noqa: E712
    return o


def _audit(user_id, azione, order_id, dettaglio):
    try:
        AuditManager.log(user_id=user_id, action=azione, entity_type='order',
                         entity_id=order_id, detail=dettaglio)
    except Exception:
        pass


def _leggi_data(valore, cosa):
    """(datetime a mezzanotte, None) oppure (None, errore) per una data AAAA-MM-GG."""
    try:
        return data_calendario(valore), None
    except (TypeError, ValueError):
        return None, {'error': f'Data {cosa} non valida: usa il formato AAAA-MM-GG.',
                      'codice': 'data_non_valida'}


def registra_completamento(order_id: str, user_id: str, quando=None) -> dict:
    """L'officina ha finito: l'ordine passa alla preparazione del DDT.

    Ripetere l'operazione non cambia nulla (idempotente): il primo che la
    registra resta l'autore, e `gia_registrato` lo dice a chi chiama.
    """
    session = get_session()
    try:
        o = _carica(session, order_id)
        if not o:
            return {'error': 'Ordine non trovato', 'codice': 'non_trovato'}
        if fase(o) == 'archivio':
            return {'error': 'Ordine gia\' archiviato: riaprilo prima di modificarlo.',
                    'codice': 'archiviato'}
        if getattr(o, 'data_completamento_operativo', None):
            return {'success': True, 'gia_registrato': True,
                    'ordine': _riga(o, _nomi(session))}
        o.data_completamento_operativo = quando or datetime.utcnow()
        o.completato_operativo_da = user_id or None
        # Lo stato storico resta allineato: chi legge `status` vede quello che
        # vedeva prima quando l'officina dichiarava finito.
        if (o.status or '') not in _STATI_ARCHIVIO:
            o.status = _STATO_LAVORO_FINITO
        session.commit()
        session.refresh(o)
        out = _riga(o, _nomi(session))
    except Exception as e:
        session.rollback()
        logger.exception('registra_completamento fallita: %s', e)
        return {'error': 'Errore interno durante la registrazione. Riprova.',
                'codice': 'errore_interno'}
    finally:
        session.close()
    _audit(user_id, 'ORDINE_COMPLETAMENTO', order_id, 'Lavorazione completata')
    return {'success': True, 'ordine': out}


def annulla_completamento(order_id: str, user_id: str) -> dict:
    """Correzione: l'ordine torna fra quelli aperti. Consentita solo finche'
    non e' stato registrato un DDT o una consegna, altrimenti si creerebbe uno
    stato incoerente."""
    session = get_session()
    try:
        o = _carica(session, order_id)
        if not o:
            return {'error': 'Ordine non trovato', 'codice': 'non_trovato'}
        if ddt_di(o) or getattr(o, 'data_consegna_effettiva', None):
            return {'error': 'Ci sono gia\' un DDT o una consegna registrati: '
                             'annulla prima quelli.', 'codice': 'sequenza'}
        o.data_completamento_operativo = None
        o.completato_operativo_da = None
        if (o.status or '') == _STATO_LAVORO_FINITO:
            o.status = 'RICEVUTO'
        session.commit()
        session.refresh(o)
        out = _riga(o, _nomi(session))
    except Exception as e:
        session.rollback()
        logger.exception('annulla_completamento fallita: %s', e)
        return {'error': 'Errore interno. Riprova.', 'codice': 'errore_interno'}
    finally:
        session.close()
    _audit(user_id, 'ORDINE_COMPLETAMENTO_ANNULLATO', order_id, 'Riportato fra gli aperti')
    return {'success': True, 'ordine': out}


def _lavoro_finito(o) -> bool:
    return bool(getattr(o, 'data_completamento_operativo', None)
                or (o.status or '') == _STATO_LAVORO_FINITO)


def registra_ddt(order_id: str, user_id: str, numero: str, data=None) -> dict:
    """Registra il riferimento del DDT emesso altrove. Non genera documenti.

    `data` e' la data del documento (AAAA-MM-GG); se manca e' OGGI in Italia,
    non in UTC: alle 00:30 il DDT e' di oggi, non di ieri.
    """
    numero = (numero or '').strip()
    if not numero:
        return {'error': 'Numero DDT obbligatorio', 'codice': 'numero_mancante'}
    if len(numero) > _MAX_RIFERIMENTO:
        return {'error': 'Numero DDT troppo lungo', 'codice': 'numero_lungo'}
    quando, errore = _leggi_data(data, 'del DDT')
    if errore:
        return errore
    session = get_session()
    try:
        o = _carica(session, order_id)
        if not o:
            return {'error': 'Ordine non trovato', 'codice': 'non_trovato'}
        if fase(o) == 'archivio':
            return {'error': 'Ordine gia\' archiviato: riaprilo prima di modificarlo.',
                    'codice': 'archiviato'}
        if not _lavoro_finito(o):
            return {'error': 'Registra prima il completamento della lavorazione.',
                    'codice': 'sequenza'}
        imposta_ddt(o, numero, quando)
        session.commit()
        session.refresh(o)
        out = _riga(o, _nomi(session))
    except Exception as e:
        session.rollback()
        logger.exception('registra_ddt fallita: %s', e)
        return {'error': 'Errore interno. Riprova.', 'codice': 'errore_interno'}
    finally:
        session.close()
    _audit(user_id, 'ORDINE_DDT', order_id, f'DDT {numero}')
    return {'success': True, 'ordine': out}


def registra_consegna(order_id: str, user_id: str, completa: bool = True,
                      note: str = '', data=None) -> dict:
    """Merce consegnata. `completa=False` = consegna PARZIALE: l'ordine resta
    con un residuo e non potra' essere archiviato finche' non si registra il
    completamento della consegna.
    """
    session = get_session()
    try:
        o = _carica(session, order_id)
        if not o:
            return {'error': 'Ordine non trovato', 'codice': 'non_trovato'}
        if fase(o) == 'archivio':
            return {'error': 'Ordine gia\' archiviato: riaprilo prima di modificarlo.',
                    'codice': 'archiviato'}
        if not _lavoro_finito(o):
            return {'error': 'Registra prima il completamento della lavorazione.',
                    'codice': 'sequenza'}
        if not completa and not (note or '').strip():
            return {'error': 'Per una consegna parziale scrivi cosa resta da consegnare.',
                    'codice': 'note_mancanti'}
        o.data_consegna_effettiva = data or datetime.utcnow()
        o.consegna_registrata_da = user_id or None
        o.consegna_parziale = not completa
        o.note_consegna = (note or '').strip() or None
        session.commit()
        session.refresh(o)
        out = _riga(o, _nomi(session))
    except Exception as e:
        session.rollback()
        logger.exception('registra_consegna fallita: %s', e)
        return {'error': 'Errore interno. Riprova.', 'codice': 'errore_interno'}
    finally:
        session.close()
    _audit(user_id, 'ORDINE_CONSEGNA', order_id,
           'Consegna ' + ('completa' if completa else f'PARZIALE: {note}'))
    return {'success': True, 'ordine': out}


def chiudi_pratica(order_id: str, user_id: str, numero_fattura: str = None,
                   data_fattura=None, note: str = None) -> dict:
    """Fatturato: il ciclo amministrativo e' concluso e l'ordine va in archivio.

    E' l'UNICO modo di chiudere una pratica, e chiede due cose:
      - la merce consegnata per intero (un residuo non e' una pratica chiusa)
      - il numero della fattura (la data, se manca, e' oggi)
    Prima "Chiudi pratica" archiviava senza fattura e la vecchia chiusura
    amministrativa senza consegna: ognuna saltava un passaggio diverso.
    """
    numero_fattura = (numero_fattura or '').strip()
    if len(numero_fattura) > _MAX_RIFERIMENTO:
        return {'error': 'Numero fattura troppo lungo', 'codice': 'numero_lungo'}
    session = get_session()
    try:
        o = _carica(session, order_id)
        if not o:
            return {'error': 'Ordine non trovato', 'codice': 'non_trovato'}
        if fase(o) == 'archivio':
            return {'error': 'Pratica gia\' chiusa e archiviata.',
                    'codice': 'archiviato'}
        if getattr(o, 'consegna_parziale', False):
            return {'error': 'La consegna e\' ancora parziale: c\'e\' un residuo da '
                             'consegnare. Registra la consegna completa prima di archiviare.',
                    'codice': 'consegna_parziale'}
        if not getattr(o, 'data_consegna_effettiva', None):
            return {'error': 'Registra prima la consegna della merce: si fattura '
                             'e si chiude solo un ordine consegnato.',
                    'codice': 'sequenza'}
        if not numero_fattura:
            return {'error': 'Numero fattura obbligatorio per chiudere la pratica.',
                    'codice': 'fattura_mancante'}
        quando, errore = _leggi_data(data_fattura, 'della fattura')
        if errore:
            return errore
        o.numero_fattura = numero_fattura
        o.data_fattura = quando
        if (note or '').strip():
            o.note_chiusura = note.strip()
        o.data_chiusura_amministrativa = datetime.utcnow()
        o.chiuso_da = user_id or None
        o.status = 'CHIUSO'
        session.commit()
        session.refresh(o)
        out = _riga(o, _nomi(session))
    except Exception as e:
        session.rollback()
        logger.exception('chiudi_pratica fallita: %s', e)
        return {'error': 'Errore interno. Riprova.', 'codice': 'errore_interno'}
    finally:
        session.close()
    _audit(user_id, 'ORDINE_CHIUSO', order_id, f'Fatturato (fattura {numero_fattura}): archiviato')
    return {'success': True, 'ordine': out}


def salva_bozza(order_id: str, dati: dict) -> dict:
    """Salva DDT e dati fattura senza chiudere (scheda "Ordini da fatturare").

    Il DDT finisce nel campo buono come con `registra_ddt`; la fattura resta
    una bozza finche' `chiudi_pratica` non archivia.
    """
    session = get_session()
    try:
        o = _carica(session, order_id)
        if not o:
            return {'success': False, 'error': 'Ordine non trovato', 'codice': 'non_trovato'}
        if fase(o) == 'archivio':
            return {'success': False, 'codice': 'archiviato',
                    'error': 'Ordine gia\' archiviato: riaprilo prima di modificarlo.'}
        if not _lavoro_finito(o) and not getattr(o, 'data_consegna_effettiva', None):
            return {'success': False, 'codice': 'sequenza',
                    'error': 'Registra prima il completamento della lavorazione.'}
        if 'numero_ddt' in dati or 'ddt_numero' in dati:
            numero = (dati.get('ddt_numero') if 'ddt_numero' in dati
                      else dati.get('numero_ddt')) or ''
            if len(numero.strip()) > _MAX_RIFERIMENTO:
                return {'success': False, 'error': 'Numero DDT troppo lungo',
                        'codice': 'numero_lungo'}
            valore = dati.get('ddt_data') if 'ddt_data' in dati else dati.get('data_ddt')
            quando, errore = _leggi_data(valore, 'del DDT')
            if errore:
                return {'success': False, **errore}
            imposta_ddt(o, numero, quando)
        if 'numero_fattura' in dati:
            o.numero_fattura = (dati.get('numero_fattura') or '').strip() or None
        if 'data_fattura' in dati:
            if dati.get('data_fattura'):
                quando, errore = _leggi_data(dati.get('data_fattura'), 'della fattura')
                if errore:
                    return {'success': False, **errore}
                o.data_fattura = quando
            else:
                o.data_fattura = None
        if 'note_chiusura' in dati:
            o.note_chiusura = dati.get('note_chiusura') or None
        session.commit()
        session.refresh(o)
        out = _riga(o, _nomi(session))
    except Exception as e:
        session.rollback()
        logger.exception('salva_bozza fallita: %s', e)
        return {'success': False, 'error': 'Errore interno. Riprova.',
                'codice': 'errore_interno'}
    finally:
        session.close()
    return {'success': True, 'order_id': order_id, 'ordine': out}


def riapri(order_id: str, user_id: str) -> dict:
    """Riporta un ordine archiviato fra i consegnati, per correggere un errore.

    I dati della fattura restano: di solito si riapre proprio per correggerli.
    """
    session = get_session()
    try:
        o = _carica(session, order_id)
        if not o:
            return {'error': 'Ordine non trovato', 'codice': 'non_trovato'}
        if (o.status or '') not in _STATI_ARCHIVIO:
            return {'error': 'L\'ordine non e\' archiviato.', 'codice': 'non_archiviato'}
        o.status = _STATO_LAVORO_FINITO
        o.data_chiusura_amministrativa = None
        o.chiuso_da = None
        session.commit()
        session.refresh(o)
        out = _riga(o, _nomi(session))
    except Exception as e:
        session.rollback()
        logger.exception('riapri fallita: %s', e)
        return {'error': 'Errore interno. Riprova.', 'codice': 'errore_interno'}
    finally:
        session.close()
    _audit(user_id, 'ORDINE_RIAPERTO', order_id, 'Riportato fra i consegnati')
    return {'success': True, 'ordine': out}
