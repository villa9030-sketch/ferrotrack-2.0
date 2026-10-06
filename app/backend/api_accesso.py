"""Le rotte dell'ingresso e dell'amministrazione degli accessi.

/api/accesso/...        pagina iniziale: chi sono, registra il dispositivo,
                        entra col PIN, esci, PIN di amministratore
/api/admin/persone...   persone, PIN, amministratori
/api/admin/dispositivi  dispositivi registrati (anche in app.py, storiche)
/api/admin/accesso      transizione / protetto

Le regole stanno in backend/accesso.py; qui c'e' solo cosa succede.
"""
import logging
import re
import unicodedata

from flask import Blueprint, jsonify, make_response, request

from . import accesso as A
from .accesso import ADMIN, PUBBLICO, richiede
from .database import AuditManager, get_session
from .models import User
from .models_ore import DeviceToken
from .orario import iso_utc

logger = logging.getLogger(__name__)

bp_accesso = Blueprint('accesso', __name__)


def _registro(azione, dettaglio='', persona=None, entita='accesso', entita_id=''):
    """Riga del registro attivita' col nome VERO di chi ha agito."""
    try:
        ident = A.identita()
        p = persona or ident.admin or ident.persona
        uid = p['id'] if p else ident.utente_id
        nome = p['nome'] if p else ident.nome
        AuditManager.log(user_id=uid, user_name=nome, action=azione, entity_type=entita,
                         entity_id=entita_id, detail=(dettaglio or '')[:2000],
                         ip_address=request.remote_addr)
    except Exception as e:
        logger.warning('registro %s non scritto: %s', azione, e)


def _stazioni_pubbliche():
    return [{'id': k, 'nome': v['nome'], 'descrizione': v['descrizione'],
             'officina': not v['con_pin']} for k, v in A.STAZIONI.items()]


def _da_questo_pc() -> bool:
    return (request.remote_addr or '') in ('127.0.0.1', '::1')


def _quanti_admin(s, tranne_id=None) -> int:
    return sum(1 for u in A.persone_con_pin(s) if u.e_admin and u.id != tranne_id)


# ---------------------------------------------------------------------------
# Pagina iniziale
# ---------------------------------------------------------------------------
@bp_accesso.route('/api/accesso/io', methods=['GET'])
@richiede(PUBBLICO)
def api_io():
    """Cosa sa il server di questo dispositivo. Non dice nulla di segreto:
    serve alla pagina iniziale per scegliere cosa mostrare."""
    ident = A.identita()
    st = ident.stazione
    info = A.STAZIONI.get(st) or {}
    s = get_session()
    try:
        nessun_admin = _quanti_admin(s) == 0
    finally:
        s.close()
    return jsonify({
        'success': True,
        'modalita': A.modalita(),
        'registrato': ident.registrato,
        'dispositivo': ({'id': ident.dispositivo['id'], 'nome': ident.dispositivo['label']}
                        if ident.dispositivo else None),
        'stazione': st,
        'stazione_nome': info.get('nome'),
        'pagina': info.get('pagina'),
        'con_pin': bool(info.get('con_pin')),
        'persona': ({'id': ident.persona['id'], 'nome': ident.persona['nome']}
                    if ident.persona else None),
        'serve_pin': bool(ident.registrato and info.get('con_pin') and not ident.persona),
        'admin': ({'id': ident.admin['id'], 'nome': ident.admin['nome']} if ident.admin else None),
        'vecchio': bool(ident.vecchio),
        'nessun_admin': nessun_admin,
        'da_questo_pc': _da_questo_pc(),
        'stazioni': _stazioni_pubbliche(),
    }), 200


