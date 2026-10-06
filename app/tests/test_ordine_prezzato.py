"""Ordini GIA' PREZZATI dei clienti a gestionale (Poliform, B&B Italia).

L'ufficio carica solo il PDF (i pezzi sono gia' in Lantek): dal PDF si
propongono numero, cliente, consegna e valore dell'ordine, cosi' l'ordine resta
prezzato dentro l'app.

Copre:
 1. lettura dei due formati (righe, numero, consegna, totale)
 2. il valore si propone SOLO se la somma delle righe torna col totale
 3. formato sconosciuto: nessuna proposta (non si indovina)
 4. caricamento: /api/extract-pdf-data propone i dati, /api/orders salva il
    valore; il cliente si scrive come negli ordini esistenti
 5. il valore lo vedono solo gli uffici (non il laser)

I PDF sono COSTRUITI con valori inventati, nello stesso formato dei veri:
i documenti dei clienti non vanno nel repository.
Gira su DATABASE TEMPORANEO. Esecuzione: python app/tests/test_ordine_prezzato.py
"""
import io
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta

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
from backend.models import Base, Order  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_prezzato_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'), connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

import importlib  # noqa: E402
A = importlib.import_module('backend.app')
from backend.preventivi.ordine_prezzi import leggi_ordine_prezzato  # noqa: E402

_CARTELLE = tempfile.mkdtemp(prefix='ft_prezzato_')
A.PDFS_FOLDER = os.path.join(_CARTELLE, 'pdfs')
os.makedirs(A.PDFS_FOLDER, exist_ok=True)

OK = 0
KO = []


def check(nome, cond, extra=''):
    global OK
    if cond:
        OK += 1
        print(f'  [OK] {nome}')
    else:
        KO.append(nome)
        print(f'  [KO] {nome}  {extra}')


CONS = (datetime.now() + timedelta(days=30))
C8, C10 = CONS.strftime('%d/%m/%y'), CONS.strftime('%d/%m/%Y')


def pdf(righe):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = 810
    for r in righe:
        c.drawString(30, y, r)
        y -= 16
    c.showPage(); c.save()
    return buf.getvalue()


def poliform(totale='1.234,50'):
    return pdf([
        'Ordine Fornitore', 'Order to Supplier', 'Spett.le', 'L.S. SRL',
        'N. 300 099 001 Indirizzo di Consegna', 'Data / Date 01/09/2026',
        'Ns. Rif. / Our Ref. Del / Of Consegna richiesta / Delivery date Fornitore / Supplier',
        f'01/09/2026 {C10} Week 45 / 2026 F00000000',
        'Pos Codice Articolo Quantita Descrizione Prezzo Unitario 1 sc. 2 sc Importo D. cons.',
        f'3 05TEST0100---- NR 100 SQUADRETTA DI PROVA 2,5000 250,00 {C8}',
        'IN FERRO VERNICIATO',
        f'6 05TEST0200---- NR 50 TELAIO DI PROVA 19,6900 984,50 {C8}',
        'Importo Netto / Net amount Imponibile / Taxable amount % IVA / VAT Importo IVA / VAT amount Importo Totale',
        f'{totale} {totale} 22,00 271,59 1.506,09',
    ])


def bebitalia():
    return pdf([
        'B&B ITALIA S.p.A.', 'Divisione CASA Data 05/10/2026',
        'Richiedente Documento Ordine Acquisto', 'Nr. 20269999999-01',
        'Pos Prodotto Descrizione U.M. Q.ta Prezzo Netto Unitario Prevista',
        f'1 M9000001 TF PEZZO DI PROVA UNO NR 5,00 10,0000 {C10}',
        f'2 M9000002 TF PEZZO DI PROVA DUE NR 10,00 7,5500 {C10}',
        'Totale EUR 125,50',
    ])


