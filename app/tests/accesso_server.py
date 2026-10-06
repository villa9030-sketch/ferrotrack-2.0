"""Aiuti per i test che parlano con un SERVER DI PROVA vero (tools/server_prova.py).

Il server di prova lavora su una COPIA del database: qui si registrano
dispositivi e si creano persone di prova senza toccare i dati veri.

    from tests.accesso_server import Sessione, contesto_stazione
    s = Sessione(BASE, 'ufficio')          # urllib con i cookie, gia' dentro
    s.get('/api/ordini/viste')             # -> (codice, json)
    contesto_stazione(context, BASE, 'laser')   # Playwright: stesso, nel browser

Il PIN dell'amministratore di prova:
 - se sul server non c'e' ancora nessun amministratore, lo si crea dal PC del
   server (/api/accesso/primo-admin, solo da localhost);
 - se c'e' gia', si usa FT_PIN_ADMIN (variabile d'ambiente, default 8642).
"""
import http.cookiejar
import json
import os
import urllib.error
import urllib.request

PIN_ADMIN = os.environ.get('FT_PIN_ADMIN', '8642')
# Una persona di prova per ufficio, ognuna col suo PIN (i PIN sono unici).
PERSONE = {'ufficio': ('Prova Ufficio', 'Amministrazione', '7913'),
           'commerciale': ('Prova Commerciale', 'Commerciale', '3691')}


class Sessione:
    """Un "dispositivo" che parla col server: cookie suoi, come un browser."""

    def __init__(self, base, stazione=None, entra=True):
        self.base = base.rstrip('/')
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        if stazione:
            prepara(self.base)
            c, d = self.post('/api/accesso/registra', {'stazione': stazione, 'pin': PIN_ADMIN,
                                                       'nome': 'Prova ' + stazione})
            assert c == 200, ('registrazione', stazione, c, d)
            if stazione in PERSONE and entra:
                nome, ruolo, pin = PERSONE[stazione]
                persona_con_pin(self.base, nome, ruolo, pin)
                c, d = self.post('/api/accesso/entra', {'pin': pin})
                assert c == 200, ('ingresso', stazione, c, d)

    def chiama(self, metodo, percorso, corpo=None, intestazioni=None):
        dati = json.dumps(corpo).encode() if corpo is not None else None
        h = {'Content-Type': 'application/json'} if dati is not None else {}
        h.update(intestazioni or {})
        req = urllib.request.Request(self.base + percorso, data=dati, method=metodo, headers=h)
        try:
            with self.op.open(req, timeout=60) as r:
                testo = r.read()
                codice = r.status
        except urllib.error.HTTPError as e:
            testo, codice = e.read(), e.code
        try:
            return codice, json.loads(testo.decode('utf-8') or 'null')
        except Exception:
            return codice, testo

    def get(self, p, **kw):
        return self.chiama('GET', p, **kw)

    def post(self, p, corpo=None, **kw):
        return self.chiama('POST', p, {} if corpo is None else corpo, **kw)

    def put(self, p, corpo=None, **kw):
        return self.chiama('PUT', p, {} if corpo is None else corpo, **kw)

    def delete(self, p, **kw):
        return self.chiama('DELETE', p, **kw)

    def eleva(self):
        c, d = self.post('/api/accesso/admin', {'pin': PIN_ADMIN})
        assert c == 200, ('PIN amministratore di prova rifiutato: imposta FT_PIN_ADMIN', c, d)
        return self

    def cookie(self, nome):
        return next((c.value for c in self.jar if c.name == nome), None)


_PRONTO = set()


def prepara(base):
    """C'e' un amministratore col PIN di prova? Se il server non ne ha, si crea."""
    if base in _PRONTO:
        return
    s = Sessione(base)
    c, io = s.get('/api/accesso/io')
    assert c == 200, ('server di prova non raggiungibile', c)
    if io.get('nessun_admin'):
        c, d = s.post('/api/accesso/primo-admin', {'nome': 'Amministratore Prova', 'pin': PIN_ADMIN})
        assert c in (201, 200), ('primo amministratore', c, d)
    _PRONTO.add(base)


def persona_con_pin(base, nome, ruolo, pin):
    """La persona di prova esiste e ha quel PIN (la si crea o le si rimette)."""
    adm = Sessione(base).eleva()
    c, d = adm.get('/api/admin/persone')
    p = next((x for x in (d.get('persone') or []) if x['nome'] == nome), None)
    if p is None:
        c, d = adm.post('/api/admin/persone', {'nome': nome, 'ruolo': ruolo, 'admin': False, 'pin': pin})
        assert c == 201, ('persona di prova', c, d)
    else:
        if not p['attivo']:
            adm.put('/api/admin/persone/%s' % p['id'], {'attivo': True})
        c, d = adm.put('/api/admin/persone/%s/pin' % p['id'], {'pin': pin})
        assert c == 200, ('PIN della persona di prova', c, d)


def contesto_stazione(context, base, stazione):
    """Un contesto Playwright registrato come quella stazione (e dentro col
    PIN, negli uffici). Usa context.request: i cookie sono quelli del browser."""
    base = base.rstrip('/')
    prepara(base)
    if stazione in PERSONE:
        nome, ruolo, pin = PERSONE[stazione]
        persona_con_pin(base, nome, ruolo, pin)
    r = context.request.post(base + '/api/accesso/registra',
                             data={'stazione': stazione, 'pin': PIN_ADMIN, 'nome': 'Prova ' + stazione})
    assert r.status == 200, ('registrazione', stazione, r.status, r.text())
    if stazione in PERSONE:
        r = context.request.post(base + '/api/accesso/entra', data={'pin': PERSONE[stazione][2]})
        assert r.status == 200, ('ingresso', stazione, r.status, r.text())
    return context
