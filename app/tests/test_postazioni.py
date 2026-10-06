# -*- coding: utf-8 -*-
"""Le postazioni e le persone, viste dal server.

Copre le due cose che, sbagliate, si notano solo in azienda:

  1. le cinque POSTAZIONI (timbratrice, tablet di visione, amministrazione,
     laser, commerciale) ci sono ancora: sono il "chi" dello storico quando
     non c'e' una persona (al laser, in officina). Oggi all'ingresso non si
     sceglie piu' da un elenco: il dispositivo e' registrato come stazione
     (backend/accesso.py) e negli uffici si entra col PIN;

  2. gli OPERAI restano, con la loro storia di ore attaccata. Se sparissero,
     la bacheca della timbratrice non avrebbe piu' nomi da mostrare e le
     giornate gia' dichiarate diventerebbero di nessuno.

Serve un server in ascolto (default 127.0.0.1:5056, cioe' un'istanza di prova
su una COPIA del database). Senza, il test si salta.

    python app/tests/test_postazioni.py [url]
"""
import json
import os
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)

BASE = sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:5056'

# Il posto, e la pagina che deve aprirsi entrandoci.
POSTAZIONI = {
    'postazione-timbratrice':     ('Tablet timbratrice', 'Timbratrice'),
    'postazione-visione':         ('Tablet di visione', 'Visione'),
    'postazione-amministrazione': ('Amministrazione', 'Amministrazione'),
    'postazione-laser':           ('Laser', 'Laser'),
    'postazione-commerciale':     ('Commerciale', 'Commerciale'),
}

OPERAI = ('mirko-laser', 'enzo-officina')

OK = 0
KO = []


def check(nome, cond, extra=''):
    global OK
    if cond:
        OK += 1
        print('  [OK] %s' % nome)
    else:
        KO.append(nome)
        print('  [KO] %s %s' % (nome, extra))


def chiama(percorso):
    try:
        with urllib.request.urlopen(BASE + percorso, timeout=10) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception:
        return 0, {}