@bp_accesso.route('/api/accesso/registra', methods=['POST'])
@richiede(PUBBLICO)
def api_registra():
    """"Cosa e' questo dispositivo?" Serve il PIN di un amministratore,
    digitato adesso. Su un dispositivo gia' registrato e' il "cambia"."""
    dati = request.get_json(silent=True) or {}
    stazione = (dati.get('stazione') or '').strip()
    if stazione not in A.STAZIONI:
        return jsonify({'success': False, 'codice': 'stazione_sconosciuta',
                        'error': 'Scegli cosa è questo dispositivo.'}), 400
    ident = A.identita()
    esistente = ident.dispositivo['id'] if ident.dispositivo else None
    admin, errore = A.verifica_pin(dati.get('pin'), A._chiave_tentativi(esistente), serve_admin=True)
    if errore:
        return jsonify(errore), (429 if errore.get('codice') == 'bloccato' else 403)
    nome = dati.get('nome') or A.STAZIONI[stazione]['nome']
    prima = ident.stazione
    segreto, disp = A.registra_dispositivo(stazione, nome, da=admin['id'], esistente_id=esistente)
    _registro('DISPOSITIVO_REGISTRATO' if not esistente else 'DISPOSITIVO_CAMBIATO',
              '%s -> %s (%s)' % (prima or 'nuovo', stazione, disp['nome']), persona=admin,
              entita='dispositivo', entita_id=disp['id'])
    resp = make_response(jsonify({'success': True, 'stazione': stazione,
                                  'pagina': A.STAZIONI[stazione]['pagina'],
                                  'con_pin': A.STAZIONI[stazione]['con_pin'],
                                  'dispositivo': {'id': disp['id'], 'nome': disp['nome']}}))
    if segreto:
        A.imposta_cookie(resp, A.COOKIE_DISP, segreto, max_age=10 * 365 * 86400)
    # Cambiando stazione chi era dentro esce (le sessioni sono gia' chiuse)
    A.cancella_cookie(resp, A.COOKIE_SESS)
    return resp


@bp_accesso.route('/api/accesso/usa-codice', methods=['POST'])
@richiede(PUBBLICO)
def api_usa_codice():
    """Adotta un dispositivo creato dal server (riga di comando o pagina
    Amministrazione): il codice lungo diventa il cookie di questo browser."""
    codice = ((request.get_json(silent=True) or {}).get('codice') or '').strip()
    s = get_session()
    try:
        riga = A._dispositivo(s, codice) if codice else None
        if riga is None or riga.scope not in A.STAZIONI:
            return jsonify({'success': False, 'codice': 'codice_non_valido',
                            'error': 'Codice non valido o dispositivo revocato.'}), 403
        st = riga.scope
    finally:
        s.close()
    resp = make_response(jsonify({'success': True, 'stazione': st,
                                  'pagina': A.STAZIONI[st]['pagina']}))
    A.imposta_cookie(resp, A.COOKIE_DISP, codice, max_age=10 * 365 * 86400)
    return resp


@bp_accesso.route('/api/accesso/entra', methods=['POST'])
@richiede(PUBBLICO)
def api_entra():
    """PIN personale negli uffici: apre la sessione fino alle 3 di notte."""
    ident = A.identita()
    if not ident.registrato:
        return jsonify({'success': False, 'codice': 'non_registrato',
                        'error': 'Questo dispositivo non è registrato: chiedi all’amministrazione.'}), 401
    if not A.STAZIONI[ident.stazione]['con_pin']:
        return jsonify({'success': True, 'pagina': A.STAZIONI[ident.stazione]['pagina']}), 200
    pin = (request.get_json(silent=True) or {}).get('pin')
    persona, errore = A.verifica_pin(pin, A._chiave_tentativi(ident.dispositivo['id']))
    if errore:
        return jsonify(errore), (429 if errore.get('codice') == 'bloccato' else 403)
    segreto, scade = A.apri_sessione('ufficio', persona['id'], ident.dispositivo['id'])
    _registro('LOGIN', 'entrata in %s (%s)' % (A.STAZIONI[ident.stazione]['nome'],
                                                 ident.dispositivo['label']), persona=persona)
    resp = make_response(jsonify({'success': True, 'persona': {'id': persona['id'], 'nome': persona['nome']},
                                  'pagina': A.STAZIONI[ident.stazione]['pagina'],
                                  'scade': iso_utc(scade)}))
    A.imposta_cookie(resp, A.COOKIE_SESS, segreto, scade=scade)
    return resp


@bp_accesso.route('/api/accesso/esci', methods=['POST'])
@bp_accesso.route('/api/auth/logout', methods=['POST'])
@richiede(PUBBLICO)
def api_esci():
    """"Esci": chiude la sessione della persona e il PIN di amministratore.
    Il dispositivo resta registrato."""
    ident = A.identita()
    if ident.persona:
        _registro('LOGOUT', 'uscita da %s' % A.STAZIONI[ident.stazione]['nome'])
    A.chiudi_sessione(request.cookies.get(A.COOKIE_SESS))
    A.chiudi_sessione(request.cookies.get(A.COOKIE_ADMIN))
    resp = make_response(jsonify({'success': True}))
    A.cancella_cookie(resp, A.COOKIE_SESS)
    A.cancella_cookie(resp, A.COOKIE_ADMIN)
    return resp


