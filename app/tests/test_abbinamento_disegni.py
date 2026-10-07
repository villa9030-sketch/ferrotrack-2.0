"""Abbinamento disegno <-> codice nel pacchetto dell'ordine (rfq_importer).

Caso vero (07/10/2026, ordine DECA A 001252): ogni riga del PDF prendeva il
file "piu' simile" ancora libero; 25CCPA0595-G-00 non aveva disegno, rubava
quello di 0599 e da li' ogni codice prendeva il disegno del successivo
(19 su 27 sbagliati, nessun avviso). Si prova che:
 1. nome uguale al codice vince sempre (niente catene)
 2. codice senza revisione <-> file con revisione, solo se il candidato e' uno
 3. revisione diversa o nome solo simile: abbinato "per somiglianza", da confermare
 4. la copia " (2)" di Windows conta come il file vero
 5. un codice ripetuto nel PDF diventa un pezzo solo con le quantita' sommate

Esecuzione: python app/tests/test_abbinamento_disegni.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['FERROTRACK_SKIP_DB_INIT'] = '1'
from backend.preventivi.rfq_importer import match_dxf_to_articoli  # noqa: E402

OK, KO = 0, []


def check(nome, cond, extra=''):
    global OK
    if cond:
        OK += 1
        print(f'  [OK] {nome}')
    else:
        KO.append(nome)
        print(f'  [X]  {nome} {extra}')


def abbina(codici, files):
    m, no = match_dxf_to_articoli([{'codice': c, 'quantita': q} for c, q in codici], files)
    return {a.codice: a for a in m}, no


print('1) Niente catene')
m, no = abbina([('25CCPA0595-G-00', 2), ('25CCPA0599-00', 1), ('25CCPA0600-00', 1), ('25CCPA0602-00', 1),
                ('25CCPA0604-00', 1)],
               ['25CCPA0599-00.dxf', '25CCPA0600-00.dxf', '25CCPA0602-00.dxf', '25CCPA0604-00.dxf'])
check('ogni codice il suo file', all(m[c].matched_dxf == c + '.dxf' and m[c].abbinamento == 'esatto'
                                     for c in ('25CCPA0599-00', '25CCPA0600-00', '25CCPA0602-00', '25CCPA0604-00')),
      {c: m[c].matched_dxf for c in m})
check('il codice senza disegno resta senza (non ruba)', m['25CCPA0595-G-00'].matched_dxf is None)

print('\n2) Revisioni')
m, _ = abbina([('25NDSPA1979', 2), ('25NDSPA2086', 1)], ['25NDSPA1979-00.dxf', '25NDSPA2086-00.DXF'])
check('codice senza revisione -> file -00 (unico)', m['25NDSPA1979'].matched_dxf == '25NDSPA1979-00.dxf'
      and m['25NDSPA1979'].abbinamento == 'senza_revisione')
m, _ = abbina([('C1', 1)], ['C1-00.dxf', 'C1-01.dxf'])
check('due revisioni: non sceglie in silenzio', m['C1'].abbinamento in (None, 'somiglianza'),
      (m['C1'].matched_dxf, m['C1'].abbinamento))
m, _ = abbina([('25NDSSA0615-00', 2)], ['25NDSSA0615-01.DXF'])
check('revisione diversa: solo per somiglianza (da confermare)', m['25NDSSA0615-00'].abbinamento == 'somiglianza')

print('\n3) Copie e ripetuti')
m, no = abbina([('E1-00', 1)], ['E1-00 (2).dxf'])
check('la copia " (2)" vale come il file', m['E1-00'].abbinamento == 'esatto' and not no)
m, _ = abbina([('F1', 1), ('G1-00', 3), ('F1', 2)], ['F1-00.dxf', 'G1-00.dxf'])
check('codice ripetuto: un pezzo, quantita 1+2=3, due righe d\'ordine',
      len(m) == 2 and m['F1'].quantita == 3 and len(m['F1'].righe_ordine) == 2, {c: m[c].quantita for c in m})
m, no = abbina([('H1-00', 1)], ['H1-00.dxf', 'ALTRO.dxf'])
check('file in piu\' senza riga restano fuori', no == ['ALTRO.dxf'])

print(f'\nPASSATI: {OK}   FALLITI: {len(KO)}')
sys.exit(1 if KO else 0)
