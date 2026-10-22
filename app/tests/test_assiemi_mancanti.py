"""Assiemi dell'ordine senza componenti: non si perdono piu' in silenzio.

Caso vero, DECA A 001252 (20/10/2026): caricati i soli 27 disegni della
radice, senza le 7 sottocartelle degli assiemi. Gli assiemi sono diventati
righe senza disegno, nessuno ha avvisato, l'ordine e' stato tagliato senza
45 componenti.

Copre:
 1. assiemi_mancanti: riconoscimento (SA = assieme, PA = particolare,
    descrizioni da assieme), pacchetto e distinta
 2. caso vero LS 1252 (se la cartella c'e'): solo radice -> i 7 codici;
    cartella intera -> niente blocco
 3. ufficio: /analizza porta il blocco; /conferma rifiuta (409) senza
    "CREA", crea con "CREA" e lo registra; cartella intera: nessun blocco
 4. laser: righe rosse (mancano_disegni), "Taglio fatto" e "Manda a Lantek"
    rifiutati senza conferma esplicita, accettati con la conferma

    python app/tests/test_assiemi_mancanti.py
"""
import io
import os
import shutil
import sys
import zipfile

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)

# Stesso ambiente del test del pacchetto (database e cartelle temporanee,
# Lantek simulato spento): se ne riusano gli aiuti.
from tests import test_ordine_pacchetto as T  # noqa: E402

A = T.A
from backend import assiemi_mancanti as am  # noqa: E402
from backend import models  # noqa: E402
from backend.models import User, Order  # noqa: E402
from backend.preventivi import rfq_importer  # noqa: E402

check = T.check
LS1252 = r'C:\Users\Stefano\Desktop\AUTOCAD\TAGLIO\DECA\ORDINI\LS 1252\LS 1252'


class _Art:
    def __init__(self, codice, dxf=None, ass=None, master=False, desc='', riga=True):
        self.codice, self.matched_dxf, self.codice_assieme = codice, dxf, ass
        self.e_master, self.descrizione, self.quantita = master, desc, 1
        self.righe_ordine = [{'pos': 1}] if riga else []


class _Res:
    def __init__(self, articoli):
        self.articoli = articoli


def zip_cartella(d, solo_radice):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        for root, _dirs, files in os.walk(d):
            rel = os.path.relpath(root, d)
            if solo_radice and rel != '.':
                continue
            for f in files:
                if f.lower().endswith(('.pdf', '.dxf')) and '_cleaned' not in f:
                    p = os.path.join(root, f)
                    z.write(p, os.path.relpath(p, d).replace(os.sep, '/'))
    return buf.getvalue()


