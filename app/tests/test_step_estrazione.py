"""Test dell'ESTRAZIONE DATI DA STEP 3D (import-step del preventivatore).

Senza kernel CAD: i file STEP (AP214, testo) sono generati qui sotto da un
piccolo writer B-rep (estrusione di profili 2D con linee, archi, cerchi) e poi
letti dal parser a regex di backend/preventivi/step_*.py.

Copre:
  S1  quantita' d'istanza (parte x4, sotto-assieme x2 con parte x3 = 6)
  S2  tubo con testa a 45 gradi NON contato anche come piastra
  S3/S4/S13  sezioni non a catalogo tenute col peso geometrico, 40x40x1.5,
      spezzone corto, nuove misure a catalogo
  S5  piastra forata: nessun tubo fantasma
  S6  disco forato / semiguscio / tondo pieno non sono tubi cavi
  S7/S8/S9/S10  sviluppo lamiera piegata, area vera (gusset, piastra ruotata,
      fori sottratti), spessore minimo (U), piastra 50mm
  S11 file in pollici
  S12 taglio obliquo su tubo tondo (30 gradi)
  S14 saldatura: solo anello esterno, trasformazioni composte
  S15 ';' dentro le stringhe, entita' complesse

Esecuzione: python app/tests/test_step_estrazione.py [--salva-fixture]
(--salva-fixture scrive i file generati in app/_test_input/step/)
"""
import io
import math
import os
import shutil
import sys
import tempfile
import uuid

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)

# importare backend.preventivi carica anche backend.app: DATABASE TEMPORANEO
os.environ['FERROTRACK_SKIP_DB_INIT'] = '1'

import werkzeug  # noqa: E402
if not hasattr(werkzeug, '__version__'):
    werkzeug.__version__ = '3.0.0'

from sqlalchemy import create_engine  # noqa: E402

from backend import models  # noqa: E402
from backend.models import Base, User  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_step_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'),
                     connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

from backend.app import app, UPLOAD_FOLDER  # noqa: E402
from backend.preventivi import step_parser, step_tubolari, step_piastre, step_assieme  # noqa: E402

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


def vicino(a, b, tol):
    return a is not None and abs(a - b) <= tol


# ===========================================================================
# Writer STEP minimale
# ===========================================================================