@bp_accesso.route('/api/accesso/admin', methods=['POST'])
@richiede(PUBBLICO)
def api_admin_pin():
    """PIN di amministratore digitato adesso: vale DURATA_ADMIN su questo
    dispositivo (o su questo browser, se non e' registrato)."""
    ident = A.identita()
    dev = ident.dispositivo['id'] if ident.dispositivo else None
    pin = (request.get_json(silent=True) or {}).get('pin')
    admin, errore = A.verifica_pin(pin, A._chiave_tentativi(dev), serve_admin=True)
    if errore:
        return jsonify(errore), (429 if errore.get('codice') == 'bloccato' else 403)
    segreto, scade = A.apri_sessione('admin', admin['id'], dev)
    _registro('PIN_AMMINISTRATORE', 'su %s' % (ident.dispositivo['label'] if ident.dispositivo else request.remote_addr),
              persona=admin)
    resp = make_response(jsonify({'success': True, 'admin': {'id': admin['id'], 'nome': admin['nome']},
                                  'scade': iso_utc(scade)}))
    A.imposta_cookie(resp, A.COOKIE_ADMIN, segreto, scade=scade)
    return resp


@bp_accesso.route('/api/accesso/admin', methods=['DELETE'])
@richiede(PUBBLICO)
def api_admin_fine():
    A.chiudi_sessione(request.cookies.get(A.COOKIE_ADMIN))
    resp = make_response(jsonify({'success': True}))
    A.cancella_cookie(resp, A.COOKIE_ADMIN)
    return resp


@bp_accesso.route('/api/accesso/primo-admin', methods=['POST'])
@richiede(PUBBLICO)
def api_primo_admin():
    """Il PRIMO amministratore, quando non ce n'e' nessuno, solo dal PC
    server (localhost). Di solito lo si crea con tools/persone.py."""
    if not _da_questo_pc():
        return jsonify({'success': False, 'codice': 'solo_dal_server',
                        'error': 'Il primo amministratore si crea dal PC del server.'}), 403
    dati = request.get_json(silent=True) or {}
    s = get_session()
    try:
        if _quanti_admin(s) > 0:
            return jsonify({'success': False, 'codice': 'esiste_gia',
                            'error': 'Un amministratore c’è già: chiedi a lui.'}), 409
    finally:
        s.close()
    esito, codice = crea_persona(dati.get('nome'), dati.get('ruolo') or 'Amministrazione',
                                 admin=True, pin=dati.get('pin'))
    if esito.get('success'):
        _registro('PRIMO_AMMINISTRATORE', esito['persona']['nome'], persona=esito['persona'],
                  entita='user', entita_id=esito['persona']['id'])
    return jsonify(esito), codice


# ---------------------------------------------------------------------------
# Persone
# ---------------------------------------------------------------------------
def _id_da_nome(s, nome: str) -> str:
    base = unicodedata.normalize('NFKD', nome).encode('ascii', 'ignore').decode('ascii')
    base = re.sub(r'[^a-z0-9]+', '-', base.lower()).strip('-') or 'persona'
    oid, n = base, 2
    while s.get(User, oid) is not None:
        oid = '%s-%d' % (base, n)
        n += 1
    return oid


def _persona_dict(u) -> dict:
    return {'id': u.id, 'nome': u.name, 'ruolo': u.role, 'attivo': bool(u.is_active),
            'admin': bool(u.e_admin), 'ha_pin': bool(u.pin_hash),
            'pin_impostato_il': iso_utc(u.pin_impostato_il),
            'ultimo_ingresso': iso_utc(u.last_login)}


def crea_persona(nome, ruolo, admin=None, pin=None):
    """(esito, codice http). Usata dalla pagina, dal primo admin e dalla riga di comando."""
    nome = re.sub(r'\s+', ' ', str(nome or '')).strip()
    if len(nome) < 2:
        return {'success': False, 'error': 'Scrivi nome e cognome.'}, 400
    if ruolo not in A.RUOLI_PERSONA:
        return {'success': False, 'error': 'Ruolo sconosciuto.'}, 400
    if admin is None:
        admin = ruolo in A.RUOLI_ADMIN_DI_PARTENZA
    s = get_session()
    try:
        if pin not in (None, ''):
            err = A.errore_formato_pin(pin)
            if err:
                return {'success': False, 'error': err}, 400
            if A.pin_gia_usato(s, str(pin)):
                return {'success': False, 'codice': 'pin_usato',
                        'error': 'Questo PIN non si può usare: scegline un altro.'}, 409
        piatto = nome.lower()
        for x in s.query(User).filter(User.is_active == True).all():  # noqa: E712
            if ' '.join((x.name or '').split()).lower() == piatto:
                return {'success': False, 'codice': 'gia_presente',
                        'error': 'C’è già %s.' % x.name}, 409
        if ruolo == 'Operaio':
            # Un operaio va sulla bacheca della timbratrice come quando lo si
            # aggiunge dal tablet: tenuto a dichiarare da oggi, non da prima.
            from . import ore_service
            esito = ore_service.aggiungi_operaio(nome, aggiunto_da='amministrazione')
            if not esito.get('success'):
                return esito, (409 if esito.get('codice') == 'gia_presente' else 400)
            u = s.get(User, esito['operatore_id'])
            u.e_admin = bool(admin)
        else:
            u = User(id=_id_da_nome(s, nome), name=nome, role=ruolo,
                     initials=''.join(p[0] for p in nome.split()[:2]).upper(),
                     permissions=[], machines=[], is_active=True, e_postazione=False,
                     e_admin=bool(admin))
        if pin not in (None, ''):
            u.pin_hash = A.impronta_pin(str(pin))
            u.pin_impostato_il = A.adesso()
        s.add(u)
        s.commit()
        return {'success': True, 'persona': _persona_dict(u)}, 201
    finally:
        s.close()


