"""Riconoscimento contorno: segni disegnati sul pezzo scambiati per cartiglio.

Caso reale 13PA00680-00 (DECA): nello sviluppo ci sono rettangolini 100x15
attorno alle coppie di fori svasati, coi fori a 0,1 mm dal bordo. Venivano
presi per celle del cartiglio, e lo sviluppo intero (che li contiene) era
scartato: si prezzava la finestra interna 441x110 e la lista dei contorni da
scegliere a mano non conteneva il pezzo.

Il disegno qui e' COSTRUITO (stessa geometria essenziale): i DXF dei clienti
non vanno nel repository.

Controlla anche che la regola non tocchi i fogli veri: la cornice interna di
un foglio con le viste dentro resta scartata.

Esecuzione: python app/tests/test_contorno_segni.py
"""
import os
import sys
import tempfile

_QUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_QUI))

import ezdxf  # noqa: E402

from backend.preventivi import dxf_polygon_detector_v3 as d  # noqa: E402

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


def rett(msp, x0, y0, x1, y1):
    msp.add_lwpolyline([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], close=True)


def foglio(msp):
    """Cornice doppia A0 e cartiglio con celle attaccate alla cornice interna."""
    rett(msp, 25, 25, 2075, 1460)
    rett(msp, 50, 50, 2050, 1435)
    for i in range(4):                         # celle del cartiglio
        rett(msp, 1114 + i * 234, 50, 1114 + (i + 1) * 234, 271)


def disegno_13pa(path):
    doc = ezdxf.new(); doc.header['$INSUNITS'] = 4
    msp = doc.modelspace()
    foglio(msp)
    # sviluppo 248,4 x 1181,2 con finestra a freccia
    x0, y0 = 132.14, 225.0
    rett(msp, x0, y0, x0 + 248.44, y0 + 1181.22)
    msp.add_lwpolyline([(208.8, 700), (258.4, 511), (269.2, 511), (318.8, 700),
                        (318.8, 952), (208.8, 952)], close=True)
    # 4 segni 100x15 con 2 fori svasati ciascuno, fori a 0,15 mm dal bordo
    for yc in (258.19, 368.19, 1213.19, 1338.19):
        rett(msp, 206.35, yc - 7.5, 306.35, yc + 7.5)
        for xc in (211.35, 301.35):
            msp.add_circle((xc, yc), 4.855)
            msp.add_circle((xc, yc), 6.855)
    # vista frontale: la stessa finestra, ruotata (seconda copia identica)
    msp.add_lwpolyline([(1154, 844), (969, 794), (969, 784), (1154, 734),
                        (1410, 734), (1410, 844)], close=True)
    doc.saveas(path)


def foglio_con_viste(path):
    """Foglio vero: cornice interna con una vista (pezzo con fori) dentro e un
    pezzo staccato: la cornice non deve diventare il pezzo."""
    doc = ezdxf.new(); doc.header['$INSUNITS'] = 4
    msp = doc.modelspace()
    foglio(msp)
    rett(msp, 300, 600, 700, 1300)             # riquadro di una vista
    rett(msp, 350, 650, 650, 1250)             # pezzo nella vista
    msp.add_circle((500, 900), 10)
    rett(msp, 1200, 600, 1500, 800)            # pezzo da tagliare
    msp.add_circle((1350, 700), 8)
    doc.saveas(path)


def main():
    tmp = tempfile.mkdtemp(prefix='test_segni_')
    p1 = os.path.join(tmp, '13pa.dxf')
    disegno_13pa(p1)
    print('1) Sviluppo con segni attorno ai fori (13PA00680)')
    r = d.detect_pezzo_geometry_v3(p1)
    check('preso lo sviluppo 248,4 x 1181,2',
          abs(r['bbox_width_mm'] - 248.44) < 0.5 and abs(r['bbox_height_mm'] - 1181.22) < 0.5,
          (r['bbox_width_mm'], r['bbox_height_mm']))
    check('9 tagli interni: finestra + 8 fori (i segni non si tagliano)', r['n_fori'] == 9, r['n_fori'])
    check('nessuna scelta a mano', not r['needs_manual_select'], r.get('confidence'))
    c = d.compute_geometry_from_point(p1, 133, 700)
    check('anche col clic sul bordo: stesso pezzo e 9 tagli',
          abs(c['bbox_height_mm'] - 1181.22) < 0.5 and c['n_fori'] == 9, (c['bbox_height_mm'], c['n_fori']))

    print('\n2) Foglio vero: la cornice non diventa il pezzo')
    p2 = os.path.join(tmp, 'foglio.dxf')
    foglio_con_viste(p2)
    r = d.detect_pezzo_geometry_v3(p2)
    check('la cornice interna non e\' il pezzo', r['bbox_width_mm'] < 1000, (r['bbox_width_mm'], r['bbox_height_mm']))

    print('\n3) Vista di DETTAGLIO: la sua scala non vale per il pezzo (33PP00086)')
    doc = ezdxf.new()
    msp = doc.modelspace()
    msp.add_circle((2170, 1780), 189)                       # cerchio del dettaglio
    msp.add_text('DETTAGLIO A', dxfattribs={'insert': (2164, 1501), 'height': 20})
    check('scritta "DETTAGLIO A" sotto la vista: riconosciuta',
          d._etichetta_dettaglio_vicina(doc, (1981, 1592, 2359, 1970)))
    check('lontano dalla scritta: non e\' un dettaglio',
          not d._etichetta_dettaglio_vicina(doc, (100, 100, 400, 300)))

    print(f'\nPASSATI: {OK}   FALLITI: {len(KO)}')
    return 0 if not KO else 1


if __name__ == '__main__':
    sys.exit(main())
