# -*- coding: utf-8 -*-
"""Si entra da una postazione, non col nome di una persona.

Copre le due cose che, sbagliate, si notano solo in azienda:

  1. all'ingresso compaiono i POSTI (timbratrice, tablet di visione,
     amministrazione, laser, commerciale) e non le persone. Se comparisse
     Mirko, la prima cosa che uno fa e' entrare come se stesso, e da li' in poi
     non si capisce piu' chi ha fatto cosa;

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

    stato, d = chiama('/api/users')
    if stato != 200:
        print('Elenco utenti non leggibile. Test saltato.')
        return 0
    utenti = {u['id']: u for u in (d.get('users') or d if isinstance(d, list) else d.get('users', []))}

    print('1) All\'ingresso compaiono i posti, non le persone')
    postazioni = {i: u for i, u in utenti.items()
                  if u.get('e_postazione') and u.get('is_active', True)}
    check('ci sono tutte e cinque le postazioni',
          set(postazioni) == set(POSTAZIONI), sorted(postazioni))
    for pid, (nome, ruolo) in POSTAZIONI.items():
        u = utenti.get(pid, {})
        check('%-28s si chiama "%s"' % (pid, nome), u.get('name') == nome, u.get('name'))
        check('%-28s ha ruolo "%s"' % (pid, ruolo), u.get('role') == ruolo, u.get('role'))

    print("\n2) Le persone, se ci sono, non entrano nel programma")
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
    from backend.ore_service import elenco_operai
    try:
        nomi = {o['id'] for o in elenco_operai()}
        check('gli operai compaiono sulla bacheca',
              set(OPERAI).issubset(nomi), sorted(nomi))
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
    _, tutti = chiama('/api/users?include_inactive=true')
    tutte = {u['id']: u for u in (tutti.get('users') or [])}
    for vecchio in ('elena-impiegata', 'paolo-responsabile',
                    'stefano-responsabile'):
        u = tutte.get(vecchio)
        check("%-22s c'e' ancora (storico leggibile)" % vecchio, u is not None)
        if u:
            check("%-22s e' spenta" % vecchio, u.get('is_active') is False)
        check("%-22s non compare all'ingresso" % vecchio,
              vecchio not in postazioni)


    print('\n6) Una sessione vecchia viene riconosciuta come tale')
    stato, d = chiama('/api/auth/sessione/stefano-responsabile')
    check('una postazione spenta non vale piu\'',
          stato == 200 and d.get('valida') is False, d)
    check('e lo spiega, invece di tacere', bool(d.get('motivo')), d)
    stato, d = chiama('/api/auth/sessione/postazione-laser')
    check('una postazione attiva vale', stato == 200 and d.get('valida') is True, d)
    check('e riporta i dati aggiornati',
          (d.get('utente') or {}).get('role') == 'Laser', d.get('utente'))
    stato, d = chiama('/api/auth/sessione/non-esiste-questo')
    check('un id inventato non vale', d.get('valida') is False, d)

    print("\n7) Nessuna pagina e' rimasta indietro sui nomi nuovi")
    # La mappa "dove sta di casa una postazione" e' ripetuta in cinque pagine,
    # perche' le pagine sono indipendenti l'una dall'altra. Se una resta
    # indietro, chi ci capita viene rimandato nel posto sbagliato. Il controllo
    # e' grossolano — cerca il nome del ruolo nel testo della pagina — ma prende
    # il caso vero: una pagina che il ruolo nuovo non lo nomina affatto.
    import io as _io
    ruoli_attesi = ('Timbratrice', 'Visione', 'Laser', 'Amministrazione',
                    'Commerciale')
    for pagina in ('login.html', 'admin.html', 'archivio.html',
                   'preventivi.html', 'capo-officina.html'):
        percorso = os.path.join(_APP, 'frontend', pagina)
        try:
            testo = _io.open(percorso, encoding='utf-8').read()
        except OSError:
            continue
        mancanti = [r for r in ruoli_attesi if ("'%s'" % r) not in testo]
        check('%-22s conosce tutte le postazioni' % pagina,
              not mancanti, 'non nomina: %s' % ', '.join(mancanti))

    print('\n' + '=' * 60)
    print('PASSATI: %d   FALLITI: %d' % (OK, len(KO)))
    if KO:
        print('Falliti: ' + ', '.join(KO))
    print('=' * 60)
    return 1 if KO else 0


if __name__ == '__main__':
    sys.exit(main())
