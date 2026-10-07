"""Flask Backend per Schedulatore Laser"""
from flask import Flask, request, jsonify, send_file, send_from_directory, redirect
from werkzeug.exceptions import HTTPException
from flask_cors import CORS
from datetime import datetime, timedelta
import os
import sys
import re
import uuid
import logging
import threading
import time

logger = logging.getLogger(__name__)

# Importa moduli locali
from . import email_sender as _email_sender
from .models import (initialize_database, Order, OrderFile, get_session,
                     e_istanza_di_prova,
                     RUOLI_UFFICIO)
from .database import OrderManager, UserManager, AuditManager, ArchiveManager, FatturazioneManager, NotificationManager, AlertManager, KPIManager, ConfigManager, PreventivoManager
from .events import OrderEventBus
from .orario import iso_utc, iso_data, data_locale, oggi_locale, giorno_locale_in_utc
from urllib.parse import quote
from .preventivi import (
    xlsx_importer as _xlsx_importer,
    dxf_scanner as _dxf_scanner,
    laser_cost_estimator as _laser_estimator,
    step_assieme as _step_assieme,
    step_tubolari as _step_tubolari,
    step_piastre as _step_piastre,
    pdf_exporter as _pdf_exporter,
)
import json as _json_mod
# Carica DB profili tubolari una volta (file copiato dal Preventivatore desktop)
_PROFILI_TUBOLARI_DB = {}
try:
    _profili_path = os.path.join(os.path.dirname(__file__), 'preventivi', 'profili_tubolari.json')
    if os.path.exists(_profili_path):
        with open(_profili_path, 'r', encoding='utf-8') as _f:
            _PROFILI_TUBOLARI_DB = _json_mod.load(_f)
except Exception as _e:
    logger.warning('profili_tubolari.json non caricato: %s', _e)


def _find_oda_converter():
    """Cerca ODA File Converter installato sul sistema. Ritorna path .exe o None.
    Strategia: env var ODA_FC_PATH > glob su Program Files (versione qualsiasi).
    """
    import glob
    custom = os.environ.get('ODA_FC_PATH')
    if custom and os.path.exists(custom):
        return custom
    candidates = []
    for base in (r'C:\Program Files\ODA', r'C:\Program Files (x86)\ODA'):
        if os.path.isdir(base):
            candidates += glob.glob(os.path.join(base, 'ODAFileConverter*', 'ODAFileConverter.exe'))
    return candidates[-1] if candidates else None


def _convert_dwg_to_dxf(dwg_path):
    """Converte un .dwg in .dxf usando ODA File Converter (subprocess).

    Ritorna path del .dxf convertito, oppure dict {'error': ...} se conversione
    impossibile (ODA non installato, timeout, file corrotto).
    """
    import subprocess
    import shutil
    import tempfile
    import glob

    oda = _find_oda_converter()
    if not oda:
        return {'error': 'ODA_NOT_INSTALLED'}

    tmp_in = tempfile.mkdtemp(prefix='dwg_in_')
    tmp_out = tempfile.mkdtemp(prefix='dwg_out_')
    try:
        # ODA processa intere cartelle, non file singoli
        in_path = os.path.join(tmp_in, os.path.basename(dwg_path))
        shutil.copy2(dwg_path, in_path)
        # CLI: <input_dir> <output_dir> <out_ver> <out_format> <recurse> <audit>
        cmd = [oda, tmp_in, tmp_out, 'ACAD2018', 'DXF', '0', '1']
        proc = subprocess.run(cmd, capture_output=True, timeout=90)
        if proc.returncode != 0:
            return {'error': 'CONVERTER_FAILED', 'detail': proc.stderr.decode('utf-8', errors='ignore')[:200]}
        out_files = glob.glob(os.path.join(tmp_out, '*.dxf'))
        if not out_files:
            return {'error': 'NO_OUTPUT', 'detail': 'ODA non ha prodotto DXF'}
        # Sposta il DXF in un path che non scompare al cleanup di tmp_out
        out_path = os.path.join(UPLOAD_FOLDER, 'tmp_conv_' + uuid.uuid4().hex + '.dxf')
        shutil.copy2(out_files[0], out_path)
        return out_path
    except subprocess.TimeoutExpired:
        return {'error': 'TIMEOUT'}
    except Exception as e:
        return {'error': 'EXCEPTION', 'detail': str(e)}
    finally:
        shutil.rmtree(tmp_in, ignore_errors=True)
        shutil.rmtree(tmp_out, ignore_errors=True)

app = Flask(__name__, static_folder=None)

# Sottosistema DICHIARAZIONI ORE (blueprint isolato: la logica non sta qui).
# I suoi endpoint vogliono un dispositivo registrato (vedi backend/accesso.py).
from .api_ore import bp_ore  # noqa: E402
app.register_blueprint(bp_ore)
# Ingresso (dispositivo, PIN, amministratore) e gestione di persone e dispositivi.
from .api_accesso import bp_accesso  # noqa: E402
app.register_blueprint(bp_accesso)
# Il controllo unico degli accessi, prima di qualunque altra cosa.
from . import accesso  # noqa: E402
from .accesso import richiede, identita, ADMIN, PUBBLICO, UFFICI  # noqa: E402,F401
accesso.installa(app)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50 MB max upload
# Le pagine sono servite da questo stesso server: allo stesso indirizzo il
# browser non chiede permessi CORS. La regola serve solo a chi apre le pagine
# da un altro indirizzo della rete locale; prima le espressioni non erano
# ancorate in fondo e "http://192.168.1.10.evil.example" passava.
CORS(app, origins=[r"^http://localhost(:\d+)?$", r"^http://127\.0\.0\.1(:\d+)?$",
                   r"^https?://192\.168\.\d{1,3}\.\d{1,3}(:\d+)?$"])

# Gruppi di stazioni ricorrenti nelle regole delle rotte
OFFICINA_LETTURA = ('commerciale', 'ufficio', 'laser', 'reparto')   # ordini, disegni, foglio
CON_AVVISI = ('commerciale', 'ufficio', 'laser')                      # hanno la campanella

# Configurazioni
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), '..', 'uploads')
DRAWINGS_FOLDER = os.path.join(UPLOAD_FOLDER, 'drawings')
PDFS_FOLDER = os.path.join(UPLOAD_FOLDER, 'pdfs')
FRONTEND_FOLDER = os.path.join(os.path.dirname(__file__), '..', 'frontend')

os.makedirs(DRAWINGS_FOLDER, exist_ok=True)
os.makedirs(PDFS_FOLDER, exist_ok=True)

# Error handler globale — no stack trace nelle risposte
@app.errorhandler(HTTPException)
def handle_http_error(e):
    """Risposte HTTP normali: passano con il loro codice, non diventano 500.

    "Non trovato" e "metodo non ammesso" non sono guasti: trasformarli in 500
    riempiva i log di allarmi falsi e impediva al browser di capire che aveva
    solo sbagliato indirizzo. Alle chiamate API si risponde in JSON, perche'
    e' quello che il codice del frontend si aspetta di leggere.
    """
    if request.path.startswith('/api/'):
        codice, testo = e.code, e.description
        # Un indirizzo /api/ che non esiste (es. /api/scan, tolto con le
        # pistole) con un metodo diverso da GET finirebbe sulla pagina
        # "/<path:filename>", che accetta solo GET: la risposta sarebbe 405
        # "metodo non ammesso", come se l'indirizzo esistesse. E' un 404.
        if codice == 405:
            try:
                regola, _ = app.url_map.bind('').match(request.path, method='GET')
            except HTTPException:
                regola = None
            if regola == 'serve_frontend':
                codice, testo = 404, 'Indirizzo non trovato'
        return jsonify({'error': testo, 'codice': codice}), codice
    return e


@app.errorhandler(Exception)
def handle_unexpected_error(e):
    logging.error(f"Errore non gestito: {e}", exc_info=True)
    return jsonify({'error': 'Errore interno del server'}), 500

@app.errorhandler(413)
def handle_file_too_large(e):
    return jsonify({'error': 'File troppo grande (max 50MB)'}), 413

# Inizializza database.
# NOTA: avviene all'IMPORT del modulo (effetto collaterale storico). Resta il
# comportamento di default per non cambiare gli avvii esistenti, ma puo' essere
# disattivato per i test, che non devono toccare il database di lavoro.
if os.environ.get('FERROTRACK_SKIP_DB_INIT') != '1':
    initialize_database()

# ============ CHI STA AGENDO ============
# I permessi li decide backend/accesso.py (@richiede su ogni rotta). Qui
# resta solo come scrivere nello storico chi ha fatto una cosa: la persona
# entrata col PIN, oppure la postazione (laser, tablet). Mai uno user_id,
# admin_id o created_by mandato dalla pagina.

def _chi() -> str:
    """L'id da scrivere nello storico per l'operazione in corso."""
    ident = identita()
    if ident.admin and not ident.persona and ident.stazione is None:
        return ident.admin['id']
    return ident.utente_id or (ident.admin or {}).get('id') or ''


def _chi_nome() -> str:
    ident = identita()
    if ident.persona:
        return ident.persona['nome']
    if ident.admin and ident.stazione is None:
        return ident.admin['nome']
    return ident.nome


def _chi_admin() -> str:
    """Per le operazioni da amministratore: chi ha digitato il PIN."""
    ident = identita()
    return (ident.admin or {}).get('id') or _chi()


def _audit(azione: str, entita: str, entita_id: str, dettaglio: str):
    try:
        AuditManager.log(user_id=_chi() or None, user_name=_chi_nome(), action=azione,
                         entity_type=entita, entity_id=entita_id, detail=dettaglio[:2000],
                         ip_address=request.remote_addr)
    except Exception as e:
        logger.warning('audit %s non registrato: %s', azione, e)


# Prezzi, costi e margini: li vedono solo gli uffici (il laser vede gli
# ordini, non quanto valgono).
_CHIAVI_PREZZO = ('prezzo_quotato', 'costo_manodopera', 'margine', 'margine_pct',
                  'importo', 'valore', 'totale_lotto', 'totale_pezzo')


def _senza_prezzi(ordine):
    if isinstance(ordine, dict) and identita().stazione not in UFFICI:
        for k in _CHIAVI_PREZZO:
            ordine.pop(k, None)
    return ordine

# ============ FRONTEND ROUTES ============

@app.route('/')
def index():
    """Serve login page"""
    return send_from_directory(FRONTEND_FOLDER, 'login.html')


@app.route('/favicon.ico')
def favicon():
    """Icona LS (scheda del browser, finestre delle postazioni, barra di Windows)."""
    from flask import Response, send_file
    f = os.path.join(os.path.dirname(__file__), '..', 'frontend', 'favicon.ico')
    if os.path.exists(f):
        return send_file(f, mimetype='image/x-icon', max_age=86400)
    return Response(b'', status=204, mimetype='image/x-icon')

@app.route('/download-cert')
def download_cert():
    """Scarica il certificato SSL per installazione su tablet Android."""
    cert_dir = os.path.join(os.path.dirname(__file__), '..', 'certs')
    cert_path = os.path.join(cert_dir, 'cert.pem')
    if os.path.exists(cert_path):
        return send_file(cert_path, as_attachment=True, download_name='ferrotrack-cert.crt',
                        mimetype='application/x-x509-ca-cert')
    return jsonify({'error': 'Certificato non trovato'}), 404

# Pagine legacy rimosse — redirect verso le nuove
_LEGACY_REDIRECTS = {
    'officina.html': '/capo-officina.html',
    'laser-v2.html': '/capo-officina.html',
    'approva-ordine.html': '/capo-officina.html',
    'dettaglio-ordine.html': '/capo-officina.html',
}


@app.route('/<path:filename>')
def serve_frontend(filename):
    """Serve frontend files. Redirect su pagine legacy demolite."""
    if filename in _LEGACY_REDIRECTS:
        return redirect(_LEGACY_REDIRECTS[filename], code=302)
    resp = send_from_directory(FRONTEND_FOLDER, filename)
    # No-cache per HTML: iterazione veloce in dev, l'utente ha sempre la
    # versione fresca del JS/CSS (evita bug post-fix nascosti dietro cache).
    if filename.endswith('.html'):
        resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        resp.headers['Pragma'] = 'no-cache'
        resp.headers['Expires'] = '0'
    return resp

# ============ API AUTH ============
# L'ingresso vero sta in backend/api_accesso.py (/api/accesso/...). Qui resta
# solo il vecchio "entra come postazione", per i dispositivi non ancora
# registrati e SOLO in modalita' transizione: risponde con i dati della
# postazione, che la pagina tiene in memoria e manda come X-User-Id. Non apre
# sessioni e non da' mai i permessi di un amministratore.

@app.route('/api/auth/login', methods=['POST'])
@richiede(PUBBLICO)
def login():
    """Vecchio ingresso per postazione (solo in transizione)."""
    try:
        if accesso.modalita() != 'transizione':
            return jsonify({'success': False, 'codice': 'protetto',
                            'error': "Questo dispositivo non e' registrato: chiedi all'amministrazione."}), 403
        data = request.get_json(silent=True) or {}
        user_id = (data.get('user_id') or '').strip()
        if not user_id:
            return jsonify({'success': False, 'error': 'user_id obbligatorio'}), 400
        u = UserManager.get_user(user_id)
        if not u or not u.get('is_active', True) or not u.get('e_postazione'):
            return jsonify({'success': False, 'error': 'Postazione non trovata'}), 404
        user = UserManager.authenticate(user_id)
        if not user:
            return jsonify({'success': False, 'error': 'Postazione non trovata'}), 404
        return jsonify({
            'success': True,
            'user_id': user['id'],
            'name': user['name'],
            'role': user['role'],
            'phase': user['phase'],
            'permissions': user['permissions'],
            'machines': user['machines'],
            'is_capo': user.get('is_capo', False),
            'e_postazione': True,
            'assigned_clients': user.get('assigned_clients', [])
        }), 200

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

def _e_ultimo_admin(user_id) -> bool:
    """Spegnere l'ultimo amministratore chiuderebbe fuori tutti."""
    from .database import get_session as _gs
    from .models import User as _U
    s = _gs()
    try:
        u = s.get(_U, user_id)
        if u is None or not (u.e_admin and u.pin_hash and u.is_active):
            return False
        return not any(x.e_admin and x.id != user_id for x in accesso.persone_con_pin(s))
    finally:
        s.close()


@app.route('/api/users', methods=['GET'])
@richiede('ufficio', ADMIN)
def get_users():
    """Recupera lista utenti (attivi, o tutti se include_inactive=true)"""
    try:
        include_inactive = request.args.get('include_inactive', 'false').lower() == 'true'
        users = UserManager.get_all_users(include_inactive=include_inactive)
        return jsonify({
            'success': True,
            'users': users
        }), 200

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/users', methods=['POST'])
@richiede(ADMIN)
def create_user():
    """Crea un nuovo utente"""
    try:
        data = request.get_json()

        user_id = data.get('user_id', '').strip()
        name = data.get('name', '').strip()
        role = data.get('role', '').strip()
        phase = data.get('phase', 'LASER').strip()
        permissions = data.get('permissions', [])
        machines = data.get('machines', [])

        if not user_id or not name or not role:
            return jsonify({'success': False, 'error': 'Missing required fields'}), 400

        result = UserManager.create_user(
            user_id=user_id,
            name=name,
            role=role,
            phase=phase,
            permissions=permissions,
            machines=machines
        )

        if result is None:
            return jsonify({'success': False, 'error': 'User already exists'}), 400

        return jsonify({'success': True, 'user': result}), 201

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/users/<user_id>', methods=['DELETE'])
@richiede(ADMIN)
def delete_user(user_id):
    """Disattiva un utente (soft delete)"""
    try:
        if _e_ultimo_admin(user_id):
            return jsonify({'success': False, 'codice': 'ultimo_admin',
                            'error': "È l’unico amministratore: prima nominane un altro."}), 409
        success = UserManager.delete_user(user_id)
        accesso.chiudi_sessioni(user_id=user_id)
        if not success:
            return jsonify({'success': False, 'error': 'User not found'}), 404

        return jsonify({'success': True, 'message': f'User {user_id} deleted'}), 200

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/users/<user_id>', methods=['PUT'])
@richiede(ADMIN)
def update_user(user_id):
    """Modifica un utente esistente"""
    try:
        data = request.get_json()
        if data.get('is_active') is False and _e_ultimo_admin(user_id):
            return jsonify({'success': False, 'codice': 'ultimo_admin',
                            'error': "È l’unico amministratore: prima nominane un altro."}), 409

        result = UserManager.update_user(
            user_id=user_id,
            name=data.get('name'),
            role=data.get('role'),
            phase=data.get('phase'),
            is_active=data.get('is_active')
        )
        if result is None:
            return jsonify({'success': False, 'error': 'User not found'}), 404
        return jsonify({'success': True, 'user': result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

# ============ API ORDINI ============

@app.route('/api/orders', methods=['POST'])
@richiede('ufficio')
def create_order():
    """Crea un nuovo ordine con routing dinamico"""
    try:
        data = request.get_json()

        numero_ordine = data.get('numero_ordine', '').strip()
        if not numero_ordine:
            return jsonify({'success': False, 'error': 'Numero ordine obbligatorio'}), 400

        cliente = (data.get('cliente') or '').strip()
        if not cliente:
            return jsonify({'success': False, 'error': 'Cliente obbligatorio'}), 400

        data_consegna = data.get('data_consegna')
        if not data_consegna:
            return jsonify({'success': False, 'error': 'Data consegna obbligatoria'}), 400
        # Validazione: data consegna non nel passato (tolleranza: ieri)
        try:
            dc = datetime.strptime(data_consegna, '%Y-%m-%d').date()
            ieri = (datetime.utcnow() - timedelta(days=1)).date()
            if dc < ieri:
                return jsonify({'success': False, 'error': f'Data consegna {data_consegna} è nel passato'}), 400
        except ValueError:
            pass  # formato non standard, lascia che il DB gestisca

        # Check duplicati: stesso cliente + numero ordine (non archiviati)
        existing = OrderManager.get_all_orders_dict(cliente=cliente)
        for ex in existing:
            if (ex.get('numero_ordine') or '').strip().lower() == numero_ordine.lower():
                return jsonify({'success': False, 'error': f'Ordine #{numero_ordine} per {cliente} esiste già'}), 409

        # Valore dell'ordine (netto, IVA esclusa): letto dal PDF gia' prezzato
        # del cliente o scritto dall'ufficio. Vuoto = non si sa (mai zero).
        valore = data.get('valore_ordine')
        try:
            valore = round(float(str(valore).replace(',', '.')), 2) if valore not in (None, '') else None
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': "Il valore dell'ordine non e' un numero"}), 400
        if valore is not None and valore < 0:
            return jsonify({'success': False, 'error': "Il valore dell'ordine non puo' essere negativo"}), 400

        order = OrderManager.create_order(
            cliente=cliente,
            data_consegna=data_consegna,
            numero_ordine=numero_ordine,
            note=data.get('note', ''),
            prezzo_quotato=valore,
        )

        # Registra il file PDF nel DB
        session = get_session()
        try:
            pdf_filename = data.get('pdf_filename')
            if pdf_filename:
                pdf_filename = os.path.basename(pdf_filename)
                pdfs_folder = os.path.join(os.path.dirname(__file__), '..', 'uploads', 'pdfs')
                pdf_path = os.path.join(pdfs_folder, pdf_filename)
                if os.path.exists(pdf_path):
                    file_record = OrderFile(
                        id=str(uuid.uuid4()),
                        order_id=order.id,
                        filename=pdf_filename,
                        filepath=pdf_path,
                        file_type='PDF'
                    )
                    session.add(file_record)

            session.commit()
        finally:
            session.close()

        # Notifica a tutti i capi officina ("Nuovo ordine")
        numero_display = order.numero_ordine or order.id[:8]
        try:
            for u in UserManager.get_all_users() or []:
                if u.get('is_capo') and u.get('is_active', True):
                    NotificationManager.create_notification(
                        user_id=u['id'],
                        order_id=order.id,
                        title='Nuovo ordine',
                        message=f'Ordine #{numero_display} ({order.cliente})',
                        notification_type='order',
                        notification_category='informativa'
                    )
        except Exception as exc:
            logger.warning('notifica nuovo ordine ai capi fallita: %s', exc)

        return jsonify({
            'success': True,
            'order_id': order.id,
            'cliente': order.cliente,
            'data_consegna': order.data_consegna.isoformat(),
        }), 201

    except Exception as e:
        logger.error(f"Create order error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/orders/<order_id>', methods=['GET'])
@richiede('commerciale', 'ufficio', 'laser')
def get_order(order_id):
    """Recupera dettagli ordine con stato articoli"""
    try:
        details = OrderManager.get_order_details(order_id)
        if 'error' in details:
            return jsonify(details), 404
        return jsonify(_senza_prezzi(details)), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/orders/<order_id>/delivery-date', methods=['PUT'])
@richiede('ufficio')
def update_delivery_date(order_id):
    """Aggiorna data di consegna di un ordine (solo amministrazione/capi)."""
    try:
        data = request.get_json()
        if not data or 'data_consegna' not in data:
            return jsonify({'success': False, 'error': 'data_consegna obbligatoria'}), 400
        new_date_str = data['data_consegna']
        try:
            new_date = datetime.strptime(new_date_str[:10], '%Y-%m-%d')
        except ValueError:
            return jsonify({'success': False, 'error': 'Formato YYYY-MM-DD richiesto'}), 400
        _o = OrderManager.get_order(order_id)
        _prima = getattr(_o, 'data_consegna', None) if _o else None
        result = OrderManager.update_delivery_date(order_id, new_date)
        if result.get('success'):
            _audit('ORDINE_DATA_CONSEGNA', 'orders', order_id,
                   f"data_consegna: {str(_prima)[:10] if _prima else '-'} -> {new_date_str[:10]}")
            return jsonify(result), 200
        return jsonify(result), 404
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/orders/<order_id>', methods=['PUT'])
@richiede('ufficio')
def update_order(order_id):
    """Aggiorna dati ordine: cliente, note, data_consegna, numero_ordine.
    Solo amministrazione/capi, con audit dei valori prima e dopo."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'Body JSON richiesto'}), 400
        _prima = {}
        _s = get_session()
        try:
            _ord = _s.query(Order).filter(Order.id == order_id).first()
            if _ord:
                _prima = {k: str(getattr(_ord, k)) for k in data if k not in ('user_id', 'admin_id') and hasattr(_ord, k)}
        finally:
            _s.close()
        result = OrderManager.update_order(order_id, data)
        if result.get('success'):
            _audit('ORDINE_MODIFICA', 'orders', order_id,
                   '; '.join(f'{k}: {v} -> {data.get(k)}' for k, v in _prima.items()))
            return jsonify(result), 200
        # 404 solo se l'ordine non c'e': un dato non valido e' 400, altrimenti
        # chi chiama non distingue "non esiste" da "hai sbagliato a scrivere".
        manca = 'non trovato' in (result.get('error') or '').lower()
        return jsonify(result), 404 if manca else 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/orders/<order_id>', methods=['DELETE'])
@richiede('ufficio')
def delete_order(order_id):
    """Soft delete di un ordine (is_deleted=True): solo amministrazione/capi, con audit."""
    try:
        session = get_session()
        try:
            order = session.query(Order).filter(Order.id == order_id).first()
            if not order:
                return jsonify({'success': False, 'error': 'Ordine non trovato'}), 404
            order.is_deleted = True
            session.commit()
            _audit('ORDINE_ELIMINATO', 'orders', order_id,
                   f'{getattr(order, "numero_ordine", "")} {getattr(order, "cliente", "")} (recuperabile)')
            return jsonify({'success': True, 'message': f'Ordine {order_id} eliminato (recuperabile)'}), 200
        finally:
            session.close()
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/orders/<order_id>/mark-laser-done', methods=['POST'])
@richiede('laser')
def mark_laser_done(order_id):
    """Marca il taglio laser come completato — chiama il LASER (Mirko).
    Da questo momento i pezzi sono pronti per l'officina.
    """
    try:
        # Solo la stazione Laser (regola sopra): chi ha tagliato e' il laser
        result = OrderManager.mark_laser_done(order_id, user_id=_chi())
        return jsonify(result), (200 if result.get('success') else 400)
    except Exception as e:
        logger.exception('mark_laser_done endpoint failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/orders/<order_id>/smistamento', methods=['POST'])
@richiede('laser')
def smista_ordine(order_id):
    """Il laser dice se un ordine passa da lui.

    Corpo: {"va_tagliato": true|false} oppure {"annulla": true} per rimettere
    l'ordine fra quelli da guardare.
    """
    try:
        data = request.get_json(silent=True) or {}
        # Smistare e' una decisione del laser: la stessa porta della marcatura
        # del taglio, non una piu' larga (regola sopra).
        user_id = _chi()

        if data.get('annulla'):
            result = OrderManager.annulla_smistamento(order_id, user_id=user_id)
        else:
            if 'va_tagliato' not in data:
                return jsonify({'error': 'va_tagliato obbligatorio'}), 400
            result = OrderManager.smista(
                order_id, bool(data.get('va_tagliato')), user_id=user_id)
        return jsonify(result), (200 if result.get('success') else 400)
    except Exception as e:
        logger.exception('smistamento endpoint fallito')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/orders/<order_id>/importato', methods=['POST'])
@richiede('laser')
def segna_importato_lantek(order_id):
    """Il laser segna che i disegni dell'ordine sono in Lantek.
    Corpo: {annulla?: true}."""
    try:
        data = request.get_json(silent=True) or {}
        r = OrderManager.segna_importato(order_id, user_id=_chi(), annulla=bool(data.get('annulla')))
        return jsonify(r), (200 if r.get('success') else (404 if r.get('codice') == 'non_trovato' else 400))
    except Exception as e:
        logger.exception('importato endpoint fallito')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/orders/<order_id>/pianifica-taglio', methods=['POST'])
@richiede('laser')
def pianifica_taglio(order_id):
    """Calendario del laser. Corpo: {data?: 'AAAA-MM-GG' | null,
    durata_min?: numero | null}. data null = torna sul giorno di consegna;
    durata_min = durata stimata a mano (ordini senza disegni)."""
    try:
        data = request.get_json(silent=True) or {}
        kw = {}
        if 'data' in data:
            g = data.get('data')
            if g:
                try:
                    g = datetime.strptime(str(g)[:10], '%Y-%m-%d').date()
                except ValueError:
                    return jsonify({'success': False, 'error': 'Data non valida (serve AAAA-MM-GG)'}), 400
                from .orario import oggi_locale as _oggi
                if g < _oggi():
                    return jsonify({'success': False, 'error': 'Non si pianifica un taglio nel passato'}), 400
            kw['giorno'] = g or None
        if 'durata_min' in data:
            d = data.get('durata_min')
            if d in (None, ''):
                kw['durata_min'] = None
            else:
                try:
                    d = float(d)
                except (TypeError, ValueError):
                    return jsonify({'success': False, 'error': 'Durata non valida'}), 400
                if not (0 < d <= 24 * 60 * 10):
                    return jsonify({'success': False, 'error': 'Durata fuori misura'}), 400
                kw['durata_min'] = round(d, 1)
        if not kw:
            return jsonify({'success': False, 'error': 'Niente da cambiare (data o durata_min)'}), 400
        r = OrderManager.pianifica_taglio(order_id, user_id=_chi(), **kw)
        return jsonify(r), (200 if r.get('success') else (404 if r.get('codice') == 'non_trovato' else 400))
    except Exception as e:
        logger.exception('pianifica_taglio endpoint fallito')
        return jsonify({'success': False, 'error': str(e)}), 500


_CAL_DEFAULT = {'ore_turno': 8, 'ore_riserva': 2, 'giorni': [1, 2, 3, 4, 5],
                'carico_min_lamiera': 5, 'scarico_s_pezzo': 3, 'fattore_tempo': 1.18,
                # giorni speciali: {'2026-10-20': {'ore': 0, 'nota': 'manutenzione'}}
                # (ore UTILI di quel giorno: 0 = laser fermo)
                'eccezioni': {}}
_CAL_LIMITI = {'ore_turno': (1, 24), 'ore_riserva': (0, 23), 'carico_min_lamiera': (0, 120),
               'scarico_s_pezzo': (0, 600), 'fattore_tempo': (0.5, 3)}


def _cal_config() -> dict:
    v = (ConfigManager.load_config() or {}).get('laser_calendario') or {}
    return {**_CAL_DEFAULT, **{k: v[k] for k in _CAL_DEFAULT if k in v}}


@app.route('/api/laser/calendario-config', methods=['GET', 'PUT'])
@richiede('laser', 'ufficio', metodi=('GET',))
@richiede('laser', metodi=('PUT',))
def api_laser_calendario_config():
    """Impostazioni del calendario del laser: ore del turno, riserva, giorni
    lavorativi, carico/scarico, correzione dei tempi verso Lantek. Stanno
    fuori da laser_config: cambiarle non deve far ristimare i preventivi."""
    try:
        if request.method == 'GET':
            return jsonify({'success': True, 'config': _cal_config()}), 200
        data = request.get_json(silent=True) or {}
        cfg = _cal_config()
        for k, (lo, hi) in _CAL_LIMITI.items():
            if k in data:
                try:
                    v = float(data[k])
                except (TypeError, ValueError):
                    return jsonify({'success': False, 'error': f'{k}: non e\' un numero'}), 400
                if not lo <= v <= hi:
                    return jsonify({'success': False, 'error': f'{k}: fuori misura ({lo}-{hi})'}), 400
                cfg[k] = v
        if 'giorni' in data:
            g = data.get('giorni')
            if not isinstance(g, list) or not all(isinstance(x, int) and 1 <= x <= 7 for x in g):
                return jsonify({'success': False, 'error': 'giorni: lista di numeri 1-7 (1 = lunedi)'}), 400
            cfg['giorni'] = sorted(set(g))
        if 'eccezioni' in data:
            ecc = data.get('eccezioni')
            if not isinstance(ecc, dict) or len(ecc) > 400:
                return jsonify({'success': False, 'error': 'eccezioni: elenco di giorni non valido'}), 400
            pulite = {}
            limite = (datetime.now() - timedelta(days=60)).strftime('%Y-%m-%d')
            for g, v in ecc.items():
                try:
                    g = datetime.strptime(str(g)[:10], '%Y-%m-%d').strftime('%Y-%m-%d')
                    ore = float((v or {}).get('ore'))
                except (TypeError, ValueError, AttributeError):
                    return jsonify({'success': False, 'error': f'eccezione {g}: serve data e ore'}), 400
                if not 0 <= ore <= 24:
                    return jsonify({'success': False, 'error': f'eccezione {g}: ore fuori misura'}), 400
                if g >= limite:            # le vecchie si buttano
                    pulite[g] = {'ore': ore, 'nota': str((v or {}).get('nota') or '')[:60]}
            cfg['eccezioni'] = pulite
        if cfg['ore_riserva'] >= cfg['ore_turno']:
            return jsonify({'success': False, 'error': 'La riserva deve essere minore del turno'}), 400
        ConfigManager.save_config({'laser_calendario': cfg})
        return jsonify({'success': True, 'config': _cal_config()}), 200
    except Exception as e:
        logger.exception('calendario-config fallito')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/orders/<order_id>/mark-laser-undone', methods=['POST'])
@richiede('laser')
def mark_laser_undone(order_id):
    """Rollback marcatura taglio completato (errore, va re-tagliato)."""
    try:
        result = OrderManager.mark_laser_undone(order_id, user_id=_chi())
        return jsonify(result), (200 if result.get('success') else 400)
    except Exception as e:
        logger.exception('mark_laser_undone endpoint failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/orders/<order_id>/close', methods=['POST'])
@richiede('ufficio', 'laser')
def close_order(order_id):
    """Capo officina: "Lavoro finito". E' il completamento del ciclo ordini
    (data e autore registrati, ufficio avvisato), non uno stato a parte.

    Permesso: capi officina E impiegata (Elena fa da backup quando i capi
    si dimenticano o accumulano).

    Risposta: {success, order_id, nuovo_status, gia_registrato,
    ordine, avviso?}. `avviso` c'e' quando il laser doveva tagliare l'ordine e
    non ha segnato il taglio: non blocca, ma va detto.
    """
    try:
        result = OrderManager.close_order(order_id, user_id=_chi())
        if result.get('success'):
            return jsonify(result), 200
        return jsonify(result), (404 if result.get('codice') == 'non_trovato' else 400)
    except Exception as e:
        logger.exception('close_order endpoint failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/orders/sospetti-finiti', methods=['GET'])
@richiede('ufficio', 'laser')
def api_ordini_sospetti_finiti():
    """Ordini che probabilmente sono finiti ma nessuno li ha chiusi.

    Usato da Elena (sezione dedicata) e dal Pannello Capo (badge rosso).
    Soglie configurabili via /api/admin/config.
    """
    try:
        items = OrderManager.get_ordini_sospetti_finiti()
        cfg = ConfigManager.load_config() or {}
        return jsonify({
            'success': True,
            'count': len(items),
            'orders': [_senza_prezzi(o) for o in items],
            # Solo le soglie: il resto della configurazione ha i prezzi, e
            # questa la legge anche il laser.
            'config': {k: v for k, v in cfg.items() if k.startswith('sospetto_')},
        }), 200
    except Exception as e:
        logger.exception('sospetti-finiti endpoint failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/admin/config', methods=['GET'])
@richiede('commerciale', 'ufficio', ADMIN)
def api_admin_config_get():
    """Config app (soglie sospetto, ecc.). Lettura aperta."""
    try:
        return jsonify({'success': True, 'config': ConfigManager.load_config()}), 200
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/admin/config', methods=['PUT'])
@richiede(ADMIN)
def api_admin_config_update():
    """Modifica config app (solo capi/admin)."""
    try:
        data = request.get_json(silent=True) or {}
        user_id = _chi_admin()
        # Pagine vecchie possono ancora mandare `user_id`/`admin_id`: non sono
        # impostazioni, e senza toglierli finirebbero fra quelle "sconosciute".
        updates = {k: v for k, v in data.items()
                   if k not in ('admin_id', 'user_id')}
        new_cfg = ConfigManager.save_config(updates)
        if 'error' in new_cfg:
            return jsonify({'success': False, 'error': new_cfg['error']}), 500
        try:
            AuditManager.log(
                user_id=user_id, action='UPDATE_CONFIG',
                entity_type='config', entity_id='app_config',
                detail=str(updates),
            )
        except Exception:
            pass
        return jsonify({'success': True, 'config': new_cfg}), 200
    except Exception as e:
        logger.exception('config update failed')
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/orders/<order_id>/replace-pdf', methods=['POST'])
@richiede('ufficio')
def replace_order_pdf(order_id):
    """Sostituisce il PDF di un ordine esistente (solo amministrazione/capi, con audit)."""
    try:
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'Nessun file inviato'}), 400
        file = request.files['file']
        if not file.filename or not file.filename.lower().endswith('.pdf'):
            return jsonify({'success': False, 'error': 'Il file deve essere un PDF'}), 400

        session = get_session()
        try:
            order = session.query(Order).filter(Order.id == order_id).first()
            if not order:
                return jsonify({'success': False, 'error': 'Ordine non trovato'}), 404

            # Salva il nuovo file
            pdf_filename = f"{os.path.basename(order_id)}_{os.path.basename(file.filename)}"
            pdf_path = os.path.join(PDFS_FOLDER, pdf_filename)
            file.save(pdf_path)
            _audit('ORDINE_PDF_SOSTITUITO', 'orders', order_id, pdf_filename)

            # Aggiorna o crea il record file
            existing_pdf = session.query(OrderFile).filter(
                OrderFile.order_id == order_id,
                OrderFile.file_type == 'PDF'
            ).first()
            if existing_pdf:
                existing_pdf.filename = pdf_filename
                existing_pdf.filepath = pdf_path
            else:
                new_file = OrderFile(
                    id=str(uuid.uuid4()),
                    order_id=order_id,
                    filename=pdf_filename,
                    filepath=pdf_path,
                    file_type='PDF'
                )
                session.add(new_file)

            session.commit()
            return jsonify({'success': True, 'filename': pdf_filename}), 200
        finally:
            session.close()
    except Exception as e:
        logger.error(f"replace_order_pdf: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/orders/<order_id>/pdf', methods=['GET'])
@richiede(*OFFICINA_LETTURA)
def get_order_pdf(order_id):
    """Serve il PDF dell'ordine inline (per iframe viewer)"""
    try:
        session = get_session()
        try:
            order = session.query(Order).filter(Order.id == order_id).first()
            if not order:
                return jsonify({'success': False, 'codice': 'non_trovato',
                                'error': 'Ordine non trovato'}), 404

            # Cerca il file PDF tra i file allegati
            pdf_file = session.query(OrderFile).filter(
                OrderFile.order_id == order_id,
                OrderFile.file_type == 'PDF'
            ).first()

            # Gli ordini nati da un preventivo spesso non hanno un PDF: non e'
            # un errore del server, e la pagina deve poterlo distinguere da un
            # guasto per mostrare "nessun PDF" invece di un messaggio rosso.
            if not pdf_file or not pdf_file.filepath or not os.path.exists(pdf_file.filepath):
                return jsonify({'success': False, 'codice': 'pdf_assente',
                                'error': "Quest'ordine non ha un PDF allegato"}), 404

            # Serve il PDF inline per iframe
            return send_file(
                pdf_file.filepath,
                mimetype='application/pdf',
                as_attachment=False,
                download_name=pdf_file.filename
            )

        finally:
            session.close()

    except Exception as e:
        logger.error(f"get_order_pdf: {e}")
        return jsonify({'error': str(e)}), 500


# ============ STAMPA ORDINE ============
#
# In officina l'ordine si riconosce dal PDF d'ordine stampato (i cartellini
# A6 col barcode non ci sono piu'). Un solo indirizzo per "Stampa ordine":
# chi stampa non deve sapere da dove e' nato l'ordine.

@app.route('/api/orders/<order_id>/stampa', methods=['GET'])
@richiede(*OFFICINA_LETTURA)
def api_stampa_ordine(order_id):
    """Cosa stampare per un ordine.

    - col PDF del cliente: quel PDF, lo stesso di /api/orders/<id>/pdf (e del
      foglio d'ordine sul tablet dell'officina);
    - senza (ordini nati da un preventivo): il foglio d'ordine A4 in HTML,
      con la stessa distinta che mostra il tablet (backend/foglio_ordine.py).
    """
    try:
        order = _ordine_esistente(order_id)
        if not order:
            return _non_trovato_ordine()
        session = get_session()
        try:
            pdf_file = session.query(OrderFile).filter(
                OrderFile.order_id == order_id,
                OrderFile.file_type == 'PDF'
            ).first()
        finally:
            session.close()
        if pdf_file and pdf_file.filepath and os.path.exists(pdf_file.filepath):
            return send_file(pdf_file.filepath, mimetype='application/pdf',
                             as_attachment=False, download_name=pdf_file.filename)

        from .foglio_ordine import html_foglio
        righe, _origine, prev = _distinta_ordine(order)
        num_cli = ((prev or {}).get('numero_ordine_cliente') or '').strip()
        testata = {
            # il numero che conosce l'officina, come sul tablet
            'numero': num_cli or order.numero_ordine or order.id[:8],
            'numero_ordine': order.numero_ordine or '',
            'cliente': order.cliente or '',
            'data_consegna': iso_data(order.data_consegna),
            'note': order.note or '',
        }
        risposta = app.response_class(html_foglio(testata, righe),
                                      mimetype='text/html')
        risposta.headers['Cache-Control'] = 'no-store'
        return risposta
    except Exception as e:
        logger.exception('stampa ordine fallita')
        return jsonify({'success': False, 'error': str(e)}), 500

# ============ DISEGNI E DISTINTA DELL'ORDINE ============
#
# Accettando un preventivo i DXF vengono copiati in uploads/drawings/<order_id>/,
# ma lo scaricamento li cercava in uploads/drawings/<nome> (la cartella piatta
# dei caricamenti vecchi): 404, e nessuna pagina riusciva ad aprire i disegni
# di un ordine. Qui c'e' UN solo elenco dei disegni di un ordine, e tutti gli
# endpoint (elenco, scarica, anteprima, zip) passano da li': un nome che non
# e' nell'elenco non si apre, e questo chiude anche la porta ai "../".

_ESTENSIONI_DISEGNO = ('.dxf', '.dwg')


def _ordine_esistente(order_id: str):
    """L'ordine (anche annullato: i disegni restano consultabili), o None."""
    session = get_session()
    try:
        return session.query(Order).filter(Order.id == order_id).first()
    finally:
        session.close()


def _nome_mostrato(nome: str, order_id: str) -> str:
    """Il nome con cui l'utente riconosce il file.

    I caricamenti vecchi stavano nella cartella piatta come "<id>_<nome>" e a
    volte con un "draft_" davanti: quei prefissi sono nostri, non del disegno.
    """
    base = os.path.basename(nome or '')
    if order_id and base.startswith(order_id + '_'):
        base = base[len(order_id) + 1:]
    if base.startswith('draft_'):
        base = base[len('draft_'):]
    return base


def _elenco_cartella(cartella: str) -> list:
    """[(nome, percorso, dimensione)] dei FILE di una cartella, in ordine di nome.

    os.scandir legge nome, tipo e dimensione in un colpo solo: su Windows
    chiedere a parte "e' un file?" e "quanto pesa?" per ogni disegno costava
    piu' di tutto il resto (il tablet chiede i disegni di tutti gli ordini)."""
    out = []
    try:
        with os.scandir(cartella) as it:
            for e in it:
                try:
                    if e.is_file():
                        out.append((e.name, e.path, e.stat().st_size))
                except OSError:
                    continue
    except OSError:
        return []
    return sorted(out, key=lambda x: x[0].lower())


def _disegni_ordine(order, piatti=None, righe_file=None) -> list:
    """Tutti i disegni di un ordine: [{nome, percorso, dimensione}].

    Dove si cercano, nell'ordine:
      1. uploads/drawings/<order_id>/   (ordini da preventivo, e da oggi)
      2. la cartella piatta uploads/drawings/<order_id>_<nome> (caricamenti vecchi)
      3. le righe di order_files, per gli ordini vecchi che hanno il percorso
         registrato altrove
    Lo stesso nome compare una volta sola: vince il primo trovato.

    Chi chiede i disegni di MOLTI ordini (il tablet dell'officina) puo' passare
    la cartella piatta gia' letta (piatti = _elenco_cartella(DRAWINGS_FOLDER))
    e le righe di order_files gia' caricate (righe_file = order.files): il
    risultato e' lo stesso, senza rileggerle per ogni ordine.
    """
    order_id = order.id
    visti = {}

    def aggiungi(percorso, nome=None, dim=-1):
        if not percorso:
            return
        nome = _nome_mostrato(nome or percorso, order_id)
        if not nome.lower().endswith(_ESTENSIONI_DISEGNO):
            return
        chiave = nome.lower()
        if chiave in visti:
            return
        if dim == -1:          # percorso non visto da una lettura di cartella
            if not os.path.isfile(percorso):
                return
            try:
                dim = os.path.getsize(percorso)
            except OSError:
                dim = None
        visti[chiave] = {'nome': nome, 'percorso': os.path.normpath(percorso),
                         'dimensione': dim}

    cartella = os.path.join(DRAWINGS_FOLDER, order_id)
    for nome, percorso, dim in _elenco_cartella(cartella):
        aggiungi(percorso, nome, dim)

    prefisso = order_id + '_'
    if piatti is None:
        piatti = _elenco_cartella(DRAWINGS_FOLDER)
    for nome, percorso, dim in piatti:
        if nome.startswith(prefisso):
            aggiungi(percorso, nome, dim)

    if righe_file is None:
        session = get_session()
        try:
            righe_file = session.query(OrderFile).filter(OrderFile.order_id == order_id).all()
        finally:
            session.close()
    for r in righe_file:
        nome = r.filename or os.path.basename(r.filepath or '')
        if not nome.lower().endswith(_ESTENSIONI_DISEGNO):
            continue
        if nome.lower() in visti or _nome_mostrato(nome, order_id).lower() in visti:
            continue           # gia' trovato nelle cartelle: niente controlli in piu'
        percorso = r.filepath
        if not (percorso and os.path.isfile(percorso)):
            # Percorso registrato su un'altra macchina o spostato: si
            # riprova nelle cartelle note col solo nome del file.
            base = os.path.basename(nome)
            for tentativo in (os.path.join(cartella, base),
                              os.path.join(DRAWINGS_FOLDER, base),
                              os.path.join(DRAWINGS_FOLDER, prefisso + base)):
                if os.path.isfile(tentativo):
                    percorso = tentativo
                    break
        aggiungi(percorso, nome)
    return list(visti.values())


def _trova_disegno(order, filename: str):
    """Il disegno richiesto, solo se fa parte dell'elenco dell'ordine."""
    richiesto = os.path.basename((filename or '').replace('\\', '/'))
    if not richiesto or richiesto in ('.', '..') or richiesto != (filename or ''):
        return None
    chiave = _nome_mostrato(richiesto, order.id).lower()
    for d in _disegni_ordine(order):
        if d['nome'].lower() == chiave:
            return d
    return None


def _cartella_condivisa(order) -> dict:
    """Stato della copia dei disegni nella cartella di rete (quella di Lantek)."""
    out = {'configurata': False, 'percorso': None, 'esportato': False}
    try:
        root = ((ConfigManager.load_config() or {}).get('disegni_export_root') or '').strip()
        if not root:
            return out
        out['configurata'] = True
        from .preventivi.dxf_cleanup import _sanitize_path_part
        percorso = os.path.normpath(os.path.join(
            root,
            _sanitize_path_part(order.cliente or '', 'cliente_sconosciuto'),
            _sanitize_path_part(_numero_per_cartelle(order), 'ordine')))
        out['percorso'] = percorso
        out['esportato'] = os.path.isdir(percorso) and any(f for _b, _d, f in os.walk(percorso))
    except Exception:
        logger.exception('stato cartella condivisa non determinabile')
    return out


def _url_disegno(order_id: str, nome: str) -> str:
    from urllib.parse import quote
    return f'/api/orders/{order_id}/dxf/{quote(nome)}'


def _non_trovato_ordine():
    return jsonify({'success': False, 'codice': 'non_trovato',
                    'error': 'Ordine non trovato'}), 404


@app.route('/api/orders/<order_id>/disegni', methods=['GET'])
@richiede(*OFFICINA_LETTURA)
def api_ordine_disegni(order_id):
    """Elenco dei disegni dell'ordine, con i link per scaricarli e vederli."""
    try:
        order = _ordine_esistente(order_id)
        if not order:
            return _non_trovato_ordine()
        disegni = []
        for d in _disegni_ordine(order):
            url = _url_disegno(order_id, d['nome'])
            disegni.append({
                'nome': d['nome'],
                'dimensione': d['dimensione'],
                'url_dxf': url,
                # L'anteprima c'e' solo per i DXF: un DWG va prima convertito.
                'url_svg': (url + '/svg') if d['nome'].lower().endswith('.dxf') else None,
            })
        return jsonify({
            'success': True,
            'disegni': disegni,
            'url_zip': f'/api/orders/{order_id}/disegni.zip',
            'cartella_condivisa': _cartella_condivisa(order),
        }), 200
    except Exception as e:
        logger.exception('elenco disegni ordine fallito')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/orders/<order_id>/dxf/<filename>', methods=['GET'])
@richiede(*OFFICINA_LETTURA)
def get_dxf_file(order_id, filename):
    """Scarica un disegno dell'ordine, col suo nome."""
    try:
        order = _ordine_esistente(order_id)
        if not order:
            return _non_trovato_ordine()
        d = _trova_disegno(order, filename)
        if not d:
            return jsonify({'success': False, 'codice': 'disegno_assente',
                            'error': 'Disegno non trovato per quest\'ordine'}), 404
        return send_file(
            d['percorso'],
            mimetype='application/dxf',
            as_attachment=True,
            download_name=d['nome']
        )
    except Exception as e:
        logger.error(f"Get DXF file error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/orders/<order_id>/dxf/<filename>/svg', methods=['GET'])
@richiede(*OFFICINA_LETTURA)
def api_ordine_dxf_svg(order_id, filename):
    """Anteprima SVG di un disegno dell'ordine: stesso disegnatore (e stessa
    cache) dell'anteprima nel preventivo, cosi' il pezzo si vede uguale."""
    try:
        order = _ordine_esistente(order_id)
        if not order:
            return _non_trovato_ordine()
        d = _trova_disegno(order, filename)
        if not d:
            return jsonify({'success': False, 'codice': 'disegno_assente',
                            'error': 'Disegno non trovato per quest\'ordine'}), 404
        if not d['nome'].lower().endswith('.dxf'):
            return jsonify({'success': False, 'codice': 'anteprima_non_disponibile',
                            'error': 'Anteprima disponibile solo per i file DXF'}), 415
        from flask import Response
        resp = Response(_get_dxf_svg_cached(d['percorso']),
                        mimetype='image/svg+xml; charset=utf-8')
        resp.headers['Cache-Control'] = 'private, max-age=3600'
        return resp
    except Exception as e:
        logger.exception('svg disegno ordine fallito')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/orders/<order_id>/disegni.zip', methods=['GET'])
@richiede(*OFFICINA_LETTURA)
def api_ordine_disegni_zip(order_id):
    """Tutti i disegni dell'ordine in un file zip (in memoria: sono decine di
    file da pochi KB, non serve scrivere su disco)."""
    try:
        import io
        import zipfile
        order = _ordine_esistente(order_id)
        if not order:
            return _non_trovato_ordine()
        disegni = _disegni_ordine(order)
        if not disegni:
            return jsonify({'success': False, 'codice': 'nessun_disegno',
                            'error': 'Quest\'ordine non ha disegni'}), 404
        from .preventivi.dxf_cleanup import _sanitize_path_part
        radice = _sanitize_path_part(
            ' - '.join(x for x in (order.cliente or '', _numero_per_cartelle(order)) if x), 'disegni')
        try:
            righe = _distinta_ordine(order)[0]
        except Exception:
            logger.warning('distinta per lo zip non letta', exc_info=True)
            righe = []
        voci = _struttura_zip(radice, os.path.join(DRAWINGS_FOLDER, order_id), disegni, righe)
        try:
            q = _quantita_lantek(order, righe)
            dati_lt = _dati_lantek_per_disegno(order, righe, q)
        except Exception:
            logger.warning('dati Lantek per i disegni non letti', exc_info=True)
            q, dati_lt = None, {}
        from . import lantek as _lt
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            for percorso, arcname in voci:
                v = _voce_per_lantek(arcname, dati_lt)
                if v:
                    try:
                        zf.writestr(v[0], _lt.dxf_con_dati(percorso, v[1]))
                        continue
                    except Exception:
                        logger.warning('scritte Lantek non aggiunte a %s', arcname, exc_info=True)
                zf.write(percorso, arcname=arcname)
        buf.seek(0)
        return send_file(buf, mimetype='application/zip', as_attachment=True,
                         download_name=radice + '.zip')
    except Exception as e:
        logger.exception('zip disegni ordine fallito')
        return jsonify({'success': False, 'error': str(e)}), 500


def _struttura_zip(radice: str, cartella_ordine: str, disegni: list, righe: list) -> list:
    """[(percorso, nome nello zip)] dei disegni di un ordine, divisi per lamiera.

    Ordini accettati col pulito per Lantek: la cartella LANTEK com'e' (puliti
    per lamiera + _DA PREPARARE) e gli originali in _DISEGNI ORIGINALI.
    Ordini di prima: gli originali divisi per lamiera secondo la distinta;
    quelli senza materiale in "_SENZA MATERIALE"."""
    voci = []
    lantek = os.path.join(cartella_ordine, 'LANTEK')
    if os.path.isdir(lantek):
        for base, _dirs, files in os.walk(lantek):
            for f in sorted(files):
                p = os.path.join(base, f)
                rel = os.path.relpath(p, lantek).replace(os.sep, '/')
                voci.append((p, f'{radice}/{rel}'))
        for d in disegni:
            voci.append((d['percorso'], f"{radice}/{CARTELLA_ORIGINALI}/{d['nome']}"))
        return _senza_doppioni(voci, cartella_ordine)
    lamiera_di = {}
    for r in righe or []:
        nome = (r.get('disegno') or '').lower()
        if nome and r.get('materiale'):
            lamiera_di.setdefault(nome, _nome_lamiera(r.get('materiale'), r.get('spessore_mm')))
    for d in disegni:
        cartella = lamiera_di.get(_nome_base_disegno(d['nome']).lower(),
                                  lamiera_di.get(d['nome'].lower(), '_SENZA MATERIALE'))
        voci.append((d['percorso'], f"{radice}/{cartella}/{d['nome']}"))
    return _senza_doppioni(voci, cartella_ordine)


def _senza_doppioni(voci: list, cartella_ordine: str) -> list:
    """Nella stessa cartella dello zip, lo stesso disegno una volta sola e
    col nome senza "(2)". Uguale = stesso nome base e stesso ORIGINALE (i
    puliti si confrontano col loro originale: due puliti dello stesso disegno
    non sono identici byte per byte). Codice uguale ma disegno diverso: restano
    tutti e due."""
    gruppi = {}
    for percorso, arc in voci:
        cartella, nome = arc.rsplit('/', 1)
        gruppi.setdefault((cartella, _nome_base_disegno(nome).lower()), []).append((percorso, arc, nome))
    out = []
    for (cartella, _b), membri in gruppi.items():
        tenuti, impronte = [], set()
        for percorso, arc, nome in membri:
            orig = os.path.join(cartella_ordine, nome)
            try:
                imp = _impronta_file(orig if os.path.isfile(orig) else percorso)
            except OSError:
                imp = percorso
            if imp in impronte:
                continue
            impronte.add(imp)
            tenuti.append((percorso, arc, nome))
        if len(tenuti) == 1:
            p_, _a, nome = tenuti[0]
            out.append((p_, f'{cartella}/{_nome_base_disegno(nome)}'))
        else:
            out.extend((p_, a_) for p_, a_, _n in tenuti)
    return out


_TEMPO_CACHE: dict = {}
_CFG_TEMPO = {'t': 0.0, 'cfg': None}


def _cfg_per_tempi() -> dict:
    """Configurazione laser (velocita' delle ricette) riletta al massimo ogni 30 s."""
    if time.time() - _CFG_TEMPO['t'] > 30 or _CFG_TEMPO['cfg'] is None:
        _CFG_TEMPO['cfg'] = ConfigManager.load_config() or {}
        _CFG_TEMPO['t'] = time.time()
    return _CFG_TEMPO['cfg']


def _tempo_pezzo_laser(a: dict, tempo_preventivo) -> dict:
    """Tempo di taglio di un pezzo per il calendario del laser.

    'preventivo': stimato dal preventivatore; 'calcolato': ordini che non ci
    passano (pacchetto PDF + DXF), stesso calcolo SENZA prezzo;
    'da_verificare': calcolato ma col contorno incerto; 'mancante': non
    calcolabile (niente perimetro, spessore o materiale)."""
    incerto = _contorno_incerto(a, {})
    if tempo_preventivo:
        return {'tempo_min': tempo_preventivo, 'tempo_fonte': 'da_verificare' if incerto else 'preventivo'}
    chiave = tuple(str(a.get(k)) for k in ('materiale', 'spessore_mm', 'perimetro_taglio_m', 'n_forature',
                                             'area_dm2', 'bbox_w_mm', 'bbox_h_mm', 'lunghezza_vuoto_mm'))
    if chiave not in _TEMPO_CACHE:
        try:
            _TEMPO_CACHE[chiave] = _laser_estimator.tempo_taglio_min(a, _cfg_per_tempi())
        except Exception as e:
            _TEMPO_CACHE[chiave] = (None, str(e))
    t, motivo = _TEMPO_CACHE[chiave]
    if t is None:
        return {'tempo_min': None, 'tempo_fonte': 'mancante', 'tempo_motivo': motivo}
    return {'tempo_min': t, 'tempo_fonte': 'da_verificare' if incerto else 'calcolato'}


def _controllo_pezzo(a: dict) -> dict | None:
    """Cosa resta da verificare su un pezzo prima di crearlo in Lantek
    (verifica automatica al caricamento dell'ordine): None se niente.
    {'stato': 'da_confermare'|'da_guardare', 'motivi': [...]}"""
    motivi, stato = [], None
    ab = a.get('abbinamento') or {}
    if a.get('dxf_filename') and ab.get('tipo') == 'somiglianza' and not ab.get('confermato'):
        motivi.append(f"disegno «{a.get('dxf_filename')}» abbinato al codice solo per somiglianza")
        stato = 'da_confermare'
    ev = a.get('esito_verifica') or {}
    if ev.get('stato') in ('da_guardare', 'corretto_da_confermare'):
        motivi.extend(m for m in (ev.get('motivi') or []) if m)
        stato = 'da_guardare' if ev['stato'] == 'da_guardare' else (stato or 'da_confermare')
    ca = a.get('contorno_auto') or {}
    if ca.get('stato') == 'da_confermare' and ev.get('stato') != 'confermato':
        if not any('contorno' in m for m in motivi):
            motivi.append('contorno corretto in automatico col peso del cartiglio')
        stato = stato or 'da_confermare'
    return {'stato': stato, 'motivi': motivi[:6]} if stato else None


def _distinta_da_preventivo(prev: dict, disegni_per_nome: dict) -> list:
    """Le righe della distinta di un ordine nato da un preventivo.

    Le quantita' seguono le stesse regole del calcolo del prezzo
    (preventivi/calcolo.py):
      - articolo sciolto: quantita' x quantita' del lotto
      - articolo, tubolare o piastra dentro un assieme: quantita' x pezzi
        dell'assieme (la riga descrive UN assieme)
      - tubolare o piastra sciolti: la riga e' gia' il totale (qty, o 1)
    """
    def _q(v, default=1):
        try:
            n = int(round(float(v)))
            return n if n > 0 else default
        except (TypeError, ValueError):
            return default

    def _f(v):
        try:
            x = float(v)
            return x if x > 0 else None
        except (TypeError, ValueError):
            return None

    def _geometria(area_dm2, w, h, stima):
        """Ingombro del pezzo per il banco lamiere del laser.

        'disegno': larghezza x altezza lette dal DXF nel preventivatore.
        'stimato': c'e' solo l'area (pezzi vecchi, piastre da STEP): si usa il
        quadrato di pari area, e il laser lo vede segnato come stima.
        """
        area = _f(area_dm2)
        w, h = _f(w), _f(h)
        if w and h:
            fonte = 'disegno'
        elif area:
            w = h = round((area * 10000) ** 0.5, 1)
            fonte = 'stimato'
        else:
            fonte = 'mancante'
        tempo = _f((stima or {}).get('tempo_totale_min')) if isinstance(stima, dict) else None
        return {'area_dm2': area, 'bbox_w_mm': w, 'bbox_h_mm': h,
                'ingombro': fonte, 'tempo_min': tempo}

    lotto = _q(prev.get('quantita'))
    qta_assieme = {a.get('codice_assieme'): _q(a.get('qty'))
                   for a in (prev.get('assiemi') or []) if a.get('codice_assieme')}
    righe = []

    def disegno_di(nome):
        if not nome:
            return None
        d = disegni_per_nome.get(os.path.basename(nome).lower())
        return d['nome'] if d else None

    for a in prev.get('assiemi') or []:
        cod = a.get('codice_assieme')
        if not cod:
            continue
        lav = []
        if float(a.get('ore_montaggio') or 0) > 0 or float(a.get('costo') or 0) > 0:
            lav.append('Montaggio')
        if float(a.get('ore_puntatura') or 0) > 0:
            lav.append('Puntatura')
        if float(a.get('saldatura_mt') or 0) > 0 or float(a.get('costo_saldatura_assieme') or 0) > 0:
            lav.append('Saldatura')
        righe.append({'codice': cod, 'descrizione': 'Assieme', 'tipo': 'assieme',
                      'quantita': qta_assieme.get(cod, 1), 'materiale': None,
                      'spessore_mm': None, 'lavorazioni': lav, 'assieme': None,
                      'disegno': None})

    for a in prev.get('articoli') or []:
        cod_ass = a.get('codice_assieme') or None
        molt = qta_assieme.get(cod_ass, 1) if cod_ass else lotto
        lav = ['Taglio laser']
        if int(a.get('pieghe') or 0) > 0:
            lav.append(f"Piegatura ({int(a['pieghe'])} pieghe)")
        if float(a.get('saldatura_ml') or 0) > 0 or float(a.get('saldatura_min') or 0) > 0:
            lav.append('Saldatura')
        if int(a.get('filettatura_pz') or 0) > 0:
            lav.append(f"Filettatura ({int(a['filettatura_pz'])})")
        if int(a.get('svasatura_pz') or 0) > 0:
            lav.append(f"Svasatura ({int(a['svasatura_pz'])})")
        sp = a.get('spessore_mm')
        desc = 'Lamiera' + (f' sp. {sp:g} mm' if isinstance(sp, (int, float)) and sp else '')
        geo = _geometria(a.get('area_dm2'), a.get('bbox_w_mm'), a.get('bbox_h_mm'),
                         a.get('stima_dettaglio'))
        geo.update(_tempo_pezzo_laser(a, geo.get('tempo_min')))
        righe.append({'codice': a.get('codice') or '', 'descrizione': desc,
                      'tipo': 'lamiera', 'quantita': _q(a.get('quantita')) * molt,
                      'materiale': a.get('materiale') or None, 'spessore_mm': sp,
                      'lavorazioni': lav, 'assieme': cod_ass,
                      'disegno': disegno_di(a.get('dxf_filename')),
                      'articolo_id': a.get('id'),
                      'controllo': _controllo_pezzo(a),
                      **geo})

    for t in prev.get('tubolari') or []:
        cod_ass = t.get('codice_assieme') or None
        n = _q(t.get('qty'))
        if cod_ass:
            n *= qta_assieme.get(cod_ass, 1)
        lung = t.get('lunghezza_pezzo_mm')
        if not lung and t.get('lunghezza_m'):
            lung = round(float(t['lunghezza_m']) * 1000)
        desc = (t.get('profilo') or 'Tubolare') + (f' L={lung:g} mm' if lung else '')
        lav = []
        if int(t.get('n_tagli_dritti') or 0) > 0:
            lav.append(f"Taglio dritto ({int(t['n_tagli_dritti'])})")
        if int(t.get('n_tagli_obliqui') or 0) > 0:
            lav.append(f"Taglio obliquo ({int(t['n_tagli_obliqui'])})")
        righe.append({'codice': t.get('posizione') or t.get('profilo') or '',
                      'descrizione': desc, 'tipo': 'tubolare', 'quantita': n,
                      'materiale': t.get('materiale') or None, 'spessore_mm': None,
                      'lavorazioni': lav or ['Taglio'], 'assieme': cod_ass,
                      'disegno': None})

    for pl in prev.get('piastre') or []:
        cod_ass = pl.get('codice_assieme') or None
        n = qta_assieme.get(cod_ass, 1) if cod_ass else 1
        sp = pl.get('spessore_mm')
        righe.append({'codice': f'Piastra sp. {sp:g}' if isinstance(sp, (int, float)) and sp else 'Piastra',
                      'descrizione': 'Piastra' + (f" {float(pl['area_dm2']):g} dm2" if pl.get('area_dm2') else ''),
                      'tipo': 'piastra', 'quantita': n,
                      'materiale': pl.get('materiale') or None, 'spessore_mm': sp,
                      'lavorazioni': ['Taglio'], 'assieme': cod_ass, 'disegno': None,
                      **_geometria(pl.get('area_dm2'), None, None, None)})
    return righe


@app.route('/api/orders/<order_id>/distinta', methods=['GET'])
@richiede(*OFFICINA_LETTURA)
def api_ordine_distinta(order_id):
    """Distinta dei pezzi dell'ordine.

    Un ordine nato da un preventivo non ha articoli suoi: stanno sul
    preventivo d'origine (orders.preventivo_id_origine), e da li' si
    ricostruiscono, ognuno col suo disegno quando c'e'.
    """
    try:
        order = _ordine_esistente(order_id)
        if not order:
            return _non_trovato_ordine()
        righe, origine, prev = _distinta_ordine(order)
        totale = sum(r['quantita'] for r in righe if r.get('tipo') != 'assieme')
        return jsonify({'success': True, 'origine': origine, 'righe': righe,
                        'totale_pezzi': totale,
                        'preventivo_id': order.preventivo_id_origine if prev else None}), 200
    except Exception as e:
        logger.exception('distinta ordine fallita')
        return jsonify({'success': False, 'error': str(e)}), 500


def _distinta_ordine(order, disegni=None):
    """(righe, origine, preventivo) della distinta di un ordine.

    disegni: l'elenco di _disegni_ordine se chi chiama ce l'ha gia' (il
    tablet lo usa anche lui): cercarli costa, non si cercano due volte."""
    if disegni is None:
        disegni = _disegni_ordine(order)
    disegni_per_nome = {d['nome'].lower(): d for d in disegni}
    righe, origine = [], 'nessuna'
    prev = None
    if order.preventivo_id_origine:
        prev = PreventivoManager.get(order.preventivo_id_origine, include_children=True)
    if prev:
        righe = _distinta_da_preventivo(prev, disegni_per_nome)
        # Ordine caricato dall'ufficio col pacchetto del cliente: i pezzi
        # stanno su un preventivo tecnico, ma vengono dall'ordine del cliente.
        origine = 'pacchetto' if prev.get('solo_tecnico') else 'preventivo'
    else:
        # Ordini a mano: se un domani avranno articoli propri, si usano.
        propri = getattr(order, 'articles', None) or []
        if isinstance(propri, list) and propri:
            origine = 'ordine'
            for a in propri:
                if not isinstance(a, dict):
                    continue
                nome_dis = a.get('disegno') or a.get('dxf_filename')
                d = disegni_per_nome.get(os.path.basename(nome_dis).lower()) if nome_dis else None
                righe.append({
                    'codice': a.get('codice') or '',
                    'descrizione': a.get('descrizione') or '',
                    'tipo': 'articolo',
                    'quantita': int(a.get('quantita') or 1),
                    'materiale': a.get('materiale') or None,
                    'spessore_mm': a.get('spessore_mm'),
                    'lavorazioni': list(a.get('lavorazioni') or []),
                    'assieme': a.get('assieme') or None,
                    'disegno': d['nome'] if d else None,
                })
    return righe, origine, prev


# ============ TABLET OFFICINA ============
#
# Il tablet appeso in officina chiedeva l'elenco degli ordini e poi, per ogni
# ordine aperto, distinta e disegni: per sapere quali ordini servono alla piega
# o quanto e' tagliato avrebbe dovuto fare decine di richieste a ogni giro.
# Qui c'e' UNA richiesta con tutto quello che il tablet mostra, gia' tradotto
# nella lingua dell'officina (reparti, pieghe, stato del taglio).

def _chi_tablet() -> dict:
    """Chi guarda il tablet: il nome del dispositivo (es. "Tablet piega"), o la
    persona se la pagina e' aperta da un PC d'ufficio. Chi puo' farlo lo
    decide la regola della rotta, non questa funzione."""
    ident = identita()
    nome = ident.persona['nome'] if ident.persona else (
        (ident.dispositivo or {}).get('label') or ident.nome)
    return {'id': _chi() or None, 'nome': nome}


def _file_step(preventivo_id) -> list:
    """Nomi dei file STEP del preventivo d'origine (vuoto se non ce ne sono).

    Solo id fatti come un UUID (come _cartella_preventivo): bastano a
    restare dentro preventivi_tmp, senza risolvere il percorso per ogni ordine."""
    if not _RX_UUID.match(str(preventivo_id or '')):
        return []
    cart = os.path.join(UPLOAD_FOLDER, 'preventivi_tmp', str(preventivo_id))
    return [n for n, _p, _d in _elenco_cartella(cart) if n.lower().endswith(('.step', '.stp'))]


def _step_per_assieme(codice: str, files: list):
    """Lo STEP dell'assieme, con le stesse regole del preventivatore
    (_stepPerAssieme in preventivi.html): nome uguale, poi stesso codice
    (prima parola), poi stesso codice senza la revisione finale (-00)."""
    def nome(s):
        return re.sub(r'\.[a-z0-9]{2,4}$', '', str(s or ''), flags=re.IGNORECASE).strip().lower()

    def cod(s):
        return (nome(s).split() or [''])[0]

    def senza_rev(s):
        return re.sub(r'-\d{1,3}$', '', cod(s))
    if not nome(codice):
        return None
    for uguale in (lambda f: nome(f) == nome(codice), lambda f: cod(f) == cod(codice),
                   lambda f: senza_rev(f) == senza_rev(codice)):
        trovato = next((f for f in files if uguale(f)), None)
        if trovato:
            return trovato
    return None


def _segnalazioni_di_oggi(order_ids) -> dict:
    """{order_id: [segnalazioni di oggi]} lette dall'audit, dalla piu' recente."""
    from .models import AuditLog
    from . import tablet_officina as tab
    if not order_ids:
        return {}
    inizio, _fine = giorno_locale_in_utc()
    session = get_session()
    try:
        righe = session.query(AuditLog).filter(
            AuditLog.action == tab.AZIONE_AUDIT,
            AuditLog.entity_id.in_(list(order_ids)),
            AuditLog.timestamp >= inizio,
        ).order_by(AuditLog.timestamp.desc()).all()
        out = {}
        for r in righe:
            s = tab.leggi_segnalazione(r.detail)
            if s:
                s['quando'] = iso_utc(r.timestamp)
                out.setdefault(r.entity_id, []).append(s)
        return out
    finally:
        session.close()


def _ordine_per_tablet(o, segnalazioni: dict, piatti=None) -> dict:
    """Un ordine come lo mostra il tablet: testata, reparti, taglio, pezzi."""
    from .ordini_service import stato_taglio, fase_laser
    from . import tablet_officina as tab
    disegni = _disegni_ordine(o, piatti=piatti, righe_file=list(o.files or []))
    righe, origine, prev = _distinta_ordine(o, disegni)
    st = stato_taglio(o)
    stati, taglio = tab.stato_taglio_pezzi(o, righe, st, tab.tagliati_da_lantek(o, righe))
    steps = _file_step(o.preventivo_id_origine) if prev else []
    n_assiemi = sum(1 for r in righe if r.get('tipo') == 'assieme')

    conteggi = {k: {'pezzi': 0, 'codici': 0} for k, _ in tab.REPARTI}
    pezzi = []
    for r, t in zip(righe, stati):
        reparti = tab.reparti_di(r.get('lavorazioni'), r.get('tipo'))
        q = int(r.get('quantita') or 0)
        for k in reparti:
            conteggi[k]['pezzi'] += q
            conteggi[k]['codici'] += 1
        dis = r.get('disegno')
        url_dis = _url_disegno(o.id, dis) if dis else None
        dxf = bool(dis and dis.lower().endswith('.dxf'))
        pieghe = tab.pieghe_di(r.get('lavorazioni'))
        url_step = None
        if r.get('tipo') == 'assieme' and steps:
            f = _step_per_assieme(r.get('codice'), steps)
            # Un solo assieme e un solo STEP: e' quello, anche col nome diverso.
            if not f and n_assiemi == 1 and len(steps) == 1:
                f = steps[0]
            if f:
                url_step = '/api/preventivi/%s/step/%s' % (o.preventivo_id_origine, quote(f))
        pezzi.append({
            'codice': r.get('codice') or '',
            'descrizione': r.get('descrizione') or '',
            'tipo': r.get('tipo'),
            'materiale': r.get('materiale'),
            'spessore_mm': r.get('spessore_mm'),
            'quantita': q,
            'pieghe': pieghe,
            'lavorazioni': tab.lavorazioni_officina(r.get('lavorazioni')),
            'reparti': reparti,
            'assieme': r.get('assieme'),
            'disegno': dis,
            'url_svg': (url_dis + '/svg') if dxf else None,
            # Il piegato 3D si ricostruisce dal DXF sviluppato: solo lamiere
            # che piegano e che hanno il disegno.
            'url_piega_3d': ('%s/fold-model?spessore=%s' % (url_dis, '%g' % r['spessore_mm'] if r.get('spessore_mm') else '')
                             if dxf and pieghe > 0 else None),
            'url_step': url_step,
            'taglio': t,
        })

    num_cli = ((prev or {}).get('numero_ordine_cliente') or '').strip()
    pdf = next((f for f in (o.files or []) if f.file_type == 'PDF'), None)
    return {
        'id': o.id,
        'cliente': o.cliente or '',
        # il numero che conosce l'officina: quello dell'ordine del cliente
        # (come il laser); il nostro PREV-... resta in numero_ordine
        'numero': num_cli or o.numero_ordine or o.id[:8],
        'numero_ordine': o.numero_ordine or '',
        'numero_ordine_cliente': num_cli or None,
        # data di calendario e basta: la consegna e' un giorno, non un istante
        'data_consegna': (iso_data(o.data_consegna) or '')[:10] or None,
        'note': (o.note or '').strip(),
        'lotto_numero': o.lotto_numero or 0,
        'lotto_nome': o.lotto_nome or '',
        'origine': origine,
        'stato_taglio': st,
        'fase_laser': fase_laser(o),
        'taglio_completato': bool(o.taglio_completato),
        'taglio_richiesto': o.taglio_richiesto,
        'importato_lantek_il': iso_utc(getattr(o, 'importato_lantek_il', None)),
        'data_taglio_completato': iso_utc(getattr(o, 'data_taglio_completato', None)),
        'taglio': taglio,
        'reparti': [{'reparto': k, 'etichetta': e, **conteggi[k]}
                    for k, e in tab.REPARTI if conteggi[k]['codici']],
        'pezzi_totali': sum(p['quantita'] for p in pezzi if p['tipo'] != 'assieme'),
        'n_codici': len(pezzi),
        'n_disegni': len(disegni),
        'disegni': [{'nome': d['nome'],
                     'url_svg': (_url_disegno(o.id, d['nome']) + '/svg')
                     if d['nome'].lower().endswith('.dxf') else None} for d in disegni],
        'has_pdf': bool(pdf and pdf.filepath and os.path.exists(pdf.filepath)),
        'pezzi': pezzi,
        'segnalazioni_oggi': segnalazioni.get(o.id, []),
    }


@app.route('/api/officina/tablet', methods=['GET'])
@richiede('reparto', 'ufficio', 'laser')
def api_officina_tablet():
    """Tutto quello che mostra il tablet dell'officina, in una richiesta.

    Gli ordini sono quelli che il tablet mostrava gia': non eliminati, non
    archiviati e ancora "aperti" (non finiti, non consegnati), dalla consegna
    piu' vicina. Sola lettura.
    """
    chi = _chi_tablet()
    try:
        from sqlalchemy.orm import selectinload
        from .ordini_service import STATI_ARCHIVIO, fase as fase_ordine
        t0 = time.perf_counter()
        session = get_session()
        try:
            ordini = session.query(Order).options(selectinload(Order.files)).filter(
                Order.is_deleted == False,  # noqa: E712
                (Order.status.is_(None)) | (~Order.status.in_(STATI_ARCHIVIO)),
            ).order_by(Order.data_consegna.asc()).all()
            ordini = [o for o in ordini if fase_ordine(o) == 'aperto']
            segnalazioni = _segnalazioni_di_oggi([o.id for o in ordini])
            piatti = _elenco_cartella(DRAWINGS_FOLDER)   # una volta, non per ogni ordine
            out = [_ordine_per_tablet(o, segnalazioni, piatti) for o in ordini]
        finally:
            session.close()
        ms = round((time.perf_counter() - t0) * 1000)
        if ms > 1500:
            logger.warning('tablet officina: %d ordini in %d ms', len(out), ms)
        return jsonify({'success': True, 'ordini': out, 'oggi': oggi_locale().isoformat(),
                        'dispositivo': chi.get('nome'), 'ms': ms}), 200
    except Exception as e:
        logger.exception('tablet officina fallito')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/orders/<order_id>/segnalazioni', methods=['POST'])
@richiede('reparto', 'ufficio', 'laser')
def api_segnala_problema(order_id):
    """"Segnala un problema" dal tablet dell'officina: arriva all'ufficio.

    Corpo: {tipo, nota?, codice_pezzo?, da?}; tipo fra quelli di
    tablet_officina.TIPI_SEGNALAZIONE. Diventa una notifica per ogni utenza
    d'ufficio attiva (come "Lavoro finito: pronto per DDT") e una riga
    d'audit. Lo stesso problema sullo stesso pezzo entro 10 minuti non si
    ripete: risponde {success, gia_segnalato: true}.
    """
    from . import tablet_officina as tab
    from .models import AuditLog
    chi = _chi_tablet()
    try:
        data = request.get_json(silent=True)
        data = data if isinstance(data, dict) else {}
        tipo = str(data.get('tipo') or '').strip()
        if tipo not in tab.TIPI_SEGNALAZIONE:
            return jsonify({'success': False, 'codice': 'tipo_non_valido',
                            'error': 'Tipo di problema non valido',
                            'tipi': list(tab.TIPI_SEGNALAZIONE)}), 400

        def pulisci(v):
            # " | " separa i campi nella riga d'audit: dentro i testi diventa "/"
            return ' '.join(str(v or '').replace('|', '/').split())
        nota = pulisci(data.get('nota'))
        codice = pulisci(data.get('codice_pezzo'))
        da = pulisci(data.get('da'))[:tab.MAX_DA]
        if len(nota) > tab.MAX_NOTA:
            return jsonify({'success': False, 'codice': 'nota_lunga',
                            'error': 'Nota troppo lunga (al massimo %d caratteri)' % tab.MAX_NOTA}), 400
        if len(codice) > tab.MAX_CODICE:
            return jsonify({'success': False, 'codice': 'pezzo_non_valido',
                            'error': 'Codice del pezzo non valido'}), 400
        if tipo == 'altro' and not nota:
            return jsonify({'success': False, 'codice': 'nota_mancante',
                            'error': 'Per "Altro" scrivi due parole: cosa succede?'}), 400
        order = _ordine_esistente(order_id)
        if not order or getattr(order, 'is_deleted', False):
            return _non_trovato_ordine()
        righe, _origine, prev = _distinta_ordine(order)
        if codice and righe and codice not in {(r.get('codice') or '') for r in righe}:
            return jsonify({'success': False, 'codice': 'pezzo_non_valido',
                            'error': 'Il pezzo non fa parte di quest\'ordine'}), 400

        chiave = tab.chiave_segnalazione(tipo, codice)
        session = get_session()
        try:
            dal = datetime.utcnow() - timedelta(minutes=tab.MINUTI_DOPPIONE)
            recenti = session.query(AuditLog.detail).filter(
                AuditLog.action == tab.AZIONE_AUDIT, AuditLog.entity_id == order_id,
                AuditLog.timestamp >= dal).all()
        finally:
            session.close()
        if any((d or '').startswith(chiave) for (d,) in recenti):
            return jsonify({'success': True, 'gia_segnalato': True}), 200

        from .models import RUOLI_UFFICIO
        etichetta = tab.TIPI_SEGNALAZIONE[tipo]
        numero = (((prev or {}).get('numero_ordine_cliente') or '').strip()
                  or order.numero_ordine or order.id[:8])
        mittente = da or chi.get('nome') or 'tablet officina'
        messaggio = f'Ordine #{numero} ({order.cliente or "cliente ?"}): {etichetta.lower()}'
        if codice:
            messaggio += f', pezzo {codice}'
        if nota:
            messaggio += f'. "{nota}"'
        messaggio += f'. Segnalato da {mittente}.'
        avvisati = 0
        for u in UserManager.get_all_users() or []:
            # Gli avvisi sono della STAZIONE (la campanella dell'Ufficio), non
            # delle singole persone che ci entrano col PIN.
            if u.get('is_active', True) and u.get('e_postazione') and u.get('role') in RUOLI_UFFICIO:
                if NotificationManager.create_notification(
                        user_id=u['id'], order_id=order_id,
                        title=f'Officina: {etichetta.lower()}',
                        message=messaggio, notification_type='order',
                        notification_category='attiva'):
                    avvisati += 1
        AuditManager.log(user_id=chi.get('id'), user_name=chi.get('nome'),
                         action=tab.AZIONE_AUDIT, entity_type='order', entity_id=order_id,
                         detail=f'{chiave} nota: {nota or "-"} | da: {mittente}'[:2000],
                         ip_address=request.remote_addr)
        return jsonify({'success': True, 'gia_segnalato': False, 'avvisati': avvisati,
                        'tipo': tipo, 'etichetta': etichetta}), 201
    except Exception as e:
        logger.exception('segnalazione officina fallita')
        return jsonify({'success': False, 'error': str(e)}), 500


_PIEGA_CACHE: dict = {}


@app.route('/api/orders/<order_id>/dxf/<filename>/fold-model', methods=['GET'])
@richiede(*OFFICINA_LETTURA)
def api_ordine_dxf_piega(order_id, filename):
    """Il pezzo piegato in 3D, dal disegno dell'ordine (per il tablet).

    Sola lettura: lo stesso calcolo del preventivatore, sul DXF dell'ordine.
    ?spessore=<mm>. Il risultato si tiene in memoria finche' il file non
    cambia: ricostruire il contorno costa qualche secondo.
    """
    try:
        order = _ordine_esistente(order_id)
        if not order:
            return _non_trovato_ordine()
        d = _trova_disegno(order, filename)
        if not d or not d['nome'].lower().endswith('.dxf'):
            return jsonify({'success': False, 'codice': 'disegno_assente',
                            'error': 'Disegno non trovato per quest\'ordine'}), 404
        try:
            sp = float(request.args.get('spessore') or 0) or 2.0
        except ValueError:
            sp = 2.0
        chiave = (d['percorso'], os.path.getmtime(d['percorso']), sp)
        if chiave not in _PIEGA_CACHE:
            from .preventivi.dxf_scanner import estrai_pieghe_3d
            cfg = (ConfigManager.load_config() or {}).get('dxf_detection', {})
            pieghe = estrai_pieghe_3d(d['percorso'], cfg)
            if len(_PIEGA_CACHE) > 200:
                _PIEGA_CACHE.clear()
            _PIEGA_CACHE[chiave] = _modello_piega(d['percorso'], pieghe, None, sp, cfg)
        return jsonify(_PIEGA_CACHE[chiave]), 200
    except Exception as e:
        logger.exception('piega 3D ordine fallita')
        return jsonify({'success': False, 'error': str(e)}), 500


# Formati di lamiera proposti al laser (mm). Si cambiano da app_config.json,
# chiave "laser_formati_lamiera": [[3000, 1500], [2500, 1250], ...].
_FORMATI_LAMIERA = ((3000, 1500), (2500, 1250), (2000, 1000))


def _formati_lamiera():
    try:
        cfg = (ConfigManager.load_config() or {}).get('laser_formati_lamiera')
        formati = [(float(w), float(h)) for w, h in (cfg or []) if float(w) > 0 and float(h) > 0]
    except (TypeError, ValueError):
        formati = []
    return [{'nome': '%g × %g' % (w, h), 'w_mm': w, 'h_mm': h}
            for w, h in (formati or _FORMATI_LAMIERA)]


@app.route('/api/laser/banco', methods=['GET'])
@richiede('laser', 'ufficio')
def api_laser_banco():
    """Banco lamiere: i pezzi da tagliare raggruppati per materiale e spessore.

    In macchina non si taglia un ordine alla volta: si carica una lamiera e ci
    si mettono i pezzi di piu' ordini. Qui ogni gruppo e' una lamiera
    (materiale + spessore) con i pezzi di tutti gli ordini in coda, ognuno col
    suo ingombro: la pagina li dispone sui fogli e dice quanti ne servono.

    ?da_smistare=1 aggiunge gli ordini che il laser non ha ancora smistato.
    Gli ordini senza distinta (caricati a mano col solo PDF) sono elencati a
    parte: per loro fa fede il PDF.
    """
    try:
        from .ordini_service import stato_taglio, STATI_ARCHIVIO
        con_smistare = request.args.get('da_smistare') in ('1', 'true', 'si')
        stati_ok = ('da_tagliare', 'da_smistare') if con_smistare else ('da_tagliare',)
        # ?dal=AAAA-MM-GG (calendario): anche gli ordini tagliati da quel giorno,
        # perche' il calendario mostri quanto e' stato fatto nei giorni passati.
        dal = None
        if request.args.get('dal'):
            try:
                dal = datetime.strptime(request.args['dal'][:10], '%Y-%m-%d')
            except ValueError:
                return jsonify({'success': False, 'error': 'dal: serve AAAA-MM-GG'}), 400
        session = get_session()
        try:
            ordini = session.query(Order).filter(
                Order.is_deleted == False,  # noqa: E712
                (Order.status.is_(None)) | (~Order.status.in_(STATI_ARCHIVIO)),
                (Order.taglio_completato.is_(None)) | (Order.taglio_completato == False),  # noqa: E712
            ).order_by(Order.data_consegna.asc()).all()
            ordini = [o for o in ordini if stato_taglio(o) in stati_ok]
            if dal is not None:
                ordini += session.query(Order).filter(
                    Order.is_deleted == False,  # noqa: E712
                    Order.taglio_completato == True,  # noqa: E712
                    Order.data_taglio_completato >= dal).all()
            gruppi, senza = {}, []
            for o in ordini:
                righe, origine, _prev = _distinta_ordine(o)
                # il numero che conosce l'officina: quello dell'ordine del cliente
                num_cli = ((_prev or {}).get('numero_ordine_cliente') or '').strip()
                info = {'ordine_id': o.id, 'numero_ordine': num_cli or o.numero_ordine or '',
                        'cliente': o.cliente or '', 'stato_taglio': stato_taglio(o),
                        'data_consegna': iso_data(o.data_consegna)}
                lamiere = [r for r in righe if r.get('tipo') in ('lamiera', 'piastra')]
                if not lamiere:
                    senza.append({**info, 'motivo': 'solo PDF, nessuna distinta'
                                  if origine == 'nessuna' else 'nessuna lamiera da tagliare'})
                    continue
                for r in lamiere:
                    mat = (r.get('materiale') or '').strip().upper() or 'MATERIALE ?'
                    sp = r.get('spessore_mm')
                    try:
                        sp = round(float(sp), 2) if sp else None
                    except (TypeError, ValueError):
                        sp = None
                    chiave = '%s|%s' % (mat, sp if sp is not None else '?')
                    g = gruppi.setdefault(chiave, {
                        'chiave': chiave, 'materiale': mat, 'spessore_mm': sp, 'pezzi': []})
                    disegno = r.get('disegno')
                    g['pezzi'].append({
                        **info,
                        'codice': r.get('codice') or '', 'descrizione': r.get('descrizione') or '',
                        'quantita': int(r.get('quantita') or 1),
                        'w_mm': r.get('bbox_w_mm'), 'h_mm': r.get('bbox_h_mm'),
                        'area_dm2': r.get('area_dm2'), 'ingombro': r.get('ingombro') or 'mancante',
                        'tempo_min': r.get('tempo_min'),
                        'tempo_fonte': r.get('tempo_fonte') or ('preventivo' if r.get('tempo_min') else 'mancante'),
                        'url_svg': ('/api/orders/%s/dxf/%s/svg' % (o.id, quote(disegno))
                                    if disegno and disegno.lower().endswith('.dxf') else None),
                    })
        finally:
            session.close()

        out = []
        for g in gruppi.values():
            pz = g['pezzi']
            q = lambda p: p['quantita']  # noqa: E731
            consegne = [p['data_consegna'] for p in pz if p['data_consegna']]
            out.append({
                **g,
                'n_ordini': len({p['ordine_id'] for p in pz}),
                'n_codici': len(pz),
                'n_pezzi': sum(q(p) for p in pz),
                'area_dm2': round(sum((p['area_dm2'] or 0) * q(p) for p in pz), 2),
                'tempo_min': round(sum((p['tempo_min'] or 0) * q(p) for p in pz), 1),
                'n_ingombro_stimato': sum(1 for p in pz if p['ingombro'] == 'stimato'),
                'n_ingombro_mancante': sum(1 for p in pz if p['ingombro'] == 'mancante'),
                'consegna_prima': min(consegne) if consegne else None,
            })
        # La lamiera piu' urgente per prima; a parita', la piu' grossa.
        out.sort(key=lambda g: (g['consegna_prima'] or '9999', -g['area_dm2']))
        return jsonify({'success': True, 'gruppi': out, 'senza_distinta': senza,
                        'formati': _formati_lamiera(),
                        'parametri': {'margine_mm': 10, 'distanza_mm': 5}}), 200
    except Exception as e:
        logger.exception('banco lamiere fallito')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/orders', methods=['GET'])
@richiede('commerciale', 'ufficio', 'laser')
def get_orders():
    """Recupera lista ordini con filtri per il nuovo workflow"""
    try:
        cliente = request.args.get('cliente')
        status = request.args.get('status')
        fase_corrente = request.args.get('fase_corrente')
        operatore = request.args.get('operatore')
        # ?aperti=1: senza le pratiche archiviate (pagine di reparto).
        solo_aperti = request.args.get('aperti') in ('1', 'true', 'si')
        orders_data = OrderManager.get_all_orders_dict(
            cliente=cliente, status=status,
            fase_corrente=fase_corrente, operatore=operatore,
            solo_aperti=solo_aperti
        )

        # Includi ordini in supporto per l'operatore
        if operatore:
            supported_ids = SupportManager.get_supported_order_ids(operatore)
            if supported_ids:
                existing_ids = {o['id'] for o in orders_data}
                new_ids = [sid for sid in supported_ids if sid not in existing_ids]
                if new_ids:
                    # Carica solo gli ordini supportati mancanti (non tutti)
                    for so in OrderManager.get_orders_by_ids(new_ids):
                        orders_data.append(so)

        return jsonify({'orders': [_senza_prezzi(o) for o in orders_data]}), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ============ API MARK ORDER SEEN ============

@app.route('/api/orders/<order_id>/mark-seen', methods=['POST'])
@richiede('ufficio', 'laser')
def mark_order_seen(order_id):
    """Marca un ordine come visto dall'operatore"""
    try:
        result = OrderManager.mark_order_seen(order_id)
        return jsonify(result), 200 if result.get('success') else 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ============ API ALERTS ============

@app.route('/api/alerts/check', methods=['GET'])
@richiede('ufficio', 'laser')
def check_alerts():
    """Controlla alert automatici (timer lunghi, ordini fermi, scadenze)"""
    try:
        alerts = AlertManager.check_alerts()
        return jsonify({'success': True, 'alerts': alerts, 'count': len(alerts)}), 200
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ============ API KPI DASHBOARD ============

@app.route('/api/kpi/dashboard', methods=['GET'])
@richiede('ufficio', ADMIN)
def get_kpi_dashboard():
    """KPI operatori e fasi per dashboard Capo Officina"""
    try:
        result = KPIManager.get_dashboard_kpi()
        return jsonify(result), 200 if result.get('success') else 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ============ API ADMIN ============

def _giorno_iso(testo):
    """Giorno italiano di un istante ISO (con o senza Z). None se illeggibile."""
    try:
        v = datetime.fromisoformat(str(testo).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    if v.tzinfo is not None:
        from datetime import timezone as _tz
        v = v.astimezone(_tz.utc).replace(tzinfo=None)
        return data_locale(v)
    return v.date()


@app.route('/api/admin/kpi', methods=['GET'])
@richiede('ufficio', ADMIN)
def get_admin_kpi():
    """Recupera KPI sistema per admin dashboard"""
    try:
        from datetime import datetime as dt, timedelta

        all_orders = OrderManager.get_all_orders_dict()
        active_orders = [o for o in all_orders if o.get('status') not in ('COMPLETATO', 'PARZIALE', 'SPEDITO')]
        ordini_attivi = len(active_orders)

        # KPI operai (calcoli reali, nessun mock)
        kpi_operai = AuditManager.get_kpi_operai()

        # Login oggi (giorno italiano: i timestamp arrivano in UTC con la Z)
        today = oggi_locale()
        audit_logs = AuditManager.get_recent(limit=1000)
        login_oggi = len([
            log for log in audit_logs
            if log['action'] == 'LOGIN' and _giorno_iso(log['timestamp']) == today
        ])

        # Efficienza: per ordini COMPLETATI, verifica se ultimo step <= data_consegna
        # Usa il KPI dashboard che ha gia' il calcolo corretto
        kpi_dashboard = KPIManager.get_dashboard_kpi()
        efficienza = kpi_dashboard.get('riepilogo', {}).get('efficienza_puntualita', 0) if kpi_dashboard.get('success') else 0

        # Ritardi: ordini scaduti non completati
        ritardi = sum(1 for o in active_orders if o.get('data_consegna') and dt.fromisoformat(o['data_consegna']).date() < today)

        # Completati oggi (dal KPI dashboard)
        completati_oggi = kpi_dashboard.get('riepilogo', {}).get('completati_oggi', 0) if kpi_dashboard.get('success') else 0

        # Operai online
        operai_online = sum(1 for op in kpi_operai if op.get('saturazione', 0) > 0 or
            (op.get('ultimo_accesso') and op['ultimo_accesso'] != 'Mai' and
             _giorno_iso(op['ultimo_accesso']) == today))

        return jsonify({
            'success': True,
            'kpi_globali': {
                'ordini_attivi': ordini_attivi,
                'login_oggi': login_oggi,
                'efficienza': efficienza,
                'ritardi': ritardi,
                'completati_oggi': completati_oggi,
                'operai_online': operai_online,
                'totale_operai': len(kpi_operai)
            },
            'kpi_operai': kpi_operai
        }), 200

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/admin/audit-log', methods=['GET'])
@richiede(ADMIN)
def get_admin_audit_log():
    """Recupera log di audit per admin dashboard"""
    try:
        # user_id qui e' un FILTRO (di chi vedere le azioni), non un'identita'
        limit = min(request.args.get('limit', 100, type=int), 500)
        user_id = request.args.get('user_id', None)

        audit_logs = AuditManager.get_recent(limit=limit, user_id=user_id)

        return jsonify({
            'success': True,
            'audit_logs': audit_logs,
            'count': len(audit_logs)
        }), 200

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

# ============ API FATTURAZIONE (Chiusura Amministrativa) ============

@app.route('/api/ordini-da-fatturare', methods=['GET'])
@richiede('ufficio')
def get_ordini_da_fatturare():
    """Ordini consegnati e non ancora fatturati.

    E' lo STESSO insieme della vista "consegnati / da fatturare" di
    /api/ordini/viste: prima qui finivano anche ordini mai consegnati.
    """
    try:
        page = max(1, request.args.get('page', 1, type=int))
        limit = min(request.args.get('limit', 20, type=int), 100)
        sort_by = request.args.get('sort_by', 'data_consegna')
        sort_dir = request.args.get('sort_dir', 'asc')

        filters = {}
        if request.args.get('cliente'):
            filters['cliente'] = request.args.get('cliente')
        if request.args.get('numero_ordine'):
            filters['numero_ordine'] = request.args.get('numero_ordine')
        if request.args.get('date_from'):
            filters['date_from'] = request.args.get('date_from')
        if request.args.get('date_to'):
            filters['date_to'] = request.args.get('date_to')

        result = FatturazioneManager.get_ordini_da_fatturare(
            filters=filters if filters else None,
            page=page, limit=limit,
            sort_by=sort_by, sort_dir=sort_dir
        )

        return jsonify({'success': True, 'data': result}), 200

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@app.route('/api/ordini-da-fatturare/count', methods=['GET'])
@richiede('ufficio')
def get_ordini_da_fatturare_count():
    """Conteggio ordini da fatturare (per badge): uguale alla vista "consegnati"."""
    try:
        count = FatturazioneManager.get_count()
        return jsonify({'success': True, 'count': count}), 200
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@app.route('/api/orders/<order_id>/salva-bozza-fattura', methods=['PUT'])
@richiede('ufficio')
def salva_bozza_fattura(order_id):
    """Salva dati DDT/fattura come bozza senza chiudere l'ordine.
    Autorizzato: Impiegata, Capi, Amministratore."""
    try:
        data = request.get_json() or {}
        user_id = _chi()
        result = FatturazioneManager.salva_bozza(order_id, data)
        if not result['success']:
            return jsonify(result), 400
        return jsonify(result), 200
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@app.route('/api/orders/<order_id>/chiudi-amministrativo', methods=['POST'])
@richiede('ufficio')
def chiudi_ordine_amministrativo(order_id):
    """Vecchia chiusura amministrativa, tenuta per compatibilita'.

    Passa dalla stessa chiusura di POST /api/ordini/<id>/chiudi: servono la
    consegna registrata e il numero della fattura. Se nel corpo c'e' un
    numero_ddt e l'ordine non ha ancora un DDT, lo registra prima.
    Corpo: {user_id, numero_fattura, data_fattura?, numero_ddt?, data_ddt?,
    note_chiusura?}. Autorizzato: Impiegata, Capi, Amministratore."""
    try:
        data = request.get_json() or {}
        user_id = _chi()
        result = FatturazioneManager.chiudi_ordine(order_id, data, user_id)
        if not result['success']:
            return jsonify(result), (404 if result.get('codice') == 'non_trovato' else 400)

        # Log audit
        AuditManager.log(
            user_id=user_id,
            action='CHIUSURA_AMMINISTRATIVA',
            entity_type='order',
            entity_id=order_id,
            detail=f"DDT: {data.get('numero_ddt') or data.get('ddt_numero') or '-'}, "
                   f"Fattura: {data.get('numero_fattura', '-')}",
            ip_address=request.remote_addr
        )

        return jsonify(result), 200
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@app.route('/api/orders/<order_id>/riapri', methods=['POST'])
@richiede('ufficio')
def riapri_ordine(order_id):
    """Riapre ordine CHIUSO riportandolo a DA_FATTURARE.
    Autorizzato: Impiegata, Capi, Amministratore."""
    try:
        data = request.get_json() or {}
        user_id = _chi()
        result = FatturazioneManager.riapri_ordine(order_id, user_id)
        if not result['success']:
            return jsonify(result), 400

        AuditManager.log(
            user_id=user_id,
            action='RIAPERTURA_ORDINE',
            entity_type='order',
            entity_id=order_id,
            detail='Ordine riaperto da CHIUSO a DA_FATTURARE',
            ip_address=request.remote_addr
        )

        return jsonify(result), 200
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


# ============ API ARCHIVE ============

@app.route('/api/archive/orders', methods=['GET'])
@richiede(*UFFICI)
def get_archive_orders():
    """Recupera ordini completati con paginazione e filtri"""
    try:
        # Parametri paginazione
        page = max(1, request.args.get('page', 1, type=int))
        limit = min(request.args.get('limit', 10, type=int), 100)
        ALLOWED_SORT = {'data_consegna', 'cliente', 'numero_ordine', 'status'}
        sort_by = request.args.get('sort_by', 'data_consegna')
        if sort_by not in ALLOWED_SORT:
            sort_by = 'data_consegna'
        sort_dir = request.args.get('sort_dir', 'desc')

        # Parametri filtri
        filters = {}
        if request.args.get('cliente'):
            filters['cliente'] = request.args.get('cliente')
        if request.args.get('operatore'):
            filters['operatore'] = request.args.get('operatore')
        if request.args.get('date_from'):
            filters['date_from'] = request.args.get('date_from')
        if request.args.get('date_to'):
            filters['date_to'] = request.args.get('date_to')

        result = ArchiveManager.get_completed_orders(
            filters=filters if filters else None,
            page=page,
            limit=limit,
            sort_by=sort_by,
            sort_dir=sort_dir
        )

        return jsonify({
            'success': True,
            'data': result
        }), 200

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/archive/orders/<order_id>/details', methods=['GET'])
@richiede(*UFFICI)
def get_archive_order_details(order_id):
    """Recupera dettagli completi di un ordine completato"""
    try:
        order_details = ArchiveManager.get_order_details(order_id)
        if not order_details:
            return jsonify({'success': False, 'error': 'Ordine non trovato'}), 404

        return jsonify({
            'success': True,
            'data': order_details
        }), 200

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/archive/export/csv', methods=['GET'])
@richiede(*UFFICI)
def export_archive_csv():
    """Esporta ordini completati come CSV"""
    try:
        import csv
        import io

        # Parametri filtri
        filters = {}
        if request.args.get('cliente'):
            filters['cliente'] = request.args.get('cliente')
        if request.args.get('date_from'):
            filters['date_from'] = request.args.get('date_from')
        if request.args.get('date_to'):
            filters['date_to'] = request.args.get('date_to')

        csv_data = ArchiveManager.export_csv_data(
            filters=filters if filters else None
        )

        if not csv_data:
            return jsonify({'success': False, 'error': 'Nessun dato da esportare'}), 400

        # Crea buffer CSV
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=csv_data[0].keys())
        writer.writeheader()
        writer.writerows(csv_data)

        # Converti in bytes
        csv_bytes = output.getvalue().encode('utf-8-sig')

        return csv_bytes, 200, {
            'Content-Type': 'text/csv; charset=utf-8',
            'Content-Disposition': 'attachment; filename=archivio-ordini.csv'
        }

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/archive/export/excel', methods=['GET'])
@richiede(*UFFICI)
def export_archive_excel():
    """Esporta ordini completati come Excel (una riga per fase)"""
    try:
        import io
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

        filters = {}
        if request.args.get('cliente'):
            filters['cliente'] = request.args.get('cliente')
        if request.args.get('operatore'):
            filters['operatore'] = request.args.get('operatore')
        if request.args.get('date_from'):
            filters['date_from'] = request.args.get('date_from')
        if request.args.get('date_to'):
            filters['date_to'] = request.args.get('date_to')

        rows, summary_indices = ArchiveManager.export_excel_data(filters=filters if filters else None)

        if not rows:
            return jsonify({'success': False, 'error': 'Nessun dato da esportare'}), 400

        wb = Workbook()
        ws = wb.active
        ws.title = "Archivio Ordini"

        headers = list(rows[0].keys())
        header_font = Font(bold=True, color="FFFFFF", size=11)
        header_fill = PatternFill(start_color="1A7A48", end_color="1A7A48", fill_type="solid")
        header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
        thin_border = Border(
            left=Side(style='thin', color='CCCCCC'),
            right=Side(style='thin', color='CCCCCC'),
            top=Side(style='thin', color='CCCCCC'),
            bottom=Side(style='thin', color='CCCCCC')
        )

        # Stili riga riepilogo
        summary_font = Font(bold=True, size=11)
        summary_fill = PatternFill(start_color="E8F5E9", end_color="E8F5E9", fill_type="solid")
        summary_border = Border(
            left=Side(style='thin', color='CCCCCC'),
            right=Side(style='thin', color='CCCCCC'),
            top=Side(style='medium', color='1A7A48'),
            bottom=Side(style='medium', color='1A7A48')
        )

        for col_idx, header in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col_idx, value=header)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_align
            cell.border = thin_border

        # Set di indici riepilogo per lookup veloce
        summary_set = set(summary_indices)

        for row_idx, row_data in enumerate(rows, 2):
            is_summary = (row_idx - 2) in summary_set
            for col_idx, header in enumerate(headers, 1):
                cell = ws.cell(row=row_idx, column=col_idx, value=row_data.get(header, ''))
                if is_summary:
                    cell.font = summary_font
                    cell.fill = summary_fill
                    cell.border = summary_border
                    cell.alignment = Alignment(vertical="center")
                else:
                    cell.border = thin_border
                    cell.alignment = Alignment(vertical="center")

        col_widths = {
            'Cliente': 22, 'Numero Ordine': 16, 'Data Caricamento': 18,
            'Data Completamento': 18, 'Fase': 14,
            'Operatore': 22, 'Ruolo': 12, 'Inizio': 18, 'Fine': 18,
            'Tempo Lavorato': 18, 'Sessioni': 10,
        }
        for col_idx, header in enumerate(headers, 1):
            ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = col_widths.get(header, 15)

        ws.auto_filter.ref = f"A1:{ws.cell(row=1, column=len(headers)).column_letter}{len(rows)+1}"

        output = io.BytesIO()
        wb.save(output)
        output.seek(0)

        return output.getvalue(), 200, {
            'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            'Content-Disposition': 'attachment; filename=archivio-ordini.xlsx'
        }

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/archive/filters', methods=['GET'])
@richiede(*UFFICI)
def get_archive_filters():
    """Ritorna liste per i filtri dell'archivio (clienti e operatori)"""
    try:
        clients = ArchiveManager.get_archive_clients()
        operators = ArchiveManager.get_archive_operators()
        return jsonify({
            'success': True,
            'clienti': clients,
            'operatori': operators
        }), 200
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

# ============ API FILE ============

def _cliente_noto(nome):
    """Il nome del cliente come e' gia' scritto negli ordini ("POLIFORM S.p.A."
    invece di "Poliform"), cosi' riepiloghi e filtri lo riconoscono."""
    if not nome:
        return nome
    try:
        from .ore_service import chiave_cliente
        k = chiave_cliente(nome)
        session = get_session()
        try:
            for (c,) in session.query(Order.cliente).filter(Order.cliente.isnot(None)).distinct():
                if c and chiave_cliente(c) == k:
                    return c
        finally:
            session.close()
    except Exception:
        pass
    return nome


@app.route('/api/extract-pdf-data', methods=['POST'])
@richiede('ufficio')
def extract_pdf_data():
    """Carica il PDF dell'ordine. Se e' un ordine gia' prezzato di un formato
    noto (Poliform, B&B Italia: preventivi/ordine_prezzi.py, a regole, senza AI)
    propone numero, cliente, consegna e valore; l'ufficio controlla e conferma."""
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'Nessun file caricato'}), 400

        file = request.files['file']

        if file.filename == '' or not file.filename.lower().endswith('.pdf'):
            return jsonify({'error': 'Solo file PDF sono supportati'}), 400

        # Salva il file (no parsing)
        safe_name = os.path.basename(file.filename)
        pdf_filename = f"{uuid.uuid4()}_{safe_name}"
        filepath = os.path.join(PDFS_FOLDER, pdf_filename)
        file.save(filepath)

        dati = {'pdf_filename': pdf_filename}
        try:
            from .preventivi.ordine_prezzi import leggi_ordine_prezzato
            with open(filepath, 'rb') as fh:
                letto = leggi_ordine_prezzato(fh.read())
        except Exception:
            logger.exception('lettura prezzi ordine fallita')
            letto = None
        if letto:
            dati.update({
                'numero_ordine': letto.get('numero_ordine'),
                'cliente': _cliente_noto(letto.get('cliente')),
                'data_consegna': letto.get('data_consegna'),
                'valore_ordine': letto.get('valore_ordine'),
                'lettura_prezzi': {
                    'formato': letto['formato'], 'n_righe': len(letto['righe']),
                    'generico': bool(letto.get('generico')),
                    'somma_righe': letto['somma_righe'],
                    'totale_stampato': letto['totale_stampato'], 'quadra': letto['quadra'],
                },
            })
        return jsonify({'success': True, 'data': dati}), 200

    except Exception as e:
        logging.error(f"[ERROR] extract_pdf_data: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 400

# ============ SEGNALA ERRORE ============
# Pulsante su ogni pagina (frontend/ft-segnala.js). Chi trova un problema
# scrive due parole; il resto (pagina, postazione, persona, ora, ultimi errori
# della pagina) parte da solo. Finisce nel database, in logs/segnalazioni.log
# e come avviso all'amministrazione.
_SEGNALAZIONI_RECENTI = {}          # dispositivo -> [istanti]: non piu' di 10 l'ora
SEGNALAZIONI_LOG = os.path.join(os.path.dirname(__file__), '..', 'logs', 'segnalazioni.log')
_LOCK_SEGNALAZIONI = threading.Lock()


@app.route('/api/segnalazioni-errore', methods=['POST'])
@richiede(*accesso.TUTTE)
def api_segnala_errore():
    from .models_ore import SegnalazioneErrore
    import json as _json
    import time as _time
    data = request.get_json(silent=True) or {}
    testo = str(data.get('testo') or '').strip()
    if len(testo) < 3:
        return jsonify({'success': False, 'error': 'Scrivi in due parole cosa non va.'}), 400
    testo = testo[:2000]
    dettagli = data.get('dettagli') if isinstance(data.get('dettagli'), dict) else {}
    try:
        if len(_json.dumps(dettagli)) > 30000:
            dettagli = {'troncati': True}
    except (TypeError, ValueError):
        dettagli = {}
    ident = identita()
    disp = (ident.dispositivo or {}).get('id') or request.remote_addr or '?'
    adesso = _time.time()
    with _LOCK_SEGNALAZIONI:
        recenti = [t for t in _SEGNALAZIONI_RECENTI.get(disp, []) if adesso - t < 3600]
        if len(recenti) >= 10:
            return jsonify({'success': False, 'error': "Troppe segnalazioni in un'ora da questo dispositivo."}), 429
        recenti.append(adesso)
        _SEGNALAZIONI_RECENTI[disp] = recenti
    pagina = str(data.get('pagina') or '')[:300]
    seg = SegnalazioneErrore(
        id=str(uuid.uuid4()), creata_il=datetime.utcnow(), stazione=ident.stazione,
        dispositivo=(ident.dispositivo or {}).get('label') or (ident.vecchio or {}).get('nome'),
        persona=(ident.persona or {}).get('nome'), pagina=pagina, testo=testo,
        dettagli=dettagli, stato='aperta')
    session = get_session()
    try:
        session.add(seg)
        session.commit()
        sid = seg.id
    except Exception as e:
        session.rollback()
        logger.exception('segnalazione non salvata')
        return jsonify({'success': False, 'error': 'Segnalazione non salvata: ' + str(e)}), 500
    finally:
        session.close()
    chi = (ident.persona or {}).get('nome') or (ident.dispositivo or {}).get('label') or ident.stazione or '?'
    logger.warning('SEGNALAZIONE %s da %s su %s: %s', sid[:8], chi, pagina, testo[:200])
    try:
        os.makedirs(os.path.dirname(SEGNALAZIONI_LOG), exist_ok=True)
        with open(SEGNALAZIONI_LOG, 'a', encoding='utf-8') as fh:
            fh.write(_json.dumps({'id': sid, 'quando': iso_utc(datetime.utcnow()), 'chi': chi,
                                  'stazione': ident.stazione, 'pagina': pagina, 'testo': testo,
                                  'dettagli': dettagli}, ensure_ascii=False) + '\n')
    except Exception:
        logger.exception('segnalazioni.log non scritto')
    try:
        NotificationManager.create_notification(
            'postazione-amministrazione', None, 'Segnalato un errore',
            f'{chi} ({pagina or "pagina?"}): {testo[:180]}',
            notification_type='alert', notification_category='attiva')
    except Exception:
        pass
    return jsonify({'success': True, 'id': sid}), 201


def _segnalazione_dict(x):
    return {'id': x.id, 'creata_il': iso_utc(x.creata_il), 'stazione': x.stazione,
            'dispositivo': x.dispositivo, 'persona': x.persona, 'pagina': x.pagina,
            'testo': x.testo, 'dettagli': x.dettagli or {}, 'stato': x.stato,
            'risolta_il': iso_utc(x.risolta_il) if x.risolta_il else None,
            'risolta_da': x.risolta_da, 'nota': x.nota}


@app.route('/api/segnalazioni-errore', methods=['GET'])
@richiede('ufficio', ADMIN)
def api_elenco_segnalazioni():
    from .models_ore import SegnalazioneErrore
    stato = request.args.get('stato')
    session = get_session()
    try:
        q = session.query(SegnalazioneErrore)
        if stato in ('aperta', 'risolta'):
            q = q.filter(SegnalazioneErrore.stato == stato)
        righe = q.order_by(SegnalazioneErrore.creata_il.desc()).limit(200).all()
        aperte = session.query(SegnalazioneErrore).filter(SegnalazioneErrore.stato == 'aperta').count()
        return jsonify({'success': True, 'segnalazioni': [_segnalazione_dict(x) for x in righe],
                        'aperte': aperte}), 200
    finally:
        session.close()


@app.route('/api/segnalazioni-errore/<sid>', methods=['PUT'])
@richiede('ufficio', ADMIN)
def api_aggiorna_segnalazione(sid):
    """{stato: 'risolta'|'aperta', nota?}"""
    from .models_ore import SegnalazioneErrore
    data = request.get_json(silent=True) or {}
    stato = data.get('stato')
    if stato not in ('aperta', 'risolta'):
        return jsonify({'success': False, 'error': 'stato non valido'}), 400
    session = get_session()
    try:
        x = session.query(SegnalazioneErrore).filter(SegnalazioneErrore.id == sid).first()
        if not x:
            return jsonify({'success': False, 'error': 'Segnalazione non trovata'}), 404
        x.stato = stato
        x.risolta_il = datetime.utcnow() if stato == 'risolta' else None
        x.risolta_da = _chi_nome() if stato == 'risolta' else None
        if 'nota' in data:
            x.nota = str(data.get('nota') or '')[:1000] or None
        session.commit()
        return jsonify({'success': True, 'segnalazione': _segnalazione_dict(x)}), 200
    finally:
        session.close()


# ============ HEALTH CHECK ============

@app.route('/api/health', methods=['GET'])
@richiede(PUBBLICO)
def health_check():
    """Dice che il programma risponde, e su quali dati sta lavorando.

    `istanza_di_prova` serve alle prove automatiche: quelle che scrivono lo
    controllano e si fermano se la risposta e' falsa. Un commento che dice "usa
    il server di prova" non ferma nessuno; questo si'.
    """
    return jsonify({
        'status': 'online',
        'timestamp': iso_utc(datetime.utcnow()),
        'istanza_di_prova': e_istanza_di_prova(),
    }), 200


@app.route('/api/dashboard-live', methods=['GET'])
@richiede(PUBBLICO)
def api_dashboard_live():
    """Dashboard live pubblica (info-panel per TV in ufficio Elena).

    Ritorna SOLO snapshot momentanei anonimi + KPI relative — safe per pubblico
    esterno (clienti in visita). NON contiene:
    - Nomi clienti / operai
    - Prezzi / margini / fatturato
    - Totali storici DB (numeri assoluti che possano dare info competitiva)
    - Consegne per giorno / on-time delivery %

    Endpoint aperto (no auth) perché serve a schermo condiviso e non contiene
    dati sensibili.
    """
    try:
        from .models import get_session, Order
        session = get_session()
        try:
            # "Attivi ora" e la curva dell'attivita' della giornata venivano
            # dalle scansioni con la pistola: pistole tolte, tolti anche loro
            # (sarebbero stati zeri per sempre).
            # Ordini per fase: le STESSE fasi delle viste dell'ufficio
            # (ordini_service.fase). Prima "pronti" contava gli ordini
            # tagliati ma ancora in officina (taglio fatto + RICEVUTO), cioe'
            # proprio quelli in lavorazione.
            #   in produzione = ordini aperti: in coda al laser, tagliati, in
            #                   lavorazione in officina
            #   pronti        = lavoro finito, non ancora consegnati
            #   ricevuti      = aperti che nessuno ha ancora toccato: il laser
            #                   non li ha ancora smistati e non sono tagliati.
            #                   Smistato "non va tagliato" vuol dire che e' gia'
            #                   passato in officina: prima lo diceva la prima
            #                   scansione, ora lo dice lo smistamento.
            from .ordini_service import fase as _fase
            ora = datetime.utcnow()
            ordini = session.query(Order).filter(
                Order.is_deleted == False  # noqa: E712
            ).all()
            n_in_produzione = 0
            n_pronti = 0
            n_ricevuti_puri = 0
            for o in ordini:
                f = _fase(o)
                if f == 'pronto_ddt':
                    n_pronti += 1
                elif f == 'aperto':
                    n_in_produzione += 1
                    toccato = (o.taglio_richiesto is not None
                               or bool(o.taglio_completato))
                    if not toccato:
                        n_ricevuti_puri += 1
            n_kanban_lavorazione = n_in_produzione - n_ricevuti_puri
            n_kanban_pronti = n_pronti

            return jsonify({
                'success': True,
                'snapshot': {
                    'ordini_in_produzione': n_in_produzione,
                    'ordini_pronti': n_pronti,
                },
                'kanban': {
                    'ricevuti': n_ricevuti_puri,
                    'lavorazione': n_kanban_lavorazione,
                    'pronti': n_kanban_pronti,
                },
                'timestamp': iso_utc(ora),
            }), 200
        finally:
            session.close()
    except Exception as e:
        logger.exception('dashboard-live failed')
        return jsonify({'success': False, 'error': str(e)}), 500

# ============ BACKUP & EXPORT ============

# Funzioni di backup. Lo scheduler NON parte piu' all'import: lo avvia solo
# run.py (prima erano due, questo ogni 12 h e quello orario di run.py, che si
# contendevano le stesse copie).
try:
    _backup_sys_path = os.path.join(os.path.dirname(__file__), '..')
    if _backup_sys_path not in sys.path:
        sys.path.insert(0, _backup_sys_path)
    from backup_db import backup as _do_backup, integrity_check as _integrity_check
    from backup_db import load_config as _backup_load_config, save_config as _backup_save_config
    from backup_db import list_backups as _backup_list, cartella_valida as _backup_cartella_valida
except Exception as _e:
    logging.warning(f'[BACKUP] modulo backup non disponibile: {_e}')

@app.route('/api/admin/backup', methods=['POST'])
@richiede('ufficio', ADMIN)
def manual_backup():
    """Esegue un backup manuale del database (solo amministrazione/capi)"""
    try:
        ok = _integrity_check()
        path = _do_backup(motivo='manuale')
        if path:
            return jsonify({'success': True, 'backup_path': os.path.basename(path), 'integrity_ok': ok}), 200
        return jsonify({'success': False, 'integrity_ok': ok,
                        'error': 'Backup fallito' + ('' if ok else ': il database non passa il controllo di integrita (vedi RIPRISTINO.md)')}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/admin/backup/settings', methods=['GET', 'PUT'])
@richiede('ufficio', ADMIN, metodi=('GET',))
@richiede(ADMIN, metodi=('PUT',))
def backup_settings():
    """Leggi o aggiorna impostazioni backup (solo amministrazione/capi)"""
    try:
        if request.method == 'GET':
            config = _backup_load_config()
            return jsonify({'success': True, 'settings': config}), 200
        else:
            data = request.get_json() or {}
            config = _backup_load_config()
            if 'backup_enabled' in data:
                config['backup_enabled'] = bool(data['backup_enabled'])
            if 'interval_hours' in data:
                config['interval_hours'] = max(1, min(168, int(data['interval_hours'])))
            if 'max_backups' in data:
                config['max_backups'] = max(5, min(100, int(data['max_backups'])))
            # Cartelle: devono esistere ed essere fuori dal programma. Prima
            # si accettava qualunque stringa e la rotazione avrebbe cancellato
            # file scheduler_*.db ovunque puntasse.
            for campo in ('backup_path', 'remote_path'):
                if campo in data:
                    v = str(data[campo] or '').strip()
                    errore = _backup_cartella_valida(v)
                    if errore:
                        return jsonify({'success': False, 'error': f'{campo}: {errore}'}), 400
                    config[campo] = v
            _backup_save_config(config)
            _audit('BACKUP_IMPOSTAZIONI', 'backup', '', _json_mod.dumps({k: config.get(k) for k in data if k in config}, ensure_ascii=False))
            return jsonify({'success': True, 'settings': config}), 200
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/admin/backup/list', methods=['GET'])
@richiede('ufficio', ADMIN)
def backup_list():
    """Lista dei backup esistenti (solo amministrazione/capi)"""
    try:
        backups = _backup_list()
        return jsonify({'success': True, 'backups': backups, 'count': len(backups)}), 200
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/admin/audit', methods=['GET'])
@richiede(ADMIN)
def get_audit_log():
    """Recupera log attività recenti (solo amministrazione/capi; user_id = filtro)"""
    try:
        limit = min(request.args.get('limit', 100, type=int), 500)
        user_id = request.args.get('user_id')
        logs = AuditManager.get_recent(limit=limit, user_id=user_id)
        return jsonify({'success': True, 'logs': logs}), 200
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/admin/export-json', methods=['GET'])
@richiede('ufficio', ADMIN)
def export_json():
    """Esporta tutti gli ordini attivi in formato JSON (download, solo amministrazione/capi)"""
    try:
        import json, io
        orders = OrderManager.get_all_orders_dict()
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        payload = json.dumps(orders, ensure_ascii=False, indent=2, default=str).encode('utf-8')
        buf = io.BytesIO(payload)
        buf.seek(0)
        return send_file(
            buf,
            mimetype='application/json',
            as_attachment=True,
            download_name=f'ordini_{ts}.json'
        )
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ============ NOTIFICATION SYSTEM (WhatsApp-like) ============
# Gli avvisi sono della STAZIONE: la campanella dell'Ufficio e' la stessa per
# chiunque ci entri col PIN. Ogni stazione vede e tocca solo i suoi: prima
# bastava scrivere un altro user_id nell'indirizzo per leggere (o cancellare)
# gli avvisi di chiunque.

def _proprietario_avvisi() -> str:
    st = identita().stazione
    return accesso.STAZIONI[st]['utente'] if st in accesso.STAZIONI else ''


def _avviso_mio(notification_id: str) -> bool:
    from .models import Notification as _N
    s = get_session()
    try:
        n = s.get(_N, notification_id)
        return n is not None and n.user_id == _proprietario_avvisi()
    finally:
        s.close()


@app.route('/api/notifications', methods=['GET', 'POST'])
@richiede(*CON_AVVISI, metodi=('GET',))
@richiede(ADMIN, metodi=('POST',))
def handle_notifications():
    """GET: Recupera notifiche | POST: Crea notifica"""
    if request.method == 'GET':
        try:
            user_id = _proprietario_avvisi()    # lo user_id dell'indirizzo non conta
            limit = request.args.get('limit', 50, type=int)
            notifications = NotificationManager.get_notifications(user_id, limit=limit)
            unread_count = NotificationManager.get_unread_count(user_id)

            return jsonify({
                'success': True,
                'data': {
                    'notifications': notifications,
                    'unread_count': unread_count,
                    'total': len(notifications)
                }
            }), 200
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 400

    elif request.method == 'POST':
        try:
            # Gli avvisi li crea il server; da fuori solo un amministratore
            # (regola sopra). Qui user_id e' il DESTINATARIO, un dato.
            data = request.get_json() or {}
            user_id = data.get('user_id')
            order_id = data.get('order_id')
            title = data.get('title', 'Notifica')
            message = data.get('message', '')
            notification_type = data.get('notification_type', 'order')
            notification_category = data.get('notification_category', 'informativa')

            if not user_id or not title:
                return jsonify({'success': False, 'error': 'user_id e title obbligatori'}), 400

            notification = NotificationManager.create_notification(
                user_id=user_id,
                order_id=order_id,
                title=title,
                message=message,
                notification_type=notification_type,
                notification_category=notification_category
            )

            if notification:
                return jsonify({
                    'success': True,
                    'data': notification
                }), 201
            else:
                return jsonify({'success': False, 'error': 'Failed to create notification'}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/notifications/<notification_id>/read', methods=['PUT'])
@richiede(*CON_AVVISI)
def mark_notification_read(notification_id):
    """Segna una notifica come letta"""
    try:
        if not _avviso_mio(notification_id):
            return jsonify({'success': False, 'error': 'Notifica non trovata'}), 404
        success = NotificationManager.mark_as_read(notification_id)
        return jsonify({'success': success}), 200 if success else 404
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/notifications/<notification_id>', methods=['DELETE'])
@richiede(*CON_AVVISI)
def delete_notification(notification_id):
    """Cancella una singola notifica (soft delete)"""
    try:
        if not _avviso_mio(notification_id):
            return jsonify({'success': False, 'error': 'Notifica non trovata'}), 404
        success = NotificationManager.delete_notification(notification_id)

        if success:
            return jsonify({'success': True, 'message': 'Notifica cancellata'}), 200
        else:
            return jsonify({'success': False, 'error': 'Notifica non trovata'}), 404
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/notifications/clear-all', methods=['DELETE'])
@richiede(*CON_AVVISI)
def clear_all_notifications():
    """Cancella tutte le notifiche dell'utente"""
    try:
        success = NotificationManager.delete_all_notifications(_proprietario_avvisi())

        if success:
            return jsonify({'success': True, 'message': 'Tutte le notifiche cancellate'}), 200
        else:
            return jsonify({'success': False, 'error': 'Errore durante la cancellazione'}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

# ============================================================================
#  CICLO AMMINISTRATIVO DEGLI ORDINI — riservato all'ufficio
#  L'operaio comunica a voce che ha finito: nessuno in officina cambia lo stato
#  di un ordine. Queste transizioni sono consentite solo alla stazione Ufficio
#  (@richiede), e il controllo e' nel backend: nascondere i pulsanti non
#  basta, l'API va protetta anche contro una chiamata diretta.
# ============================================================================

def _verifica_preventivo(preventivo_id):
    """Esito della verifica, o None se il preventivo non esiste."""
    prev = PreventivoManager.get(preventivo_id)
    if not prev or prev.get('error'):
        return None
    from .preventivi.verifica import verifica as _v
    cfg = (ConfigManager.load_config() or {}).get('preventivi_config') or {}
    snap = prev.get('snapshot_economico') or None
    if snap:
        cfg = {'costo_generali_pct': snap.get('costo_generali_pct') or 0}
    cfg = dict(cfg, _materiali=((ConfigManager.load_config() or {}).get('laser_config') or {}).get('materiali') or {})
    return _v(prev, cfg)


@app.route('/api/preventivi/<preventivo_id>/verifica', methods=['GET'])
@richiede(*UFFICI)
def api_preventivo_verifica(preventivo_id):
    """E' pronto per l'invio? Se no, cosa manca e su quale riga.

    Usato dall'editor prima delle operazioni definitive: senza questo l'invio
    partiva e i buchi si scoprivano dal cliente.
    """
    try:
        prev = PreventivoManager.get(preventivo_id)
        if not prev or prev.get('error'):
            return jsonify({'success': False, 'error': 'Preventivo non trovato'}), 404
        from .preventivi.verifica import verifica as _verifica
        cfg = (ConfigManager.load_config() or {}).get('preventivi_config') or {}
        # Se l'offerta e' gia' partita valgono le percentuali congelate allora.
        snap = prev.get('snapshot_economico') or None
        if snap:
            cfg = {'costo_generali_pct': snap.get('costo_generali_pct') or 0}
        # Materiali aggiunti nelle Impostazioni (C75...): non sono "sconosciuti"
        cfg = dict(cfg, _materiali=((ConfigManager.load_config() or {}).get('laser_config') or {}).get('materiali') or {})
        return jsonify({'success': True, **_verifica(prev, cfg)}), 200
    except Exception as e:
        logger.exception('api_preventivo_verifica failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/totali', methods=['GET'])
@richiede(*UFFICI)
def api_preventivo_totali(preventivo_id):
    """Totali calcolati dal SERVER sui dati salvati, con la composizione.

    Serve all'editor per confrontare la propria anteprima con il numero che
    fara' testo, e al riepilogo per mostrare da dove viene il prezzo.
    """
    try:
        prev = PreventivoManager.get(preventivo_id)
        if not prev or prev.get('error'):
            return jsonify({'success': False, 'error': 'Preventivo non trovato'}), 404
        from .preventivi.calcolo import calcola
        cfg = (ConfigManager.load_config() or {}).get('preventivi_config') or {}
        return jsonify({'success': True, 'totali': calcola(prev, cfg)}), 200
    except Exception as e:
        logger.exception('api_preventivo_totali failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/ordini/viste', methods=['GET'])
@richiede('ufficio')
def api_ordini_viste():
    """Le quattro viste dell'ufficio con i conteggi di tutte le linguette."""
    try:
        from .ordini_service import elenco
        return jsonify({'success': True, **elenco(
            fase_richiesta=(request.args.get('fase') or '').strip() or None,
            cliente=(request.args.get('cliente') or '').strip() or None)}), 200
    except Exception as e:
        logger.exception('api_ordini_viste failed')
        return jsonify({'success': False, 'error': str(e)}), 500


def _transizione(funzione, order_id, **extra):
    # Chi puo' lo dice la regola della rotta (solo l'Ufficio); qui si scrive
    # nello storico la persona entrata col PIN.
    res = funzione(order_id, _chi(), **extra)
    if res.get('error'):
        codice = res.get('codice')
        stato = 404 if codice == 'non_trovato' else (
            500 if codice == 'errore_interno' else 409)
        return jsonify({'success': False, **res}), stato
    return jsonify(res), 200


@app.route('/api/ordini/<order_id>/completamento', methods=['POST'])
@richiede('ufficio')
def api_ordine_completamento(order_id):
    """L'officina ha comunicato che ha finito: passa alla preparazione del DDT."""
    from .ordini_service import registra_completamento
    return _transizione(registra_completamento, order_id)


@app.route('/api/ordini/<order_id>/completamento', methods=['DELETE'])
@richiede('ufficio')
def api_ordine_completamento_annulla(order_id):
    from .ordini_service import annulla_completamento
    return _transizione(annulla_completamento, order_id)


@app.route('/api/ordini/<order_id>/ddt', methods=['POST'])
@richiede('ufficio')
def api_ordine_ddt(order_id):
    """Registra il riferimento del DDT emesso nell'altro sistema.

    Corpo: {user_id, numero, data?: AAAA-MM-GG (default: oggi, ora italiana)}.
    """
    from .ordini_service import registra_ddt
    dati = request.get_json(silent=True) or {}
    return _transizione(registra_ddt, order_id,
                        numero=dati.get('numero') or dati.get('ddt_numero'),
                        data=dati.get('data') or dati.get('ddt_data'))


@app.route('/api/ordini/<order_id>/consegna', methods=['POST'])
@richiede('ufficio')
def api_ordine_consegna(order_id):
    """Merce consegnata. `completa=false` lascia un residuo da consegnare."""
    from .ordini_service import registra_consegna
    dati = request.get_json(silent=True) or {}
    return _transizione(registra_consegna, order_id,
                        completa=bool(dati.get('completa', True)),
                        note=dati.get('note') or '')


@app.route('/api/ordini/<order_id>/chiudi', methods=['POST'])
@richiede('ufficio')
def api_ordine_chiudi(order_id):
    """Fatturato: ciclo amministrativo concluso, l'ordine va in archivio.

    Corpo: {user_id, numero_fattura (obbligatorio), data_fattura?: AAAA-MM-GG
    (default: oggi), note?}. Richiede la consegna registrata e completa:
    altrimenti 409 con un messaggio che dice cosa manca (`codice`:
    sequenza | consegna_parziale | fattura_mancante | data_non_valida).
    """
    from .ordini_service import chiudi_pratica
    dati = request.get_json(silent=True) or {}
    return _transizione(chiudi_pratica, order_id,
                        numero_fattura=dati.get('numero_fattura'),
                        data_fattura=dati.get('data_fattura'),
                        note=dati.get('note') or dati.get('note_chiusura'))


@app.route('/api/ordini/<order_id>/riapri', methods=['POST'])
@richiede('ufficio')
def api_ordine_riapri(order_id):
    from .ordini_service import riapri
    return _transizione(riapri, order_id)


# ============================================================================
#  TABLET DI OFFICINA — abilitazione dei dispositivi condivisi
#  Ogni dispositivo e' registrato UNA volta come stazione (backend/accesso.py).
#  Qui un amministratore li vede, li rinomina e li revoca senza riga di comando.
# ============================================================================

# Stazioni creabili dall'interfaccia: tutte, ora che queste rotte vogliono il
# PIN di un amministratore. Di solito pero' un dispositivo si registra dalla
# sua pagina iniziale; questo serve a preparare un codice da portare a mano.
_SCOPE_DA_UI = tuple(accesso.STAZIONI)


def _url_base_lan() -> str:
    """Indirizzo con cui il tablet raggiunge il server (non 'localhost')."""
    import socket
    host = request.host.split(':')[0]
    if host not in ('localhost', '127.0.0.1'):
        return f'{request.scheme}://{request.host}'
    porta = request.host.split(':')[1] if ':' in request.host else '5000'
    try:
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sk.connect(('8.8.8.8', 80))       # non invia nulla: serve solo l'IP locale
        ip = sk.getsockname()[0]
        sk.close()
    except Exception:
        ip = socket.gethostbyname(socket.gethostname())
    return f'http://{ip}:{porta}'


@app.route('/api/admin/dispositivi', methods=['GET'])
@richiede(ADMIN)
def api_dispositivi_list():
    """Elenco dei tablet abilitati (senza segreti) + indirizzo base per il QR.
    Solo amministrazione/capi."""
    try:
        from .models_ore import DeviceToken as _DT
        questo = (identita().dispositivo or {}).get('id')
        s = get_session()
        try:
            righe = s.query(_DT).order_by(_DT.is_active.desc(), _DT.created_at.desc()).all()
            out = []
            for r in righe:
                d = accesso._dispositivo_dict(r)
                # nomi vecchi, per chi legge ancora label/scope/is_active
                d.update({'label': r.label, 'scope': r.scope, 'is_active': bool(r.is_active),
                          'created_at': d['registrato_il'], 'last_used_at': d['ultimo_uso'],
                          'questo': r.id == questo})
                out.append(d)
        finally:
            s.close()
        return jsonify({'success': True, 'dispositivi': out,
                        'url_base': _url_base_lan()}), 200
    except Exception as e:
        logger.exception('api_dispositivi_list failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/admin/dispositivi', methods=['POST'])
@richiede(ADMIN)
def api_dispositivi_create():
    """Abilita un tablet. Il token in chiaro viene restituito UNA sola volta."""
    try:
        data = request.get_json(silent=True) or {}
        user_id = _chi_admin()
        scope = (data.get('scope') or 'ore').strip().lower()
        if scope not in _SCOPE_DA_UI:
            return jsonify({'error': 'Stazione sconosciuta.'}), 400
        etichetta = (data.get('label') or '').strip()
        if not etichetta:
            return jsonify({'error': 'Dai un nome al tablet (es. "Tablet timbratrice")'}), 400

        from .auth_device import crea_token
        res = crea_token(etichetta, scope, created_by=user_id)
        if res.get('error'):
            return jsonify({'error': res['error']}), 400
        try:
            AuditManager.log(user_id=user_id, action='CREA_DEVICE_TOKEN',
                             entity_type='device_token', entity_id=res.get('id'),
                             detail=f'{etichetta} ({scope})')
        except Exception:
            pass
        # Si apre la pagina iniziale col codice: diventa il cookie del
        # dispositivo e la pagina sparisce dall'indirizzo.
        res['url'] = f"{_url_base_lan()}/?codice={res['token']}"
        return jsonify({'success': True, 'dispositivo': res}), 201
    except Exception as e:
        logger.exception('api_dispositivi_create failed')
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/dispositivi/<token_id>', methods=['DELETE'])
@richiede(ADMIN)
def api_dispositivi_revoke(token_id):
    """Revoca un tablet (es. smarrito). Da quel momento non salva piu' nulla."""
    try:
        user_id = _chi_admin()
        from .auth_device import revoca_token
        res = revoca_token(token_id, da=user_id)
        if res.get('error'):
            return jsonify(res), 404
        # Dispositivo perso o rubato: chi era entrato da li' esce subito.
        accesso.chiudi_sessioni(device_id=token_id)
        try:
            AuditManager.log(user_id=user_id, action='REVOCA_DEVICE_TOKEN',
                             entity_type='device_token', entity_id=token_id, detail='')
        except Exception:
            pass
        return jsonify({'success': True}), 200
    except Exception as e:
        logger.exception('api_dispositivi_revoke failed')
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/dispositivi/qr', methods=['GET'])
@richiede(ADMIN)
def api_dispositivi_qr():
    """QR PNG dell'indirizzo di abilitazione, da inquadrare col tablet.

    Il testo da codificare arriva come parametro e NON viene salvato: il QR e'
    usa-e-getta, si genera subito dopo aver creato il token.
    """
    try:
        testo = (request.args.get('testo') or '').strip()
        if not testo or len(testo) > 512:
            return jsonify({'error': 'testo mancante o troppo lungo'}), 400
        # SVG e non PNG: il backend raster di reportlab richiede una libreria
        # nativa che qui non c'e', mentre l'SVG e' puro testo e si vede uguale.
        from reportlab.graphics.barcode import qr
        from reportlab.graphics.shapes import Drawing
        from reportlab.graphics import renderSVG
        import io as _io

        codice = qr.QrCodeWidget(testo, barLevel='M')
        x1, y1, x2, y2 = codice.getBounds()
        lato = 560
        d = Drawing(lato, lato,
                    transform=[lato / (x2 - x1), 0, 0, lato / (y2 - y1), -x1, -y1])
        d.add(codice)
        buf = _io.BytesIO(renderSVG.drawToString(d).encode('utf-8'))
        return send_file(buf, mimetype='image/svg+xml', max_age=0)
    except Exception as e:
        logger.exception('api_dispositivi_qr failed')
        return jsonify({'error': str(e)}), 500


@app.route('/api/capo/kpi-operai', methods=['GET'])
@richiede('ufficio', ADMIN)
def api_capo_kpi_operai():
    """KPI ore per operaio (oggi/settimana/mese) — pagina capo officina.
    Le ore sono quelle dichiarate dagli operai sul tablet della timbratrice."""
    try:
        return jsonify(KPIManager.get_kpi_operai()), 200
    except Exception as e:
        logger.exception('api_capo_kpi_operai failed')
        return jsonify({'error': str(e)}), 500


@app.route('/api/capo/calendario-ordini', methods=['GET'])
@richiede('ufficio', 'laser')
def api_capo_calendario():
    """Ordini per data consegna nel mese (param ?mese=YYYY-MM)."""
    try:
        mese = (request.args.get('mese') or '').strip()
        if not mese:
            now = datetime.utcnow()
            year, month = now.year, now.month
        else:
            try:
                year, month = mese.split('-')
                year = int(year); month = int(month)
                if not (1 <= month <= 12):
                    raise ValueError
            except Exception:
                return jsonify({'error': 'Formato mese non valido (usa YYYY-MM)'}), 400
        return jsonify({
            'mese': f'{year:04d}-{month:02d}',
            'giorni': OrderManager.get_calendario_ordini(year, month),
        }), 200
    except Exception as e:
        logger.exception('api_capo_calendario failed')
        return jsonify({'error': str(e)}), 500


# ============================================================================
#  PREVENTIVI — API REST (Fase 2 merge preventivatore)
# ============================================================================

# Ruoli autorizzati a write/read sui preventivi (decisione: aperto interni, no operai)
_PREV_READ_ROLES = ['Commerciale', 'Amministratore', 'CAPO', 'Impiegata']

_RX_ROTTA_PREVENTIVO = re.compile(
    r'^/api/preventivi/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(/.*)?$')


_RX_UUID = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')


def _cartella_preventivo(pid) -> str:
    """Cartella dei file di un preventivo (uploads/preventivi_tmp/<uuid>).

    L'id arriva dall'indirizzo: prima si univa al percorso cosi' com'era, e un
    id come ".." puntava fuori dalla cartella. Solo UUID, e il risultato deve
    stare dentro preventivi_tmp."""
    pid = str(pid or '')
    if not _RX_UUID.match(pid):
        raise ValueError(f'id preventivo non valido: {pid[:40]!r}')
    base = os.path.realpath(os.path.join(UPLOAD_FOLDER, 'preventivi_tmp'))
    cart = os.path.realpath(os.path.join(base, pid))
    if os.path.dirname(cart) != base:
        raise ValueError('percorso fuori da preventivi_tmp')
    return cart


@app.before_request
def _valida_id_preventivo():
    """Ogni rotta con <preventivo_id>/<pacchetto_id> accetta solo un UUID:
    un id sbagliato si ferma qui con 400, prima di toccare file o database."""
    va = request.view_args or {}
    for k in ('preventivo_id', 'pacchetto_id'):
        if k in va and not _RX_UUID.match(str(va[k] or '')):
            return jsonify({'success': False, 'error': 'Identificativo del preventivo non valido'}), 400
    return None


@app.before_request
def _blocca_preventivi_tecnici():
    """Il preventivo tecnico di un ordine caricato col pacchetto del cliente
    non passa dalle rotte del preventivatore: non si apre, non si modifica,
    non si invia, non si accetta. Si lasciano solo le anteprime in lettura
    (SVG dei disegni, PDF d'ordine) che la revisione dell'ufficio usa prima
    di creare l'ordine.

    Un solo controllo qui, invece che in ognuna delle decine di rotte
    /api/preventivi/<id>/...: una rotta aggiunta domani e' coperta da sola.
    """
    m = _RX_ROTTA_PREVENTIVO.match(request.path or '')
    if not m or not PreventivoManager.e_tecnico(m.group(1)):
        return None
    resto = m.group(2) or ''
    if request.method == 'GET' and re.match(r'^/(dxf/.+/svg|disegni-pdf/[^/]+)$', resto):
        return None
    from .database import ERRORE_TECNICO
    if request.method == 'GET':
        return jsonify({'success': False, 'codice': 'pacchetto_ordine',
                        'error': 'Preventivo non trovato. ' + ERRORE_TECNICO}), 404
    return jsonify({'success': False, 'codice': 'pacchetto_ordine',
                    'error': ERRORE_TECNICO}), 409


@app.route('/api/preventivi', methods=['GET'])
@richiede(*UFFICI)
def api_preventivi_list():
    """Lista preventivi (filtri opzionali: cliente, status)."""
    try:
        cliente = request.args.get('cliente')
        status = request.args.get('status')
        limit = min(int(request.args.get('limit', 200)), 500)
        items = PreventivoManager.list(cliente=cliente, status=status, limit=limit)
        return jsonify({'success': True, 'count': len(items), 'preventivi': items}), 200
    except Exception as e:
        logger.exception('preventivi list failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi', methods=['POST'])
@richiede('commerciale')
def api_preventivi_create():
    """Crea preventivo (BOZZA). Riservato a Commerciale + Admin."""
    try:
        data = request.get_json() or {}
        created_by = _chi()
        cliente = (data.get('cliente') or '').strip()
        if not cliente:
            return jsonify({'success': False, 'error': 'Cliente obbligatorio'}), 400
        dcp = data.get('data_consegna_proposta')
        dcp_dt = None
        if dcp:
            try:
                dcp_dt = datetime.strptime(dcp[:10], '%Y-%m-%d')
            except ValueError:
                return jsonify({'success': False, 'error': 'data_consegna_proposta: formato YYYY-MM-DD'}), 400
        p = PreventivoManager.create(
            cliente=cliente,
            created_by=created_by,
            quantita=data.get('quantita', 1),
            numero_ordine_cliente=data.get('numero_ordine_cliente'),
            margine_pct=data.get('margine_pct', 0.0),
            data_consegna_proposta=dcp_dt,
            note=data.get('note'),
        )
        try:
            AuditManager.log(user_id=created_by, action='CREATE_PREVENTIVO',
                             entity_type='preventivi', entity_id=p['id'],
                             detail='cliente=' + cliente)
        except Exception:
            pass
        return jsonify({'success': True, 'preventivo': p}), 201
    except Exception as e:
        logger.exception('preventivi create failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>', methods=['GET'])
@richiede(*UFFICI)
def api_preventivi_get(preventivo_id):
    """Dettaglio preventivo + articoli/assiemi/tubolari/piastre.

    Arricchisce gli articoli SENZA misure salvate con `bbox_w_mm`/`bbox_h_mm`
    dal DXF pulito, solo se il pulito è affidabile.

    BUG FIX: prima le misure venivano SOVRASCRITTE a ogni apertura leggendo il
    pulito con un loop ingenuo (niente blocchi/unità/spline) → un 170×100
    confermato nel CAD diventava 18×18 (pulito errato = una svasatura).
    Ora:
      - bbox_w_mm/bbox_h_mm già presenti sull'articolo non si toccano;
      - il pulito si misura con ezdxf.bbox + scala unità (dxf_cleanup);
      - un pulito automatico "legacy" (vecchia pulizia, possibile cornice o
        foro) viene rigenerato UNA volta dall'originale; se resta non
        affidabile (misure diverse dall'articolo o incompatibili con l'area)
        nella risposta cleaned_dxf_filename = None (si mostra l'originale) e
        `cleaned_non_affidabile` spiega il motivo. L'originale non si tocca.
    """
    try:
        p = PreventivoManager.get(preventivo_id, include_children=True)
        if not p:
            return jsonify({'success': False, 'error': 'Preventivo non trovato'}), 404
        prev_dir = _cartella_preventivo(preventivo_id)
        detection_cfg = None
        for art in (p.get('articoli') or []):
            cleaned_name = art.get('cleaned_dxf_filename')
            if not cleaned_name:
                continue
            cleaned_path = os.path.join(prev_dir, os.path.basename(cleaned_name))
            if not os.path.exists(cleaned_path):
                continue
            try:
                from .preventivi import dxf_cleanup as _dxc
                if detection_cfg is None:
                    detection_cfg = (ConfigManager.load_config() or {}).get('dxf_detection', {})
                orig_name = art.get('dxf_filename')
                orig_path = os.path.join(prev_dir, os.path.basename(orig_name)) if orig_name else None
                esito = _dxc.verifica_pulito_articolo(
                    cleaned_path, art, original_path=orig_path, config=detection_cfg)
                if not esito.get('affidabile'):
                    logger.info('preventivo %s: DXF pulito %s non affidabile (%s)',
                                preventivo_id, cleaned_name, esito.get('motivo'))
                    art['cleaned_dxf_filename'] = None
                    art['cleaned_status'] = None
                    art['cleaned_non_affidabile'] = esito.get('motivo') or True
                    continue
                try:
                    ha_misure = float(art.get('bbox_w_mm') or 0) > 0 and float(art.get('bbox_h_mm') or 0) > 0
                except (TypeError, ValueError):
                    ha_misure = False
                if not ha_misure and esito.get('w_mm') and esito.get('h_mm'):
                    art['bbox_w_mm'] = esito['w_mm']
                    art['bbox_h_mm'] = esito['h_mm']
            except Exception as ee:
                logger.debug('verifica DXF pulito %s fallita: %s', cleaned_name, ee)
                # non fatale — fallback formula lato client
        # Preventivo accettato: l'ordine che ne e' nato, per "Stampa ordine"
        # dal preventivatore (qui e non in PreventivoManager.get, che il
        # tablet chiama per ogni ordine aperto).
        if p.get('status') == 'ACCETTATO':
            session = get_session()
            try:
                o = session.query(Order.id).filter(
                    Order.preventivo_id_origine == preventivo_id,
                    Order.is_deleted == False,  # noqa: E712
                ).first()
                p['ordine_id'] = o[0] if o else None
            finally:
                session.close()
        return jsonify({'success': True, 'preventivo': p}), 200
    except Exception as e:
        logger.exception('preventivo get failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>', methods=['PUT'])
@richiede('commerciale')
def api_preventivi_update(preventivo_id):
    """Modifica preventivo. Bloccato se status INVIATO/ACCETTATO (immutabili)."""
    try:
        data = request.get_json() or {}
        data.pop('updated_by', None)      # pagine vecchie: non e' un campo del preventivo
        updated_by = _chi()
        # data_consegna_proposta come stringa → datetime
        if 'data_consegna_proposta' in data and data['data_consegna_proposta']:
            try:
                data['data_consegna_proposta'] = datetime.strptime(
                    data['data_consegna_proposta'][:10], '%Y-%m-%d')
            except (ValueError, TypeError):
                data['data_consegna_proposta'] = None
        result = PreventivoManager.update(preventivo_id, data)
        if result is None:
            return jsonify({'success': False, 'error': 'Preventivo non trovato'}), 404
        if isinstance(result, dict) and result.get('error'):
            return jsonify({'success': False, 'error': result['error']}), 409
        try:
            AuditManager.log(user_id=updated_by, action='UPDATE_PREVENTIVO',
                             entity_type='preventivi', entity_id=preventivo_id,
                             detail=str(list(data.keys())))
        except Exception:
            pass
        return jsonify({'success': True, 'preventivo': result}), 200
    except Exception as e:
        logger.exception('preventivo update failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/duplica', methods=['POST'])
@richiede('commerciale')
def api_preventivi_duplicate(preventivo_id):
    """Duplica un preventivo esistente in un nuovo BOZZA.

    Body opzionale: {created_by, cliente (default = cliente sorgente),
                     copy_articoli (default True)}
    Utile per commesse ricorrenti dello stesso cliente o come template.
    Copia anche i file DXF nella cartella del nuovo preventivo.
    """
    try:
        data = request.get_json(silent=True) or {}
        created_by = _chi()
        new_cliente = data.get('cliente') or ''
        copy_articoli = bool(data.get('copy_articoli', True))
        result = PreventivoManager.duplicate(
            preventivo_id, new_cliente=new_cliente,
            created_by=created_by, copy_articoli=copy_articoli,
        )
        if not result:
            return jsonify({'success': False, 'error': 'Preventivo sorgente non trovato'}), 404
        # Copia anche i file DXF (se presenti) nella cartella del nuovo preventivo
        try:
            src_dir = _cartella_preventivo(preventivo_id)
            dst_dir = _cartella_preventivo(result['id'])
            if os.path.isdir(src_dir):
                os.makedirs(dst_dir, exist_ok=True)
                import shutil
                for f in os.listdir(src_dir):
                    if f.lower().endswith(('.dxf', '.step', '.stp')):
                        shutil.copy2(os.path.join(src_dir, f), os.path.join(dst_dir, f))
        except Exception as _copy_err:
            logger.warning('copia file DXF durante duplica fallita: %s', _copy_err)
        try:
            AuditManager.log(user_id=created_by, action='DUPLICATE_PREVENTIVO',
                             entity_type='preventivi', entity_id=result['id'],
                             detail=f'sorgente={preventivo_id}')
        except Exception:
            pass
        return jsonify({'success': True, 'preventivo': result}), 201
    except Exception as e:
        logger.exception('preventivi duplicate failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>', methods=['DELETE'])
@richiede('commerciale')
def api_preventivi_delete(preventivo_id):
    """Soft delete (is_deleted=True). Riservato a Commerciale + Admin."""
    try:
        deleted_by = _chi()
        ok = PreventivoManager.soft_delete(preventivo_id)
        if not ok:
            return jsonify({'success': False, 'error': 'Preventivo non trovato'}), 404
        try:
            AuditManager.log(user_id=deleted_by, action='DELETE_PREVENTIVO',
                             entity_type='preventivi', entity_id=preventivo_id, detail='')
        except Exception:
            pass
        _cleanup_preventivo_files(preventivo_id)
        return jsonify({'success': True}), 200
    except Exception as e:
        logger.exception('preventivo delete failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/import-xlsx', methods=['POST'])
@richiede('commerciale')
def api_preventivi_import_xlsx(preventivo_id):
    """Upload XLSX Lantek → estrae articoli e li ritorna (NON li salva ancora).
    La UI mostra l'anteprima; il save effettivo avviene quando l'utente conferma.
    """
    try:
        admin_id = _chi()
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'File XLSX obbligatorio'}), 400
        f = request.files['file']
        if not f.filename or not f.filename.lower().endswith('.xlsx'):
            return jsonify({'success': False, 'error': 'File deve essere .xlsx'}), 400
        # Salva temporaneo per processing
        tmp_path = os.path.join(UPLOAD_FOLDER, 'tmp_' + uuid.uuid4().hex + '_' + os.path.basename(f.filename))
        f.save(tmp_path)
        try:
            articoli = _xlsx_importer.importa_xlsx(tmp_path)
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return jsonify({'success': True, 'articoli': articoli, 'count': len(articoli)}), 200
    except Exception as e:
        logger.exception('preventivi import xlsx failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/import-dxf', methods=['POST'])
@richiede('commerciale')
def api_preventivi_import_dxf(preventivo_id):
    """Upload DXF → estrae lavorazioni (pieghe/saldature) + geometria (area/perimetro).
    Il file viene scartato dopo l'estrazione (decisione: no storage DXF).
    """
    try:
        admin_id = _chi()
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'File disegno obbligatorio'}), 400
        f = request.files['file']
        fname_lower = (f.filename or '').lower()
        if not (fname_lower.endswith('.dxf') or fname_lower.endswith('.dwg')):
            return jsonify({'success': False, 'error': 'File deve essere .dxf o .dwg'}), 400
        # Salva DXF (o DXF convertito da DWG) in uploads/preventivi_tmp/<id>/
        # per consentire la preview interattiva. Sarà cancellato all'accettazione/rifiuto/delete.
        prev_dir = _cartella_preventivo(preventivo_id)
        os.makedirs(prev_dir, exist_ok=True)
        # Nome che non ne sovrascrive un altro: due "flangia.dxf" da cartelle
        # diverse sono la norma, e il secondo cancellava il primo.
        saved_filename = _nome_disegno_libero(prev_dir, f.filename)

        is_dwg = fname_lower.endswith('.dwg')
        if is_dwg:
            tmp_dwg = os.path.join(UPLOAD_FOLDER, 'tmp_' + uuid.uuid4().hex + '.dwg')
            f.save(tmp_dwg)
            conv = _convert_dwg_to_dxf(tmp_dwg)
            try: os.remove(tmp_dwg)
            except OSError: pass
            if isinstance(conv, dict) and conv.get('error'):
                err_code = conv['error']
                if err_code == 'ODA_NOT_INSTALLED':
                    return jsonify({
                        'success': False,
                        'error': 'ODA File Converter non installato. Scarica e installa da: '
                                 'https://www.opendesign.com/guestfiles/oda_file_converter '
                                 '(gratuito, ~50MB). Una volta installato il DWG viene '
                                 'convertito automaticamente in DXF. Alternativa: salva il '
                                 'DWG come DXF (AutoCAD 2018) dal tuo CAD e ricarica.',
                    }), 415
                return jsonify({'success': False, 'error': 'Conversione DWG fallita: ' + err_code + ' ' + str(conv.get('detail', ''))}), 500
            # Sposta il convertito (DXF) nella cartella preview persistente con nome originale
            saved_filename = os.path.splitext(saved_filename)[0] + '.dxf'
            tmp_path = os.path.join(prev_dir, saved_filename)
            try:
                import shutil
                shutil.move(conv, tmp_path)
            except Exception:
                tmp_path = conv  # fallback
        else:
            tmp_path = os.path.join(prev_dir, saved_filename)
            f.save(tmp_path)
        # Parsing con la STESSA pipeline dell'import batch (dxf_batch_worker):
        # cache (chiave = contenuto + nome file + config + Gemini), lavorazioni,
        # detector v3 (+ fallback v2/v1), cartiglio, spessore, fallback
        # descrizione ("45x12 sp.3"), auto-cleanup. Prima questa route ne aveva
        # una copia che divergeva (e una cache hit restituiva il DXF pulito
        # della cartella di un ALTRO preventivo).
        # Config rilevamento da app_config.json (sezione dxf_detection) — valori calibrati
        # sul config Preventivatore desktop (ratio_min=1.8, filtra_zona=True, ecc.)
        from .preventivi.dxf_batch_worker import process_single_dxf
        app_cfg = ConfigManager.load_config()
        dxf_cfg = app_cfg.get('dxf_detection') or {
            'dxf_colori_piega': [2], 'dxf_colori_saldatura': [1],
            'dxf_lunghezza_minima': 15.0, 'dxf_tolleranza_centro': 1.0,
            'dxf_svasatura_ratio_min': 1.8, 'dxf_svasatura_ratio_max': 3.0,
            'dxf_semicerchio_angolo_min': 150.0, 'dxf_semicerchio_angolo_max': 320.0,
            'dxf_filtra_zona_sviluppata': True,
        }
        payload = process_single_dxf(tmp_path, saved_filename, dxf_cfg)
        # Materiale del cartiglio che corrisponde a un materiale aggiunto (C75...)
        from .preventivi.materiali_personali import applica_a_risultato as _mat_pers
        payload = _mat_pers(payload, ((ConfigManager.load_config() or {}).get('laser_config') or {}).get('materiali'))
        if not payload.get('success'):
            try: os.remove(tmp_path)
            except OSError: pass
            return jsonify({'success': False, 'error': payload.get('error') or 'Parsing DXF fallito'}), 500
        # NOTA: tmp_path resta su disco (in uploads/preventivi_tmp/<preventivo_id>/<filename>.dxf)
        # per consentire la preview successiva. Cleanup quando preventivo viene
        # accettato/rifiutato/eliminato.
        # payload: {success, filename, lavorazioni, geometria, cartiglio
        # {materiale, materiale_raw, confidence}, spessore {spessore_mm, confidence,
        # source, details}, cleanup {cleaned_dxf_filename, cleaned_status, ...}}
        return jsonify(payload), 200
    except Exception as e:
        logger.exception('preventivi import dxf failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/rfq-diagnostic', methods=['GET'])
@richiede('commerciale')
def api_preventivi_rfq_diagnostic():
    """L'AI (Gemini) e' stata tolta il 2026-09-29: gli ordini si leggono dal
    testo del PDF, sul server. Nessuna chiamata esterna."""
    return jsonify({'ai': False, 'messaggio': 'Lettura ordini senza AI: nessun servizio esterno.'}), 200


def _zip_dalla_richiesta():
    """Il pacchetto del cliente come ZIP in memoria, dal form multipart.

    Accetta sia uno ZIP (campo "zip", flusso commerciale) sia file SCIOLTI
    (campo "files": PDF + DXF). I file sciolti vengono impacchettati in uno
    ZIP in memoria e passati alla stessa pipeline; "paths" porta i percorsi
    relativi di una cartella trascinata (le sottocartelle sono gli assiemi).
    Ritorna (zip_bytes, file_caricato); (None, None) se non c'e' nessun file.
    Usato dall'import del preventivatore e dal pacchetto d'ordine dell'ufficio.
    """
    f = request.files.get('zip')
    loose = [x for x in request.files.getlist('files') if x and x.filename]
    if f and f.filename:
        zip_bytes = f.read()
    elif len(loose) == 1 and loose[0].filename.lower().endswith('.zip'):
        zip_bytes = loose[0].read()
    elif loose:
        import io as _io
        import zipfile as _zipfile
        # Percorsi relativi (cartella trascinata o scelta): le sottocartelle
        # sono gli assiemi, come nello ZIP. Senza, tutto finiva alla radice.
        percorsi = request.form.getlist('paths')
        buf = _io.BytesIO()
        with _zipfile.ZipFile(buf, 'w', _zipfile.ZIP_DEFLATED) as zf:
            for i, uf in enumerate(loose):
                data = uf.read()
                if not data:
                    continue
                rel = percorsi[i] if i < len(percorsi) and percorsi[i] else uf.filename
                parti = [p for p in str(rel).replace('\\', '/').split('/')
                         if p and p not in ('.', '..') and ':' not in p]
                zf.writestr('/'.join(parti) or os.path.basename(uf.filename), data)
        zip_bytes = buf.getvalue()
    else:
        return None, None
    # nome per la nota "importato da …"
    return zip_bytes, (f if (f and f.filename) else (loose[0] if loose else None))


# Da dove viene la quantita' di un pezzo del pacchetto (rfq_importer.qta_fonte)
# → stato della riga d'ordine sul pezzo (extra 'ordine', lo stesso del preventivatore).
_STATO_RIGA_ORDINE = {'pdf': 'ok', 'dubbio': 'dubbio', 'senza_qta': 'senza_qta',
                      'non_trovato': 'non_trovato'}


def _stima_item_pacchetto(item: dict, app_cfg) -> None:
    """Costo base e scomposizione della stima di un pezzo del pacchetto (se ha
    materiale, spessore, area e perimetro)."""
    if not (item.get('materiale') and item.get('spessore_mm') and item.get('area_dm2')
            and item.get('perimetro_taglio_m')):
        return
    try:
        st = _laser_estimator.stima_base(item, app_cfg)
        item['costo_base_stimato'] = st.get('base') or 0
        item['stima_dettaglio'] = {k: st.get(k) for k in (
            'peso_kg', 'costo_materiale', 'costo_lavoro', 'setup_eur',
            'tempo_taglio_s', 'tempo_pierce_s', 'tempo_vuoto_s',
            'tempo_ausiliario_s', 'tempo_totale_min', 'base')}
        item['avvisi_stima'] = list(st.get('warnings') or [])[:10]
        item['stima_firma'] = _firma_tariffe(app_cfg)
        for k in ('ricetta_mancante', 'materiale_sconosciuto', 'spessore_fuori_tabella'):
            item[k] = bool(st.get(k))
    except Exception:
        logger.exception('stima import RFQ fallita per %s', item.get('codice'))


def _verifica_pezzi_pacchetto(prev_dir: str, items: list, app_cfg, materiali) -> dict:
    """Verifica automatica (preventivi/verifica_ordine.py) di ogni pezzo con
    disegno: scrive su ogni item 'esito_verifica', 'verifica' e, se il contorno
    e' stato corretto col peso del cartiglio, la geometria nuova e
    'contorno_auto' da confermare (il DXF pulito per Lantek era del contorno
    vecchio: si toglie, il pezzo va "da preparare"). Ritorna i conteggi."""
    from .preventivi.verifica_ordine import verifica_ordine
    from . import lantek as _lt
    con_disegno = [it for it in items if it.get('dxf_filename') and not it.get('_errore_dxf')]
    if not con_disegno:
        return {}
    # come quei codici sono nell'archivio di Lantek (gia' tagliati in passato)
    lpc = {}
    try:
        info = _lt.pezzi_in_lantek([it['codice'] for it in con_disegno if it.get('codice')])
        if info.get('disponibile'):
            for cod, p in (info.get('pezzi') or {}).items():
                if p.get('esiste') and p.get('e_pezzo') is not False and p.get('area_dm2'):
                    lpc[cod] = {'codice': p.get('codice_lantek'), 'area_dm2': p['area_dm2'],
                                'perimetro_m': p.get('perimetro_m'), 'spessore': p.get('spessore'),
                                'materiale': p.get('materiale')}
    except Exception:
        logger.warning('archivio Lantek non letto per la verifica', exc_info=True)
    cfg = (app_cfg or {}).get('dxf_detection', {}) if isinstance(app_cfg, dict) else {}
    risultati = verifica_ordine(prev_dir, con_disegno, dxf_cfg=cfg, materiali=materiali,
                                lantek_per_codice=lpc)
    conti = {}
    for it, r in zip(con_disegno, risultati):
        esito = (r or {}).get('esito') or {}
        it['esito_verifica'] = esito
        conti[esito.get('stato')] = conti.get(esito.get('stato'), 0) + 1
        if (r or {}).get('verifica'):
            it['verifica'] = r['verifica']
        corr = (r or {}).get('correzione')
        if corr:
            for k in ('area_dm2', 'perimetro_taglio_m', 'n_forature', 'bbox_w_mm', 'bbox_h_mm', 'geometry_source'):
                if corr.get(k) is not None:
                    it[k] = corr[k]
            it['contorno_auto'] = corr.get('contorno_auto')
            it['cleaned_dxf_filename'] = None
            it['cleaned_status'] = None
            _stima_item_pacchetto(it, app_cfg)
    logger.info('verifica automatica pacchetto: %s', conti)
    return conti


def _crea_preventivo_da_pacchetto(result, zip_bytes, nome_file, *, creato_da,
                                  da_prezzare=False, solo_tecnico=False):
    """Dal pacchetto letto (rfq_importer.process_rfq_package) al preventivo
    con i suoi pezzi: salva PDF, tavole, DXF e STEP in preventivi_tmp/<id>/,
    analizza i DXF in parallelo (detector v3 + scanner), unisce i dati del
    PDF con quelli dei disegni, stima i costi, salva articoli e assiemi.

    E' la stessa strada per il preventivatore (import-rfq-package) e per
    l'ordine caricato dall'ufficio col pacchetto (solo_tecnico=True): i pezzi
    di un ordine devono uscire identici a quelli di un preventivo.

    Ritorna {preventivo_id, articoli_db, saved_tasks, n_step, dxf_results},
    oppure None se il preventivo non si e' potuto creare.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from .preventivi.dxf_batch_worker import process_single_dxf

    # 2. Crea preventivo BOZZA
    data_consegna_dt = None
    if result.data_consegna:
        try:
            data_consegna_dt = datetime.strptime(result.data_consegna[:10], '%Y-%m-%d')
        except (ValueError, TypeError):
            pass
    new_prev = PreventivoManager.create(
        cliente=result.cliente,
        created_by=creato_da,
        quantita=1,
        numero_ordine_cliente=result.numero_ordine_cliente or None,
        margine_pct=25.0,
        data_consegna_proposta=data_consegna_dt,
        note=result.note or (f'Importato da {nome_file}' if nome_file else ''),
        da_prezzare=da_prezzare,
        solo_tecnico=solo_tecnico,
    )
    if not new_prev or 'id' not in new_prev:
        return None
    preventivo_id = new_prev['id']

    # 3. Scrivi DXF su disco (solo quelli matchati)
    prev_dir = _cartella_preventivo(preventivo_id)
    os.makedirs(prev_dir, exist_ok=True)

    # Salva il PDF ordine originale: è il documento con disegni/lavorazioni che
    # Mirko deve vedere. All'accettazione viene allegato all'ordine FerroTrack.
    rfq_pdf_bytes = getattr(result, 'pdf_bytes', None)
    rfq_pdf_filename = getattr(result, 'pdf_filename', None) or 'ordine.pdf'
    if rfq_pdf_bytes:
        try:
            pdf_target = os.path.join(prev_dir, os.path.basename(rfq_pdf_filename))
            with open(pdf_target, 'wb') as fp:
                fp.write(rfq_pdf_bytes)
            _segna_ordine_cliente(prev_dir, os.path.basename(rfq_pdf_filename))
        except Exception:
            logger.warning('salvataggio PDF ordine RFQ fallito: %s', rfq_pdf_filename)

    # PDF dei disegni (tavole del cliente, con quote e note): si salvano
    # tutti e si abbinano ai pezzi per nome, come nell'import da cartella.
    # Prima si teneva solo il PDF dell'ordine: i pezzi mostravano solo il
    # DXF e gli assiemi senza STEP non avevano nulla da guardare.
    def _chiave_disegno(n):
        stem = os.path.splitext(os.path.basename(n or ''))[0]
        return re.sub(r'\s*\(\d+\)$', '', stem).strip().lower().replace(' ', '-')
    pdf_per_chiave = {}
    try:
        import io as _io2
        import zipfile as _zf2
        with _zf2.ZipFile(_io2.BytesIO(zip_bytes)) as _z:
            for info in _z.infolist():
                base = os.path.basename(info.filename)
                if (info.is_dir() or not base.lower().endswith('.pdf') or '__MACOSX' in info.filename
                        or base == os.path.basename(rfq_pdf_filename)):
                    continue
                dati = _z.read(info)
                if not dati.startswith(b'%PDF-'):
                    continue
                salvato = _nome_disegno_libero(prev_dir, base)
                with open(os.path.join(prev_dir, salvato), 'wb') as fp:
                    fp.write(dati)
                pdf_per_chiave.setdefault(_chiave_disegno(base), salvato)
    except Exception:
        logger.exception('salvataggio PDF disegni RFQ fallito')

    dxf_map = getattr(result, 'dxf_map', {}) or {}
    saved_tasks = []  # (dxf_path, nome_salvato, nome_originale)
    matched_dxfs = {a.matched_dxf for a in result.articoli if a.matched_dxf}
    for fname, data in dxf_map.items():
        if fname not in matched_dxfs:
            continue  # skip DXF non associati (ma appaiono in dxf_no_match warning)
        target_path = os.path.join(prev_dir, os.path.basename(fname))
        with open(target_path, 'wb') as fp:
            fp.write(data)
        saved_tasks.append((target_path, os.path.basename(fname)))

    # 3b. Scrivi gli STEP su disco: il visore 3D li aggancia per nome
    # all'assieme (montaggio esatto = il vero wow 3D). Non serve matching:
    # li salviamo tutti, il frontend li abbina per codice.
    step_map = getattr(result, 'step_map', {}) or {}
    n_step = 0
    for sname, sdata in step_map.items():
        try:
            with open(os.path.join(prev_dir, os.path.basename(sname)), 'wb') as fp:
                fp.write(sdata)
            n_step += 1
        except Exception:
            logger.warning('salvataggio STEP fallito: %s', sname)

    # 4. Processa i DXF in parallelo (detector v3 + scanner dettagli)
    app_cfg = ConfigManager.load_config()
    dxf_cfg = app_cfg.get('dxf_detection') or {
        'dxf_colori_piega': [2], 'dxf_colori_saldatura': [1],
        'dxf_lunghezza_minima': 15.0, 'dxf_tolleranza_centro': 1.0,
        'dxf_svasatura_ratio_min': 1.8, 'dxf_svasatura_ratio_max': 3.0,
        'dxf_semicerchio_angolo_min': 150.0, 'dxf_semicerchio_angolo_max': 320.0,
        'dxf_filtra_zona_sviluppata': True,
    }
    dxf_results: dict[str, dict] = {}
    from .preventivi.materiali_personali import applica_a_risultato as _mat_pers_rfq, trova as _trova_mat
    _mats_rfq = ((ConfigManager.load_config() or {}).get('laser_config') or {}).get('materiali')
    if saved_tasks:
        max_workers = min(8, max(1, len(saved_tasks)))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(process_single_dxf, path, fname, dxf_cfg): fname
                for path, fname in saved_tasks
            }
            for fut in as_completed(futures):
                fname = futures[fut]
                try:
                    dxf_results[fname] = _mat_pers_rfq(fut.result(), _mats_rfq)
                except Exception as e:
                    logger.exception('rfq worker fail per %s', fname)
                    dxf_results[fname] = {'success': False, 'error': str(e)}

    # 5. Costruisci articoli DB combinando dati PDF + DXF
    # L'assieme NON è un pezzo tagliabile: il suo disegno 2D (master) non va
    # parsato né prezzato. È rappresentato dal record ASSIEME (rollup dei
    # componenti + montaggio) e dallo STEP per il 3D. Quindi salto il pezzo
    # il cui codice è un codice-assieme.
    assiemi_set = {c for c in (getattr(result, 'assiemi', None) or [])}
    n_master_saltati = 0
    articoli_db = []
    for a in result.articoli:
        if a.codice in assiemi_set:
            n_master_saltati += 1
            continue  # master assieme: rappresentato dal record assieme, non tagliabile
        dxf_info = dxf_results.get(a.matched_dxf) if a.matched_dxf else None
        # process_single_dxf risponde con 'geometria', 'lavorazioni' e
        # 'cartiglio' (come l'import batch): qui si leggevano 'geometry',
        # le lavorazioni al primo livello e 'cartiglio_materiale', che non
        # esistono → ogni pezzo importato con l'AI arrivava senza area,
        # perimetro, lavorazioni e materiale del cartiglio.
        geom = (dxf_info or {}).get('geometria') or (dxf_info or {}).get('geometry') or {}
        lav = (dxf_info or {}).get('lavorazioni') or dxf_info or {}
        item = {
            'codice': a.codice,
            'quantita': a.quantita,
            'codice_assieme': getattr(a, 'codice_assieme', None),  # da sottocartella ZIP
            'materiale': _trova_mat(a.materiale, _mats_rfq) or a.materiale,  # dal PDF (o None); C75... se aggiunto
            'spessore_mm': a.spessore_mm,      # dal PDF (o None)
            'area_dm2': geom.get('area_dm2', 0),
            'perimetro_taglio_m': geom.get('perimetro_taglio_m', 0),
            'n_forature': geom.get('n_pierce', 0),
            'dxf_filename': a.matched_dxf,
            'pieghe': lav.get('pieghe', 0) or 0,
            'saldatura_ml': lav.get('saldatura_ml', 0) or 0,
            'filettatura_pz': lav.get('filettatura_pz', 0) or 0,
            'svasatura_pz': lav.get('svasatura_pz', 0) or 0,
            # come l'import batch: affidabilita' del contorno, ingombro,
            # spostamenti a vuoto (campi extra dell'articolo)
            'dxf_confidence': geom.get('confidence'),
            'dxf_needs_verify': bool(geom.get('needs_manual_select')),
            'bbox_w_mm': geom.get('bbox_width_mm'),
            'bbox_h_mm': geom.get('bbox_height_mm'),
            'lunghezza_vuoto_mm': geom.get('lunghezza_vuoto_mm'),
            # DXF pulito per Lantek scritto dall'analisi: prima non si registrava
            # e all'accettazione tutti i pezzi del pacchetto finivano "da preparare"
            'cleaned_dxf_filename': ((dxf_info or {}).get('cleanup') or {}).get('cleaned_dxf_filename'),
            'cleaned_status': ((dxf_info or {}).get('cleanup') or {}).get('cleaned_status'),
            # tavola PDF del pezzo (stesso nome del DXF o del codice)
            'pdf_filename': (pdf_per_chiave.get(_chiave_disegno(a.matched_dxf)) if a.matched_dxf else None)
                             or pdf_per_chiave.get(_chiave_disegno(a.codice)),
            # Non salvati (chiavi con "_"): servono alla revisione dell'ufficio.
            '_descrizione': getattr(a, 'descrizione', '') or '',
            '_qta_fonte': getattr(a, 'qta_fonte', 'pdf') or 'pdf',
            '_errore_dxf': ((dxf_info or {}).get('error')
                            if dxf_info and dxf_info.get('success') is False else None),
        }
        if a.matched_dxf:
            # come e' stato abbinato il disegno: "per somiglianza" va confermato
            # (blocca Lantek finche' nessuno lo guarda)
            tipo = getattr(a, 'abbinamento', None) or 'esatto'
            item['abbinamento'] = {'tipo': tipo, 'file': a.matched_dxf,
                                   'score': round(float(getattr(a, '_matched_score', 0) or 0), 3),
                                   'confermato': tipo in ('esatto', 'senza_revisione')}
        if solo_tecnico:
            # Riga dell'ordine del cliente: dice all'ufficio quali quantita'
            # ricontrollare, e alla conferma quali disegni l'ordine non
            # chiedeva (entrano solo se l'ufficio da' loro una quantita').
            item['ordine'] = {'stato': _STATO_RIGA_ORDINE.get(item['_qta_fonte'], 'ok'),
                              'pos': len(articoli_db), 'qta': a.quantita,
                              'file': os.path.basename(rfq_pdf_filename)}
            if len(getattr(a, 'righe_ordine', None) or []) > 1:
                # codice ripetuto su piu' righe del PDF: quantita' sommate
                item['ordine']['righe'] = a.righe_ordine
        # Se il PDF NON aveva materiale/spessore, prova a leggerli dal cartiglio DXF
        if not item['materiale'] and dxf_info:
            cart = dxf_info.get('cartiglio') or dxf_info.get('cartiglio_materiale') or {}
            cart_mat = cart.get('materiale') if (cart.get('confidence') or 0) >= 0.5 else None
            if cart_mat:
                item['materiale'] = cart_mat
        if not item['spessore_mm'] and dxf_info:
            # come per il materiale: dal disegno solo se la lettura e' sicura
            # almeno a meta' (prima si prendeva anche un tabellare incerto)
            sp_info = dxf_info.get('spessore') or {}
            cart_sp = sp_info.get('spessore_mm')
            if cart_sp and (sp_info.get('confidence') is None or (sp_info.get('confidence') or 0) >= 0.5):
                item['spessore_mm'] = float(cart_sp)
        # Arrotonda lo spessore agli spessori realmente tagliati (1-1.5-2-3-4-…)
        if item.get('spessore_mm'):
            from .preventivi.pick_part import _snap_stock
            item['spessore_mm'] = _snap_stock(float(item['spessore_mm']))
        # Costo stimato subito, come l'import dei disegni: prima nasceva a 0
        # e si calcolava solo aprendo i pezzi uno per uno (LS 1184: 37 pezzi
        # su 70 a 0 € dopo l'import, e nessun avviso).
        _stima_item_pacchetto(item, app_cfg)
        articoli_db.append(item)

    # Ordine caricato dall'ufficio: ogni pezzo si controlla e, se serve, si
    # corregge SUBITO, come nel preventivatore (peso del cartiglio, quote,
    # STEP) e in piu' contro l'archivio di Lantek per i codici gia' tagliati.
    # Cosi' l'ordine arriva al laser gia' controllato: restano da guardare
    # solo i pezzi dubbi (Stefano, 07/10/2026).
    if solo_tecnico and articoli_db:
        try:
            _verifica_pezzi_pacchetto(prev_dir, articoli_db, app_cfg, _mats_rfq)
        except Exception:
            logger.exception('verifica automatica del pacchetto fallita')

    if n_master_saltati:
        result.warnings.append(
            f'{n_master_saltati} disegno/i assieme (master) non prezzati come pezzo — '
            f'l\'assieme costa come somma componenti + montaggio')
    # Materiale e spessore mancanti si contano DOPO i cartigli dei DXF: il
    # conteggio fatto sul solo PDF segnalava 46 "senza materiale" su LS 1184
    # quando i disegni li avevano quasi tutti.
    result.warnings = [w for w in result.warnings
                       if not re.search(r'articoli senza (materiale|spessore) specificato', w)]
    n_no_mat = sum(1 for it in articoli_db if not it.get('materiale'))
    n_no_sp = sum(1 for it in articoli_db if not it.get('spessore_mm'))
    if n_no_mat:
        result.warnings.append(f'{n_no_mat} articoli senza materiale (ne\' nell\'ordine ne\' nel cartiglio)')
    if n_no_sp:
        result.warnings.append(f'{n_no_sp} articoli senza spessore (ne\' nell\'ordine ne\' nel cartiglio)')

    # Salva articoli
    if articoli_db:
        try:
            PreventivoManager.replace_articoli(preventivo_id, articoli_db)
        except Exception as ae:
            logger.warning('replace_articoli fallito: %s', ae)
            result.warnings.append(f'Errore salvataggio articoli: {ae}')

    # Crea i record ASSIEME riconosciuti dalle sottocartelle
    if getattr(result, 'assiemi', None):
        assiemi_db = []
        for cod in result.assiemi:
            assiemi_db.append({'codice_assieme': cod, 'qty': 1,
                               'componenti_qty': {}, 'ore_montaggio': 0, 'costo': 0})
        try:
            PreventivoManager.replace_assiemi(preventivo_id, assiemi_db)
        except Exception as ae:
            logger.warning('replace_assiemi fallito: %s', ae)

    return {'preventivo_id': preventivo_id, 'articoli_db': articoli_db,
            'saved_tasks': saved_tasks, 'n_step': n_step, 'dxf_results': dxf_results}


@app.route('/api/preventivi/import-rfq-package', methods=['POST'])
@richiede(*UFFICI)
def api_preventivi_import_rfq_package():
    """AI RFQ Importer: da ZIP (PDF ordine + cartella DXF) → preventivo BOZZA pronto.

    Workflow:
    1. Upload ZIP multipart (con PDF + DXFs)
    2. Lettura del PDF senza AI (formato DECA o codici dei DXF) → righe d'ordine
    3. Fuzzy match articoli PDF ↔ file DXF (per codice)
    4. Crea preventivo BOZZA + scrive DXF su disco + processa ognuno
       (detector v3 + scanner dettagli + cache)
    5. Response: preventivo_id, articoli, warnings

    I passi 2-5 stanno in _crea_preventivo_da_pacchetto, condivisi con
    l'ordine caricato dall'ufficio col pacchetto del cliente.

    Body multipart/form-data:
        zip: file .zip contenente PDF ordine + N file .dxf
        admin_id: id utente

    Response 200: {success:True, preventivo_id, cliente, n_articoli, warnings, articoli:[...]}
    Response 400/403/500: {success:False, error}
    """
    from .preventivi import rfq_importer
    try:
        admin_id = _chi()
        # Anche l'Ufficio (Elena) può caricare una richiesta: crea la BOZZA
        # "da prezzare" e la gira al commerciale. Non prezza né tocca il CAD.
        # Lo dice la STAZIONE, non il ruolo dichiarato: senza questo la bozza
        # non risulta "da prezzare" e il commerciale non sa che c'e' da fare.
        is_intake_elena = identita().stazione == 'ufficio'
        _caller = {'name': _chi_nome()}

        zip_bytes, f = _zip_dalla_richiesta()
        if zip_bytes is None:
            return jsonify({'success': False, 'error': 'Nessun file: carica il PDF della richiesta e i DXF (oppure uno ZIP)'}), 400
        if not zip_bytes:
            return jsonify({'success': False, 'error': 'File vuoto'}), 400

        # 1. Pipeline AI extraction
        result = rfq_importer.process_rfq_package(zip_bytes)
        if not result.success:
            return jsonify({'success': False, 'error': result.error or 'RFQ parsing fallito'}), 400

        # 1b. Anti-doppione: se esiste già un preventivo con lo stesso numero
        # ordine cliente, avvisa PRIMA di crearne un altro (a meno di force=1).
        force = str(request.form.get('force', '')).lower() in ('1', 'true', 'yes')
        if not force:
            esistente = PreventivoManager.find_by_numero_ordine(result.numero_ordine_cliente)
            if esistente:
                return jsonify({
                    'success': False,
                    'duplicato': True,
                    'esistente': esistente,
                    'cliente': result.cliente,
                    'numero_ordine_cliente': result.numero_ordine_cliente,
                }), 200

        # 2-5. Preventivo, file, disegni analizzati, articoli e assiemi
        esito = _crea_preventivo_da_pacchetto(
            result, zip_bytes, f.filename if f else '',
            creato_da=admin_id, da_prezzare=is_intake_elena)
        if not esito:
            return jsonify({'success': False, 'error': 'Creazione preventivo fallita'}), 500
        preventivo_id = esito['preventivo_id']
        articoli_db = esito['articoli_db']
        saved_tasks = esito['saved_tasks']
        n_step = esito['n_step']

        # Audit
        try:
            AuditManager.log(
                user_id=admin_id, action='RFQ_IMPORT',
                entity_type='preventivi', entity_id=preventivo_id,
                detail=f'AI RFQ importato: {len(articoli_db)} articoli, {len(saved_tasks)} DXF',
            )
        except Exception:
            pass

        # Se caricato da Elena → avvisa il commerciale che c'è una richiesta da prezzare.
        if is_intake_elena:
            try:
                for u in UserManager.get_all_users() or []:
                    if not u.get('is_active', True):
                        continue
                    if u.get('role') in ('Commerciale', 'Amministratore'):
                        NotificationManager.create_notification(
                            user_id=u['id'],
                            order_id=None,
                            title='Nuova richiesta da prezzare',
                            message=f'{result.cliente} — {len(articoli_db)} pezzi, caricata da {_caller.get("name") or "Elena"}',
                            notification_type='preventivo',
                            notification_category='attiva',
                        )
            except Exception as exc:
                logger.warning('notifica commerciale nuova richiesta fallita: %s', exc)

        return jsonify({
            'success': True,
            'preventivo_id': preventivo_id,
            'cliente': result.cliente,
            'numero_ordine_cliente': result.numero_ordine_cliente,
            'data_consegna': result.data_consegna,
            'n_articoli': len(articoli_db),
            'n_assiemi': len(getattr(result, 'assiemi', []) or []),
            'assiemi': getattr(result, 'assiemi', []),
            'n_dxf_matched': len(saved_tasks),
            'n_step': n_step,
            'dxf_no_match': result.dxf_no_match,
            'warnings': result.warnings,
            # Elenco compatto per la rivelazione animata lato UI (non è la fonte
            # di verità: la geometria si conferma poi col click nel CAD interno).
            'articoli': [
                {
                    'codice': it.get('codice'),
                    'quantita': it.get('quantita'),
                    'materiale': it.get('materiale'),
                    'spessore_mm': it.get('spessore_mm'),
                    'assieme': it.get('codice_assieme'),
                    'ha_geometria': bool(it.get('area_dm2')),
                }
                for it in articoli_db
            ],
        }), 200
    except Exception as e:
        logger.exception('import-rfq-package failed')
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
#  ORDINE DAL PACCHETTO DEL CLIENTE (PDF d'ordine + disegni)
#  L'ufficio trascina la cartella che manda il cliente. Il pacchetto si legge
#  con la stessa pipeline dell'import del preventivatore e i pezzi finiscono
#  su un PREVENTIVO TECNICO nascosto (solo_tecnico, senza prezzi) a cui
#  l'ordine si aggancia con preventivo_id_origine: distinta, disegni e banco
#  lamiere funzionano come per un ordine nato da un preventivo accettato.
#  Due passi: /analizza (legge, l'ufficio rivede) e /conferma (crea l'ordine).
# ============================================================================

# Un'analisi lasciata a meta' (pagina chiusa) si butta dopo tanto tempo.
_ORE_PACCHETTO_ABBANDONATO = 24
# Due clic su "Crea ordine" non devono creare due ordini dallo stesso pacchetto.
_LOCK_PACCHETTO = threading.Lock()
_FONTE_QTA_UFFICIO = {'pdf': 'pdf', 'dubbio': 'dubbio', 'senza_qta': 'mancante',
                      'non_trovato': 'mancante'}


def _errore_pacchetto(codice, messaggio, stato=400, **extra):
    return jsonify({'success': False, 'codice': codice, 'error': messaggio, **extra}), stato


def _pulisci_pacchetti_abbandonati():
    """Butta le analisi mai confermate da piu' di un giorno (record + file).
    Si fa qui, all'inizio di una nuova analisi: non serve un processo a parte."""
    try:
        for pid in PreventivoManager.tecnici_abbandonati(_ORE_PACCHETTO_ABBANDONATO):
            if PreventivoManager.elimina_tecnico(pid):
                _cleanup_preventivo_files(pid)
                logger.info('Pacchetto d\'ordine %s abbandonato: eliminato', pid)
    except Exception:
        logger.exception('pulizia pacchetti abbandonati fallita')


def _ordine_gia_caricato(numero_ordine, cliente):
    """Un ordine non cancellato con lo stesso numero (e lo stesso cliente, se
    lo si conosce). Per avvisare di un doppione: stesso controllo, a meno
    delle maiuscole, del caricamento col solo PDF."""
    numero = (numero_ordine or '').strip().lower()
    if not numero:
        return None
    cli = (cliente or '').strip().lower()
    session = get_session()
    try:
        from sqlalchemy import func
        q = session.query(Order).filter(
            Order.is_deleted == False,  # noqa: E712
            func.lower(func.trim(Order.numero_ordine)) == numero)
        if cli:
            q = q.filter(func.lower(func.trim(Order.cliente)) == cli)
        o = q.first()
        return ({'id': o.id, 'numero_ordine': o.numero_ordine, 'cliente': o.cliente}
                if o else None)
    finally:
        session.close()


def _avvisa_capi_nuovo_ordine(order):
    """Notifica "Nuovo ordine" a tutti i capi officina (come il caricamento
    col solo PDF)."""
    numero_display = order.numero_ordine or order.id[:8]
    try:
        for u in UserManager.get_all_users() or []:
            if u.get('is_capo') and u.get('is_active', True):
                NotificationManager.create_notification(
                    user_id=u['id'],
                    order_id=order.id,
                    title='Nuovo ordine',
                    message=f'Ordine #{numero_display} ({order.cliente})',
                    notification_type='order',
                    notification_category='informativa'
                )
    except Exception as exc:
        logger.warning('notifica nuovo ordine ai capi fallita: %s', exc)


def _avvisi_riga_pacchetto(it, fonte):
    """Avvisi per l'UFFICIO su un pezzo del pacchetto: solo cio' che chi
    carica l'ordine sa controllare guardando il PDF (quantita', disegni che
    l'ordine non cita, righe senza disegno). Disegni, contorni, materiale e
    spessore li controlla il laser (Stefano, 07/10/2026: "l'impiegata non sa
    leggere i disegni"): vedi _avvisi_tecnici_pacchetto."""
    avvisi = []
    if fonte == 'non_trovato':
        avvisi.append("Disegno non citato nell'ordine: entra solo se gli dai una quantita'")
    elif fonte == 'senza_qta':
        avvisi.append("Quantita' non trovata nell'ordine: messa 1, controllala")
    elif fonte == 'dubbio':
        avvisi.append("Quantita' incerta: sulla stessa riga dell'ordine ci sono piu' codici")
    if not it.get('dxf_filename'):
        avvisi.append('Nessun disegno per questo pezzo')
    return avvisi


def _avvisi_tecnici_pacchetto(it):
    """Avvisi TECNICI su un pezzo del pacchetto (disegno, contorno, materiale,
    spessore): li vede e li risolve il laser in "Metti in Lantek", non
    l'ufficio."""
    avvisi = []
    ab = it.get('abbinamento') or {}
    if it.get('dxf_filename') and ab.get('tipo') == 'somiglianza' and not ab.get('confermato'):
        avvisi.append(f"Disegno abbinato solo per somiglianza: «{it['dxf_filename']}» per il codice "
                      f"«{it.get('codice')}». Controlla che sia il suo, altrimenti toglilo")
    if not it.get('dxf_filename'):
        pass
    elif it.get('_errore_dxf'):
        avvisi.append('Disegno non letto: ' + str(it['_errore_dxf'])[:120])
    else:
        conf = it.get('dxf_confidence')
        if it.get('dxf_needs_verify') or (isinstance(conf, (int, float)) and conf < 0.5):
            avvisi.append("Contorno del disegno incerto: l'ingombro va controllato")
    ev = it.get('esito_verifica') or {}
    if ev.get('stato') == 'corretto_da_confermare':
        avvisi.append('Contorno corretto in automatico col peso del cartiglio: da confermare al laser')
    elif ev.get('stato') == 'da_guardare':
        avvisi.append('Controllo automatico: ' + '; '.join(ev.get('motivi') or ['da guardare']))
    if not it.get('materiale'):
        avvisi.append('Materiale da indicare')
    if not it.get('spessore_mm'):
        avvisi.append('Spessore da indicare')
    return avvisi


@app.route('/api/orders/da-pacchetto/analizza', methods=['POST'])
@richiede('ufficio')
def api_ordine_pacchetto_analizza():
    """Legge il pacchetto del cliente (PDF d'ordine + disegni) senza creare
    l'ordine: l'ufficio rivede quantita', materiali e spessori, poi conferma.

    Multipart: `zip` oppure `files` (+ `paths` per le cartelle), `user_id`.
    Crea il preventivo tecnico nascosto (pacchetto_id) con i pezzi e i file.
    """
    from .preventivi import rfq_importer
    try:
        user_id = _chi()
        _pulisci_pacchetti_abbandonati()

        zip_bytes, file_caricato = _zip_dalla_richiesta()
        if zip_bytes is None:
            return _errore_pacchetto('nessun_file',
                                     "Nessun file: trascina la cartella del cliente, lo ZIP "
                                     "oppure il PDF dell'ordine con i disegni.")
        if not zip_bytes:
            return _errore_pacchetto('file_vuoto', 'Il file caricato e\' vuoto.')

        result = rfq_importer.process_rfq_package(zip_bytes, tollerante=True)
        if not result.success:
            return _errore_pacchetto('pacchetto_illeggibile',
                                     result.error or 'Non riesco a leggere il pacchetto.')

        # I disegni che l'ordine non cita si analizzano lo stesso: l'ufficio
        # li vede e decide se aggiungerli (quantita' > 0) o lasciarli fuori.
        for fn in list(result.dxf_no_match or []):
            result.articoli.append(rfq_importer.ArticoloRFQ(
                codice=os.path.splitext(fn)[0], quantita=1, matched_dxf=fn,
                qta_fonte='non_trovato'))

        esito = _crea_preventivo_da_pacchetto(
            result, zip_bytes, file_caricato.filename if file_caricato else '',
            creato_da=user_id, solo_tecnico=True)
        if not esito:
            return _errore_pacchetto('errore_interno', 'Non riesco a salvare il pacchetto. Riprova.', 500)
        pid = esito['preventivo_id']

        # Id dei pezzi salvati: stessa posizione della lista (extra 'riga').
        salvato = PreventivoManager.get(pid, include_children=True) or {}
        per_riga = {a.get('riga'): a for a in salvato.get('articoli') or []}
        righe = []
        for i, it in enumerate(esito['articoli_db']):
            a = per_riga.get(i) or {}
            fonte = it.get('_qta_fonte') or 'pdf'
            dxf = it.get('dxf_filename')
            conf = it.get('dxf_confidence')
            righe.append({
                'id': a.get('id'),
                'codice': it.get('codice') or '',
                'descrizione': it.get('_descrizione') or '',
                # 0 = "non entra nell'ordine": il disegno non c'e' nell'ordine
                'quantita': 0 if fonte == 'non_trovato' else int(it.get('quantita') or 1),
                'quantita_fonte': _FONTE_QTA_UFFICIO.get(fonte, 'pdf'),
                'fuori_ordine': fonte == 'non_trovato',
                'assieme': it.get('codice_assieme'),
                'materiale': it.get('materiale'),
                'spessore_mm': it.get('spessore_mm'),
                'bbox_w_mm': it.get('bbox_w_mm'),
                'bbox_h_mm': it.get('bbox_h_mm'),
                'area_dm2': it.get('area_dm2') or None,
                'confidenza': round(float(conf), 3) if isinstance(conf, (int, float)) else None,
                'disegno': dxf,
                'url_svg': (f'/api/preventivi/{pid}/dxf/{quote(dxf)}/svg'
                            if dxf and dxf.lower().endswith('.dxf') else None),
                'avvisi': _avvisi_riga_pacchetto(it, fonte),
                # disegno/contorno/materiale/spessore: li controlla il laser
                'avvisi_tecnici': _avvisi_tecnici_pacchetto(it) if dxf else [],
            })

        cliente = (result.cliente or '').strip()
        if cliente == 'Cliente da specificare':
            cliente = ''   # il segnaposto del preventivatore non e' un cliente
        numero = (result.numero_ordine_cliente or '').strip()
        pdf_nome = getattr(result, 'pdf_filename', None)

        try:
            AuditManager.log(
                user_id=user_id, action='ORDER_PACKAGE_ANALYZE',
                entity_type='preventivi', entity_id=pid,
                detail=f'Pacchetto d\'ordine letto: {len(righe)} pezzi, '
                       f'{len(esito["saved_tasks"])} DXF')
        except Exception:
            pass

        return jsonify({
            'success': True,
            'pacchetto_id': pid,
            'cliente': cliente,
            'numero_ordine': numero,
            'data_consegna': (result.data_consegna or '')[:10] or None,
            'note': result.note or '',
            'pdf': ({'nome': pdf_nome,
                     'url': f'/api/preventivi/{pid}/disegni-pdf/{quote(os.path.basename(pdf_nome))}'}
                    if getattr(result, 'pdf_bytes', None) and pdf_nome else None),
            'righe': righe,
            'assiemi': [{'codice': cod,
                         'n_pezzi': sum(1 for r in righe if r['assieme'] == cod)}
                        for cod in (getattr(result, 'assiemi', None) or [])],
            'tubolari': [],   # la lettura del pacchetto non ne produce (vengono dagli STEP)
            'dxf_senza_riga': list(result.dxf_no_match or []),
            'righe_senza_disegno': [r['codice'] for r in righe
                                    if not r['disegno'] and not r['fuori_ordine']],
            'avvisi': list(result.warnings or []),
            'ordine_esistente': _ordine_gia_caricato(numero, cliente),
        }), 200
    except Exception as e:
        logger.exception('analisi pacchetto ordine fallita')
        return _errore_pacchetto('errore_interno', 'Errore nella lettura del pacchetto: ' + str(e), 500)


def _leggi_correzioni_pacchetto(prev, righe_in):
    """Valida le correzioni dell'ufficio e ritorna (articoli, errore).

    Le righe non nominate tengono i loro valori; quantita' 0 toglie il pezzo;
    i disegni che l'ordine non citava entrano solo con una quantita' > 0.
    """
    articoli = prev.get('articoli') or []
    per_id = {a['id']: a for a in articoli}
    correzioni = {}
    if righe_in is not None and not isinstance(righe_in, list):
        return None, ('righe_non_valide', 'Le righe corrette non sono una lista.')
    for r in righe_in or []:
        if not isinstance(r, dict) or r.get('id') not in per_id:
            return None, ('riga_sconosciuta',
                          'Una riga corretta non fa parte di questo pacchetto: ricarica la pagina.')
        c = {}
        cod = per_id[r['id']].get('codice') or '?'
        if 'quantita' in r:
            try:
                q = float(r['quantita'])
                if q != int(q) or q < 0 or q > 100000:
                    raise ValueError
                c['quantita'] = int(q)
            except (TypeError, ValueError):
                return None, ('quantita_non_valida',
                              f"Quantita' non valida per {cod}: serve un numero intero da 0 in su.")
        if 'spessore_mm' in r:
            v = r['spessore_mm']
            if v in (None, ''):
                c['spessore_mm'] = None
            else:
                try:
                    v = float(str(v).replace(',', '.'))
                    if not (0 < v <= 200):
                        raise ValueError
                    c['spessore_mm'] = round(v, 2)
                except (TypeError, ValueError):
                    return None, ('spessore_non_valido',
                                  f'Spessore non valido per {cod}: scrivi i millimetri (es. 3 o 1,5).')
        if 'materiale' in r:
            m = str(r.get('materiale') or '').strip()
            if len(m) > 40:
                return None, ('materiale_non_valido', f'Materiale troppo lungo per {cod}.')
            c['materiale'] = m or None
        correzioni[r['id']] = c

    nuovi = []
    for a in articoli:
        c = correzioni.get(a['id'], {})
        od = a.get('ordine') if isinstance(a.get('ordine'), dict) else None
        fuori = bool(od and od.get('stato') == 'non_trovato')
        q = c['quantita'] if 'quantita' in c else (0 if fuori else int(a.get('quantita') or 0))
        if q <= 0:
            continue
        a = dict(a)
        a['quantita'] = q
        for k in ('materiale', 'spessore_mm'):
            if k in c:
                a[k] = c[k]
        if c and od:
            # controllata dall'ufficio: non e' piu' "da controllare"
            a['ordine'] = {**od, 'stato': 'controllato'}
        nuovi.append(a)
    return nuovi, None


@app.route('/api/orders/da-pacchetto/<pacchetto_id>/conferma', methods=['POST'])
@richiede('ufficio')
def api_ordine_pacchetto_conferma(pacchetto_id):
    """Crea l'ordine dal pacchetto analizzato, con le correzioni dell'ufficio.

    JSON: {user_id, numero_ordine, cliente, data_consegna (YYYY-MM-DD), note?,
           righe: [{id, quantita, materiale, spessore_mm}], forza?}
    `forza`: crea anche se esiste gia' un ordine con lo stesso numero.
    """
    try:
        data = request.get_json(silent=True) or {}
        user_id = _chi()

        numero = str(data.get('numero_ordine') or '').strip()
        cliente = str(data.get('cliente') or '').strip()
        data_consegna = str(data.get('data_consegna') or '').strip()
        note = str(data.get('note') or '').strip()
        if not numero:
            return _errore_pacchetto('numero_mancante', "Scrivi il numero dell'ordine.")
        if not cliente:
            return _errore_pacchetto('cliente_mancante', 'Scrivi il cliente.')
        if not data_consegna:
            return _errore_pacchetto('data_mancante', 'Scegli la data di consegna.')
        try:
            dc = datetime.strptime(data_consegna, '%Y-%m-%d').date()
        except ValueError:
            return _errore_pacchetto('data_non_valida',
                                     'La data di consegna non e\' valida (serve AAAA-MM-GG).')
        # Come il caricamento col solo PDF: tolleranza di un giorno.
        if dc < (datetime.utcnow() - timedelta(days=1)).date():
            return _errore_pacchetto('data_passata',
                                     f'La data di consegna {dc.strftime("%d/%m/%Y")} e\' nel passato.')

        with _LOCK_PACCHETTO:
            # Gia' confermato? (anche se l'ordine poi e' stato cancellato:
            # il pacchetto e' stato usato, non si riusa)
            session = get_session()
            try:
                gia = session.query(Order).filter(
                    Order.preventivo_id_origine == pacchetto_id).first()
                gia = {'id': gia.id, 'numero_ordine': gia.numero_ordine} if gia else None
            finally:
                session.close()
            if gia:
                return _errore_pacchetto(
                    'gia_confermato',
                    f"Da questo pacchetto e' gia' stato creato l'ordine {gia['numero_ordine'] or gia['id'][:8]}.",
                    409, order_id=gia['id'], numero_ordine=gia['numero_ordine'])
            prev = PreventivoManager.get_tecnico(pacchetto_id)
            if not prev:
                return _errore_pacchetto('pacchetto_non_trovato',
                                         'Pacchetto non trovato: forse e\' stato annullato. Ricaricalo.', 404)
            if prev.get('status') != 'BOZZA':
                return _errore_pacchetto('gia_confermato',
                                         "Questo pacchetto e' gia' stato usato per un ordine.", 409)

            nuovi, err = _leggi_correzioni_pacchetto(prev, data.get('righe'))
            if err:
                return _errore_pacchetto(*err)

            doppione = _ordine_gia_caricato(numero, cliente)
            if doppione and not data.get('forza'):
                return _errore_pacchetto(
                    'ordine_duplicato', f'Ordine #{numero} per {cliente} esiste gia\'.', 409,
                    ordine_esistente=doppione)

            # Correzioni dell'ufficio sul preventivo tecnico (ancora in BOZZA)
            r = PreventivoManager.replace_articoli(pacchetto_id, nuovi)
            if isinstance(r, dict) and r.get('error'):
                return _errore_pacchetto('righe_non_valide', 'Pezzi non salvati: ' + r['error'])
            PreventivoManager.aggiorna_tecnico(
                pacchetto_id, cliente=cliente, numero_ordine_cliente=numero,
                data_consegna_proposta=datetime.combine(dc, datetime.min.time()), note=note)

            # L'ordine, come il caricamento col solo PDF, ma agganciato ai pezzi
            order = OrderManager.create_order(
                cliente=cliente, data_consegna=data_consegna, numero_ordine=numero,
                note=note, origine='PACCHETTO', preventivo_id_origine=pacchetto_id)
            # Pacchetto usato: non si conferma una seconda volta
            PreventivoManager.aggiorna_tecnico(pacchetto_id, status='ACCETTATO')

        # Disegni nella cartella dell'ordine (li taglia il laser da li') e
        # PDF d'ordine allegato: le stesse funzioni dell'accettazione.
        dxf_stats = _copy_cleaned_dxf_to_drawings(pacchetto_id, order.id)
        try:
            export = _esporta_disegni_per_officina(order.id, cliente, numero)
        except Exception as _e:
            logger.warning('export disegni in cartella di rete: %s', _e)
            export = {'error': str(_e)}
        pdf_stats = _copy_order_pdf_to_order(pacchetto_id, order.id)

        # I file temporanei si cancellano solo se e' arrivato tutto quello che
        # doveva arrivare (stessa regola dell'accettazione). Un pezzo senza
        # disegno non e' un disegno perso: si contano solo i DXF attesi.
        cartella = os.path.join(UPLOAD_FOLDER, 'drawings', order.id)
        attesi = {os.path.basename(a['dxf_filename']) for a in nuovi if a.get('dxf_filename')}
        mancanti = sum(1 for n in attesi if not os.path.isfile(os.path.join(cartella, n)))
        src_dir = _cartella_preventivo(pacchetto_id)
        if _ordine_cliente_segnato(src_dir) and not pdf_stats.get('copied'):
            mancanti += 1
        avviso = None
        if mancanti:
            avviso = (f"{mancanti} file non sono passati all'ordine: gli originali sono "
                      "stati conservati. Controlla i disegni dell'ordine.")
            logger.warning('Pacchetto %s: %d file non trasferiti, sorgenti conservati',
                           pacchetto_id, mancanti)
        else:
            _cleanup_preventivo_files(pacchetto_id)

        _avvisa_capi_nuovo_ordine(order)
        try:
            OrderEventBus.publish('order.created', {
                'order_id': order.id, 'numero_ordine': numero, 'cliente': cliente,
                'origine': 'PACCHETTO', 'preventivo_id_origine': pacchetto_id})
        except Exception:
            pass
        try:
            AuditManager.log(
                user_id=user_id, action='CREATE_ORDER_FROM_PACKAGE',
                entity_type='orders', entity_id=order.id,
                detail=f'Ordine {numero} ({cliente}) dal pacchetto {pacchetto_id}: '
                       f'{len(nuovi)} pezzi, {len(attesi) - mancanti} disegni'
                       + (f', {mancanti} file non trasferiti' if mancanti else ''))
        except Exception:
            pass

        from .ordini_service import riga_ordine
        return jsonify({
            'success': True,
            'order_id': order.id,
            'numero_ordine': numero,
            'ordine': riga_ordine(order.id),
            'n_pezzi': len(nuovi),
            'dxf_transfer': dxf_stats,
            'pdf_transfer': pdf_stats,
            'export_disegni': export,
            'avviso': avviso,
        }), 201
    except Exception as e:
        logger.exception('conferma pacchetto ordine fallita')
        return _errore_pacchetto('errore_interno', 'Ordine non creato: ' + str(e), 500)


@app.route('/api/orders/da-pacchetto/<pacchetto_id>', methods=['DELETE'])
@richiede('ufficio')
def api_ordine_pacchetto_scarta(pacchetto_id):
    """L'ufficio annulla un'analisi: via il preventivo tecnico e i suoi file.
    Un pacchetto gia' diventato ordine non si tocca (la distinta sta li')."""
    try:
        data = request.get_json(silent=True) or {}
        user_id = _chi()
        if not PreventivoManager.e_tecnico(pacchetto_id):
            return _errore_pacchetto('pacchetto_non_trovato', 'Pacchetto non trovato.', 404)
        if not PreventivoManager.elimina_tecnico(pacchetto_id):
            return _errore_pacchetto('gia_confermato',
                                     "Questo pacchetto e' gia' diventato un ordine: non si annulla.", 409)
        _cleanup_preventivo_files(pacchetto_id)
        return jsonify({'success': True}), 200
    except Exception as e:
        logger.exception('annullamento pacchetto fallito')
        return _errore_pacchetto('errore_interno', str(e), 500)


@app.route('/api/preventivi/<preventivo_id>/import-dxf-batch', methods=['POST'])
@richiede('commerciale')
def api_preventivi_import_dxf_batch(preventivo_id):
    """Import batch di N file DXF con parsing parallelizzato.

    Target scala: 100 DXF in <30s. Combina:
    - Cache SHA256 (A5): file già visti = risposta istantanea
    - ThreadPoolExecutor con max 8 worker: parallelismo effettivo (ezdxf+
      Shapely rilasciano il GIL nelle chiamate C)
    - Skip errori singoli senza far cadere l'intero batch

    Body multipart/form-data:
        files: N file .dxf (o .dwg)
        admin_id: id utente

    Response: {success: bool, results: [{filename, ...payload o {error}}]}
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from .preventivi.dxf_batch_worker import process_single_dxf
    try:
        admin_id = _chi()
        files = request.files.getlist('files')
        if not files:
            return jsonify({'success': False, 'error': 'Nessun file inviato'}), 400
        # Percorsi relativi (webkitRelativePath) paralleli ai file, se caricata
        # una CARTELLA già estratta → riconoscimento assiemi dalle sottocartelle.
        rel_paths = request.form.getlist('paths')
        from .preventivi.rfq_importer import assiemi_from_paths, disegni_assieme_da_paths
        assieme_by_base = assiemi_from_paths(rel_paths) if rel_paths else {}
        # Disegno d'insieme di assiemi e sotto-assiemi: come nello ZIP non e' un
        # pezzo da tagliare (si prezzano i componenti + il montaggio).
        disegni_assieme = disegni_assieme_da_paths(rel_paths) if rel_paths else set()
        master_saltati = []
        # Salva tutti i file su disco (solo DXF; per DWG serve conversione singola)
        prev_dir = _cartella_preventivo(preventivo_id)
        os.makedirs(prev_dir, exist_ok=True)
        saved_tasks = []  # (dxf_path, filename)
        skipped = []
        n_step = 0
        for f in files:
            fname_lower = (f.filename or '').lower()
            # STEP degli assiemi (cartella caricata): come per lo ZIP si salvano
            # soltanto, per il visore 3D che li aggancia all'assieme per codice.
            # Niente analisi tubolari/piastre: i pezzi ci sono gia' come DXF.
            if fname_lower.endswith(('.step', '.stp')):
                f.save(os.path.join(prev_dir, os.path.basename(f.filename)))
                n_step += 1
                continue
            if os.path.basename(f.filename) in disegni_assieme:
                # non e' un pezzo da prezzare, ma si tiene: e' il disegno
                # d'insieme che la scheda assieme mostra quando manca lo STEP
                f.save(os.path.join(prev_dir, os.path.basename(f.filename)))
                master_saltati.append(os.path.basename(f.filename))
                continue
            if not fname_lower.endswith('.dxf'):
                skipped.append({
                    'filename': f.filename,
                    'error': 'Batch supporta solo .dxf (per .dwg usa import single)',
                })
                continue
            # In un albero di cartelle lo stesso nome ricorre spesso (ogni
            # fornitore ha la sua "flangia.dxf"): salvare col solo basename
            # faceva sparire il primo file. Si tiene traccia del nome originale
            # perche' l'assieme e' associato a quello.
            nome_originale = os.path.basename(f.filename)
            saved_filename = _nome_disegno_libero(prev_dir, nome_originale)
            tmp_path = os.path.join(prev_dir, saved_filename)
            f.save(tmp_path)
            saved_tasks.append((tmp_path, saved_filename, nome_originale))
        if not saved_tasks:
            return jsonify({'success': True, 'results': skipped, 'step_salvati': n_step,
                            'master_saltati': master_saltati}), 200
        # Config DXF (una volta per tutti)
        app_cfg = ConfigManager.load_config()
        dxf_cfg = app_cfg.get('dxf_detection') or {
            'dxf_colori_piega': [2], 'dxf_colori_saldatura': [1],
            'dxf_lunghezza_minima': 15.0, 'dxf_tolleranza_centro': 1.0,
            'dxf_svasatura_ratio_min': 1.8, 'dxf_svasatura_ratio_max': 3.0,
            'dxf_semicerchio_angolo_min': 150.0, 'dxf_semicerchio_angolo_max': 320.0,
            'dxf_filtra_zona_sviluppata': True,
        }
        # Parallelismo controllato: max 8 worker anche se ho 100 file
        results = list(skipped)
        max_workers = min(8, max(1, len(saved_tasks)))
        from .preventivi.materiali_personali import applica_a_risultato as _mat_pers_b
        _mats_b = ((ConfigManager.load_config() or {}).get('laser_config') or {}).get('materiali')
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(process_single_dxf, path, fname, dxf_cfg): (fname, originale)
                for path, fname, originale in saved_tasks
            }
            for fut in as_completed(futures):
                fname, originale = futures[fut]
                try:
                    r = fut.result()
                    # L'assieme e' associato al nome ORIGINALE: se il file e'
                    # stato rinominato per non sovrascriverne un altro, cercare
                    # col nome nuovo non troverebbe nulla.
                    if assieme_by_base:
                        r['codice_assieme'] = (assieme_by_base.get(originale)
                                               or assieme_by_base.get(fname))
                    results.append(_mat_pers_b(r, _mats_b))
                except Exception as e:
                    logger.exception('worker fail per %s', fname)
                    results.append({'success': False, 'filename': fname,
                                    'filename_originale': originale, 'error': str(e)})
        # Pre-warm SVG cache in background: subito dopo la response la UI
        # richiederà /svg per ogni file. Se la cache è fredda ezdxf ci mette
        # 0.5-1.5s per file → il primo caricamento della tabella articoli
        # mostra icone rotte. Popoliamo _SVG_CACHE in un thread separato senza
        # bloccare la response (l'utente vede i risultati subito).
        failed_fnames = {r.get('filename') for r in results if r.get('success') is False}
        # saved_tasks contiene terne (percorso, nome salvato, nome originale):
        # spacchettarle a coppie faceva fallire con 500 ogni import di piu' DXF.
        successful_paths = [(p, f) for (p, f, _orig) in saved_tasks if f not in failed_fnames]
        if successful_paths:
            threading.Thread(
                target=_prewarm_dxf_cache,
                args=(successful_paths,),
                daemon=True,
                name=f'dxf-prewarm-{preventivo_id[:8]}',
            ).start()
        assiemi_rilevati = sorted({v for v in assieme_by_base.values() if v})
        return jsonify({'success': True, 'results': results,
                        'assiemi': assiemi_rilevati, 'step_salvati': n_step,
                        'master_saltati': master_saltati}), 200
    except Exception as e:
        logger.exception('import-dxf-batch failed')
        return jsonify({'success': False, 'error': str(e)}), 500


def _prewarm_dxf_cache(tasks):
    """Rende SVG (thumbnail) E geometria (CAD interno) per una lista di
    (path, filename) in un thread pool, popolando le cache in-memory in background.
    Così i thumbnail e l'apertura del CAD rispondono in <50ms invece di 0.1-1.5s.
    Errori silenziati (il render live gestirà comunque).
    """
    from concurrent.futures import ThreadPoolExecutor
    detection_cfg = (ConfigManager.load_config() or {}).get('dxf_detection', {})
    # 4 worker (non 8): ezdxf ha race condition al cold-start dei moduli con
    # troppi thread paralleli (rilevato: primo run può perdere 1-2 file su 7).
    # Con 4 worker + moduli caldi va sempre a 7/7. I fallimenti residui vengono
    # comunque recuperati dal render live.
    max_workers = min(4, max(1, len(tasks)))
    def _one(item):
        path, fname = item
        try:
            _get_dxf_svg_cached(path)
        except Exception:
            logger.warning('prewarm SVG fail per %s', fname, exc_info=False)
        try:
            _get_dxf_geometry_cached(path, detection_cfg)
        except Exception:
            logger.warning('prewarm geometria fail per %s', fname, exc_info=False)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        list(pool.map(_one, tasks))
    logger.info('Prewarm DXF (SVG+geometria) completato (%d file)', len(tasks))


@app.route('/api/preventivi/<preventivo_id>/step-files', methods=['GET'])
@richiede(*UFFICI)
def api_preventivi_step_files_list(preventivo_id):
    """Elenca i file STEP (.step/.stp) caricati per il preventivo (in preventivi_tmp/<id>/)."""
    try:
        prev_dir = _cartella_preventivo(preventivo_id)
        if not os.path.isdir(prev_dir):
            return jsonify({'success': True, 'files': []}), 200
        files = []
        for name in sorted(os.listdir(prev_dir)):
            if name.lower().endswith(('.step', '.stp')):
                fp = os.path.join(prev_dir, name)
                try:
                    size = os.path.getsize(fp)
                except OSError:
                    size = 0
                files.append({'filename': name, 'size': size})
        return jsonify({'success': True, 'files': files}), 200
    except Exception as e:
        logger.exception('step_files_list failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/verifica-pezzo', methods=['GET'])
@richiede(*UFFICI)
def api_preventivi_verifica_pezzo(preventivo_id):
    """Dati indipendenti per il controllo di coerenza di un pezzo (sola lettura):
    peso del cartiglio del DXF e, se c'e' lo STEP dello stesso pezzo, i suoi dati
    di lamiera. Non tocca il riconoscimento del contorno.

    Query: dxf=<nome file DXF>, codice=<codice pezzo>.
    """
    try:
        from .preventivi.verifica_coerenza import verifica_pezzo
        prev_dir = _cartella_preventivo(preventivo_id)
        dxf = os.path.basename(request.args.get('dxf') or '') or None
        codice = request.args.get('codice') or None
        cfg = (ConfigManager.load_config() or {}).get('dxf_detection', {})
        return jsonify({'success': True, **verifica_pezzo(prev_dir, dxf, codice, cfg)}), 200
    except Exception as e:
        logger.exception('verifica-pezzo failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/profili-tubolari', methods=['GET'])
@richiede(*UFFICI)
def api_preventivi_profili_tubolari():
    """Catalogo dei profili (tubi quadri, rettangolari, tondi) con kg/m, per
    aggiungere un tubolare a mano. Sola lettura."""
    return jsonify({'success': True, 'profili': _PROFILI_TUBOLARI_DB or {}}), 200


@app.route('/api/preventivi/<preventivo_id>/distinte', methods=['GET'])
@richiede(*UFFICI)
def api_preventivi_distinte(preventivo_id):
    """Distinte base lette dai PDF d'insieme caricati (sola lettura).
    Query: codici=46SA00579-00,... (codici degli assiemi). Restituisce anche le
    distinte dei sotto-assiemi citati (es. il telaio di tubolari)."""
    try:
        from .preventivi.distinte_pdf import distinte_per_codici
        prev_dir = _cartella_preventivo(preventivo_id)
        codici = [c.strip() for c in (request.args.get('codici') or '').split(',') if c.strip()]
        return jsonify({'success': True, 'distinte': distinte_per_codici(prev_dir, codici)}), 200
    except Exception as e:
        logger.exception('distinte failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/proponi-contorno', methods=['GET'])
@richiede(*UFFICI)
def api_preventivi_proponi_contorno(preventivo_id):
    """Suggerisce il contorno del disegno che pesa quanto il cartiglio (sola
    lettura: l'operatore vede la forma e decide). Query: dxf, spessore,
    densita (kg/dm3), peso (kg, dal cartiglio)."""
    try:
        from .preventivi.verifica_coerenza import proponi_contorno
        prev_dir = _cartella_preventivo(preventivo_id)
        dxf_path = os.path.join(prev_dir, os.path.basename(request.args.get('dxf') or ''))
        if not os.path.isfile(dxf_path):
            return jsonify({'success': False, 'error': 'File DXF non trovato'}), 404

        def _f(k):
            try:
                return float(str(request.args.get(k) or '').replace(',', '.'))
            except ValueError:
                return 0.0
        cfg = (ConfigManager.load_config() or {}).get('dxf_detection', {})
        r = proponi_contorno(dxf_path, _f('spessore'), _f('densita'), _f('peso'), cfg)
        return jsonify({'success': True, **r}), 200
    except Exception as e:
        logger.exception('proponi-contorno failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/step/<path:filename>', methods=['GET'])
@richiede(*OFFICINA_LETTURA)     # anche il tablet: il modello 3D del pezzo
def api_preventivi_step_file(preventivo_id, filename):
    """Serve il file STEP raw (per viewer 3D preview-step.html che lo scarica via fetch)."""
    try:
        safe_name = os.path.basename(filename)
        prev_dir = _cartella_preventivo(preventivo_id)
        step_path = os.path.join(prev_dir, safe_name)
        if not os.path.exists(step_path):
            return jsonify({'error': 'File STEP non trovato'}), 404
        if not safe_name.lower().endswith(('.step', '.stp')):
            return jsonify({'error': 'Estensione file non valida'}), 400
        return send_file(step_path, mimetype='application/octet-stream',
                         as_attachment=False, download_name=safe_name)
    except Exception as e:
        logger.exception('step_file failed')
        return jsonify({'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/disegni-pdf', methods=['POST'])
@richiede('commerciale')
def api_preventivi_disegni_pdf_upload(preventivo_id):
    """PDF dei disegni del cliente (di solito nella stessa cartella dei DXF).

    Sono la tavola ufficiale, con quote e note leggibili: il preventivatore li
    mostra al posto dell'anteprima ricavata dal DXF. Qui si salvano soltanto;
    l'abbinamento al pezzo lo fa il browser (per nome) e resta sull'articolo.
    Form: files[] (+ admin_id). Risposta: [{originale, salvato}].
    """
    try:
        admin_id = _chi()
        files = request.files.getlist('files')
        if not files:
            return jsonify({'success': False, 'error': 'Nessun PDF ricevuto'}), 400
        prev_dir = _cartella_preventivo(preventivo_id)
        os.makedirs(prev_dir, exist_ok=True)
        salvati, scartati = [], []
        for f in files:
            nome = os.path.basename(f.filename or '')
            if not nome.lower().endswith('.pdf'):
                scartati.append(nome); continue
            testa = f.stream.read(5)
            f.stream.seek(0)
            if testa != b'%PDF-':            # non e' davvero un PDF
                scartati.append(nome); continue
            salvato = _nome_disegno_libero(prev_dir, nome)
            f.save(os.path.join(prev_dir, salvato))
            salvati.append({'originale': nome, 'salvato': salvato})
        return jsonify({'success': True, 'pdf': salvati, 'scartati': scartati}), 200
    except Exception as e:
        logger.exception('upload pdf disegni failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/file-disegni', methods=['GET'])
@richiede(*UFFICI)
def api_preventivi_file_disegni(preventivo_id):
    """Disegni salvati col preventivo (PDF e DXF originali, non i puliti).
    Servono alla scheda assieme: senza STEP si mostra il disegno d'insieme
    (13C050126-00: senza, non c'era modo di stimare la manodopera)."""
    try:
        prev_dir = _cartella_preventivo(preventivo_id)
        pdf, dxf = [], []
        if os.path.isdir(prev_dir):
            for nome in sorted(os.listdir(prev_dir)):
                basso = nome.lower()
                if '_cleaned' in basso or '_canonico' in basso:
                    continue
                if basso.endswith('.pdf'):
                    pdf.append(nome)
                elif basso.endswith('.dxf'):
                    dxf.append(nome)
        return jsonify({'success': True, 'pdf': pdf, 'dxf': dxf}), 200
    except Exception as e:
        logger.exception('file-disegni failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/disegni-pdf/<path:filename>', methods=['GET'])
@richiede(*UFFICI)
def api_preventivi_disegno_pdf(preventivo_id, filename):
    """Restituisce un PDF di disegno salvato con il preventivo."""
    try:
        safe_name = os.path.basename(filename)
        if not safe_name.lower().endswith('.pdf'):
            return jsonify({'error': 'Estensione file non valida'}), 400
        path = os.path.join(_cartella_preventivo(preventivo_id), safe_name)
        if not os.path.exists(path):
            return jsonify({'error': 'PDF non trovato'}), 404
        return send_file(path, mimetype='application/pdf', as_attachment=False,
                         download_name=safe_name, max_age=3600)
    except Exception as e:
        logger.exception('pdf disegno failed')
        return jsonify({'error': str(e)}), 500


# Cache in-memory dei SVG generati da ezdxf. La generazione costa 0.5-1.5s
# per DXF (ezdxf.readfile + Frontend + SVGBackend). Con più thumbnail nella
# tabella articoli, senza cache l'apertura preventivo diventa lenta.
# Chiave: (path, mtime). Se il file cambia (nuovo import), il mtime cambia e
# la cache viene invalidata. Cap a 128 entry (LRU manuale) per non crescere
# indefinitamente.
_SVG_CACHE = {}
_SVG_CACHE_MAX = 128


def _get_dxf_svg_cached(dxf_path: str) -> str:
    try:
        mtime = os.path.getmtime(dxf_path)
    except OSError:
        mtime = 0
    key = (dxf_path, mtime)
    hit = _SVG_CACHE.get(key)
    if hit is not None:
        return hit
    svg = _dxf_scanner.dxf_to_svg_string(dxf_path)
    if len(_SVG_CACHE) >= _SVG_CACHE_MAX:
        # Evict qualsiasi entry (dict Python mantiene insertion order, tolgo la più vecchia)
        for old_key in list(_SVG_CACHE.keys())[:8]:
            _SVG_CACHE.pop(old_key, None)
    _SVG_CACHE[key] = svg
    return svg


# Cache della geometria del CAD (geometry_json): è ciò che il CAD interno carica
# all'apertura. Parsing ezdxf ~0.1-0.5s per file; con la cache la RIapertura è
# istantanea e il pre-warm all'apertura preventivo rende snappy anche la prima.
# Chiave (path, mtime): un nuovo import cambia mtime → invalidazione automatica.
_GEOMETRY_CACHE = {}
_GEOMETRY_CACHE_MAX = 128


def _get_dxf_geometry_cached(dxf_path: str, detection_cfg: dict) -> str:
    """Ritorna la geometria del CAD già SERIALIZZATA in JSON (string). Cachare la
    stringa (non il dict) evita di ri-serializzare a ogni apertura la geometria
    grande (centinaia di polilinee) → warm hit quasi istantaneo."""
    from .preventivi.pick_part import geometry_json
    try:
        mtime = os.path.getmtime(dxf_path)
    except OSError:
        mtime = 0
    key = (dxf_path, mtime)
    hit = _GEOMETRY_CACHE.get(key)
    if hit is not None:
        return hit
    geo = geometry_json(dxf_path, detection_cfg or {})
    payload = _json_mod.dumps(geo)
    if len(_GEOMETRY_CACHE) >= _GEOMETRY_CACHE_MAX:
        for old_key in list(_GEOMETRY_CACHE.keys())[:8]:
            _GEOMETRY_CACHE.pop(old_key, None)
    _GEOMETRY_CACHE[key] = payload
    return payload


@app.route('/api/preventivi/<preventivo_id>/dxf/<path:filename>/svg', methods=['GET'])
@richiede(*UFFICI)
def api_preventivi_dxf_svg(preventivo_id, filename):
    """Ritorna SVG ad alta fedeltà del DXF (caricato in import-dxf).

    Usato dalla preview interattiva preview-dxf.html (pan/zoom + lavorazioni)
    E dai thumbnail SVG nella tabella articoli. Cachato in memoria + header
    HTTP per far cachare anche al browser.
    """
    try:
        safe_name = os.path.basename(filename)
        prev_dir = _cartella_preventivo(preventivo_id)
        dxf_path = os.path.join(prev_dir, safe_name)
        # Fallback: il DXF 'pulito' auto talvolta non è stato scritto (import RFQ);
        # invece di un 404 "DXF non trovato" mostra l'originale del pezzo.
        if not os.path.exists(dxf_path) and safe_name.endswith('_cleaned.dxf'):
            original = os.path.join(prev_dir, safe_name[:-len('_cleaned.dxf')] + '.dxf')
            if os.path.exists(original):
                dxf_path = original
        if not os.path.exists(dxf_path):
            return jsonify({'error': 'File DXF non trovato'}), 404
        from flask import Response
        svg_string = _get_dxf_svg_cached(dxf_path)
        resp = Response(svg_string, mimetype='image/svg+xml; charset=utf-8')
        # Cache lato browser (1h). Se il DXF viene ri-importato, il file cambia
        # e il memory cache serve la nuova versione — il browser continuerà con
        # la vecchia fino allo scadere, ma è una preview, accettabile.
        resp.headers['Cache-Control'] = 'private, max-age=3600'
        return resp
    except Exception as e:
        logger.exception('dxf_svg failed')
        return jsonify({'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/dxf-confidenze', methods=['POST'])
@richiede('commerciale')
def api_preventivi_dxf_confidenze(preventivo_id):
    """Confidenza del riconoscimento automatico per piu' disegni in una volta.

    Serve ai preventivi importati prima che la confidenza venisse salvata:
    senza, ogni pezzo non confermato a mano sembrava da verificare.
    Body: {files: [nome, ...]}. Risposta: {risultati: {nome: {confidence,
    needs_manual_select}}}. Sola lettura.
    """
    try:
        data = request.get_json(silent=True) or {}
        nomi = [os.path.basename(str(n)) for n in (data.get('files') or [])][:300]
        prev_dir = _cartella_preventivo(preventivo_id)
        from .preventivi.dxf_polygon_detector_v3 import detect_pezzo_geometry_v3
        app_cfg = ConfigManager.load_config() or {}
        detection_cfg = app_cfg.get('dxf_detection', {})
        risultati = {}
        for nome in nomi:
            path = os.path.join(prev_dir, nome)
            if not nome.lower().endswith('.dxf') or not os.path.exists(path):
                continue
            try:
                r = detect_pezzo_geometry_v3(path, detection_cfg)
                risultati[nome] = {'confidence': r.get('confidence'),
                                   'needs_manual_select': bool(r.get('needs_manual_select'))}
            except Exception as e:  # un disegno illeggibile non ferma gli altri
                risultati[nome] = {'confidence': 0, 'needs_manual_select': True, 'errore': str(e)}
        return jsonify({'success': True, 'risultati': risultati}), 200
    except Exception as e:
        logger.exception('dxf confidenze failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/dxf/<path:filename>/candidates', methods=['GET'])
@richiede(*UFFICI)
def api_preventivi_dxf_candidates(preventivo_id, filename):
    """Ritorna la lista dei poligoni candidati come pezzo (per UI selezione manuale).

    Il detector v3 (Shapely) calcola uno score per ogni poligono chiuso e determina
    il migliore + confidence. Se confidence bassa, il frontend mostra all'utente
    tutti i candidati sovrapposti al DXF con overlay cliccabili.

    Response:
        {
            success: bool,
            geometry: {area_dm2, perimetro_taglio_m, n_pierce, bbox_*, ...},
            candidates: [
                {idx, area_dm2, perimetro_m, bbox: [minx,miny,maxx,maxy],
                 n_circles, n_inner, score, is_selected, geometry: [[x,y], ...]},
                ...
            ],
            selected_candidate_idx: int,
            confidence: 0-1,
            confidence_label: 'alta'|'media'|'bassa'|'nessuna',
            needs_manual_select: bool
        }
    """
    try:
        safe_name = os.path.basename(filename)
        prev_dir = _cartella_preventivo(preventivo_id)
        dxf_path = os.path.join(prev_dir, safe_name)
        if not os.path.exists(dxf_path):
            return jsonify({'success': False, 'error': 'File DXF non trovato'}), 404
        from .preventivi.dxf_polygon_detector_v3 import detect_pezzo_geometry_v3
        app_cfg = ConfigManager.load_config() or {}
        detection_cfg = app_cfg.get('dxf_detection', {})
        r = detect_pezzo_geometry_v3(dxf_path, detection_cfg)
        return jsonify({'success': True, **r}), 200
    except Exception as e:
        logger.exception('dxf_candidates failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/dxf/<path:filename>/spessore', methods=['POST'])
@richiede('commerciale')
def api_preventivi_dxf_spessore(preventivo_id, filename):
    """Ricalcola lo spessore lamiera per un DXF dato area (dal detector/manuale)
    e materiale (che l'utente potrebbe aver cambiato dopo l'import).

    Body: {area_dm2: float, materiale: str}
    Response: {success, spessore: {spessore_mm, confidence, source, details}}
    """
    try:
        safe_name = os.path.basename(filename)
        prev_dir = _cartella_preventivo(preventivo_id)
        dxf_path = os.path.join(prev_dir, safe_name)
        if not os.path.exists(dxf_path):
            return jsonify({'success': False, 'error': 'File DXF non trovato'}), 404
        data = request.get_json(silent=True) or {}
        area = float(data.get('area_dm2') or 0)
        mat = (data.get('materiale') or '').strip()
        sp = _dxf_scanner.estrai_spessore_da_cartiglio(dxf_path,
                                                       area_dm2=area or None,
                                                       materiale=mat or None)
        return jsonify({'success': True, 'spessore': sp}), 200
    except Exception as e:
        logger.exception('dxf spessore failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/dxf/<path:filename>/select-point', methods=['POST'])
@richiede('commerciale')
def api_preventivi_dxf_select_point(preventivo_id, filename):
    """Pattern 'Trova pezzo' Lantek: click su un contorno chiuso → sistema
    identifica quel poligono e i suoi contorni interni.

    Body: {x: float, y: float}   (coord DXF in mm)
    Response: {success, area_dm2, perimetro_taglio_m, n_pierce, ...}
    """
    try:
        data = request.get_json(silent=True) or {}
        try:
            x = float(data.get('x'))
            y = float(data.get('y'))
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'x/y richiesti (float mm DXF)'}), 400
        safe_name = os.path.basename(filename)
        prev_dir = _cartella_preventivo(preventivo_id)
        dxf_path = os.path.join(prev_dir, safe_name)
        if not os.path.exists(dxf_path):
            return jsonify({'success': False, 'error': 'File DXF non trovato'}), 404
        from .preventivi.dxf_polygon_detector_v3 import compute_geometry_from_point
        app_cfg = ConfigManager.load_config() or {}
        detection_cfg = app_cfg.get('dxf_detection', {})
        r = compute_geometry_from_point(dxf_path, x, y, detection_cfg)
        return jsonify({'success': True, **r}), 200
    except Exception as e:
        logger.exception('dxf_select_point failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/dxf/<path:filename>/generate-canonical', methods=['POST'])
@richiede('commerciale')
def api_preventivi_dxf_generate_canonical(preventivo_id, filename):
    """Genera il DXF CANONICO dal contorno confermato (solo pezzo + fori).
    È il file byte-identico che andrà in produzione (preventivato≡prodotto) e
    già pulito per il nesting Lantek.

    Body: {outer_xy: [[x,y]...], holes_xy: [[[x,y]...]...], codice: str}
    Response: {success, filename, sha256}
    """
    try:
        data = request.get_json(silent=True) or {}
        outer = data.get('outer_xy') or []
        holes = data.get('holes_xy') or []
        if not outer or len(outer) < 3:
            return jsonify({'success': False, 'error': 'Contorno esterno mancante'}), 400
        codice = (data.get('codice') or 'pezzo').strip() or 'pezzo'
        safe_codice = ''.join(c if c.isalnum() or c in '-_' else '_' for c in codice)
        prev_dir = _cartella_preventivo(preventivo_id)
        os.makedirs(prev_dir, exist_ok=True)
        out_name = f'{safe_codice}_canonico.dxf'
        out_path = os.path.join(prev_dir, out_name)
        from .preventivi.pick_part import genera_dxf_canonico
        r = genera_dxf_canonico(outer, holes, out_path)
        if not r.get('success'):
            return jsonify({'success': False, 'error': r.get('error', 'generazione fallita')}), 500
        # Pulito per Lantek dalle entita' vere del disegno (archi veri, mm veri)
        pulito = {'cleaned_dxf_filename': None, 'errore': None}
        try:
            src_name = os.path.basename(filename)
            src_path = os.path.join(prev_dir, src_name)
            if os.path.exists(src_path) and src_name.lower().endswith('.dxf'):
                from .preventivi import dxf_cleanup as _dxc
                base_p, ext_p = os.path.splitext(src_path)
                cleaned_path = base_p + '_cleaned' + ext_p
                cfg = (ConfigManager.load_config() or {}).get('dxf_detection', {})
                rp = _dxc.pulito_da_contorno(src_path, cleaned_path, outer, holes, cfg)
                if rp.get('success'):
                    pulito['cleaned_dxf_filename'] = os.path.basename(cleaned_path)
                    pulito.update({k: rp.get(k) for k in ('n_taglio', 'n_piega', 'n_marcatura', 'n_simboli_tolti')})
                else:
                    pulito['errore'] = rp.get('error')
                    logger.info('pulito da CAD non scritto (%s): %s', src_name, rp.get('error'))
        except Exception as pe:
            logger.warning('pulito da CAD fallito: %s', pe)
            pulito['errore'] = str(pe)
        return jsonify({'success': True, 'filename': out_name, 'sha256': r['sha256'], 'pulito': pulito}), 200
    except Exception as e:
        logger.exception('generate-canonical failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/warm-cad', methods=['POST'])
@richiede('commerciale')
def api_preventivi_warm_cad(preventivo_id):
    """Pre-scalda in background le cache SVG + geometria per TUTTI i DXF del
    preventivo, così l'apertura del CAD (e i thumbnail) è istantanea. Ritorna
    subito: il warming gira in un thread. Idempotente (cache hit = no-op)."""
    try:
        prev_dir = _cartella_preventivo(preventivo_id)
        if not os.path.isdir(prev_dir):
            return jsonify({'success': True, 'count': 0}), 200
        tasks = [(os.path.join(prev_dir, n), n) for n in os.listdir(prev_dir)
                 if n.lower().endswith('.dxf') and not n.lower().endswith('_cleaned.dxf')]
        if tasks:
            threading.Thread(
                target=_prewarm_dxf_cache, args=(tasks,), daemon=True,
                name=f'warm-cad-{preventivo_id[:8]}',
            ).start()
        return jsonify({'success': True, 'count': len(tasks)}), 200
    except Exception as e:
        logger.exception('warm-cad failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/dxf/<path:filename>/geometry-json', methods=['GET'])
@richiede(*UFFICI)
def api_preventivi_dxf_geometry_json(preventivo_id, filename):
    """CAD interno — geometria del DXF come polilinee in mm, per il viewer.

    Response: {extents:[minx,miny,maxx,maxy], polylines:[{pts:[[x,y]..], kind}]}
    kind = 'geo' (contorno, cliccabile) | 'annot' (cartiglio/quote, grigio).
    """
    try:
        safe_name = os.path.basename(filename)
        prev_dir = _cartella_preventivo(preventivo_id)
        dxf_path = os.path.join(prev_dir, safe_name)
        if not os.path.exists(dxf_path):
            return jsonify({'error': 'File DXF non trovato'}), 404
        app_cfg = ConfigManager.load_config() or {}
        payload = _get_dxf_geometry_cached(dxf_path, app_cfg.get('dxf_detection', {}))
        from flask import Response
        resp = Response(payload, mimetype='application/json')
        resp.headers['Cache-Control'] = 'private, max-age=3600'
        return resp
    except Exception as e:
        logger.exception('dxf_geometry_json failed')
        return jsonify({'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/dxf/<path:filename>/follow-contour', methods=['POST'])
@richiede('commerciale')
def api_preventivi_dxf_follow_contour(preventivo_id, filename):
    """CAD interno — tracciamento contorno stile Lantek Detect Part.

    Click sul contorno del pezzo → segue la catena di segmenti (continuazione
    più dritta, ignora le diramazioni delle quote) → geometria deterministica.
    Validato: area entro ~0,1% di Lantek sui contorni puliti.

    Body: {x: float, y: float}   (coord DXF in mm)
    Response: {success, area_dm2, perimetro_taglio_m, n_forature, bbox, outer_xy,
               holes_xy, source}  oppure  {success: False, error}
    Su success False il frontend BLOCCA: chiede all'operatore di ri-cliccare
    sul bordo del pezzo (mai un valore inventato).
    """
    try:
        data = request.get_json(silent=True) or {}
        try:
            x = float(data.get('x'))
            y = float(data.get('y'))
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'x/y richiesti (float mm DXF)'}), 400
        safe_name = os.path.basename(filename)
        prev_dir = _cartella_preventivo(preventivo_id)
        dxf_path = os.path.join(prev_dir, safe_name)
        if not os.path.exists(dxf_path):
            return jsonify({'success': False, 'error': 'File DXF non trovato'}), 404
        from .preventivi.pick_part import follow_contour_from_click
        app_cfg = ConfigManager.load_config() or {}
        detection_cfg = app_cfg.get('dxf_detection', {})
        r = follow_contour_from_click(dxf_path, x, y, detection_cfg)
        status = 200 if r.get('success') else 200  # success:False non è errore HTTP
        return jsonify(r), status
    except Exception as e:
        logger.exception('dxf_follow_contour failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/dxf/<path:filename>/pick-candidates', methods=['POST'])
@richiede('commerciale')
def api_preventivi_dxf_pick_candidates(preventivo_id, filename):
    """CAD interno — MULTI-IPOTESI: dal click enumera i contorni chiusi plausibili
    e li ritorna come candidati (il corretto in cima). Se 1 solo → l'UI auto-
    seleziona; se >1 → l'operatore sceglie.

    Body: {x, y}   Response: {success, candidates: [...], click}
    """
    try:
        data = request.get_json(silent=True) or {}
        try:
            x = float(data.get('x')); y = float(data.get('y'))
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'x/y richiesti'}), 400
        safe_name = os.path.basename(filename)
        dxf_path = os.path.join(_cartella_preventivo(preventivo_id), safe_name)
        if not os.path.exists(dxf_path):
            return jsonify({'success': False, 'error': 'File DXF non trovato'}), 404
        from .preventivi.pick_part import pick_candidates
        app_cfg = ConfigManager.load_config() or {}
        r = pick_candidates(dxf_path, x, y, app_cfg.get('dxf_detection', {}))
        return jsonify(r), 200
    except Exception as e:
        logger.exception('pick-candidates failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/dxf/<path:filename>/lavorazioni', methods=['GET'])
@richiede(*UFFICI)
def api_preventivi_dxf_lavorazioni(preventivo_id, filename):
    """Riconta le lavorazioni dal DXF: pieghe (testi SU/GIU'), saldatura,
    filettatura, svasatura. Usato alla conferma del contorno nel CAD per
    aggiornare il conteggio pieghe (che dipende dal disegno, non dal contorno).

    Response: {pieghe, saldatura_ml, filettatura_pz, svasatura_pz}
    """
    try:
        safe_name = os.path.basename(filename)
        dxf_path = os.path.join(_cartella_preventivo(preventivo_id), safe_name)
        if not os.path.exists(dxf_path):
            return jsonify({'error': 'File DXF non trovato'}), 404
        from .preventivi.dxf_scanner import scansiona_dxf_dettagli
        app_cfg = ConfigManager.load_config() or {}
        cfg = app_cfg.get('dxf_detection', {}) or {
            'dxf_colori_piega': [2], 'dxf_colori_saldatura': [1],
            'dxf_lunghezza_minima': 15.0, 'dxf_tolleranza_centro': 1.0,
            'dxf_svasatura_ratio_min': 1.8, 'dxf_svasatura_ratio_max': 3.0,
            'dxf_semicerchio_angolo_min': 150.0, 'dxf_semicerchio_angolo_max': 320.0,
            'dxf_filtra_zona_sviluppata': True,
        }
        pieghe, sald_ml, fil, svas = scansiona_dxf_dettagli(dxf_path, cfg)
        return jsonify({'pieghe': pieghe, 'saldatura_ml': sald_ml,
                        'filettatura_pz': fil, 'svasatura_pz': svas}), 200
    except Exception as e:
        logger.exception('lavorazioni recount failed')
        return jsonify({'error': str(e)}), 500


def _modello_piega(dxf_path, pieghe, outer, thickness, cfg) -> dict:
    """Modello 3D piegato di un DXF sviluppato (facce + cerniere) per fold3d.html,
    oppure {success: False, reason, error}. Lo usano il preventivatore e il
    tablet dell'officina: stesso calcolo, stesso risultato."""
    from .preventivi.pick_part import pick_candidates
    from .preventivi.pick_fold import build_fold_model
    if not pieghe:
        return {'success': False, 'reason': 'no_bends',
                'error': 'Nessuna piega leggibile in questo pezzo'}
    if not outer:
        # Semina il pick vicino a OGNI cerniera (non al centroide: su disegni
        # multi-vista il centroide cade nel vuoto). Offset lungo la normale
        # per non stare sulla linea di piega. Tieni il candidato di area
        # maggiore = il blank sviluppato completo.
        import math as _m
        best_area = -1.0
        for b in pieghe:
            h = b['hinge']
            mx, my = (h[0] + h[2]) / 2, (h[1] + h[3]) / 2
            dx, dy = h[2] - h[0], h[3] - h[1]
            L = _m.hypot(dx, dy) or 1.0
            nx, ny = -dy / L, dx / L
            for off in (12, 20, -12, -20):
                sx, sy = mx + nx * off, my + ny * off
                try:
                    cand = pick_candidates(dxf_path, sx, sy, cfg)
                    for c in (cand.get('candidates') or []):
                        a = c.get('area_dm2') or 0
                        oxy = c.get('outer_xy') or c.get('outer')
                        if oxy and a > best_area:
                            best_area, outer = a, oxy
                except Exception:
                    continue
            # trovato un contorno reale (non una scheggia) → basta, non
            # scandiamo tutte le 15 cerniere (sarebbe lentissimo)
            if best_area > 0.03:
                break
    if not outer:
        return {'success': False, 'reason': 'no_outline',
                'error': 'Contorno non ricavabile in automatico'}
    try:
        thickness = float(thickness or 0) or 2.0
    except (TypeError, ValueError):
        thickness = 2.0
    return build_fold_model(dxf_path, outer, thickness, cfg)


@app.route('/api/preventivi/<preventivo_id>/dxf/<path:filename>/fold-model', methods=['POST'])
@richiede('commerciale')
def api_preventivi_dxf_fold_model(preventivo_id, filename):
    """Anteprima piega 3D — modello {facce, cerniere, radice} per il viewer.

    Legge le pieghe (verso+gradi+raggio) dal disegno sviluppato e spacca il
    contorno lungo le cerniere. Best-effort per la miniatura: se l'outline non
    è fornito, lo cerca da solo seminando il pick vicino alle pieghe.

    Body (opzionale): {outer_xy:[[x,y]..] contorno CONFERMATO, thickness}
    Response: build_fold_model(...) oppure {success:False, reason}
    """
    try:
        data = request.get_json(silent=True) or {}
        safe_name = os.path.basename(filename)
        dxf_path = os.path.join(_cartella_preventivo(preventivo_id), safe_name)
        if not os.path.exists(dxf_path):
            return jsonify({'success': False, 'error': 'File DXF non trovato'}), 404
        app_cfg = ConfigManager.load_config() or {}
        cfg = app_cfg.get('dxf_detection', {})
        from .preventivi.dxf_scanner import estrai_pieghe_3d

        pieghe = estrai_pieghe_3d(dxf_path, cfg)
        # Probe veloce: solo conteggio pieghe (per il badge), niente pick_candidates
        if data.get('probe'):
            return jsonify({'probe': True, 'has_bends': bool(pieghe),
                            'n_bends': len(pieghe)}), 200
        return jsonify(_modello_piega(dxf_path, pieghe, data.get('outer_xy'),
                                      data.get('thickness'), cfg)), 200
    except Exception as e:
        logger.exception('fold-model failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/dxf/<path:filename>/trace-waypoints', methods=['POST'])
@richiede('commerciale')
def api_preventivi_dxf_trace_waypoints(preventivo_id, filename):
    """CAD interno — tracciamento GUIDATO con waypoint (per bivi/pezzi complessi).

    L'operatore fornisce N punti lungo il contorno; il sistema segue la
    geometria DXF reale (cammino minimo) tra waypoint consecutivi e chiude.

    Body: {points: [[x,y], ...]}   (coord DXF mm, almeno 2)
    Response: come follow-contour, oppure {success:False, error}.
    """
    try:
        data = request.get_json(silent=True) or {}
        points = data.get('points')
        if not isinstance(points, list) or len(points) < 2:
            return jsonify({'success': False, 'error': 'points: lista di almeno 2 [x,y]'}), 400
        safe_name = os.path.basename(filename)
        prev_dir = _cartella_preventivo(preventivo_id)
        dxf_path = os.path.join(prev_dir, safe_name)
        if not os.path.exists(dxf_path):
            return jsonify({'success': False, 'error': 'File DXF non trovato'}), 404
        from .preventivi.pick_part import trace_contour_waypoints
        app_cfg = ConfigManager.load_config() or {}
        r = trace_contour_waypoints(dxf_path, points, app_cfg.get('dxf_detection', {}))
        return jsonify(r), 200
    except Exception as e:
        logger.exception('dxf_trace_waypoints failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/dxf/<path:filename>/select-region', methods=['POST'])
@richiede('commerciale')
def api_preventivi_dxf_select_region(preventivo_id, filename):
    """Ricalcola geometria pezzo prendendo tutti i contorni chiusi nella
    region bbox (mm) indicata dall'utente col marquee drag sulla preview.

    Body: {minx, miny, maxx, maxy, articolo_id: str (opz)}
    Response: {success, area_dm2, perimetro_taglio_m, n_pierce, ...}
    """
    try:
        data = request.get_json(silent=True) or {}
        try:
            minx = float(data.get('minx'))
            miny = float(data.get('miny'))
            maxx = float(data.get('maxx'))
            maxy = float(data.get('maxy'))
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'minx/miny/maxx/maxy richiesti (float)'}), 400
        safe_name = os.path.basename(filename)
        prev_dir = _cartella_preventivo(preventivo_id)
        dxf_path = os.path.join(prev_dir, safe_name)
        if not os.path.exists(dxf_path):
            return jsonify({'success': False, 'error': 'File DXF non trovato'}), 404
        from .preventivi.dxf_polygon_detector_v3 import compute_geometry_from_region
        app_cfg = ConfigManager.load_config() or {}
        detection_cfg = app_cfg.get('dxf_detection', {})
        r = compute_geometry_from_region(dxf_path, (minx, miny, maxx, maxy), detection_cfg)
        return jsonify({'success': True, **r}), 200
    except Exception as e:
        logger.exception('dxf_select_region failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/dxf/<path:filename>/save-cleaned', methods=['POST'])
@richiede('commerciale')
def api_preventivi_dxf_save_cleaned(preventivo_id, filename):
    """Salva un DXF pulito manualmente definito dall'utente col drag rettangolare.

    Usa lo stesso bbox del select-region per (1) generare il DXF pulito filtrando
    le entità dentro il bbox, (2) ricalcolare area/perim/n_forature esatti.
    Se articolo_id è fornito, aggiorna il record DB con cleaned_status='manual'.

    Body: {minx, miny, maxx, maxy, articolo_id: str (opz), admin_id: str}
    Response: {success, cleaned_dxf_filename, entities_copied, area_dm2,
               perimetro_taglio_m, n_pierce, ...}
    """
    try:
        data = request.get_json(silent=True) or {}
        admin_id = _chi()
        try:
            bbox = (
                float(data.get('minx')), float(data.get('miny')),
                float(data.get('maxx')), float(data.get('maxy')),
            )
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'minx/miny/maxx/maxy richiesti (float)'}), 400
        safe_name = os.path.basename(filename)
        prev_dir = _cartella_preventivo(preventivo_id)
        dxf_path = os.path.join(prev_dir, safe_name)
        if not os.path.exists(dxf_path):
            return jsonify({'success': False, 'error': 'File DXF non trovato'}), 404

        # 1. Genera DXF pulito filtrando per bbox utente
        from .preventivi import dxf_cleanup
        base_p, ext_p = os.path.splitext(dxf_path)
        cleaned_path = base_p + '_cleaned' + ext_p
        cleanup_r = dxf_cleanup.save_cleaned_dxf(dxf_path, cleaned_path, bbox)
        if not cleanup_r.get('success'):
            return jsonify({'success': False, 'error': cleanup_r.get('error') or 'Cleanup fallito'}), 400

        # Formato Lantek (TAGLIO/PIEGA/MARCATURA, mm veri): se non riesce resta il pulito di prima
        try:
            _cfg_l = (ConfigManager.load_config() or {}).get('dxf_detection', {})
            _rl = dxf_cleanup.converti_pulito_in_lantek(dxf_path, cleaned_path, _cfg_l)
            if _rl.get('success'):
                cleanup_r['bbox_mm'] = _rl.get('bbox_mm_mm') or cleanup_r.get('bbox_mm')
            else:
                logger.info('pulito %s non convertito per Lantek: %s', os.path.basename(cleaned_path), _rl.get('error'))
        except Exception as _le:
            logger.warning('conversione pulito Lantek fallita: %s', _le)

        # 2. Ricalcola geometria (area/perim/n_forature) SUL FILE PULITO
        # Il pulito contiene SOLO le entità del pezzo → detector v3 dà valori
        # esatti se trova un poligono chiuso. Se invece il pezzo ha contorno
        # aperto (LINE sparse non chain-walkable), il detector prende una
        # sotto-parte piccola (es. cerchio di un foro come "pezzo") → dimensioni
        # sballate. In quel caso uso il bbox reale delle entità come fallback.
        geom = {}
        try:
            from .preventivi.dxf_polygon_detector_v3 import detect_pezzo_geometry_v3
            app_cfg = ConfigManager.load_config() or {}
            detection_cfg = app_cfg.get('dxf_detection', {})
            geom = detect_pezzo_geometry_v3(cleaned_path, detection_cfg) or {}
            if geom.get('n_pierce') is None and geom.get('n_forature') is not None:
                geom['n_pierce'] = geom.get('n_forature')
        except Exception as ge:
            logger.warning('geometria post-cleanup fallita: %s', ge)

        # Fallback bbox-based: se detector v3 ha ritornato area molto piccola
        # rispetto al bbox reale del cleaned (< 25% area bbox), o non ha area,
        # uso il bbox come rettangolo equivalente + perimetro somma fori (BUG FIX #2).
        clean_bbox = cleanup_r.get('bbox_mm')
        if clean_bbox and len(clean_bbox) == 4:
            from .preventivi import dxf_cleanup as _dxc
            # bbox in unità disegno → mm, così il confronto con l'area del
            # detector (già in mm) vale anche per DXF in pollici/cm
            _s = _dxc.scala_mm(cleaned_path)
            bx1, by1, bx2, by2 = clean_bbox
            bbox_w = max(0.0, bx2 - bx1) * _s
            bbox_h = max(0.0, by2 - by1) * _s
            bbox_area_dm2 = (bbox_w * bbox_h) / 10000.0
            det_area = float(geom.get('area_dm2') or 0)
            use_bbox = (not det_area) or (bbox_area_dm2 > 0 and det_area < bbox_area_dm2 * 0.25)
            if use_bbox:
                # Perimetro = rettangolo esterno + perimetri dei SOLI contorni
                # interni (fori/asole). BUG FIX D15: prima si sommavano tutti gli
                # archi e le polilinee chiuse → contorno esterno contato due volte,
                # bulge ignorati. Il calcolo usa la geometria del detector.
                _fb = _dxc.perimetro_interni_fallback(cleaned_path, clean_bbox)
                bbox_perim_mm = 2.0 * (bbox_w + bbox_h)
                fori_perim_mm = float(_fb.get('perim_fori_mm') or 0.0)
                n_pierce_fallback = int(_fb.get('n_pierce') or 1)
                total_perim_mm = bbox_perim_mm + fori_perim_mm
                bbox_perim_m = total_perim_mm / 1000.0
                logger.info(
                    'save-cleaned: fallback bbox %.1fx%.1fmm perim=%.1fmm '
                    '(ext=%.1f + fori=%.1f) pierce=%d',
                    bbox_w, bbox_h, total_perim_mm, bbox_perim_mm,
                    fori_perim_mm, n_pierce_fallback
                )
                geom['area_dm2'] = round(bbox_area_dm2, 4)
                geom['perimetro_taglio_m'] = round(bbox_perim_m, 4)
                cur_pierce = geom.get('n_pierce') or 0
                if not cur_pierce or cur_pierce < n_pierce_fallback:
                    geom['n_pierce'] = n_pierce_fallback

        cleaned_dxf_filename = os.path.basename(cleaned_path)

        # 3. Se articolo_id fornito, aggiorna record DB
        articolo_id = data.get('articolo_id')
        if articolo_id:
            try:
                from .models import get_session, PreventivoArticolo
                session = get_session()
                try:
                    art = session.query(PreventivoArticolo).filter_by(id=articolo_id).first()
                    if art:
                        art.cleaned_dxf_filename = cleaned_dxf_filename
                        art.cleaned_status = 'manual'
                        # Aggiorna area/perim/n_forature se disponibili
                        if geom.get('area_dm2'):
                            art.area_dm2 = float(geom['area_dm2'])
                        if geom.get('perimetro_taglio_m'):
                            art.perimetro_taglio_m = float(geom['perimetro_taglio_m'])
                        if geom.get('n_pierce') is not None:
                            art.n_forature = int(geom['n_pierce'])
                        session.commit()
                        logger.info('articolo %s aggiornato: cleaned=manual', articolo_id)
                finally:
                    session.close()
            except Exception as dbe:
                logger.warning('articolo update fallito: %s', dbe)

        # 4. Invalida cache SVG server-side per il file pulito (mtime cambierà)
        try:
            keys_to_drop = [k for k in _SVG_CACHE if isinstance(k, tuple) and cleaned_path in k[0]]
            for k in keys_to_drop:
                _SVG_CACHE.pop(k, None)
        except Exception:
            pass

        # BBox reale del file pulito → dim W×H precise anche per pezzi con smussi
        # (evita la formula matematica del rettangolo equivalente che sballa
        #  perché area/perim reali riflettono gli smussi angolari).
        bbox_w_mm = None
        bbox_h_mm = None
        cbbox = cleanup_r.get('bbox_mm')
        if cbbox and len(cbbox) == 4:
            from .preventivi import dxf_cleanup as _dxc
            _s = _dxc.scala_mm(cleaned_path)   # unità disegno → mm
            bbox_w_mm = round((cbbox[2] - cbbox[0]) * _s, 2)
            bbox_h_mm = round((cbbox[3] - cbbox[1]) * _s, 2)

        return jsonify({
            'success': True,
            'cleaned_dxf_filename': cleaned_dxf_filename,
            'cleaned_status': 'manual',
            'entities_copied': cleanup_r['entities_copied'],
            'entities_source': cleanup_r['entities_source'],
            'tolerance_mm': cleanup_r['tolerance_mm'],
            'area_dm2': geom.get('area_dm2'),
            'perimetro_taglio_m': geom.get('perimetro_taglio_m'),
            'n_pierce': geom.get('n_pierce'),
            'bbox_w_mm': bbox_w_mm,
            'bbox_h_mm': bbox_h_mm,
        }), 200
    except Exception as e:
        logger.exception('dxf save-cleaned failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/dxf/<path:filename>/save-cleaned-by-click', methods=['POST'])
@richiede('commerciale')
def api_preventivi_dxf_save_cleaned_by_click(preventivo_id, filename):
    """Pulizia DXF a partire da un CLICK sul pezzo (invece del drag rettangolo).

    L'utente clicca una linea/arco/cerchio del contorno o un foro del pezzo.
    Il sistema identifica il cluster spazialmente connesso a quel punto e
    lo salva come DXF pulito. Elimina l'imprecisione del drag rettangolare.

    Body: {x, y, articolo_id: str (opz), admin_id: str}
    Response: {success, cleaned_dxf_filename, bbox_w_mm, bbox_h_mm, ...}
    """
    try:
        data = request.get_json(silent=True) or {}
        admin_id = _chi()
        try:
            cx = float(data.get('x'))
            cy = float(data.get('y'))
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'x/y richiesti (float)'}), 400
        safe_name = os.path.basename(filename)
        prev_dir = _cartella_preventivo(preventivo_id)
        dxf_path = os.path.join(prev_dir, safe_name)
        if not os.path.exists(dxf_path):
            return jsonify({'success': False, 'error': 'File DXF non trovato'}), 404

        from .preventivi import dxf_cleanup
        base_p, ext_p = os.path.splitext(dxf_path)
        cleaned_path = base_p + '_cleaned' + ext_p
        cleanup_r = dxf_cleanup.save_cleaned_dxf_by_click(dxf_path, cleaned_path, cx, cy)
        if not cleanup_r.get('success'):
            return jsonify({'success': False, 'error': cleanup_r.get('error') or 'Cleanup fallito'}), 400

        # Formato Lantek (TAGLIO/PIEGA/MARCATURA, mm veri): se non riesce resta il pulito di prima
        try:
            _cfg_l = (ConfigManager.load_config() or {}).get('dxf_detection', {})
            _rl = dxf_cleanup.converti_pulito_in_lantek(dxf_path, cleaned_path, _cfg_l)
            if _rl.get('success'):
                cleanup_r['bbox_mm'] = _rl.get('bbox_mm_mm') or cleanup_r.get('bbox_mm')
            else:
                logger.info('pulito %s non convertito per Lantek: %s', os.path.basename(cleaned_path), _rl.get('error'))
        except Exception as _le:
            logger.warning('conversione pulito Lantek fallita: %s', _le)

        # Ricalcola geometria sul cleaned
        geom = {}
        try:
            from .preventivi.dxf_polygon_detector_v3 import detect_pezzo_geometry_v3
            app_cfg = ConfigManager.load_config() or {}
            detection_cfg = app_cfg.get('dxf_detection', {})
            geom = detect_pezzo_geometry_v3(cleaned_path, detection_cfg) or {}
            if geom.get('n_pierce') is None and geom.get('n_forature') is not None:
                geom['n_pierce'] = geom.get('n_forature')
        except Exception as ge:
            logger.warning('geometria post-cleanup fallita: %s', ge)

        # Fallback bbox se detector v3 dà area anomala
        clean_bbox = cleanup_r.get('bbox_mm')
        bbox_w_mm = bbox_h_mm = None
        if clean_bbox and len(clean_bbox) == 4:
            # bbox in unità disegno → mm (confronto coerente con l'area del detector)
            _s = dxf_cleanup.scala_mm(cleaned_path)
            bx1, by1, bx2, by2 = clean_bbox
            bbox_w_mm = round((bx2 - bx1) * _s, 2)
            bbox_h_mm = round((by2 - by1) * _s, 2)
            bbox_area_dm2 = (bbox_w_mm * bbox_h_mm) / 10000.0
            det_area = float(geom.get('area_dm2') or 0)
            use_bbox = (not det_area) or (bbox_area_dm2 > 0 and det_area < bbox_area_dm2 * 0.25)
            if use_bbox:
                # BUG FIX #2: al fallback il perimetro NON è solo 2*(w+h) del rettangolo
                # esterno — deve includere anche i perimetri dei fori interni.
                # BUG FIX D15: solo i contorni INTERNI (prima si sommavano tutti
                # gli archi e le polilinee chiuse → contorno esterno contato due
                # volte, bulge ignorati). Stesso helper della route save-cleaned.
                _fb = dxf_cleanup.perimetro_interni_fallback(cleaned_path, clean_bbox)
                bbox_perim_mm = 2.0 * (bbox_w_mm + bbox_h_mm)
                fori_perim_mm = float(_fb.get('perim_fori_mm') or 0.0)
                n_pierce_fallback = int(_fb.get('n_pierce') or 1)  # 1 contorno esterno + fori
                total_perim_mm = bbox_perim_mm + fori_perim_mm
                bbox_perim_m = total_perim_mm / 1000.0
                logger.info(
                    'save-cleaned-by-click: fallback bbox %.1fx%.1fmm perim=%.1fmm '
                    '(ext=%.1f + fori=%.1f) pierce=%d',
                    bbox_w_mm, bbox_h_mm, total_perim_mm, bbox_perim_mm,
                    fori_perim_mm, n_pierce_fallback
                )
                geom['area_dm2'] = round(bbox_area_dm2, 4)
                geom['perimetro_taglio_m'] = round(bbox_perim_m, 4)
                # Sovrascrivi n_pierce se il detector v3 non l'ha dato o è < fallback
                cur_pierce = geom.get('n_pierce') or 0
                if not cur_pierce or cur_pierce < n_pierce_fallback:
                    geom['n_pierce'] = n_pierce_fallback

        cleaned_dxf_filename = os.path.basename(cleaned_path)

        # Ricalcola pieghe/saldatura/filettatura/svasatura sul FILE ORIGINALE.
        # Motivo: il cleaned rimuove tutti i TEXT/MTEXT (annotazioni di piega
        # come "SU 90° R2", "GIU' 20° R2"), quindi rileggerli sul cleaned darebbe 0.
        # Le lavorazioni sono attributi del pezzo indipendenti dalla pulizia
        # geometrica → sempre calcolate sull'originale.
        dettagli = None
        try:
            from .preventivi import dxf_scanner as _scn
            dettagli = _scn.scansiona_dxf_dettagli(dxf_path, detection_cfg)
        except Exception as sce:
            logger.warning('scansiona_dxf_dettagli fallito: %s', sce)

        # Aggiorna record DB
        articolo_id = data.get('articolo_id')
        if articolo_id:
            try:
                from .models import get_session, PreventivoArticolo
                session = get_session()
                try:
                    art = session.query(PreventivoArticolo).filter_by(id=articolo_id).first()
                    if art:
                        art.cleaned_dxf_filename = cleaned_dxf_filename
                        art.cleaned_status = 'manual'
                        if geom.get('area_dm2'):
                            art.area_dm2 = float(geom['area_dm2'])
                        if geom.get('perimetro_taglio_m'):
                            art.perimetro_taglio_m = float(geom['perimetro_taglio_m'])
                        if geom.get('n_pierce') is not None:
                            art.n_forature = int(geom['n_pierce'])
                        # Aggiorna lavorazioni dal ricalcolo sul file originale
                        if dettagli is not None:
                            p, s, f, v = dettagli
                            art.pieghe = int(p)
                            art.saldatura_ml = float(s)
                            art.filettatura_pz = int(f)
                            art.svasatura_pz = int(v)
                        session.commit()
                finally:
                    session.close()
            except Exception as dbe:
                logger.warning('articolo update fallito: %s', dbe)

        # Invalida cache SVG server-side
        try:
            keys_to_drop = [k for k in _SVG_CACHE if isinstance(k, tuple) and cleaned_path in k[0]]
            for k in keys_to_drop:
                _SVG_CACHE.pop(k, None)
        except Exception:
            pass

        # AUTO-EXPORT nella cartella <root>/<cliente>/<numero_ordine>/
        # Best-effort: se fallisce, non blocca la response ma include l'info nel payload.
        export_info = {'exported': False, 'path': None, 'error': None}
        try:
            # Disattivata: la cartella master riceve solo gli ORDINI (accettazione o
            # "Crea cartella"), non i pezzi dei preventivi mentre si puliscono.
            export_root = ''
            if export_root:
                prev = PreventivoManager.get(preventivo_id, include_children=False)
                if prev:
                    cliente = prev.get('cliente') or 'cliente_sconosciuto'
                    ord_num = (prev.get('numero_ordine_cliente') or '').strip()
                    if not ord_num:
                        # Fallback: PREV-YYYY-<primi-8-char-id>
                        anno = datetime.now().year
                        ord_num = f"PREV-{anno}-{preventivo_id[:8]}"
                    export_r = dxf_cleanup.export_cleaned_dxf_to_client_folder(
                        cleaned_source_path=cleaned_path,
                        cliente=cliente,
                        numero_ordine=ord_num,
                        original_filename=os.path.basename(dxf_path),  # senza _cleaned
                        export_root=export_root,
                    )
                    export_info['exported'] = export_r.get('success', False)
                    export_info['path'] = export_r.get('exported_path')
                    export_info['error'] = export_r.get('error')
        except Exception as ee:
            logger.warning('auto-export DXF fallito: %s', ee)
            export_info['error'] = str(ee)

        # Prepara response con lavorazioni ricalcolate
        pieghe_val = int(dettagli[0]) if dettagli else None
        sald_val = float(dettagli[1]) if dettagli else None
        filett_val = int(dettagli[2]) if dettagli else None
        svas_val = int(dettagli[3]) if dettagli else None

        return jsonify({
            'success': True,
            'cleaned_dxf_filename': cleaned_dxf_filename,
            'cleaned_status': 'manual',
            'entities_copied': cleanup_r['entities_copied'],
            'entities_source': cleanup_r['entities_source'],
            'clicked_entity_type': cleanup_r.get('clicked_entity_type'),
            'clicked_entity_distance_mm': cleanup_r.get('clicked_entity_distance_mm'),
            'area_dm2': geom.get('area_dm2'),
            'perimetro_taglio_m': geom.get('perimetro_taglio_m'),
            'n_pierce': geom.get('n_pierce'),
            'bbox_w_mm': bbox_w_mm,
            'bbox_h_mm': bbox_h_mm,
            'pieghe': pieghe_val,
            'saldatura_ml': sald_val,
            'filettatura_pz': filett_val,
            'svasatura_pz': svas_val,
            'export': export_info,
        }), 200
    except Exception as e:
        logger.exception('dxf save-cleaned-by-click failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/dxf/<path:filename>/select-polygon', methods=['POST'])
@richiede('commerciale')
def api_preventivi_dxf_select_polygon(preventivo_id, filename):
    """Ricalcola area/perimetro/n_pierce assumendo che l'utente ha scelto un
    poligono specifico come outer del pezzo (invece del top-scored automatico).

    Body:
        {"candidate_idx": int, "articolo_id": str (opz — se presente aggiorna DB)}

    Response:
        {success, geometry: {area_dm2, perimetro_taglio_m, n_pierce, ...}}
    """
    try:
        data = request.get_json(silent=True) or {}
        cand_idx = data.get('candidate_idx')
        articolo_id = data.get('articolo_id') or ''
        if cand_idx is None:
            return jsonify({'success': False, 'error': 'candidate_idx richiesto'}), 400
        try:
            cand_idx = int(cand_idx)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'candidate_idx non valido'}), 400

        safe_name = os.path.basename(filename)
        prev_dir = _cartella_preventivo(preventivo_id)
        dxf_path = os.path.join(prev_dir, safe_name)
        if not os.path.exists(dxf_path):
            return jsonify({'success': False, 'error': 'File DXF non trovato'}), 404

        from .preventivi.dxf_polygon_detector_v3 import compute_geometry_from_candidate
        app_cfg = ConfigManager.load_config() or {}
        detection_cfg = app_cfg.get('dxf_detection', {})
        r = compute_geometry_from_candidate(dxf_path, cand_idx, detection_cfg)
        return jsonify({'success': True, **r}), 200
    except Exception as e:
        logger.exception('dxf_select_polygon failed')
        return jsonify({'success': False, 'error': str(e)}), 500


def _cleanup_preventivo_files(preventivo_id):
    """Rimuove la cartella uploads/preventivi_tmp/<preventivo_id>/ (DXF/STEP temporanei).
    Chiamato all'accettazione, rifiuto o delete del preventivo.
    """
    try:
        import shutil
        d = _cartella_preventivo(preventivo_id)
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
    except Exception as exc:
        logger.warning('cleanup preventivo files failed for %s: %s', preventivo_id, exc)


def _numero_per_cartelle(order) -> str:
    """Numero con cui si chiamano cartelle e zip dei disegni: quello
    dell'ORDINE DEL CLIENTE (es. 1252) se l'ordine nasce da un preventivo che
    lo riporta, altrimenti il numero dell'ordine (PREV-2026-0005 diceva poco)."""
    get = (lambda k: order.get(k)) if isinstance(order, dict) else (lambda k: getattr(order, k, None))
    pid = get('preventivo_id_origine')
    if pid:
        try:
            from .models import Preventivo
            s = get_session()
            try:
                p = s.query(Preventivo).filter(Preventivo.id == pid).first()
                n = ((p.numero_ordine_cliente if p else '') or '').strip()
            finally:
                s.close()
            if n:
                return n
        except Exception:
            logger.warning('numero ordine cliente non letto', exc_info=True)
    return get('numero_ordine') or (get('id') or '')[:8]


_RE_COPIA = re.compile(r'\s*\(\d+\)(?=\.[^.]+$)')


def _nome_base_disegno(nome: str) -> str:
    """'25CCPA0041-00 (3).dxf' -> '25CCPA0041-00.dxf' (copie fatte all'import
    quando lo stesso disegno sta in piu' assiemi)."""
    return _RE_COPIA.sub('', nome or '')


def _impronta_file(path: str) -> str:
    import hashlib
    h = hashlib.md5()
    with open(path, 'rb') as fp:
        for blocco in iter(lambda: fp.read(65536), b''):
            h.update(blocco)
    return h.hexdigest()


CARTELLA_DA_PREPARARE = '_DA PREPARARE'
CARTELLA_ORIGINALI = '_DISEGNI ORIGINALI'


def _nome_lamiera(materiale, spessore) -> str:
    """Nome della cartella di una lamiera: "INOX 304 - 2 mm", "S235 - 1,5 mm"."""
    from .preventivi.dxf_cleanup import _sanitize_path_part
    m = (materiale or '').replace('_', ' ').strip().upper() or 'MATERIALE NON INDICATO'
    try:
        s = float(spessore)
        sp = (f'{s:g}'.replace('.', ',') + ' mm') if s > 0 else 'SPESSORE NON INDICATO'
    except (TypeError, ValueError):
        sp = 'SPESSORE NON INDICATO'
    return _sanitize_path_part(f'{m} - {sp}', 'lamiera')


def _copy_cleaned_dxf_to_drawings(preventivo_id: str, order_id: str) -> dict:
    """Copia i DXF PULITI (o originali con warning) da preventivi_tmp/<pid>/
    in uploads/drawings/<order_id>/ per Mirko (nesting Lantek).

    Preferenza:
      - `cleaned_dxf_filename` se disponibile (pulito auto o manuale)
      - fallback all'originale `dxf_filename` con warning ("cliente riceve
        DXF sporco, da pulire in Lantek")

    Ritorna stats: {copied_cleaned, copied_original_fallback, missing, warnings, drawings_dir}
    """
    import shutil
    import hashlib
    stats = {
        'copied_cleaned': 0,
        'copied_original_fallback': 0,
        'missing': 0,
        'warnings': [],
        'articoli_da_pulire_manualmente': [],  # nomi articoli senza pulito
        'drawings_dir': None,
        'lantek_pronti': 0,           # puliti messi in <ordine>/LANTEK
        'lantek_da_guardare': [],     # [{codice, motivi}] da preparare in Lantek
        'file_hashes': [],  # {filename, sha256, cleaned} — impronte file produzione
    }

    def _sha256(path):
        h = hashlib.sha256()
        with open(path, 'rb') as fp:
            for chunk in iter(lambda: fp.read(65536), b''):
                h.update(chunk)
        return h.hexdigest()
    try:
        p = PreventivoManager.get(preventivo_id, include_children=True)
        if not p:
            stats['warnings'].append(f'Preventivo {preventivo_id} non trovato')
            return stats
        src_dir = _cartella_preventivo(preventivo_id)
        dst_dir = os.path.join(UPLOAD_FOLDER, 'drawings', order_id)
        os.makedirs(dst_dir, exist_ok=True)
        stats['drawings_dir'] = dst_dir

        articoli = p.get('articoli') or []
        # in LANTEK lo stesso disegno una volta sola (lo stesso codice in piu'
        # assiemi: 25CCPA0041-00 in 4 assiemi del DECA 1252 dava 4 file)
        lantek_visti = {}
        stats['lantek_doppioni'] = 0
        for a in articoli:
            codice = a.get('codice') or '?'
            original = a.get('dxf_filename')
            # A Mirko va l'ORIGINALE: la pulizia/nesting la fa lui in Lantek (affidabile).
            # L'app non genera più un disegno "pulito" per la produzione.
            src = None
            if original:
                candidate = os.path.join(src_dir, original)
                if os.path.exists(candidate):
                    src = candidate
            if not src:
                stats['missing'] += 1
                stats['warnings'].append(f'{codice}: nessun DXF disponibile')
                continue
            dst_name = os.path.basename(src)
            dst = os.path.join(dst_dir, dst_name)
            try:
                shutil.copy2(src, dst)
                stats['copied_original_fallback'] += 1
                # Impronta SHA256 del file mandato in produzione (integrità/tracciabilità)
                try:
                    digest = _sha256(dst)
                    stats['file_hashes'].append(
                        {'filename': dst_name, 'sha256': digest, 'tipo': 'originale', 'integro': None})
                    OrderManager.add_order_file(
                        order_id, dst_name, dst, 'DXF', sha256=digest)
                except Exception as he:
                    logger.warning('hash/registrazione OrderFile per %s fallita: %s', dst_name, he)
            except Exception as e:
                logger.warning('copy dxf %s -> %s failed: %s', src, dst, e)
                stats['warnings'].append(f'{codice}: copy fallita ({e})')
                continue
            # Accanto all'originale, in LANTEK/, il pulito pronto per il nesting
            # (TAGLIO/PIEGA/MARCATURA in mm) con lo stesso nome. Solo se passa la
            # verifica: gli altri si preparano in Lantek come prima.
            try:
                from .preventivi import dxf_cleanup as _dxc
                pulito = a.get('cleaned_dxf_filename')
                pulito_path = os.path.join(src_dir, os.path.basename(pulito)) if pulito else None
                if not pulito_path:
                    # pulito scritto ma non registrato sul pezzo (pacchetti di prima)
                    b_, e_ = os.path.splitext(src)
                    if os.path.isfile(b_ + '_cleaned' + e_):
                        pulito_path = b_ + '_cleaned' + e_
                cfg_l = (ConfigManager.load_config() or {}).get('dxf_detection', {})
                v = _dxc.prepara_pulito_lantek(src, pulito_path, a, cfg_l)
                # divisi per lamiera (materiale + spessore): in Lantek si apre
                # una cartella e si annida, niente smistamento a mano
                lamiera = _nome_lamiera(a.get('materiale'), a.get('spessore_mm'))
                chiave = (lamiera, _nome_base_disegno(dst_name).lower())
                impronta = _impronta_file(src)
                gia = lantek_visti.get(chiave)
                if gia == impronta:
                    stats['lantek_doppioni'] += 1
                    continue
                nome_l = _nome_base_disegno(dst_name) if gia is None else dst_name
                lantek_visti.setdefault(chiave, impronta)
                if v.get('stato') == 'pronto':
                    lantek_dir = os.path.join(dst_dir, 'LANTEK', lamiera)
                    os.makedirs(lantek_dir, exist_ok=True)
                    shutil.copy2(v['path'], os.path.join(lantek_dir, nome_l))
                    stats['lantek_pronti'] += 1
                else:
                    # l'originale, nella stessa divisione, da preparare come prima
                    da_prep = os.path.join(dst_dir, 'LANTEK', CARTELLA_DA_PREPARARE, lamiera)
                    os.makedirs(da_prep, exist_ok=True)
                    shutil.copy2(src, os.path.join(da_prep, nome_l))
                    stats['lantek_da_guardare'].append({'codice': codice, 'motivi': v.get('motivi') or []})
            except Exception as le:
                logger.warning('pulito Lantek di %s non preparato: %s', codice, le)
                stats['lantek_da_guardare'].append({'codice': codice, 'motivi': [str(le)]})
        return stats
    except Exception as e:
        logger.exception('_copy_cleaned_dxf_to_drawings failed')
        stats['warnings'].append(f'errore inatteso: {e}')
        return stats


_ORDINE_SEGNO = '_ordine_cliente.json'


def _segna_ordine_cliente(prev_dir: str, nome: str) -> None:
    """Ricorda quale PDF della cartella del preventivo e' l'ordine del cliente."""
    try:
        with open(os.path.join(prev_dir, _ORDINE_SEGNO), 'w', encoding='utf-8') as fp:
            _json_mod.dump({'file': nome}, fp)
    except OSError as e:
        logger.warning('segno ordine cliente non scritto: %s', e)


def _ordine_cliente_segnato(prev_dir: str) -> str | None:
    try:
        with open(os.path.join(prev_dir, _ORDINE_SEGNO), encoding='utf-8') as fp:
            nome = os.path.basename((_json_mod.load(fp) or {}).get('file') or '')
        return nome if nome and os.path.isfile(os.path.join(prev_dir, nome)) else None
    except (OSError, ValueError):
        return None


@app.route('/api/preventivi/<preventivo_id>/ordine-cliente', methods=['POST'])
@richiede(*UFFICI)
def api_preventivi_ordine_cliente(preventivo_id):
    """Allega il PDF dell'ordine del cliente e lo legge SENZA AI: per ogni
    codice dei pezzi (form 'codici', JSON) quantita' e posizione nell'ordine.
    Il PDF resta sul server e all'accettazione diventa il documento
    dell'ordine di produzione (laser). Non modifica i pezzi: la proposta la
    applica la pagina dopo la conferma."""
    try:
        f = request.files.get('file')
        # 'salvato': un PDF gia' caricato col resto dei disegni (non si ricarica)
        salvato = os.path.basename(request.form.get('salvato') or '')
        if not salvato and (not f or not f.filename.lower().endswith('.pdf')):
            return jsonify({'success': False, 'error': 'Serve il PDF dell\'ordine'}), 400
        prev = PreventivoManager.get(preventivo_id, include_children=False)
        if not prev or prev.get('error'):
            return jsonify({'success': False, 'error': 'Preventivo non trovato'}), 404
        if prev.get('status') in ('INVIATO', 'ACCETTATO'):
            return jsonify({'success': False, 'error': 'Preventivo ' + prev['status'] + ': immutabile'}), 400
        prev_dir = _cartella_preventivo(preventivo_id)
        os.makedirs(prev_dir, exist_ok=True)
        if salvato:
            if not salvato.lower().endswith('.pdf') or not os.path.isfile(os.path.join(prev_dir, salvato)):
                return jsonify({'success': False, 'error': 'PDF non trovato tra i file del preventivo'}), 404
            nome = salvato
            with open(os.path.join(prev_dir, nome), 'rb') as fp:
                dati = fp.read()
        else:
            dati = f.read()
            nome = _nome_disegno_libero(prev_dir, os.path.basename(f.filename))
            with open(os.path.join(prev_dir, nome), 'wb') as fp:
                fp.write(dati)
        _segna_ordine_cliente(prev_dir, nome)
        try:
            codici = _json_mod.loads(request.form.get('codici') or '[]')
        except ValueError:
            codici = []
        from .preventivi.ordine_codici import leggi_per_codici, intestazione
        try:
            righe = leggi_per_codici(dati, [str(c) for c in codici if c])
            testa = intestazione(dati)
        except Exception as e:
            logger.warning('lettura ordine cliente: %s', e)
            righe, testa = {}, {}
        return jsonify({'success': True, 'file': nome, 'righe': righe, **testa}), 200
    except Exception as e:
        logger.exception('ordine-cliente failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/ordine-cliente', methods=['GET'])
@richiede(*UFFICI)
def api_preventivi_ordine_cliente_get(preventivo_id):
    """Nome del PDF d'ordine allegato (None se non c'e')."""
    prev_dir = _cartella_preventivo(preventivo_id)
    return jsonify({'success': True, 'file': _ordine_cliente_segnato(prev_dir)}), 200


def _copy_order_pdf_to_order(preventivo_id: str, order_id: str) -> dict:
    """Allega all'ordine FerroTrack il PDF ordine originale (disegni/lavorazioni)
    salvato in preventivi_tmp/<pid>/ così Mirko vede "cosa deve fare".

    Il PDF diventa il documento principale dell'ordine (file_type='PDF' in
    uploads/pdfs/), come per gli ordini caricati manualmente. Se non c'è alcun
    PDF (preventivo creato da soli DXF), non fa nulla.

    Ritorna {copied: bool, filename, warnings}.
    """
    import shutil
    import hashlib
    out = {'copied': False, 'filename': None, 'warnings': []}
    try:
        src_dir = _cartella_preventivo(preventivo_id)
        if not os.path.isdir(src_dir):
            return out
        # Il PDF dell'ordine del cliente e' quello segnato all'import. Prima si
        # prendeva il primo in ordine alfabetico: con le tavole PDF dei pezzi
        # nella stessa cartella all'ordine finiva un disegno.
        segnato = _ordine_cliente_segnato(src_dir)
        if segnato:
            pdfs = [segnato]
        else:
            pdfs = sorted(f for f in os.listdir(src_dir) if f.lower().endswith('.pdf'))
            if len(pdfs) != 1:
                # nessun ordine allegato e piu' PDF (tavole): non si indovina
                if pdfs:
                    out['warnings'].append('Nessun PDF d\'ordine allegato: tavole non copiate come ordine')
                return out
        src = os.path.join(src_dir, pdfs[0])
        os.makedirs(PDFS_FOLDER, exist_ok=True)
        dst_name = f"{order_id}_{pdfs[0]}"
        dst = os.path.join(PDFS_FOLDER, dst_name)
        shutil.copy2(src, dst)
        h = hashlib.sha256()
        with open(dst, 'rb') as fp:
            for chunk in iter(lambda: fp.read(65536), b''):
                h.update(chunk)
        OrderManager.add_order_file(order_id, dst_name, dst, 'PDF', sha256=h.hexdigest())
        out['copied'] = True
        out['filename'] = dst_name
        return out
    except Exception as e:
        logger.exception('_copy_order_pdf_to_order failed')
        out['warnings'].append(str(e))
        return out


@app.route('/api/orders/<order_id>/verify-files', methods=['GET'])
@richiede('commerciale', 'ufficio', 'laser')
def api_orders_verify_files(order_id):
    """Verifica integrità: ricalcola l'hash dei file DXF in produzione
    (uploads/drawings/<order_id>/) e lo confronta con quello registrato
    all'accettazione. Garantisce che il file tagliato sia quello preventivato.

    Response: {success, files: [{filename, sha256_registrato, sha256_attuale,
               ok, stato}], all_ok}
    """
    import hashlib

    def _sha256(path):
        h = hashlib.sha256()
        with open(path, 'rb') as fp:
            for chunk in iter(lambda: fp.read(65536), b''):
                h.update(chunk)
        return h.hexdigest()

    try:
        session = get_session()
        try:
            rows = session.query(OrderFile).filter(
                OrderFile.order_id == order_id, OrderFile.file_type == 'DXF').all()
            files = []
            all_ok = True
            for of in rows:
                path = of.filepath
                if not path or not os.path.exists(path):
                    files.append({'filename': of.filename, 'sha256_registrato': of.sha256,
                                  'sha256_attuale': None, 'ok': False, 'stato': 'file mancante'})
                    all_ok = False
                    continue
                attuale = _sha256(path)
                ok = (of.sha256 is not None and attuale == of.sha256)
                if not ok:
                    all_ok = False
                files.append({
                    'filename': of.filename,
                    'sha256_registrato': of.sha256,
                    'sha256_attuale': attuale,
                    'ok': ok,
                    'stato': 'integro' if ok else ('hash non registrato' if not of.sha256 else 'FILE MODIFICATO'),
                })
            return jsonify({'success': True, 'files': files, 'all_ok': all_ok,
                            'n_files': len(files)}), 200
        finally:
            session.close()
    except Exception as e:
        logger.exception('verify-files failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/import-step', methods=['POST'])
@richiede('commerciale')
def api_preventivi_import_step(preventivo_id):
    """Upload STEP (.stp/.step) → estrae assiemi 3D + tubolari + piastre.

    Esegue 3 analisi indipendenti:
      - step_assieme.analizza_step_assieme  → conteggio corpi + saldatura totale
      - step_tubolari.analizza_step_tubolari → lista profili tubolari (CHS/SHS/RHS)
      - step_piastre.analizza_step_piastre  → lista piastre (spessore + area)

    Il file viene scartato dopo (no storage). Costi base calcolati su materiale='acciaio'
    di default (commerciale può modificare poi).
    """
    try:
        admin_id = _chi()
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'File STEP obbligatorio'}), 400
        f = request.files['file']
        if not f.filename or not f.filename.lower().endswith(('.step', '.stp')):
            return jsonify({'success': False, 'error': 'File deve essere .step o .stp'}), 400

        # Salva STEP in preventivi_tmp/<id>/ (persistente per preview 3D; cleanup su accept/reject/delete)
        prev_dir = _cartella_preventivo(preventivo_id)
        os.makedirs(prev_dir, exist_ok=True)
        safe_name = os.path.basename(f.filename)
        step_path = os.path.join(prev_dir, safe_name)
        f.save(step_path)
        try:
            assieme_data = _step_assieme.analizza_step_assieme(step_path) or {}
            tubolari_data = _step_tubolari.analizza_step_tubolari(step_path, _PROFILI_TUBOLARI_DB) or {}
            # I corpi riconosciuti come tubi NON vanno contati anche come piastre
            # (tubo con testa a 45° finiva anche tra le piastre).
            piastre_data = _step_piastre.analizza_step_piastre(
                step_path, escludi_body_ids=tubolari_data.get('body_ids_tubi') or []) or {}
            # Quantità d'istanza per corpo (albero d'assieme, moltiplicate lungo
            # i sotto-assiemi). File a parte singola → tutti 1.
            istanze_step = _step_assieme.conta_istanze_nauo(step_path) or {}
        except Exception:
            # Se l'analisi fallisce non lasciare il file orfano
            try: os.remove(step_path)
            except OSError: pass
            raise

        # Coefficienti: €/kg e tagli dei tubolari dalle Impostazioni
        # (preventivi_config.tubolari_*), gli altri ancora fissi.
        _pc_tub = (ConfigManager.load_config() or {}).get('preventivi_config') or {}

        def _tar(k, d):
            try:
                v = float(_pc_tub.get(k))
                return v if v >= 0 else d
            except (TypeError, ValueError):
                return d
        config_tubolari_piastre = {
            'costo_materiale_acciaio_kg': _tar('tubolari_euro_kg', 1.50),
            'costo_materiale_inox_kg': 4.50,
            'costo_materiale_alluminio_kg': 3.50,
            'costo_orario_taglio_tubo': 40.0,
            'costo_taglio_dritto': _tar('tubolari_taglio_dritto', 1.0),
            'costo_taglio_obliquo': _tar('tubolari_taglio_obliquo', 2.5),
            'costo_taglio_sagomato': 5.0,
        }

        def _qty_corpo(body_id):
            try:
                return max(1, int(istanze_step.get(body_id, 1) or 1))
            except (TypeError, ValueError):
                return 1

        # Totali tubolari PER ISTANZA: peso e tagli moltiplicati per la qty
        # (un telaio con una gamba istanziata 4 volte = 4 gambe, 8 tagli).
        tubi_step = tubolari_data.get('tubi') or []

        def _n_tagli(tag):
            return sum(_qty_corpo(t.get('body_id')) * [t.get('taglio_1'), t.get('taglio_2')].count(tag)
                       for t in tubi_step)
        tubolari_aggr = {
            'peso_totale_kg': round(sum((t.get('peso_kg') or 0) * _qty_corpo(t.get('body_id'))
                                        for t in tubi_step), 2),
            'n_tagli_dritti': _n_tagli('dritto'),
            'n_tagli_obliqui': _n_tagli('obliquo'),
            'n_tagli_sagomati': _n_tagli('sagomato'),
        }

        # Costi tubolari + piastre
        tub_costi = {}
        pia_costi = {}
        try:
            tub_costi = _step_tubolari.calcola_costo_tubolare(tubolari_aggr, config_tubolari_piastre, 'acciaio')
        except Exception as e:
            logger.warning('calcola_costo_tubolare failed: %s', e)
        try:
            pia_costi = _step_piastre.calcola_costo_piastre(piastre_data, config_tubolari_piastre, 'acciaio')
        except Exception as e:
            logger.warning('calcola_costo_piastre failed: %s', e)

        # Codice dell'assieme "macro" di questo STEP: tubolari e piastre vengono
        # LINKATI a questo codice → compaiono come componenti dell'assieme (non
        # standalone), coerente col 3D. Il rollup costo evita il doppio conteggio.
        codice_assieme_step = os.path.splitext(f.filename)[0]

        # Normalizza tubolari per la UI/DB
        # Una riga per CORPO con 'qty' = istanze nell'assieme. peso_kg,
        # costo_materiale e n_tagli_* sono il TOTALE della riga (qty inclusa):
        # la tabella DB non ha un campo quantità, così i totali restano giusti
        # anche dopo il salvataggio. lunghezza_m resta quella del singolo pezzo.
        tubolari_list = []
        for t in tubi_step:
            q = _qty_corpo(t.get('body_id'))
            peso_u = t.get('peso_kg') or 0
            tagli = [t.get('taglio_1'), t.get('taglio_2')]
            tubolari_list.append({
                'codice_assieme': codice_assieme_step,
                'profilo': t.get('profilo') or '',
                'tipo': t.get('tipo'),
                'materiale': 'acciaio',
                'qty': q,
                'lunghezza_m': t.get('lunghezza_m') or 0,
                'lunghezza_totale_m': round((t.get('lunghezza_m') or 0) * q, 3),
                'peso_unitario_kg': peso_u,
                'peso_kg': round(peso_u * q, 2),
                'costo_materiale': round(peso_u * q * config_tubolari_piastre['costo_materiale_acciaio_kg'], 2),
                'costo_taglio_totale': 0,  # aggregato in tub_costi.totale, qui zero per articolo singolo
                'n_tagli_dritti': tagli.count('dritto') * q,
                'n_tagli_obliqui': tagli.count('obliquo') * q,
                'n_tagli_sagomati': tagli.count('sagomato') * q,
                'taglio_1': t.get('taglio_1'),
                'taglio_2': t.get('taglio_2'),
                'angolo_taglio_1': t.get('angolo_taglio_1'),
                'angolo_taglio_2': t.get('angolo_taglio_2'),
                'avvisi': list(t.get('avvisi') or []),
            })

        # Normalizza piastre per la UI/DB
        # Costo piastre: a PESO × €/kg (coerente coi tubolari). Prima usava la
        # tabella prezzo_dm2 di calcola_costo_piastre, che NON è configurata →
        # costo 0 (bug: piastre a prezzo zero, preventivo sottostimato).
        kg_eur = config_tubolari_piastre['costo_materiale_acciaio_kg']
        piastre_list = []
        for p in (piastre_data.get('piastre') or []):
            q = _qty_corpo(p.get('body_id'))
            peso_u = p.get('peso_kg') or 0
            # area_dm2 / peso_kg / costo = TOTALE riga (qty inclusa), come i tubolari
            piastre_list.append({
                'codice_assieme': codice_assieme_step,
                'spessore_mm': p.get('spessore_mm') or 0,
                'qty': q,
                'area_unitaria_dm2': p.get('area_dm2') or 0,
                'area_dm2': round((p.get('area_dm2') or 0) * q, 3),
                'peso_unitario_kg': peso_u,
                'peso_kg': round(peso_u * q, 2),
                'costo': round(peso_u * q * kg_eur, 2),
                'materiale': 'acciaio',
                'n_pieghe': p.get('n_pieghe') or 0,
                'sviluppo': bool(p.get('sviluppo')),
                'larghezza_mm': p.get('larghezza_mm') or 0,
                'altezza_mm': p.get('altezza_mm') or 0,
                'avvisi': list(p.get('avvisi') or []),
            })
        costo_totale_piastre = round(sum(x['costo'] for x in piastre_list), 2)
        peso_totale_step = round(sum(x['peso_kg'] for x in tubolari_list)
                                 + sum(x['peso_kg'] for x in piastre_list), 2)

        # Avvisi di estrazione (unità, profili fuori catalogo, corpi non quotati)
        avvisi_step = []
        for fonte in (assieme_data, tubolari_data, piastre_data):
            for a in (fonte.get('avvisi') or []):
                if a not in avvisi_step:
                    avvisi_step.append(a)

        # Aggrega un assieme "macro" dal file STEP (saldatura totale + componenti count)
        assiemi_list = []
        saldatura_mt_tot = (assieme_data.get('saldatura_mm') or 0) / 1000.0
        if tubolari_list or piastre_list or saldatura_mt_tot > 0:
            assiemi_list.append({
                'codice_assieme': codice_assieme_step,
                'qty': 1,
                'ore_montaggio': 0,
                'ore_puntatura': 0,
                'costo': 0,
                'costo_puntatura': 0,
                'costo_saldatura_assieme': saldatura_mt_tot * 18.0,  # default 18 EUR/ml saldatura
                'saldatura_mt': saldatura_mt_tot,
                'peso_kg': peso_totale_step,
                'componenti_qty': {},
            })

        return jsonify({
            'success': True,
            'assiemi': assiemi_list,
            'tubolari': tubolari_list,
            'piastre': piastre_list,
            'avvisi': avvisi_step,
            'summary': {
                'n_tubolari': len(tubolari_list),
                'n_piastre': len(piastre_list),
                'n_tubolari_pezzi': sum(x['qty'] for x in tubolari_list),
                'n_piastre_pezzi': sum(x['qty'] for x in piastre_list),
                'peso_totale_kg': peso_totale_step,
                'unita_step': tubolari_data.get('unita') or piastre_data.get('unita') or 'mm',
                'saldatura_mt_tot': round(saldatura_mt_tot, 2),
                'costo_totale_tubolari': tub_costi.get('totale', 0),
                'costo_totale_piastre': costo_totale_piastre,
            },
        }), 200
    except Exception as e:
        logger.exception('preventivi import step failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/articoli', methods=['PUT'])
@richiede('commerciale')
def api_preventivi_articoli_replace(preventivo_id):
    """Sostituisce l'intera lista degli articoli del preventivo (bulk replace).

    Usato dal frontend come autosave dopo import DXF / cambi editor. Il
    backend fa delete + insert atomici via PreventivoManager.replace_articoli.
    Bloccato se preventivo INVIATO / ACCETTATO.
    """
    try:
        data = request.get_json(silent=True) or {}
        admin_id = _chi()
        articoli = data.get('articoli', [])
        if not isinstance(articoli, list):
            return jsonify({'success': False, 'error': 'articoli deve essere una lista'}), 400
        result = PreventivoManager.replace_articoli(preventivo_id, articoli)
        if isinstance(result, dict) and 'error' in result:
            return jsonify({'success': False, 'error': result['error']}), 409
        _persisti_totali(preventivo_id)  # aggiorna totale_lotto sul DB → storico
        try:
            AuditManager.log(user_id=admin_id, action='REPLACE_ARTICOLI',
                             entity_type='preventivi', entity_id=preventivo_id,
                             detail=f'n_articoli={result.get("count", 0)}')
        except Exception:
            pass
        return jsonify({'success': True, 'count': result.get('count', 0)}), 200
    except Exception as e:
        logger.exception('replace articoli failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/assiemi', methods=['PUT'])
@richiede('commerciale')
def api_preventivi_assiemi_replace(preventivo_id):
    """Sostituisce l'intera lista degli assiemi del preventivo (bulk replace).

    Simmetrico ad api_preventivi_articoli_replace: usato dal frontend come
    autosave dopo modifiche editor (nuovo assieme, cambio qty/costi, link
    articoli). Bloccato se preventivo INVIATO/ACCETTATO.
    """
    try:
        data = request.get_json(silent=True) or {}
        admin_id = _chi()
        assiemi = data.get('assiemi', [])
        if not isinstance(assiemi, list):
            return jsonify({'success': False, 'error': 'assiemi deve essere una lista'}), 400
        result = PreventivoManager.replace_assiemi(preventivo_id, assiemi)
        if isinstance(result, dict) and 'error' in result:
            return jsonify({'success': False, 'error': result['error']}), 409
        _persisti_totali(preventivo_id)  # aggiorna totale_lotto sul DB → storico
        try:
            AuditManager.log(user_id=admin_id, action='REPLACE_ASSIEMI',
                             entity_type='preventivi', entity_id=preventivo_id,
                             detail=f'n_assiemi={result.get("count", 0)}')
        except Exception:
            pass
        return jsonify({'success': True, 'count': result.get('count', 0)}), 200
    except Exception as e:
        logger.exception('replace assiemi failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/tubolari', methods=['PUT'])
@richiede('commerciale')
def api_preventivi_tubolari_replace(preventivo_id):
    """Sostituisce l'intera lista dei tubolari (da STEP). Autosave dopo import
    STEP / modifiche. Bloccato se INVIATO/ACCETTATO. Simmetrico ad assiemi."""
    try:
        data = request.get_json(silent=True) or {}
        admin_id = _chi()
        tubolari = data.get('tubolari', [])
        if not isinstance(tubolari, list):
            return jsonify({'success': False, 'error': 'tubolari deve essere una lista'}), 400
        result = PreventivoManager.replace_tubolari(preventivo_id, tubolari)
        if isinstance(result, dict) and 'error' in result:
            return jsonify({'success': False, 'error': result['error']}), 409
        _persisti_totali(preventivo_id)
        try:
            AuditManager.log(user_id=admin_id, action='REPLACE_TUBOLARI',
                             entity_type='preventivi', entity_id=preventivo_id,
                             detail=f'n_tubolari={result.get("count", 0)}')
        except Exception:
            pass
        return jsonify({'success': True, 'count': result.get('count', 0)}), 200
    except Exception as e:
        logger.exception('replace tubolari failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/piastre', methods=['PUT'])
@richiede('commerciale')
def api_preventivi_piastre_replace(preventivo_id):
    """Sostituisce l'intera lista delle piastre (da STEP). Autosave dopo import
    STEP / modifiche. Bloccato se INVIATO/ACCETTATO. Simmetrico ad assiemi."""
    try:
        data = request.get_json(silent=True) or {}
        admin_id = _chi()
        piastre = data.get('piastre', [])
        if not isinstance(piastre, list):
            return jsonify({'success': False, 'error': 'piastre deve essere una lista'}), 400
        result = PreventivoManager.replace_piastre(preventivo_id, piastre)
        if isinstance(result, dict) and 'error' in result:
            return jsonify({'success': False, 'error': result['error']}), 409
        _persisti_totali(preventivo_id)
        try:
            AuditManager.log(user_id=admin_id, action='REPLACE_PIASTRE',
                             entity_type='preventivi', entity_id=preventivo_id,
                             detail=f'n_piastre={result.get("count", 0)}')
        except Exception:
            pass
        return jsonify({'success': True, 'count': result.get('count', 0)}), 200
    except Exception as e:
        logger.exception('replace piastre failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/articoli/<articolo_id>/stima-base', methods=['POST'])
@richiede('commerciale')
def api_preventivi_stima_base(preventivo_id, articolo_id):
    """Calcola stima costo base laser per un articolo (richiede spessore+materiale).

    Body opzionale: { articolo: {area_dm2, perimetro_taglio_m, n_forature,
                                  spessore_mm, materiale} }
    Se body non fornito, legge dal DB l'articolo per id.
    """
    try:
        data = request.get_json(silent=True) or {}
        admin_id = _chi()
        articolo = data.get('articolo')
        if not articolo:
            # Leggi articolo dal DB
            p = PreventivoManager.get(preventivo_id, include_children=True)
            if not p:
                return jsonify({'success': False, 'error': 'Preventivo non trovato'}), 404
            articolo = next((a for a in p['articoli'] if a['id'] == articolo_id), None)
            if not articolo:
                return jsonify({'success': False, 'error': 'Articolo non trovato'}), 404
        cfg = ConfigManager.load_config()
        stima = _laser_estimator.stima_base(articolo, cfg)
        stima['firma_tariffe'] = _firma_tariffe(cfg)
        return jsonify({'success': True, 'stima': stima}), 200
    except Exception as e:
        logger.exception('preventivi stima-base failed')
        return jsonify({'success': False, 'error': str(e)}), 500


def _firma_tariffe(cfg: dict) -> str:
    """Impronta delle tariffe che determinano il costo base di un pezzo
    (laser_config: €/kg, €/h, ricette, resa, vuoto...). Ogni stima la porta
    con se': se le Impostazioni cambiano, i pezzi in bozza stimati con la
    firma vecchia si ristimano (prima restavano coi prezzi vecchi)."""
    import hashlib
    laser = cfg.get('laser_config') or _laser_estimator.DEFAULT_LASER_CONFIG
    testo = _json_mod.dumps(laser, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha1(testo.encode('utf-8')).hexdigest()[:12]


@app.route('/api/preventivi/<preventivo_id>/stima-batch', methods=['POST'])
@richiede('commerciale')
def api_preventivi_stima_batch(preventivo_id):
    """Stima del costo base di piu' pezzi in UNA richiesta (tariffe attuali).
    E' solo calcolo, niente DXF: decine di pezzi in pochi millisecondi, senza
    le decine di richieste che saturavano il server all'apertura.
    Body: {admin_id, articoli: [{...articolo...}]} → {stime: [stima|null], firma}."""
    try:
        data = request.get_json(silent=True) or {}
        admin_id = _chi()
        articoli = data.get('articoli') or []
        if not isinstance(articoli, list) or len(articoli) > 2000:
            return jsonify({'success': False, 'error': 'Elenco articoli non valido'}), 400
        cfg = ConfigManager.load_config()
        firma = _firma_tariffe(cfg)
        stime = []
        for a in articoli:
            try:
                st = _laser_estimator.stima_base(a if isinstance(a, dict) else {}, cfg)
                st['firma_tariffe'] = firma
                stime.append(st)
            except Exception:
                logger.exception('stima-batch: articolo non stimabile')
                stime.append(None)
        return jsonify({'success': True, 'stime': stime, 'firma_tariffe': firma}), 200
    except Exception as e:
        logger.exception('preventivi stima-batch failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/config', methods=['GET'])
@richiede(*UFFICI)
def api_preventivi_config_get():
    """Coefficienti globali usati dallo stimatore + cost calculator.

    Ritorna laser_config (costi orari, €/kg, densità) + preventivi_config
    (costi lavorazioni post-taglio, sconti, margini default). Usato dalla tab
    Impostazioni per il form editabile.

    Accessibile a Commerciale + Amministratore + Capo.
    """
    try:
        cfg = ConfigManager.load_config()
        laser_cfg = cfg.get('laser_config') or _laser_estimator.DEFAULT_LASER_CONFIG
        # Ricette taglio: default = file JSON calibrato; effettive = override utente se presente.
        from .preventivi.lantek_lookup import load_recipes as _load_recipes
        default_recipes = _load_recipes().get('ricette', [])
        effective_recipes = laser_cfg.get('ricette_taglio') or default_recipes
        return jsonify({
            'success': True,
            'laser_config': laser_cfg,
            'preventivi_config': cfg.get('preventivi_config') or {},
            'disegni_export_root': cfg.get('disegni_export_root') or '',
            'ricette_taglio': effective_recipes,          # da mostrare/editare
            'ricette_taglio_default': default_recipes,    # per "ripristina default"
            'ricette_taglio_custom': bool(laser_cfg.get('ricette_taglio')),
            'firma_tariffe': _firma_tariffe(cfg),
        }), 200
    except Exception as e:
        logger.exception('preventivi/config GET failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/config', methods=['PUT'])
@richiede(ADMIN)
def api_preventivi_config_put():
    """Aggiorna coefficienti globali. Salva quello che riceve senza validazione
    stretta sui valori (l'admin è responsabile). Loggato in audit.

    Body: {admin_id, laser_config?, preventivi_config?}. I singoli sub-oggetti
    sono opzionali: se assenti si mantiene quello attuale.
    """
    try:
        data = request.get_json(silent=True) or {}
        admin_id = _chi_admin()
        updates = {}
        if isinstance(data.get('laser_config'), dict):
            updates['laser_config'] = data['laser_config']
        if isinstance(data.get('preventivi_config'), dict):
            updates['preventivi_config'] = data['preventivi_config']
        # Path export DXF puliti per officina (Mirko). Top-level string.
        if 'disegni_export_root' in data:
            root = (data.get('disegni_export_root') or '').strip()
            updates['disegni_export_root'] = root
        if not updates:
            return jsonify({'success': False, 'error': 'Nessuna sezione da aggiornare'}), 400
        saved = ConfigManager.save_config(updates)
        if 'error' in saved:
            return jsonify({'success': False, 'error': saved['error']}), 500
        try:
            AuditManager.log(user_id=admin_id, action='UPDATE_PREVENTIVI_CONFIG',
                             entity_type='config', entity_id='preventivi',
                             detail=str(list(updates.keys())))
        except Exception:
            pass
        return jsonify({
            'success': True,
            'laser_config': saved.get('laser_config'),
            'preventivi_config': saved.get('preventivi_config'),
            'disegni_export_root': saved.get('disegni_export_root') or '',
            'firma_tariffe': _firma_tariffe(saved),
        }), 200
    except Exception as e:
        logger.exception('preventivi/config PUT failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/calcola', methods=['POST'])
@richiede('commerciale')
def api_preventivi_calcola(preventivo_id):
    """Totali del preventivo col calcolo AUTOREVOLE (preventivi.calcolo), gli
    stessi di PDF, invio e accettazione. Per un preventivo gia' inviato valgono
    le percentuali congelate all'invio (generali, ricarico, sconto, tariffe).
    In BOZZA aggiorna anche i totali salvati (lista storico)."""
    try:
        data = request.get_json(silent=True) or {}
        admin_id = _chi()
        p = PreventivoManager.get(preventivo_id, include_children=True)
        if not p:
            return jsonify({'success': False, 'error': 'Preventivo non trovato'}), 404
        from .preventivi.calcolo import calcola as _calcola
        cfg = (ConfigManager.load_config() or {}).get('preventivi_config') or {}
        snap = p.get('snapshot_economico') or None
        calc_p = p
        if snap:
            cfg = {'costo_generali_pct': snap.get('costo_generali_pct') or 0}
            calc_p = {**p,
                      'margine_pct': snap.get('ricarico_pct', p.get('margine_pct')),
                      'sconto_pct': snap.get('sconto_pct', p.get('sconto_pct'))}
        totali = _calcola(calc_p, cfg)
        if p.get('status') == 'BOZZA':
            _persisti_totali(preventivo_id)
        return jsonify({
            'success': True,
            'totali': totali,
            'congelato': bool(snap),
            'preventivo': {k: v for k, v in p.items()
                           if k not in ('articoli', 'assiemi', 'tubolari', 'piastre')},
        }), 200
    except Exception as e:
        logger.exception('preventivi calcola failed')
        return jsonify({'success': False, 'error': str(e)}), 500


def _etichetta_cartella_disegni(order) -> str:
    """Come si chiama, per un umano, la sottocartella dei disegni di quest'ordine.

    Vale solo per la cartella CONDIVISA con l'ufficio, quella che l'operatore
    apre a mano per importare in Lantek: la' dentro cerca <cliente>/<numero>.
    Se i disegni stanno ancora soltanto nella cartella interna
    dell'applicazione non c'e' niente da dire, perche' quel percorso non e'
    un posto dove qualcuno vada a guardare.
    """
    try:
        numero = _numero_per_cartelle(order) or ''
        cliente = (order.get('cliente') if isinstance(order, dict)
                   else getattr(order, 'cliente', None)) or ''
        root = ((ConfigManager.load_config() or {}).get('disegni_export_root') or '').strip()
        if not (root and numero):
            return ''
        from .preventivi.dxf_cleanup import _sanitize_path_part
        c = _sanitize_path_part(cliente, 'cliente_sconosciuto')
        n = _sanitize_path_part(numero, 'ordine')
        if not os.path.isdir(os.path.join(root, c, n)):
            return ''
        return (cliente + '  \u203a  ' + numero) if cliente else numero
    except Exception:
        logger.exception('etichetta cartella disegni non determinabile')
        return ''


def _cartella_disegni_ordine(order) -> str:
    """Percorso della cartella da aprire in Lantek per quest'ordine.

    Preferisce quella leggibile sulla rete (<root>/<cliente>/<numero>), che e'
    quella che l'operatore riconosce; ripiega su uploads/drawings/<id> se la
    cartella di rete non e' configurata. Ritorna stringa vuota se non c'e'
    niente da aprire.
    """
    try:
        numero = _numero_per_cartelle(order) or ''
        cliente = (order.get('cliente') if isinstance(order, dict)
                   else getattr(order, 'cliente', None)) or ''
        oid = (order.get('id') if isinstance(order, dict)
               else getattr(order, 'id', None)) or ''
        root = ((ConfigManager.load_config() or {}).get('disegni_export_root') or '').strip()
        if root and numero:
            from .preventivi.dxf_cleanup import _sanitize_path_part
            leggibile = os.path.join(root,
                                     _sanitize_path_part(cliente, 'cliente_sconosciuto'),
                                     _sanitize_path_part(numero, 'ordine'))
            if os.path.isdir(leggibile):
                return os.path.normpath(leggibile)
        interna = os.path.join(DRAWINGS_FOLDER, oid)
        # normpath: senza, il percorso esce con un ".." in mezzo e non si puo'
        # incollare in Esplora risorse.
        return os.path.normpath(interna) if os.path.isdir(interna) else ''
    except Exception:
        logger.exception('cartella disegni non determinabile')
        return ''


def _esporta_disegni_per_officina(order_id: str, cliente: str = '', numero_ordine: str = '') -> dict:
    r"""Crea la cartella dell'ordine dentro la cartella "master" delle Impostazioni:

        <cartella>\<CLIENTE>\<numero ordine del cliente>\
            <lamiera>\...          puliti pronti per Lantek (INOX 304 - 2 mm, ...)
            _DA PREPARARE\<lamiera>\...   originali da sistemare a mano
            _DISEGNI ORIGINALI\...        per confronto

    La stessa struttura dello zip (stesso disegno una volta sola). Si rifa'
    quando si vuole: i file si sovrascrivono, niente si cancella.
    cliente/numero_ordine: ripiego se l'ordine non si trova."""
    esito = {'esportati': 0, 'percorso': None, 'error': None}
    root = ((ConfigManager.load_config() or {}).get('disegni_export_root') or '').strip()
    if not root:
        esito['error'] = 'cartella dei disegni non impostata'
        return esito
    sorgente = os.path.join(DRAWINGS_FOLDER, order_id)
    order = _ordine_esistente(order_id)
    disegni = _disegni_ordine(order) if order else []
    if not disegni:
        esito['error'] = 'nessun disegno da esportare'
        return esito
    try:
        import shutil
        from .preventivi.dxf_cleanup import _sanitize_path_part
        cliente = (order.cliente if order else '') or cliente
        numero = (_numero_per_cartelle(order) if order else '') or numero_ordine or order_id[:8]
        destinazione = os.path.join(root,
                                    _sanitize_path_part(cliente, 'cliente_sconosciuto'),
                                    _sanitize_path_part(numero, 'ordine'))
        try:
            righe = _distinta_ordine(order)[0] if order else []
        except Exception:
            righe = []
        try:
            dati_lt = _dati_lantek_per_disegno(order, righe) if order else {}
        except Exception:
            logger.warning('dati Lantek per i disegni non letti', exc_info=True)
            dati_lt = {}
        from . import lantek as _lt
        n = 0
        for percorso, arc in _struttura_zip('X', sorgente, disegni, righe):
            v = _voce_per_lantek(arc, dati_lt)
            rel = (v[0] if v else arc).split('/', 1)[1]
            dst = os.path.join(destinazione, *rel.split('/'))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if v:
                try:
                    with open(dst, 'wb') as fh:
                        fh.write(_lt.dxf_con_dati(percorso, v[1]))
                    n += 1
                    continue
                except Exception:
                    logger.warning('scritte Lantek non aggiunte a %s', arc, exc_info=True)
            shutil.copy2(percorso, dst)
            n += 1
        esito['esportati'] = n
        esito['percorso'] = os.path.normpath(destinazione)
        logger.info('Disegni ordine %s esportati in %s (%d file)', order_id, destinazione, n)
    except Exception as e:
        esito['error'] = str(e)
        logger.exception('export disegni in cartella di rete fallito')
    return esito


def _dati_lantek_per_disegno(order, righe, q=None) -> dict:
    """{nome base del disegno (minuscolo): (codice Lantek, [scritte])} per i
    DXF pronti per Lantek: dentro il pezzo FerroTrack scrive "QTA 4",
    "MAT FERRO", "SP 3", "ORD 1252" e l'importatore DXF di Lantek compila da
    solo la griglia (configurazione provata con Stefano il 06/10/2026)."""
    from . import lantek as _lt
    if q is None:
        q = _quantita_lantek(order, righe)
    cfg = ((ConfigManager.load_config() or {}).get('lantek_db') or {})
    if isinstance(cfg, dict) and cfg.get('scritte_nei_dxf') is False:
        return {}
    decimale = (cfg.get('decimale') if isinstance(cfg, dict) else None) or ','
    per_cod = {r['codice_ft'].lower(): r for r in q['righe']}
    out = {}
    for r in righe or []:
        if r.get('tipo') != 'lamiera' or not r.get('codice'):
            continue
        x = per_cod.get(str(r['codice']).strip().lower())
        if not x:
            continue
        val = (x['codice'], _lt.testi_dati(x, q['commessa'], decimale,
                                           getattr(order, 'cliente', '') or '', q['consegna']))
        out[str(r['codice']).strip().lower()] = val
        if r.get('disegno'):
            out[_nome_base_disegno(r['disegno']).rsplit('.', 1)[0].lower()] = val
    return out


def _voce_per_lantek(arc: str, dati: dict):
    """(nuovo nome nello zip/cartella, scritte) se `arc` e' un DXF pronto per
    Lantek di cui si conoscono i dati; altrimenti None. Gli originali e quelli
    da preparare non si toccano."""
    parti = arc.split('/')
    if len(parti) < 2 or not parti[-1].lower().endswith('.dxf'):
        return None
    if any(p in (CARTELLA_ORIGINALI, CARTELLA_DA_PREPARARE) for p in parti[:-1]):
        return None
    base = parti[-1].rsplit('.', 1)[0]
    x = dati.get(base.lower())
    if not x:
        return None
    codice, testi = x
    # il file col codice di Lantek (25NDSPA1979 -> 25NDSPA1979-00): cosi'
    # Lantek riconosce il pezzo che ha gia' invece di crearne un altro
    parti[-1] = codice + '.dxf'
    return '/'.join(parti), testi


def _nuovi_per_lantek(order, q, righe=None) -> tuple:
    """I codici NUOVI dell'ordine (non ancora in Lantek): quali si possono
    creare in automatico col loro DXF (pronto per Lantek, materiale che Lantek
    conosce, spessore) e quali no, col motivo.

    ([{'codice', 'dxf', 'materiale', 'spessore'}], [{'codice', 'motivo'}])"""
    from . import lantek as _lt
    nuovi = [r for r in q['righe'] if r['stato'] == 'nuovo']
    if not nuovi:
        return [], []
    if righe is None:
        righe = _distinta_ordine(order)[0]
    dxf, da_preparare = {}, set()
    try:
        # codice/nome del disegno -> codice Lantek (come _dati_lantek_per_disegno,
        # ma senza dipendere dall'impostazione delle scritte nei DXF)
        per_cod = {r['codice_ft'].lower(): r['codice'] for r in nuovi}
        dati = {}
        for r in righe or []:
            c = str(r.get('codice') or '').strip().lower()
            if r.get('tipo') != 'lamiera' or c not in per_cod:
                continue
            dati[c] = (per_cod[c], [])
            if r.get('disegno'):
                dati[_nome_base_disegno(r['disegno']).rsplit('.', 1)[0].lower()] = (per_cod[c], [])
        for percorso, arc in _struttura_zip('X', os.path.join(DRAWINGS_FOLDER, order.id),
                                            _disegni_ordine(order), righe):
            v = _voce_per_lantek(arc, dati)
            if v:
                dxf.setdefault(v[0].rsplit('/', 1)[-1][:-4].lower(), percorso)
            elif f'/{CARTELLA_DA_PREPARARE}/' in arc:
                base = arc.rsplit('/', 1)[-1].rsplit('.', 1)[0].lower()
                if base in dati:
                    da_preparare.add(dati[base][0].lower())
    except Exception:
        logger.warning('DXF dei pezzi nuovi non trovati', exc_info=True)
    noti = _lt.materiali_lantek_noti()
    si, no = [], []
    for r in nuovi:
        cod = r['codice']
        if any('scegli' in a for a in r['avvisi']):
            no.append({'codice': cod, 'motivo': 'in Lantek ci sono più revisioni: scegli quella giusta'})
        elif r.get('controllo'):
            no.append({'codice': cod, 'motivo': 'disegno da verificare: ' + '; '.join(r['controllo']['motivi']),
                       'verifica': True, 'articolo_id': r.get('articolo_id'), 'disegno': r.get('disegno')})
        elif not dxf.get(cod.lower()):
            no.append({'codice': cod, 'motivo': 'disegno da preparare in FerroTrack' if cod.lower() in da_preparare
                       else 'manca il disegno'})
        elif str(r.get('materiale') or '').upper() not in noti:
            no.append({'codice': cod, 'motivo': f"materiale {r.get('materiale') or '?'} da scegliere in Lantek"})
        elif not r.get('spessore'):
            no.append({'codice': cod, 'motivo': 'spessore mancante'})
        else:
            si.append({'codice': cod, 'dxf': dxf[cod.lower()], 'materiale': r['materiale'],
                       'spessore': r['spessore'], 'quantita': r['quantita']})
    return si, no


def _quantita_lantek(order, righe=None) -> dict:
    """Le quantita' da importare in Lantek (iErp, modello ImportProduzione) per
    un ordine, coi controlli letti da Lantek in sola lettura (backend/lantek.py).

    {righe, lantek: {disponibile, errore}, commessa, consegna, nome_file}"""
    from . import lantek as _lt
    from .preventivi.dxf_cleanup import _sanitize_path_part
    if righe is None:
        try:
            righe = _distinta_ordine(order)[0]
        except Exception:
            logger.warning('distinta per le quantita Lantek non letta', exc_info=True)
            righe = []
    codici = [r.get('codice') for r in righe if r.get('tipo') == 'lamiera' and r.get('codice')]
    info = _lt.pezzi_in_lantek(codici) if codici else {'disponibile': False, 'errore': None, 'pezzi': {}}
    q = _lt.righe_quantita(righe, info)
    commessa = _lt.commessa_lantek(_numero_per_cartelle(order) or (order.numero_ordine or ''))
    consegna = order.data_consegna.date() if getattr(order, 'data_consegna', None) else None
    nome = _sanitize_path_part(f'QUANTITA LANTEK - {commessa or order.id[:8]}', 'QUANTITA LANTEK') + '.xlsx'
    noti = _lt.clienti_lantek() if info.get('disponibile') else []
    if info.get('disponibile'):
        _lt.segna_gia_ordinati(q, _lt.ordini_in_lantek(commessa), _lt.ordini_fatti_in_lantek(commessa))
    return {'righe': q, 'lantek': {'disponibile': info.get('disponibile'), 'errore': info.get('errore')},
            'commessa': commessa, 'consegna': consegna.isoformat() if consegna else None,
            'cliente_lantek': _lt.cliente_lantek(order.cliente or '', noti),
            'nome_file': nome,
            'nome_file_xml': _sanitize_path_part(f'ORDINI LANTEK - {commessa or order.id[:8]}',
                                                 'ORDINI LANTEK') + '.xml'}


@app.route('/api/orders/<order_id>/lantek-quantita', methods=['GET'])
@richiede('laser', 'ufficio')
def api_ordine_lantek_quantita(order_id):
    """Le righe del file delle quantita' per Lantek, coi controlli (codice
    gia' in Lantek o nuovo, materiale/spessore diversi, altre revisioni,
    gia' negli ordini di produzione di Lantek)."""
    try:
        from . import lantek as _lt
        order = _ordine_esistente(order_id)
        if not order:
            return _non_trovato_ordine()
        if request.args.get('fresco'):
            # "Ricontrolla": rileggere Lantek adesso (dopo un import dal MES)
            _lt._svuota_cache()
        q = _quantita_lantek(order)
        n_avvisi = sum(1 for r in q['righe'] if r['avvisi'])
        nuovi_si, nuovi_no = [], []
        if q['lantek'].get('disponibile') and _lt.procesos_disponibile():
            nuovi_si, nuovi_no = _nuovi_per_lantek(order, q)
        return jsonify({'success': True, **q, 'n_codici': len(q['righe']),
                        # pezzi nuovi che "Manda a Lantek" crea col loro DXF, e quelli no
                        'nuovi_da_creare': [{k: x[k] for k in ('codice', 'materiale', 'spessore', 'quantita')}
                                            for x in nuovi_si],
                        'nuovi_esclusi': nuovi_no,
                        'n_pezzi': sum(r['quantita'] for r in q['righe']),
                        'n_nuovi': sum(1 for r in q['righe'] if r['stato'] == 'nuovo'),
                        'n_in_produzione': sum(1 for r in q['righe'] if r.get('in_produzione')),
                        'n_gia_fatti': sum(1 for r in q['righe'] if r.get('gia_fatti')),
                        'n_xml': len(_lt.righe_per_xml(q['righe'])),
                        # per la conferma di "Manda a Lantek"
                        'da_mandare': [{'codice': r['codice'], 'quantita': r['quantita'],
                                        'gia_fatti': r.get('gia_fatti') or 0, 'gia_fatti_il': r.get('gia_fatti_il')}
                                       for r in _lt.righe_per_xml(q['righe'])],
                        'invio_automatico': _lt.xmlimporter_disponibile(),
                        'n_avvisi': n_avvisi}), 200
    except Exception as e:
        logger.exception('quantita Lantek fallite')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/orders/<order_id>/lantek-quantita.xlsx', methods=['GET'])
@richiede('laser', 'ufficio')
def api_ordine_lantek_quantita_xlsx(order_id):
    """Il file Excel da importare in Lantek con iErp (Codice Articolo,
    Quantita', Materiale, Spessore, Data consegna, Commessa)."""
    try:
        import io
        from . import lantek as _lt
        order = _ordine_esistente(order_id)
        if not order:
            return _non_trovato_ordine()
        q = _quantita_lantek(order)
        if not q['righe']:
            return jsonify({'success': False, 'codice': 'nessun_pezzo',
                            'error': "Quest'ordine non ha pezzi di lamiera con un codice"}), 404
        buf = io.BytesIO()
        _lt.scrivi_excel(buf, q['righe'], q['consegna'], q['commessa'])
        buf.seek(0)
        _audit('LANTEK_QUANTITA', 'orders', order_id,
               f"File quantita' per Lantek: {len(q['righe'])} codici")
        return send_file(buf, as_attachment=True, download_name=q['nome_file'],
                         mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    except Exception as e:
        logger.exception('excel quantita Lantek fallito')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/orders/<order_id>/lantek-ordini.xml', methods=['GET'])
@richiede('laser', 'ufficio')
def api_ordine_lantek_ordini_xml(order_id):
    """Il file per l'XML Importer di Lantek: un ordine di produzione per
    codice, con quantita', ordine, cliente e consegna. Solo i codici gia' in
    Lantek (i nuovi: prima si importano i DXF dal MES)."""
    try:
        import io
        from . import lantek as _lt
        order = _ordine_esistente(order_id)
        if not order:
            return _non_trovato_ordine()
        q = _quantita_lantek(order)
        if not q['righe']:
            return jsonify({'success': False, 'codice': 'nessun_pezzo',
                            'error': "Quest'ordine non ha pezzi di lamiera con un codice"}), 404
        righe = _lt.righe_per_xml(q['righe'])
        if not righe:
            if any(r['stato'] == 'nuovo' for r in q['righe']):
                return jsonify({'success': False, 'codice': 'pezzi_nuovi',
                                'error': 'I codici che mancano non sono ancora in Lantek: '
                                         'importa prima i DXF dal MES, poi riscarica il file'}), 409
            return jsonify({'success': False, 'codice': 'gia_in_produzione',
                            'error': "Tutti i codici sono già negli ordini di produzione di Lantek "
                                     "per quest'ordine"}), 409
        xml = _lt.xml_ordini_produzione(righe, q['commessa'], q['cliente_lantek'], q['consegna'])
        _audit('LANTEK_ORDINI_XML', 'orders', order_id,
               f"File ordini di produzione per Lantek: {len(righe)} codici"
               + (f" ({len(q['righe']) - len(righe)} nuovi esclusi)" if len(righe) < len(q['righe']) else ''))
        return send_file(io.BytesIO(xml), as_attachment=True, download_name=q['nome_file_xml'],
                         mimetype='application/xml')
    except Exception as e:
        logger.exception('xml ordini Lantek fallito')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/orders/<order_id>/pezzi/<articolo_id>/conferma', methods=['POST'])
@richiede('laser', 'ufficio')
def api_ordine_pezzo_conferma(order_id, articolo_id):
    """Verifica tecnica: il disegno di un pezzo dubbio e' giusto (abbinamento
    per somiglianza, contorno corretto in automatico, controllo che non torna)."""
    try:
        order = _ordine_esistente(order_id)
        if not order:
            return _non_trovato_ordine()
        if not order.preventivo_id_origine:
            return jsonify({'success': False, 'error': "Quest'ordine non ha pezzi da verificare"}), 404
        esito = PreventivoManager.conferma_controllo_articolo(order.preventivo_id_origine, articolo_id, _chi_nome())
        if not esito.get('success'):
            return jsonify({'success': False, 'error': esito.get('error') or 'non riuscito'}), 400
        _audit('PEZZO_VERIFICATO', 'orders', order_id, f'Disegno del pezzo {articolo_id} confermato giusto')
        return jsonify({'success': True}), 200
    except Exception as e:
        logger.exception('conferma pezzo fallita')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/orders/<order_id>/lantek-invia', methods=['POST'])
@richiede('laser', 'ufficio')
def api_ordine_lantek_invia(order_id):
    """"Manda a Lantek", dopo la conferma. Lo fanno i programmi di Lantek:
    1. i pezzi NUOVI col loro DXF (Procesos.exe, come iErp);
    2. gli ordini di produzione con le quantita' (XmlImporter).
    Poi si rilegge Lantek per dire cosa c'e' davvero.

    Corpo: {"codici": [...], "nuovi": [...]} = cio' che si e' visto nella
    conferma; se nel frattempo e' cambiato non si manda niente (409)."""
    try:
        from . import lantek as _lt
        order = _ordine_esistente(order_id)
        if not order:
            return _non_trovato_ordine()
        corpo = request.get_json(silent=True) or {}
        righe_dist = _distinta_ordine(order)[0]
        q = _quantita_lantek(order, righe_dist)
        righe = _lt.righe_per_xml(q['righe'])
        nuovi_si = []
        if q['lantek'].get('disponibile') and _lt.procesos_disponibile():
            nuovi_si = _nuovi_per_lantek(order, q, righe_dist)[0]
        if not righe and not nuovi_si:
            return jsonify({'success': False, 'codice': 'niente_da_mandare',
                            'error': 'Niente da mandare: i codici sono già in produzione in Lantek '
                                     'o i nuovi non si possono creare in automatico'}), 409
        if (sorted(str(c) for c in (corpo.get('codici') or [])) != sorted(r['codice'] for r in righe)
                or sorted(str(c) for c in (corpo.get('nuovi') or [])) != sorted(x['codice'] for x in nuovi_si)):
            return jsonify({'success': False, 'codice': 'cambiato',
                            'error': "L'elenco è cambiato da quando l'hai visto: ricontrolla e conferma di nuovo"}), 409
        if not _lt.xmlimporter_disponibile():
            return jsonify({'success': False, 'codice': 'no_xmlimporter',
                            'error': "XML Importer di Lantek non trovato su questo PC: scarica il file "
                                     "e importalo a mano"}), 503
        cartella = os.path.join(UPLOAD_FOLDER, 'lantek_import')
        esito_pezzi, creati = None, []
        # 1. pezzi nuovi col disegno
        if nuovi_si:
            cons = ''
            if q['consegna']:
                cons = datetime.strptime(q['consegna'][:10], '%Y-%m-%d').strftime('%d/%m/%Y')
            esito_pezzi = _lt.importa_pezzi_dxf(nuovi_si, cartella, [q['cliente_lantek'], cons])
            if not esito_pezzi.get('eseguito'):
                _audit('LANTEK_INVIA', 'orders', order_id,
                       'Pezzi nuovi non mandati a Lantek: ' + (esito_pezzi.get('errore') or ''))
                return jsonify({'success': False, 'codice': 'pezzi_falliti', 'error': esito_pezzi.get('errore')}), 502
            visti = _lt.pezzi_in_lantek([x['codice'] for x in nuovi_si]).get('pezzi') or {}
            creati = [x['codice'] for x in nuovi_si if (visti.get(x['codice']) or {}).get('esiste')]
            # ora ci sono: rientrano negli ordini di produzione
            q = _quantita_lantek(order, righe_dist)
            ammessi = {str(c) for c in (corpo.get('codici') or [])} | set(creati)
            righe = [r for r in _lt.righe_per_xml(q['righe']) if r['codice'] in ammessi]
        # 2. ordini di produzione
        rap, presenti = {}, []
        if righe:
            xml = _lt.xml_ordini_produzione(righe, q['commessa'], q['cliente_lantek'], q['consegna'],
                                            invio=datetime.now().strftime('%y%m%d%H%M'))
            esito = _lt.importa_xml(xml, q['nome_file_xml'].rsplit('.', 1)[0], cartella)
            if not esito.get('eseguito'):
                _audit('LANTEK_INVIA', 'orders', order_id,
                       f"Pezzi nuovi creati {len(creati)}; ordini di produzione non mandati: "
                       + (esito.get('errore') or ''))
                return jsonify({'success': False, 'codice': 'import_fallito', 'pezzi_creati': len(creati),
                                'error': esito.get('errore')}), 502
            rap = esito['rapporto']
            ora = _lt.ordini_in_lantek(q['commessa']) or {}
            presenti = [r['codice'] for r in righe if ora.get(str(r['codice']).strip().upper())]
        _audit('LANTEK_INVIA', 'orders', order_id,
               f"Mandati a Lantek (ordine {q['commessa']}): {len(creati)} di {len(nuovi_si)} pezzi nuovi creati, "
               f"{len(presenti)} di {len(righe)} ordini di produzione verificati")
        return jsonify({'success': True,
                        'pezzi_nuovi': len(nuovi_si), 'pezzi_creati': len(creati),
                        'pezzi_non_creati': [x['codice'] for x in nuovi_si if x['codice'] not in creati],
                        'mandati': len(righe), 'rapporto': rap, 'verificati': len(presenti),
                        'mancanti': [r['codice'] for r in righe if r['codice'] not in presenti]}), 200
    except Exception as e:
        logger.exception('invio a Lantek fallito')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/orders/<order_id>/crea-cartella', methods=['POST'])
@richiede('laser', 'ufficio')
def api_ordine_crea_cartella(order_id):
    """Bottone "Crea cartella" della pagina laser: la cartella dell'ordine nella
    cartella master (anche per gli ordini accettati prima)."""
    try:
        if not _ordine_esistente(order_id):
            return _non_trovato_ordine()
        e = _esporta_disegni_per_officina(order_id)
        if e.get('error'):
            codice = 'non_impostata' if 'non impostata' in e['error'] else 'errore'
            return jsonify({'success': False, 'codice': codice, 'error': e['error']}), 400
        return jsonify({'success': True, 'percorso': e['percorso'], 'esportati': e['esportati']}), 200
    except Exception as e:
        logger.exception('crea-cartella fallita')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/laser/cartella-disegni', methods=['GET', 'PUT', 'POST'])
@richiede('laser', 'ufficio', ADMIN, metodi=('GET',))
@richiede(ADMIN, metodi=('PUT', 'POST'))
def api_laser_cartella_disegni():
    """Cartella "master" dei disegni per Lantek (sul PC server).
    GET: {percorso, consentite}. PUT {percorso}: la salva (vuota = disattiva).
    POST {percorso}: prova; la crea se manca e ci scrive un file di prova.

    PUT e POST solo col PIN di un amministratore, e solo DENTRO le cartelle
    consentite (app_config.json, "cartelle_disegni_consentite", che si
    cambiano sul server con tools/persone.py): prima chiunque poteva far
    creare cartelle e scrivere file al server in qualunque percorso, anche su
    una condivisione di rete di un altro PC."""
    try:
        if request.method == 'GET':
            from .database import cartelle_consentite
            return jsonify({'success': True, 'percorso':
                            ((ConfigManager.load_config() or {}).get('disegni_export_root') or ''),
                            'consentite': cartelle_consentite()}), 200
        data = request.get_json(silent=True) or {}
        percorso = str(data.get('percorso') or '').strip().strip('"')
        if percorso and not (os.path.isabs(percorso) or percorso.startswith('\\\\')):
            return jsonify({'success': False, 'error': 'Serve un percorso completo, es. C:\\Commesse'}), 400
        from .database import cartella_non_consentita
        motivo = cartella_non_consentita(percorso)
        if motivo:
            return jsonify({'success': False, 'codice': 'non_consentita',
                            'error': 'Cartella non consentita: ' + motivo}), 400
        if request.method == 'POST' or percorso:
            # prova di scrittura (anche prima di salvare)
            try:
                os.makedirs(percorso, exist_ok=True)
                prova = os.path.join(percorso, '.ferrotrack_prova')
                with open(prova, 'w', encoding='utf-8') as fp:
                    fp.write('ok')
                os.remove(prova)
            except OSError as oe:
                return jsonify({'success': False, 'error': f'Il server non riesce a scrivere in {percorso}: {oe.strerror or oe}'}), 400
            if request.method == 'POST':
                return jsonify({'success': True, 'percorso': os.path.normpath(percorso)}), 200
        ConfigManager.save_config({'disegni_export_root': os.path.normpath(percorso) if percorso else ''})
        _audit('CARTELLA_DISEGNI', 'config', 'disegni_export_root', percorso or '(tolta)')
        return jsonify({'success': True, 'percorso': os.path.normpath(percorso) if percorso else ''}), 200
    except Exception as e:
        logger.exception('cartella-disegni fallita')
        return jsonify({'success': False, 'error': str(e)}), 500


def _nome_disegno_libero(cartella: str, nome: str) -> str:
    """Nome di file che non ne sovrascrive un altro.

    Due fornitori mandano tutti e due "flangia.dxf": prima il secondo caricamento
    cancellava il primo in silenzio e un articolo si ritrovava il disegno di un
    altro. Se il nome e' gia' occupato si aggiunge un progressivo, come farebbe
    Windows: flangia.dxf, flangia (2).dxf, ...
    """
    base = os.path.basename(nome or 'disegno.dxf')
    percorso = os.path.join(cartella, base)
    if not os.path.exists(percorso):
        return base
    radice, est = os.path.splitext(base)
    for n in range(2, 1000):
        candidato = f'{radice} ({n}){est}'
        if not os.path.exists(os.path.join(cartella, candidato)):
            logger.info('Disegno "%s" gia presente: salvato come "%s"', base, candidato)
            return candidato
    # Caso limite: si ripiega su un suffisso univoco invece di sovrascrivere.
    return f'{radice}_{uuid.uuid4().hex[:8]}{est}'


def _preventivo_to_pdf_dati(p: dict) -> dict:
    """Mappa il dict serializzato PreventivoManager al formato atteso da PDFPreventivo.genera_pdf().

    Differenze principali gestite:
    - Assiemi/tubolari/piastre: DB restituisce liste, PDF vuole dict per codice_assieme
    - Somma costi_piegatura/saldatura/filettatura/svasatura da articoli
    - Data ISO → dd/mm/yyyy italiano
    - Campi opzionali (azienda, logo_path) presi da app_config se disponibili
    """
    # Somma costi post-taglio da articoli (moltiplicati per quantità)
    articoli = p.get('articoli') or []
    tot_piega = sum(float(a.get('costo_piega') or 0) * int(a.get('quantita') or 1) for a in articoli)
    tot_sald = sum(float(a.get('costo_saldatura') or 0) * int(a.get('quantita') or 1) for a in articoli)
    tot_filett = sum(float(a.get('costo_filettatura') or 0) * int(a.get('quantita') or 1) for a in articoli)
    tot_svasat = sum(float(a.get('costo_svasatura') or 0) * int(a.get('quantita') or 1) for a in articoli)

    # Assiemi: lista → dict per codice_assieme (formato PDF)
    assiemi_list = p.get('assiemi') or []
    costi_montaggio = {}
    # Densità materiali per il peso al volo dei componenti DXF: UNA sola tabella,
    # quella dello stimatore (laser_config.materiali, con gli stessi sinonimi:
    # INOX_316L → INOX_304, ALU_6082 → ALU, S235JR → S235…). Prima qui c'era una
    # copia a mano che poteva divergere.
    from .preventivi.calcolo import costo_base_articolo as _costo_base_art
    _mat_cfg = (((ConfigManager.load_config() or {}).get('laser_config') or {}).get('materiali')
                or _laser_estimator.DEFAULT_LASER_CONFIG['materiali'])

    def _densita(materiale):
        _c, _m = _laser_estimator._resolve_material((materiale or '').upper(), _mat_cfg)
        return float((_m or {}).get('densita_kg_dm3') or 7.85)
    tubolari_list = p.get('tubolari') or []
    piastre_list = p.get('piastre') or []
    for a in assiemi_list:
        cod = a.get('codice_assieme') or a.get('id') or ''
        # BOM: articoli DXF figli di questo assieme (per stampa breakdown per componente)
        bom_articoli = []
        for art in articoli:
            if (art.get('codice_assieme') or '') != cod:
                continue
            qty_ass = int(art.get('quantita') or 1)
            base_pz = _costo_base_art(art)
            lav_pz = sum(float(art.get(k) or 0) for k in (
                'costo_piega', 'costo_saldatura', 'costo_filettatura',
                'costo_svasatura', 'costo_apporto', 'costo_pulizia'))
            tot_pz = base_pz + lav_pz
            rho = _densita(art.get('materiale'))
            peso_pz = (float(art.get('area_dm2') or 0)
                       * float(art.get('spessore_mm') or 0) / 100.0 * rho)
            bom_articoli.append({
                'codice': art.get('codice') or '',
                'materiale': art.get('materiale') or '',
                'spessore_mm': art.get('spessore_mm'),
                'qty_per_ass': qty_ass,
                'peso_kg_pz': peso_pz,
                'costo_base_pz': base_pz,
                'costo_lav_pz': lav_pz,
                'costo_tot_pz': tot_pz,
                'contributo_su_1_ass': tot_pz * qty_ass,
            })
        # BOM tubolari/piastre figli (contribuiscono per intero al costo assieme, no qty × per adesso)
        bom_tubolari = [t for t in tubolari_list if (t.get('codice_assieme') or '') == cod]
        bom_piastre = [pl for pl in piastre_list if (pl.get('codice_assieme') or '') == cod]
        costi_montaggio[cod] = {
            'ore_montaggio': a.get('ore_montaggio') or 0,
            'ore_puntatura': a.get('ore_puntatura') or 0,
            'costo': a.get('costo') or 0,
            'costo_puntatura': a.get('costo_puntatura') or 0,
            'costo_saldatura_assieme': a.get('costo_saldatura_assieme') or 0,
            'saldatura_mt': a.get('saldatura_mt') or 0,
            'peso_kg': a.get('peso_kg') or 0,
            'qty': a.get('qty') or 1,
            'bom_articoli': bom_articoli,
            'bom_tubolari': bom_tubolari,
            'bom_piastre': bom_piastre,
        }

    # Tubolari: raggruppa per codice_assieme in dict {codice: {'analisi': {'tubi': [...]}, 'costi': {...}}}
    tubolari_per_assieme = {}
    for t in tubolari_list:
        cod = t.get('codice_assieme') or 'GENERICO'
        node = tubolari_per_assieme.setdefault(cod, {'analisi': {'tubi': []}, 'costi': {'dettaglio_tubi': []}})
        # Deriva stringhe taglio da conteggio dritti/obliqui
        n_dr = int(t.get('n_tagli_dritti') or 0)
        n_ob = int(t.get('n_tagli_obliqui') or 0)
        # Assumiamo 2 estremi per tubo. Se ci sono obliqui, mostra "obliquo/dritto" o "obliquo/obliquo".
        if n_ob >= 2:
            taglio_1, taglio_2 = 'obliquo', 'obliquo'
        elif n_ob == 1:
            taglio_1, taglio_2 = 'obliquo', 'dritto'
        else:
            taglio_1, taglio_2 = 'dritto', 'dritto'
        node['analisi']['tubi'].append({
            'profilo': t.get('profilo') or '',
            'lunghezza_m': float(t.get('lunghezza_m') or 0),
            'taglio_1': taglio_1,
            'taglio_2': taglio_2,
            'peso_kg': float(t.get('peso_kg') or 0),
            'costo_materiale': float(t.get('costo_materiale') or 0),
            'costo_taglio': float(t.get('costo_taglio_totale') or 0),
            'materiale': t.get('materiale') or '',
        })

    # Piastre: raggruppa per codice_assieme nel formato dict che pdf_exporter si aspetta:
    # {codice: {'analisi': {'piastre': [{spessore_mm, area_dm2, peso_kg}, ...]},
    #           'costi':   {'dettaglio_piastre': [{'costo': N}, ...], 'totale': N}}}
    piastre_per_assieme = {}
    for pl in piastre_list:
        cod = pl.get('codice_assieme') or 'GENERICO'
        node = piastre_per_assieme.setdefault(cod, {
            'analisi': {'piastre': []},
            'costi': {'dettaglio_piastre': [], 'totale': 0.0},
        })
        node['analisi']['piastre'].append({
            'spessore_mm': pl.get('spessore_mm') or 0,
            'area_dm2': pl.get('area_dm2') or 0,
            'peso_kg': pl.get('peso_kg') or 0,
            'materiale': pl.get('materiale') or '',
        })
        costo = float(pl.get('costo') or 0)
        node['costi']['dettaglio_piastre'].append({'costo': costo})
        node['costi']['totale'] += costo

    # Data
    data_str = ''
    if p.get('data_creazione'):
        try:
            giorno = _giorno_iso(p['data_creazione'])
            data_str = giorno.strftime('%d/%m/%Y') if giorno else p['data_creazione']
        except Exception:
            data_str = p['data_creazione']
    else:
        data_str = datetime.now().strftime('%d/%m/%Y')

    # Ogni articolo per PDF vuole 'costo' = costo unitario totale
    articoli_pdf = []
    for a in articoli:
        # Stessa regola del calcolo autorevole: override, poi stima, poi il
        # costo materiale (es. valore Lantek). Prima qui mancava l'ultimo
        # ripiego e un pezzo prezzato solo da Lantek usciva a zero nel PDF.
        costo_base = _costo_base_art(a)
        costo_articolo = (float(costo_base or 0)
                          + float(a.get('costo_piega') or 0)
                          + float(a.get('costo_saldatura') or 0)
                          + float(a.get('costo_filettatura') or 0)
                          + float(a.get('costo_svasatura') or 0)
                          + float(a.get('costo_apporto') or 0)
                          + float(a.get('costo_pulizia') or 0))
        articoli_pdf.append({
            'codice': a.get('codice') or '',
            'quantita': a.get('quantita') or 1,
            'materiale': a.get('materiale') or '',
            'spessore_mm': a.get('spessore_mm'),
            'area_dm2': a.get('area_dm2') or 0,
            'costo': costo_articolo,
            'costo_base': costo_base,
            'costo_materiale': a.get('costo_materiale') or costo_base,
            'costo_piega': a.get('costo_piega') or 0,
            'costo_saldatura': a.get('costo_saldatura') or 0,
            'costo_filettatura': a.get('costo_filettatura') or 0,
            'costo_svasatura': a.get('costo_svasatura') or 0,
            'costo_apporto': a.get('costo_apporto') or 0,
            'costo_pulizia': a.get('costo_pulizia') or 0,
        })

    # Info azienda: da app_config sezione 'azienda' se presente
    app_cfg = ConfigManager.load_config() or {}
    azienda_info = app_cfg.get('azienda') or {}

    # ═══════════════════════════════════════════════════════════════════
    # CALCOLO TOTALI ON-THE-FLY (stesso algoritmo del frontend recalcTotali)
    # I campi `totale_pezzo`/`totale_lotto` nel DB non vengono mai aggiornati
    # dagli save-articoli: leggerli darebbe sempre 0. Ricalcolo qui.
    # ═══════════════════════════════════════════════════════════════════
    qty_preventivo = int(p.get('quantita') or 1)
    margine_pct = float(p.get('margine_pct') or 0)

    # Se il preventivo e' gia' stato INVIATO si usano le percentuali CONGELATE
    # a quel momento: altrimenti bastava ritoccare i costi in configurazione per
    # far cambiare da solo il PDF di un'offerta gia' in mano al cliente.
    _snap = p.get('snapshot_economico') or None
    if _snap:
        _generali_pct = float(_snap.get('costo_generali_pct') or 0)
        # Un ricarico congelato a 0 e' un valore legittimo: `or` lo scartava
        # e rimetteva il margine attuale.
        _ric_snap = _snap.get('ricarico_pct')
        margine_pct = float(_ric_snap) if _ric_snap is not None else margine_pct
    else:
        _generali_pct = float((app_cfg.get('preventivi_config') or {}).get('costo_generali_pct', 0))

    # Fattore prezzo finale = generali (overhead) × ricarico. Incorpora TUTTO
    # ciò che il cliente non deve vedere scomposto (costi + margine).
    _gen_f = 1 + _generali_pct / 100.0
    _f_finale = _gen_f * (1 + margine_pct / 100.0)

    # Calcolo autorevole PRIMA delle righe: le righe degli assiemi e dei
    # tubolari/piastre ne usano la composizione (apporto/pulizia dei cordoni
    # di assieme, qty delle righe STEP), cosi' sommano al totale.
    _tot = None
    try:
        from .preventivi.calcolo import (calcola as _calcola_autorevole,
                                         costo_tubolare as _costo_tub,
                                         costo_piastra as _costo_pia)
        _cfg_prezzo = ({'costo_generali_pct': _generali_pct} if _snap
                       else (app_cfg.get('preventivi_config') or {}))
        _p_calc = ({**p, 'margine_pct': margine_pct,
                    'sconto_pct': _snap.get('sconto_pct', p.get('sconto_pct'))}
                   if _snap else p)
        _tot = _calcola_autorevole(_p_calc, _cfg_prezzo)
    except Exception as _e:
        logger.exception('calcolo autorevole per il PDF fallito: %s', _e)
        _costo_tub = lambda t: float(t.get('costo_materiale') or 0) + float(t.get('costo_taglio_totale') or 0)  # noqa: E731
        _costo_pia = lambda pl: float(pl.get('costo') or 0)  # noqa: E731
    # Stesso ordine della lista assiemi (calcola li scorre in ordine)
    _dett_ass = list((_tot or {}).get('dettaglio_assiemi') or [])

    # Righe per il PDF CLIENTE: codice + descrizione + qty + prezzo finale.
    # Costruite QUI, negli stessi loop che formano il totale, così sommano
    # esattamente a totale_lotto senza rivelare alcun costo.
    righe_cliente = []

    def _desc_articolo(a):
        mat = (a.get('materiale') or '').replace('_', ' ').strip()
        sp = a.get('spessore_mm')
        parts = []
        if mat:
            parts.append(mat)
        if sp:
            parts.append(f"sp.{float(sp):g}")
        return ' · '.join(parts)

    # Articoli STANDALONE (senza codice_assieme): quantita = pezzi nel preventivo
    _cliente_deca = bool(re.search(r'(?<![A-Za-z])DECA(?![A-Za-z])', p.get('cliente') or '', re.I))
    totale_pezzo_calc = 0.0
    for a in articoli:
        if a.get('codice_assieme'):
            continue  # articoli linkati ad assieme già dentro pricing assieme
        base = _costo_base_art(a)
        lav = sum(float(a.get(k) or 0) for k in (
            'costo_piega', 'costo_saldatura', 'costo_filettatura',
            'costo_svasatura', 'costo_apporto', 'costo_pulizia'))
        qty_art = int(a.get('quantita') or 1)
        totale_pezzo_calc += (base + lav) * qty_art
        prezzo_unit_finale = (base + lav) * _f_finale
        qty_tot = qty_art * qty_preventivo
        # DECA: lo stesso codice compare su piu' righe dell'ordine (una per
        # commessa). Il preventivo le riporta UGUALI (codice, commessa,
        # quantita'), cosi' il cliente lo confronta riga per riga col suo
        # ordine. Solo se le righe sommano alla quantita' del pezzo: se e'
        # stata cambiata a mano, una riga sola come prima.
        _righe_ord = ((a.get('ordine') or {}).get('righe') or []) if _cliente_deca else []
        if _righe_ord and abs(sum(float(r.get('qta') or 0) for r in _righe_ord) - qty_tot) < 1e-6:
            _pu = round(prezzo_unit_finale, 2)
            for r in _righe_ord:
                _q = float(r.get('qta') or 0)
                _q = int(_q) if _q.is_integer() else _q
                righe_cliente.append({
                    'codice': a.get('codice') or '—',
                    'commessa': r.get('commessa') or '',
                    'descrizione': _desc_articolo(a),
                    'quantita': _q,
                    'prezzo_unitario': _pu,
                    'importo': round(_pu * _q, 2),
                })
            continue
        righe_cliente.append({
            'codice': a.get('codice') or '—',
            'descrizione': _desc_articolo(a),
            'quantita': qty_tot,
            'prezzo_unitario': round(prezzo_unit_finale, 2),
            'importo': round(prezzo_unit_finale * qty_tot, 2),
        })

    # Costo assiemi (totale pricing rollup)
    costo_assiemi_calc = 0.0
    for _i_ass, asm in enumerate(assiemi_list):
        cod = asm.get('codice_assieme') or asm.get('id') or ''
        info = costi_montaggio.get(cod, {})
        # Somma componenti articoli DXF (contributo_su_1_ass già calcolato sopra)
        cost_articoli_ass = sum(item.get('contributo_su_1_ass', 0) for item in info.get('bom_articoli', []))
        # Tubolari/piastre nell'assieme (costi gia' totali della riga, qty inclusa)
        cost_tubolari_ass = sum(_costo_tub(t) for t in info.get('bom_tubolari', []))
        cost_piastre_ass = sum(_costo_pia(pl) for pl in info.get('bom_piastre', []))
        # Costo intrinseco assieme (montaggio + puntatura + saldatura + apporto
        # e pulizia dei cordoni, questi ultimi dal calcolo autorevole)
        _d_ass = _dett_ass[_i_ass] if _i_ass < len(_dett_ass) else {}
        cost_intrinseco = (float(info.get('costo') or 0)
                           + float(info.get('costo_puntatura') or 0)
                           + float(info.get('costo_saldatura_assieme') or 0)
                           + float(_d_ass.get('apporto_pulizia') or 0))
        info['apporto_pulizia'] = float(_d_ass.get('apporto_pulizia') or 0)
        prezzo_1_ass = cost_intrinseco + cost_articoli_ass + cost_tubolari_ass + cost_piastre_ass
        qty_ass = int(info.get('qty') or 1)
        costo_assiemi_calc += prezzo_1_ass * qty_ass
        prezzo_ass_finale = prezzo_1_ass * _f_finale
        n_comp = len(info.get('bom_articoli', [])) + len(info.get('bom_tubolari', [])) + len(info.get('bom_piastre', []))
        righe_cliente.append({
            'codice': cod or 'Assieme',
            'descrizione': f"Assieme montato ({n_comp} componenti)" if n_comp else "Assieme montato",
            'quantita': qty_ass,
            'prezzo_unitario': round(prezzo_ass_finale, 2),
            'importo': round(prezzo_ass_finale * qty_ass, 2),
        })

    # Tubolari e piastre STANDALONE (no codice_assieme)
    costo_tubolari_std = sum(_costo_tub(t) for t in tubolari_list if not t.get('codice_assieme'))
    costo_piastre_std = sum(_costo_pia(pl) for pl in piastre_list if not pl.get('codice_assieme'))
    if costo_tubolari_std > 0:
        imp_tub = round(costo_tubolari_std * _f_finale, 2)
        righe_cliente.append({
            'codice': 'Tubolari', 'descrizione': 'Profilati a misura',
            'quantita': 1, 'prezzo_unitario': imp_tub, 'importo': imp_tub,
        })
    if costo_piastre_std > 0:
        imp_pia = round(costo_piastre_std * _f_finale, 2)
        righe_cliente.append({
            'codice': 'Piastre', 'descrizione': 'Piastre a disegno',
            'quantita': 1, 'prezzo_unitario': imp_pia, 'importo': imp_pia,
        })

    # Generali (overhead) sul costo, poi ricarico — coerente col frontend
    con_margine = totale_pezzo_calc * _gen_f * (1 + margine_pct / 100.0)
    con_margine_assiemi = costo_assiemi_calc * _gen_f * (1 + margine_pct / 100.0)
    con_margine_tubolari = costo_tubolari_std * _gen_f * (1 + margine_pct / 100.0)
    con_margine_piastre = costo_piastre_std * _gen_f * (1 + margine_pct / 100.0)
    totale_lotto_calc = (con_margine * qty_preventivo
                          + con_margine_assiemi + con_margine_tubolari + con_margine_piastre)

    # Il totale del documento lo decide il calcolo autorevole, lo stesso usato
    # all'accettazione: cosi' il numero sul PDF e quello che finisce sull'ordine
    # non possono divergere. Le righe qui sopra restano per il dettaglio.
    _sconto_pct_pdf, _sconto_eur = 0.0, 0.0
    if _tot:
        _scarto = abs(_tot['totale_lotto_lordo'] - totale_lotto_calc)
        if _scarto > 0.5:
            logger.warning(
                'PDF preventivo %s: righe %.2f vs calcolo autorevole %.2f',
                p.get('id'), totale_lotto_calc, _tot['totale_lotto_lordo'])
        totale_lotto_calc = _tot['totale_lotto_lordo']
        costo_assiemi_calc = _tot['costo_assiemi']
        # Uno sconto deve comparire come riga, altrimenti le righe non
        # sommerebbero piu' al totale e il cliente non capirebbe il numero.
        if _tot['sconto_pct']:
            _sconto_pct_pdf = _tot['sconto_pct']
            _sconto_eur = round(_tot['totale_lotto'] - _tot['totale_lotto_lordo'], 2)
            righe_cliente.append({
                'codice': 'Sconto',
                'descrizione': f"Sconto {_tot['sconto_pct']:g}%",
                'quantita': 1, 'prezzo_unitario': _sconto_eur, 'importo': _sconto_eur,
            })
            totale_lotto_calc = _tot['totale_lotto']

    return {
        'cliente': p.get('cliente') or '',
        'numero_ordine': p.get('numero_ordine_cliente') or f"PREV-{p.get('id', '')[:8]}",
        'data': data_str,
        'articoli': articoli_pdf,
        'quantita': qty_preventivo,
        'margine': margine_pct,
        # Per la distinta interna: fattore costo→prezzo (generali × ricarico) e
        # sconto, cosi' le sue righe sommano al TOTALE come quelle del cliente.
        'costi_generali_pct': _generali_pct,
        'fattore_prezzo': _f_finale,
        'sconto_pct': _sconto_pct_pdf,
        'sconto_eur': _sconto_eur,
        'costi_montaggio': costi_montaggio,
        'tubolari_per_assieme': tubolari_per_assieme,
        'piastre_per_assieme': piastre_per_assieme,
        'data_consegna': _fmt_data_it(p.get('data_consegna_proposta')),
        'totale_pezzo': round(totale_pezzo_calc, 2),
        'totale_lotto': round(totale_lotto_calc, 2),
        'righe_cliente': righe_cliente,
        'costo_piegatura': tot_piega,
        'costo_saldatura': tot_sald,
        'costo_filettatura': tot_filett,
        'costo_svasatura': tot_svasat,
        'costo_montaggio_totale': round(costo_assiemi_calc, 2),
        'costo_tubolari_totale': round(costo_tubolari_std, 2),
        'costo_piastre_totale': round(costo_piastre_std, 2),
        'note': _note_per_cliente(p.get('note')),
        'azienda': azienda_info,
        'logo_path': _resolve_logo_path(azienda_info.get('logo_path')),
    }


def _fmt_data_it(iso):
    """ISO date/datetime → 'dd/mm/yyyy' (o '' se assente/invalida)."""
    if not iso:
        return ''
    try:
        return datetime.fromisoformat(str(iso)).strftime('%d/%m/%Y')
    except (ValueError, TypeError):
        s = str(iso)
        return s[:10] if len(s) >= 10 else s


def _note_per_cliente(note):
    """Nasconde le note INTERNE dal PDF che va al cliente (es. 'Importato via AI RFQ')."""
    n = (note or '').strip()
    if n.startswith('Importato via AI RFQ'):
        return ''
    return n


def _resolve_logo_path(logo_path):
    """Risolve il path del logo: assoluto → così com'è; relativo → da app/backend/."""
    if not logo_path:
        return None
    if os.path.isabs(logo_path):
        return logo_path
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), logo_path)


def _persisti_totali(preventivo_id):
    """Ricalcola e SALVA i totali del preventivo sul DB (stessa formula del PDF/
    riepilogo). La lista storico legge totale_lotto dal DB, quindi senza questo
    resterebbe 0. No-op se preventivo non trovato o non più BOZZA (immutabile)."""
    try:
        p = PreventivoManager.get(preventivo_id, include_children=True)
        if not p or p.get('status') != 'BOZZA':
            return
        dati = _preventivo_to_pdf_dati(p)
        PreventivoManager.update(preventivo_id, {
            'totale_pezzo': dati.get('totale_pezzo') or 0,
            'totale_lotto': dati.get('totale_lotto') or 0,
            'costi_montaggio_totale': dati.get('costo_montaggio_totale') or 0,
            'costi_tubolari_totale': dati.get('costo_tubolari_totale') or 0,
            'costi_piastre_totale': dati.get('costo_piastre_totale') or 0,
        })
    except Exception:
        logger.warning('persisti_totali fallito per %s', preventivo_id, exc_info=True)


@app.route('/api/preventivi/<preventivo_id>/pdf', methods=['GET'])
@richiede(*UFFICI)
def api_preventivi_pdf(preventivo_id):
    """Genera e serve il PDF del preventivo.

    Query param `interno=1` → distinta INTERNA (costi scomposti + margine + BOM,
    uso ufficio). Default → PDF CLIENTE pulito (prezzi finali, niente costi).
    """
    try:
        p = PreventivoManager.get(preventivo_id, include_children=True)
        if not p:
            return jsonify({'success': False, 'error': 'Preventivo non trovato'}), 404

        interno = str(request.args.get('interno', '')).lower() in ('1', 'true', 'yes')
        # inline=1 → mostra nel browser (anteprima), altrimenti scarica.
        inline = str(request.args.get('inline', '')).lower() in ('1', 'true', 'yes')

        dati_pdf = _preventivo_to_pdf_dati(p)

        # Genera in cartella preventivi
        pdf_dir = os.path.join(UPLOAD_FOLDER, 'preventivi_pdf')
        os.makedirs(pdf_dir, exist_ok=True)
        suffisso = 'interno' if interno else 'cliente'
        filename = f"preventivo_{suffisso}_{p.get('numero_ordine_cliente') or preventivo_id[:8]}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
        # Sanitize filename
        filename = ''.join(c if c.isalnum() or c in '._-' else '_' for c in filename)
        pdf_path = os.path.join(pdf_dir, filename)

        app_cfg = ConfigManager.load_config() or {}
        exporter = _pdf_exporter.PDFPreventivo(app_cfg)
        exporter.genera_pdf(pdf_path, dati_pdf, interno=interno)

        # Nome del file scaricato: cliente, ordine e data (il file sul server
        # resta col nome univoco).
        return send_file(pdf_path, mimetype='application/pdf',
                         as_attachment=not inline, download_name=_nome_pdf_preventivo(p, interno))
    except Exception as e:
        logger.exception('preventivi pdf failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/storico-prezzo', methods=['GET'])
@richiede(*UFFICI)
def api_preventivi_storico_prezzo():
    """Storico prezzi di un pezzo già visto: match per codice O geometria (hash).

    Query params:
      - codice: codice del pezzo (es. 47PA01397-00)
      - sha: canonical_dxf_sha256 della geometria confermata (opzionale)
      - exclude: preventivo_id da escludere (il preventivo corrente)

    Ritorna {success, occorrenze:[...], riepilogo:{...}|None}. Costo confrontato
    = costo base del pezzo (senza margine), coerente tra preventivi.
    """
    try:
        codice = request.args.get('codice', '')
        sha = request.args.get('sha', '')
        exclude = request.args.get('exclude', '') or None
        if not codice.strip() and not sha.strip():
            return jsonify({'success': True, 'occorrenze': [], 'riepilogo': None})
        res = PreventivoManager.storico_prezzo(
            codice=codice, sha256=sha, exclude_preventivo_id=exclude)
        return jsonify({'success': True, **res})
    except Exception as e:
        logger.exception('storico-prezzo failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/storico-prezzo-batch', methods=['POST'])
@richiede(*UFFICI)
def api_preventivi_storico_prezzo_batch():
    """Batch: per una lista di pezzi ritorna chi è già stato prezzato altrove.

    Body JSON: {items:[{codice, sha}], exclude: preventivo_id}. Usato per i badge
    'già prezzato' sulla lista pezzi senza N chiamate singole.
    """
    try:
        data = request.get_json(silent=True) or {}
        items = data.get('items') or []
        exclude = data.get('exclude') or None
        risultati = PreventivoManager.storico_prezzo_batch(items, exclude_preventivo_id=exclude)
        return jsonify({'success': True, 'risultati': risultati})
    except Exception as e:
        logger.exception('storico-prezzo-batch failed')
        return jsonify({'success': False, 'error': str(e)}), 500


def _contorno_incerto(a: dict, materiali: dict) -> bool:
    """Come wbContorno(a) === 'incerto' nella pagina: riconoscimento poco
    sicuro, non confermato nel CAD, non scritto a mano, non verificato."""
    if not a.get('dxf_filename') or a.get('geometria_manuale_confermata') or a.get('pezzo_manuale'):
        return False
    if a.get('geometry_source') in ('sviluppo', 'step'):
        return False
    if (a.get('contorno_auto') or {}).get('stato') == 'confermato' or a.get('verifica_ok'):
        return False
    conf = a.get('dxf_confidence')
    if not (a.get('dxf_needs_verify') or (conf is not None and float(conf) < 0.7)):
        return False
    # peso che torna col cartiglio (tolleranza come in pagina: 15% o 15 g)
    try:
        peso = float(((a.get('verifica') or {}).get('peso_cartiglio') or {}).get('peso_kg') or 0)
        mat = materiali.get(str(a.get('materiale') or '').upper()) or {}
        dens = float(mat.get('densita_kg_dm3') or 7.85)
        calc = float(a.get('area_dm2') or 0) * float(a.get('spessore_mm') or 0) / 100.0 * dens
        if peso > 0 and calc > 0 and (abs(calc / peso - 1) <= 0.15 or abs(calc - peso) <= 0.015):
            return False
    except (TypeError, ValueError):
        pass
    return True


def _valida_costi_preventivo(preventivo_id):
    """GUARDIA CRITICA server-side: verifica che nessun articolo abbia costo base 0.

    Un articolo è valido se ha `costo_base_override > 0` OPPURE `costo_base_stimato > 0`.
    Ritorna lista di dict {codice, motivo} per gli articoli invalidi (vuota se tutto ok).
    Difesa in profondità: il frontend blocca già ma un client custom o bug JS potrebbe
    aggirare la validazione; qui è invalicabile.
    """
    prev = PreventivoManager.get(preventivo_id, include_children=True)
    if not prev:
        return []  # non trovato → l'endpoint darà 404 da sé
    invalidi = []
    _materiali_cfg = ((ConfigManager.load_config() or {}).get('laser_config') or {}).get('materiali') or {}
    for a in (prev.get('articoli') or []):
        codice = a.get('codice') or '(senza codice)'
        # 1) Contorno INCERTO (stessa regola della pagina, wbContorno): blocca
        #    solo il riconoscimento poco sicuro non confermato e non verificato
        #    col peso del cartiglio. Prima bloccava ogni DXF non aperto nel CAD:
        #    l'invio passava e l'accettazione no (DECA 1240).
        if _contorno_incerto(a, _materiali_cfg):
            invalidi.append({'codice': codice, 'motivo': 'contorno incerto: confermalo nel CAD'})
            continue
        # 2) Costo base > 0
        overr = a.get('costo_base_override')
        stim = a.get('costo_base_stimato') or 0
        if (overr is not None and overr > 0) or (stim and stim > 0):
            continue
        if not (a.get('materiale') and a.get('spessore_mm')
                and a.get('area_dm2') and a.get('perimetro_taglio_m')):
            invalidi.append({'codice': codice, 'motivo': 'dati mancanti (materiale/spessore/area/perimetro)'})
        else:
            invalidi.append({'codice': codice, 'motivo': 'stima laser non eseguita'})
    # 3) ASSIEMI: tempo di montaggio OBBLIGATORIO (l'assieme costa componenti +
    #    montaggio + saldatura). Senza → sottoprezzato → blocco.
    for A in (prev.get('assiemi') or []):
        cod = A.get('codice_assieme') or '(assieme)'
        if (A.get('ore_montaggio') or 0) <= 0 and (A.get('costo') or 0) <= 0:
            invalidi.append({'codice': cod, 'motivo': 'manca il tempo di montaggio dell\'assieme'})
    return invalidi


@app.route('/api/preventivi/<preventivo_id>/invia', methods=['POST'])
@richiede('commerciale')
def api_preventivi_invia(preventivo_id):
    """Transizione BOZZA → INVIATO. (Snapshot versioning sarà aggiunto in Fase 3.)"""
    try:
        data = request.get_json(silent=True) or {}
        user_id = _chi()
        # Controllo unico prima di un'operazione definitiva: senza, l'invio
        # partiva e i buchi si scoprivano dal cliente.
        esito = _verifica_preventivo(preventivo_id)
        if esito is None:
            return jsonify({'success': False, 'error': 'Preventivo non trovato'}), 404
        if not esito['pronto']:
            dettagli = '; '.join(x['messaggio'] for x in esito['errori'][:5])
            return jsonify({
                'success': False,
                'error': f"Impossibile inviare: {len(esito['errori'])} punti da "
                         f'risolvere ({dettagli})',
                'errori': esito['errori'],
                'avvisi': esito['avvisi'],
            }), 400
        _persisti_totali(preventivo_id)  # congela i totali sul DB prima di INVIATO
        result = PreventivoManager.transition_status(preventivo_id, 'INVIATO', user_id=user_id)
        if result is None:
            return jsonify({'success': False, 'error': 'Preventivo non trovato'}), 404
        if isinstance(result, dict) and result.get('error'):
            return jsonify({'success': False, 'error': result['error']}), 409
        try:
            AuditManager.log(user_id=user_id, action='SEND_PREVENTIVO',
                             entity_type='preventivi', entity_id=preventivo_id, detail='BOZZA->INVIATO')
        except Exception:
            pass
        return jsonify({'success': True, 'preventivo': result}), 200
    except Exception as e:
        logger.exception('preventivi invia failed')
        return jsonify({'success': False, 'error': str(e)}), 500


def _nome_pdf_preventivo(p: dict, interno: bool = False) -> str:
    """Nome leggibile del PDF: cliente, ordine e data, cosi' si ritrova nella
    cartella Download e negli allegati. L'interno comincia con INTERNO per non
    mandarlo per sbaglio al cliente."""
    def _pulito(s):
        s = ''.join(ch for ch in str(s or '') if ch not in '\\/:*?"<>|\r\n\t').strip()
        return ' '.join(s.split())[:60]
    parti = [_pulito(p.get('cliente')) or 'senza cliente']
    rif = _pulito(p.get('numero_ordine_cliente'))
    if rif:
        parti.append(f'ord {rif}')
    parti.append(datetime.now().strftime('%Y-%m-%d'))
    return ('INTERNO costi - ' if interno else 'Preventivo ') + ' - '.join(parti) + '.pdf'


def _genera_pdf_cliente_bytes(p: dict) -> tuple[bytes, str]:
    """Genera il PDF CLIENTE del preventivo e ne ritorna (bytes, filename).

    Riusa la stessa pipeline di /api/preventivi/<id>/pdf (interno=False).
    """
    dati_pdf = _preventivo_to_pdf_dati(p)
    pdf_dir = os.path.join(UPLOAD_FOLDER, 'preventivi_pdf')
    os.makedirs(pdf_dir, exist_ok=True)
    base = p.get('numero_ordine_cliente') or (p.get('id') or '')[:8]
    filename = f"Preventivo_{base}.pdf"
    filename = ''.join(c if c.isalnum() or c in '._-' else '_' for c in filename)
    pdf_path = os.path.join(pdf_dir, filename)
    app_cfg = ConfigManager.load_config() or {}
    _pdf_exporter.PDFPreventivo(app_cfg).genera_pdf(pdf_path, dati_pdf, interno=False)
    with open(pdf_path, 'rb') as fp:
        return fp.read(), _nome_pdf_preventivo(p)


_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def _azienda_info() -> dict:
    """Dati azienda (per firma email / intestazioni), da app_config.json."""
    cfg = ConfigManager.load_config() or {}
    return cfg.get('azienda') or {}


def _firma_email() -> str:
    """Firma testuale per le email, costruita dai dati azienda in config."""
    az = _azienda_info()
    nome = az.get('nome') or 'Carpenteria L.S. S.r.l.'
    righe = [nome]
    if az.get('indirizzo'):
        righe.append(az['indirizzo'])
    contatti = []
    if az.get('telefono'):
        contatti.append('Tel. ' + str(az['telefono']))
    if az.get('email'):
        contatti.append(str(az['email']))
    if contatti:
        righe.append(' — '.join(contatti))
    return '\n'.join(righe)


@app.route('/api/preventivi/email-config', methods=['GET'])
@richiede('commerciale')
def api_preventivi_email_config():
    """Stato (non sensibile) della config SMTP + dati azienda (per firma), per la UI."""
    try:
        return jsonify({
            'success': True,
            **_email_sender.config_summary(),
            'azienda': _azienda_info(),
        }), 200
    except Exception as e:
        logger.exception('email-config failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/ultima-email-cliente', methods=['GET'])
@richiede('commerciale')
def api_preventivi_ultima_email_cliente():
    """Ultimo indirizzo email usato per un cliente, per riproporlo all'invio."""
    try:
        cliente = request.args.get('cliente', '')
        return jsonify({'success': True, 'email': PreventivoManager.ultima_email_cliente(cliente)}), 200
    except Exception as e:
        logger.exception('ultima-email-cliente failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/invia-email', methods=['POST'])
@richiede('commerciale')
def api_preventivi_invia_email(preventivo_id):
    """Spedisce il PDF cliente via email e, se il preventivo è BOZZA, lo porta a
    INVIATO (immutabile). La transizione avviene SOLO se la mail parte davvero.

    Body: {user_id, to, subject?, message?}
    Se SMTP non è configurato → {success:False, email_non_configurata:True}.
    """
    try:
        data = request.get_json(silent=True) or {}
        user_id = _chi()

        to_addr = (data.get('to') or '').strip()
        if not to_addr or not _EMAIL_RE.match(to_addr):
            return jsonify({'success': False, 'error': 'Indirizzo email del destinatario non valido'}), 400

        if not _email_sender.is_configured():
            return jsonify({
                'success': False,
                'email_non_configurata': True,
                'error': 'Invio email non configurato. Imposta SMTP_HOST/SMTP_USER/SMTP_PASSWORD nel file app/.env.',
            }), 400

        p = PreventivoManager.get(preventivo_id, include_children=True)
        if not p:
            return jsonify({'success': False, 'error': 'Preventivo non trovato'}), 404

        # Stesso gate anti-catastrofe dell'invio: nessun costo laser mancante.
        invalidi = _valida_costi_preventivo(preventivo_id)
        if invalidi:
            details = '; '.join(f"{x['codice']}: {x['motivo']}" for x in invalidi[:5])
            return jsonify({
                'success': False,
                'error': f'Impossibile inviare: {len(invalidi)} punti da risolvere ({details})',
                'articoli_invalidi': invalidi,
            }), 400

        # Congela i totali prima di generare il PDF/transizione.
        _persisti_totali(preventivo_id)
        p = PreventivoManager.get(preventivo_id, include_children=True)  # ricarica coi totali freschi

        # Genera il PDF cliente e spediscilo.
        pdf_bytes, pdf_name = _genera_pdf_cliente_bytes(p)
        subject = (data.get('subject') or '').strip() or f"Preventivo {p.get('cliente') or ''}".strip()
        message = (data.get('message') or '').strip() or (
            "Buongiorno,\n\nin allegato trovate il preventivo richiesto.\n"
            "Restiamo a disposizione per qualsiasi chiarimento.\n\nCordiali saluti\n"
            + _firma_email()
        )
        ok, err = _email_sender.send_email(
            to_addr, subject, message,
            attachments=[(pdf_name, pdf_bytes, 'application', 'pdf')],
        )
        if not ok:
            return jsonify({'success': False, 'error': err or 'Invio email fallito'}), 502

        # Mail partita → se ancora BOZZA, porta a INVIATO.
        preventivo = p
        if p.get('status') == 'BOZZA':
            result = PreventivoManager.transition_status(preventivo_id, 'INVIATO', user_id=user_id)
            if isinstance(result, dict) and not result.get('error'):
                preventivo = result
        # Registra destinatario + timestamp (anche su INVIATO: è metadato d'invio).
        aggiornato = PreventivoManager.set_email_inviata(preventivo_id, to_addr)
        if aggiornato:
            preventivo = aggiornato
        try:
            AuditManager.log(user_id=user_id, action='EMAIL_PREVENTIVO',
                             entity_type='preventivi', entity_id=preventivo_id,
                             detail=f'PDF inviato a {to_addr}')
        except Exception:
            pass
        return jsonify({'success': True, 'preventivo': preventivo, 'inviato_a': to_addr}), 200
    except Exception as e:
        logger.exception('preventivi invia-email failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/accetta', methods=['POST'])
@richiede(*UFFICI)
def api_preventivi_accetta(preventivo_id):
    """Transizione INVIATO → ACCETTATO + creazione atomica Order FerroTrack.

    Body opzionale:
      {
        "user_id": "...",
        "articoli": [...],                  // se passati, sostituiscono quelli su DB
        "totali": {"totale_pezzo", "totale_pezzo_con_margine", "totale_lotto"},
        "data_consegna": "YYYY-MM-DD",      // override della data_consegna_proposta
        "note_aggiuntive": "..."            // appese alle note ordine FerroTrack
      }

    Output: {success, order_id, numero_ordine, preventivo}.
    """
    try:
        # Anche l'accettazione e' definitiva: crea un ordine e blocca un prezzo.
        _pre = _verifica_preventivo(preventivo_id)
        if _pre is not None and not _pre['pronto']:
            _dett = '; '.join(x['messaggio'] for x in _pre['errori'][:5])
            return jsonify({
                'success': False,
                'error': f"Impossibile accettare: {len(_pre['errori'])} punti da "
                         f'risolvere ({_dett})',
                'errori': _pre['errori'],
            }), 400
        data = request.get_json(silent=True) or {}
        # Anche l'Ufficio (Elena) conferma: è lei che vede la risposta via mail
        # del cliente e fa partire il ciclo produttivo (regola: i due uffici).
        user_id = _chi()
        # Data di consegna: controllata PRIMA di scrivere qualsiasi cosa. Prima
        # una data sbagliata faceva fallire la creazione dell'ordine dopo che
        # gli articoli erano gia' stati riscritti.
        _dc = (data.get('data_consegna') or '').strip()
        if _dc:
            try:
                _d = datetime.strptime(_dc[:10], '%Y-%m-%d').date()
            except ValueError:
                return jsonify({'success': False, 'error': 'Data di consegna non valida (serve AAAA-MM-GG)'}), 400
            from .orario import oggi_locale as _oggi
            if _d < _oggi() - timedelta(days=1):
                return jsonify({'success': False, 'error': 'La data di consegna risulta nel passato'}), 400
            data['data_consegna'] = _d.isoformat()
        # (Prima qui c'era replace_articoli con `except: pass`: su un preventivo
        # INVIATO falliva sempre, in silenzio. Gli articoli li salva
        # accetta_e_crea_ordine, che in caso di errore rifiuta e ripristina.)
        invalidi = _valida_costi_preventivo(preventivo_id)
        if invalidi:
            details = '; '.join(f"{x['codice']}: {x['motivo']}" for x in invalidi[:5])
            return jsonify({
                'success': False,
                'error': f'Impossibile accettare: {len(invalidi)} articoli senza costo laser ({details})',
                'articoli_invalidi': invalidi,
            }), 400
        result = PreventivoManager.accetta_e_crea_ordine(
            preventivo_id,
            user_id=user_id,
            articoli=data.get('articoli'),
            assiemi=data.get('assiemi'),
            tubolari=data.get('tubolari'),
            piastre=data.get('piastre'),
            totali=data.get('totali'),
            note_aggiuntive=data.get('note_aggiuntive'),
            data_consegna_override=data.get('data_consegna'),
        )
        if not result or result.get('error'):
            err = result.get('error') if result else 'Errore sconosciuto'
            return jsonify({'success': False, 'error': err}), 409
        # Copia i DXF puliti (o originali con warning) in uploads/drawings/<order_id>/
        # PRIMA del cleanup del tmp del preventivo. Mirko taglierà da lì.
        dxf_stats = {}
        pdf_stats = {}
        order_id = result.get('order_id')
        if order_id:
            dxf_stats = _copy_cleaned_dxf_to_drawings(preventivo_id, order_id)
            result['dxf_transfer'] = dxf_stats
            # Copia leggibile sulla cartella di rete: e' quella che
            # l'operatore apre in Lantek. Non blocca l'accettazione.
            try:
                result['export_disegni'] = _esporta_disegni_per_officina(
                    order_id,
                    (result.get('preventivo') or {}).get('cliente') or '',
                    ((result.get('preventivo') or {}).get('numero_ordine_cliente') or '').strip()
                    or result.get('numero_ordine') or '')
            except Exception as _e:
                logger.warning('export disegni in cartella di rete: %s', _e)

            # Allega il PDF ordine (disegni/lavorazioni) così Mirko vede cosa fare.
            pdf_stats = _copy_order_pdf_to_order(preventivo_id, order_id)
            result['pdf_transfer'] = pdf_stats
            # Audit log dedicato
            try:
                AuditManager.log(user_id=user_id, action='TRANSFER_DXF_TO_ORDER',
                                 entity_type='orders', entity_id=order_id,
                                 detail=f"cleaned={dxf_stats.get('copied_cleaned')} "
                                        f"fallback_original={dxf_stats.get('copied_original_fallback')} "
                                        f"missing={dxf_stats.get('missing')}")
            except Exception:
                pass
        # I file temporanei si cancellano SOLO se tutto quello che dipendeva da
        # loro e' riuscito. Prima venivano rimossi comunque: se il passaggio di
        # un disegno all'ordine falliva, l'originale spariva e in officina non
        # restava niente da tagliare.
        mancanti = 0
        try:
            mancanti = int(dxf_stats.get('missing') or 0) if dxf_stats else 0
            if pdf_stats and pdf_stats.get('error'):
                mancanti += 1
        except (TypeError, ValueError, NameError):
            mancanti = 0
        if mancanti:
            result['cleanup_rimandato'] = True
            result['avviso'] = (
                f"{mancanti} allegati non sono stati trasferiti all'ordine: "
                "i file originali del preventivo sono stati CONSERVATI per poter "
                "riprovare. Controlla i disegni dell'ordine prima di mandarlo in "
                "officina.")
            logger.warning('Accettazione %s: %d allegati non trasferiti, '
                           'sorgenti conservati in preventivi_tmp', preventivo_id, mancanti)
        else:
            _cleanup_preventivo_files(preventivo_id)
        return jsonify(result), 200
    except Exception as e:
        logger.exception('preventivi accetta failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/rifiuta', methods=['POST'])
@richiede(*UFFICI)
def api_preventivi_rifiuta(preventivo_id):
    """Transizione INVIATO → RIFIUTATO."""
    try:
        # Anche l'Ufficio (Elena) può rifiutare: vede la risposta del cliente.
        user_id = _chi()
        result = PreventivoManager.transition_status(preventivo_id, 'RIFIUTATO', user_id=user_id)
        if result is None:
            return jsonify({'success': False, 'error': 'Preventivo non trovato'}), 404
        if isinstance(result, dict) and result.get('error'):
            return jsonify({'success': False, 'error': result['error']}), 409
        try:
            AuditManager.log(user_id=user_id, action='REJECT_PREVENTIVO',
                             entity_type='preventivi', entity_id=preventivo_id, detail='INVIATO->RIFIUTATO')
        except Exception:
            pass
        # Avvisa chi ha creato il preventivo che è stato rifiutato (prima: nessuna
        # notifica → il commerciale non sapeva dell'esito). Evita autonotifica se
        # è lui stesso a rifiutare.
        try:
            creatore = (result or {}).get('created_by')
            _u = UserManager.get_user(creatore) if creatore else None
            if _u and not _u.get('e_postazione'):
                creatore = accesso.STAZIONI['commerciale']['utente']
            if creatore and creatore != _proprietario_avvisi():
                cliente = (result or {}).get('cliente') or ''
                NotificationManager.create_notification(
                    user_id=creatore, order_id=None,
                    title='Preventivo rifiutato',
                    message=f'Il preventivo per {cliente} è stato rifiutato dal cliente.',
                    notification_type='preventivo', notification_category='attiva',
                )
        except Exception as exc:
            logger.warning('notifica rifiuto preventivo fallita: %s', exc)
        _cleanup_preventivo_files(preventivo_id)
        return jsonify({'success': True, 'preventivo': result}), 200
    except Exception as e:
        logger.exception('preventivi rifiuta failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/admin/laser-config', methods=['GET'])
@richiede(*UFFICI, ADMIN)
def api_admin_laser_config_get():
    """Ritorna sezione laser_config dal app_config.json (coefficienti stimatore)."""
    try:
        cfg = ConfigManager.load_config()
        return jsonify({
            'success': True,
            'laser_config': cfg.get('laser_config') or _laser_estimator.DEFAULT_LASER_CONFIG,
        }), 200
    except Exception as e:
        logger.exception('admin laser-config GET failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/admin/laser-config', methods=['PUT'])
@richiede(ADMIN)
def api_admin_laser_config_put():
    """Aggiorna coefficienti stimatore laser (solo admin/capi)."""
    try:
        data = request.get_json(silent=True) or {}
        admin_id = _chi_admin()
        new_config = data.get('laser_config')
        if not isinstance(new_config, dict):
            return jsonify({'success': False, 'error': 'laser_config deve essere un oggetto'}), 400
        saved = ConfigManager.save_config({'laser_config': new_config})
        if 'error' in saved:
            return jsonify({'success': False, 'error': saved['error']}), 500
        try:
            AuditManager.log(user_id=admin_id, action='UPDATE_LASER_CONFIG',
                             entity_type='config', entity_id='laser_config',
                             detail=str(list(new_config.keys())))
        except Exception:
            pass
        return jsonify({'success': True, 'laser_config': saved.get('laser_config')}), 200
    except Exception as e:
        logger.exception('admin laser-config PUT failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/admin/export-orders', methods=['GET'])
@richiede('ufficio', ADMIN)
def api_admin_export_orders():
    """Export CSV ordini per gestionale esterno (cliente non ha Odoo ma userà altro gestionale).

    Query: ?format=csv|json (default: csv), ?from=YYYY-MM-DD, ?to=YYYY-MM-DD
    """
    try:
        admin_id = _chi()
        fmt = (request.args.get('format') or 'csv').lower()
        orders = OrderManager.get_all_orders_dict() or []
        # Filtri data
        date_from = request.args.get('from')
        date_to = request.args.get('to')
        if date_from:
            try:
                df = datetime.strptime(date_from, '%Y-%m-%d')
                orders = [o for o in orders if o.get('data_ricezione') and
                          datetime.fromisoformat(str(o['data_ricezione']).rstrip('Z')[:19]) >= df]
            except Exception:
                pass
        if date_to:
            try:
                dt = datetime.strptime(date_to, '%Y-%m-%d')
                orders = [o for o in orders if o.get('data_ricezione') and
                          datetime.fromisoformat(str(o['data_ricezione']).rstrip('Z')[:19]) <= dt]
            except Exception:
                pass

        if fmt == 'json':
            import json as _json
            import io
            ts = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
            payload = _json.dumps(orders, ensure_ascii=False, indent=2, default=str).encode('utf-8')
            buf = io.BytesIO(payload); buf.seek(0)
            return send_file(buf, mimetype='application/json',
                             as_attachment=True, download_name='orders_' + ts + '.json')
        else:
            import csv as _csv
            import io
            ts = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
            cols = ['id', 'numero_ordine', 'cliente', 'data_ricezione', 'data_consegna',
                    'status', 'origine', 'preventivo_id_origine',
                    'numero_ddt', 'data_ddt', 'numero_fattura', 'data_fattura', 'note']
            out = io.StringIO()
            w = _csv.DictWriter(out, fieldnames=cols, extrasaction='ignore')
            w.writeheader()
            for o in orders:
                w.writerow({c: o.get(c, '') for c in cols})
            data_bytes = out.getvalue().encode('utf-8-sig')  # BOM per Excel italiano
            buf = io.BytesIO(data_bytes); buf.seek(0)
            return send_file(buf, mimetype='text/csv; charset=utf-8',
                             as_attachment=True, download_name='orders_' + ts + '.csv')
    except Exception as e:
        logger.exception('admin export orders failed')
        return jsonify({'success': False, 'error': str(e)}), 500


if __name__ == '__main__':
    # Prima: app.run(debug=True, host='0.0.0.0') = debugger raggiungibile da
    # tutta la rete e senza backup/turni. Si passa sempre da run.py.
    import runpy
    print('Avvio tramite run.py (unico avvio di FerroTrack)')
    runpy.run_path(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'run.py'), run_name='__main__')
