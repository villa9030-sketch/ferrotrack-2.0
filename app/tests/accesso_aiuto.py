"""Aiuti per i test: entrare in FerroTrack come una stazione, senza passare
dalle pagine. Non e' un test (non si chiama test_*), lo usano i test.

Prima i test mandavano X-User-Id o user_id e il server ci credeva. Ora il
server crede solo al dispositivo registrato (cookie) e, negli uffici, al PIN
della persona: questi aiuti preparano proprio quello, in due righe.

    from tests.accesso_aiuto import entra, persona, postazioni
    postazioni()                                  # le 5 utenze di postazione
    persona('elena', 'Elena Rossi', pin='2580')   # amministrazione, admin
    c = entra(app, 'ufficio', pin='2580')         # test client gia' dentro
    c = entra(app, 'laser')                       # officina: niente PIN
    eleva(c, '2580')                              # PIN di amministratore

Vanno chiamati DOPO aver puntato models.engine sul database temporaneo.
"""
from backend import accesso as A
from backend.database import get_session
from backend.models import User

PIN_ADMIN = '2580'
PIN_UFFICIO = '7913'

_POSTAZIONI = [
    ('postazione-timbratrice', 'Tablet timbratrice', 'Timbratrice'),
    ('postazione-visione', 'Tablet di visione', 'Visione'),
    ('postazione-amministrazione', 'Amministrazione', 'Amministrazione'),
    ('postazione-laser', 'Laser', 'Laser'),
    ('postazione-commerciale', 'Commerciale', 'Commerciale'),
]


def postazioni():
    """Le utenze di postazione (come seed_users): servono come "chi" dello
    storico e come proprietarie degli avvisi delle stazioni."""
    s = get_session()
    try:
        for uid, nome, ruolo in _POSTAZIONI:
            if s.get(User, uid) is None:
                s.add(User(id=uid, name=nome, role=ruolo, e_postazione=True, is_active=True,
                           is_capo=(uid == 'postazione-laser'), e_admin=False,
                           permissions=[], machines=[]))
        s.commit()
    finally:
        s.close()


def persona(pid, nome, ruolo='Amministrazione', pin=None, admin=None, attivo=True):
    """Una persona; col PIN puo' entrare negli uffici."""
    if admin is None:
        admin = ruolo in A.RUOLI_ADMIN_DI_PARTENZA
    s = get_session()
    try:
        u = s.get(User, pid) or User(id=pid)
        u.name, u.role, u.is_active, u.e_postazione, u.e_admin = nome, ruolo, attivo, False, admin
        u.permissions, u.machines = [], []
        if pin:
            u.pin_hash = A.impronta_pin(pin)
            u.pin_impostato_il = A.adesso()
        s.merge(u)
        s.commit()
    finally:
        s.close()
    return pid


def dispositivo(stazione, nome=None):
    """(segreto, id) di un dispositivo registrato come quella stazione."""
    segreto, disp = A.registra_dispositivo(stazione, nome or ('Prova ' + stazione), da='test')
    return segreto, disp['id']


def entra(app, stazione=None, pin=None, client=None):
    """Un test client come dispositivo di quella stazione; negli uffici, col
    PIN, gia' entrato. Senza stazione: un browser qualunque, non registrato."""
    c = client or app.test_client()
    c.dispositivo_id = None
    if stazione:
        segreto, did = dispositivo(stazione)
        c.set_cookie(A.COOKIE_DISP, segreto)
        c.dispositivo_id = did
        c.segreto = segreto
    if pin:
        r = c.post('/api/accesso/entra', json={'pin': pin})
        assert r.status_code == 200, (r.status_code, r.get_json())
    return c


def eleva(c, pin=PIN_ADMIN):
    """PIN di amministratore digitato adesso su questo client."""
    r = c.post('/api/accesso/admin', json={'pin': pin})
    assert r.status_code == 200, (r.status_code, r.get_json())
    return c


def modalita(m):
    """Forza transizione/protetto senza toccare app_config.json."""
    A.modalita = (lambda: m)


def ufficio_pronto(app, pid='ufficio-test', pin=PIN_UFFICIO):
    """Scorciatoia per i test di ordini e preventivi: postazioni + una persona
    d'ufficio + un client Ufficio gia' entrato."""
    postazioni()
    persona(pid, 'Persona Ufficio', pin=pin)
    return entra(app, 'ufficio', pin=pin)


_PIN_STAZIONE = {'ufficio': PIN_UFFICIO, 'commerciale': '3691'}


def stazione_pronta(app, stazione):
    """Un client gia' dentro come quella stazione: postazioni, la persona col
    PIN (negli uffici) e il dispositivo registrato. Per i test di ordini e
    preventivi che prima mandavano user_id/admin_id."""
    postazioni()
    pin = _PIN_STAZIONE.get(stazione)
    if pin:
        persona('test-' + stazione, 'Persona ' + stazione.capitalize(),
                'Amministrazione' if stazione == 'ufficio' else 'Commerciale', pin=pin)
    return entra(app, stazione, pin=pin)


def per_token(app, pin_ufficio=None):
    """Per i test scritti col vecchio token nell'intestazione: un client
    DIVERSO per ogni token (ogni token e' un dispositivo diverso, e dopo la
    prima richiesta il token sta anche nel cookie di quel browser). Coi
    dispositivi d'ufficio, se si da' pin_ufficio, la persona entra col PIN.

        C = per_token(app, pin_ufficio=PIN_UFFICIO)
        C(t_ore).get('/api/ore/operai', headers={'X-Device-Token': t_ore})
    """
    clienti = {}

    def C(tok):
        if tok not in clienti:
            c = app.test_client()
            if pin_ufficio:
                # sui dispositivi d'officina l'ingresso risponde 200 e basta
                c.post('/api/accesso/entra', json={'pin': pin_ufficio},
                       headers={'X-Device-Token': tok})
            clienti[tok] = c
        return clienti[tok]
    return C
