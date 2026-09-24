"""Advanced STEP file feature recognition for manufacturing analysis.

Detects manufacturing-relevant features from STEP geometry:
- Holes (through, blind, countersunk, tapped)
- Slots / cutouts (rectangular, oblong)
- Bends (sheet metal bending from 3D cylindrical surfaces)
- Notches and tabs
- Surface area, volume, weight estimation
- Manufacturing complexity scoring

Follows the same regex entity-parsing pattern as step_parser, step_tubolari,
step_piastre, and step_assieme.
"""

import logging
import math
import re
from typing import Any

from .step_parser import carica_entita, GeometriaStep, tipo_entita

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Standard metric thread core-drill diameters (ISO 261 coarse pitch)
# ---------------------------------------------------------------------------
THREAD_DIAMETERS_MM: dict[str, float] = {
    'M3': 2.5, 'M4': 3.3, 'M5': 4.2, 'M6': 5.0, 'M8': 6.8,
    'M10': 8.5, 'M12': 10.2, 'M14': 12.0, 'M16': 14.0,
    'M18': 15.5, 'M20': 17.5, 'M22': 19.5, 'M24': 21.0,
}

# ---------------------------------------------------------------------------
# Internal vector helpers (same style as other step_*.py modules)
# ---------------------------------------------------------------------------

def _vec_dot(a: tuple, b: tuple) -> float:
    return a[0]*b[0] + a[1]*b[1] + a[2]*b[2]

def _vec_sub(a: tuple, b: tuple) -> tuple:
    return (a[0]-b[0], a[1]-b[1], a[2]-b[2])

def _vec_cross(a: tuple, b: tuple) -> tuple:
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])

def _vec_len(a: tuple) -> float:
    return math.sqrt(a[0]**2 + a[1]**2 + a[2]**2)

def _vec_norm(a: tuple) -> tuple:
    ln = _vec_len(a)
    return (a[0]/ln, a[1]/ln, a[2]/ln) if ln > 1e-12 else (0.0, 0.0, 0.0)

def _vec_scale(a: tuple, s: float) -> tuple:
    return (a[0]*s, a[1]*s, a[2]*s)

def _vec_add(a: tuple, b: tuple) -> tuple:
    return (a[0]+b[0], a[1]+b[1], a[2]+b[2])


# ---------------------------------------------------------------------------
# STEP entity helpers (identical to other modules for consistency)
# ---------------------------------------------------------------------------

def _parse_entities(content: str) -> dict[int, str]:
    entities: dict[int, str] = {}
    for m in re.finditer(r'#(\d+)\s*=\s*(.+?)\s*;', content, re.DOTALL):
        entities[int(m.group(1))] = m.group(2).strip()
    return entities

def _etype(val: str) -> str:
    return tipo_entita(val)  # gestisce anche le entita' complesse "( A() B() )"

def _refs(val: str) -> list[int]:
    return [int(x) for x in re.findall(r'#(\d+)', val)]

def _coords(val: str) -> tuple | None:
    nums = re.findall(r'([-+]?\d+\.?\d*(?:[eE][-+]?\d+)?)', val)
    floats = [float(x) for x in nums]
    return tuple(floats[-3:]) if len(floats) >= 3 else None

def _last_float(val: str) -> float:
    nums = re.findall(r'([-+]?\d+\.?\d*(?:[eE][-+]?\d+)?)', val)
    return float(nums[-1]) if nums else 0.0


# ---------------------------------------------------------------------------
# Body / face traversal helpers
# ---------------------------------------------------------------------------

def _get_body_ids(entities: dict[int, str]) -> list[int]:
    """Find all solid body entity IDs (MANIFOLD_SOLID_BREP + orphan shells)."""
    body_ids = [eid for eid, val in entities.items()
                if _etype(val) == 'MANIFOLD_SOLID_BREP']
    msb_child_shells: set[int] = set()
    for bid in body_ids:
        for r in _refs(entities.get(bid, '')):
            msb_child_shells.add(r)
    for eid, val in entities.items():
        if _etype(val) == 'CLOSED_SHELL' and eid not in msb_child_shells:
            body_ids.append(eid)
    for eid, val in entities.items():
        if _etype(val) == 'SHELL_BASED_SURFACE_MODEL':
            body_ids.append(eid)
    return body_ids


def _get_face_refs(entities: dict[int, str], bid: int) -> list[int]:
    """Get ADVANCED_FACE refs from a body."""
    val = entities.get(bid, '')
    et = _etype(val)
    refs_list = _refs(val)
    if not refs_list:
        return []
    if et == 'MANIFOLD_SOLID_BREP':
        return _refs(entities.get(refs_list[-1], ''))
    elif et in ('CLOSED_SHELL', 'OPEN_SHELL'):
        return refs_list
    elif et == 'SHELL_BASED_SURFACE_MODEL':
        faces: list[int] = []
        for sr in refs_list:
            sv = entities.get(sr, '')
            if _etype(sv) in ('CLOSED_SHELL', 'OPEN_SHELL'):
                faces.extend(_refs(sv))
        return faces
    else:
        return _refs(entities.get(refs_list[-1], ''))


