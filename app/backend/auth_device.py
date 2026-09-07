"""Identita' e permessi VERIFICATI DAL SERVER (token di dispositivo).

PROBLEMA RISOLTO
----------------
Oggi `/api/auth/login` accetta uno `user_id` senza password e non crea alcuna
sessione: decine di endpoint si fidano dello `user_id` inviato dal browser.
Chiunque sulla LAN puo' quindi dichiararsi impiegata e agire come tale.

SOLUZIONE (bounded, additiva)
-----------------------------
Ogni dispositivo (tablet ore, tablet reparto, PC ufficio) viene configurato UNA
VOLTA con un token segreto. Il token viaggia nell'header e il backend deriva i
permessi DAL TOKEN, ignorando qualunque ruolo/utente dichiarato dal client.

    X-Device-Token: <token>      (oppure  Authorization: Bearer <token>)

Scope disponibili:
    'ore'     -> solo dichiarazioni ore
    'reparto' -> sola lettura ordini/allegati
    'ufficio' -> operazioni dell'impiegata (correzioni, ordini)

LIMITE DICHIARATO
-----------------
E' autenticazione di DISPOSITIVO, non di persona. Sul tablet ore la selezione
del nome resta una DICHIARAZIONE dell'operaio (scelta voluta per non aggiungere
attrito). Il punto essenziale e' che un tablet di officina NON puo' ottenere
privilegi d'ufficio, nemmeno chiamando le API direttamente.

I token sono salvati come HASH (sha256): un accesso al DB non li rivela.
Il token in chiaro viene mostrato una sola volta, alla creazione.
"""
import hashlib
import logging
import secrets
import uuid
from datetime import datetime
from functools import wraps

from flask import g, jsonify, request

from .database import get_session
from .models_ore import DeviceToken

logger = logging.getLogger(__name__)

SCOPES = ('ore', 'reparto', 'ufficio')

# Header accettati (il secondo per compatibilita' con client generici)
_HEADER = 'X-Device-Token'
_HEADER_ALT = 'Authorization'


def _hash(token: str) -> str:
    return hashlib.sha256((token or '').encode('utf-8')).hexdigest()


def crea_token(label: str, scope: str, created_by: str = 'setup') -> dict:
    """Crea un token di dispositivo. Ritorna il token IN CHIARO una sola volta.

    Da usare dallo script di setup (`app/tools/crea_device_token.py`).
    """
    scope = (scope or '').strip().lower()
    if scope not in SCOPES:
        return {'error': f"scope non valido: {scope} (ammessi: {', '.join(SCOPES)})"}
    label = (label or '').strip()
    if not label:
        return {'error': 'label obbligatoria'}

    plain = secrets.token_urlsafe(32)
    session = get_session()
    try:
        row = DeviceToken(
            id=str(uuid.uuid4()),
            token_hash=_hash(plain),
            label=label,
            scope=scope,
            is_active=True,
            created_at=datetime.utcnow(),
            created_by=created_by,
        )
        session.add(row)
        session.commit()
        logger.info('device token creato: %s (scope=%s)', label, scope)
        return {'success': True, 'token': plain, 'label': label, 'scope': scope, 'id': row.id}
    except Exception as e:
        session.rollback()
        logger.exception('crea_token fallita')
        return {'error': str(e)}
    finally:
        session.close()


def revoca_token(token_id: str, da: str = '') -> dict:
    session = get_session()
    try:
        row = session.query(DeviceToken).filter(DeviceToken.id == token_id).first()
        if not row:
            return {'error': 'token non trovato'}
        row.is_active = False
        row.revoked_at = datetime.utcnow()
        session.commit()
        logger.info('device token revocato: %s (da %s)', row.label, da or '?')
        return {'success': True}
    except Exception as e:
        session.rollback()
        return {'error': str(e)}
    finally:
        session.close()


def elenca_token() -> list:
    """Elenco token (senza segreti) per la schermata di amministrazione."""
    session = get_session()
    try:
        rows = session.query(DeviceToken).order_by(DeviceToken.created_at.desc()).all()
        return [{
            'id': r.id, 'label': r.label, 'scope': r.scope,
            'is_active': bool(r.is_active),
            'created_at': r.created_at.isoformat() if r.created_at else None,
            'last_used_at': r.last_used_at.isoformat() if r.last_used_at else None,
        } for r in rows]
    finally:
        session.close()


def _estrai_token() -> str:
    """Legge il token dagli header. Nessun fallback su query string o body:
    un segreto non deve finire nei log del server o nella cronologia."""
    tok = (request.headers.get(_HEADER) or '').strip()
    if tok:
        return tok
    auth = (request.headers.get(_HEADER_ALT) or '').strip()
    if auth.lower().startswith('bearer '):
        return auth[7:].strip()
    return ''


def risolvi_dispositivo():
    """Risolve il token corrente in un dispositivo attivo, oppure None.

    Aggiorna `last_used_at` (best effort, non blocca la richiesta).
    """
    tok = _estrai_token()
    if not tok:
        return None
    session = get_session()
    try:
        row = session.query(DeviceToken).filter(
            DeviceToken.token_hash == _hash(tok),
            DeviceToken.is_active == True,  # noqa: E712
        ).first()
        if not row:
            return None
        info = {'id': row.id, 'label': row.label, 'scope': row.scope}
        try:
            row.last_used_at = datetime.utcnow()
            session.commit()
        except Exception:
            session.rollback()
        return info
    finally:
        session.close()


def require_scope(*scopes_ammessi):
    """Decoratore Flask: richiede un token di dispositivo con uno degli scope.

    In caso di successo popola `g.device` = {'id','label','scope'}.
    NON legge mai ruoli o user_id dal client per decidere i permessi.

    Nota: 'ufficio' e' considerato sovrainsieme di 'reparto' in SOLA LETTURA;
    le operazioni di scrittura richiedono lo scope esatto elencato.
    """
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            dev = risolvi_dispositivo()
            if not dev:
                return jsonify({
                    'success': False,
                    'error': 'Dispositivo non autorizzato: token mancante o non valido.',
                    'codice': 'device_non_autorizzato',
                }), 401
            if dev['scope'] not in scopes_ammessi:
                logger.warning('accesso negato: device "%s" (scope=%s) su %s',
                               dev['label'], dev['scope'], request.path)
                return jsonify({
                    'success': False,
                    'error': 'Questo dispositivo non puo\' eseguire questa operazione.',
                    'codice': 'scope_insufficiente',
                }), 403
            g.device = dev
            return fn(*args, **kwargs)
        return wrapper
    return deco


def device_corrente() -> dict:
    """Dispositivo della richiesta corrente (dopo require_scope)."""
    return getattr(g, 'device', None) or {}
