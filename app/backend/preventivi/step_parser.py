"""STEP file geometry parser.

Extracts edge wireframes (LINE + CIRCLE interpolated) and face polygons
(from ADVANCED_FACE edge loops) for solid-like rendering.
"""

import logging
import math
import os
import re

logger = logging.getLogger(__name__)


# ===========================================================================
# LETTURA ENTITA' (tokenizer robusto) + UNITA' DI MISURA
# ===========================================================================
# Condiviso da step_tubolari / step_piastre / step_assieme: tutte le coordinate
# e i raggi restituiti sono GIA' convertiti in millimetri.

# Un'entita' finisce al primo ';' FUORI dalle stringhe ('...' con '' come escape).
# Quantificatori possessivi (Python >= 3.11): nessun backtracking catastrofico.
_RE_ENTITA = re.compile(r"#(\d+)\s*=\s*((?:[^;']++|'(?:[^']|'')*+')*+);")
_RE_STRINGA = re.compile(r"'(?:[^']|'')*'")
_RE_NUMERO = re.compile(r"(?<![#\w.])[-+]?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?")
_RE_TIPO = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_]*)")
_RE_REF = re.compile(r"#(\d+)")

# Per le entita' complesse "#n=( A() B() C() );" il tipo "rappresentativo"
# e' il primo di questa lista presente tra i sotto-tipi (altrimenti il primo).
_PRIORITA_COMPLESSI = (
    'REPRESENTATION_RELATIONSHIP_WITH_TRANSFORMATION',
    'B_SPLINE_SURFACE_WITH_KNOTS', 'B_SPLINE_CURVE_WITH_KNOTS',
    'B_SPLINE_SURFACE', 'B_SPLINE_CURVE',
    'LENGTH_UNIT', 'PLANE_ANGLE_UNIT', 'SOLID_ANGLE_UNIT',
    'GEOMETRIC_REPRESENTATION_CONTEXT',
)

# Entita' con lunghezze da scalare: tipo -> indici dei numeri (non-ref) da
# scalare, None = tutti.
_PARAMETRI_LUNGHEZZA = {
    'CARTESIAN_POINT': None,
    'CIRCLE': (0,),
    'CYLINDRICAL_SURFACE': (0,),
    'CONICAL_SURFACE': (0,),
    'SPHERICAL_SURFACE': (0,),
    'TOROIDAL_SURFACE': (0, 1),
    'DEGENERATE_TOROIDAL_SURFACE': (0, 1),
    'ELLIPSE': (0, 1),
    'VECTOR': (0,),
    'OFFSET_SURFACE': (0,),
    'OFFSET_CURVE_3D': (0,),
}

_PREFISSI_SI = {None: 1000.0, 'KILO': 1e6, 'HECTO': 1e5, 'DECA': 1e4,
                'DECI': 100.0, 'CENTI': 10.0, 'MILLI': 1.0,
                'MICRO': 1e-3, 'NANO': 1e-6}
_UNITA_NOMINATE = {'INCH': 25.4, 'IN': 25.4, 'FOOT': 304.8, 'FT': 304.8,
                   'YARD': 914.4, 'MIL': 0.0254, 'THOU': 0.0254}


def sottotipi_entita(val: str) -> list:
    """Tipi delle sotto-entita' di un'entita' complessa "( A(..) B(..) )".
    Per un'entita' semplice ritorna [tipo]."""
    if not val:
        return []
    if val.lstrip()[:1] != '(':
        m = _RE_TIPO.match(val)
        return [m.group(1)] if m else []
    tipi = []
    depth = 0
    i = 0
    s = _RE_STRINGA.sub("''", val)
    n = len(s)
    while i < n:
        c = s[i]
        if c == '(':
            depth += 1
            i += 1
        elif c == ')':
            depth -= 1
            i += 1
        elif depth == 1 and (c.isalpha() or c == '_'):
            j = i
            while j < n and (s[j].isalnum() or s[j] == '_'):
                j += 1
            tipi.append(s[i:j])
            i = j
        else:
            i += 1
    return tipi


def tipo_entita(val: str) -> str:
    """Tipo di un'entita' (gestisce anche le entita' complesse)."""
    if not val:
        return ''
    if val[0] != '(':
        m = _RE_TIPO.match(val)
        return m.group(1) if m else ''
    st = sottotipi_entita(val)
    for p in _PRIORITA_COMPLESSI:
        if p in st:
            return p
    return st[0] if st else ''


def numeri_entita(val: str) -> list:
    """Numeri letterali dell'entita', escluse stringhe e riferimenti #n."""
    s = _RE_STRINGA.sub("''", val)
    return [float(x) for x in _RE_NUMERO.findall(s)]


def _scala_valore(val: str, indici, scala: float) -> str:
    """Moltiplica per `scala` i numeri (non-ref, fuori stringa) indicati."""
    parti = []
    pos = 0
    contatore = [0]

    def _sub(m):
        k = contatore[0]
        contatore[0] += 1
        if indici is not None and k not in indici:
            return m.group(0)
        return repr(float(m.group(0)) * scala)

    for m in _RE_STRINGA.finditer(val):
        parti.append(_RE_NUMERO.sub(_sub, val[pos:m.start()]))
        parti.append(m.group(0))
        pos = m.end()
    parti.append(_RE_NUMERO.sub(_sub, val[pos:]))
    return ''.join(parti)


def _scala_unita_lunghezza(eid, entities, depth=0):
    """Fattore -> mm di un'unita' di lunghezza (SI_UNIT o CONVERSION_BASED_UNIT)."""
    if depth > 5:
        return None
    val = entities.get(eid, '')
    if 'LENGTH_UNIT' not in val:
        return None
    m = re.search(r"SI_UNIT\s*\(\s*(?:\.(\w+)\.|\$|\*)\s*,\s*\.METRE\.\s*\)", val)
    if m:
        return _PREFISSI_SI.get(m.group(1), None)
    m = re.search(r"CONVERSION_BASED_UNIT\s*\(\s*'((?:[^']|'')*)'\s*,\s*#(\d+)", val)
    if m:
        nome = m.group(1).strip().upper()
        mwu = entities.get(int(m.group(2)), '')
        nums = numeri_entita(mwu)
        rr = [int(x) for x in _RE_REF.findall(mwu)]
        if nums and rr:
            base = _scala_unita_lunghezza(rr[-1], entities, depth + 1)
            if base:
                return nums[0] * base
        return _UNITA_NOMINATE.get(nome)
    return None


def _rileva_unita(entities: dict) -> tuple:
    """Ritorna (scala_mm, nome_unita, avvisi)."""
    avvisi = []
    scale_ctx = []
    for eid, val in entities.items():
        if 'GLOBAL_UNIT_ASSIGNED_CONTEXT' not in val:
            continue
        m = re.search(r"GLOBAL_UNIT_ASSIGNED_CONTEXT\s*\(\s*\(([^()]*)\)", val)
        if not m:
            continue
        for r in _RE_REF.findall(m.group(1)):
            s = _scala_unita_lunghezza(int(r), entities)
            if s:
                scale_ctx.append(s)
    if not scale_ctx:
        for eid, val in entities.items():
            if 'LENGTH_UNIT' in val:
                s = _scala_unita_lunghezza(eid, entities)
                if s:
                    scale_ctx.append(s)
                    break
    if not scale_ctx:
        avvisi.append("unita_sconosciute: unita' di lunghezza non dichiarata, assunti millimetri")
        return 1.0, 'mm?', avvisi
    distinte = sorted(set(round(s, 9) for s in scale_ctx))
    scala = max(set(scale_ctx), key=scale_ctx.count)
    if len(distinte) > 1:
        avvisi.append('unita_miste: il file dichiara piu\' unita\' di lunghezza, '
                      'usata la piu\' frequente')
    nomi = {1.0: 'mm', 10.0: 'cm', 1000.0: 'm', 25.4: 'inch', 304.8: 'foot'}
    nome = nomi.get(round(scala, 6), f'{scala:g} mm')
    return scala, nome, avvisi