def _get_surface_info(entities: dict[int, str], face_ref: int) -> tuple[str, dict]:
    """Return (surface_type, data_dict) for an ADVANCED_FACE.

    Recognised surface types: PLANE, CYL (CYLINDRICAL_SURFACE),
    CONE (CONICAL_SURFACE), TORUS (TOROIDAL_SURFACE), BSPLINE.
    """
    fval = entities.get(face_ref, '')
    if _etype(fval) != 'ADVANCED_FACE':
        return '', {}
    frefs = _refs(fval)
    if not frefs:
        return '', {}
    surf_ref = frefs[-1]
    surf_val = entities.get(surf_ref, '')
    surf_type = _etype(surf_val)

    # sense flag: .F. reverses normal
    face_sense_fwd = '.F.' not in fval

    if surf_type == 'CYLINDRICAL_SURFACE':
        radius = _last_float(surf_val)
        srefs = _refs(surf_val)
        axis_data: dict[str, Any] = {'raggio': radius, 'surf_ref': surf_ref}
        if srefs:
            ax_val = entities.get(srefs[0], '')
            ax_refs = _refs(ax_val)
            if len(ax_refs) >= 2:
                origin = _coords(entities.get(ax_refs[0], ''))
                axis_dir = _coords(entities.get(ax_refs[1], ''))
                if origin and axis_dir:
                    axis_data['origin'] = origin
                    axis_data['axis'] = _vec_norm(axis_dir)
        return 'CYL', axis_data

    elif surf_type == 'PLANE':
        srefs = _refs(surf_val)
        if srefs:
            ax_val = entities.get(srefs[0], '')
            ax_refs = _refs(ax_val)
            if len(ax_refs) >= 2:
                origin = _coords(entities.get(ax_refs[0], ''))
                normal = _coords(entities.get(ax_refs[1], ''))
                if origin and normal:
                    n = _vec_norm(normal)
                    if not face_sense_fwd:
                        n = (-n[0], -n[1], -n[2])
                    return 'PLANE', {'origin': origin, 'normal': n, 'surf_ref': surf_ref}
        return 'PLANE', {}

    elif surf_type == 'CONICAL_SURFACE':
        radius = _last_float(surf_val)
        srefs = _refs(surf_val)
        cone_data: dict[str, Any] = {'raggio': radius, 'surf_ref': surf_ref}
        if srefs:
            ax_val = entities.get(srefs[0], '')
            ax_refs = _refs(ax_val)
            if len(ax_refs) >= 2:
                origin = _coords(entities.get(ax_refs[0], ''))
                axis_dir = _coords(entities.get(ax_refs[1], ''))
                if origin and axis_dir:
                    cone_data['origin'] = origin
                    cone_data['axis'] = _vec_norm(axis_dir)
        return 'CONE', cone_data

    elif surf_type == 'TOROIDAL_SURFACE':
        return 'TORUS', {'surf_ref': surf_ref}
    elif surf_type == 'B_SPLINE_SURFACE_WITH_KNOTS':
        return 'BSPLINE', {'surf_ref': surf_ref}
    else:
        return surf_type, {}


def _get_edge_points(entities: dict[int, str], face_ref: int) -> list[tuple]:
    """Extract all vertex points from a face's edge loops."""
    fval = entities.get(face_ref, '')
    if _etype(fval) != 'ADVANCED_FACE':
        return []
    pts: list[tuple] = []
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
            ec_refs = _refs(ec_val)
            if len(ec_refs) < 2:
                continue
            for vref in ec_refs[:2]:
                vval = entities.get(vref, '')
                if _etype(vval) == 'VERTEX_POINT':
                    vrefs = _refs(vval)
                    if vrefs:
                        pt = _coords(entities.get(vrefs[0], ''))
                        if pt:
                            pts.append(pt)
    return pts


def _get_edges(entities: dict[int, str], face_refs: list[int]) -> list[tuple]:
    """Return list of (ec_ref, p1, p2, curve_type, curve_ref) for all edges."""
    edges: list[tuple] = []
    seen: set[int] = set()
    for fref in face_refs:
        fval = entities.get(fref, '')
        if _etype(fval) != 'ADVANCED_FACE':
            continue
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
                ec_ref = oe_refs[-1]
                if ec_ref in seen:
                    continue
                ec_val = entities.get(ec_ref, '')
                if _etype(ec_val) != 'EDGE_CURVE':
                    continue
                refs2 = _refs(ec_val)
                if len(refs2) < 3:
                    continue
                v1r = _refs(entities.get(refs2[0], ''))
                v2r = _refs(entities.get(refs2[1], ''))
                p1 = _coords(entities.get(v1r[0], '')) if v1r else None
                p2 = _coords(entities.get(v2r[0], '')) if v2r else None
                if p1 and p2:
                    seen.add(ec_ref)
                    curve_ref = refs2[2]
                    curve_val = entities.get(curve_ref, '')
                    curve_type = _etype(curve_val)
                    edges.append((ec_ref, p1, p2, curve_type, curve_ref))
    return edges