class ScrittoreStep:
    """Genera un file AP214 con solidi estrusi lungo Z e struttura prodotto."""

    def __init__(self, pollici=False):
        self.righe = []
        self.n = 0
        self.k = (1 / 25.4) if pollici else 1.0
        self._ctx = None
        self.pollici = pollici

    def e(self, testo):
        self.n += 1
        self.righe.append(f'#{self.n}={testo};')
        return self.n

    def f(self, x):
        return repr(float(x))

    def pt(self, p):
        return self.e("CARTESIAN_POINT('',(%s,%s,%s))" % tuple(self.f(c * self.k) for c in p))

    def dr(self, d):
        ln = math.sqrt(sum(c * c for c in d))
        return self.e("DIRECTION('',(%s,%s,%s))" % tuple(self.f(c / ln) for c in d))

    def a2p(self, o, z=(0, 0, 1), x=(1, 0, 0)):
        return self.e(f"AXIS2_PLACEMENT_3D('',#{self.pt(o)},#{self.dr(z)},#{self.dr(x)})")

    # --- contesto con unita' (condiviso da tutte le parti: caso reale) ---
    def contesto(self):
        if self._ctx:
            return self._ctx
        if self.pollici:
            mm = self.e("( LENGTH_UNIT() NAMED_UNIT(*) SI_UNIT(.MILLI.,.METRE.) )")
            lm = self.e(f"LENGTH_MEASURE_WITH_UNIT(LENGTH_MEASURE(25.4),#{mm})")
            de = self.e("DIMENSIONAL_EXPONENTS(1.,0.,0.,0.,0.,0.,0.)")
            u1 = self.e(f"( CONVERSION_BASED_UNIT('INCH',#{lm}) LENGTH_UNIT() NAMED_UNIT(#{de}) )")
        else:
            u1 = self.e("( LENGTH_UNIT() NAMED_UNIT(*) SI_UNIT(.MILLI.,.METRE.) )")
        u2 = self.e("( NAMED_UNIT(*) PLANE_ANGLE_UNIT() SI_UNIT($,.RADIAN.) )")
        u3 = self.e("( NAMED_UNIT(*) SI_UNIT($,.STERADIAN.) SOLID_ANGLE_UNIT() )")
        unc = self.e(f"UNCERTAINTY_MEASURE_WITH_UNIT(LENGTH_MEASURE(1.E-05),#{u1},'distance_accuracy_value','confusion accuracy')")
        self._ctx = self.e(
            f"( GEOMETRIC_REPRESENTATION_CONTEXT(3) GLOBAL_UNCERTAINTY_ASSIGNED_CONTEXT((#{unc})) "
            f"GLOBAL_UNIT_ASSIGNED_CONTEXT((#{u1},#{u2},#{u3})) "
            f"REPRESENTATION_CONTEXT('Context #1','3D Context with UNIT and UNCERTAINTY') )")
        return self._ctx

    # --- solido per estrusione lungo Z ---
    def estrudi(self, anelli, h, k=0.0, off=(0.0, 0.0, 0.0)):
        """anelli: [esterno antiorario, fori orari]; segmenti:
        ('L', p0, p1) | ('A', p0, p1, centro, antiorario) | ('C', centro, r, antiorario).
        Faccia superiore z = off_z + h + k*x (inclinata solo con L e C)."""
        ox, oy, oz = off
        vcache, ecache = {}, {}

        def ztop(x):
            return oz + h + k * x

        def vtx(p):
            key = tuple(round(c, 6) for c in p)
            if key not in vcache:
                vcache[key] = self.e(f"VERTEX_POINT('',#{self.pt(p)})")
            return vcache[key]

        def linea(p0, p1):
            key = ('L', vtx(p0), vtx(p1))
            if key in ecache:
                return ecache[key]
            d = tuple(b - a for a, b in zip(p0, p1))
            ln = math.sqrt(sum(c * c for c in d)) * self.k
            vec = self.e(f"VECTOR('',#{self.dr(d)},{self.f(ln)})")
            ln_id = self.e(f"LINE('',#{self.pt(p0)},#{vec})")
            ec = self.e(f"EDGE_CURVE('',#{vtx(p0)},#{vtx(p1)},#{ln_id},.T.)")
            ecache[key] = ec
            return ec

        def arco(p0, p1, c, r, ccw, z):
            key = ('A', vtx(p0), vtx(p1), round(z, 6))
            if key in ecache:
                return ecache[key]
            circ = self.e(f"CIRCLE('',#{self.a2p((c[0], c[1], z))},{self.f(r * self.k)})")
            ec = self.e(f"EDGE_CURVE('',#{vtx(p0)},#{vtx(p1)},#{circ},{'.T.' if ccw else '.F.'})")
            ecache[key] = ec
            return ec

        def cerchio_top(c, r, ccw):
            p = (c[0] + r, c[1], ztop(c[0] + r))
            key = ('E', vtx(p))
            if key in ecache:
                return ecache[key]
            if abs(k) < 1e-12:
                curva = self.e(f"CIRCLE('',#{self.a2p((c[0], c[1], ztop(c[0])))},{self.f(r * self.k)})")
            else:
                s = math.sqrt(1 + k * k)
                pl = self.a2p((c[0], c[1], ztop(c[0])), (-k, 0, 1), (1, 0, k))
                curva = self.e(f"ELLIPSE('',#{pl},{self.f(r * s * self.k)},{self.f(r * self.k)})")
            ec = self.e(f"EDGE_CURVE('',#{vtx(p)},#{vtx(p)},#{curva},{'.T.' if ccw else '.F.'})")
            ecache[key] = ec
            return ec

        def oe(ec, avanti):
            return self.e(f"ORIENTED_EDGE('',*,*,#{ec},{'.T.' if avanti else '.F.'})")

        def loop(oes):
            return self.e("EDGE_LOOP('',(%s))" % ','.join(f'#{x}' for x in oes))

        def faccia(bounds, superficie, senso):
            bb = []
            for i, (lp, esterno) in enumerate(bounds):
                t = 'FACE_OUTER_BOUND' if esterno else 'FACE_BOUND'
                bb.append(self.e(f"{t}('',#{lp},.T.)"))
            return self.e("ADVANCED_FACE('',(%s),#%d,%s)" % (
                ','.join(f'#{b}' for b in bb), superficie, '.T.' if senso else '.F.'))

        P = lambda q: (q[0] + ox, q[1] + oy)  # noqa: E731
        facce = []
        bottom_edges = []  # per anello: lista ec (verso del profilo)
        top_edges = []
        for anello in anelli:
            be, te = [], []
            for s in anello:
                if s[0] == 'L':
                    p0, p1 = P(s[1]), P(s[2])
                    b0, b1 = (p0[0], p0[1], oz), (p1[0], p1[1], oz)
                    t0, t1 = (p0[0], p0[1], ztop(p0[0])), (p1[0], p1[1], ztop(p1[0]))
                    eb, et = linea(b0, b1), linea(t0, t1)
                    v0, v1 = linea(b0, t0), linea(b1, t1)
                    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
                    pl = self.e(f"PLANE('',#{self.a2p(b0, (dy, -dx, 0), (dx, dy, 0))})")
                    lp = loop([oe(eb, True), oe(v1, True), oe(et, False), oe(v0, False)])
                    facce.append(faccia([(lp, True)], pl, True))
                elif s[0] == 'A':
                    assert abs(k) < 1e-12, 'archi solo con testa piana'
                    p0, p1, c, ccw = P(s[1]), P(s[2]), P(s[3]), s[4]
                    r = math.hypot(p0[0] - c[0], p0[1] - c[1])
                    b0, b1 = (p0[0], p0[1], oz), (p1[0], p1[1], oz)
                    t0, t1 = (p0[0], p0[1], oz + h), (p1[0], p1[1], oz + h)
                    eb, et = arco(b0, b1, c, r, ccw, oz), arco(t0, t1, c, r, ccw, oz + h)
                    v0, v1 = linea(b0, t0), linea(b1, t1)
                    cyl = self.e(f"CYLINDRICAL_SURFACE('',#{self.a2p((c[0], c[1], oz))},{self.f(r * self.k)})")
                    lp = loop([oe(eb, True), oe(v1, True), oe(et, False), oe(v0, False)])
                    facce.append(faccia([(lp, True)], cyl, ccw))
                else:  # 'C' cerchio completo
                    c, r, ccw = P(s[1]), s[2], s[3]
                    pb = (c[0] + r, c[1], oz)
                    circ = self.e(f"CIRCLE('',#{self.a2p((c[0], c[1], oz))},{self.f(r * self.k)})")
                    eb = self.e(f"EDGE_CURVE('',#{vtx(pb)},#{vtx(pb)},#{circ},{'.T.' if ccw else '.F.'})")
                    et = cerchio_top(c, r, ccw)
                    cyl = self.e(f"CYLINDRICAL_SURFACE('',#{self.a2p((c[0], c[1], oz))},{self.f(r * self.k)})")
                    l1 = loop([oe(eb, True)])
                    l2 = loop([oe(et, False)])
                    facce.append(faccia([(l1, True), (l2, False)], cyl, ccw))
                be.append(eb)
                te.append(et)
            bottom_edges.append(be)
            top_edges.append(te)
        # fondo (normale -Z: piano +Z con senso .F.), anelli percorsi al contrario
        pl_b = self.e(f"PLANE('',#{self.a2p((0, 0, oz))})")
        bounds = []
        for i, be in enumerate(bottom_edges):
            bounds.append((loop([oe(ec, False) for ec in reversed(be)]), i == 0))
        facce.append(faccia(bounds, pl_b, False))
        # testa
        pl_t = self.e(f"PLANE('',#{self.a2p((0, 0, oz + h), (-k, 0, 1), (1, 0, k))})")
        bounds = []
        for i, te in enumerate(top_edges):
            bounds.append((loop([oe(ec, True) for ec in te]), i == 0))
        facce.append(faccia(bounds, pl_t, True))
        shell = self.e("CLOSED_SHELL('',(%s))" % ','.join(f'#{x}' for x in facce))
        return self.e(f"MANIFOLD_SOLID_BREP('Corpo; \"solido\"',#{shell})")

    # --- struttura prodotto ---
    def _pd(self, nome):
        if not hasattr(self, '_ac'):
            self._ac = self.e("APPLICATION_CONTEXT('core data for automotive mechanical design processes')")
            self._pc = self.e(f"PRODUCT_CONTEXT('',#{self._ac},'mechanical')")
            self._pdc = self.e(f"PRODUCT_DEFINITION_CONTEXT('part definition',#{self._ac},'design')")
        prod = self.e(f"PRODUCT('{nome}','{nome}','',(#{self._pc}))")
        pdf = self.e(f"PRODUCT_DEFINITION_FORMATION('','',#{prod})")
        pd = self.e(f"PRODUCT_DEFINITION('design','',#{pdf},#{self._pdc})")
        pds = self.e(f"PRODUCT_DEFINITION_SHAPE('','',#{pd})")
        return pd, pds

    def parte(self, nome, solidi):
        ctx = self.contesto()
        pd, pds = self._pd(nome)
        orig = self.a2p((0, 0, 0))
        sr = self.e(f"SHAPE_REPRESENTATION('{nome}',(#{orig}),#{ctx})")
        self.e(f"SHAPE_DEFINITION_REPRESENTATION(#{pds},#{sr})")
        absr = self.e("ADVANCED_BREP_SHAPE_REPRESENTATION('',(%s,#%d),#%d)" % (
            ','.join(f'#{s}' for s in solidi), self.a2p((0, 0, 0)), ctx))
        self.e(f"SHAPE_REPRESENTATION_RELATIONSHIP('','',#{sr},#{absr})")
        return {'pd': pd, 'sr': sr, 'orig': orig}

    def assieme(self, nome, figli):
        """figli: [(nodo, [(dx,dy,dz), ...]), ...]"""
        ctx = self.contesto()
        pd, pds = self._pd(nome)
        orig = self.a2p((0, 0, 0))
        target = []
        for nodo, posizioni in figli:
            for pos in posizioni:
                target.append((nodo, self.a2p(pos)))
        sr = self.e("SHAPE_REPRESENTATION('%s',(#%d,%s),#%d)" % (
            nome, orig, ','.join(f'#{t}' for _, t in target), ctx))
        self.e(f"SHAPE_DEFINITION_REPRESENTATION(#{pds},#{sr})")
        for i, (nodo, t) in enumerate(target):
            nauo = self.e(f"NEXT_ASSEMBLY_USAGE_OCCURRENCE('{i}','inst {i}','',#{pd},#{nodo['pd']},$)")
            pds_n = self.e(f"PRODUCT_DEFINITION_SHAPE('','',#{nauo})")
            idt = self.e(f"ITEM_DEFINED_TRANSFORMATION('','',#{nodo['orig']},#{t})")
            rr = self.e(f"( REPRESENTATION_RELATIONSHIP('','',#{nodo['sr']},#{sr}) "
                        f"REPRESENTATION_RELATIONSHIP_WITH_TRANSFORMATION(#{idt}) "
                        f"SHAPE_REPRESENTATION_RELATIONSHIP() )")
            self.e(f"CONTEXT_DEPENDENT_SHAPE_REPRESENTATION(#{rr},#{pds_n})")
        return {'pd': pd, 'sr': sr, 'orig': orig}

    def testo(self):
        return ('ISO-10303-21;\nHEADER;\nFILE_DESCRIPTION((\'test; generato\'),\'2;1\');\n'
                'FILE_NAME(\'test.stp\',\'2026-01-01T00:00:00\',(\'\'),(\'\'),\'gen\',\'gen\',\'\');\n'
                'FILE_SCHEMA((\'AUTOMOTIVE_DESIGN { 1 0 10303 214 1 1 1 1 }\'));\nENDSEC;\nDATA;\n'
                + '\n'.join(self.righe) + '\nENDSEC;\nEND-ISO-10303-21;\n')