def main():
    global OK
    stato, _ = chiama('/api/health')
    if stato != 200:
        print('Server non raggiungibile su %s. Test saltato.' % BASE)
        return 0

    # L'elenco delle persone non e' piu' aperto a chiunque: lo legge l'Ufficio.
    stato, _ = chiama('/api/users')
    check('elenco utenti: da un browser qualunque non si legge (401)', stato == 401, stato)
    from tests.accesso_server import Sessione
    uff = Sessione(BASE, 'ufficio')
    stato, d = uff.get('/api/users')
    if stato != 200:
        print('Elenco utenti non leggibile neanche dall\'Ufficio (%s). Test saltato.' % stato)
        return 1
    utenti = {u['id']: u for u in (d.get('users') or [])}

    print('1) Le cinque postazioni ci sono ancora')
    postazioni = {i: u for i, u in utenti.items()
                  if u.get('e_postazione') and u.get('is_active', True)}
    check('ci sono tutte e cinque le postazioni',
          set(postazioni) == set(POSTAZIONI), sorted(postazioni))
    for pid, (nome, ruolo) in POSTAZIONI.items():
        u = utenti.get(pid, {})
        check('%-28s si chiama "%s"' % (pid, nome), u.get('name') == nome, u.get('name'))
        check('%-28s ha ruolo "%s"' % (pid, ruolo), u.get('role') == ruolo, u.get('role'))

    print("\n2) Le persone non sono postazioni e non comandano")
    # Non si pretende di trovare nomi precisi: il programma non arriva con
    # delle persone dentro, le mette chi lavora. Un test che cerca "Mirko"
    # fallirebbe il giorno in cui Mirko se ne va, cioe' quando tutto funziona.
    persone = {i: u for i, u in utenti.items()
               if not u.get('e_postazione') and u.get('is_active', True)}
    print('   persone sulla bacheca: %s' % (sorted(u['name'] for u in persone.values())
                                            or 'nessuna'))
    for pid, u in persone.items():
        check('%-20s non e\' una postazione' % pid, not u.get('e_postazione'))
        check('%-20s non ha permessi' % pid, not (u.get('permissions') or []),
              u.get('permissions'))
        check('%-20s non comanda' % pid, not u.get('is_capo'))
    check('nessuna persona compare all\'ingresso',
          not (set(persone) & set(postazioni)))


    print('\n3) La bacheca della timbratrice ha ancora i nomi da mostrare')
    # Dal SERVER di prova, non importando il backend qui: importarlo in questo
    # processo apriva il database VERO (e all'import lo migrava).
    try:
        _c, d = uff.get('/api/ore/operai')
        nomi = {o['id'] for o in (d.get('operai') or [])}
        # Non nomi precisi: le persone le mette chi lavora (e se ne vanno).
        check('gli operai compaiono sulla bacheca', bool(nomi), sorted(nomi))
        check('le postazioni NON compaiono sulla bacheca',
              not (set(POSTAZIONI) & nomi), sorted(nomi))
    except Exception as e:
        # elenco_operai legge il database dell'istanza locale, non quella di
        # prova: se puntano a database diversi il controllo non e' valido.
        print('  (bacheca non verificabile da qui: %s)' % str(e)[:60])

    print('\n4) La postazione laser porta la delega del capo')
    laser = utenti.get('postazione-laser', {})
    check('il laser puo\' decidere come il capo', laser.get('is_capo') is True)
    for pid in ('postazione-timbratrice', 'postazione-visione',
                'postazione-commerciale'):
        check('%-28s NON ha la delega' % pid, not utenti.get(pid, {}).get('is_capo'))

    print('\n5) Le utenze personali sostituite sono spente, non cancellate')
    # L'elenco normale mostra solo chi e' attivo: per vedere anche le utenze
    # spente bisogna chiederle. Devono esserci ancora, perche' l'archivio
    # delle azioni le nomina, e senza la riga lo storico diventa illeggibile.
    _, tutti = uff.get('/api/users?include_inactive=true')
    tutte = {u['id']: u for u in (tutti.get('users') or [])}
    for vecchio in ('elena-impiegata', 'paolo-responsabile',
                    'stefano-responsabile'):
        u = tutte.get(vecchio)
        if u is None:
            # installazione nata dopo il passaggio alle postazioni: non c'e' mai stata
            print("   %-22s non c'e' in questo database (mai esistita qui)" % vecchio)
            continue
        if u:
            check("%-22s e' spenta" % vecchio, u.get('is_active') is False)
        check("%-22s non compare all'ingresso" % vecchio,
              vecchio not in postazioni)


    print('\n6) La vecchia "sessione" del browser non esiste piu\'')
    # Prima la pagina chiedeva al server se lo user_id in memoria valeva
    # ancora; ora chi sei lo sa il server (cookie del dispositivo + PIN).
    stato, _ = chiama('/api/auth/sessione/postazione-laser')
    check('/api/auth/sessione/<id> tolta (404)', stato == 404, stato)

    print("\n7) Ogni pagina usa lo stesso ingresso")
    # Dove sta di casa una stazione lo dice il server (/api/accesso/io) e lo
    # applica ft-accesso.js, uguale in tutte le pagine: prima la mappa era
    # copiata in cinque pagine e una restava sempre indietro.
    import io as _io
    for pagina in ('admin.html', 'archivio.html', 'preventivi.html', 'capo-officina.html',
                   'impiegata.html', 'laser.html', 'operaio-info.html', 'ore.html',
                   'ufficio-ore.html', 'dashboard-live.html', 'dxf-editor.html', 'preview-dxf.html'):
        percorso = os.path.join(_APP, 'frontend', pagina)
        testo = _io.open(percorso, encoding='utf-8').read()
        check('%-22s usa ft-accesso.js' % pagina, '/ft-accesso.js' in testo)
        check('%-22s non manda piu\' X-User-Id' % pagina, 'X-User-Id' not in testo)

    print('\n' + '=' * 60)
    print('PASSATI: %d   FALLITI: %d' % (OK, len(KO)))
    if KO:
        print('Falliti: ' + ', '.join(KO))
    print('=' * 60)
    return 1 if KO else 0


if __name__ == '__main__':
    sys.exit(main())