_CACHE_ENTITA = {}


def carica_entita(step_path: str) -> tuple:
    """Legge un file STEP e ritorna (entities, info).

    entities: {id: testo_entita'} con lunghezze GIA' convertite in mm.
    info: {'scala_mm', 'unita', 'avvisi': [...]}.
    Solleva IOError/OSError se il file non e' leggibile.
    Cache in memoria (ultimi 3 file, chiave path+mtime+size): le tre analisi
    dell'import-step non ri-parsano lo stesso file.
    """
    st = os.stat(step_path)
    key = (os.path.abspath(step_path), st.st_mtime_ns, st.st_size)
    hit = _CACHE_ENTITA.get(key)
    if hit is not None:
        return hit
    with open(step_path, 'r', errors='replace') as f:
        content = f.read()
    idx = content.find('DATA;')
    if idx < 0:
        idx = 0
    entities = {}
    for m in _RE_ENTITA.finditer(content, idx):
        entities[int(m.group(1))] = m.group(2).strip()
    scala, unita, avvisi = _rileva_unita(entities)
    if abs(scala - 1.0) > 1e-12:
        for eid, val in entities.items():
            t = tipo_entita(val)
            if t in _PARAMETRI_LUNGHEZZA:
                entities[eid] = _scala_valore(val, _PARAMETRI_LUNGHEZZA[t], scala)
    info = {'scala_mm': scala, 'unita': unita, 'avvisi': avvisi}
    if len(_CACHE_ENTITA) >= 3:
        _CACHE_ENTITA.pop(next(iter(_CACHE_ENTITA)))
    _CACHE_ENTITA[key] = (entities, info)
    return entities, info


# ===========================================================================
# ALGEBRA MINIMA (vettori 3D, matrici 4x4 come liste di righe)
# ===========================================================================

def v_add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def v_sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def v_mul(a, s):
    return (a[0] * s, a[1] * s, a[2] * s)


def v_dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def v_cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def v_len(a):
    return math.sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])


def v_norm(a):
    ln = v_len(a)
    return (a[0] / ln, a[1] / ln, a[2] / ln) if ln > 1e-12 else (0.0, 0.0, 0.0)


def v_perp(n):
    """Un versore qualsiasi perpendicolare a n."""
    base = (1.0, 0.0, 0.0) if abs(n[0]) < 0.9 else (0.0, 1.0, 0.0)
    return v_norm(v_sub(base, v_mul(n, v_dot(base, n))))


MAT_ID = ((1.0, 0.0, 0.0, 0.0), (0.0, 1.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0), (0.0, 0.0, 0.0, 1.0))


def mat_mul(A, B):
    return tuple(tuple(sum(A[i][k] * B[k][j] for k in range(4)) for j in range(4)) for i in range(4))


def mat_da_frame(o, z, x):
    """Matrice locale->globale di un AXIS2_PLACEMENT_3D."""
    y = v_cross(z, x)
    return ((x[0], y[0], z[0], o[0]), (x[1], y[1], z[1], o[1]),
            (x[2], y[2], z[2], o[2]), (0.0, 0.0, 0.0, 1.0))


def mat_inv_rigida(M):
    R = [[M[j][i] for j in range(3)] for i in range(3)]  # trasposta
    t = (M[0][3], M[1][3], M[2][3])
    ti = tuple(-sum(R[i][k] * t[k] for k in range(3)) for i in range(3))
    return ((R[0][0], R[0][1], R[0][2], ti[0]), (R[1][0], R[1][1], R[1][2], ti[1]),
            (R[2][0], R[2][1], R[2][2], ti[2]), (0.0, 0.0, 0.0, 1.0))


def mat_punto(M, p):
    return (M[0][0] * p[0] + M[0][1] * p[1] + M[0][2] * p[2] + M[0][3],
            M[1][0] * p[0] + M[1][1] * p[1] + M[1][2] * p[2] + M[1][3],
            M[2][0] * p[0] + M[2][1] * p[1] + M[2][2] * p[2] + M[2][3])


def mat_dir(M, d):
    return (M[0][0] * d[0] + M[0][1] * d[1] + M[0][2] * d[2],
            M[1][0] * d[0] + M[1][1] * d[1] + M[1][2] * d[2],
            M[2][0] * d[0] + M[2][1] * d[1] + M[2][2] * d[2])


def _wrap_pi(a):
    while a > math.pi:
        a -= 2 * math.pi
    while a <= -math.pi:
        a += 2 * math.pi
    return a


def copertura_angolare(punti, c, a):
    """Ampiezza angolare (rad) coperta dai punti attorno all'asse (c, a)."""
    if not punti:
        return 0.0
    u = v_perp(a)
    w = v_cross(a, u)
    ang = []
    for p in punti:
        d = v_sub(p, c)
        ang.append(math.atan2(v_dot(d, w), v_dot(d, u)))
    ang.sort()
    gaps = [ang[i + 1] - ang[i] for i in range(len(ang) - 1)]
    gaps.append(ang[0] + 2 * math.pi - ang[-1])
    return 2 * math.pi - max(gaps)


def distanza_retta(p, c, a):
    """Distanza del punto p dalla retta (c, a) con a versore."""
    d = v_sub(p, c)
    return v_len(v_sub(d, v_mul(a, v_dot(d, a))))