# ---------------------------------------------------------------------------
# Face-to-surface mapping: which ADVANCED_FACE uses which surface
# ---------------------------------------------------------------------------

def _build_face_surface_map(entities: dict[int, str], face_refs: list[int]) -> dict[int, tuple[str, dict, int]]:
    """Map face_ref -> (surf_type, surf_data, face_ref) for each face."""
    result: dict[int, tuple[str, dict, int]] = {}
    for fref in face_refs:
        stype, sdata = _get_surface_info(entities, fref)
        if stype:
            result[fref] = (stype, sdata, fref)
    return result


# ---------------------------------------------------------------------------
# Feature detection: HOLES
# ---------------------------------------------------------------------------

def _detect_holes(entities: dict[int, str], face_refs: list[int]) -> list[dict]:
    """Detect cylindrical holes in a body.

    Strategy:
    - Find CYLINDRICAL_SURFACE faces with small radius (< 50mm = likely a hole)
    - Determine axis origin & direction
    - Check if a PLANE face is normal to the cylinder axis at one end -> blind hole
    - Check if cylinder spans full thickness -> through-hole
    - Look for CONICAL_SURFACE on same axis -> countersunk
    - Match core-drill diameter to thread table -> tapped hole
    """
    face_map = _build_face_surface_map(entities, face_refs)

    # Collect all cylinder surfaces
    cylinders: list[tuple[int, dict]] = []
    for fref, (stype, sdata, _) in face_map.items():
        if stype == 'CYL' and sdata.get('raggio', 0) > 0:
            cylinders.append((fref, sdata))

    # Collect plane surfaces for reference
    planes: list[tuple[int, dict]] = []
    for fref, (stype, sdata, _) in face_map.items():
        if stype == 'PLANE' and 'normal' in sdata:
            planes.append((fref, sdata))

    # Collect cone surfaces for countersink detection
    cones: list[tuple[int, dict]] = []
    for fref, (stype, sdata, _) in face_map.items():
        if stype == 'CONE' and 'axis' in sdata:
            cones.append((fref, sdata))

    # Group cylinders by axis location (holes share the same axis)
    # A cylinder with radius < 50mm is likely a hole feature
    MAX_HOLE_RADIUS = 50.0
    hole_cyls = [(fref, sd) for fref, sd in cylinders
                 if sd['raggio'] < MAX_HOLE_RADIUS and 'axis' in sd and 'origin' in sd]

    # Group by coaxial axis: same axis direction + origin on same line
    axis_groups: list[list[tuple[int, dict]]] = []
    used: set[int] = set()
    for i, (fref_i, sd_i) in enumerate(hole_cyls):
        if i in used:
            continue
        group = [(fref_i, sd_i)]
        used.add(i)
        ax_i = sd_i['axis']
        orig_i = sd_i['origin']
        for j, (fref_j, sd_j) in enumerate(hole_cyls):
            if j in used:
                continue
            ax_j = sd_j['axis']
            orig_j = sd_j['origin']
            # Same axis direction (parallel)?
            if abs(_vec_dot(ax_i, ax_j)) < 0.95:
                continue
            # Origins on same line? Project difference onto axis
            diff = _vec_sub(orig_j, orig_i)
            along = _vec_dot(diff, ax_i)
            lateral_sq = _vec_dot(diff, diff) - along * along
            if lateral_sq < 4.0:  # < 2mm lateral offset
                group.append((fref_j, sd_j))
                used.add(j)
        axis_groups.append(group)

    holes: list[dict] = []
    for group in axis_groups:
        # The hole diameter is 2 * the smallest radius in the group
        # (inner wall; larger radius may be a countersink counterbore)
        radii = sorted(set(round(sd['raggio'], 3) for _, sd in group))
        r_hole = radii[0]
        diametro = round(r_hole * 2, 2)

        # Axis and position from first cylinder
        ax = group[0][1]['axis']
        origin = group[0][1]['origin']

        # Estimate depth from edge points along axis
        all_pts: list[tuple] = []
        for fref, sd in group:
            all_pts.extend(_get_edge_points(entities, fref))
        if not all_pts:
            continue

        projs = [_vec_dot(pt, ax) for pt in all_pts]
        depth = max(projs) - min(projs) if projs else 0.0

        # Check if through-hole: does the cylinder span between two
        # plane faces with normals parallel to the axis?
        cap_count = 0
        for _, pdata in planes:
            if abs(_vec_dot(pdata['normal'], ax)) > 0.9:
                # Check if plane is near the cylinder axis
                diff = _vec_sub(pdata['origin'], origin)
                lateral_sq = _vec_dot(diff, diff) - _vec_dot(diff, ax) ** 2
                if lateral_sq < (r_hole * 3) ** 2:
                    cap_count += 1
        # through-hole: cylinder between two large planes (no bottom cap face
        # belonging to the hole itself)
        passante = cap_count >= 2

        # Check countersunk: cone on same axis
        svasato = False
        for _, cdata in cones:
            if 'axis' not in cdata or 'origin' not in cdata:
                continue
            if abs(_vec_dot(cdata['axis'], ax)) < 0.9:
                continue
            diff_c = _vec_sub(cdata['origin'], origin)
            lat_sq = _vec_dot(diff_c, diff_c) - _vec_dot(diff_c, ax) ** 2
            if lat_sq < 4.0:
                svasato = True
                break

        # Check tapped: match core-drill diameter
        filettatura = _match_thread(diametro)

        tipo = 'passante'
        if filettatura:
            tipo = 'filettato'
        elif svasato:
            tipo = 'svasato'
        elif not passante:
            tipo = 'cieco'

        holes.append({
            'tipo': tipo,
            'diametro': diametro,
            'profondita': round(depth, 2) if depth > 0 else None,
            'posizione': origin,
            'passante': passante,
            'filettatura': filettatura,
        })

    return holes


