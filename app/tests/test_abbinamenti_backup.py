"""Copia automatica degli abbinamenti Lantek (backend/abbinamenti_backup.py).

 1. ogni abbinamento creato/cambiato/tolto aggiorna dati_clienti/
    abbinamenti_lantek.json e .csv (stesso formato del tool), con la copia di
    prima nello storico quando sparisce qualcosa;
 2. all'avvio: copia mancante o vecchia -> rifatta; gia' allineata -> non
    toccata;
 3. all'avvio con la tabella VUOTA e la copia piena: niente sovrascrittura,
    un avviso (uno solo) all'amministrazione; "Ripristina" rimette le righe;
 4. le rotte Admin (stato e ripristino).

Database e cartella TEMPORANEI:  python app/tests/test_abbinamenti_backup.py
"""
import csv
import json
import os
import shutil
import sys
import tempfile
import uuid

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)
os.environ['FERROTRACK_SKIP_DB_INIT'] = '1'

import werkzeug  # noqa: E402
if not hasattr(werkzeug, '__version__'):
    werkzeug.__version__ = '3.0.0'
from sqlalchemy import create_engine  # noqa: E402

from backend import models  # noqa: E402
from backend.models import Base, AssiemeLantek, Notification  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_abb_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'), connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

from backend import abbinamenti_backup as AB  # noqa: E402
from backend import ordine_pdf_lantek as OPL  # noqa: E402
from backend.app import app  # noqa: E402

_CART = tempfile.mkdtemp(prefix='test_abb_dati_')
AB.CARTELLA = os.path.join(_CART, 'dati_clienti')
OK = KO = 0


def check(nome, cond, extra=None):
    global OK, KO
    if cond:
        OK += 1
        print(f'  OK  {nome}')
    else:
        KO += 1
        print(f'  KO  {nome} :: {extra!r}'[:500])


def _file():
    return json.load(open(AB.percorso_json(), encoding='utf-8'))


def _avvisi():
    s = models.get_session()
    try:
        return s.query(Notification).filter(Notification.notification_type == AB.TIPO_AVVISO).all()
    finally:
        s.close()


def _svuota_tabella():
    s = models.get_session()
    try:
        s.query(AssiemeLantek).delete()
        s.commit()
    finally:
        s.close()


def test_scritture():
    print('\n1) Ogni abbinamento aggiorna la copia')
    check('avvio senza abbinamenti e senza copia: niente', AB.avvio() == 'niente' and not os.path.exists(AB.percorso_json()))
    OPL.salva_abbinamento('ass-1', [{'codice': 'P1-00', 'quantita': 2}, {'codice': 'P2-00', 'quantita': 1}],
                          False, 'Assieme uno', 'Poliform', 'Stefano')
    d = _file()
    check('json scritto col formato del tool', d.get('tabella') == 'assiemi_lantek'
          and 'pezzo_lantek' in d.get('colonne', []) and len(d['righe']) == 2, d)
    check('codice in maiuscolo, pezzi e quantita\'',
          sorted((r['codice'], r['pezzo_lantek'], r['quantita']) for r in d['righe'])
          == [('ASS-1', 'P1-00', 2), ('ASS-1', 'P2-00', 1)], d['righe'])
    raw = open(AB.percorso_csv(), 'rb').read()
    check('csv per Excel (BOM, punto e virgola)', raw[:3] == b'\xef\xbb\xbf' and b';' in raw.split(b'\r\n')[0])
    righe_csv = list(csv.DictReader(open(AB.percorso_csv(), encoding='utf-8-sig'), delimiter=';'))
    check('csv con le stesse righe', len(righe_csv) == 2, righe_csv)
    check('nessun file temporaneo lasciato', not [f for f in os.listdir(AB.CARTELLA) if f.endswith('.tmp')])

    OPL.salva_abbinamento('ASS-1', [{'codice': 'P3-00', 'quantita': 4}], False, 'Assieme uno', 'Poliform', 'Stefano')
    d = _file()
    check('abbinamento cambiato: copia aggiornata', [(r['pezzo_lantek'], r['quantita']) for r in d['righe']] == [('P3-00', 4)], d['righe'])
    storico = os.path.join(AB.CARTELLA, 'storico_abbinamenti')
    check('la copia di prima resta nello storico', os.path.isdir(storico) and len(os.listdir(storico)) == 1)
    OPL.salva_abbinamento('ass-2', [], True, 'Vite', 'B&B', 'Stefano')
    check('"non va al laser" nella copia', any(r['codice'] == 'ASS-2' and r['non_laser'] for r in _file()['righe']))
    OPL.dimentica_abbinamento('ass-2')
    check('abbinamento tolto: tolto anche dalla copia', [r['codice'] for r in _file()['righe']] == ['ASS-1'])