def punto_in_poligono_2d(p, loops2d, tol=0.5):
    """True se p (u,v) sta nella regione [esterno, fori...] (bordo incluso, tol mm)."""
    if not loops2d:
        return False

    def _dist_seg(p, a, b):
        dx, dy = b[0] - a[0], b[1] - a[1]
        l2 = dx * dx + dy * dy
        t = 0.0 if l2 < 1e-18 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2))
        qx, qy = a[0] + t * dx - p[0], a[1] + t * dy - p[1]
        return math.sqrt(qx * qx + qy * qy)

    def _dentro(p, poly):
        c = False
        n = len(poly)
        for i in range(n):
            a, b = poly[i], poly[(i + 1) % n]
            if (a[1] > p[1]) != (b[1] > p[1]):
                x = a[0] + (p[1] - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
                if p[0] < x:
                    c = not c
        return c

    if tol > 0:
        for poly in loops2d:
            n = len(poly)
            for i in range(n):
                if _dist_seg(p, poly[i], poly[(i + 1) % n]) <= tol:
                    return True
    if not _dentro(p, loops2d[0]):
        return False
    for foro in loops2d[1:]:
        if _dentro(p, foro):
            return False
    return True


# ===========================================================================
# GEOMETRIA B-REP
# ===========================================================================

_TIPI_CORPO = ('MANIFOLD_SOLID_BREP', 'BREP_WITH_VOIDS', 'FACETED_BREP')
_PASSO_ARCO = math.radians(7.5)


def _bool_finale(val, default=True):
    m = re.findall(r'\.([TF])\.', val)
    return (m[-1] == 'T') if m else default


class GeometriaStep:
    """Accesso alla geometria B-rep di un file STEP gia' letto (entities in mm).

    faccia(fid) -> dict con tipo superficie, anelli campionati ORDINATI
    (ORIENTED_EDGE + orientamento bound), area esatta (piani: poligono con
    fori sottratti; cilindri: integrale nel piano (theta, z)), contributo al
    volume (teorema della divergenza), copertura angolare dei cilindri.
    """

    def __init__(self, entities: dict):
        self.ent = entities
        self._tipi = {}
        self._facce = {}
        self._edge = {}

    # --- accesso base ---
    def val(self, eid):
        return self.ent.get(eid, '')

    def tipo(self, eid):
        t = self._tipi.get(eid)
        if t is None:
            t = tipo_entita(self.ent.get(eid, ''))
            self._tipi[eid] = t
        return t

    def refs(self, eid):
        return [int(x) for x in _RE_REF.findall(_RE_STRINGA.sub("''", self.ent.get(eid, '')))]

    def punto(self, eid):
        val = self.ent.get(eid, '')
        if self.tipo(eid) == 'VERTEX_POINT':
            r = self.refs(eid)
            return self.punto(r[0]) if r else None
        nums = numeri_entita(val)
        return tuple(nums[-3:]) if len(nums) >= 3 else None

    def direzione(self, eid):
        nums = numeri_entita(self.ent.get(eid, ''))
        return v_norm(tuple(nums[-3:])) if len(nums) >= 3 else None

    def placement(self, eid):
        """AXIS2_PLACEMENT_3D -> (origine, z, x) ortonormali."""
        r = self.refs(eid)
        o = self.punto(r[0]) if r else None
        if o is None:
            o = (0.0, 0.0, 0.0)
        z = self.direzione(r[1]) if len(r) >= 2 else None
        if not z or v_len(z) < 0.5:
            z = (0.0, 0.0, 1.0)
        x = self.direzione(r[2]) if len(r) >= 3 else None
        if x:
            x = v_sub(x, v_mul(z, v_dot(x, z)))
        if not x or v_len(x) < 1e-9:
            x = v_perp(z)
        return o, z, v_norm(x)

    def matrice_placement(self, eid):
        o, z, x = self.placement(eid)
        return mat_da_frame(o, z, x)

    # --- corpi ---
    def corpi(self, includi_superfici=False):
        """Id dei corpi solidi (MSB, BREP_WITH_VOIDS, FACETED_BREP) + CLOSED_SHELL
        orfani (+ SHELL_BASED_SURFACE_MODEL se richiesto)."""
        ids = [e for e in self.ent if self.tipo(e) in _TIPI_CORPO]
        usati = set()
        for e, val in self.ent.items():
            t = self.tipo(e)
            if t in _TIPI_CORPO or t in ('ORIENTED_CLOSED_SHELL', 'SHELL_BASED_SURFACE_MODEL'):
                usati.update(self.refs(e))
        for e in self.ent:
            if self.tipo(e) == 'CLOSED_SHELL' and e not in usati:
                ids.append(e)
        if includi_superfici:
            ids.extend(e for e in self.ent if self.tipo(e) == 'SHELL_BASED_SURFACE_MODEL')
        return ids

    def facce_corpo(self, bid):
        t = self.tipo(bid)
        r = self.refs(bid)
        if not r:
            return []
        if t in ('MANIFOLD_SOLID_BREP', 'FACETED_BREP'):
            return self.refs(r[-1])
        if t == 'BREP_WITH_VOIDS':
            return self.refs(r[0])
        if t in ('CLOSED_SHELL', 'OPEN_SHELL'):
            return r
        if t == 'SHELL_BASED_SURFACE_MODEL':
            out = []
            for s in r:
                if self.tipo(s) in ('CLOSED_SHELL', 'OPEN_SHELL'):
                    out.extend(self.refs(s))
            return out
        return self.refs(r[-1])

    # --- spigoli ---
    def _curva_base(self, cid, depth=0):
        t = self.tipo(cid)
        if depth < 5 and t in ('SURFACE_CURVE', 'SEAM_CURVE', 'INTERSECTION_CURVE', 'TRIMMED_CURVE'):
            r = self.refs(cid)
            if r:
                return self._curva_base(r[0], depth + 1)
        return cid

    def campiona_edge(self, ec):
        """Punti dell'EDGE_CURVE da v1 a v2 lungo la curva.

        -> (punti, v1, v2, tipo_curva, corr) dove corr e' il vettore-area dei
        segmenti circolari tra corde e arco (percorso v1->v2): sommato al
        vettore di Newell rende ESATTA l'area delle facce piane con archi."""
        hit = self._edge.get(ec)
        if hit is not None:
            return hit
        r = self.refs(ec)
        res = ([], None, None, '', (0.0, 0.0, 0.0))
        if len(r) >= 3:
            v1, v2 = r[0], r[1]
            p1, p2 = self.punto(v1), self.punto(v2)
            if p1 and p2:
                same = _bool_finale(self.ent.get(ec, ''))
                cid = self._curva_base(r[2])
                ct = self.tipo(cid)
                pts = [p1, p2]
                corr = (0.0, 0.0, 0.0)
                chiuso = (v1 == v2) or v_len(v_sub(p1, p2)) < 1e-6
                if ct in ('CIRCLE', 'ELLIPSE'):
                    cr = self.refs(cid)
                    nums = numeri_entita(self.ent.get(cid, ''))
                    if cr and nums:
                        o, z, x = self.placement(cr[0])
                        y = v_cross(z, x)
                        a1 = nums[0]
                        a2 = nums[1] if (ct == 'ELLIPSE' and len(nums) > 1) else a1

                        def _ang(p):
                            d = v_sub(p, o)
                            return math.atan2(v_dot(d, y) / a2, v_dot(d, x) / a1)
                        t1, t2 = _ang(p1), _ang(p2)
                        if same:
                            dt = (t2 - t1) % (2 * math.pi)
                            if chiuso or dt < 1e-9:
                                dt = 2 * math.pi
                        else:
                            dt = -((t1 - t2) % (2 * math.pi))
                            if chiuso or dt > -1e-9:
                                dt = -2 * math.pi
                        n = max(2, int(math.ceil(abs(dt) / _PASSO_ARCO)))
                        pts = []
                        for i in range(n + 1):
                            tt = t1 + dt * i / n
                            pts.append(v_add(o, v_add(v_mul(x, a1 * math.cos(tt)),
                                                       v_mul(y, a2 * math.sin(tt)))))
                        pts[0] = p1
                        pts[-1] = p2
                        passo = abs(dt) / n
                        seg = n * a1 * a2 / 2.0 * (passo - math.sin(passo))
                        corr = v_mul(z, seg if dt > 0 else -seg)
                elif ct in ('B_SPLINE_CURVE_WITH_KNOTS', 'B_SPLINE_CURVE', 'POLYLINE',
                            'BEZIER_CURVE', 'QUASI_UNIFORM_CURVE', 'UNIFORM_CURVE'):
                    cps = [self.punto(x) for x in self.refs(cid)]
                    cps = [c for c in cps if c]
                    if len(cps) >= 2:
                        if v_len(v_sub(cps[-1], p1)) < v_len(v_sub(cps[0], p1)):
                            cps.reverse()
                        pts = [p1] + cps[1:-1] + [p2]
                res = (pts, v1, v2, ct, corr)
        self._edge[ec] = res
        return res

    def _anello(self, lid):
        """Anello ordinato -> (punti, [(ec, punti_orientati)], vertici, corr_area)."""
        t = self.tipo(lid)
        r = self.refs(lid)
        zero = (0.0, 0.0, 0.0)
        if t == 'POLY_LOOP':
            pts = [self.punto(x) for x in r]
            return [p for p in pts if p], [], set(), zero
        if t == 'VERTEX_LOOP':
            p = self.punto(r[0]) if r else None
            return ([p] if p else []), [], set(r[:1]), zero
        pts, edges, verts = [], [], set()
        corr_tot = zero
        for oe in r:
            if self.tipo(oe) != 'ORIENTED_EDGE':
                continue
            orr = self.refs(oe)
            if not orr:
                continue
            ec = orr[-1]
            ep, v1, v2, _, corr = self.campiona_edge(ec)
            if not ep:
                continue
            verts.update((v1, v2))
            if not _bool_finale(self.ent.get(oe, '')):
                ep = list(reversed(ep))
                corr = v_mul(corr, -1.0)
            corr_tot = v_add(corr_tot, corr)
            edges.append((ec, ep))
            if pts and v_len(v_sub(pts[-1], ep[0])) < 1e-6:
                pts.extend(ep[1:])
            else:
                pts.extend(ep)
        if len(pts) > 1 and v_len(v_sub(pts[0], pts[-1])) < 1e-6:
            pts.pop()
        return pts, edges, verts, corr_tot

    # --- facce ---
    def faccia(self, fid):
        hit = self._facce.get(fid)
        if hit is not None or fid in self._facce:
            return hit
        self._facce[fid] = None
        t = self.tipo(fid)
        if t not in ('ADVANCED_FACE', 'FACE_SURFACE', 'FACE'):
            return None
        val = self.ent.get(fid, '')
        r = self.refs(fid)
        sense = _bool_finale(val) if t != 'FACE' else True
        f = {'id': fid, 'sense': sense, 'loops': [], 'edges': set(), 'vertici': set(),
             'tipo': 'ALTRO', 'area': 0.0, 'vol': None}
        surf = r[-1] if (r and t != 'FACE') else None
        st = self.tipo(surf) if surf else ''
        for b in r:
            bt = self.tipo(b)
            if bt not in ('FACE_OUTER_BOUND', 'FACE_BOUND'):
                continue
            br = self.refs(b)
            if not br:
                continue
            pts, edges, verts, corr = self._anello(br[0])
            if not _bool_finale(self.ent.get(b, '')):
                pts = list(reversed(pts))
                edges = [(ec, list(reversed(ep))) for ec, ep in reversed(edges)]
                corr = v_mul(corr, -1.0)
            f['loops'].append({'esterno': bt == 'FACE_OUTER_BOUND', 'punti': pts, 'edges': edges,
                               'corr': corr})
            f['edges'].update(ec for ec, _ in edges)
            f['vertici'].update(verts)
        f['punti'] = [p for lp in f['loops'] for p in lp['punti']]
        segno = 1.0 if sense else -1.0
        if st == 'PLANE':
            o, z, x = self.placement(self.refs(surf)[0]) if self.refs(surf) else ((0, 0, 0), (0, 0, 1), (1, 0, 0))
            f.update(tipo='PLANE', origin=o, normal=v_mul(z, segno), u=x, v=v_cross(z, x))
            aree = []
            for lp in f['loops']:
                pts = lp['punti']
                nw = v_mul(lp['corr'], 2.0)  # segmenti circolari (archi esatti)
                for i in range(len(pts)):
                    nw = v_add(nw, v_cross(pts[i], pts[(i + 1) % len(pts)]))
                aree.append(abs(v_dot(nw, z)) / 2.0)
            if aree:
                i_est = next((i for i, lp in enumerate(f['loops']) if lp['esterno']), None)
                if i_est is None:
                    i_est = max(range(len(aree)), key=lambda i: aree[i])
                # l'anello esterno per primo (serve al test punto-in-poligono)
                if i_est != 0:
                    f['loops'].insert(0, f['loops'].pop(i_est))
                    aree.insert(0, aree.pop(i_est))
                f['area'] = max(0.0, aree[0] - sum(aree[1:]))
            f['vol'] = v_dot(o, f['normal']) * f['area'] / 3.0
        elif st == 'CYLINDRICAL_SURFACE':
            sr = self.refs(surf)
            nums = numeri_entita(self.ent.get(surf, ''))
            rr = nums[0] if nums else 0.0
            c, a, u = self.placement(sr[0]) if sr else ((0, 0, 0), (0, 0, 1), (1, 0, 0))
            w = v_cross(a, u)
            f.update(tipo='CYL', origin=c, axis=a, raggio=rr, u=u)
            tot_i = 0.0
            tot_j = (0.0, 0.0, 0.0)
            avvolgimenti = []
            angoli = []
            zs = []
            for lp in f['loops']:
                pts = lp['punti']
                if not pts:
                    continue
                th = []
                for p in pts:
                    d = v_sub(p, c)
                    th.append(math.atan2(v_dot(d, w), v_dot(d, u)))
                    zs.append(v_dot(p, a))  # quota ASSOLUTA lungo l'asse
                angoli.extend(th)
                zl = [v_dot(v_sub(p, c), a) for p in pts]
                li = 0.0
                lj = (0.0, 0.0, 0.0)
                wnd = 0.0
                n = len(pts)
                for i in range(n):
                    k = (i + 1) % n
                    dth = _wrap_pi(th[k] - th[i])
                    zm = (zl[i] + zl[k]) / 2.0
                    tm = th[i] + dth / 2.0
                    li += zm * dth
                    lj = v_add(lj, v_mul(v_add(v_mul(u, math.cos(tm)), v_mul(w, math.sin(tm))), zm * dth))
                    wnd += dth
                tot_i += li
                tot_j = v_add(tot_j, lj)
                if abs(wnd) > math.pi:
                    avvolgimenti.append((sum(zl) / n, wnd, li))
            coerente = abs(sum(x[1] for x in avvolgimenti)) < math.pi
            if coerente:
                a_raw = -rr * tot_i
                s = 1.0 if a_raw >= 0 else -1.0
                f['area'] = abs(a_raw)
                jn = v_mul(tot_j, -rr * s)
                f['vol'] = segno * (v_dot(c, jn) + rr * f['area']) / 3.0
            else:
                # Orientamento anelli incoerente: area da quote medie, volume non affidabile
                zz = [x[0] for x in avvolgimenti]
                f['area'] = rr * 2 * math.pi * (max(zz) - min(zz))
                f['vol'] = None
            # copertura angolare (max gap tra gli angoli campionati)
            if angoli:
                ss = sorted(angoli)
                gaps = [ss[i + 1] - ss[i] for i in range(len(ss) - 1)]
                gaps.append(ss[0] + 2 * math.pi - ss[-1])
                f['copertura'] = 2 * math.pi - max(gaps)
            else:
                f['copertura'] = 0.0
            f['z_min'] = min(zs) if zs else 0.0
            f['z_max'] = max(zs) if zs else 0.0
        else:
            if st == 'CONICAL_SURFACE':
                f['tipo'] = 'CONE'
            elif st in ('TOROIDAL_SURFACE', 'DEGENERATE_TOROIDAL_SURFACE'):
                f['tipo'] = 'TORUS'
            elif st.startswith('B_SPLINE_SURFACE') or st in ('RATIONAL_B_SPLINE_SURFACE',):
                f['tipo'] = 'BSPLINE'
            elif st:
                f['tipo'] = st
            # area approssimata: poligono (Newell) dell'anello esterno meno i fori
            aree = []
            for lp in f['loops']:
                pts = lp['punti']
                nw = (0.0, 0.0, 0.0)
                for i in range(len(pts)):
                    nw = v_add(nw, v_cross(pts[i], pts[(i + 1) % len(pts)]))
                aree.append(v_len(nw) / 2.0)
            if aree:
                f['area'] = max(0.0, max(aree) - (sum(aree) - max(aree)))
        self._facce[fid] = f
        return f

    def facce(self, bid):
        out = []
        for fid in self.facce_corpo(bid):
            f = self.faccia(fid)
            if f is not None:
                out.append(f)
        return out

    def volume_corpo(self, bid):
        """Volume (mm3) col teorema della divergenza; None se il corpo ha facce
        non piane/cilindriche o anelli incoerenti, o se e' un BREP_WITH_VOIDS."""
        if self.tipo(bid) == 'BREP_WITH_VOIDS':
            return None
        fs = self.facce(bid)
        if not fs:
            return None
        tot = 0.0
        for f in fs:
            if f.get('vol') is None:
                return None
            tot += f['vol']
        return abs(tot)

    def genere_corpo(self, bid):
        """Genere topologico (0 = nessun foro passante, 1 = un foro/tubo...)."""
        fs = self.facce(bid)
        if not fs:
            return 0
        V = set()
        E = set()
        L = 0
        for f in fs:
            V |= f['vertici']
            E |= f['edges']
            L += len(f['loops'])
        chi = len(V) - len(E) + 2 * len(fs) - L
        return max(0, int(round((2 - chi) / 2.0)))

    def punti_corpo(self, bid):
        pts = []
        for f in self.facce(bid):
            pts.extend(f['punti'])
        return pts

    # --- albero prodotto / istanze ---
    def albero(self):
        """Albero di prodotto -> occorrenze dei corpi con trasformazione composta.

        PRODUCT_DEFINITION --(NEXT_ASSEMBLY_USAGE_OCCURRENCE)--> figli;
        PD -> PRODUCT_DEFINITION_SHAPE -> SHAPE_DEFINITION_REPRESENTATION -> rep;
        rep <-> rep via SHAPE_REPRESENTATION_RELATIONSHIP (senza trasformazione);
        posizionamento del figlio: CONTEXT_DEPENDENT_SHAPE_REPRESENTATION ->
        REPRESENTATION_RELATIONSHIP_WITH_TRANSFORMATION -> ITEM_DEFINED_TRANSFORMATION.
        Anche MAPPED_ITEM (REPRESENTATION_MAP) e' trattato come istanza.

        Ritorna {'assieme': bool, 'occorrenze': [(body_id, M4x4)], 'qty': {body_id: n}}.
        """
        corpi = set(self.corpi(includi_superfici=True))
        pd_tipi = ('PRODUCT_DEFINITION', 'PRODUCT_DEFINITION_WITH_ASSOCIATED_DOCUMENTS')
        pds = {}          # definizione (PD o NAUO) -> [PRODUCT_DEFINITION_SHAPE]
        sdr = {}          # PDS -> [rep]
        srr = {}          # rep -> {rep} (equivalenze)
        nauo = []         # (id, padre, figlio)
        cdsr = {}         # nauo -> rrwt
        pd_ids = []
        for e in self.ent:
            t = self.tipo(e)
            if t in pd_tipi:
                pd_ids.append(e)
            elif t == 'NEXT_ASSEMBLY_USAGE_OCCURRENCE':
                r = self.refs(e)
                if len(r) >= 2:
                    nauo.append((e, r[0], r[1]))
            elif t == 'PRODUCT_DEFINITION_SHAPE':
                r = self.refs(e)
                if r:
                    pds.setdefault(r[-1], []).append(e)
            elif t == 'SHAPE_DEFINITION_REPRESENTATION':
                r = self.refs(e)
                if len(r) >= 2:
                    sdr.setdefault(r[0], []).append(r[1])
            elif t in ('SHAPE_REPRESENTATION_RELATIONSHIP', 'REPRESENTATION_RELATIONSHIP'):
                r = self.refs(e)
                if len(r) >= 2:
                    srr.setdefault(r[0], set()).add(r[1])
                    srr.setdefault(r[1], set()).add(r[0])
        for e in self.ent:
            if self.tipo(e) == 'CONTEXT_DEPENDENT_SHAPE_REPRESENTATION':
                r = self.refs(e)
                if len(r) >= 2:
                    d = self.refs(r[1])
                    if d:
                        cdsr[d[-1]] = r[0]

        def _reps_pd(pd):
            out = []
            for s in pds.get(pd, []):
                out.extend(sdr.get(s, []))
            # chiusura sulle equivalenze
            vis = set()
            coda = list(out)
            while coda:
                x = coda.pop()
                if x in vis:
                    continue
                vis.add(x)
                coda.extend(srr.get(x, ()))
            return vis

        def _items(rep):
            r = self.refs(rep)
            return r[:-1] if len(r) > 1 else r  # ultimo ref = contesto

        def _placement_trasf(nid, reps_padre, reps_figlio):
            rr = cdsr.get(nid)
            if rr is None:
                return MAT_ID
            r = self.refs(rr)
            idt = next((x for x in r if self.tipo(x) == 'ITEM_DEFINED_TRANSFORMATION'), None)
            if idt is None or len(r) < 2:
                return MAT_ID
            rep1, rep2 = r[0], r[1]
            it = self.refs(idt)
            if len(it) < 2:
                return MAT_ID
            i1, i2 = it[0], it[1]
            # quale rep e' del figlio? (per appartenenza, poi convenzione ISO)
            figlio_e_1 = True
            if rep1 in reps_figlio or rep2 in reps_padre:
                figlio_e_1 = True
            elif rep2 in reps_figlio or rep1 in reps_padre:
                figlio_e_1 = False
            item_f, item_p = (i1, i2) if figlio_e_1 else (i2, i1)
            # se gli item stanno chiaramente nell'altra rep, scambia
            items_f = set()
            for x in reps_figlio:
                items_f.update(_items(x))
            if item_p in items_f and item_f not in items_f:
                item_f, item_p = item_p, item_f
            return mat_mul(self.matrice_placement(item_p),
                           mat_inv_rigida(self.matrice_placement(item_f)))

        figli = {}
        figli_ids = set()
        for nid, p, c in nauo:
            figli.setdefault(p, []).append((nid, c))
            figli_ids.add(c)

        occorrenze = []
        raggiunti = set()

        def _visita_reps(reps, T, depth):
            if depth > 40:
                return
            for rep in reps:
                for it in _items(rep):
                    ti = self.tipo(it)
                    if it in corpi:
                        occorrenze.append((it, T))
                        raggiunti.add(it)
                    elif ti == 'MAPPED_ITEM':
                        mr = self.refs(it)
                        if len(mr) >= 2:
                            rm = self.refs(mr[0])  # REPRESENTATION_MAP(origine, rep)
                            if len(rm) >= 2:
                                Tm = mat_mul(self.matrice_placement(mr[1]),
                                             mat_inv_rigida(self.matrice_placement(rm[0])))
                                sub = {rm[1]} | srr.get(rm[1], set())
                                _visita_reps(sub, mat_mul(T, Tm), depth + 1)

        def _visita_pd(pd, T, depth, stack):
            if depth > 40 or pd in stack:
                return
            reps = _reps_pd(pd)
            _visita_reps(reps, T, depth)
            for nid, c in figli.get(pd, []):
                Tc = _placement_trasf(nid, reps, _reps_pd(c))
                _visita_pd(c, mat_mul(T, Tc), depth + 1, stack | {pd})

        radici = [pd for pd in pd_ids if pd not in figli_ids]
        for pd in radici:
            _visita_pd(pd, MAT_ID, 0, frozenset())
        # corpi non raggiunti dall'albero (file senza struttura prodotto): qty 1
        for b in self.corpi(includi_superfici=True):
            if b not in raggiunti:
                occorrenze.append((b, MAT_ID))
        qty = {}
        for b, _ in occorrenze:
            qty[b] = qty.get(b, 0) + 1
        return {'assieme': bool(nauo), 'occorrenze': occorrenze, 'qty': qty}


def parse_step_geometry(step_path: str) -> dict:
    """Parse STEP file and extract 3D geometry for visualization.

    Extracts edge wireframes (LINE + CIRCLE interpolated) and face polygons
    (from ADVANCED_FACE edge loops) for solid-like rendering.

    Args:
        step_path: Path to the STEP file.

    Returns:
        dict with keys:
            'bodies': list of dicts, each with:
                'segments': list of [(x,y,z), ...] polylines (edges)
                'faces': list of [(x,y,z), ...] polygon vertex lists
            'weld_edges': list of [(x,y,z), ...] polylines for contact edges
            'n_corpi': int
            'errore': str or None
    """
    try:
        # Tokenizer condiviso: rispetta le stringhe, entita' complesse, unita' -> mm
        entities, _info_unita = carica_entita(step_path)
    except (IOError, OSError) as e:
        return {'bodies': [], 'weld_edges': [], 'n_corpi': 0, 'errore': str(e)}

    _etype = tipo_entita

    def _refs(val):
        return [int(x) for x in re.findall(r'#(\d+)', val)]

    def _coords(val):
        nums = re.findall(r'([-+]?\d+\.?\d*(?:[eE][-+]?\d+)?)', val)
        floats = [float(x) for x in nums]
        return tuple(floats[-3:]) if len(floats) >= 3 else None

    def _vec_sub(a, b):
        return (a[0]-b[0], a[1]-b[1], a[2]-b[2])

    def _vec_dot(a, b):
        return a[0]*b[0] + a[1]*b[1] + a[2]*b[2]

    def _vec_cross(a, b):
        return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])

    def _vec_norm(a):
        ln = math.sqrt(a[0]**2 + a[1]**2 + a[2]**2)
        return (a[0]/ln, a[1]/ln, a[2]/ln) if ln > 1e-12 else (0,0,0)

    def _interpolate_arc(center, axis_normal, radius, p1, p2, n_pts=24):
        """Interpolate circular arc from p1 to p2 around center.
        Returns list of 3D points on the arc."""
        ax = _vec_norm(axis_normal)
        # Build local coordinate system on the circle plane
        v1 = _vec_sub(p1, center)
        v1_len = math.sqrt(_vec_dot(v1, v1))
        if v1_len < 1e-12:
            return [p1, p2]
        u = _vec_norm(v1)
        v = _vec_cross(ax, u)
        v = _vec_norm(v)

        # Angles of p1 and p2 in local coords
        d2 = _vec_sub(p2, center)
        angle1 = 0.0  # p1 is at angle 0 by construction
        angle2 = math.atan2(_vec_dot(d2, v), _vec_dot(d2, u))
        if angle2 <= 1e-9:
            angle2 += 2 * math.pi  # Always go CCW

        points = []
        for i in range(n_pts + 1):
            t = angle1 + (angle2 - angle1) * i / n_pts
            cos_t = math.cos(t)
            sin_t = math.sin(t)
            px = center[0] + radius * (cos_t * u[0] + sin_t * v[0])
            py = center[1] + radius * (cos_t * u[1] + sin_t * v[1])
            pz = center[2] + radius * (cos_t * u[2] + sin_t * v[2])
            points.append((px, py, pz))
        return points

    # Find solid bodies (MANIFOLD_SOLID_BREP)
    body_ids = [eid for eid, val in entities.items()
                if _etype(val) == 'MANIFOLD_SOLID_BREP']

    # Also find CLOSED_SHELL not referenced by any MANIFOLD_SOLID_BREP
    # Some exporters (SolidWorks sheet metal) use CLOSED_SHELL directly
    msb_child_shells = set()
    for bid in body_ids:
        for r in _refs(entities.get(bid, '')):
            msb_child_shells.add(r)
    for eid, val in entities.items():
        if _etype(val) == 'CLOSED_SHELL' and eid not in msb_child_shells:
            body_ids.append(eid)
    # Also SHELL_BASED_SURFACE_MODEL (open shells used for thin bodies)
    for eid, val in entities.items():
        if _etype(val) == 'SHELL_BASED_SURFACE_MODEL':
            body_ids.append(eid)

    if not body_ids:
        return {'bodies': [], 'weld_edges': [], 'n_corpi': 0,
                'errore': 'Nessun corpo solido trovato'}

    def _get_face_refs(bid):
        """Get ADVANCED_FACE refs from a body, handling different entity types."""
        val = entities.get(bid, '')
        etype = _etype(val)
        refs_list = _refs(val)
        if not refs_list:
            return []
        if etype == 'MANIFOLD_SOLID_BREP':
            # MANIFOLD_SOLID_BREP -> CLOSED_SHELL -> faces
            cs_ref = refs_list[-1]
            cs_val = entities.get(cs_ref, '')
            return _refs(cs_val)
        elif etype in ('CLOSED_SHELL', 'OPEN_SHELL'):
            # Direct shell -> faces are direct children
            return refs_list
        elif etype == 'SHELL_BASED_SURFACE_MODEL':
            # SHELL_BASED_SURFACE_MODEL -> shells -> faces
            all_faces = []
            for sr in refs_list:
                sv = entities.get(sr, '')
                if _etype(sv) in ('CLOSED_SHELL', 'OPEN_SHELL'):
                    all_faces.extend(_refs(sv))
            return all_faces
        else:
            # Fallback: try child -> grandchild
            cs_ref = refs_list[-1]
            cs_val = entities.get(cs_ref, '')
            return _refs(cs_val)

    def _get_body_edges(bid):
        """Extract edges from a body, returning raw edge data with curve info."""
        face_refs = _get_face_refs(bid)
        edges = []
        seen_ec = set()
        for fref in face_refs:
            fval = entities.get(fref, '')
            if _etype(fval) != 'ADVANCED_FACE':
                continue
            for bref in _refs(fval):
                bval = entities.get(bref, '')
                bt = _etype(bval)
                if bt not in ('FACE_OUTER_BOUND', 'FACE_BOUND'):
                    continue
                el_ref = _refs(bval)[0]
                el_val = entities.get(el_ref, '')
                if _etype(el_val) != 'EDGE_LOOP':
                    continue
                for oe_ref in _refs(el_val):
                    oe_val = entities.get(oe_ref, '')
                    if _etype(oe_val) != 'ORIENTED_EDGE':
                        continue
                    oe_refs = _refs(oe_val)
                    if not oe_refs:
                        continue
                    ec_ref = oe_refs[-1]
                    if ec_ref in seen_ec:
                        continue
                    ec_val = entities.get(ec_ref, '')
                    if _etype(ec_val) != 'EDGE_CURVE':
                        continue
                    refs2 = _refs(ec_val)
                    if len(refs2) < 3:
                        continue
                    v1_refs = _refs(entities.get(refs2[0], ''))
                    v2_refs = _refs(entities.get(refs2[1], ''))
                    p1 = _coords(entities.get(v1_refs[0], '')) if v1_refs else None
                    p2 = _coords(entities.get(v2_refs[0], '')) if v2_refs else None
                    if p1 and p2:
                        seen_ec.add(ec_ref)
                        curve_ref = refs2[2]
                        curve_val = entities.get(curve_ref, '')
                        curve_type = _etype(curve_val)
                        edges.append((ec_ref, p1, p2, curve_type, curve_ref))
        return edges

    def _edge_to_polyline(ec_ref, p1, p2, curve_type, curve_ref):
        """Convert an edge to a polyline (list of 3D points)."""
        if curve_type == 'CIRCLE':
            # CIRCLE('name', #axis2_placement, radius)
            cval = entities.get(curve_ref, '')
            crefs = _refs(cval)
            # Extract radius
            rnums = re.findall(r'([-+]?\d+\.?\d*(?:[eE][-+]?\d+)?)', cval)
            radius = float(rnums[-1]) if rnums else 0
            if crefs and radius > 0:
                # Get axis placement -> origin + axis direction
                ax_val = entities.get(crefs[0], '')
                ax_refs = _refs(ax_val)
                if len(ax_refs) >= 2:
                    center = _coords(entities.get(ax_refs[0], ''))
                    axis_dir = _coords(entities.get(ax_refs[1], ''))
                    if center and axis_dir:
                        return _interpolate_arc(center, axis_dir, radius, p1, p2, n_pts=24)
        # Fallback: straight line
        return [p1, p2]

    def _get_body_planes(bid):
        face_refs = _get_face_refs(bid)
        planes = []
        for fref in face_refs:
            fval = entities.get(fref, '')
            if _etype(fval) != 'ADVANCED_FACE':
                continue
            for sr in _refs(fval):
                sv = entities.get(sr, '')
                if _etype(sv) != 'PLANE':
                    continue
                ax_ref = _refs(sv)[0]
                ax_refs = _refs(entities.get(ax_ref, ''))
                if len(ax_refs) >= 2:
                    origin = _coords(entities.get(ax_refs[0], ''))
                    normal = _coords(entities.get(ax_refs[1], ''))
                    if origin and normal:
                        planes.append((origin, normal))
        return planes

    def _get_body_face_planes(bid):
        """Get PLANE faces with their finite bounding boxes (from edge loop vertices)."""
        face_refs = _get_face_refs(bid)
        face_planes = []
        for fref in face_refs:
            fval = entities.get(fref, '')
            if _etype(fval) != 'ADVANCED_FACE':
                continue
            # Find PLANE surface
            plane_origin = plane_normal = None
            for sr in _refs(fval):
                sv = entities.get(sr, '')
                if _etype(sv) == 'PLANE':
                    ax_ref = _refs(sv)[0]
                    ax_refs = _refs(entities.get(ax_ref, ''))
                    if len(ax_refs) >= 2:
                        plane_origin = _coords(entities.get(ax_refs[0], ''))
                        plane_normal = _coords(entities.get(ax_refs[1], ''))
                    break
            if not plane_origin or not plane_normal:
                continue
            # Collect all vertex points from this face's edge loops
            face_pts = []
            for bref in _refs(fval):
                bval = entities.get(bref, '')
                if _etype(bval) not in ('FACE_OUTER_BOUND', 'FACE_BOUND'):
                    continue
                brefs = _refs(bval)
                if not brefs:
                    continue
                el_val = entities.get(brefs[0], '')
                if _etype(el_val) != 'EDGE_LOOP':
                    continue
                for oe_ref in _refs(el_val):
                    oe_val = entities.get(oe_ref, '')
                    if _etype(oe_val) != 'ORIENTED_EDGE':
                        continue
                    oe_refs = _refs(oe_val)
                    if not oe_refs:
                        continue
                    ec_val = entities.get(oe_refs[-1], '')
                    if _etype(ec_val) != 'EDGE_CURVE':
                        continue
                    refs2 = _refs(ec_val)
                    if len(refs2) < 2:
                        continue
                    for vref_idx in (0, 1):
                        v_refs = _refs(entities.get(refs2[vref_idx], ''))
                        if v_refs:
                            pt = _coords(entities.get(v_refs[0], ''))
                            if pt:
                                face_pts.append(pt)
            if not face_pts:
                continue
            fmin = tuple(min(p[k] for p in face_pts) for k in range(3))
            fmax = tuple(max(p[k] for p in face_pts) for k in range(3))
            face_planes.append((plane_origin, plane_normal, fmin, fmax))
        return face_planes

    def _get_body_faces(bid):
        """Extract face polygons from a body for solid rendering.
        Returns list of vertex-lists (one per face outer boundary)."""
        face_refs = _get_face_refs(bid)
        faces = []
        for fref in face_refs:
            fval = entities.get(fref, '')
            if _etype(fval) != 'ADVANCED_FACE':
                continue
            # Get outer bound vertices (ordered loop)
            for bref in _refs(fval):
                bval = entities.get(bref, '')
                if _etype(bval) != 'FACE_OUTER_BOUND':
                    continue
                el_ref = _refs(bval)[0]
                el_val = entities.get(el_ref, '')
                if _etype(el_val) != 'EDGE_LOOP':
                    continue
                # Collect ordered polyline from edge loop
                face_pts = []
                for oe_ref in _refs(el_val):
                    oe_val = entities.get(oe_ref, '')
                    if _etype(oe_val) != 'ORIENTED_EDGE':
                        continue
                    oe_refs = _refs(oe_val)
                    if not oe_refs:
                        continue
                    ec_ref = oe_refs[-1]
                    ec_val = entities.get(ec_ref, '')
                    if _etype(ec_val) != 'EDGE_CURVE':
                        continue
                    refs2 = _refs(ec_val)
                    if len(refs2) < 3:
                        continue
                    v1_refs = _refs(entities.get(refs2[0], ''))
                    v2_refs = _refs(entities.get(refs2[1], ''))
                    p1 = _coords(entities.get(v1_refs[0], '')) if v1_refs else None
                    p2 = _coords(entities.get(v2_refs[0], '')) if v2_refs else None
                    if not p1 or not p2:
                        continue
                    curve_ref = refs2[2]
                    curve_val = entities.get(curve_ref, '')
                    curve_type = _etype(curve_val)
                    poly = _edge_to_polyline(ec_ref, p1, p2, curve_type, curve_ref)
                    # Check orientation flag (.T. or .F.)
                    orient_true = '.T.' in oe_val
                    if not orient_true:
                        poly = list(reversed(poly))
                    # Use geometric connectivity to ensure correct stitching
                    if face_pts and poly:
                        last_pt = face_pts[-1]
                        d_start = sum((a - b) ** 2 for a, b in zip(last_pt, poly[0]))
                        d_end = sum((a - b) ** 2 for a, b in zip(last_pt, poly[-1]))
                        if d_end < d_start:
                            poly = list(reversed(poly))
                        face_pts.extend(poly[1:])
                    else:
                        face_pts.extend(poly)
                if len(face_pts) >= 3:
                    faces.append(face_pts)
        return faces

    # Build body data with bounding box
    body_info = {}
    for bid in body_ids:
        raw_edges = _get_body_edges(bid)
        planes = _get_body_planes(bid)
        faces = _get_body_faces(bid)

        segments = []
        total_len = 0
        all_pts = []
        for ec_ref, p1, p2, ctype, cref in raw_edges:
            polyline = _edge_to_polyline(ec_ref, p1, p2, ctype, cref)
            segments.append(polyline)
            total_len += math.sqrt(sum((a - b) ** 2 for a, b in zip(p1, p2)))
            all_pts.extend([p1, p2])

        # Bounding box
        if all_pts:
            bbox_min = (min(p[0] for p in all_pts), min(p[1] for p in all_pts), min(p[2] for p in all_pts))
            bbox_max = (max(p[0] for p in all_pts), max(p[1] for p in all_pts), max(p[2] for p in all_pts))
        else:
            bbox_min = bbox_max = (0, 0, 0)

        body_info[bid] = {
            'segments': segments, 'faces': faces, 'raw_edges': raw_edges,
            'planes': planes, 'total_len': total_len,
            'bbox_min': bbox_min, 'bbox_max': bbox_max
        }

    # --- Merge overlapping bodies (SolidWorks splits parts into sub-bodies) ---
    # Bodies with nearly identical bounding boxes are the same physical part.
    BB_TOL = 2.0  # mm tolerance for bbox overlap detection
    def _bbox_similar(b1, b2):
        mn1, mx1 = body_info[b1]['bbox_min'], body_info[b1]['bbox_max']
        mn2, mx2 = body_info[b2]['bbox_min'], body_info[b2]['bbox_max']
        return all(abs(a - b) < BB_TOL for a, b in zip(mn1, mn2)) and \
               all(abs(a - b) < BB_TOL for a, b in zip(mx1, mx2))

    # Group bodies into physical parts
    merged_groups = []  # Each group = list of body_ids
    assigned = set()
    for bid in body_ids:
        if bid in assigned:
            continue
        group = [bid]
        assigned.add(bid)
        for bid2 in body_ids:
            if bid2 not in assigned and _bbox_similar(bid, bid2):
                group.append(bid2)
                assigned.add(bid2)
        merged_groups.append(group)

    # Build visual bodies by merging segments and faces of overlapping bodies
    bodies = []
    merged_body_ids = []  # Representative ID per merged group
    for group in merged_groups:
        merged_segments = []
        merged_faces = []
        for bid in group:
            merged_segments.extend(body_info[bid]['segments'])
            merged_faces.extend(body_info[bid]['faces'])
        bodies.append({'segments': merged_segments, 'faces': merged_faces, 'id': group[0]})
        merged_body_ids.append(group[0])

    # --- Find weld (contact) edges between DISTINCT physical parts ---
    TOL = 0.5  # mm
    MIN_WELD_LEN = 5.0  # mm -- ignore very short contact edges (fillets, chamfers)
    PROX_TOL = 3.0  # mm -- proximity tolerance for bbox overlap check

    def _on_plane(point, origin, normal):
        d = sum((p - o) * n for p, o, n in zip(point, origin, normal))
        return abs(d) < TOL

    def _group_bbox(group):
        """Compute combined bounding box of a merged body group."""
        all_min = [float('inf')] * 3
        all_max = [float('-inf')] * 3
        for bid in group:
            bmin = body_info[bid]['bbox_min']
            bmax = body_info[bid]['bbox_max']
            for k in range(3):
                all_min[k] = min(all_min[k], bmin[k])
                all_max[k] = max(all_max[k], bmax[k])
        return tuple(all_min), tuple(all_max)

    def _bbox_overlap(min1, max1, min2, max2, tol):
        """Compute the overlap zone of two bounding boxes (expanded by tol).
        Returns (overlap_min, overlap_max) or None if no overlap."""
        o_min = [max(min1[k], min2[k]) - tol for k in range(3)]
        o_max = [min(max1[k], max2[k]) + tol for k in range(3)]
        if all(o_min[k] <= o_max[k] for k in range(3)):
            return tuple(o_min), tuple(o_max)
        return None

    def _edge_in_box(p1, p2, box_min, box_max):
        """Check if BOTH endpoints of an edge are within a bounding box."""
        for pt in (p1, p2):
            if not all(box_min[k] <= pt[k] <= box_max[k] for k in range(3)):
                return False
        return True

    def _edge_key(p1, p2, precision=1.0):
        """Create a position-based key for deduplication.
        Rounds coordinates to `precision` mm and sorts endpoints."""
        def _round(pt):
            return tuple(round(c / precision) for c in pt)
        a, b = _round(p1), _round(p2)
        return (min(a, b), max(a, b))

    weld_edges = []
    seen_positions = set()  # Deduplicate by position across ALL body pairs

    for i, group1 in enumerate(merged_groups):
        bbox1_min, bbox1_max = _group_bbox(group1)
        for group2 in merged_groups[i + 1:]:
            bbox2_min, bbox2_max = _group_bbox(group2)

            # Only check pairs whose bounding boxes overlap (proximity check)
            overlap = _bbox_overlap(bbox1_min, bbox1_max, bbox2_min, bbox2_max, PROX_TOL)
            if not overlap:
                continue
            overlap_min, overlap_max = overlap

            all_edges_1 = []
            for bid in group1:
                all_edges_1.extend(body_info[bid]['raw_edges'])

            all_edges_2 = []
            for bid in group2:
                all_edges_2.extend(body_info[bid]['raw_edges'])

            # Get face-bounded planes (with face bbox) for each group
            face_planes_1 = []
            for bid in group1:
                face_planes_1.extend(_get_body_face_planes(bid))
            face_planes_2 = []
            for bid in group2:
                face_planes_2.extend(_get_body_face_planes(bid))

            # Check BOTH directions: edges of A on faces of B, AND edges of B on faces of A
            FACE_TOL = 2.0  # mm tolerance for face bbox check
            check_pairs = [
                (all_edges_1, face_planes_2, bbox1_min, bbox1_max),
                (all_edges_2, face_planes_1, bbox2_min, bbox2_max),
            ]

            for small_edges, large_face_planes, src_bmin, src_bmax in check_pairs:
                seen_ec = set()
                for ec_ref, p1, p2, curve_type, cref in small_edges:
                    if curve_type != 'LINE':
                        continue
                    edge_len = math.sqrt(sum((a - b) ** 2 for a, b in zip(p1, p2)))
                    if edge_len < MIN_WELD_LEN:
                        continue
                    # Edge must be within the overlap zone of the two bodies
                    if not _edge_in_box(p1, p2, overlap_min, overlap_max):
                        continue
                    # Filter INNER edges: at least one coordinate of each endpoint
                    # must be at the boundary of the SOURCE body's bbox.
                    # Inner edges (inside tube wall) are not weldable.
                    BOUNDARY_TOL = 1.0
                    is_boundary = False
                    for pt in (p1, p2):
                        for k in range(3):
                            if (abs(pt[k] - src_bmin[k]) < BOUNDARY_TOL or
                                    abs(pt[k] - src_bmax[k]) < BOUNDARY_TOL):
                                is_boundary = True
                                break
                        if is_boundary:
                            break
                    if not is_boundary:
                        continue
                    for origin, normal, fmin, fmax in large_face_planes:
                        if _on_plane(p1, origin, normal) and _on_plane(p2, origin, normal):
                            # BOTH endpoints must be within the FACE's bounding box
                            if (all(fmin[k] - FACE_TOL <= p1[k] <= fmax[k] + FACE_TOL
                                    for k in range(3)) and
                                all(fmin[k] - FACE_TOL <= p2[k] <= fmax[k] + FACE_TOL
                                    for k in range(3))):
                                if ec_ref not in seen_ec:
                                    seen_ec.add(ec_ref)
                                    ekey = _edge_key(p1, p2)
                                    if ekey not in seen_positions:
                                        seen_positions.add(ekey)
                                        weld_edges.append([p1, p2])
                            break

    return {
        'bodies': bodies,
        'weld_edges': weld_edges,
        'n_corpi': len(merged_groups),
        'errore': None
    }