# ---------------------------------------------------------------------------
# Feature detection: SLOTS / CUTOUTS
# ---------------------------------------------------------------------------

def _detect_slots(entities: dict[int, str], face_refs: list[int]) -> list[dict]:
    """Detect rectangular/oblong slots and cutouts.

    Strategy:
    - Find groups of PLANE faces forming a U-shaped pocket
    - Two parallel planes (sides) + one perpendicular plane (bottom)
    - Optional: two half-cylindrical ends (oblong slot)
    - Slot width < 100mm, depth < body thickness
    """
    face_map = _build_face_surface_map(entities, face_refs)

    planes_data: list[tuple[int, tuple, tuple]] = []
    for fref, (stype, sdata, _) in face_map.items():
        if stype == 'PLANE' and 'origin' in sdata and 'normal' in sdata:
            planes_data.append((fref, sdata['origin'], sdata['normal']))

    if len(planes_data) < 3:
        return []

    # Group planes by normal direction
    normal_groups: dict[int, list] = {}
    for fref, origin, normal in planes_data:
        matched = False
        for gk, glist in normal_groups.items():
            ref_n = glist[0][2]
            if abs(_vec_dot(normal, ref_n)) > 0.95:
                glist.append((fref, origin, normal))
                matched = True
                break
        if not matched:
            normal_groups[len(normal_groups)] = [(fref, origin, normal)]

    # Look for slot pattern: two small parallel planes (sides) close together
    # with a perpendicular plane between them (bottom)
    slots: list[dict] = []
    MAX_SLOT_WIDTH = 100.0

    for gk, gplanes in normal_groups.items():
        if len(gplanes) < 2:
            continue
        ref_n = _vec_norm(gplanes[0][2])
        # Sort by projection along normal
        proj_items = [(fref, origin, _vec_dot(origin, ref_n))
                      for fref, origin, _ in gplanes]
        proj_items.sort(key=lambda x: x[2])

        # Check consecutive pairs for slot-like gap
        for idx in range(len(proj_items) - 1):
            gap = proj_items[idx + 1][2] - proj_items[idx][2]
            if 1.0 < gap < MAX_SLOT_WIDTH:
                # Found two parallel planes with a slot-like gap
                fref_a = proj_items[idx][0]
                fref_b = proj_items[idx + 1][0]
                origin_a = proj_items[idx][1]

                # Get edge points to estimate slot length
                pts_a = _get_edge_points(entities, fref_a)
                pts_b = _get_edge_points(entities, fref_b)
                all_slot_pts = pts_a + pts_b

                if len(all_slot_pts) < 3:
                    continue

                # Calculate slot length along a direction perpendicular to slot normal
                # Find two orthogonal dirs to ref_n
                if abs(ref_n[0]) < 0.9:
                    u_base = (1, 0, 0)
                else:
                    u_base = (0, 1, 0)
                d = _vec_dot(u_base, ref_n)
                u = _vec_norm((u_base[0]-d*ref_n[0], u_base[1]-d*ref_n[1],
                               u_base[2]-d*ref_n[2]))
                v = _vec_norm(_vec_cross(ref_n, u))

                us = [_vec_dot(p, u) for p in all_slot_pts]
                vs = [_vec_dot(p, v) for p in all_slot_pts]
                span_u = max(us) - min(us) if us else 0
                span_v = max(vs) - min(vs) if vs else 0
                lunghezza = max(span_u, span_v)
                # Depth: estimate from points projected along the normal of the
                # body's main face (the direction perp to both slot normal and length)
                depth_dir = u if span_u < span_v else v
                ds = [_vec_dot(p, depth_dir) for p in all_slot_pts]
                profondita = max(ds) - min(ds) if ds else 0

                # Filter: a real slot has length >> width
                if lunghezza > gap * 1.2 and lunghezza > 5.0:
                    center_u = (min(us) + max(us)) / 2
                    center_v = (min(vs) + max(vs)) / 2
                    center_n = (proj_items[idx][2] + proj_items[idx + 1][2]) / 2
                    pos = _vec_add(_vec_add(_vec_scale(u, center_u),
                                           _vec_scale(v, center_v)),
                                  _vec_scale(ref_n, center_n))
                    slots.append({
                        'larghezza': round(gap, 2),
                        'lunghezza': round(lunghezza, 2),
                        'profondita': round(profondita, 2),
                        'posizione': pos,
                    })

    return slots


