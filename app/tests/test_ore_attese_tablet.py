"""Test dell'OBIETTIVO ORE sul tablet (8 ore) e della conferma esplicita.

L'operaio deve accorgersi SUBITO se non arriva alle ore dovute, non il giorno
dopo tramite l'ufficio. Non c'e' blocco: chi ha lavorato meno dichiara il vero
e conferma, e la conferma viene registrata per distinguere "sa quello che fa"
da "ha dimenticato".

Copre:
 1. le ore attese arrivano al tablet insieme alla giornata
 2. giornata non dovuta (assenza/festivo/giorno non lavorativo) -> nessun obiettivo
 3. la conferma dello scostamento viene salvata e restituita
 4. correggendo fino alle ore attese la conferma decade
 5. l'ufficio vede quali giornate corte erano confermate dall'operaio
 6. la conferma NON e' una scorciatoia: l'anomalia resta aperta per l'ufficio

Gira su DATABASE TEMPORANEO. Esecuzione: python app/tests/test_ore_attese_tablet.py
"""
import os
import sys
import tempfile
import uuid
from datetime import date, timedelta

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
from backend import models_ore  # noqa: E402
from backend.models_ore import Cliente, GiornataOre, OreAttese  # noqa: E402

_TMP = os.path.join(tempfile.gettempdir(), f'test_attese_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'),
                     connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

from backend.app import app  # noqa: E402
from backend.auth_device import crea_token  # noqa: E402
from backend import ore_service as svc  # noqa: E402
from backend import anomalie_service as an  # noqa: E402

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


OGGI = svc.oggi_locale()
# Un giorno feriale passato, per i test che non dipendono da "oggi"
FERIALE = OGGI - timedelta(days=1)
while FERIALE.isoweekday() > 5:
    FERIALE -= timedelta(days=1)


def setup():
    s = models.SessionLocal()
    try:
        s.add(User(id='enzo', name='Enzo Bianchi', role='Operaio Officina',
                   is_active=True))
        s.add(Cliente(id=str(uuid.uuid4()), nome='Cliente Alfa', attivo=True))
        # 8 ore, lunedi-venerdi: la configurazione che il seed applica agli operai
        s.add(OreAttese(id=str(uuid.uuid4()), operatore_id='enzo',
                        tenuto_alla_compilazione=True, minuti_attesi=480,
                        giorni_settimana=[1, 2, 3, 4, 5]))
        s.commit()
    finally:
        s.close()
    return (crea_token('Tablet TEST', 'ore', 'test')['token'],
            crea_token('Ufficio TEST', 'ufficio', 'test')['token'])


