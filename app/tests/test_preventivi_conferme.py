"""Registro delle CONFERME del pezzo (preventivatore): si salva e torna uguale.

Il problema (14/10/2026): le domande di un disegno ("Spessore 5 o 4 mm?",
"Peso diverso dal cartiglio", "Contorno da verificare"...) si ricalcolavano
dai dati del momento. Alla riapertura del preventivo quelle sul cartiglio
incerto sparivano senza risposta, perche' la confidenza letta all'import non
si salvava; le risposte non lasciavano traccia.

Ora il pezzo porta un registro `conferme` (domande viste, risposte, chi le ha
chiuse, confidenza del cartiglio). Qui si prova che:
 1. il registro passa dal salvataggio e torna uguale alla riapertura
 2. solo campi noti e testo corto (niente dati arbitrari in colonna)
 3. voci con stato sconosciuto si scartano, il resto resta
 4. la confidenza fuori da 0..1 non passa
 5. senza registro non si scrive nulla

Gira su DATABASE TEMPORANEO:
    python app/tests/test_preventivi_conferme.py
"""
import os
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
from backend.models import Base, User  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_conf_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'), connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

from backend.database import PreventivoManager  # noqa: E402

OK = 0
KO = []


def check(nome, cond, extra=''):
    global OK
    if cond:
        OK += 1
        print(f'  [OK] {nome}')
    else:
        KO.append(nome)
        print(f'  [KO] {nome} {extra}')


FIRMA = {'materiale': 'S235', 'spessore': 4, 'area': 2.83, 'taglio': 0.9, 'inneschi': 2,
         'pieghe': 0, 'disegno': '20PA00848-00.DXF', 'costo': None}
REGISTRO = {
    'vista': True,
    'firma': FIRMA,
    'ultima': {'k': 'spessore', 'g': 'Spessore da confermare', 'l': 'Usa 4 mm', 'r': True, 't': 1760000000000},
    'incerti': {'materiale': 0.55, 'spessore': 1, 'suggerito': 'INOX_304'},
    'd': {
        'spessore': {'stato': 'fatta', 'g': 'Spessore da confermare', 't': 'Spessore: 5 o 4 mm?',
                     'risposta': 'Usa 4 mm', 'quando': 1760000000000, 'l': 'warn', 'firma': FIRMA},
        'peso_diverso_dal_cartiglio': {'stato': 'aperta', 'g': 'Peso diverso dal cartiglio', 'l': 'warn',
                                       't': 'Peso 0,89 kg, nel cartiglio 1,11 kg',
                                       'nuova': "perché è cambiato lo spessore (5 mm → 4 mm)"},
        'contorno_da_verificare': {'stato': 'risolta', 'g': 'Contorno da verificare', 'l': 'err',
                                   'da': "da «Ho verificato» su peso diverso dal cartiglio", 'firma': FIRMA},
        'avviso:Spessore fuori tabella': {'stato': 'fatta', 'risposta': 'Ho verificato'},
        'strana': {'stato': 'boh', 'g': 'x'},
    },
}


def main():
    s = models.SessionLocal()
    s.add(User(id='commerciale', name='Marco', role='Commerciale', is_active=True))
    s.commit(); s.close()
    pr = PreventivoManager.create('DECA', 'commerciale', quantita=1)
    pid = pr['id'] if isinstance(pr, dict) else pr

    base = {'codice': '20PA00848-00', 'quantita': 1, 'materiale': 'S235', 'spessore_mm': 4,
            'area_dm2': 2.83, 'perimetro_taglio_m': 0.9, 'n_forature': 2, 'dxf_filename': '20PA00848-00.DXF'}
    r = PreventivoManager.replace_articoli(pid, [dict(base, conferme=REGISTRO), dict(base, codice='SENZA')])
    check('salvataggio accettato', not (isinstance(r, dict) and r.get('error')), r)
    arts = PreventivoManager.get(pid, include_children=True)['articoli']
    a = next(x for x in arts if x['codice'] == '20PA00848-00')
    b = next(x for x in arts if x['codice'] == 'SENZA')
    c = a.get('conferme') or {}
    d = c.get('d') or {}

    # 1. torna uguale
    check('registro presente alla riapertura', bool(c))
    check('risposta conservata', (d.get('spessore') or {}).get('risposta') == 'Usa 4 mm', d.get('spessore'))
    check('firma della risposta conservata', (d.get('spessore') or {}).get('firma', {}).get('spessore') == 4)
    check('domanda nuova col suo perché', 'spessore' in ((d.get('peso_diverso_dal_cartiglio') or {}).get('nuova') or ''))
    check('"risolta da" conservato', 'Ho verificato' in ((d.get('contorno_da_verificare') or {}).get('da') or ''))
    check('avviso visto conservato (chiave col testo)', (d.get('avviso:Spessore fuori tabella') or {}).get('stato') == 'fatta')
    check('confidenza del cartiglio conservata', c.get('incerti') == {'materiale': 0.55, 'spessore': 1, 'suggerito': 'INOX_304'},
          c.get('incerti'))
    check('ultima risposta conservata', (c.get('ultima') or {}).get('l') == 'Usa 4 mm')
    check('istantanea "vista" conservata', c.get('vista') is True and (c.get('firma') or {}).get('area') == 2.83)

    # 3. stato sconosciuto scartato
    check('voce con stato sconosciuto scartata', 'strana' not in d)

    # 2. campi noti, testo corto
    sporco = {'d': {'x': {'stato': 'fatta', 'risposta': 'A' * 500, 'hack': '<script>', 'firma': {'spessore': 3, 'boh': 1}}},
              'altro': 'non ammesso', 'incerti': {'materiale': 7, 'spessore': -1}}
    p = PreventivoManager._pulisci_conferme(sporco)
    check('campo sconosciuto tolto', 'altro' not in p and 'hack' not in p['d']['x'], p)
    check('testo accorciato', len(p['d']['x']['risposta']) == 80)
    check('firma solo coi campi noti', p['d']['x']['firma'] == {'spessore': 3})
    # 4. confidenza fuori scala
    check('confidenza fuori da 0..1 scartata', 'incerti' not in p, p.get('incerti'))
    # 5. niente registro, niente campo
    check('pezzo senza registro: nessun campo', 'conferme' not in b, b.get('conferme'))
    check('registro non dizionario: nulla', PreventivoManager._pulisci_conferme('ciao') is None)

    print(f'\n{OK} ok, {len(KO)} falliti')
    if KO:
        print('FALLITI:', KO)
    return 1 if KO else 0


if __name__ == '__main__':
    try:
        rc = main()
    finally:
        try:
            _ENG.dispose(); os.remove(_TMP)
        except OSError:
            pass
    sys.exit(rc)