# ---------------------------------------------------------------------------
# Feature detection: BENDS (sheet metal)
# ---------------------------------------------------------------------------

def _detect_bends_3d(entities: dict[int, str], face_refs: list[int]) -> list[dict]:
    """Detect sheet metal bends from 3D geometry.

    Strategy:
    - Find CYLINDRICAL_SURFACE with small radius (1-15mm, typical bend radius)
    - Must be adjacent to two PLANE faces
    - The angle between the two planes = bend angle
    - The cylinder's edge span along the axis = bend length
    """
    face_map = _build_face_surface_map(entities, face_refs)

    # Collect small-radius cylinders (bend candidates)
    MIN_BEND_R = 0.5
    MAX_BEND_R = 15.0
    bend_cyls: list[tuple[int, dict]] = []
    for fref, (stype, sdata, _) in face_map.items():
        if stype == 'CYL':
            r = sdata.get('raggio', 0)
            if MIN_BEND_R <= r <= MAX_BEND_R and 'axis' in sdata and 'origin' in sdata:
                bend_cyls.append((fref, sdata))

    # Collect planes
    plane_faces: list[tuple[int, dict]] = []
    for fref, (stype, sdata, _) in face_map.items():
        if stype == 'PLANE' and 'normal' in sdata and 'origin' in sdata:
            plane_faces.append((fref, sdata))

    bends: list[dict] = []
    used_cyls: set[int] = set()

    for cyl_fref, cyl_data in bend_cyls:
        if cyl_fref in used_cyls:
            continue

        cyl_axis = cyl_data['axis']
        cyl_origin = cyl_data['origin']
        cyl_radius = cyl_data['raggio']

        # Find adjacent planes: normals perpendicular to cylinder axis
        # (bend connects two flat sheets that are NOT parallel to the axis)
        adjacent_planes: list[tuple[int, dict]] = []
        for pfref, pdata in plane_faces:
            pn = pdata['normal']
            # The plane normal should be perpendicular to the bend axis
            dot_axis = abs(_vec_dot(pn, cyl_axis))
            if dot_axis < 0.3:  # nearly perpendicular to axis
                # Check proximity: plane origin should be near the cylinder
                diff = _vec_sub(pdata['origin'], cyl_origin)
                dist = _vec_len(diff)
                if dist < cyl_radius * 20:  # within reasonable distance
                    adjacent_planes.append((pfref, pdata))

        if len(adjacent_planes) < 2:
            continue

        # Find best pair: the two planes whose normals make the bend angle
        best_angle = None
        best_pair = None
        for i in range(len(adjacent_planes)):
            for j in range(i + 1, len(adjacent_planes)):
                n1 = adjacent_planes[i][1]['normal']
                n2 = adjacent_planes[j][1]['normal']
                dot = _vec_dot(n1, n2)
                dot = max(-1.0, min(1.0, dot))
                angle_rad = math.acos(abs(dot))
                angle_deg = math.degrees(angle_rad)
                # Bends are typically 1-179 degrees
                if 1.0 < angle_deg < 179.0:
                    if best_angle is None or abs(angle_deg - 90) < abs(best_angle - 90):
                        best_angle = angle_deg
                        best_pair = (adjacent_planes[i], adjacent_planes[j])

        if best_angle is None or best_pair is None:
            continue

        # Estimate bend length from cylinder edge points along axis
        cyl_pts = _get_edge_points(entities, cyl_fref)
        if cyl_pts:
            projs = [_vec_dot(pt, cyl_axis) for pt in cyl_pts]
            bend_length = max(projs) - min(projs)
        else:
            bend_length = 0.0

        if bend_length < 1.0:
            continue

        # The actual bend angle between the two sheet faces
        n1 = best_pair[0][1]['normal']
        n2 = best_pair[1][1]['normal']
        dot = _vec_dot(n1, n2)
        dot = max(-1.0, min(1.0, dot))
        # Angle between normals: 180 - this = bend angle (supplementary)
        angle_between_normals = math.degrees(math.acos(dot))
        bend_angle = 180.0 - abs(angle_between_normals)

        used_cyls.add(cyl_fref)
        # Il cilindro interno (r) e quello esterno (r+t) sono la STESSA piega:
        # marca come usati i cilindri coassiali per non contarla due volte.
        ax_n = _vec_norm(cyl_axis)
        for other_fref, other_data in bend_cyls:
            if other_fref in used_cyls:
                continue
            if abs(_vec_dot(_vec_norm(other_data['axis']), ax_n)) < 0.999:
                continue
            d = _vec_sub(other_data['origin'], cyl_origin)
            off = _vec_sub(d, _vec_scale(ax_n, _vec_dot(d, ax_n)))
            if _vec_len(off) < 0.1:
                used_cyls.add(other_fref)
        bends.append({
            'angolo': round(bend_angle, 1),
            'lunghezza_mm': round(bend_length, 1),
            'raggio_mm': round(cyl_radius, 2),
        })

    return bends