def test_avvio():
    print('\n2) Avvio: copia mancante, vecchia, allineata')
    os.remove(AB.percorso_json())
    check('copia mancante -> rifatta', AB.avvio() == 'esportato' and os.path.exists(AB.percorso_json()))
    t0 = os.path.getmtime(AB.percorso_json())
    check('copia allineata -> non toccata', AB.avvio() == 'gia_allineato' and os.path.getmtime(AB.percorso_json()) == t0)
    d = _file()
    d['righe'][0]['quantita'] = 99
    json.dump(d, open(AB.percorso_json(), 'w', encoding='utf-8'))
    check('copia vecchia -> rifatta', AB.avvio() == 'esportato' and _file()['righe'][0]['quantita'] == 4)


def test_tabella_vuota():
    print('\n3) Tabella vuota ma copia piena: avviso, niente sovrascrittura')
    from tests.accesso_aiuto import persona
    persona('admin-test', 'Admin Prova', pin='2580', admin=True)
    prima = open(AB.percorso_json(), 'rb').read()
    _svuota_tabella()
    check('esito "da ripristinare"', AB.avvio() == 'da_ripristinare')
    check('copia NON sovrascritta', open(AB.percorso_json(), 'rb').read() == prima)
    av = _avvisi()
    check('avviso all\'amministrazione', len(av) == 1 and av[0].user_id == 'admin-test'
          and 'Ripristina' in av[0].message, [(a.user_id, a.message) for a in av])
    AB.avvio()
    check('al riavvio nessun avviso doppio', len(_avvisi()) == 1)
    st = AB.stato()
    check('stato: 0 nel database, 1 nella copia, da ripristinare', st['n_tabella'] == 0 and st['n_file'] == 1
          and st['da_ripristinare'], st)


def test_rotte():
    print('\n4) Admin: stato e ripristino')
    from tests.accesso_aiuto import postazioni, entra, eleva, modalita
    modalita('protetto')
    postazioni()
    c = entra(app, 'ufficio', pin='2580')
    r = c.get('/api/admin/abbinamenti-lantek')
    d = r.get_json() or {}
    check('GET stato', r.status_code == 200 and d.get('n_mancanti') == 1, (r.status_code, d))
    eleva(c, '2580')
    r = c.post('/api/admin/abbinamenti-lantek/ripristina')
    d = r.get_json() or {}
    check('ripristino: 1 abbinamento rimesso', r.status_code == 200 and d.get('ripristinati') == 1
          and d.get('n_tabella') == 1, (r.status_code, d))
    check('avviso chiuso dopo il ripristino', all(a.is_read for a in _avvisi()))
    r = c.post('/api/admin/abbinamenti-lantek/ripristina')
    check('secondo ripristino: niente doppioni', (r.get_json() or {}).get('ripristinati') == 0, r.get_json())
    s = models.get_session()
    try:
        a = s.query(AssiemeLantek).one()
        check('riga rimessa uguale (codice, pezzo, quantita\', chi)',
              (a.codice, a.pezzo_lantek, a.quantita, a.creato_da) == ('ASS-1', 'P3-00', 4, 'Stefano'),
              (a.codice, a.pezzo_lantek, a.quantita, a.creato_da))
    finally:
        s.close()
    anon = app.test_client()
    check('senza accesso: niente stato', anon.get('/api/admin/abbinamenti-lantek').status_code in (401, 403))


def main():
    try:
        test_scritture()
        test_avvio()
        test_tabella_vuota()
        test_rotte()
    finally:
        shutil.rmtree(_CART, ignore_errors=True)
        try:
            _ENG.dispose()
            os.remove(_TMP)
        except OSError:
            pass
    print(f'\n{OK} OK, {KO} KO')
    sys.exit(1 if KO else 0)


if __name__ == '__main__':
    main()