@bp_accesso.route('/api/admin/persone', methods=['GET'])
@richiede(ADMIN)
def api_persone():
    s = get_session()
    try:
        righe = s.query(User).filter((User.e_postazione == False) | (User.e_postazione.is_(None))).all()  # noqa: E712
        persone = sorted((_persona_dict(u) for u in righe),
                         key=lambda p: (not p['attivo'], not p['ha_pin'], (p['nome'] or '').lower()))
        return jsonify({'success': True, 'persone': persone, 'ruoli': list(A.RUOLI_PERSONA),
                        'amministratori': _quanti_admin(s)}), 200
    finally:
        s.close()


@bp_accesso.route('/api/admin/persone', methods=['POST'])
@richiede(ADMIN)
def api_crea_persona():
    dati = request.get_json(silent=True) or {}
    esito, codice = crea_persona(dati.get('nome'), dati.get('ruolo'), admin=dati.get('admin'),
                                 pin=dati.get('pin'))
    if esito.get('success'):
        p = esito['persona']
        _registro('PERSONA_CREATA', '%s (%s)%s' % (p['nome'], p['ruolo'], ', amministratore' if p['admin'] else ''),
                  entita='user', entita_id=p['id'])
    return jsonify(esito), codice


@bp_accesso.route('/api/admin/persone/<pid>', methods=['PUT'])
@richiede(ADMIN)
def api_modifica_persona(pid):
    dati = request.get_json(silent=True) or {}
    s = get_session()
    try:
        u = s.get(User, pid)
        if u is None or u.e_postazione:
            return jsonify({'success': False, 'error': 'Persona non trovata.'}), 404
        cambi = []
        if 'nome' in dati:
            nome = re.sub(r'\s+', ' ', str(dati.get('nome') or '')).strip()
            if len(nome) < 2:
                return jsonify({'success': False, 'error': 'Scrivi nome e cognome.'}), 400
            if nome != u.name:
                cambi.append('nome %s -> %s' % (u.name, nome))
                u.name = nome
        if 'ruolo' in dati and dati['ruolo'] != u.role:
            if dati['ruolo'] not in A.RUOLI_PERSONA:
                return jsonify({'success': False, 'error': 'Ruolo sconosciuto.'}), 400
            cambi.append('ruolo %s -> %s' % (u.role, dati['ruolo']))
            u.role = dati['ruolo']
        toglie_admin = 'admin' in dati and not dati['admin'] and u.e_admin
        spegne = 'attivo' in dati and not dati['attivo'] and u.is_active
        if (toglie_admin or spegne) and u.e_admin and u.pin_hash and _quanti_admin(s, tranne_id=u.id) == 0:
            return jsonify({'success': False, 'codice': 'ultimo_admin',
                            'error': 'È l’unico amministratore: prima nominane un altro.'}), 409
        if 'admin' in dati and bool(dati['admin']) != bool(u.e_admin):
            cambi.append('amministratore %s' % ('si' if dati['admin'] else 'no'))
            u.e_admin = bool(dati['admin'])
        if 'attivo' in dati and bool(dati['attivo']) != bool(u.is_active):
            cambi.append('attiva' if dati['attivo'] else 'disattivata')
            u.is_active = bool(dati['attivo'])
        s.commit()
        out = _persona_dict(u)
    finally:
        s.close()
    if spegne:
        A.chiudi_sessioni(user_id=pid)
    if cambi:
        _registro('PERSONA_MODIFICATA', '%s: %s' % (out['nome'], '; '.join(cambi)), entita='user', entita_id=pid)
    return jsonify({'success': True, 'persona': out}), 200


