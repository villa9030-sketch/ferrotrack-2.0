"""Il giro ufficio <-> laser: quello che va avanti e quello che torna indietro.

Verifica del 13/10/2026 (giro completo con i dati veri, su una copia):

 1. "Forse finiti": quando il laser preme "Taglio fatto" l'ufficio non
    sapeva niente. Ora l'ordine tagliato e senza altro in sospeso compare come
    PROPOSTA nell'ufficio; "Finito" lo porta a "DDT da fare" (la stessa
    /completamento), "Non ancora" lo rimanda di due giorni (nello storico).
    Non compare se: taglio non fatto, da procurare non arrivato, lavorazione
    esterna, gia' completato, eliminato.

Gira su DATABASE TEMPORANEO, impostazioni temporanee, LANTEK SIMULATO.
Esecuzione: python app/tests/test_flusso_ufficio_laser.py
"""
import json
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
os.environ['FERROTRACK_ESEMPI_DIR'] = tempfile.mkdtemp(prefix='ft_fl_esempi_')
_CFG = os.path.join(tempfile.mkdtemp(prefix='ft_fl_cfg_'), 'app_config.json')
with open(_CFG, 'w', encoding='utf-8') as f:
    json.dump({'laser_calendario': {'giorni': [1, 2, 3, 4, 5], 'eccezioni': {}}}, f)
os.environ['FERROTRACK_CONFIG'] = _CFG

import werkzeug  # noqa: E402
if not hasattr(werkzeug, '__version__'):
    werkzeug.__version__ = '3.0.0'

from sqlalchemy import create_engine  # noqa: E402

from backend import models  # noqa: E402
from backend.models import Base, Order  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_fl_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'), connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

import importlib  # noqa: E402
A = importlib.import_module('backend.app')
from backend import lantek as L  # noqa: E402
from backend import ordini_service as OS  # noqa: E402
from backend.database import ConfigManager  # noqa: E402
from backend.preventivi import tempi_laser as TL  # noqa: E402

ConfigManager.load_config()
assert os.path.abspath(ConfigManager._CONFIG_PATH) == os.path.abspath(_CFG), 'impostazioni NON temporanee: fermo'
_CARTELLE = tempfile.mkdtemp(prefix='ft_fl_')
A.UPLOAD_FOLDER = os.path.join(_CARTELLE, 'uploads')
A.DRAWINGS_FOLDER = os.path.join(A.UPLOAD_FOLDER, 'drawings')
A.PDFS_FOLDER = os.path.join(A.UPLOAD_FOLDER, 'pdfs')
os.makedirs(A.DRAWINGS_FOLDER, exist_ok=True)
os.makedirs(A.PDFS_FOLDER, exist_ok=True)

TL._leggi_lantek = lambda codici: None
L.pezzi_in_lantek = lambda codici: {'disponibile': False, 'errore': 'prova', 'pezzi': {}}
L.importa_pezzi_dxf = lambda *a, **k: (_ for _ in ()).throw(AssertionError('Lantek non si lancia nei test'))
L.importa_xml = lambda *a, **k: (_ for _ in ()).throw(AssertionError('Lantek non si lancia nei test'))

from tests.accesso_aiuto import modalita, entra, ufficio_pronto  # noqa: E402

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


modalita('protetto')
UFF = ufficio_pronto(A.app)
LAS = entra(A.app, 'laser')


def nuovo_ordine(**k):
    oid = str(uuid.uuid4())
    s = models.SessionLocal()
    s.add(Order(id=oid, cliente=k.pop('cliente', 'DECA S.r.l.'), numero_ordine=k.pop('numero', 'FL-' + oid[:4]),
                data_consegna=datetime.utcnow() + timedelta(days=5), status='RICEVUTO', **k))
    s.commit(); s.close()
    return oid