# ---------------------------------------------------------------------------
# Feature detection: NOTCHES
# ---------------------------------------------------------------------------

def _detect_notches(entities: dict[int, str], face_refs: list[int]) -> list[dict]:
    """Detect edge notches and tabs.

    Strategy:
    - Find small rectangular pockets on edges of flat bodies.
    - Identified as groups of 3 small PLANE faces forming a U shape
      with width < 50mm and depth < 30mm.
    """
    face_map = _build_face_surface_map(entities, face_refs)

    planes_data: list[tuple[int, tuple, tuple]] = []
    for fref, (stype, sdata, _) in face_map.items():
        if stype == 'PLANE' and 'origin' in sdata and 'normal' in sdata:
            planes_data.append((fref, sdata['origin'], sdata['normal']))

    # Estimate body bounding box from all plane face edge points
    all_body_pts: list[tuple] = []
    for fref, _, _ in planes_data:
        all_body_pts.extend(_get_edge_points(entities, fref))

    if not all_body_pts:
        return []

    bbox_min = tuple(min(p[k] for p in all_body_pts) for k in range(3))
    bbox_max = tuple(max(p[k] for p in all_body_pts) for k in range(3))
    bbox_dims = tuple(bbox_max[k] - bbox_min[k] for k in range(3))

    MAX_NOTCH_WIDTH = 50.0
    MAX_NOTCH_DEPTH = 30.0

    # For each small plane face, check if it could be a notch bottom
    notches: list[dict] = []
    for fref, origin, normal in planes_data:
        pts = _get_edge_points(entities, fref)
        if len(pts) < 3:
            continue
        # Project points to get face dimensions
        if abs(normal[0]) < 0.9:
            u_base = (1, 0, 0)
        else:
            u_base = (0, 1, 0)
        d = _vec_dot(u_base, normal)
        u = _vec_norm((u_base[0]-d*normal[0], u_base[1]-d*normal[1],
                       u_base[2]-d*normal[2]))
        v = _vec_norm(_vec_cross(normal, u))
        us = [_vec_dot(p, u) for p in pts]
        vs = [_vec_dot(p, v) for p in pts]
        w = max(us) - min(us)
        h = max(vs) - min(vs)
        face_w = min(w, h)
        face_d = max(w, h)

        if face_w < 1.0 or face_d < 1.0:
            continue
        if face_w > MAX_NOTCH_WIDTH or face_d > MAX_NOTCH_DEPTH:
            continue

        # Check that this face is on the boundary of the body (edge notch)
        # At least one edge point should be on the body bounding box
        on_edge = False
        TOL = 1.0
        for pt in pts:
            for k in range(3):
                if abs(pt[k] - bbox_min[k]) < TOL or abs(pt[k] - bbox_max[k]) < TOL:
                    on_edge = True
                    break
            if on_edge:
                break

        if on_edge:
            notches.append({
                'larghezza': round(face_w, 2),
                'profondita': round(face_d, 2),
            })

    return notches


# ---------------------------------------------------------------------------
# Surface area estimation
# ---------------------------------------------------------------------------