@bp_accesso.route('/api/admin/persone/<pid>/pin', methods=['PUT'])
@richiede(ADMIN)
def api_imposta_pin(pid):
    """Dare o rimettere il PIN (PIN dimenticato: lo rimette un amministratore)."""
    pin = str((request.get_json(silent=True) or {}).get('pin') or '').strip()
    err = A.errore_formato_pin(pin)
    if err:
        return jsonify({'success': False, 'error': err}), 400
    s = get_session()
    try:
        u = s.get(User, pid)
        if u is None or u.e_postazione:
            return jsonify({'success': False, 'error': 'Persona non trovata.'}), 404
        if A.pin_gia_usato(s, pin, tranne_id=pid):
            return jsonify({'success': False, 'codice': 'pin_usato',
                            'error': 'Questo PIN non si può usare: scegline un altro.'}), 409
        u.pin_hash = A.impronta_pin(pin)
        u.pin_impostato_il = A.adesso()
        s.commit()
        nome = u.name
    finally:
        s.close()
    # Il PIN vecchio non deve tenere aperte sessioni (era stato visto da qualcuno?)
    A.chiudi_sessioni(user_id=pid)
    _registro('PIN_IMPOSTATO', nome, entita='user', entita_id=pid)
    return jsonify({'success': True}), 200


@bp_accesso.route('/api/admin/persone/<pid>/pin', methods=['DELETE'])
@richiede(ADMIN)
def api_togli_pin(pid):
    s = get_session()
    try:
        u = s.get(User, pid)
        if u is None or u.e_postazione:
            return jsonify({'success': False, 'error': 'Persona non trovata.'}), 404
        if u.e_admin and u.pin_hash and _quanti_admin(s, tranne_id=pid) == 0:
            return jsonify({'success': False, 'codice': 'ultimo_admin',
                            'error': 'È l’unico amministratore: prima nominane un altro.'}), 409
        u.pin_hash = None
        u.pin_impostato_il = A.adesso()
        s.commit()
        nome = u.name
    finally:
        s.close()
    A.chiudi_sessioni(user_id=pid)
    _registro('PIN_TOLTO', nome, entita='user', entita_id=pid)
    return jsonify({'success': True}), 200


# ---------------------------------------------------------------------------
# Dispositivi e modalita'
# ---------------------------------------------------------------------------
@bp_accesso.route('/api/admin/dispositivi/<token_id>', methods=['PUT'])
@richiede(ADMIN)
def api_rinomina_dispositivo(token_id):
    nome = re.sub(r'\s+', ' ', str((request.get_json(silent=True) or {}).get('nome') or '')).strip()[:60]
    if not nome:
        return jsonify({'success': False, 'error': 'Scrivi un nome.'}), 400
    s = get_session()
    try:
        r = s.get(DeviceToken, token_id)
        if r is None:
            return jsonify({'success': False, 'error': 'Dispositivo non trovato.'}), 404
        prima, r.label = r.label, nome
        s.commit()
    finally:
        s.close()
    _registro('DISPOSITIVO_RINOMINATO', '%s -> %s' % (prima, nome), entita='dispositivo', entita_id=token_id)
    return jsonify({'success': True}), 200


@bp_accesso.route('/api/admin/accesso', methods=['GET'])
@richiede(ADMIN)
def api_stato_accesso():
    """Modalita' e quali stazioni hanno almeno un dispositivo registrato."""
    s = get_session()
    try:
        attivi = s.query(DeviceToken).filter(DeviceToken.is_active == True).all()  # noqa: E712
        per_stazione = {k: 0 for k in A.STAZIONI}
        for r in attivi:
            if r.scope in per_stazione:
                per_stazione[r.scope] += 1
        return jsonify({'success': True, 'modalita': A.modalita(),
                        'stazioni': [{'id': k, 'nome': v['nome'], 'dispositivi': per_stazione[k]}
                                     for k, v in A.STAZIONI.items()],
                        'amministratori': _quanti_admin(s)}), 200
    finally:
        s.close()


@bp_accesso.route('/api/admin/accesso', methods=['PUT'])
@richiede(ADMIN)
def api_cambia_modalita():
    m = (request.get_json(silent=True) or {}).get('modalita')
    if m not in A.MODALITA:
        return jsonify({'success': False, 'error': 'Modalità sconosciuta.'}), 400
    esito = A.imposta_modalita(m)
    if esito.get('error'):
        return jsonify({'success': False, 'error': esito['error']}), 500
    _registro('MODALITA_ACCESSO', m)
    return jsonify({'success': True, 'modalita': m}), 200
