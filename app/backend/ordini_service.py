"""Ciclo amministrativo degli ORDINI gestito dall'impiegata.

L'operaio comunica a voce che ha finito: nessuno in officina tocca lo stato di
un ordine. E' l'ufficio a registrare i passaggi, e sono quattro fatti DIVERSI
che prima finivano tutti nello stesso stato:

  1. lavorazione completata   -> data_completamento_operativo
  2. DDT preparato            -> ddt_numero / ddt_data
  3. merce consegnata         -> data_consegna_effettiva
  4. ciclo amministrativo concluso -> status CHIUSO

Da questi quattro fatti derivano le quattro viste dell'ufficio: ordini aperti,
pronti per DDT, consegnati/da fatturare, archivio.

STORICO: la colonna `status` non viene toccata nel suo significato precedente.
Gli ordini gia' in DA_FATTURARE prima di questo intervento non hanno le nuove
date, e verrebbero classificati come "aperti" per errore: per questo `fase()`
guarda ANCHE lo stato storico. Nessun dato viene riscritto per adeguarlo.

Il DDT vero e' emesso da un altro sistema: qui si registra soltanto il
passaggio e il riferimento al documento, non si genera nulla di fiscale.
"""
import logging
from datetime import datetime

from .database import get_session, AuditManager
from .models import Order, User

logger = logging.getLogger(__name__)

# Le quattro viste funzionali, nell'ordine in cui un ordine le attraversa.
FASI = ('aperto', 'pronto_ddt', 'consegnato', 'archivio')

ETICHETTE = {
    'aperto': 'Ordini aperti',
    'pronto_ddt': 'Pronti per DDT',
    'consegnato': 'Consegnati / da fatturare',
    'archivio': 'Archivio',
}

# Stati storici che significano "pratica chiusa"
_STATI_ARCHIVIO = ('CHIUSO', 'SPEDITO', 'ARCHIVIATO')
# Stato storico che significava "lavorazione finita, da chiudere in ufficio"
_STATO_LAVORO_FINITO = 'DA_FATTURARE'


# ---------------------------------------------------------------------------
# Classificazione
# ---------------------------------------------------------------------------
def fase(order) -> str:
    """In quale delle quattro viste ricade l'ordine.

    L'ordine dei controlli va dal fatto piu' avanzato al piu' arretrato: un
    ordine consegnato resta consegnato anche se qualcuno non aveva registrato
    il completamento della lavorazione.
    """
    if (order.status or '') in _STATI_ARCHIVIO:
        return 'archivio'
    if getattr(order, 'data_consegna_effettiva', None) or getattr(order, 'ddt_numero', None):
        return 'consegnato'
    if getattr(order, 'data_completamento_operativo', None):
        return 'pronto_ddt'
    if (order.status or '') == _STATO_LAVORO_FINITO:
        # Ordini chiusi dall'officina PRIMA di questo intervento: la data non
        # c'e', ma la lavorazione era finita davvero. Non vanno fra gli aperti.
        return 'pronto_ddt'
    return 'aperto'