def _calc_surface_area(entities: dict[int, str], face_refs: list[int]) -> float:
    """Estimate total surface area from all faces (mm^2).

    - PLANE: polygon area from edge loop vertices (Shoelace formula projected)
    - CYLINDRICAL_SURFACE: 2*pi*r * axial_span
    - Others: bounding-box approximation of face vertices
    """
    total_area = 0.0

    for fref in face_refs:
        stype, sdata = _get_surface_info(entities, fref)
        pts = _get_edge_points(entities, fref)

        if stype == 'PLANE' and 'normal' in sdata and len(pts) >= 3:
            normal = sdata['normal']
            # Build local 2D coords
            if abs(normal[0]) < 0.9:
                u_base = (1, 0, 0)
            else:
                u_base = (0, 1, 0)
            d = _vec_dot(u_base, normal)
            u = _vec_norm((u_base[0]-d*normal[0], u_base[1]-d*normal[1],
                           u_base[2]-d*normal[2]))
            v = _vec_norm(_vec_cross(normal, u))

            coords_2d = [(_vec_dot(p, u), _vec_dot(p, v)) for p in pts]
            # Remove near-duplicates to get ordered polygon
            unique: list[tuple[float, float]] = []
            for c in coords_2d:
                is_dup = False
                for uc in unique:
                    if abs(c[0]-uc[0]) < 0.01 and abs(c[1]-uc[1]) < 0.01:
                        is_dup = True
                        break
                if not is_dup:
                    unique.append(c)
            if len(unique) >= 3:
                # Shoelace formula
                area = 0.0
                n_u = len(unique)
                for i in range(n_u):
                    j = (i + 1) % n_u
                    area += unique[i][0] * unique[j][1]
                    area -= unique[j][0] * unique[i][1]
                total_area += abs(area) / 2.0
            else:
                # Fallback: bbox area
                us = [c[0] for c in coords_2d]
                vs = [c[1] for c in coords_2d]
                total_area += (max(us)-min(us)) * (max(vs)-min(vs))

        elif stype == 'CYL' and 'raggio' in sdata and 'axis' in sdata and pts:
            r = sdata['raggio']
            ax = sdata['axis']
            projs = [_vec_dot(pt, ax) for pt in pts]
            span = max(projs) - min(projs) if projs else 0.0
            # Approximate as full cylinder lateral area
            total_area += 2 * math.pi * r * span

        elif pts and len(pts) >= 3:
            # Generic approximation: bounding box of face points
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            zs = [p[2] for p in pts]
            dx = max(xs) - min(xs)
            dy = max(ys) - min(ys)
            dz = max(zs) - min(zs)
            dims = sorted([dx, dy, dz], reverse=True)
            total_area += dims[0] * dims[1]

    return total_area


# ---------------------------------------------------------------------------
# Volume and weight estimation
# ---------------------------------------------------------------------------

def _calc_volume_bbox(all_pts: list[tuple]) -> float:
    """Rough volume estimate from axis-aligned bounding box (mm^3)."""
    if not all_pts:
        return 0.0
    xs = [p[0] for p in all_pts]
    ys = [p[1] for p in all_pts]
    zs = [p[2] for p in all_pts]
    dx = max(xs) - min(xs)
    dy = max(ys) - min(ys)
    dz = max(zs) - min(zs)
    return dx * dy * dz


# ---------------------------------------------------------------------------
# Complexity scoring
# ---------------------------------------------------------------------------

def _calc_complexity(n_faces: int, n_holes: int, n_slots: int,
                     n_bends: int, n_bodies: int) -> int:
    """Manufacturing complexity score from 1 (trivial) to 10 (very complex).

    Weighted factors:
    - Geometry: number of faces
    - Drilling: number of holes
    - Milling: number of slots
    - Bending: number of bends
    - Multi-body: assembly complexity
    """
    score = 1.0

    # Faces contribution (logarithmic: 6 faces=1pt, 50 faces=3pt, 200+=5pt)
    if n_faces > 0:
        score += min(math.log(max(n_faces, 1)) / math.log(6), 5.0)

    # Holes: each hole adds 0.3, max 2.0
    score += min(n_holes * 0.3, 2.0)

    # Slots: each slot adds 0.5, max 1.5
    score += min(n_slots * 0.5, 1.5)

    # Bends: each bend adds 0.4, max 1.5
    score += min(n_bends * 0.4, 1.5)

    # Multi-body: each extra body adds 0.2
    if n_bodies > 1:
        score += min((n_bodies - 1) * 0.2, 1.0)

    return max(1, min(10, int(round(score))))


# ---------------------------------------------------------------------------
# Thread matching
# ---------------------------------------------------------------------------

