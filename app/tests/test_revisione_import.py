"""Revisione diversa all'import (28/10/2026, punto 4 della proposta Lantek del 27/10).

Caso vero, DECA A 001252: l'ordine chiede 25NDSSA0615-00, nella cartella
c'e' solo 25NDSSA0615-01.DXF. FerroTrack lo abbinava "per somiglianza" e
nessuno diceva che la revisione era un'altra.

 1. riconoscimento: -00 / -01 si', stesso codice senza revisione no,
    codici diversi no, variante DECA -G- si'
 2. ufficio: /analizza porta "revisioni_diverse"; /conferma rifiuta (409)
    senza "Ho controllato", crea con la spunta e lo scrive nel registro;
    con quantita' 0 su quel pezzo non serve
 3. laser: nel controllo dei disegni il motivo e' "Revisione diversa"

    python app/tests/test_revisione_import.py
"""
import io
import os
import sys

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from tests import test_ordine_pacchetto as T  # noqa: E402

A = T.A
from backend import models  # noqa: E402
from backend.models import User  # noqa: E402

check = T.check


def pacchetto():
    cons = T.CONSEGNA.strftime('%d/%m/%y')
    pdf = T.pdf_ordine(righe=[f'PZ-A100-00 C26-101 Piastra base sp.3 S235JR 4,00Nr {cons}',
                              f'PZ-B200-00 C26-101 Staffa sp.2 AISI 304 6,00Nr {cons}'], numero='1252')
    files = [(io.BytesIO(pdf), 'OAFA1252.pdf'), (io.BytesIO(T.dxf_rettangolo(200, 100)), 'PZ-A100-01.dxf'),
             (io.BytesIO(T.DXF['PZ-B200-00.dxf']), 'PZ-B200-00.dxf')]
    return files, ['LS 1252/' + n for _, n in files]


def main():
    s = models.SessionLocal()
    try:
        s.add(User(id='ufficio', name='Amministrazione', role='Amministrazione', is_active=True))
        s.commit()
    finally:
        s.close()
    from tests.accesso_aiuto import postazioni, persona, entra, modalita
    modalita('protetto')
    postazioni()
    persona('ufficio', 'Amministrazione', 'Amministrazione', pin='2580')
    uff = entra(A.app, 'ufficio', pin='2580')

    print('\n1) Riconoscimento')
    rd = A._revisione_diversa
    check('-00 nell\'ordine, disegno -01: diversa', rd('25NDSSA0615-00', '25NDSSA0615-01.DXF') == {'rev_ordine': '00', 'rev_disegno': '01'})
    check('stessa revisione: no', rd('25NDSSA0615-00', '25NDSSA0615-00.DXF') is None)
    check('ordine senza revisione: no', rd('25NDSSA0615', '25NDSSA0615-01.DXF') is None)
    check('codici diversi: no', rd('25NDSSA0616-00', '25NDSSA0615-01.DXF') is None)
    check('variante DECA -G-: si\'', rd('191700609-G-00', '191700609-01.dxf') == {'rev_ordine': '00', 'rev_disegno': '01'})

    print('\n2) Ufficio: analisi e conferma')
    files, paths = pacchetto()
    r = T.analizza(uff, files=files, paths=paths)
    d = r.get_json() or {}
    check('analisi riuscita', r.status_code == 200 and d.get('success'), d.get('error'))
    rv = d.get('revisioni_diverse') or []
    check('revisioni_diverse: PZ-A100-00 -> disegno -01',
          [(x['codice'], x['rev_ordine'], x['rev_disegno'], x['disegno']) for x in rv] == [('PZ-A100-00', '00', '01', 'PZ-A100-01.dxf')], rv)
    riga = next((x for x in d.get('righe') or [] if x['codice'] == 'PZ-A100-00'), {})
    check('nella riga: avviso tecnico "Revisione diversa"',
          any(a.startswith('Revisione diversa') for a in riga.get('avvisi_tecnici') or []), riga.get('avvisi_tecnici'))
    pid = d.get('pacchetto_id')
    r = T.conferma(uff, pid, numero_ordine='1252', cliente='DECA')
    dd = r.get_json() or {}
    check('conferma senza "Ho controllato": 409 revisioni_diverse',
          r.status_code == 409 and dd.get('codice') == 'revisioni_diverse'
          and [x['codice'] for x in dd.get('revisioni_diverse') or []] == ['PZ-A100-00'], dd)
    r = T.conferma(uff, pid, numero_ordine='1252', cliente='DECA', revisioni_controllate=['ALTRO'])
    check('spunta su un altro codice: 409', r.status_code == 409, r.get_json())
    r = T.conferma(uff, pid, numero_ordine='1252', cliente='DECA', revisioni_controllate=['pz-a100-00'])
    dd = r.get_json() or {}
    check('con "Ho controllato": ordine creato', r.status_code == 201 and dd.get('order_id'), dd)
    from backend.database import get_session
    from backend.models import AuditLog
    sess = get_session()
    try:
        au = sess.query(AuditLog).filter(AuditLog.action == 'ORDER_PACKAGE_REVISIONE').all()
        check('nel registro: chi ha controllato e quale revisione',
              len(au) == 1 and 'PZ-A100-00' in (au[0].detail or '') and '-01' in (au[0].detail or ''), [a.detail for a in au])
    finally:
        sess.close()

    files, paths = pacchetto()
    d = T.analizza(uff, files=files, paths=paths).get_json() or {}
    ida = next(x['id'] for x in d['righe'] if x['codice'] == 'PZ-A100-00')
    r = T.conferma(uff, d['pacchetto_id'], numero_ordine='1253', cliente='DECA',
                   righe=[{'id': ida, 'quantita': 0}])
    check('PZ-A100-00 tolto (quantita\' 0): nessuna spunta serve', r.status_code == 201, r.get_json())

    print('\n3) Laser: il motivo nel controllo dei disegni')
    a = {'codice': 'PZ-A100-00', 'dxf_filename': 'PZ-A100-01.dxf',
         'abbinamento': {'tipo': 'somiglianza', 'confermato': False}}
    c = A._controllo_pezzo(a) or {}
    check('controllo: "revisione diversa: l\'ordine chiede la -00, il disegno è la -01"',
          c.get('stato') == 'da_confermare' and c.get('motivi') == ["revisione diversa: l'ordine chiede la -00, il disegno è la -01"], c)
    check('frase corta: "Revisione diversa: ..."',
          A.motivo_breve(c['motivi'][0]) == "Revisione diversa: l'ordine chiede la -00, il disegno è la -01",
          A.motivo_breve(c['motivi'][0]))
    a = {'codice': 'PZ-A100-00', 'dxf_filename': 'PZ-A1OO-00.dxf', 'abbinamento': {'tipo': 'somiglianza', 'confermato': False}}
    check('somiglianza senza revisione diversa: come prima',
          'somiglianza' in ((A._controllo_pezzo(a) or {}).get('motivi') or [''])[0])


if __name__ == '__main__':
    import shutil
    try:
        main()
    finally:
        A.ConfigManager.load_config = T._config_vera
        try:
            T._ENG.dispose()
            os.remove(T._TMP)
        except Exception:
            pass
        shutil.rmtree(T._CARTELLE, ignore_errors=True)
    print(f'\nPASSATI: {T.OK}   FALLITI: {len(T.KO)}')
    sys.exit(1 if T.KO else 0)