def pdf_con_assieme():
    cons = T.CONSEGNA.strftime('%d/%m/%y')
    return T.pdf_ordine(righe=[
        f'PZ-A100-00 C26-101 Piastra base sp.3 S235JR 4,00Nr {cons}',
        f'TSSA1021-00 C26-193-5 Spalla DX saldata S235JR 1,00Nr {cons}',
        f'PZ-B200-00 C26-101 Staffa sp.2 AISI 304 6,00Nr {cons}'], numero='1252')


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
    laser = entra(A.app, 'laser')

    # =====================================================================
    print('\n1) Riconoscimento')
    check('SA = assieme', am.e_assieme('25NDSSA1021') and am.e_assieme('08SA01057-00'))
    check('PA = particolare, anche con "spalla"', not am.e_assieme('25NDSPA1241-00', 'Spalla sx'))
    check('descrizione da assieme', am.e_assieme('X-100', 'Struttura saldata'))
    check('pezzo normale', not am.e_assieme('X-100', 'Piastra sp.3'))
    check('chiave senza revisione', am.chiave('25NDSSA1021-00') == am.chiave('25NDSSA1021'))
    r = _Res([_Art('25NDSSA1021'), _Art('25NDSSA1076', dxf='25NDSSA1076-00.DXF'),
              _Art('X-1', dxf='X-1.dxf'), _Art('GUIDA', desc='guida in plastica')])
    check('assieme senza disegno e senza componenti: bloccato', [x['codice'] for x in am.dal_pacchetto(r)] == ['25NDSSA1021'])
    r = _Res([_Art('25NDSSA1021'), _Art('C-1', dxf='C-1.dxf', ass='25NDSSA1021-00', riga=False)])
    check('assieme coi componenti: niente blocco', am.dal_pacchetto(r) == [])
    r = _Res([_Art('25NDSSA0622-00', dxf='25NDSSA0622-00.DXF', ass='25NDSSA0622-00', master=True)])
    check('cartella col solo disegno d\'insieme: bloccato (dopo la distinta)', len(am.dal_pacchetto(r, componenti=set())) == 1)
    check('... ma non prima della distinta', am.dal_pacchetto(r) == [])
    check('... ne\' se la distinta gli ha trovato i pezzi', am.dal_pacchetto(r, componenti={'25NDSSA0622-00'}) == [])
    righe = [{'codice': 'TSSA1021-00', 'tipo': 'lamiera'}, {'codice': 'P1', 'tipo': 'lamiera', 'disegno': 'P1.dxf'},
             {'codice': 'SA2', 'tipo': 'assieme'}, {'codice': 'TSSA0999', 'tipo': 'assieme'},
             {'codice': 'C1', 'tipo': 'lamiera', 'disegno': 'C1.dxf', 'assieme': 'TSSA0999'}]
    check('distinta: assiemi senza pezzi', am.assiemi_senza_componenti(righe) == {'TSSA1021', 'SA2'},
          am.assiemi_senza_componenti(righe))

    # =====================================================================
    print('\n2) Caso vero LS 1252 (DECA A 001252)')
    if os.path.isdir(LS1252):
        attesi = ['25NDSSA0622-00', '25NDSSA0625-00', '25NDSSA1021', '25NDSSA1022',
                  '25NDSSA1024', '25NDSSA1028', '25NDSSA1055']
        res = rfq_importer.process_rfq_package(zip_cartella(LS1252, True), tollerante=True)
        cod = [x['codice'] for x in res.assiemi_senza_componenti]
        check('solo i file della radice: bloccati i 7 assiemi', cod == attesi, cod)
        check('primo avviso della lettura = il blocco', res.warnings and res.warnings[0].startswith('Mancano i disegni di 7 assiemi'),
              res.warnings[:1])
        res = rfq_importer.process_rfq_package(zip_cartella(LS1252, False), tollerante=True)
        check('cartella intera: nessun blocco', res.assiemi_senza_componenti == [], res.assiemi_senza_componenti)
    else:
        print('  (cartella LS 1252 non trovata: salto)')

    # =====================================================================
    print('\n3) Ufficio: analisi e conferma')
    pdf = pdf_con_assieme()
    files = [(io.BytesIO(pdf), 'OAFA1252.pdf'), (io.BytesIO(T.DXF['PZ-A100-00.dxf']), 'PZ-A100-00.dxf'),
             (io.BytesIO(T.DXF['PZ-B200-00.dxf']), 'PZ-B200-00.dxf')]
    r = T.analizza(uff, files=files, paths=[n for _, n in files])
    d = r.get_json() or {}
    check('analisi riuscita', r.status_code == 200 and d.get('success'), d.get('error'))
    check('blocco con il codice dell\'assieme', (d.get('blocco') or {}).get('codice') == 'assiemi_senza_componenti'
          and [x['codice'] for x in d.get('assiemi_senza_componenti') or []] == ['TSSA1021-00']
          and 'TSSA1021-00' in d['blocco']['messaggio'] and 'cartella intera' in d['blocco']['messaggio'], d.get('blocco'))
    pid = d.get('pacchetto_id')
    r = T.conferma(uff, pid, numero_ordine='1252', cliente='DECA')
    dd = r.get_json() or {}
    check('conferma senza CREA: 409', r.status_code == 409 and dd.get('codice') == 'assiemi_senza_componenti', dd)
    r = T.conferma(uff, pid, numero_ordine='1252', cliente='DECA', crea_lo_stesso='sì')
    check('una parola qualsiasi non basta', r.status_code == 409, r.get_json())
    r = T.conferma(uff, pid, numero_ordine='1252', cliente='DECA', crea_lo_stesso='crea', al_laser=True)
    dd = r.get_json() or {}
    check('con CREA: ordine creato', r.status_code == 201 and dd.get('order_id'), dd)
    oid = dd.get('order_id')
    from backend.database import get_session
    from backend.models import AuditLog
    sess = get_session()
    try:
        au = sess.query(AuditLog).filter(AuditLog.action == 'ORDER_PACKAGE_ASSIEMI_MANCANTI').all()
        check('registrato nel registro', len(au) == 1 and 'TSSA1021-00' in (au[0].detail or ''), [a.detail for a in au])
    finally:
        sess.close()

    # cartella intera: l'assieme ha la sua sottocartella coi pezzi
    files = [(io.BytesIO(pdf), 'OAFA1252.pdf'), (io.BytesIO(T.DXF['PZ-A100-00.dxf']), 'PZ-A100-00.dxf'),
             (io.BytesIO(T.DXF['PZ-B200-00.dxf']), 'PZ-B200-00.dxf'),
             (io.BytesIO(T.dxf_rettangolo(300, 120)), 'TSPA2001-00.dxf')]
    paths = ['LS 1252/OAFA1252.pdf', 'LS 1252/PZ-A100-00.dxf', 'LS 1252/PZ-B200-00.dxf',
             'LS 1252/TSSA1021-00/TSPA2001-00.dxf']
    r = T.analizza(uff, files=files, paths=paths)
    d = r.get_json() or {}
    check('cartella intera: nessun blocco', r.status_code == 200 and not d.get('blocco')
          and d.get('assiemi_senza_componenti') == [], (d.get('blocco'), d.get('assiemi_senza_componenti')))
    uff.delete(f"/api/orders/da-pacchetto/{d.get('pacchetto_id')}", json={})

    # =====================================================================
    print('\n4) Laser: righe rosse, taglio fatto e Manda a Lantek')
    from backend import api_laser as AL
    sess = models.SessionLocal()
    try:
        o = sess.query(Order).filter(Order.id == oid).first()
        a = AL.analisi(o, completa=True, con_lantek=False)
    finally:
        sess.close()
    mm = a.get('mancano_disegni') or []
    check('l\'assieme senza componenti e\' fra le righe bloccate', [x['codice'] for x in mm] == ['TSSA1021-00']
          and mm[0]['tipo'] == 'assieme', mm)
    pz = {p['codice']: p for p in a.get('pezzi') or []}
    check('la riga e\' rossa (manca_disegno), non "assieme che non va al laser"',
          (pz.get('TSSA1021-00') or {}).get('manca_disegno') and not pz['TSSA1021-00'].get('assieme_senza_disegno'),
          pz.get('TSSA1021-00'))
    AL.invalida(oid)
    r = laser.post(f'/api/orders/{oid}/mark-laser-done', json={})
    dd = r.get_json() or {}
    check('taglio fatto senza conferma: 409 mancano_disegni', r.status_code == 409
          and dd.get('codice') == 'mancano_disegni' and dd.get('codici') == ['TSSA1021-00'], dd)
    with A.app.test_request_context(json={}):
        sess = models.SessionLocal()
        try:
            o = sess.query(Order).filter(Order.id == oid).first()
            codici, err = A._mancano_disegni_confermati(o, {}, 'mandare a Lantek il resto')
            check('Manda a Lantek senza conferma: 409', err is not None and err[1] == 409, codici)
            codici, err = A._mancano_disegni_confermati(o, {'conferma_mancanti': ['TSSA1021-00']}, 'x')
            check('Manda a Lantek con la conferma: passa', err is None and codici == ['TSSA1021-00'])
        finally:
            sess.close()
    r = laser.post(f'/api/orders/{oid}/lantek-invia', json={'codici': [], 'nuovi': []})
    dd = r.get_json() or {}
    check('lantek-invia rifiutato senza conferma', r.status_code == 409 and dd.get('codice') == 'mancano_disegni', dd)
    r = laser.post(f'/api/orders/{oid}/mark-laser-done', json={'conferma_mancanti': ['TSSA1021-00']})
    check('taglio fatto con la conferma: fatto', r.status_code == 200 and (r.get_json() or {}).get('success'), r.get_json())
    sess = get_session()
    try:
        au = sess.query(AuditLog).filter(AuditLog.action == 'TAGLIO_FATTO_SENZA_DISEGNI').all()
        check('conferma del taglio registrata', len(au) == 1, len(au))
    finally:
        sess.close()

    print('\n' + '=' * 60)
    print(f'PASSATI: {T.OK}   FALLITI: {len(T.KO)}')
    if T.KO:
        print('Falliti: ' + ', '.join(T.KO))
    print('=' * 60)
    return 1 if T.KO else 0


if __name__ == '__main__':
    code = 1
    try:
        code = main()
    finally:
        A.ConfigManager.load_config = T._config_vera
        try:
            T._ENG.dispose()
            os.remove(T._TMP)
        except Exception:
            pass
        shutil.rmtree(T._CARTELLE, ignore_errors=True)
    sys.exit(code)