def _match_thread(diameter_mm: float, tolerance: float = 0.3) -> str | None:
    """Match a hole diameter to a standard metric thread size.

    Compares against THREAD_DIAMETERS_MM (ISO 261 core-drill diameters).

    Args:
        diameter_mm: Measured hole diameter in mm.
        tolerance: Maximum deviation to accept a match (default 0.3mm).

    Returns:
        Thread designation string (e.g. 'M8') or None if no match.
    """
    best_name: str | None = None
    best_dist = tolerance + 1  # start above tolerance
    for name, core_d in THREAD_DIAMETERS_MM.items():
        dist = abs(diameter_mm - core_d)
        if dist < best_dist:
            best_dist = dist
            best_name = name
    return best_name if best_dist <= tolerance else None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def analizza_features(step_path: str) -> dict:
    """Comprehensive STEP feature analysis.

    Detects manufacturing-relevant features:
    - Holes (through-holes, blind holes, countersunk, tapped)
    - Slots / cutouts (rectangular, oblong)
    - Bends (sheet metal)
    - Notches and tabs
    - Surface area, volume, weight estimation
    - Manufacturing complexity score

    Args:
        step_path: Path to the STEP file.

    Returns:
        dict with keys:
            'fori', 'asole', 'pieghe_3d', 'tacche',
            'area_totale_mm2', 'volume_mm3', 'peso_stimato_kg',
            'bbox', 'complessita', 'n_facce', 'n_spigoli',
            'n_corpi', 'errore'
    """
    empty_result: dict[str, Any] = {
        'fori': [], 'asole': [], 'pieghe_3d': [], 'tacche': [],
        'area_totale_mm2': 0.0, 'volume_mm3': 0.0, 'peso_stimato_kg': 0.0,
        'bbox': {'min': (0, 0, 0), 'max': (0, 0, 0), 'dimensioni': (0, 0, 0)},
        'complessita': 1, 'n_facce': 0, 'n_spigoli': 0, 'n_corpi': 0,
        'errore': None,
    }

    try:
        with open(step_path, 'r', errors='replace') as f:
            content = f.read()
    except (IOError, OSError) as e:
        empty_result['errore'] = str(e)
        return empty_result

    # Tokenizer condiviso (stringhe, entita' complesse, unita' -> mm)
    try:
        entities, _ = carica_entita(step_path)
    except (IOError, OSError):
        entities = _parse_entities(content)
    body_ids = _get_body_ids(entities)

    if not body_ids:
        empty_result['errore'] = 'Nessun corpo solido trovato'
        return empty_result

    # Aggregate face refs and edges across all bodies
    all_face_refs: list[int] = []
    for bid in body_ids:
        all_face_refs.extend(_get_face_refs(entities, bid))

    # Count unique ADVANCED_FACE entities
    n_facce = sum(1 for fref in all_face_refs
                  if _etype(entities.get(fref, '')) == 'ADVANCED_FACE')

    # Get all edges
    all_edges = _get_edges(entities, all_face_refs)
    n_spigoli = len(all_edges)

    # Collect all vertex points for bbox
    all_pts: list[tuple] = []
    for _, p1, p2, _, _ in all_edges:
        all_pts.extend([p1, p2])

    # Bounding box
    if all_pts:
        bbox_min = tuple(min(p[k] for p in all_pts) for k in range(3))
        bbox_max = tuple(max(p[k] for p in all_pts) for k in range(3))
        bbox_dims = tuple(round(bbox_max[k] - bbox_min[k], 2) for k in range(3))
    else:
        bbox_min = bbox_max = (0.0, 0.0, 0.0)
        bbox_dims = (0.0, 0.0, 0.0)

    # --- Detect features ---
    try:
        fori = _detect_holes(entities, all_face_refs)
    except Exception as e:
        logger.warning("Errore rilevamento fori: %s", e)
        fori = []

    try:
        asole = _detect_slots(entities, all_face_refs)
    except Exception as e:
        logger.warning("Errore rilevamento asole: %s", e)
        asole = []

    try:
        pieghe = _detect_bends_3d(entities, all_face_refs)
    except Exception as e:
        logger.warning("Errore rilevamento pieghe: %s", e)
        pieghe = []

    try:
        tacche = _detect_notches(entities, all_face_refs)
    except Exception as e:
        logger.warning("Errore rilevamento tacche: %s", e)
        tacche = []

    # --- Surface area, volume, weight ---
    # Area e volume dalla geometria B-rep condivisa (anelli ORDINATI, fori
    # sottratti; volume col teorema della divergenza). Prima: shoelace su
    # vertici non ordinati e peso = bbox x 0.3.
    STEEL_DENSITY_KG_MM3 = 7.85e-6  # 7850 kg/m^3
    geo = GeometriaStep(entities)
    try:
        area_mm2 = sum(f['area'] for bid in body_ids for f in geo.facce(bid))
    except Exception as e:
        logger.warning("Errore calcolo area: %s", e)
        area_mm2 = _calc_surface_area(entities, all_face_refs)

    volumi = [geo.volume_corpo(bid) for bid in body_ids]
    if volumi and all(v is not None for v in volumi):
        volume_mm3 = sum(volumi)
        peso_kg = round(volume_mm3 * STEEL_DENSITY_KG_MM3, 3)
    else:
        # superfici non analitiche: stima grossolana bbox x fill factor 0.3
        volume_mm3 = _calc_volume_bbox(all_pts)
        peso_kg = round(volume_mm3 * 0.3 * STEEL_DENSITY_KG_MM3, 3)

    # --- Complexity ---
    n_corpi = len(body_ids)
    complessita = _calc_complexity(n_facce, len(fori), len(asole),
                                   len(pieghe), n_corpi)

    return {
        'fori': fori,
        'asole': asole,
        'pieghe_3d': pieghe,
        'tacche': tacche,
        'area_totale_mm2': round(area_mm2, 1),
        'volume_mm3': round(volume_mm3, 1),
        'peso_stimato_kg': peso_kg,
        'bbox': {'min': bbox_min, 'max': bbox_max, 'dimensioni': bbox_dims},
        'complessita': complessita,
        'n_facce': n_facce,
        'n_spigoli': n_spigoli,
        'n_corpi': n_corpi,
        'errore': None,
    }
