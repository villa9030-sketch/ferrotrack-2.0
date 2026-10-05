"""Avviso "molto sfrido": archi e anelli che usano poco del loro rettangolo.

47PA02031-00 (DECA): mezzo anello Ø1180×60, inox 2 mm, 1,69 kg. Il materiale
si conta sul netto (7,72 € con resa 0,7 e 3,20 €/kg) ma per tagliarlo serve un
rettangolo 1180×590: la stima avvisa coi due importi, il prezzo non cambia.
Esecuzione: python app/tests/test_molto_sfrido.py   (exit 0 = tutto ok)
"""
import os
import sys
import types

_APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _APP)
if 'backend' not in sys.modules:
    _pkg = types.ModuleType('backend')
    _pkg.__path__ = [os.path.join(_APP, 'backend')]
    sys.modules['backend'] = _pkg
from backend.preventivi import laser_cost_estimator as L  # noqa: E402

CFG = {'laser_config': dict(L.DEFAULT_LASER_CONFIG, resa_nesting=0.7,
                            materiali={'INOX_304': {'densita_kg_dm3': 8, 'euro_kg': 3.2}})}
ANELLO = {'area_dm2': 10.5545, 'perimetro_taglio_m': 3.6387, 'n_forature': 1,
          'spessore_mm': 2.0, 'materiale': 'INOX_304'}
KO = []


def check(nome, cond, extra=''):
    print(('  [OK] ' if cond else '  [KO] ') + nome + ('' if cond else f' {extra}'))
    if not cond:
        KO.append(nome)


s = L.stima_base(dict(ANELLO, bbox_w_mm=1180, bbox_h_mm=590), CFG)
av = [w for w in s['warnings'] if w.startswith('Molto sfrido')]
check('anello: avviso molto sfrido', len(av) == 1, s['warnings'])
check('anello: quota 15% e importi col rettangolo', av and '15%' in av[0] and '35,' in av[0] and '7,72' in av[0], av)
check('anello: materiale ancora sul netto (7,72 €)', abs(s['costo_materiale'] - 7.72) < 0.01, s['costo_materiale'])
check('anello: resa del pezzo restituita', abs(s['resa_pezzo'] - 0.152) < 0.002, s.get('resa_pezzo'))
s = L.stima_base(dict(ANELLO, bbox_w_mm=None, bbox_h_mm=None), CFG)
check('senza ingombro: nessun avviso', not any(w.startswith('Molto sfrido') for w in s['warnings']), s['warnings'])
s = L.stima_base(dict(ANELLO, area_dm2=6.0, bbox_w_mm=300, bbox_h_mm=250), CFG)
check('piastra piena (80%): nessun avviso', not any(w.startswith('Molto sfrido') for w in s['warnings']), s['warnings'])
s = L.stima_base(dict(ANELLO, bbox_w_mm=1180, bbox_h_mm=590, area_stimata_piega=True), CFG)
check('sviluppo stimato a mano: nessun confronto', not any(w.startswith('Molto sfrido') for w in s['warnings']))
print(f'\nRisultato: {7 - len(KO)} ok, {len(KO)} ko')
sys.exit(1 if KO else 0)