# --- profili 2D ---
def rett(x0, y0, x1, y1, ccw=True):
    p = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    if not ccw:
        p.reverse()
    return [('L', p[i], p[(i + 1) % 4]) for i in range(4)]


def poligono(punti):
    return [('L', punti[i], punti[(i + 1) % len(punti)]) for i in range(len(punti))]


def foro(cx, cy, r):
    return [('C', (cx, cy), r, False)]


def cerchio(cx, cy, r):
    return [('C', (cx, cy), r, True)]


PROFILI_DB = step_tubolari.carica_profili_tubolari(os.path.join(_APP, 'backend', 'preventivi'))
DIR = tempfile.mkdtemp(prefix='test_step_')


def scrivi(nome, costruisci, pollici=False):
    w = ScrittoreStep(pollici=pollici)
    costruisci(w)
    path = os.path.join(DIR, nome)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(w.testo())
    return path


def singolo(anelli, h, k=0.0, nome="Pezzo; rev.A l''angolo"):
    def _c(w):
        w.parte(nome, [w.estrudi(anelli, h, k)])
    return _c


def analizza(path):
    tub = step_tubolari.analizza_step_tubolari(path, PROFILI_DB)
    pia = step_piastre.analizza_step_piastre(path, escludi_body_ids=tub.get('body_ids_tubi'))
    return tub, pia