def _riga(order, nomi: dict) -> dict:
    """Dati che servono all'ufficio per decidere, senza fasi produttive."""
    f = fase(order)
    return {
        'id': order.id,
        'numero_ordine': order.numero_ordine or order.id[:8],
        'cliente': order.cliente or '',
        'fase': f,
        'fase_etichetta': ETICHETTE[f],
        'status': order.status,
        'data_ricezione': order.data_ricezione.isoformat() if order.data_ricezione else None,
        'data_consegna': order.data_consegna.isoformat() if order.data_consegna else None,
        'note': order.note or '',
        'prezzo_quotato': order.prezzo_quotato,
        'lotto_numero': order.lotto_numero or 0,
        'lotto_nome': order.lotto_nome or '',
        'parent_order_id': order.parent_order_id,
        # I quattro fatti, distinti
        # Unico segnale su dove sia un ordine ancora aperto: al laser o gia'
        # in officina. Lo marca il laser, non blocca nulla, ma all'ufficio
        # serve per rispondere al cliente che chiede "a che punto siamo".
        'taglio_fatto': bool(getattr(order, 'taglio_completato', False)),
        'taglio_il': _quando(order, 'data_taglio_completato'),
        'completamento': _quando(order, 'data_completamento_operativo'),
        'completato_da_id': getattr(order, 'completato_operativo_da', None),
        'completato_da': nomi.get(getattr(order, 'completato_operativo_da', None) or '',
                                  getattr(order, 'completato_operativo_da', None)),
        'ddt_numero': getattr(order, 'ddt_numero', None),
        'ddt_data': _quando(order, 'ddt_data'),
        'consegna': _quando(order, 'data_consegna_effettiva'),
        'consegna_registrata_da_id': getattr(order, 'consegna_registrata_da', None),
        'consegna_registrata_da': nomi.get(getattr(order, 'consegna_registrata_da', None) or '',
                                           getattr(order, 'consegna_registrata_da', None)),
        'consegna_parziale': bool(getattr(order, 'consegna_parziale', False)),
        'note_consegna': getattr(order, 'note_consegna', None) or '',
    }


def _quando(order, campo):
    v = getattr(order, campo, None)
    return v.isoformat() if v else None


def elenco(fase_richiesta: str = None, cliente: str = None, limite: int = 500) -> dict:
    """Ordini raggruppati per fase, con i conteggi di tutte le viste.

    I conteggi arrivano sempre completi: le linguette devono mostrare il numero
    anche delle viste non aperte.
    """
    session = get_session()
    try:
        nomi = {u.id: u.name for u in session.query(User).all()}
        q = session.query(Order).filter(Order.is_deleted == False)  # noqa: E712
        if cliente:
            q = q.filter(Order.cliente == cliente)
        righe = [_riga(o, nomi) for o in q.order_by(Order.data_consegna.asc()).all()]

        conteggi = {f: 0 for f in FASI}
        for r in righe:
            conteggi[r['fase']] += 1

        if fase_richiesta in FASI:
            righe = [r for r in righe if r['fase'] == fase_richiesta]
        return {'ordini': righe[:limite], 'conteggi': conteggi,
                'totale': len(righe), 'etichette': ETICHETTE}
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Transizioni — riservate all'ufficio
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


def registra_completamento(order_id: str, user_id: str, quando=None) -> dict:
    """L'officina ha comunicato che ha finito: l'ordine passa alla preparazione
    del DDT. Ripetere l'operazione non cambia nulla (idempotente)."""
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
                    'ordine': _riga(o, {})}
        o.data_completamento_operativo = quando or datetime.utcnow()
        o.completato_operativo_da = user_id or None
        # Lo stato storico resta allineato: chi legge `status` vede quello che
        # vedeva prima quando l'officina dichiarava finito.
        if (o.status or '') not in _STATI_ARCHIVIO:
            o.status = _STATO_LAVORO_FINITO
        session.commit()
        session.refresh(o)
        out = _riga(o, {})
    except Exception as e:
        session.rollback()
        logger.exception('registra_completamento fallita: %s', e)
        return {'error': 'Errore interno durante la registrazione. Riprova.',
                'codice': 'errore_interno'}
    finally:
        session.close()
    _audit(user_id, 'ORDINE_COMPLETAMENTO', order_id, 'Lavorazione completata (ufficio)')
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
        if getattr(o, 'ddt_numero', None) or getattr(o, 'data_consegna_effettiva', None):
            return {'error': 'Ci sono gia\' un DDT o una consegna registrati: '
                             'annulla prima quelli.', 'codice': 'sequenza'}
        o.data_completamento_operativo = None
        o.completato_operativo_da = None
        if (o.status or '') == _STATO_LAVORO_FINITO:
            o.status = 'RICEVUTO'
        session.commit()
        session.refresh(o)
        out = _riga(o, {})
    except Exception as e:
        session.rollback()
        logger.exception('annulla_completamento fallita: %s', e)
        return {'error': 'Errore interno. Riprova.', 'codice': 'errore_interno'}
    finally:
        session.close()
    _audit(user_id, 'ORDINE_COMPLETAMENTO_ANNULLATO', order_id, 'Riportato fra gli aperti')
    return {'success': True, 'ordine': out}