def forse_finiti():
    print('1) Forse finiti: il ritorno dal laser all\'ufficio')
    tagliato = nuovo_ordine(taglio_richiesto=True)
    nontagliato = nuovo_ordine(taglio_richiesto=True)
    esterno = nuovo_ordine(taglio_richiesto=True, lavorazione_esterna=True)
    procura = nuovo_ordine(taglio_richiesto=True)
    for oid in (tagliato, esterno, procura):
        r = LAS.post(f'/api/orders/{oid}/mark-laser-done', json={})
        check(f'il laser segna "Taglio fatto" ({oid[:4]})', r.status_code == 200, r.get_json())
    from backend.models import DaProcurare
    s = models.SessionLocal()
    s.add(DaProcurare(order_id=procura, chiave='art:x', tipo='tubo', quantita=2, stato='ordinato'))
    s.commit(); s.close()

    r = UFF.get('/api/ordini/forse-finiti')
    ids = [x['id'] for x in (r.get_json() or {}).get('ordini', [])]
    check("l'ordine tagliato compare fra i forse finiti", tagliato in ids, ids)
    check('non tagliato: no', nontagliato not in ids)
    check('lavorazione esterna dichiarata: no', esterno not in ids)
    check('da procurare non ancora arrivato: no', procura not in ids)
    riga = next((x for x in r.get_json()['ordini'] if x['id'] == tagliato), {})
    check('dice quando e\' stato tagliato', bool(riga.get('tagliato_il')), riga)
    check('il laser non vede questa lista (e\' dell\'ufficio)', LAS.get('/api/ordini/forse-finiti').status_code in (401, 403))

    r = UFF.post(f'/api/ordini/{tagliato}/forse-finito/non-ancora', json={})
    check('"Non ancora": risposta 200', r.status_code == 200, r.get_json())
    ids = [x['id'] for x in UFF.get('/api/ordini/forse-finiti').get_json()['ordini']]
    check('"Non ancora": sparisce per ora', tagliato not in ids, ids)
    from backend.models import AuditLog
    s = models.SessionLocal()
    a = s.query(AuditLog).filter(AuditLog.action == 'FORSE_FINITO_NON_ANCORA', AuditLog.entity_id == tagliato).first()
    if a:
        a.timestamp = datetime.utcnow() - timedelta(days=OS.GIORNI_NON_ANCORA + 1)
        s.commit()
    s.close()
    check('"Non ancora" e\' nello storico', a is not None)
    ids = [x['id'] for x in UFF.get('/api/ordini/forse-finiti').get_json()['ordini']]
    check('passati i giorni, torna a chiederlo', tagliato in ids, ids)

    r = UFF.post(f'/api/ordini/{tagliato}/completamento', json={})
    check('"Finito": la stessa /completamento dell\'ufficio', r.status_code == 200, r.get_json())
    ids = [x['id'] for x in UFF.get('/api/ordini/forse-finiti').get_json()['ordini']]
    check('confermato: non e\' piu\' fra i forse finiti', tagliato not in ids)
    v = {o['id']: o for o in UFF.get('/api/ordini/viste').get_json()['ordini']}
    check('e nell\'ufficio e\' "DDT da fare"', v[tagliato]['passo'] == 'attesa_ddt', v[tagliato].get('passo'))

    print('   (regola pura)')

    class _O:
        is_deleted = False
        status = 'RICEVUTO'
        taglio_completato = True
        data_taglio_completato = datetime(2026, 10, 10, 10)
        lavorazione_esterna = None
        data_consegna_effettiva = None
        data_completamento_operativo = None
        ddt_numero = numero_ddt = None
    o = _O()
    check('regola: tagliato e niente in sospeso', OS.motivo_non_forse_finito(o) is None)
    check('regola: fase d\'officina aperta', OS.motivo_non_forse_finito(o, n_fasi_aperte=1) == "fase d'officina aperta")
    check('regola: "Non ancora" PRIMA del taglio non conta (ritagliato dopo)',
          OS.motivo_non_forse_finito(o, rimandato_il=datetime(2026, 10, 9), adesso=datetime(2026, 10, 10, 12)) is None)
    check('regola: "Non ancora" dopo il taglio, entro 2 giorni',
          OS.motivo_non_forse_finito(o, rimandato_il=datetime(2026, 10, 10, 11), adesso=datetime(2026, 10, 11)) == 'rimandato')


def main():
    forse_finiti()
    print(f'\n{OK} ok, {len(KO)} ko')
    if KO:
        print('FALLITI:', *KO, sep='\n  ')
    return 1 if KO else 0


if __name__ == '__main__':
    code = main()
    try:
        _ENG.dispose()
        os.remove(_TMP)
    except OSError:
        pass
    sys.exit(code)