def main():
    t_ore, t_uff = setup()
    c = app.test_client()
    H = lambda tok: {'X-Device-Token': tok}  # noqa: E731

    # =====================================================================
    print('\n1) Le ore attese arrivano al tablet insieme alla giornata')
    r = c.get(f'/api/ore/giornata?operatore_id=enzo&data={OGGI.isoformat()}',
              headers=H(t_ore))
    g = r.get_json().get('giornata', {})
    dovuta_oggi = OGGI.isoweekday() <= 5
    check('la giornata include minuti_attesi', 'minuti_attesi' in g, g)
    if dovuta_oggi:
        check('oggi sono dovute 8 ore (480 min)', g.get('minuti_attesi') == 480, g)
    else:
        check('oggi non e giorno lavorativo: nessun obiettivo',
              g.get('minuti_attesi') is None, g)

    r = c.get(f'/api/ore/giornata?operatore_id=enzo&data={FERIALE.isoformat()}',
              headers=H(t_uff))
    check('anche in un feriale passato sono 480',
          r.get_json()['giornata'].get('minuti_attesi') == 480,
          r.get_json()['giornata'])

    # =====================================================================
    print('\n2) Giornata non dovuta: nessun obiettivo da rispettare')
    sabato = OGGI
    while sabato.isoweekday() != 6:
        sabato -= timedelta(days=1)
    r = c.get(f'/api/ore/giornata?operatore_id=enzo&data={sabato.isoformat()}',
              headers=H(t_uff))
    check('sabato: nessun obiettivo',
          r.get_json()['giornata'].get('minuti_attesi') is None)

    an.salva_eccezione('enzo', FERIALE, 'assenza', nota='ferie', da='elena')
    r = c.get(f'/api/ore/giornata?operatore_id=enzo&data={FERIALE.isoformat()}',
              headers=H(t_uff))
    check('assenza approvata: nessun obiettivo',
          r.get_json()['giornata'].get('minuti_attesi') is None)
    an.elimina_eccezione('enzo', FERIALE)
    r = c.get(f'/api/ore/giornata?operatore_id=enzo&data={FERIALE.isoformat()}',
              headers=H(t_uff))
    check('tolta l eccezione, l obiettivo torna',
          r.get_json()['giornata'].get('minuti_attesi') == 480)

    # =====================================================================
    print('\n3) La conferma dello scostamento viene registrata')
    corpo = {'operatore_id': 'enzo', 'data': FERIALE.isoformat(),
             'righe': [{'cliente': 'Cliente Alfa', 'minuti': 300}],
             'revisione_attesa': 0, 'richiesta_id': 'c1',
             'scostamento_confermato': True}
    r = c.post('/api/ore/giornata', json=corpo, headers=H(t_uff))
    g = r.get_json().get('giornata', {})
    check('salvataggio riuscito', r.status_code == 200 and g.get('totale_minuti') == 300,
          r.get_json())
    check('la conferma e registrata', g.get('scostamento_confermato') is True, g)
    check('la risposta riporta anche l obiettivo', g.get('minuti_attesi') == 480, g)

    s = models.SessionLocal()
    try:
        riga = s.query(GiornataOre).filter(GiornataOre.operatore_id == 'enzo',
                                           GiornataOre.data == FERIALE).first()
        salvata = bool(riga.scostamento_confermato)
    finally:
        s.close()
    check('la conferma e persistita a database', salvata is True)

    # =====================================================================
    print('\n4) Correggendo fino alle ore dovute la conferma decade')
    corpo = {'operatore_id': 'enzo', 'data': FERIALE.isoformat(),
             'righe': [{'cliente': 'Cliente Alfa', 'minuti': 480}],
             'revisione_attesa': g['revisione'], 'richiesta_id': 'c2',
             'scostamento_confermato': False}
    r = c.post('/api/ore/giornata', json=corpo, headers=H(t_uff))
    g2 = r.get_json().get('giornata', {})
    check('giornata completa salvata', g2.get('totale_minuti') == 480, g2)
    check('nessuna conferma appesa', g2.get('scostamento_confermato') is False, g2)

    # =====================================================================
    print('\n5) L ufficio vede quali giornate corte erano confermate')
    corpo = {'operatore_id': 'enzo', 'data': FERIALE.isoformat(),
             'righe': [{'cliente': 'Cliente Alfa', 'minuti': 240}],
             'revisione_attesa': g2['revisione'], 'richiesta_id': 'c3',
             'scostamento_confermato': True}
    c.post('/api/ore/giornata', json=corpo, headers=H(t_uff))
    an.controlla_periodo(dal=FERIALE, al=FERIALE)
    anomalie = [a for a in an.elenco_anomalie(stato='aperta')
                if a['operatore_id'] == 'enzo' and a['data'] == FERIALE.isoformat()]
    check('anomalia presente per la giornata corta', len(anomalie) == 1, anomalie)
    check('segnalata come confermata dall operaio',
          anomalie and anomalie[0].get('confermata_dall_operaio') is True, anomalie)
    check('riporta dichiarato e atteso',
          anomalie and anomalie[0]['minuti_dichiarati'] == 240
          and anomalie[0]['minuti_attesi'] == 480, anomalie)

    # =====================================================================
    print('\n6) La conferma non nasconde il problema all ufficio')
    check('l anomalia resta APERTA nonostante la conferma',
          anomalie and anomalie[0]['stato'] == 'aperta', anomalie)

    r = c.get('/api/ore/anomalie', headers=H(t_uff))
    check('visibile anche dalla pagina ufficio', r.status_code == 200, r.status_code)

    print('\n' + '=' * 60)
    print(f'PASSATI: {OK}   FALLITI: {len(KO)}')
    if KO:
        print('Falliti: ' + ', '.join(KO))
    print('=' * 60)
    return 1 if KO else 0


if __name__ == '__main__':
    code = main()
    try:
        _ENG.dispose()
        os.remove(_TMP)
    except Exception:
        pass
    sys.exit(code)