# ===========================================================================
def main():
    print(f'File generati in {DIR}')

    # -----------------------------------------------------------------
    print('\n1) Piastra 100x50x3 con foro D10 (area vera, fori sottratti, S5)')
    p = scrivi('piastra_foro.stp', singolo([rett(0, 0, 100, 50), foro(30, 25, 5)], 3))
    tub, pia = analizza(p)
    pp = pia['piastre']
    check('una piastra', len(pp) == 1, pp)
    if pp:
        check('spessore 3.0', pp[0]['spessore_mm'] == 3.0, pp[0]['spessore_mm'])
        check('area 0.492 dm2 (5000 - foro 78.5 mm2)', vicino(pp[0]['area_dm2'], 0.4921, 0.002), pp[0]['area_dm2'])
        check('peso 0.12 kg', vicino(pp[0]['peso_kg'], 0.116, 0.01), pp[0]['peso_kg'])
        check('nessuna piega', pp[0]['n_pieghe'] == 0 and pp[0]['sviluppo'] is False)
    check('nessun tubo fantasma', tub['tubi'] == [], tub['tubi'])

    print('\n   piastra con 4 fori D12 (prima: "Tondo" fantasma di 500mm)')
    p = scrivi('piastra_4fori.stp', singolo(
        [rett(0, 0, 200, 100), foro(20, 20, 6), foro(180, 20, 6), foro(20, 80, 6), foro(180, 80, 6)], 5))
    tub, pia = analizza(p)
    check('nessun tubo', tub['tubi'] == [], tub['tubi'])
    check('una piastra 5mm', len(pia['piastre']) == 1 and pia['piastre'][0]['spessore_mm'] == 5.0)

    # -----------------------------------------------------------------
    print('\n2) Gusset triangolare e piastra ruotata di 45 gradi (S8)')
    p = scrivi('gusset.stp', singolo([poligono([(0, 0), (100, 0), (0, 100)])], 8))
    _, pia = analizza(p)
    a = pia['piastre'][0]['area_dm2'] if pia['piastre'] else None
    check('gusset 100x100/2 = 0.50 dm2 (non 1.00)', vicino(a, 0.5, 0.001), a)
    c45, s45 = math.cos(math.pi / 4), math.sin(math.pi / 4)
    rot = [(x * c45 - y * s45, x * s45 + y * c45) for x, y in [(0, 0), (100, 0), (100, 50), (0, 50)]]
    p = scrivi('piastra_45.stp', singolo([poligono(rot)], 4))
    _, pia = analizza(p)
    a = pia['piastre'][0]['area_dm2'] if pia['piastre'] else None
    check('piastra 100x50 ruotata 45 = 0.50 dm2', vicino(a, 0.5, 0.001), a)
    if pia['piastre']:
        check('dimensioni 100 x 50 nel piano della faccia',
              pia['piastre'][0]['larghezza_mm'] == 100.0 and pia['piastre'][0]['altezza_mm'] == 50.0,
              (pia['piastre'][0]['larghezza_mm'], pia['piastre'][0]['altezza_mm']))

    # -----------------------------------------------------------------
    print('\n3) Staffa a L piegata t=3 r=3 (sviluppo, S7)')
    L_prof = [('L', (6, 0), (50, 0)), ('L', (50, 0), (50, 3)), ('L', (50, 3), (6, 3)),
              ('A', (6, 3), (3, 6), (6, 6), False), ('L', (3, 6), (3, 40)), ('L', (3, 40), (0, 40)),
              ('L', (0, 40), (0, 6)), ('A', (0, 6), (6, 0), (6, 6), True)]
    p = scrivi('staffa_L.stp', singolo([L_prof], 100))
    tub, pia = analizza(p)
    pp = pia['piastre']
    check('una lamiera', len(pp) == 1, pp)
    svil = (44 + 34 + 4.5 * math.pi / 2) * 100 / 1e4
    if pp:
        check('spessore 3.0', pp[0]['spessore_mm'] == 3.0, pp[0]['spessore_mm'])
        check(f'area sviluppata {svil:.3f} dm2 (prima meta\')', vicino(pp[0]['area_dm2'], svil, 0.003),
              pp[0]['area_dm2'])
        check('1 piega, sviluppo=True', pp[0]['n_pieghe'] == 1 and pp[0]['sviluppo'] is True,
              (pp[0]['n_pieghe'], pp[0]['sviluppo']))
        check('peso 0.20 kg', vicino(pp[0]['peso_kg'], 0.20, 0.01), pp[0]['peso_kg'])
    check('non e\' un tubo', tub['tubi'] == [], tub['tubi'])

    print('\n   profilo a U t=3 con luce interna 24mm (spessore minimo, S9)')
    U = poligono([(0, 0), (30, 0), (30, 40), (27, 40), (27, 3), (3, 3), (3, 40), (0, 40)])
    p = scrivi('profilo_U.stp', singolo([U], 200))
    _, pia = analizza(p)
    pp = pia['piastre']
    check('spessore 3 (non 24/30)', bool(pp) and pp[0]['spessore_mm'] == 3.0, pp and pp[0]['spessore_mm'])
    check('area 2.08 dm2', bool(pp) and vicino(pp[0]['area_dm2'], 2.08, 0.005), pp and pp[0]['area_dm2'])

    print('\n   piastra 200x200x50 (prima scartata sopra 30mm, S10)')
    p = scrivi('piastra_50.stp', singolo([rett(0, 0, 200, 200)], 50))
    _, pia = analizza(p)
    pp = pia['piastre']
    check('spessore 50 accettato', bool(pp) and pp[0]['spessore_mm'] == 50.0, pp)
    check('peso 15.7 kg', bool(pp) and vicino(pp[0]['peso_kg'], 15.7, 0.05), pp and pp[0]['peso_kg'])

    print('\n   blocco pieno 100x100x80: non e\' lamiera')
    p = scrivi('blocco.stp', singolo([rett(0, 0, 100, 100)], 80))
    _, pia = analizza(p)
    check('nessuna piastra, con avviso', pia['piastre'] == [] and any('non quotato' in a for a in pia['avvisi']),
          pia)

    # -----------------------------------------------------------------
    print('\n4) Tubi rettangolari (S3, S13)')
    p = scrivi('rhs_40x30x2.stp', singolo([rett(-20, -15, 20, 15), rett(-18, -13, 18, 13, ccw=False)], 500))
    tub, pia = analizza(p)
    t = tub['tubi'][0] if tub['tubi'] else {}
    check('un tubo Rett. 40x30 sp.2mm (nuovo a catalogo)', len(tub['tubi']) == 1 and t.get('profilo') == 'Rett. 40x30 sp.2mm',
          tub['tubi'])
    check('lunghezza 0.5 m', t.get('lunghezza_m') == 0.5, t.get('lunghezza_m'))
    check('tagli dritto/dritto', (t.get('taglio_1'), t.get('taglio_2')) == ('dritto', 'dritto'))
    check('peso 1.0 kg', vicino(t.get('peso_kg'), 0.995, 0.02), t.get('peso_kg'))
    check('non contato anche come piastra', pia['piastre'] == [], pia['piastre'])

    p = scrivi('shs_40x40x1_5.stp', singolo([rett(-20, -20, 20, 20), rett(-18.5, -18.5, 18.5, 18.5, ccw=False)], 600))
    tub, _ = analizza(p)
    t = tub['tubi'][0] if tub['tubi'] else {}
    check('40x40 sp.1.5 riconosciuto come 1.5 (non 2)', t.get('profilo') == 'Quadro 40x40 sp.1.5mm', t.get('profilo'))

    p = scrivi('rhs_corto.stp', singolo([rett(-15, -10, 15, 10), rett(-13, -8, 13, 8, ccw=False)], 40))
    tub, _ = analizza(p)
    t = tub['tubi'][0] if tub['tubi'] else {}
    check('spezzone 30x20 lungo 40mm tenuto', t.get('profilo') == 'Rett. 30x20 sp.2mm' and t.get('lunghezza_m') == 0.04,
          tub['tubi'])

    p = scrivi('shs_45.stp', singolo([rett(-22.5, -22.5, 22.5, 22.5), rett(-20, -20, 20, 20, ccw=False)], 1000))
    tub, _ = analizza(p)
    t = tub['tubi'][0] if tub['tubi'] else {}
    kgm = step_tubolari.peso_teorico_rhs(45, 45, 2.5)
    check('45x45x2.5 non a catalogo: tenuto', len(tub['tubi']) == 1, tub)
    check('con avviso profilo_non_a_catalogo', 'profilo_non_a_catalogo' in t.get('avvisi', []), t.get('avvisi'))
    check(f'peso dalla geometria {kgm:.2f} kg', vicino(t.get('peso_kg'), kgm, 0.02), t.get('peso_kg'))

    # -----------------------------------------------------------------
    print('\n5) Tubo con testa a 45 gradi (S2, S12)')
    p = scrivi('rhs_45gradi.stp', singolo([rett(-20, -15, 20, 15), rett(-18, -13, 18, 13, ccw=False)], 500, k=1.0))
    tub, pia = analizza(p)
    t = tub['tubi'][0] if tub['tubi'] else {}
    check('riconosciuto come tubo 40x30', t.get('profilo') == 'Rett. 40x30 sp.2mm', tub['tubi'])
    check('lunghezza punta-punta 0.52 m', t.get('lunghezza_m') == 0.52, t.get('lunghezza_m'))
    check('taglio dritto + obliquo 45', t.get('taglio_1') == 'dritto' and t.get('taglio_2') == 'obliquo'
          and vicino(t.get('angolo_taglio_2'), 45.0, 0.5), (t.get('taglio_1'), t.get('taglio_2'), t.get('angolo_taglio_2')))
    check('NON anche piastra (esclusione per body_id)', pia['piastre'] == [], pia['piastre'])
    pia2 = step_piastre.analizza_step_piastre(p)
    check('NON piastra nemmeno senza esclusione (test corpo cavo)', pia2['piastre'] == [], pia2['piastre'])

    # -----------------------------------------------------------------
    print('\n6) Tubi tondi (S4, S6, S12)')
    p = scrivi('chs_40x2.stp', singolo([cerchio(0, 0, 20), foro(0, 0, 18)], 300))
    tub, pia = analizza(p)
    t = tub['tubi'][0] if tub['tubi'] else {}
    check('Tondo D40 sp.2 (nuovo a catalogo EN 10305)', t.get('profilo') == 'Tondo Ø40 sp.2mm', tub['tubi'])
    check('lunghezza 0.3 m, peso > 0', t.get('lunghezza_m') == 0.3 and (t.get('peso_kg') or 0) > 0.5, t)
    check('non anche piastra', pia['piastre'] == [], pia['piastre'])

    k30 = math.tan(math.radians(30))
    p = scrivi('chs_30gradi.stp', singolo([cerchio(0, 0, 20), foro(0, 0, 18)], 300, k=k30))
    tub, _ = analizza(p)
    t = tub['tubi'][0] if tub['tubi'] else {}
    check('taglio obliquo a 30 gradi riconosciuto (prima "dritto")',
          t.get('taglio_2') == 'obliquo' and vicino(t.get('angolo_taglio_2'), 30.0, 0.5) and t.get('taglio_1') == 'dritto',
          (t.get('taglio_1'), t.get('taglio_2'), t.get('angolo_taglio_2')))
    check('lunghezza punta-punta 0.312 m', vicino(t.get('lunghezza_m'), 0.3115, 0.0015), t.get('lunghezza_m'))

    p = scrivi('chs_fuori_catalogo.stp', singolo([cerchio(0, 0, 36), foro(0, 0, 33)], 1000))
    tub, _ = analizza(p)
    t = tub['tubi'][0] if tub['tubi'] else {}
    kgm = step_tubolari.peso_teorico_chs(72, 3)
    check('D72x3 fuori catalogo: peso dalla geometria (prima 0)', vicino(t.get('peso_kg'), kgm, 0.02)
          and 'profilo_non_a_catalogo' in t.get('avvisi', []), t)

    p = scrivi('disco_forato.stp', singolo([cerchio(0, 0, 100), foro(0, 0, 50)], 10))
    tub, pia = analizza(p)
    check('disco D200 t10 foro D100: NON e\' un tubo', tub['tubi'] == [], tub['tubi'])
    a = pia['piastre'][0]['area_dm2'] if pia['piastre'] else None
    check('e\' una piastra da 2.356 dm2', vicino(a, math.pi * (100 ** 2 - 50 ** 2) / 1e4, 0.005), a)

    semi = [('A', (50, 0), (-50, 0), (0, 0), True), ('L', (-50, 0), (-47, 0)),
            ('A', (-47, 0), (47, 0), (0, 0), False), ('L', (47, 0), (50, 0))]
    p = scrivi('semiguscio.stp', singolo([semi], 300))
    tub, pia = analizza(p)
    check('semiguscio calandrato: NON e\' un tubo intero', tub['tubi'] == [], tub['tubi'])
    a = pia['piastre'][0]['area_dm2'] if pia['piastre'] else None
    check('lamiera t=3 sviluppo 4.57 dm2', vicino(a, 48.5 * math.pi * 300 / 1e4, 0.01)
          and pia['piastre'][0]['spessore_mm'] == 3.0, pia['piastre'])

    p = scrivi('tondo_pieno.stp', singolo([cerchio(0, 0, 10)], 300))
    tub, _ = analizza(p)
    t = tub['tubi'][0] if tub['tubi'] else {}
    check('tondo pieno D20: peso del pieno 0.74 kg', t.get('tipo') == 'TONDO_PIENO'
          and vicino(t.get('peso_kg'), 0.74, 0.01), t)

    # -----------------------------------------------------------------
    print('\n7) Assieme: parte x4 + sotto-assieme x2 con parte x3 (S1)')

    def _ass(w):
        gamba = w.parte('Gamba', [w.estrudi([rett(-20, -15, 20, 15), rett(-18, -13, 18, 13, ccw=False)], 500)])
        piastrina = w.parte('Piastrina', [w.estrudi([rett(0, 0, 100, 50), foro(30, 25, 5)], 3)])
        sub = w.assieme('Sotto', [(piastrina, [(0, 0, 0), (200, 0, 0), (400, 0, 0)])])
        w.assieme('Telaio', [(gamba, [(0, 0, 0), (1000, 0, 0), (0, 1000, 0), (1000, 1000, 0)]),
                             (sub, [(0, 0, 600), (0, 500, 600)])])
    p = scrivi('assieme_qty.stp', _ass)
    qty = step_assieme.conta_istanze_nauo(p)
    tub, pia = analizza(p)
    q_tubo = [qty.get(t['body_id']) for t in tub['tubi']]
    q_pia = [qty.get(x['body_id']) for x in pia['piastre']]
    check('gamba istanziata 4 volte', q_tubo == [4], q_tubo)
    check('piastrina 2 x 3 = 6 (moltiplicato lungo l\'albero)', q_pia == [6], q_pia)
    ass = step_assieme.analizza_step_assieme(p)
    check('n_corpi = 10 istanze', ass['n_corpi'] == 10, ass['n_corpi'])

    p = scrivi('parte_singola.stp', singolo([rett(0, 0, 100, 50)], 3))
    qty = step_assieme.conta_istanze_nauo(p)
    check('parte singola: qty 1', list(qty.values()) == [1], qty)

    # -----------------------------------------------------------------
    print('\n8) File in pollici (S11)')
    p = scrivi('piastra_pollici.stp', singolo([rett(0, 0, 100, 50), foro(30, 25, 5)], 3), pollici=True)
    _, info = step_parser.carica_entita(p)
    check('unita\' letta: inch (scala 25.4)', info['unita'] == 'inch' and info['scala_mm'] == 25.4, info)
    _, pia = analizza(p)
    pp = pia['piastre']
    check('stessa piastra in mm: 3.0 / 0.492 dm2', bool(pp) and pp[0]['spessore_mm'] == 3.0
          and vicino(pp[0]['area_dm2'], 0.4921, 0.002), pp)

    # -----------------------------------------------------------------
    print('\n9) Saldature (S14)')

    def _tubo_su_piastra(w):
        base = w.estrudi([rett(-100, -100, 100, 100)], 10, off=(0, 0, -10))
        tubo = w.estrudi([rett(-20, -15, 20, 15), rett(-18, -13, 18, 13, ccw=False)], 500)
        w.parte('Multibody', [base, tubo])
    p = scrivi('saldatura_rhs.stp', _tubo_su_piastra)
    s = step_assieme.analizza_step_assieme(p)['saldatura_mm']
    check('RHS 40x30 su piastra: 140 mm (solo perimetro esterno, non 264)', vicino(s, 140, 1), s)

    def _chs_su_piastra(w):
        base = w.estrudi([rett(-100, -100, 100, 100)], 10, off=(0, 0, -10))
        tubo = w.estrudi([cerchio(0, 0, 20), foro(0, 0, 18)], 300)
        w.parte('Multibody', [base, tubo])
    p = scrivi('saldatura_chs.stp', _chs_su_piastra)
    s = step_assieme.analizza_step_assieme(p)['saldatura_mm']
    check('CHS D40 su piastra: 125.7 mm', vicino(s, 40 * math.pi, 1.5), s)

    def _affiancate(w):
        a = w.estrudi([rett(0, 0, 100, 100)], 5)
        b = w.estrudi([rett(100, 0, 200, 100)], 5)
        w.parte('Affiancate', [a, b])
    p = scrivi('saldatura_affiancate.stp', _affiancate)
    s = step_assieme.analizza_step_assieme(p)['saldatura_mm']
    check('piastre complanari affiancate: solo la giunzione (<= 2x100), non tutti i bordi',
          s <= 200.5 and s >= 99.5, s)

    def _ass_composto(w):
        base = w.parte('Base', [w.estrudi([rett(-100, -100, 100, 100)], 10)])
        tubo = w.parte('Tubo', [w.estrudi([rett(-20, -15, 20, 15), rett(-18, -13, 18, 13, ccw=False)], 500)])
        sub = w.assieme('SottoTubo', [(tubo, [(0, 0, 4)])])
        w.assieme('Root', [(base, [(0, 0, 0)]), (sub, [(0, 0, 6)])])
    p = scrivi('saldatura_assieme.stp', _ass_composto)
    s = step_assieme.analizza_step_assieme(p)['saldatura_mm']
    check('assieme con sotto-assieme (z 4+6 composte): 140 mm', vicino(s, 140, 1), s)

    # -----------------------------------------------------------------
    print('\n10) Parser (S15)')
    ent, _ = step_parser.carica_entita(os.path.join(DIR, 'piastra_foro.stp'))
    prod = [v for v in ent.values() if v.startswith('PRODUCT(')]
    check("';' e '' dentro le stringhe non troncano l'entita'",
          len(prod) == 1 and prod[0].endswith(')') and "l''angolo" in prod[0], prod)
    cplx = [v for v in ent.values() if 'GLOBAL_UNIT_ASSIGNED_CONTEXT' in v]
    check('entita\' complessa: sotto-tipi elencati',
          bool(cplx) and 'GLOBAL_UNIT_ASSIGNED_CONTEXT' in step_parser.sottotipi_entita(cplx[0])
          and step_parser.tipo_entita(cplx[0]) == 'GEOMETRIC_REPRESENTATION_CONTEXT', cplx)
    rr = [v for v in step_parser.carica_entita(os.path.join(DIR, 'assieme_qty.stp'))[0].values()
          if 'REPRESENTATION_RELATIONSHIP_WITH_TRANSFORMATION' in v]
    check('RRWT complessa tipizzata', bool(rr) and step_parser.tipo_entita(rr[0]) ==
          'REPRESENTATION_RELATIONSHIP_WITH_TRANSFORMATION')
    geo = step_parser.GeometriaStep(ent)
    b = geo.corpi()
    check('volume B-rep piastra forata = 14764 mm3', len(b) == 1 and
          vicino(geo.volume_corpo(b[0]), (5000 - 25 * math.pi) * 3, 1.0), geo.volume_corpo(b[0]) if b else None)

    # -----------------------------------------------------------------
    print('\n11) Route POST /api/preventivi/<id>/import-step')
    s = models.SessionLocal()
    try:
        s.add(User(id='commerciale', name='Marco', role='Commerciale', is_active=True))
        s.commit()
    finally:
        s.close()
    c = app.test_client()
    pid = 'test-step-' + uuid.uuid4().hex[:8]

    def _import(nome):
        with open(os.path.join(DIR, nome), 'rb') as fh:
            dati = fh.read()
        r = c.post(f'/api/preventivi/{pid}/import-step?admin_id=commerciale',
                   data={'file': (io.BytesIO(dati), nome)}, content_type='multipart/form-data')
        return r.status_code, r.get_json() or {}
    try:
        st, d = _import('assieme_qty.stp')
        check('risposta 200', st == 200 and d.get('success'), d)
        tr = d.get('tubolari') or []
        pr = d.get('piastre') or []
        check('tubolare con qty 4', len(tr) == 1 and tr[0].get('qty') == 4, tr)
        if tr:
            check('peso riga = 4 x unitario', vicino(tr[0]['peso_kg'], 4 * tr[0]['peso_unitario_kg'], 0.011), tr[0])
            check('tagli riga = 2 teste x 4', tr[0]['n_tagli_dritti'] == 8, tr[0])
        check('piastra con qty 6', len(pr) == 1 and pr[0].get('qty') == 6, pr)
        sm = d.get('summary') or {}
        check('summary: 4 tubi + 6 piastre come pezzi', sm.get('n_tubolari_pezzi') == 4
              and sm.get('n_piastre_pezzi') == 6, sm)
        atteso = round(sum(x['peso_kg'] for x in tr) + sum(x['peso_kg'] for x in pr), 2)
        check('peso totale = somma righe (qty incluse)', vicino(sm.get('peso_totale_kg'), atteso, 0.011)
              and vicino(d['assiemi'][0]['peso_kg'], atteso, 0.011), (sm.get('peso_totale_kg'), atteso))
        check('avvisi presenti nella risposta (lista)', isinstance(d.get('avvisi'), list), d.get('avvisi'))

        st, d = _import('rhs_45gradi.stp')
        check('tubo a 45: 1 tubolare, 0 piastre', len(d.get('tubolari') or []) == 1 and d.get('piastre') == [],
              (d.get('tubolari'), d.get('piastre')))
        if d.get('tubolari'):
            t = d['tubolari'][0]
            check('tagli per riga: 1 dritto + 1 obliquo (prima contava solo la testa 1)',
                  t['n_tagli_dritti'] == 1 and t['n_tagli_obliqui'] == 1, t)

        st, d = _import('shs_45.stp')
        check('profilo fuori catalogo segnalato negli avvisi',
              any('profilo_non_a_catalogo' in a for a in d.get('avvisi') or []), d.get('avvisi'))
    finally:
        shutil.rmtree(os.path.join(UPLOAD_FOLDER, 'preventivi_tmp', pid), ignore_errors=True)

    if '--salva-fixture' in sys.argv:
        dest = os.path.join(_APP, '_test_input', 'step')
        os.makedirs(dest, exist_ok=True)
        for fn in os.listdir(DIR):
            shutil.copy(os.path.join(DIR, fn), os.path.join(dest, fn))
        print(f'\nFixture copiate in {dest}')

    print('\n' + '=' * 60)
    print(f'PASSATI: {OK}   FALLITI: {len(KO)}')
    if KO:
        print('Falliti: ' + ', '.join(KO))
    print('=' * 60)
    return 1 if KO else 0


if __name__ == '__main__':
    code = main()
    shutil.rmtree(DIR, ignore_errors=True)
    try:
        _ENG.dispose()
        os.remove(_TMP)
    except Exception:
        pass
    sys.exit(code)