def main():
    print('1) Lettura dei due formati')
    d = leggi_ordine_prezzato(poliform())
    check('Poliform riconosciuto', d and d['cliente'] == 'Poliform', d and d.get('formato'))
    check('Poliform: numero, consegna, 2 righe',
          d and d['numero_ordine'] == '300099001' and d['data_consegna'] == CONS.date().isoformat()
          and len(d['righe']) == 2 and d['righe'][0]['codice'] == '05TEST0100', d)
    check('Poliform: valore 1234,50 (la somma torna)', d and d['quadra'] and d['valore_ordine'] == 1234.5, d)
    b = leggi_ordine_prezzato(bebitalia())
    check('B&B Italia: 2 righe, importo = q.ta x prezzo, totale 125,50',
          b and len(b['righe']) == 2 and b['righe'][1]['importo'] == 75.5 and b['valore_ordine'] == 125.5, b)
    check('B&B Italia: numero e consegna', b and b['numero_ordine'] == '20269999999-01'
          and b['data_consegna'] == CONS.date().isoformat(), b)

    print('\n2) Se la somma non torna, niente valore')
    d = leggi_ordine_prezzato(poliform(totale='1.300,00'))
    check('righe lette ma valore NON proposto', d and not d['quadra'] and d['valore_ordine'] is None, d)

    print('\n3) Formato sconosciuto')
    check('nessuna proposta', leggi_ordine_prezzato(pdf(['Ordine qualsiasi', 'riga 1 10 pezzi'])) is None)

    print('\n4) Caricamento dall\'ufficio')
    from tests.accesso_aiuto import postazioni, persona, entra, modalita
    modalita('protetto')
    postazioni()
    persona('ufficio', 'Amministrazione', 'Amministrazione', pin='2580')
    uff = entra(A.app, 'ufficio', pin='2580')
    laser = entra(A.app, 'laser')
    # un ordine Poliform esistente, scritto in un altro modo
    s = models.SessionLocal()
    s.add(Order(id=str(uuid.uuid4()), cliente='POLIFORM S.p.A.', numero_ordine='VECCHIO',
                data_consegna=CONS, status='RICEVUTO', origine='PDF'))
    s.commit(); s.close()
    r = uff.post('/api/extract-pdf-data', data={'file': (io.BytesIO(poliform()), '300099001.pdf')},
                 content_type='multipart/form-data')
    dd = (r.get_json() or {}).get('data') or {}
    check('proposti numero, consegna e valore', dd.get('numero_ordine') == '300099001'
          and dd.get('data_consegna') == CONS.date().isoformat() and dd.get('valore_ordine') == 1234.5, dd)
    check('cliente scritto come negli ordini esistenti', dd.get('cliente') == 'POLIFORM S.p.A.', dd.get('cliente'))
    check('spiegazione della lettura', (dd.get('lettura_prezzi') or {}).get('quadra') is True, dd.get('lettura_prezzi'))
    r = uff.post('/api/orders', json={'cliente': dd['cliente'], 'numero_ordine': '300099001',
                                      'data_consegna': CONS.date().isoformat(), 'note': '',
                                      'pdf_filename': dd['pdf_filename'], 'valore_ordine': 1234.5})
    oid = (r.get_json() or {}).get('order_id')
    s = models.SessionLocal()
    o = s.query(Order).filter(Order.numero_ordine == '300099001').first()
    s.close()
    check('ordine creato col valore', o is not None and o.prezzo_quotato == 1234.5, o and o.prezzo_quotato)
    r = uff.post('/api/orders', json={'cliente': 'X', 'numero_ordine': 'N2', 'data_consegna': CONS.date().isoformat(),
                                      'pdf_filename': dd['pdf_filename'], 'valore_ordine': 'abc'})
    check('valore non numerico rifiutato', r.status_code == 400)
    r = uff.post('/api/orders', json={'cliente': 'X', 'numero_ordine': 'N3', 'data_consegna': CONS.date().isoformat(),
                                      'pdf_filename': dd['pdf_filename']})
    s = models.SessionLocal()
    o3 = s.query(Order).filter(Order.numero_ordine == 'N3').first()
    s.close()
    check('senza valore: resta vuoto (non zero)', o3 is not None and o3.prezzo_quotato is None, o3 and o3.prezzo_quotato)

    print('\n5) Il valore lo vedono solo gli uffici')
    if oid:
        ru = uff.get(f'/api/orders/{oid}').get_json() or {}
        rl = laser.get(f'/api/orders/{oid}').get_json() or {}
        testo_u, testo_l = str(ru), str(rl)
        check('l\'ufficio vede 1234.5', '1234.5' in testo_u, testo_u[:200])
        check('il laser no', '1234.5' not in testo_l, testo_l[:200])

    print(f'\nPASSATI: {OK}   FALLITI: {len(KO)}')
    return 0 if not KO else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    finally:
        try:
            _ENG.dispose(); os.remove(_TMP)
        except Exception:
            pass