def registra_ddt(order_id: str, user_id: str, numero: str, data=None) -> dict:
    """Registra il riferimento del DDT emesso altrove. Non genera documenti."""
    numero = (numero or '').strip()
    if not numero:
        return {'error': 'Numero DDT obbligatorio', 'codice': 'numero_mancante'}
    if len(numero) > 60:
        return {'error': 'Numero DDT troppo lungo', 'codice': 'numero_lungo'}
    session = get_session()
    try:
        o = _carica(session, order_id)
        if not o:
            return {'error': 'Ordine non trovato', 'codice': 'non_trovato'}
        if not getattr(o, 'data_completamento_operativo', None) \
                and (o.status or '') != _STATO_LAVORO_FINITO:
            return {'error': 'Registra prima il completamento della lavorazione.',
                    'codice': 'sequenza'}
        o.ddt_numero = numero
        o.ddt_data = data or datetime.utcnow()
        session.commit()
        session.refresh(o)
        out = _riga(o, {})
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
        if not getattr(o, 'data_completamento_operativo', None) \
                and (o.status or '') != _STATO_LAVORO_FINITO:
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
        out = _riga(o, {})
    except Exception as e:
        session.rollback()
        logger.exception('registra_consegna fallita: %s', e)
        return {'error': 'Errore interno. Riprova.', 'codice': 'errore_interno'}
    finally:
        session.close()
    _audit(user_id, 'ORDINE_CONSEGNA', order_id,
           'Consegna ' + ('completa' if completa else f'PARZIALE: {note}'))
    return {'success': True, 'ordine': out}


def chiudi_pratica(order_id: str, user_id: str) -> dict:
    """Ciclo amministrativo concluso: l'ordine va in archivio.

    Rifiutata se resta un residuo da consegnare: un ordine consegnato a meta'
    non e' una pratica chiusa.
    """
    session = get_session()
    try:
        o = _carica(session, order_id)
        if not o:
            return {'error': 'Ordine non trovato', 'codice': 'non_trovato'}
        if getattr(o, 'consegna_parziale', False):
            return {'error': 'La consegna e\' ancora parziale: c\'e\' un residuo da '
                             'consegnare. Registra la consegna completa prima di archiviare.',
                    'codice': 'consegna_parziale'}
        if not getattr(o, 'data_consegna_effettiva', None):
            return {'error': 'Registra prima la consegna della merce.',
                    'codice': 'sequenza'}
        o.status = 'CHIUSO'
        session.commit()
        session.refresh(o)
        out = _riga(o, {})
    except Exception as e:
        session.rollback()
        logger.exception('chiudi_pratica fallita: %s', e)
        return {'error': 'Errore interno. Riprova.', 'codice': 'errore_interno'}
    finally:
        session.close()
    _audit(user_id, 'ORDINE_CHIUSO', order_id, 'Ciclo amministrativo concluso')
    return {'success': True, 'ordine': out}


def riapri(order_id: str, user_id: str) -> dict:
    """Riporta un ordine archiviato fra i consegnati, per correggere un errore."""
    session = get_session()
    try:
        o = _carica(session, order_id)
        if not o:
            return {'error': 'Ordine non trovato', 'codice': 'non_trovato'}
        if (o.status or '') not in _STATI_ARCHIVIO:
            return {'error': 'L\'ordine non e\' archiviato.', 'codice': 'non_archiviato'}
        o.status = _STATO_LAVORO_FINITO
        session.commit()
        session.refresh(o)
        out = _riga(o, {})
    except Exception as e:
        session.rollback()
        logger.exception('riapri fallita: %s', e)
        return {'error': 'Errore interno. Riprova.', 'codice': 'errore_interno'}
    finally:
        session.close()
    _audit(user_id, 'ORDINE_RIAPERTO', order_id, 'Riportato fra i consegnati')
    return {'success': True, 'ordine': out}
