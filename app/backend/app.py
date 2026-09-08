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

logger = logging.getLogger(__name__)

# Importa moduli locali
from . import email_sender as _email_sender
from .models import (initialize_database, Order, OrderFile, get_session,
                     RUOLI_UFFICIO, RUOLI_COMANDO, RUOLI_LASER)
from .database import OrderManager, UserManager, AuditManager, ArchiveManager, FatturazioneManager, NotificationManager, AlertManager, KPIManager, BarcodeManager, PreventivoManager
from .pdf_cartellino import genera_cartellino_pdf
from .events import OrderEventBus
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
# I suoi endpoint usano token di dispositivo verificati dal server.
from .api_ore import bp_ore  # noqa: E402
app.register_blueprint(bp_ore)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50 MB max upload
CORS(app, origins=[r"http://localhost:*", r"http://127\.0\.0\.1:*", r"http://192\.168\.\d+\.\d+:*"])

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
        return jsonify({'error': e.description, 'codice': e.code}), e.code
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

# ============ UTILITÀ AUTORIZZAZIONE ============

def _require_capo(user_id: str) -> bool:
    """Verifica che user_id appartenga a un capo (is_capo=True) O a un Amministratore.
    L'Amministratore fa da FALLBACK: senza questo, azioni come gestione pistole/utenti/
    config sarebbero eseguibili SOLO dai due capi seedati → collo di bottiglia se assenti."""
    if not user_id:
        return False
    user = UserManager.get_user(user_id)
    return bool(user and (user.get('is_capo', False)
                          or user.get('role') in RUOLI_COMANDO))


def _require_role(user_id: str, roles: list) -> bool:
    """Verifica che user_id abbia uno dei ruoli specificati.

    `roles` accetta: ruoli espliciti ('Commerciale', 'Admin', 'Impiegata',
    'Capo Officina', 'Operaio Laser', 'Operaio Officina', 'Amministratore')
    o l'alias virtuale 'CAPO' che include is_capo=True OR role='Amministratore'.

    Usato dagli endpoint preventivi per autorizzare Commerciale, Admin, Capi.
    """
    if not user_id:
        return False
    user = UserManager.get_user(user_id)
    if not user or not user.get('is_active', True):
        return False
    user_role = user.get('role', '')
    for r in roles:
        if r == 'CAPO' and (user.get('is_capo') or user_role in RUOLI_COMANDO):
            return True
        # 'Impiegata' vale come "chi sta in amministrazione", col nome nuovo
        # o con quello vecchio: gli endpoint scritti prima non vanno riscritti.
        if r == 'Impiegata' and user_role in RUOLI_UFFICIO:
            return True
        if r == user_role:
            return True
    return False

# ============ FRONTEND ROUTES ============

@app.route('/')
def index():
    """Serve login page"""
    return send_from_directory(FRONTEND_FOLDER, 'login.html')


@app.route('/favicon.ico')
def favicon():
    """Evita il 500 sui browser che chiedono automaticamente il favicon."""
    from flask import Response
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

@app.route('/api/auth/sessione/<user_id>', methods=['GET'])
def api_sessione_valida(user_id):
    """Dice se una sessione salvata nel browser vale ancora.

    Serve a chi apre una pagina con in memoria un'utenza spenta o sostituita:
    meglio rimandarlo all'ingresso che lasciarlo davanti a una pagina che si
    apre ma rifiuta ogni salvataggio. Non registra un accesso, perche' viene
    chiamata a ogni caricamento e riempirebbe l'archivio di accessi finti.
    """
    utente = UserManager.get_user(user_id)
    if not utente or not utente.get('is_active', True):
        return jsonify({'valida': False,
                        'motivo': 'Questa postazione non e\' piu\' attiva.'}), 200
    return jsonify({'valida': True, 'utente': utente}), 200


@app.route('/api/auth/login', methods=['POST'])
def login():
    """Autentica utente e registra nel log di audit"""
    try:
        data = request.get_json() or {}
        user_id = data.get('user_id')

        if not user_id:
            return jsonify({'success': False, 'error': 'user_id obbligatorio'}), 400

        # Autentica e aggiorna last_login
        user = UserManager.authenticate(user_id)
        if not user:
            return jsonify({'success': False, 'error': 'Utente non trovato'}), 404

        return jsonify({
            'success': True,
            'user_id': user['id'],
            'name': user['name'],
            'role': user['role'],
            'phase': user['phase'],
            'permissions': user['permissions'],
            'machines': user['machines'],
            'is_capo': user.get('is_capo', False),
            'assigned_clients': user.get('assigned_clients', [])
        }), 200

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/auth/logout', methods=['POST'])
def logout():
    """Registra logout nel log di audit"""
    try:
        data = request.get_json() or {}
        user_id = data.get('user_id')

        if user_id:
            user = UserManager.get_user(user_id)
            if user:
                AuditManager.log(
                    user_id=user_id,
                    user_name=user.get('name'),
                    action='LOGOUT',
                    ip_address=request.remote_addr
                )

        return jsonify({'success': True}), 200

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/users', methods=['GET'])
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
def create_user():
    """Crea un nuovo utente"""
    try:
        data = request.get_json()
        created_by = data.get('created_by')
        if not _require_capo(created_by):
            return jsonify({'success': False, 'error': 'Operazione riservata al Capo Officina'}), 403

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
def delete_user(user_id):
    """Disattiva un utente (soft delete)"""
    try:
        data = request.get_json() or {}
        deleted_by = data.get('deleted_by')
        if not _require_capo(deleted_by):
            return jsonify({'success': False, 'error': 'Operazione riservata al Capo Officina'}), 403

        success = UserManager.delete_user(user_id)
        if not success:
            return jsonify({'success': False, 'error': 'User not found'}), 404

        return jsonify({'success': True, 'message': f'User {user_id} deleted'}), 200

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/users/<user_id>', methods=['PUT'])
def update_user(user_id):
    """Modifica un utente esistente"""
    try:
        data = request.get_json()
        updated_by = data.get('updated_by')
        if not _require_capo(updated_by):
            return jsonify({'success': False, 'error': 'Operazione riservata al Capo Officina'}), 403

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

        order = OrderManager.create_order(
            cliente=cliente,
            data_consegna=data_consegna,
            numero_ordine=numero_ordine,
            note=data.get('note', '')
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
def get_order(order_id):
    """Recupera dettagli ordine con stato articoli"""
    try:
        details = OrderManager.get_order_details(order_id)
        if 'error' in details:
            return jsonify(details), 404
        return jsonify(details), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/orders/<order_id>/delivery-date', methods=['PUT'])
def update_delivery_date(order_id):
    """Aggiorna data di consegna di un ordine (drag & drop calendario laser)"""
    try:
        data = request.get_json()
        if not data or 'data_consegna' not in data:
            return jsonify({'success': False, 'error': 'data_consegna obbligatoria'}), 400
        new_date_str = data['data_consegna']
        try:
            new_date = datetime.strptime(new_date_str[:10], '%Y-%m-%d')
        except ValueError:
            return jsonify({'success': False, 'error': 'Formato YYYY-MM-DD richiesto'}), 400
        result = OrderManager.update_delivery_date(order_id, new_date)
        if result.get('success'):
            return jsonify(result), 200
        return jsonify(result), 404
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/orders/<order_id>', methods=['PUT'])
def update_order(order_id):
    """Aggiorna dati ordine: cliente, note, data_consegna, numero_ordine."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'Body JSON richiesto'}), 400
        result = OrderManager.update_order(order_id, data)
        if result.get('success'):
            return jsonify(result), 200
        # 404 solo se l'ordine non c'e': un dato non valido e' 400, altrimenti
        # chi chiama non distingue "non esiste" da "hai sbagliato a scrivere".
        manca = 'non trovato' in (result.get('error') or '').lower()
        return jsonify(result), 404 if manca else 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/orders/<order_id>', methods=['DELETE'])
def delete_order(order_id):
    """Soft delete di un ordine: setta is_deleted=True, non cancella fisicamente i dati"""
    try:
        session = get_session()
        try:
            order = session.query(Order).filter(Order.id == order_id).first()
            if not order:
                return jsonify({'success': False, 'error': 'Ordine non trovato'}), 404
            order.is_deleted = True
            session.commit()
            return jsonify({'success': True, 'message': f'Ordine {order_id} eliminato (recuperabile)'}), 200
        finally:
            session.close()
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/orders/<order_id>/mark-laser-done', methods=['POST'])
def mark_laser_done(order_id):
    """Marca il taglio laser come completato — chiama il LASER (Mirko).
    Da questo momento gli operai officina possono scansionare il cartellino.
    """
    try:
        data = request.get_json(silent=True) or {}
        user_id = (data.get('user_id') or '').strip()
        if not user_id:
            return jsonify({'error': 'user_id obbligatorio'}), 400
        user = UserManager.get_user(user_id)
        if not user:
            return jsonify({'error': 'utente non trovato'}), 403
        # Solo ruolo Laser (o capo) può marcare il taglio completato
        is_laser = user.get('role') in RUOLI_LASER or user.get('is_capo')
        if not is_laser:
            return jsonify({'error': 'Solo operatore Laser o capo può marcare il taglio'}), 403
        result = OrderManager.mark_laser_done(order_id, user_id=user_id)
        return jsonify(result), (200 if result.get('success') else 400)
    except Exception as e:
        logger.exception('mark_laser_done endpoint failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/orders/<order_id>/mark-laser-undone', methods=['POST'])
def mark_laser_undone(order_id):
    """Rollback marcatura taglio completato (errore, va re-tagliato)."""
    try:
        data = request.get_json(silent=True) or {}
        user_id = (data.get('user_id') or '').strip()
        if not user_id:
            return jsonify({'error': 'user_id obbligatorio'}), 400
        user = UserManager.get_user(user_id)
        if not user:
            return jsonify({'error': 'utente non trovato'}), 403
        is_laser = user.get('role') in RUOLI_LASER or user.get('is_capo')
        if not is_laser:
            return jsonify({'error': 'Solo operatore Laser o capo può annullare il taglio'}), 403
        result = OrderManager.mark_laser_undone(order_id, user_id=user_id)
        return jsonify(result), (200 if result.get('success') else 400)
    except Exception as e:
        logger.exception('mark_laser_undone endpoint failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/orders/<order_id>/close', methods=['POST'])
def close_order(order_id):
    """Marca un ordine come 'lavoro finito' → status=DA_FATTURARE.

    Permesso: capi officina E impiegata (Elena fa da backup quando i capi
    si dimenticano o accumulano).
    """
    try:
        data = request.get_json(silent=True) or {}
        user_id = (data.get('user_id') or '').strip()
        if not user_id:
            return jsonify({'error': 'user_id obbligatorio'}), 400
        user = UserManager.get_user(user_id)
        if not user:
            return jsonify({'error': 'utente non trovato'}), 403
        is_allowed = (user.get('is_capo')
                      or user.get('role') in RUOLI_UFFICIO + RUOLI_COMANDO)
        if not is_allowed:
            return jsonify({'error': 'Permesso negato'}), 403
        result = OrderManager.close_order(order_id, user_id=user_id)
        return jsonify(result), (200 if result.get('success') else 400)
    except Exception as e:
        logger.exception('close_order endpoint failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/orders/sospetti-finiti', methods=['GET'])
def api_ordini_sospetti_finiti():
    """Ordini che probabilmente sono finiti ma nessuno li ha chiusi.

    Usato da Elena (sezione dedicata) e dal Pannello Capo (badge rosso).
    Soglie configurabili via /api/admin/config.
    """
    try:
        items = BarcodeManager.get_ordini_sospetti_finiti()
        return jsonify({
            'success': True,
            'count': len(items),
            'orders': items,
            'config': BarcodeManager.load_config(),
        }), 200
    except Exception as e:
        logger.exception('sospetti-finiti endpoint failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/admin/config', methods=['GET'])
def api_admin_config_get():
    """Config app (soglie sospetto, ecc.). Lettura aperta."""
    try:
        return jsonify({'success': True, 'config': BarcodeManager.load_config()}), 200
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/admin/config', methods=['PUT'])
def api_admin_config_update():
    """Modifica config app (solo capi/admin)."""
    try:
        data = request.get_json(silent=True) or {}
        user_id = (data.get('admin_id') or '').strip()
        if not _require_capo(user_id):
            return jsonify({'error': 'Permesso negato'}), 403
        # `user_id` e `admin_id` servono al controllo dei permessi, non sono
        # impostazioni: senza toglierli finirebbero fra quelle "sconosciute".
        updates = {k: v for k, v in data.items()
                   if k not in ('admin_id', 'user_id')}
        new_cfg = BarcodeManager.save_config(updates)
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
def replace_order_pdf(order_id):
    """Sostituisce il PDF di un ordine esistente"""
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
            pdf_filename = f"{order_id}_{os.path.basename(file.filename)}"
            pdf_path = os.path.join(PDFS_FOLDER, pdf_filename)
            file.save(pdf_path)

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
def get_order_pdf(order_id):
    """Serve il PDF dell'ordine inline (per iframe viewer)"""
    try:
        session = get_session()
        try:
            order = session.query(Order).filter(Order.id == order_id).first()
            if not order:
                return jsonify({'error': 'Ordine non trovato'}), 404

            # Cerca il file PDF tra i file allegati
            pdf_file = session.query(OrderFile).filter(
                OrderFile.order_id == order_id,
                OrderFile.file_type == 'PDF'
            ).first()

            if not pdf_file or not os.path.exists(pdf_file.filepath):
                return jsonify({'error': 'PDF non trovato'}), 404

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

@app.route('/api/orders/<order_id>/dxf/<filename>', methods=['GET'])
def get_dxf_file(order_id, filename):
    """Serve DXF file per download"""
    try:
        # Sanitize filename to prevent directory traversal
        filename = os.path.basename(filename)

        # Verify file exists in drawings folder
        dxf_path = os.path.join(DRAWINGS_FOLDER, filename)

        if not os.path.exists(dxf_path):
            return jsonify({'error': 'File non trovato'}), 404

        # Serve file for download with proper headers
        return send_file(
            dxf_path,
            mimetype='application/dxf',
            as_attachment=True,
            download_name=filename.replace('draft_', '')
        )

    except Exception as e:
        logger.error(f"Get DXF file error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/orders', methods=['GET'])
def get_orders():
    """Recupera lista ordini con filtri per il nuovo workflow"""
    try:
        cliente = request.args.get('cliente')
        status = request.args.get('status')
        fase_corrente = request.args.get('fase_corrente')
        operatore = request.args.get('operatore')
        orders_data = OrderManager.get_all_orders_dict(
            cliente=cliente, status=status,
            fase_corrente=fase_corrente, operatore=operatore
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

        return jsonify({'orders': orders_data}), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ============ API MARK ORDER SEEN ============

@app.route('/api/orders/<order_id>/mark-seen', methods=['POST'])
def mark_order_seen(order_id):
    """Marca un ordine come visto dall'operatore"""
    try:
        result = OrderManager.mark_order_seen(order_id)
        return jsonify(result), 200 if result.get('success') else 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ============ API ALERTS ============

@app.route('/api/alerts/check', methods=['GET'])
def check_alerts():
    """Controlla alert automatici (timer lunghi, ordini fermi, scadenze)"""
    try:
        alerts = AlertManager.check_alerts()
        return jsonify({'success': True, 'alerts': alerts, 'count': len(alerts)}), 200
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ============ API KPI DASHBOARD ============

@app.route('/api/kpi/dashboard', methods=['GET'])
def get_kpi_dashboard():
    """KPI operatori e fasi per dashboard Capo Officina"""
    try:
        result = KPIManager.get_dashboard_kpi()
        return jsonify(result), 200 if result.get('success') else 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ============ API ADMIN ============

@app.route('/api/admin/kpi', methods=['GET'])
def get_admin_kpi():
    """Recupera KPI sistema per admin dashboard"""
    try:
        from datetime import datetime as dt, timedelta

        all_orders = OrderManager.get_all_orders_dict()
        active_orders = [o for o in all_orders if o.get('status') not in ('COMPLETATO', 'PARZIALE', 'SPEDITO')]
        ordini_attivi = len(active_orders)

        # KPI operai (calcoli reali, nessun mock)
        kpi_operai = AuditManager.get_kpi_operai()

        # Login oggi
        today = dt.now().date()
        audit_logs = AuditManager.get_recent(limit=1000)
        login_oggi = len([
            log for log in audit_logs
            if log['action'] == 'LOGIN' and dt.fromisoformat(log['timestamp']).date() == today
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
             dt.fromisoformat(op['ultimo_accesso']).date() == today))

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
def get_admin_audit_log():
    """Recupera log di audit per admin dashboard"""
    try:
        requester_id = request.args.get('requester_id')
        if not _require_capo(requester_id):
            return jsonify({'success': False, 'error': 'Operazione riservata al Capo Officina'}), 403

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
def get_ordini_da_fatturare():
    """Recupera ordini in attesa di chiusura amministrativa"""
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
def get_ordini_da_fatturare_count():
    """Conteggio ordini da fatturare (per badge)"""
    try:
        count = FatturazioneManager.get_count()
        return jsonify({'success': True, 'count': count}), 200
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@app.route('/api/orders/<order_id>/salva-bozza-fattura', methods=['PUT'])
def salva_bozza_fattura(order_id):
    """Salva dati DDT/fattura come bozza senza chiudere l'ordine.
    Autorizzato: Impiegata, Capi, Amministratore."""
    try:
        data = request.get_json() or {}
        user_id = (data.get('user_id') or '').strip()
        if not _require_role(user_id, ['Impiegata', 'CAPO']):
            return jsonify({'success': False, 'error': 'Permesso negato'}), 403
        result = FatturazioneManager.salva_bozza(order_id, data)
        if not result['success']:
            return jsonify(result), 400
        return jsonify(result), 200
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@app.route('/api/orders/<order_id>/chiudi-amministrativo', methods=['POST'])
def chiudi_ordine_amministrativo(order_id):
    """Chiude ordine amministrativamente — status → CHIUSO.
    Autorizzato: Impiegata, Capi, Amministratore."""
    try:
        data = request.get_json() or {}
        user_id = data.get('user_id', '')
        if not _require_role(user_id, ['Impiegata', 'CAPO']):
            return jsonify({'success': False, 'error': 'Permesso negato'}), 403
        result = FatturazioneManager.chiudi_ordine(order_id, data, user_id)
        if not result['success']:
            return jsonify(result), 400

        # Log audit
        AuditManager.log(
            user_id=user_id,
            action='CHIUSURA_AMMINISTRATIVA',
            entity_type='order',
            entity_id=order_id,
            detail=f"DDT: {data.get('numero_ddt', '-')}, Fattura: {data.get('numero_fattura', '-')}",
            ip_address=request.remote_addr
        )

        return jsonify(result), 200
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@app.route('/api/orders/<order_id>/riapri', methods=['POST'])
def riapri_ordine(order_id):
    """Riapre ordine CHIUSO riportandolo a DA_FATTURARE.
    Autorizzato: Impiegata, Capi, Amministratore."""
    try:
        data = request.get_json() or {}
        user_id = data.get('user_id', '')
        if not _require_role(user_id, ['Impiegata', 'CAPO']):
            return jsonify({'success': False, 'error': 'Permesso negato'}), 403
        result = FatturazioneManager.riapri_ordine(order_id)
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

@app.route('/api/extract-pdf-data', methods=['POST'])
def extract_pdf_data():
    """Carica un PDF e restituisce il filename salvato (no parsing)"""
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

        return jsonify({
            'success': True,
            'data': {'pdf_filename': pdf_filename}
        }), 200

    except Exception as e:
        logging.error(f"[ERROR] extract_pdf_data: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/upload-drawing', methods=['POST'])
def upload_drawing():
    """Carica un disegno DXF o immagine"""
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'Nessun file'}), 400
        
        file = request.files['file']
        order_id = request.form.get('order_id', 'unknown')

        from werkzeug.utils import secure_filename
        safe_name = secure_filename(file.filename)
        if not safe_name:
            return jsonify({'error': 'Nome file non valido'}), 400
        ALLOWED_EXTENSIONS = {'.dxf', '.dwg', '.png', '.jpg', '.jpeg', '.pdf', '.step', '.stp'}
        ext = os.path.splitext(safe_name)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            return jsonify({'error': f'Tipo file non supportato: {ext}'}), 400
        filename = f"{order_id}_{safe_name}"
        filepath = os.path.join(DRAWINGS_FOLDER, filename)
        file.save(filepath)

        return jsonify({
            'success': True,
            'filename': filename
        }), 200
        
    except Exception as e:
        return jsonify({'error': str(e)}), 400

# ============ HEALTH CHECK ============

@app.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({'status': 'online', 'timestamp': datetime.utcnow().isoformat()}), 200


@app.route('/api/dashboard-live', methods=['GET'])
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
        from .models import get_session, Order, OfficinaScan
        session = get_session()
        try:
            # 1. Operatori attivi ORA (sessioni aperte, non chiuse)
            n_attivi = session.query(OfficinaScan).filter(
                OfficinaScan.timestamp_fine == None  # noqa: E711
            ).count()

            # 2. Ordini in produzione ORA (con scan attive negli ultimi 24h, non chiusi)
            from datetime import timedelta
            ora = datetime.utcnow()
            since = ora - timedelta(hours=24)
            in_produzione_ids = session.query(OfficinaScan.order_id).filter(
                OfficinaScan.timestamp_inizio >= since
            ).distinct()
            in_produzione_ids_set = {r[0] for r in in_produzione_ids}
            n_in_produzione = session.query(Order).filter(
                Order.id.in_(in_produzione_ids_set),
                Order.is_deleted == False,  # noqa: E712
                Order.status.in_(['RICEVUTO'])
            ).count() if in_produzione_ids_set else 0

            # 3. Ordini pronti (taglio completato + non chiusi)
            n_pronti = session.query(Order).filter(
                Order.is_deleted == False,  # noqa: E712
                Order.taglio_completato == True,  # noqa: E712
                Order.status == 'RICEVUTO'
            ).count()

            # 4. Ordini "ricevuti" mai iniziati (per kanban proporzionale)
            n_ricevuti = session.query(Order).filter(
                Order.is_deleted == False,  # noqa: E712
                Order.status == 'RICEVUTO',
                Order.taglio_completato == False,  # noqa: E712
            ).count()
            # Sottraggo quelli già in produzione per non contarli 2 volte
            n_ricevuti_puri = max(0, n_ricevuti - n_in_produzione)

            # 5. Ordini "in lavorazione" per kanban (ricevuti in produzione)
            n_kanban_lavorazione = n_in_produzione

            # 6. Ordini "pronti" per kanban (taglio ok, in attesa chiusura Elena)
            n_kanban_pronti = n_pronti

            # 7. Curva attività ORE giornata (anonimo — solo count sessioni per ora)
            # Dalle 07:00 alle 18:00 dell'oggi corrente
            oggi_00 = datetime(ora.year, ora.month, ora.day)
            curva_ore = []
            for h in range(7, 19):  # 7-18
                slot_start = oggi_00 + timedelta(hours=h)
                slot_end = slot_start + timedelta(hours=1)
                if slot_start > ora:
                    curva_ore.append({'ora': h, 'attivita': 0})
                    continue
                # Conta sessioni che erano attive in quell'ora
                q = session.query(OfficinaScan).filter(
                    OfficinaScan.timestamp_inizio < slot_end,
                ).filter(
                    (OfficinaScan.timestamp_fine == None) |  # noqa: E711
                    (OfficinaScan.timestamp_fine >= slot_start)
                )
                curva_ore.append({'ora': h, 'attivita': q.count()})

            return jsonify({
                'success': True,
                'snapshot': {
                    'operatori_attivi': n_attivi,
                    'ordini_in_produzione': n_in_produzione,
                    'ordini_pronti': n_pronti,
                },
                'kanban': {
                    'ricevuti': n_ricevuti_puri,
                    'lavorazione': n_kanban_lavorazione,
                    'pronti': n_kanban_pronti,
                },
                'curva_ore': curva_ore,
                'timestamp': ora.isoformat(),
            }), 200
        finally:
            session.close()
    except Exception as e:
        logger.exception('dashboard-live failed')
        return jsonify({'success': False, 'error': str(e)}), 500

# ============ BACKUP & EXPORT ============

# Avvia backup scheduler all'import del modulo
try:
    _backup_sys_path = os.path.join(os.path.dirname(__file__), '..')
    if _backup_sys_path not in sys.path:
        sys.path.insert(0, _backup_sys_path)
    from backup_db import backup as _do_backup, integrity_check as _integrity_check
    from backup_db import load_config as _backup_load_config, save_config as _backup_save_config
    from backup_db import list_backups as _backup_list, start_scheduler as _start_backup_scheduler
    _start_backup_scheduler()
except Exception as _e:
    logging.warning(f'[BACKUP] Impossibile avviare scheduler: {_e}')

@app.route('/api/admin/backup', methods=['POST'])
def manual_backup():
    """Esegue un backup manuale del database (solo admin/capo)"""
    try:
        ok = _integrity_check()
        path = _do_backup(motivo='manuale')
        if path:
            return jsonify({'success': True, 'backup_path': os.path.basename(path), 'integrity_ok': ok}), 200
        return jsonify({'success': False, 'error': 'Backup fallito'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/admin/backup/settings', methods=['GET', 'PUT'])
def backup_settings():
    """Leggi o aggiorna impostazioni backup"""
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
            if 'backup_path' in data:
                config['backup_path'] = str(data['backup_path']).strip()
            if 'remote_path' in data:
                config['remote_path'] = str(data['remote_path']).strip()
            _backup_save_config(config)
            return jsonify({'success': True, 'settings': config}), 200
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/admin/backup/list', methods=['GET'])
def backup_list():
    """Lista dei backup esistenti"""
    try:
        backups = _backup_list()
        return jsonify({'success': True, 'backups': backups, 'count': len(backups)}), 200
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/admin/audit', methods=['GET'])
def get_audit_log():
    """Recupera log attività recenti"""
    try:
        limit = min(request.args.get('limit', 100, type=int), 500)
        user_id = request.args.get('user_id')
        logs = AuditManager.get_recent(limit=limit, user_id=user_id)
        return jsonify({'success': True, 'logs': logs}), 200
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/admin/export-json', methods=['GET'])
def export_json():
    """Esporta tutti gli ordini attivi in formato JSON (download)"""
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

@app.route('/api/notifications', methods=['GET', 'POST'])
def handle_notifications():
    """GET: Recupera notifiche | POST: Crea notifica"""
    if request.method == 'GET':
        try:
            user_id = request.args.get('user_id')
            limit = request.args.get('limit', 50, type=int)

            if not user_id:
                return jsonify({'success': False, 'error': 'user_id obbligatorio'}), 400

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
            data = request.get_json() or {}
            sender_id = data.get('sender_id')
            if not sender_id or not UserManager.get_user(sender_id):
                return jsonify({'success': False, 'error': 'sender_id obbligatorio e deve essere un utente valido'}), 403

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
def mark_notification_read(notification_id):
    """Segna una notifica come letta"""
    try:
        success = NotificationManager.mark_as_read(notification_id)
        return jsonify({'success': success}), 200 if success else 404
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/notifications/<notification_id>', methods=['DELETE'])
def delete_notification(notification_id):
    """Cancella una singola notifica (soft delete)"""
    try:
        success = NotificationManager.delete_notification(notification_id)

        if success:
            return jsonify({'success': True, 'message': 'Notifica cancellata'}), 200
        else:
            return jsonify({'success': False, 'error': 'Notifica non trovata'}), 404
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/notifications/clear-all', methods=['DELETE'])
def clear_all_notifications():
    """Cancella tutte le notifiche dell'utente"""
    try:
        user_id = request.args.get('user_id')

        if not user_id:
            return jsonify({'success': False, 'error': 'user_id obbligatorio'}), 400

        success = NotificationManager.delete_all_notifications(user_id)

        if success:
            return jsonify({'success': True, 'message': 'Tutte le notifiche cancellate'}), 200
        else:
            return jsonify({'success': False, 'error': 'Errore durante la cancellazione'}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

# ============================================================================
#  BARCODE / OFFICINA SCAN — endpoint per pistole WiFi e UI dedicate
# ============================================================================

@app.route('/api/scan', methods=['POST'])
def api_scan():
    """Endpoint chiamato dalle pistole WiFi a ogni scansione.

    Body: { "pistola_id": "<id hw configurato>", "codice": "<numero ordine>" }
    Risposta 200 = beep ok sulla pistola; 4xx = beep errore.
    """
    try:
        data = request.get_json(silent=True) or {}
        pistola_id = data.get('pistola_id') or ''
        codice = data.get('codice') or ''
        result = BarcodeManager.process_scan(pistola_id, codice)
        status = result.pop('status_code', 200 if result.get('ok') else 500)
        return jsonify(result), status
    except Exception as e:
        logger.exception('api_scan failed')
        return jsonify({'error': str(e)}), 500


# ============================================================================
#  CICLO AMMINISTRATIVO DEGLI ORDINI — riservato all'ufficio
#  L'operaio comunica a voce che ha finito: nessuno in officina cambia lo stato
#  di un ordine. Queste transizioni sono consentite solo a Impiegata, capi e
#  amministratori, e il controllo e' qui nel backend: nascondere i pulsanti non
#  basta, l'API va protetta anche contro una chiamata diretta.
# ============================================================================

_RUOLI_UFFICIO = ['Impiegata', 'CAPO']


def _utente_ufficio():
    """Ritorna (user_id, None) se autorizzato, altrimenti (None, risposta 403).

    Se la richiesta arriva da un DISPOSITIVO registrato (tablet), comanda lo
    scope del dispositivo e non lo user_id scritto nel corpo: un tablet di
    officina o di reparto viene respinto anche se dichiara di essere
    l'impiegata. Senza dispositivo si ricade sul controllo di ruolo classico,
    usato dai PC dell'ufficio che entrano col login.
    """
    dati = request.get_json(silent=True) or {}
    user_id = (dati.get('user_id') or request.args.get('user_id') or '').strip()

    def negato():
        return None, (jsonify({
            'error': "Solo l'ufficio puo' registrare i passaggi di un ordine.",
            'codice': 'permesso_negato'}), 403)

    try:
        from .auth_device import risolvi_dispositivo
        dispositivo = risolvi_dispositivo()
    except Exception:
        dispositivo = None

    if dispositivo:
        if dispositivo.get('scope') != 'ufficio':
            logger.warning(
                'Transizione ordine rifiutata: dispositivo "%s" (scope=%s) '
                'si dichiarava utente "%s"',
                dispositivo.get('label'), dispositivo.get('scope'), user_id)
            return negato()
        # Dispositivo d'ufficio: identita' verificata dal server. Lo user_id
        # serve solo a registrare CHI ha agito, e deve comunque essere valido.
        if user_id and not _require_role(user_id, _RUOLI_UFFICIO):
            return negato()
        return user_id or ('dispositivo:' + (dispositivo.get('label') or '')), None

    if not _require_role(user_id, _RUOLI_UFFICIO):
        return negato()
    return user_id, None


def _verifica_preventivo(preventivo_id):
    """Esito della verifica, o None se il preventivo non esiste."""
    prev = PreventivoManager.get(preventivo_id)
    if not prev or prev.get('error'):
        return None
    from .preventivi.verifica import verifica as _v
    cfg = (BarcodeManager.load_config() or {}).get('preventivi_config') or {}
    snap = prev.get('snapshot_economico') or None
    if snap:
        cfg = {'costo_generali_pct': snap.get('costo_generali_pct') or 0}
    return _v(prev, cfg)


@app.route('/api/preventivi/<preventivo_id>/verifica', methods=['GET'])
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
        cfg = (BarcodeManager.load_config() or {}).get('preventivi_config') or {}
        # Se l'offerta e' gia' partita valgono le percentuali congelate allora.
        snap = prev.get('snapshot_economico') or None
        if snap:
            cfg = {'costo_generali_pct': snap.get('costo_generali_pct') or 0}
        return jsonify({'success': True, **_verifica(prev, cfg)}), 200
    except Exception as e:
        logger.exception('api_preventivo_verifica failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/totali', methods=['GET'])
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
        cfg = (BarcodeManager.load_config() or {}).get('preventivi_config') or {}
        return jsonify({'success': True, 'totali': calcola(prev, cfg)}), 200
    except Exception as e:
        logger.exception('api_preventivo_totali failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/ordini/viste', methods=['GET'])
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
    user_id, negato = _utente_ufficio()
    if negato:
        return negato
    res = funzione(order_id, user_id, **extra)
    if res.get('error'):
        codice = res.get('codice')
        stato = 404 if codice == 'non_trovato' else (
            500 if codice == 'errore_interno' else 409)
        return jsonify({'success': False, **res}), stato
    return jsonify(res), 200


@app.route('/api/ordini/<order_id>/completamento', methods=['POST'])
def api_ordine_completamento(order_id):
    """L'officina ha comunicato che ha finito: passa alla preparazione del DDT."""
    from .ordini_service import registra_completamento
    return _transizione(registra_completamento, order_id)


@app.route('/api/ordini/<order_id>/completamento', methods=['DELETE'])
def api_ordine_completamento_annulla(order_id):
    from .ordini_service import annulla_completamento
    return _transizione(annulla_completamento, order_id)


@app.route('/api/ordini/<order_id>/ddt', methods=['POST'])
def api_ordine_ddt(order_id):
    """Registra il riferimento del DDT emesso nell'altro sistema."""
    from .ordini_service import registra_ddt
    dati = request.get_json(silent=True) or {}
    return _transizione(registra_ddt, order_id, numero=dati.get('numero'))


@app.route('/api/ordini/<order_id>/consegna', methods=['POST'])
def api_ordine_consegna(order_id):
    """Merce consegnata. `completa=false` lascia un residuo da consegnare."""
    from .ordini_service import registra_consegna
    dati = request.get_json(silent=True) or {}
    return _transizione(registra_consegna, order_id,
                        completa=bool(dati.get('completa', True)),
                        note=dati.get('note') or '')


@app.route('/api/ordini/<order_id>/chiudi', methods=['POST'])
def api_ordine_chiudi(order_id):
    """Ciclo amministrativo concluso: in archivio."""
    from .ordini_service import chiudi_pratica
    return _transizione(chiudi_pratica, order_id)


@app.route('/api/ordini/<order_id>/riapri', methods=['POST'])
def api_ordine_riapri(order_id):
    from .ordini_service import riapri
    return _transizione(riapri, order_id)


# ============================================================================
#  TABLET DI OFFICINA — abilitazione dei dispositivi condivisi
#  Il tablet appeso vicino alla timbratrice non ha login: viene abilitato UNA
#  volta con un token di dispositivo, e da quel momento gli operai toccano solo
#  il proprio nome. Qui l'ufficio puo' crearlo, vederlo e revocarlo senza CLI.
# ============================================================================

# Scope creabili dall'interfaccia. 'ufficio' NO: darebbe i poteri
# dell'impiegata a chiunque sappia chiamare l'endpoint, e questa API e'
# protetta solo dall'id utente inviato dal browser come il resto dell'admin.
# I token d'ufficio restano da riga di comando (app/tools/device_token.py).
_SCOPE_DA_UI = ('ore', 'reparto')


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
def api_dispositivi_list():
    """Elenco dei tablet abilitati (senza segreti) + indirizzo base per il QR."""
    try:
        from .auth_device import elenca_token
        return jsonify({'success': True, 'dispositivi': elenca_token(),
                        'url_base': _url_base_lan()}), 200
    except Exception as e:
        logger.exception('api_dispositivi_list failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/admin/dispositivi', methods=['POST'])
def api_dispositivi_create():
    """Abilita un tablet. Il token in chiaro viene restituito UNA sola volta."""
    try:
        data = request.get_json(silent=True) or {}
        user_id = (data.get('admin_id') or '').strip()
        if not _require_capo(user_id):
            return jsonify({'error': 'Permesso negato'}), 403
        scope = (data.get('scope') or 'ore').strip().lower()
        if scope not in _SCOPE_DA_UI:
            return jsonify({'error': 'Da qui si abilitano solo i tablet di officina '
                                     'e di reparto.'}), 400
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
        base = _url_base_lan()
        pagina = '/ore.html' if scope == 'ore' else '/operaio-info.html'
        res['url'] = f"{base}{pagina}?token={res['token']}"
        return jsonify({'success': True, 'dispositivo': res}), 201
    except Exception as e:
        logger.exception('api_dispositivi_create failed')
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/dispositivi/<token_id>', methods=['DELETE'])
def api_dispositivi_revoke(token_id):
    """Revoca un tablet (es. smarrito). Da quel momento non salva piu' nulla."""
    try:
        user_id = (request.args.get('admin_id') or '').strip()
        if not _require_capo(user_id):
            return jsonify({'error': 'Permesso negato'}), 403
        from .auth_device import revoca_token
        res = revoca_token(token_id, da=user_id)
        if res.get('error'):
            return jsonify(res), 404
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


@app.route('/api/orders/<order_id>/cartellino', methods=['GET'])
def api_cartellino(order_id):
    """Ritorna il PDF A6 col cartellino barcode dell'ordine."""
    try:
        session = get_session()
        try:
            order = session.query(Order).filter(Order.id == order_id).first()
            if not order:
                return jsonify({'error': 'Ordine non trovato'}), 404
            codice = order.numero_ordine or order.id[:8]
            cliente = order.cliente or ''
            data_consegna = order.data_consegna
            note = ''
            if order.lotto_numero and order.lotto_numero > 0:
                note = f'Lotto {order.lotto_numero}'
                if order.lotto_nome:
                    note += f' — {order.lotto_nome}'
        finally:
            session.close()

        pdf_bytes = genera_cartellino_pdf(
            codice=codice,
            cliente=cliente,
            data_consegna=data_consegna,
            note=note,
        )
        import io as _io
        return send_file(
            _io.BytesIO(pdf_bytes),
            mimetype='application/pdf',
            as_attachment=False,
            download_name=f'cartellino_{codice}.pdf',
        )
    except Exception as e:
        logger.exception('api_cartellino failed for %s', order_id)
        return jsonify({'error': str(e)}), 500


@app.route('/api/orders/<order_id>/tempo-officina', methods=['GET'])
def api_tempo_officina(order_id):
    """Ritorna il dettaglio delle sessioni officina per un ordine."""
    try:
        data = BarcodeManager.get_tempo_officina(order_id)
        return jsonify(data), 200
    except Exception as e:
        logger.exception('api_tempo_officina failed')
        return jsonify({'error': str(e)}), 500


@app.route('/api/officina/live-status', methods=['GET'])
def api_officina_live_status():
    """Feed live per la pagina 'Stato officina' dell'impiegata."""
    try:
        return jsonify(BarcodeManager.get_live_status()), 200
    except Exception as e:
        logger.exception('api_officina_live_status failed')
        return jsonify({'error': str(e)}), 500


@app.route('/api/capo/kpi-operai', methods=['GET'])
def api_capo_kpi_operai():
    """KPI ore per operaio (oggi/settimana/mese) — pagina capo officina."""
    try:
        return jsonify(BarcodeManager.get_kpi_operai()), 200
    except Exception as e:
        logger.exception('api_capo_kpi_operai failed')
        return jsonify({'error': str(e)}), 500


@app.route('/api/capo/calendario-ordini', methods=['GET'])
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
            'giorni': BarcodeManager.get_calendario_ordini(year, month),
        }), 200
    except Exception as e:
        logger.exception('api_capo_calendario failed')
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/close-residual', methods=['POST'])
def api_admin_close_residual():
    """Chiude tutte le scan ancora aperte (fine turno).

    Richiede capo/admin: passa user_id nel body per audit.
    """
    try:
        data = request.get_json(silent=True) or {}
        user_id = data.get('user_id') or ''
        if not _require_capo(user_id):
            return jsonify({'error': 'Permesso negato'}), 403
        motivo = data.get('motivo') or 'fine_turno'
        n = BarcodeManager.close_residual_scans(motivo=motivo)
        AuditManager.log(
            user_id=user_id,
            action='CLOSE_RESIDUAL_SCANS',
            entity_type='officina_scans',
            entity_id='*',
            detail=f'Chiuse {n} scan, motivo={motivo}',
        )
        return jsonify({'ok': True, 'chiuse': n}), 200
    except Exception as e:
        logger.exception('api_admin_close_residual failed')
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/pistole', methods=['GET'])
def api_admin_pistole_list():
    """Lista pistole registrate."""
    try:
        return jsonify(BarcodeManager.list_pistole(include_inactive=True)), 200
    except Exception as e:
        logger.exception('api_admin_pistole_list failed')
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/pistole', methods=['POST'])
def api_admin_pistole_create():
    """Registra una nuova pistola."""
    try:
        data = request.get_json(silent=True) or {}
        user_id = data.get('admin_id') or ''
        if not _require_capo(user_id):
            return jsonify({'error': 'Permesso negato'}), 403
        result = BarcodeManager.create_pistola(
            pistola_id=data.get('pistola_id') or '',
            operatore_id=data.get('operatore_id') or '',
            note=data.get('note') or '',
        )
        if result.get('error'):
            return jsonify(result), 400
        AuditManager.log(
            user_id=user_id,
            action='CREATE_PISTOLA',
            entity_type='pistole',
            entity_id=result.get('id'),
            detail=f'pistola_id={data.get("pistola_id")} → {data.get("operatore_id")}',
        )
        return jsonify(result), 201
    except Exception as e:
        logger.exception('api_admin_pistole_create failed')
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/pistole/<pistola_uuid>', methods=['PUT'])
def api_admin_pistole_update(pistola_uuid):
    """Aggiorna pistola (operatore, attiva, note)."""
    try:
        data = request.get_json(silent=True) or {}
        user_id = data.get('admin_id') or ''
        if not _require_capo(user_id):
            return jsonify({'error': 'Permesso negato'}), 403
        result = BarcodeManager.update_pistola(
            pistola_uuid=pistola_uuid,
            operatore_id=data.get('operatore_id'),
            attiva=data.get('attiva'),
            note=data.get('note'),
        )
        if result.get('error'):
            return jsonify(result), 400
        AuditManager.log(
            user_id=user_id,
            action='UPDATE_PISTOLA',
            entity_type='pistole',
            entity_id=pistola_uuid,
            detail=str(data),
        )
        return jsonify(result), 200
    except Exception as e:
        logger.exception('api_admin_pistole_update failed')
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/pistole/<pistola_uuid>', methods=['DELETE'])
def api_admin_pistole_delete(pistola_uuid):
    """Elimina pistola."""
    try:
        admin_id = request.args.get('admin_id') or ''
        if not _require_capo(admin_id):
            return jsonify({'error': 'Permesso negato'}), 403
        result = BarcodeManager.delete_pistola(pistola_uuid)
        if result.get('error'):
            return jsonify(result), 400
        AuditManager.log(
            user_id=admin_id,
            action='DELETE_PISTOLA',
            entity_type='pistole',
            entity_id=pistola_uuid,
            detail='',
        )
        return jsonify(result), 200
    except Exception as e:
        logger.exception('api_admin_pistole_delete failed')
        return jsonify({'error': str(e)}), 500


# ============================================================================
#  PREVENTIVI — API REST (Fase 2 merge preventivatore)
# ============================================================================

# Ruoli autorizzati a write/read sui preventivi (decisione: aperto interni, no operai)
_PREV_WRITE_ROLES = ['Commerciale', 'Amministratore', 'CAPO']
_PREV_READ_ROLES = ['Commerciale', 'Amministratore', 'CAPO', 'Impiegata']


@app.route('/api/preventivi', methods=['GET'])
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
def api_preventivi_create():
    """Crea preventivo (BOZZA). Riservato a Commerciale + Admin."""
    try:
        data = request.get_json() or {}
        created_by = data.get('created_by') or ''
        if not _require_role(created_by, _PREV_WRITE_ROLES):
            return jsonify({'success': False, 'error': 'Permesso negato'}), 403
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
def api_preventivi_get(preventivo_id):
    """Dettaglio preventivo + articoli/assiemi/tubolari/piastre.

    Arricchisce gli articoli con `bbox_w_mm` e `bbox_h_mm` letti dal DXF
    pulito (se presente). Le dimensioni bbox sono più affidabili della
    formula matematica del rettangolo equivalente derivata da area+perim
    perché quest'ultima sballa per pezzi con smussi/curve al bordo.
    """
    try:
        p = PreventivoManager.get(preventivo_id, include_children=True)
        if not p:
            return jsonify({'success': False, 'error': 'Preventivo non trovato'}), 404
        # Enrich articoli con bbox reale del cleaned DXF (best-effort)
        prev_dir = os.path.join(UPLOAD_FOLDER, 'preventivi_tmp', preventivo_id)
        for art in (p.get('articoli') or []):
            cleaned_name = art.get('cleaned_dxf_filename')
            if not cleaned_name:
                continue
            dxf_path = os.path.join(prev_dir, os.path.basename(cleaned_name))
            if not os.path.exists(dxf_path):
                continue
            try:
                import ezdxf as _ez
                d = _ez.readfile(dxf_path)
                xs, ys = [], []
                for e in d.modelspace():
                    et = e.dxftype()
                    if et == 'LINE':
                        s, ee = e.dxf.start, e.dxf.end
                        xs += [s[0], ee[0]]; ys += [s[1], ee[1]]
                    elif et in ('CIRCLE', 'ARC'):
                        c = e.dxf.center
                        r = float(getattr(e.dxf, 'radius', 0) or 0)
                        xs += [c[0] - r, c[0] + r]; ys += [c[1] - r, c[1] + r]
                    elif et == 'LWPOLYLINE':
                        for pt in e.get_points('xy'):
                            xs.append(pt[0]); ys.append(pt[1])
                    elif et == 'POLYLINE':
                        for v in e.vertices:
                            xs.append(v.dxf.location.x); ys.append(v.dxf.location.y)
                if xs and ys:
                    art['bbox_w_mm'] = round(max(xs) - min(xs), 2)
                    art['bbox_h_mm'] = round(max(ys) - min(ys), 2)
            except Exception:
                pass  # non fatale — fallback formula lato client
        return jsonify({'success': True, 'preventivo': p}), 200
    except Exception as e:
        logger.exception('preventivo get failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>', methods=['PUT'])
def api_preventivi_update(preventivo_id):
    """Modifica preventivo. Bloccato se status INVIATO/ACCETTATO (immutabili)."""
    try:
        data = request.get_json() or {}
        updated_by = data.pop('updated_by', '')
        if not _require_role(updated_by, _PREV_WRITE_ROLES):
            return jsonify({'success': False, 'error': 'Permesso negato'}), 403
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
def api_preventivi_duplicate(preventivo_id):
    """Duplica un preventivo esistente in un nuovo BOZZA.

    Body opzionale: {created_by, cliente (default = cliente sorgente),
                     copy_articoli (default True)}
    Utile per commesse ricorrenti dello stesso cliente o come template.
    Copia anche i file DXF nella cartella del nuovo preventivo.
    """
    try:
        data = request.get_json(silent=True) or {}
        created_by = data.get('created_by') or ''
        if not _require_role(created_by, _PREV_WRITE_ROLES):
            return jsonify({'success': False, 'error': 'Permesso negato'}), 403
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
            src_dir = os.path.join(UPLOAD_FOLDER, 'preventivi_tmp', preventivo_id)
            dst_dir = os.path.join(UPLOAD_FOLDER, 'preventivi_tmp', result['id'])
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
def api_preventivi_delete(preventivo_id):
    """Soft delete (is_deleted=True). Riservato a Commerciale + Admin."""
    try:
        deleted_by = request.args.get('deleted_by') or ''
        if not _require_role(deleted_by, _PREV_WRITE_ROLES):
            return jsonify({'success': False, 'error': 'Permesso negato'}), 403
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
def api_preventivi_import_xlsx(preventivo_id):
    """Upload XLSX Lantek → estrae articoli e li ritorna (NON li salva ancora).
    La UI mostra l'anteprima; il save effettivo avviene quando l'utente conferma.
    """
    try:
        admin_id = request.form.get('admin_id') or request.args.get('admin_id') or ''
        if not _require_role(admin_id, _PREV_WRITE_ROLES):
            return jsonify({'success': False, 'error': 'Permesso negato'}), 403
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
def api_preventivi_import_dxf(preventivo_id):
    """Upload DXF → estrae lavorazioni (pieghe/saldature) + geometria (area/perimetro).
    Il file viene scartato dopo l'estrazione (decisione: no storage DXF).
    """
    try:
        admin_id = request.form.get('admin_id') or request.args.get('admin_id') or ''
        if not _require_role(admin_id, _PREV_WRITE_ROLES):
            return jsonify({'success': False, 'error': 'Permesso negato'}), 403
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'File disegno obbligatorio'}), 400
        f = request.files['file']
        fname_lower = (f.filename or '').lower()
        if not (fname_lower.endswith('.dxf') or fname_lower.endswith('.dwg')):
            return jsonify({'success': False, 'error': 'File deve essere .dxf o .dwg'}), 400
        # Salva DXF (o DXF convertito da DWG) in uploads/preventivi_tmp/<id>/
        # per consentire la preview interattiva. Sarà cancellato all'accettazione/rifiuto/delete.
        prev_dir = os.path.join(UPLOAD_FOLDER, 'preventivi_tmp', preventivo_id)
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
        # ---- CACHE HIT? Se il DXF è già stato parsato prima (stesso hash) restituisco
        # subito il payload cachato — utile per commesse ripetute e batch grandi.
        from .preventivi import dxf_cache as _dxf_cache
        try:
            file_hash = _dxf_cache.hash_file(tmp_path)
            cached = _dxf_cache.get(file_hash)
        except Exception as _cache_err:
            logger.warning('cache lookup fallito: %s', _cache_err)
            file_hash = None
            cached = None
        if cached and cached.get('payload'):
            payload = cached['payload']
            # Aggiorna il filename salvato con quello attuale (potrebbe differire
            # dal precedente upload dello stesso hash)
            payload['filename'] = saved_filename
            payload['_cache_hit'] = True
            return jsonify(payload), 200
        try:
            # Config minimo per dxf_scanner (colori standard Lantek)
            # Config rilevamento da app_config.json (sezione dxf_detection) — valori calibrati
            # sul config Preventivatore desktop (ratio_min=1.8, filtra_zona=True, ecc.)
            app_cfg = BarcodeManager.load_config()
            dxf_cfg = app_cfg.get('dxf_detection') or {
                'dxf_colori_piega': [2], 'dxf_colori_saldatura': [1],
                'dxf_lunghezza_minima': 15.0, 'dxf_tolleranza_centro': 1.0,
                'dxf_svasatura_ratio_min': 1.8, 'dxf_svasatura_ratio_max': 3.0,
                'dxf_semicerchio_angolo_min': 150.0, 'dxf_semicerchio_angolo_max': 320.0,
                'dxf_filtra_zona_sviluppata': True,
            }
            pieghe, sald_ml, fil, svas = _dxf_scanner.scansiona_dxf_dettagli(tmp_path, dxf_cfg)
            # v3 detector (Shapely) — fornisce anche confidence + candidati per UI manuale
            try:
                from .preventivi.dxf_polygon_detector_v3 import detect_pezzo_geometry_v3
                geo = detect_pezzo_geometry_v3(tmp_path, dxf_cfg)
                # Se v3 non riesce, fallback a v2 legacy
                if not geo or geo.get('area_dm2', 0) == 0:
                    geo = _dxf_scanner.estrai_geometria_taglio(tmp_path, dxf_cfg)
            except Exception as _v3err:
                logger.warning('detector v3 fallito, fallback v2: %s', _v3err)
                geo = _dxf_scanner.estrai_geometria_taglio(tmp_path, dxf_cfg)
            cartiglio = _dxf_scanner.estrai_materiale_da_cartiglio(tmp_path)
            # Stima spessore: peso da cartiglio + area detector + materiale.
            # Alta affidabilità quando l'area è del pezzo vero (post trova-pezzo)
            # e materiale è stato riconosciuto. Se auto-detect ha bassa confidenza
            # sull'area, lo spessore ritornato va marcato come incerto in UI.
            mat_for_calc = cartiglio.get('materiale') if cartiglio.get('confidence', 0) >= 0.5 else None
            spessore = _dxf_scanner.estrai_spessore_da_cartiglio(
                tmp_path,
                area_dm2=(geo or {}).get('area_dm2'),
                materiale=mat_for_calc,
            )
            # Se l'area del detector è inaffidabile, abbatto la confidenza
            # dello spessore (dipende dall'area).
            if geo and geo.get('needs_manual_select'):
                spessore = {**spessore, 'confidence': min(spessore.get('confidence', 0), 0.4)}
            # FALLBACK cartiglio-descrizione: parsing testuale del cartiglio.
            # Esempio: 38APA253 ha 'Lama di contenimento 45x12 sp.3' → area 5.4 dm²
            # + spessore 3.0. Chiamato sempre (è solo text parsing, veloce), poi
            # applicato selettivamente:
            #  - area/perimetro/n_forature: solo se detector geometrico è debole
            #    (confidence < 0.5 o area quasi nulla)
            #  - spessore: se il parsing spessore diretto è null MA il cartiglio
            #    contiene "sp.X" (indipendente dallo stato area — es. 20PA00690
            #    ha detector OK ma cartiglio con "Sp.3" sfugge al parser diretto)
            dim_info = _dxf_scanner.estrai_dimensioni_da_descrizione_cartiglio(tmp_path)
            geo_weak = geo and (geo.get('confidence', 0) < 0.5 or geo.get('area_dm2', 0) < 0.01)
            if dim_info and dim_info.get('area_dm2') and geo_weak:
                logger.info('cartiglio fallback area attivato per %s: %s',
                            saved_filename, dim_info.get('raw_text'))
                geo = {
                    **(geo or {}),
                    'area_dm2': dim_info['area_dm2'],
                    'perimetro_taglio_m': dim_info['perimetro_taglio_m'],
                    'n_forature': max(
                        dim_info.get('n_forature', 0),
                        (geo or {}).get('n_forature', 0),
                    ),
                    'confidence': dim_info['confidence'],
                    'confidence_label': 'media (cartiglio)',
                    'needs_manual_select': False,
                    '_source': 'cartiglio_descrizione',
                    '_raw_text': dim_info['raw_text'],
                    '_dim_x_mm': dim_info['dim_x_mm'],
                    '_dim_y_mm': dim_info['dim_y_mm'],
                }
            # Cartiglio-descrizione (fonte esplicita "sp.3" letta dal disegno)
            # prevale sulla stima peso_area (indiretta) quando confidence maggiore.
            # 20PA00690: peso_area 1.2mm (conf 0.4) vs cartiglio sp.3 (conf 0.85).
            if dim_info and dim_info.get('spessore_mm'):
                dim_conf = dim_info.get('confidence', 0) or 0
                curr_sp = spessore.get('spessore_mm')
                curr_conf = spessore.get('confidence', 0) or 0
                if not curr_sp or dim_conf > curr_conf:
                    logger.info('cartiglio fallback spessore per %s: %.1fmm (conf %.2f) sostituisce %s (conf %.2f)',
                                saved_filename, dim_info['spessore_mm'], dim_conf,
                                curr_sp, curr_conf)
                    spessore = {
                        'spessore_mm': dim_info['spessore_mm'],
                        'confidence': dim_conf,
                        'source': 'cartiglio_descrizione',
                        'warnings': [],
                        'details': {'raw': dim_info['raw_text']},
                    }
            # Auto-cleanup DXF: se il detector ha alta confidence, salva un DXF
            # "pulito" (solo pezzo + fori interni) accanto all'originale.
            # Il commerciale verifica poi nella griglia review post-import.
            cleaned_info = {'cleaned_dxf_filename': None, 'cleaned_status': None,
                            'cleanup_reason': None, 'cleanup_stats': None}
            try:
                from .preventivi import dxf_cleanup
                proceed, reason = dxf_cleanup.should_cleanup(geo)
                cleaned_info['cleanup_reason'] = reason
                if proceed:
                    bbox = dxf_cleanup.get_pezzo_bbox(geo)
                    if bbox:
                        base_p, ext_p = os.path.splitext(tmp_path)
                        cleaned_path = base_p + '_cleaned' + ext_p
                        r_c = dxf_cleanup.save_cleaned_dxf(tmp_path, cleaned_path, bbox)
                        if r_c.get('success'):
                            cleaned_info['cleaned_dxf_filename'] = os.path.basename(cleaned_path)
                            conf = float((geo or {}).get('confidence', 0) or 0)
                            cleaned_info['cleaned_status'] = 'auto' if conf >= 0.7 else 'auto_review'
                            cleaned_info['cleanup_stats'] = {
                                'entities_copied': r_c['entities_copied'],
                                'entities_source': r_c['entities_source'],
                                'tolerance_mm': r_c['tolerance_mm'],
                                'warnings': r_c.get('warnings') or [],
                            }
                            logger.info('%s cleanup auto: %d/%d entità (%s)',
                                        saved_filename, r_c['entities_copied'],
                                        r_c['entities_source'], cleaned_info['cleaned_status'])
                        else:
                            logger.info('%s cleanup fallito: %s', saved_filename, r_c.get('error'))
            except Exception as ce:
                logger.warning('%s cleanup pipeline error: %s', saved_filename, ce)
            # NOTA: tmp_path resta su disco (in uploads/preventivi_tmp/<preventivo_id>/<filename>.dxf)
            # per consentire la preview successiva. Cleanup quando preventivo viene
            # accettato/rifiutato/eliminato.
        except Exception as e:
            try: os.remove(tmp_path)
            except OSError: pass
            raise e
        payload = {
            'success': True,
            'filename': saved_filename,
            'lavorazioni': {
                'pieghe': pieghe, 'saldatura_ml': sald_ml,
                'filettatura_pz': fil, 'svasatura_pz': svas,
            },
            'geometria': geo,
            'cartiglio': cartiglio,  # {materiale, materiale_raw, confidence}
            'spessore': spessore,    # {spessore_mm, confidence, source, details}
            'cleanup': cleaned_info, # {cleaned_dxf_filename, cleaned_status, cleanup_reason, cleanup_stats}
        }
        # Salva in cache per hit successivi (best-effort, non blocca la response)
        if file_hash:
            try:
                _dxf_cache.put(file_hash, saved_filename, payload)
            except Exception as _put_err:
                logger.warning('cache put fallito: %s', _put_err)
        return jsonify(payload), 200
    except Exception as e:
        logger.exception('preventivi import dxf failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/rfq-diagnostic', methods=['GET'])
def api_preventivi_rfq_diagnostic():
    """Diagnostica rapida AI RFQ importer.

    Ritorna:
      - api_key_present: bool (GEMINI_API_KEY caricata?)
      - api_key_prefix: primi 5 char (per verifica visiva)
      - genai_installed: bool
      - test_call_ok: bool (ha risposto Gemini a un ping test?)
      - test_error: str (motivo se test_call_ok=False)

    Da aprire nel browser: http://localhost:5000/api/preventivi/rfq-diagnostic
    """
    diag = {
        'api_key_present': False,
        'api_key_prefix': '',
        'genai_installed': False,
        'test_call_ok': False,
        'test_error': '',
    }
    api_key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')
    if api_key:
        diag['api_key_present'] = True
        diag['api_key_prefix'] = (api_key[:5] + '…') if len(api_key) > 5 else api_key
    else:
        diag['test_error'] = 'GEMINI_API_KEY non trovata in os.environ. Verifica app/.env + RIAVVIA backend.'
        return jsonify(diag), 200
    try:
        import google.generativeai as genai
        diag['genai_installed'] = True
    except ImportError as e:
        diag['test_error'] = f'google-generativeai non installato: {e}'
        return jsonify(diag), 200
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-flash-latest')
        r = model.generate_content(
            'Rispondi solo con la parola "OK".',
            generation_config={'temperature': 0.0},
        )
        txt = (r.text or '').strip()
        diag['test_call_ok'] = True
        diag['test_response'] = txt[:50]
    except Exception as e:
        diag['test_error'] = f'{type(e).__name__}: {e}'
    return jsonify(diag), 200


@app.route('/api/preventivi/import-rfq-package', methods=['POST'])
def api_preventivi_import_rfq_package():
    """AI RFQ Importer: da ZIP (PDF ordine + cartella DXF) → preventivo BOZZA pronto.

    Workflow:
    1. Upload ZIP multipart (con PDF + DXFs)
    2. Gemini parsa il PDF → header + tabella articoli
    3. Fuzzy match articoli PDF ↔ file DXF (per codice)
    4. Crea preventivo BOZZA + scrive DXF su disco + processa ognuno
       (detector v3 + scanner dettagli + cache)
    5. Response: preventivo_id, articoli, warnings

    Body multipart/form-data:
        zip: file .zip contenente PDF ordine + N file .dxf
        admin_id: id utente

    Response 200: {success:True, preventivo_id, cliente, n_articoli, warnings, articoli:[...]}
    Response 400/403/500: {success:False, error}
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from .preventivi.dxf_batch_worker import process_single_dxf
    from .preventivi import rfq_importer
    try:
        admin_id = request.form.get('admin_id') or ''
        # Anche l'Impiegata (Elena) può caricare una richiesta: crea la BOZZA
        # "da prezzare" e la gira al commerciale. Non prezza né tocca il CAD.
        if not _require_role(admin_id, _PREV_WRITE_ROLES + ['Impiegata']):
            return jsonify({'success': False, 'error': 'Permesso negato'}), 403
        _caller = UserManager.get_user(admin_id) or {}
        # Una richiesta che arriva dall'amministrazione, col nome nuovo o
        # con quello vecchio: senza questo, la bozza non risulta "da prezzare"
        # e il commerciale non sa che c'e' qualcosa da fare.
        is_intake_elena = (_caller.get('role') in RUOLI_UFFICIO)

        # Accetta sia uno ZIP (campo "zip", flusso commerciale) sia file SCIOLTI
        # (campo "files": PDF della richiesta + DXF). I file sciolti vengono
        # impacchettati in uno ZIP in memoria e passati alla stessa pipeline.
        f = request.files.get('zip')
        loose = [x for x in request.files.getlist('files') if x and x.filename]
        if f and f.filename:
            zip_bytes = f.read()
        elif len(loose) == 1 and loose[0].filename.lower().endswith('.zip'):
            zip_bytes = loose[0].read()
        elif loose:
            import io as _io
            import zipfile as _zipfile
            buf = _io.BytesIO()
            with _zipfile.ZipFile(buf, 'w', _zipfile.ZIP_DEFLATED) as zf:
                for uf in loose:
                    data = uf.read()
                    if data:
                        zf.writestr(os.path.basename(uf.filename), data)
            zip_bytes = buf.getvalue()
        else:
            return jsonify({'success': False, 'error': 'Nessun file: carica il PDF della richiesta e i DXF (oppure uno ZIP)'}), 400
        f = (f if (f and f.filename) else (loose[0] if loose else None))  # nome per la nota "importato da …"
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

        # 2. Crea preventivo BOZZA
        data_consegna_dt = None
        if result.data_consegna:
            try:
                data_consegna_dt = datetime.strptime(result.data_consegna[:10], '%Y-%m-%d')
            except (ValueError, TypeError):
                pass
        new_prev = PreventivoManager.create(
            cliente=result.cliente,
            created_by=admin_id,
            quantita=1,
            numero_ordine_cliente=result.numero_ordine_cliente or None,
            margine_pct=25.0,
            data_consegna_proposta=data_consegna_dt,
            note=result.note or f'Importato via AI RFQ da {f.filename}',
            da_prezzare=is_intake_elena,
        )
        if not new_prev or 'id' not in new_prev:
            return jsonify({'success': False, 'error': 'Creazione preventivo fallita'}), 500
        preventivo_id = new_prev['id']

        # 3. Scrivi DXF su disco (solo quelli matchati)
        prev_dir = os.path.join(UPLOAD_FOLDER, 'preventivi_tmp', preventivo_id)
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
            except Exception:
                logger.warning('salvataggio PDF ordine RFQ fallito: %s', rfq_pdf_filename)

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
        app_cfg = BarcodeManager.load_config()
        dxf_cfg = app_cfg.get('dxf_detection') or {
            'dxf_colori_piega': [2], 'dxf_colori_saldatura': [1],
            'dxf_lunghezza_minima': 15.0, 'dxf_tolleranza_centro': 1.0,
            'dxf_svasatura_ratio_min': 1.8, 'dxf_svasatura_ratio_max': 3.0,
            'dxf_semicerchio_angolo_min': 150.0, 'dxf_semicerchio_angolo_max': 320.0,
            'dxf_filtra_zona_sviluppata': True,
        }
        dxf_results: dict[str, dict] = {}
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
                        dxf_results[fname] = fut.result()
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
            geom = (dxf_info or {}).get('geometry') or {}
            item = {
                'codice': a.codice,
                'quantita': a.quantita,
                'codice_assieme': getattr(a, 'codice_assieme', None),  # da sottocartella ZIP
                'materiale': a.materiale,          # dal PDF (o None)
                'spessore_mm': a.spessore_mm,      # dal PDF (o None)
                'area_dm2': geom.get('area_dm2', 0),
                'perimetro_taglio_m': geom.get('perimetro_taglio_m', 0),
                'n_forature': geom.get('n_pierce', 0),
                'dxf_filename': a.matched_dxf,
                'pieghe': (dxf_info or {}).get('pieghe', 0),
                'saldatura_ml': (dxf_info or {}).get('saldatura_ml', 0),
                'filettatura_pz': (dxf_info or {}).get('filettatura_pz', 0),
                'svasatura_pz': (dxf_info or {}).get('svasatura_pz', 0),
            }
            # Se il PDF NON aveva materiale/spessore, prova a leggerli dal cartiglio DXF
            if not item['materiale'] and dxf_info:
                cart_mat = (dxf_info.get('cartiglio_materiale') or {}).get('materiale')
                if cart_mat:
                    item['materiale'] = cart_mat
            if not item['spessore_mm'] and dxf_info:
                cart_sp = (dxf_info.get('spessore') or {}).get('spessore_mm')
                if cart_sp:
                    item['spessore_mm'] = float(cart_sp)
            # Arrotonda lo spessore agli spessori realmente tagliati (1-1.5-2-3-4-…)
            if item.get('spessore_mm'):
                from .preventivi.pick_part import _snap_stock
                item['spessore_mm'] = _snap_stock(float(item['spessore_mm']))
            articoli_db.append(item)

        if n_master_saltati:
            result.warnings.append(
                f'{n_master_saltati} disegno/i assieme (master) non prezzati come pezzo — '
                f'l\'assieme costa come somma componenti + montaggio')

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
                n_comp = sum(1 for it in articoli_db if it.get('codice_assieme') == cod)
                assiemi_db.append({'codice_assieme': cod, 'qty': 1,
                                   'componenti_qty': {}, 'ore_montaggio': 0, 'costo': 0})
            try:
                PreventivoManager.replace_assiemi(preventivo_id, assiemi_db)
            except Exception as ae:
                logger.warning('replace_assiemi fallito: %s', ae)

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


@app.route('/api/preventivi/<preventivo_id>/import-dxf-batch', methods=['POST'])
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
        admin_id = request.form.get('admin_id') or request.args.get('admin_id') or ''
        if not _require_role(admin_id, _PREV_WRITE_ROLES):
            return jsonify({'success': False, 'error': 'Permesso negato'}), 403
        files = request.files.getlist('files')
        if not files:
            return jsonify({'success': False, 'error': 'Nessun file inviato'}), 400
        # Percorsi relativi (webkitRelativePath) paralleli ai file, se caricata
        # una CARTELLA già estratta → riconoscimento assiemi dalle sottocartelle.
        rel_paths = request.form.getlist('paths')
        from .preventivi.rfq_importer import assiemi_from_paths
        assieme_by_base = assiemi_from_paths(rel_paths) if rel_paths else {}
        # Salva tutti i file su disco (solo DXF; per DWG serve conversione singola)
        prev_dir = os.path.join(UPLOAD_FOLDER, 'preventivi_tmp', preventivo_id)
        os.makedirs(prev_dir, exist_ok=True)
        saved_tasks = []  # (dxf_path, filename)
        skipped = []
        for f in files:
            fname_lower = (f.filename or '').lower()
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
            return jsonify({'success': True, 'results': skipped}), 200
        # Config DXF (una volta per tutti)
        app_cfg = BarcodeManager.load_config()
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
                    results.append(r)
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
        successful_paths = [(p, f) for (p, f) in saved_tasks if f not in failed_fnames]
        if successful_paths:
            threading.Thread(
                target=_prewarm_dxf_cache,
                args=(successful_paths,),
                daemon=True,
                name=f'dxf-prewarm-{preventivo_id[:8]}',
            ).start()
        assiemi_rilevati = sorted({v for v in assieme_by_base.values() if v})
        return jsonify({'success': True, 'results': results,
                        'assiemi': assiemi_rilevati}), 200
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
    detection_cfg = (BarcodeManager.load_config() or {}).get('dxf_detection', {})
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
def api_preventivi_step_files_list(preventivo_id):
    """Elenca i file STEP (.step/.stp) caricati per il preventivo (in preventivi_tmp/<id>/)."""
    try:
        prev_dir = os.path.join(UPLOAD_FOLDER, 'preventivi_tmp', preventivo_id)
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


@app.route('/api/preventivi/<preventivo_id>/step/<path:filename>', methods=['GET'])
def api_preventivi_step_file(preventivo_id, filename):
    """Serve il file STEP raw (per viewer 3D preview-step.html che lo scarica via fetch)."""
    try:
        safe_name = os.path.basename(filename)
        prev_dir = os.path.join(UPLOAD_FOLDER, 'preventivi_tmp', preventivo_id)
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
def api_preventivi_dxf_svg(preventivo_id, filename):
    """Ritorna SVG ad alta fedeltà del DXF (caricato in import-dxf).

    Usato dalla preview interattiva preview-dxf.html (pan/zoom + lavorazioni)
    E dai thumbnail SVG nella tabella articoli. Cachato in memoria + header
    HTTP per far cachare anche al browser.
    """
    try:
        safe_name = os.path.basename(filename)
        prev_dir = os.path.join(UPLOAD_FOLDER, 'preventivi_tmp', preventivo_id)
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


@app.route('/api/preventivi/<preventivo_id>/dxf/<path:filename>/candidates', methods=['GET'])
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
        prev_dir = os.path.join(UPLOAD_FOLDER, 'preventivi_tmp', preventivo_id)
        dxf_path = os.path.join(prev_dir, safe_name)
        if not os.path.exists(dxf_path):
            return jsonify({'success': False, 'error': 'File DXF non trovato'}), 404
        from .preventivi.dxf_polygon_detector_v3 import detect_pezzo_geometry_v3
        app_cfg = BarcodeManager.load_config() or {}
        detection_cfg = app_cfg.get('dxf_detection', {})
        r = detect_pezzo_geometry_v3(dxf_path, detection_cfg)
        return jsonify({'success': True, **r}), 200
    except Exception as e:
        logger.exception('dxf_candidates failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/dxf/<path:filename>/spessore', methods=['POST'])
def api_preventivi_dxf_spessore(preventivo_id, filename):
    """Ricalcola lo spessore lamiera per un DXF dato area (dal detector/manuale)
    e materiale (che l'utente potrebbe aver cambiato dopo l'import).

    Body: {area_dm2: float, materiale: str}
    Response: {success, spessore: {spessore_mm, confidence, source, details}}
    """
    try:
        safe_name = os.path.basename(filename)
        prev_dir = os.path.join(UPLOAD_FOLDER, 'preventivi_tmp', preventivo_id)
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
        prev_dir = os.path.join(UPLOAD_FOLDER, 'preventivi_tmp', preventivo_id)
        dxf_path = os.path.join(prev_dir, safe_name)
        if not os.path.exists(dxf_path):
            return jsonify({'success': False, 'error': 'File DXF non trovato'}), 404
        from .preventivi.dxf_polygon_detector_v3 import compute_geometry_from_point
        app_cfg = BarcodeManager.load_config() or {}
        detection_cfg = app_cfg.get('dxf_detection', {})
        r = compute_geometry_from_point(dxf_path, x, y, detection_cfg)
        return jsonify({'success': True, **r}), 200
    except Exception as e:
        logger.exception('dxf_select_point failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/dxf/<path:filename>/generate-canonical', methods=['POST'])
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
        prev_dir = os.path.join(UPLOAD_FOLDER, 'preventivi_tmp', preventivo_id)
        os.makedirs(prev_dir, exist_ok=True)
        out_name = f'{safe_codice}_canonico.dxf'
        out_path = os.path.join(prev_dir, out_name)
        from .preventivi.pick_part import genera_dxf_canonico
        r = genera_dxf_canonico(outer, holes, out_path)
        if not r.get('success'):
            return jsonify({'success': False, 'error': r.get('error', 'generazione fallita')}), 500
        return jsonify({'success': True, 'filename': out_name, 'sha256': r['sha256']}), 200
    except Exception as e:
        logger.exception('generate-canonical failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/warm-cad', methods=['POST'])
def api_preventivi_warm_cad(preventivo_id):
    """Pre-scalda in background le cache SVG + geometria per TUTTI i DXF del
    preventivo, così l'apertura del CAD (e i thumbnail) è istantanea. Ritorna
    subito: il warming gira in un thread. Idempotente (cache hit = no-op)."""
    try:
        prev_dir = os.path.join(UPLOAD_FOLDER, 'preventivi_tmp', preventivo_id)
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
def api_preventivi_dxf_geometry_json(preventivo_id, filename):
    """CAD interno — geometria del DXF come polilinee in mm, per il viewer.

    Response: {extents:[minx,miny,maxx,maxy], polylines:[{pts:[[x,y]..], kind}]}
    kind = 'geo' (contorno, cliccabile) | 'annot' (cartiglio/quote, grigio).
    """
    try:
        safe_name = os.path.basename(filename)
        prev_dir = os.path.join(UPLOAD_FOLDER, 'preventivi_tmp', preventivo_id)
        dxf_path = os.path.join(prev_dir, safe_name)
        if not os.path.exists(dxf_path):
            return jsonify({'error': 'File DXF non trovato'}), 404
        app_cfg = BarcodeManager.load_config() or {}
        payload = _get_dxf_geometry_cached(dxf_path, app_cfg.get('dxf_detection', {}))
        from flask import Response
        resp = Response(payload, mimetype='application/json')
        resp.headers['Cache-Control'] = 'private, max-age=3600'
        return resp
    except Exception as e:
        logger.exception('dxf_geometry_json failed')
        return jsonify({'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/dxf/<path:filename>/follow-contour', methods=['POST'])
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
        prev_dir = os.path.join(UPLOAD_FOLDER, 'preventivi_tmp', preventivo_id)
        dxf_path = os.path.join(prev_dir, safe_name)
        if not os.path.exists(dxf_path):
            return jsonify({'success': False, 'error': 'File DXF non trovato'}), 404
        from .preventivi.pick_part import follow_contour_from_click
        app_cfg = BarcodeManager.load_config() or {}
        detection_cfg = app_cfg.get('dxf_detection', {})
        r = follow_contour_from_click(dxf_path, x, y, detection_cfg)
        status = 200 if r.get('success') else 200  # success:False non è errore HTTP
        return jsonify(r), status
    except Exception as e:
        logger.exception('dxf_follow_contour failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/dxf/<path:filename>/pick-candidates', methods=['POST'])
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
        dxf_path = os.path.join(UPLOAD_FOLDER, 'preventivi_tmp', preventivo_id, safe_name)
        if not os.path.exists(dxf_path):
            return jsonify({'success': False, 'error': 'File DXF non trovato'}), 404
        from .preventivi.pick_part import pick_candidates
        app_cfg = BarcodeManager.load_config() or {}
        r = pick_candidates(dxf_path, x, y, app_cfg.get('dxf_detection', {}))
        return jsonify(r), 200
    except Exception as e:
        logger.exception('pick-candidates failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/dxf/<path:filename>/lavorazioni', methods=['GET'])
def api_preventivi_dxf_lavorazioni(preventivo_id, filename):
    """Riconta le lavorazioni dal DXF: pieghe (testi SU/GIU'), saldatura,
    filettatura, svasatura. Usato alla conferma del contorno nel CAD per
    aggiornare il conteggio pieghe (che dipende dal disegno, non dal contorno).

    Response: {pieghe, saldatura_ml, filettatura_pz, svasatura_pz}
    """
    try:
        safe_name = os.path.basename(filename)
        dxf_path = os.path.join(UPLOAD_FOLDER, 'preventivi_tmp', preventivo_id, safe_name)
        if not os.path.exists(dxf_path):
            return jsonify({'error': 'File DXF non trovato'}), 404
        from .preventivi.dxf_scanner import scansiona_dxf_dettagli
        app_cfg = BarcodeManager.load_config() or {}
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


@app.route('/api/preventivi/<preventivo_id>/dxf/<path:filename>/fold-model', methods=['POST'])
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
        dxf_path = os.path.join(UPLOAD_FOLDER, 'preventivi_tmp', preventivo_id, safe_name)
        if not os.path.exists(dxf_path):
            return jsonify({'success': False, 'error': 'File DXF non trovato'}), 404
        app_cfg = BarcodeManager.load_config() or {}
        cfg = app_cfg.get('dxf_detection', {})
        from .preventivi.dxf_scanner import estrai_pieghe_3d
        from .preventivi.pick_part import pick_candidates
        from .preventivi.pick_fold import build_fold_model

        pieghe = estrai_pieghe_3d(dxf_path, cfg)
        # Probe veloce: solo conteggio pieghe (per il badge), niente pick_candidates
        if data.get('probe'):
            return jsonify({'probe': True, 'has_bends': bool(pieghe),
                            'n_bends': len(pieghe)}), 200
        if not pieghe:
            return jsonify({'success': False, 'reason': 'no_bends',
                            'error': 'Nessuna piega leggibile in questo pezzo'}), 200

        outer = data.get('outer_xy')
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
            return jsonify({'success': False, 'reason': 'no_outline',
                            'error': 'Contorno non ricavabile in automatico'}), 200

        try:
            thickness = float(data.get('thickness') or 0) or 2.0
        except (TypeError, ValueError):
            thickness = 2.0
        model = build_fold_model(dxf_path, outer, thickness, cfg)
        return jsonify(model), 200
    except Exception as e:
        logger.exception('fold-model failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/dxf/<path:filename>/trace-waypoints', methods=['POST'])
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
        prev_dir = os.path.join(UPLOAD_FOLDER, 'preventivi_tmp', preventivo_id)
        dxf_path = os.path.join(prev_dir, safe_name)
        if not os.path.exists(dxf_path):
            return jsonify({'success': False, 'error': 'File DXF non trovato'}), 404
        from .preventivi.pick_part import trace_contour_waypoints
        app_cfg = BarcodeManager.load_config() or {}
        r = trace_contour_waypoints(dxf_path, points, app_cfg.get('dxf_detection', {}))
        return jsonify(r), 200
    except Exception as e:
        logger.exception('dxf_trace_waypoints failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/dxf/<path:filename>/select-region', methods=['POST'])
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
        prev_dir = os.path.join(UPLOAD_FOLDER, 'preventivi_tmp', preventivo_id)
        dxf_path = os.path.join(prev_dir, safe_name)
        if not os.path.exists(dxf_path):
            return jsonify({'success': False, 'error': 'File DXF non trovato'}), 404
        from .preventivi.dxf_polygon_detector_v3 import compute_geometry_from_region
        app_cfg = BarcodeManager.load_config() or {}
        detection_cfg = app_cfg.get('dxf_detection', {})
        r = compute_geometry_from_region(dxf_path, (minx, miny, maxx, maxy), detection_cfg)
        return jsonify({'success': True, **r}), 200
    except Exception as e:
        logger.exception('dxf_select_region failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/dxf/<path:filename>/save-cleaned', methods=['POST'])
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
        admin_id = data.get('admin_id') or ''
        if not _require_role(admin_id, _PREV_WRITE_ROLES):
            return jsonify({'success': False, 'error': 'Permesso negato'}), 403
        try:
            bbox = (
                float(data.get('minx')), float(data.get('miny')),
                float(data.get('maxx')), float(data.get('maxy')),
            )
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'minx/miny/maxx/maxy richiesti (float)'}), 400
        safe_name = os.path.basename(filename)
        prev_dir = os.path.join(UPLOAD_FOLDER, 'preventivi_tmp', preventivo_id)
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

        # 2. Ricalcola geometria (area/perim/n_forature) SUL FILE PULITO
        # Il pulito contiene SOLO le entità del pezzo → detector v3 dà valori
        # esatti se trova un poligono chiuso. Se invece il pezzo ha contorno
        # aperto (LINE sparse non chain-walkable), il detector prende una
        # sotto-parte piccola (es. cerchio di un foro come "pezzo") → dimensioni
        # sballate. In quel caso uso il bbox reale delle entità come fallback.
        geom = {}
        try:
            from .preventivi.dxf_polygon_detector_v3 import detect_pezzo_geometry_v3
            app_cfg = BarcodeManager.load_config() or {}
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
            bx1, by1, bx2, by2 = clean_bbox
            bbox_w = max(0.0, bx2 - bx1)
            bbox_h = max(0.0, by2 - by1)
            bbox_area_dm2 = (bbox_w * bbox_h) / 10000.0
            det_area = float(geom.get('area_dm2') or 0)
            use_bbox = (not det_area) or (bbox_area_dm2 > 0 and det_area < bbox_area_dm2 * 0.25)
            if use_bbox:
                # Perimetro = rettangolo esterno + somma perimetri fori interni
                bbox_perim_mm = 2.0 * (bbox_w + bbox_h)
                fori_perim_mm = 0.0
                n_pierce_fallback = 1
                try:
                    import ezdxf as _e
                    import math as _math
                    _d = _e.readfile(cleaned_path)
                    for _ent in _d.modelspace():
                        _t = _ent.dxftype()
                        if _t == 'CIRCLE':
                            _r = float(getattr(_ent.dxf, 'radius', 0) or 0)
                            fori_perim_mm += 2.0 * _math.pi * _r
                            n_pierce_fallback += 1
                        elif _t == 'ARC':
                            _r = float(getattr(_ent.dxf, 'radius', 0) or 0)
                            _sa = float(getattr(_ent.dxf, 'start_angle', 0) or 0)
                            _ea = float(getattr(_ent.dxf, 'end_angle', 0) or 0)
                            _sweep = (_ea - _sa) % 360.0
                            if _sweep == 0:
                                _sweep = 360.0
                            fori_perim_mm += 2.0 * _math.pi * _r * (_sweep / 360.0)
                            if _sweep >= 300.0:
                                n_pierce_fallback += 1
                        elif _t == 'LWPOLYLINE':
                            _pts = list(_ent.get_points('xy'))
                            _closed = bool(_ent.closed)
                            if len(_pts) >= 2:
                                for _i in range(len(_pts) - 1):
                                    _dx = _pts[_i+1][0] - _pts[_i][0]
                                    _dy = _pts[_i+1][1] - _pts[_i][1]
                                    fori_perim_mm += (_dx*_dx + _dy*_dy) ** 0.5
                                if _closed:
                                    _dx = _pts[0][0] - _pts[-1][0]
                                    _dy = _pts[0][1] - _pts[-1][1]
                                    fori_perim_mm += (_dx*_dx + _dy*_dy) ** 0.5
                                    n_pierce_fallback += 1
                except Exception as _pe:
                    logger.warning('fallback perim/pierce calc failed: %s', _pe)
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
            bbox_w_mm = round(cbbox[2] - cbbox[0], 2)
            bbox_h_mm = round(cbbox[3] - cbbox[1], 2)

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
        admin_id = data.get('admin_id') or ''
        if not _require_role(admin_id, _PREV_WRITE_ROLES):
            return jsonify({'success': False, 'error': 'Permesso negato'}), 403
        try:
            cx = float(data.get('x'))
            cy = float(data.get('y'))
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'x/y richiesti (float)'}), 400
        safe_name = os.path.basename(filename)
        prev_dir = os.path.join(UPLOAD_FOLDER, 'preventivi_tmp', preventivo_id)
        dxf_path = os.path.join(prev_dir, safe_name)
        if not os.path.exists(dxf_path):
            return jsonify({'success': False, 'error': 'File DXF non trovato'}), 404

        from .preventivi import dxf_cleanup
        base_p, ext_p = os.path.splitext(dxf_path)
        cleaned_path = base_p + '_cleaned' + ext_p
        cleanup_r = dxf_cleanup.save_cleaned_dxf_by_click(dxf_path, cleaned_path, cx, cy)
        if not cleanup_r.get('success'):
            return jsonify({'success': False, 'error': cleanup_r.get('error') or 'Cleanup fallito'}), 400

        # Ricalcola geometria sul cleaned
        geom = {}
        try:
            from .preventivi.dxf_polygon_detector_v3 import detect_pezzo_geometry_v3
            app_cfg = BarcodeManager.load_config() or {}
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
            bx1, by1, bx2, by2 = clean_bbox
            bbox_w_mm = round(bx2 - bx1, 2)
            bbox_h_mm = round(by2 - by1, 2)
            bbox_area_dm2 = (bbox_w_mm * bbox_h_mm) / 10000.0
            det_area = float(geom.get('area_dm2') or 0)
            use_bbox = (not det_area) or (bbox_area_dm2 > 0 and det_area < bbox_area_dm2 * 0.25)
            if use_bbox:
                # BUG FIX #2: al fallback il perimetro NON è solo 2*(w+h) del rettangolo
                # esterno — deve includere anche i perimetri di TUTTI i fori interni
                # (CIRCLE, ARC chiusi, LWPOLYLINE chiuse) perché il taglio laser
                # taglia sia il contorno che ogni foratura. Sotto-stima ~30-50% su
                # pezzi con molti fori.
                bbox_perim_mm = 2.0 * (bbox_w_mm + bbox_h_mm)
                fori_perim_mm = 0.0
                n_pierce_fallback = 1  # 1 pierce per contorno esterno
                try:
                    import ezdxf as _e
                    import math as _math
                    _d = _e.readfile(cleaned_path)
                    for _ent in _d.modelspace():
                        _t = _ent.dxftype()
                        if _t == 'CIRCLE':
                            _r = float(getattr(_ent.dxf, 'radius', 0) or 0)
                            fori_perim_mm += 2.0 * _math.pi * _r
                            n_pierce_fallback += 1
                        elif _t == 'ARC':
                            _r = float(getattr(_ent.dxf, 'radius', 0) or 0)
                            _sa = float(getattr(_ent.dxf, 'start_angle', 0) or 0)
                            _ea = float(getattr(_ent.dxf, 'end_angle', 0) or 0)
                            _sweep = (_ea - _sa) % 360.0
                            if _sweep == 0:
                                _sweep = 360.0
                            fori_perim_mm += 2.0 * _math.pi * _r * (_sweep / 360.0)
                            # Solo archi ~chiusi (>300°) contano come pierce
                            if _sweep >= 300.0:
                                n_pierce_fallback += 1
                        elif _t == 'LWPOLYLINE':
                            _pts = list(_ent.get_points('xy'))
                            _closed = bool(_ent.closed)
                            if len(_pts) >= 2:
                                for _i in range(len(_pts) - 1):
                                    _dx = _pts[_i+1][0] - _pts[_i][0]
                                    _dy = _pts[_i+1][1] - _pts[_i][1]
                                    fori_perim_mm += (_dx*_dx + _dy*_dy) ** 0.5
                                if _closed:
                                    _dx = _pts[0][0] - _pts[-1][0]
                                    _dy = _pts[0][1] - _pts[-1][1]
                                    fori_perim_mm += (_dx*_dx + _dy*_dy) ** 0.5
                                    n_pierce_fallback += 1
                except Exception as _pe:
                    logger.warning('fallback perim/pierce calc failed: %s', _pe)
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
            export_root = (app_cfg.get('disegni_export_root') or '').strip()
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
        prev_dir = os.path.join(UPLOAD_FOLDER, 'preventivi_tmp', preventivo_id)
        dxf_path = os.path.join(prev_dir, safe_name)
        if not os.path.exists(dxf_path):
            return jsonify({'success': False, 'error': 'File DXF non trovato'}), 404

        from .preventivi.dxf_polygon_detector_v3 import compute_geometry_from_candidate
        app_cfg = BarcodeManager.load_config() or {}
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
        d = os.path.join(UPLOAD_FOLDER, 'preventivi_tmp', preventivo_id)
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
    except Exception as exc:
        logger.warning('cleanup preventivo files failed for %s: %s', preventivo_id, exc)


def _copy_cleaned_dxf_to_drawings(preventivo_id: str, order_id: str) -> dict:
    """Copia i DXF PULITI (o originali con warning) da preventivi_tmp/<pid>/
    in uploads/drawings/<order_id>/ per Mirko (nesting Lantek).

    Preferenza:
      - `cleaned_dxf_filename` se disponibile (pulito auto o manuale)
      - fallback all'originale `dxf_filename` con warning ("cliente riceve
        DXF sporco, cartellino segnala che va pulito in Lantek")

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
        src_dir = os.path.join(UPLOAD_FOLDER, 'preventivi_tmp', preventivo_id)
        dst_dir = os.path.join(UPLOAD_FOLDER, 'drawings', order_id)
        os.makedirs(dst_dir, exist_ok=True)
        stats['drawings_dir'] = dst_dir

        articoli = p.get('articoli') or []
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
        return stats
    except Exception as e:
        logger.exception('_copy_cleaned_dxf_to_drawings failed')
        stats['warnings'].append(f'errore inatteso: {e}')
        return stats


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
        src_dir = os.path.join(UPLOAD_FOLDER, 'preventivi_tmp', preventivo_id)
        if not os.path.isdir(src_dir):
            return out
        pdfs = sorted(f for f in os.listdir(src_dir) if f.lower().endswith('.pdf'))
        if not pdfs:
            return out
        # Se ce n'è più d'uno, prende il primo (il PDF ordine RFQ è unico per pacchetto).
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
        admin_id = request.form.get('admin_id') or request.args.get('admin_id') or ''
        if not _require_role(admin_id, _PREV_WRITE_ROLES):
            return jsonify({'success': False, 'error': 'Permesso negato'}), 403
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'File STEP obbligatorio'}), 400
        f = request.files['file']
        if not f.filename or not f.filename.lower().endswith(('.step', '.stp')):
            return jsonify({'success': False, 'error': 'File deve essere .step o .stp'}), 400

        # Salva STEP in preventivi_tmp/<id>/ (persistente per preview 3D; cleanup su accept/reject/delete)
        prev_dir = os.path.join(UPLOAD_FOLDER, 'preventivi_tmp', preventivo_id)
        os.makedirs(prev_dir, exist_ok=True)
        safe_name = os.path.basename(f.filename)
        step_path = os.path.join(prev_dir, safe_name)
        f.save(step_path)
        try:
            assieme_data = _step_assieme.analizza_step_assieme(step_path) or {}
            tubolari_data = _step_tubolari.analizza_step_tubolari(step_path, _PROFILI_TUBOLARI_DB) or {}
            piastre_data = _step_piastre.analizza_step_piastre(step_path) or {}
        except Exception:
            # Se l'analisi fallisce non lasciare il file orfano
            try: os.remove(step_path)
            except OSError: pass
            raise

        # Coefficienti da config del Preventivatore desktop (oggi inline, in futuro spostiamoli in app_config)
        config_tubolari_piastre = {
            'costo_materiale_acciaio_kg': 1.50,
            'costo_materiale_inox_kg': 4.50,
            'costo_materiale_alluminio_kg': 3.50,
            'costo_orario_taglio_tubo': 40.0,
            'costo_taglio_dritto': 1.0,
            'costo_taglio_obliquo': 2.5,
            'costo_taglio_sagomato': 5.0,
        }

        # Costi tubolari + piastre
        tub_costi = {}
        pia_costi = {}
        try:
            tub_costi = _step_tubolari.calcola_costo_tubolare(tubolari_data, config_tubolari_piastre, 'acciaio')
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
        tubolari_list = []
        for t in (tubolari_data.get('tubi') or []):
            tubolari_list.append({
                'codice_assieme': codice_assieme_step,
                'profilo': t.get('profilo') or '',
                'tipo': t.get('tipo'),
                'materiale': 'acciaio',
                'lunghezza_m': t.get('lunghezza_m') or 0,
                'peso_kg': t.get('peso_kg') or 0,
                'costo_materiale': (t.get('peso_kg') or 0) * config_tubolari_piastre['costo_materiale_acciaio_kg'],
                'costo_taglio_totale': 0,  # aggregato in tub_costi.totale, qui zero per articolo singolo
                'n_tagli_dritti': 1 if t.get('taglio_1') == 'dritto' else 0,
                'n_tagli_obliqui': 1 if t.get('taglio_1') == 'obliquo' else 0,
            })

        # Normalizza piastre per la UI/DB
        # Costo piastre: a PESO × €/kg (coerente coi tubolari). Prima usava la
        # tabella prezzo_dm2 di calcola_costo_piastre, che NON è configurata →
        # costo 0 (bug: piastre a prezzo zero, preventivo sottostimato).
        kg_eur = config_tubolari_piastre['costo_materiale_acciaio_kg']
        piastre_list = []
        for p in (piastre_data.get('piastre') or []):
            peso_p = p.get('peso_kg') or 0
            costo_p = round(peso_p * kg_eur, 2)
            piastre_list.append({
                'codice_assieme': codice_assieme_step,
                'spessore_mm': p.get('spessore_mm') or 0,
                'area_dm2': p.get('area_dm2') or 0,
                'peso_kg': peso_p,
                'costo': costo_p,
                'materiale': 'acciaio',
            })
        costo_totale_piastre = round(sum(x['costo'] for x in piastre_list), 2)

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
                'peso_kg': (tubolari_data.get('peso_totale_kg') or 0) + (piastre_data.get('peso_totale_kg') or 0),
                'componenti_qty': {},
            })

        return jsonify({
            'success': True,
            'assiemi': assiemi_list,
            'tubolari': tubolari_list,
            'piastre': piastre_list,
            'summary': {
                'n_tubolari': len(tubolari_list),
                'n_piastre': len(piastre_list),
                'peso_totale_kg': round(((tubolari_data.get('peso_totale_kg') or 0) + (piastre_data.get('peso_totale_kg') or 0)), 2),
                'saldatura_mt_tot': round(saldatura_mt_tot, 2),
                'costo_totale_tubolari': tub_costi.get('totale', 0),
                'costo_totale_piastre': costo_totale_piastre,
            },
        }), 200
    except Exception as e:
        logger.exception('preventivi import step failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/articoli', methods=['PUT'])
def api_preventivi_articoli_replace(preventivo_id):
    """Sostituisce l'intera lista degli articoli del preventivo (bulk replace).

    Usato dal frontend come autosave dopo import DXF / cambi editor. Il
    backend fa delete + insert atomici via PreventivoManager.replace_articoli.
    Bloccato se preventivo INVIATO / ACCETTATO.
    """
    try:
        data = request.get_json(silent=True) or {}
        admin_id = data.get('admin_id') or ''
        if not _require_role(admin_id, _PREV_WRITE_ROLES):
            return jsonify({'success': False, 'error': 'Permesso negato'}), 403
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
def api_preventivi_assiemi_replace(preventivo_id):
    """Sostituisce l'intera lista degli assiemi del preventivo (bulk replace).

    Simmetrico ad api_preventivi_articoli_replace: usato dal frontend come
    autosave dopo modifiche editor (nuovo assieme, cambio qty/costi, link
    articoli). Bloccato se preventivo INVIATO/ACCETTATO.
    """
    try:
        data = request.get_json(silent=True) or {}
        admin_id = data.get('admin_id') or ''
        if not _require_role(admin_id, _PREV_WRITE_ROLES):
            return jsonify({'success': False, 'error': 'Permesso negato'}), 403
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
def api_preventivi_tubolari_replace(preventivo_id):
    """Sostituisce l'intera lista dei tubolari (da STEP). Autosave dopo import
    STEP / modifiche. Bloccato se INVIATO/ACCETTATO. Simmetrico ad assiemi."""
    try:
        data = request.get_json(silent=True) or {}
        admin_id = data.get('admin_id') or ''
        if not _require_role(admin_id, _PREV_WRITE_ROLES):
            return jsonify({'success': False, 'error': 'Permesso negato'}), 403
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
def api_preventivi_piastre_replace(preventivo_id):
    """Sostituisce l'intera lista delle piastre (da STEP). Autosave dopo import
    STEP / modifiche. Bloccato se INVIATO/ACCETTATO. Simmetrico ad assiemi."""
    try:
        data = request.get_json(silent=True) or {}
        admin_id = data.get('admin_id') or ''
        if not _require_role(admin_id, _PREV_WRITE_ROLES):
            return jsonify({'success': False, 'error': 'Permesso negato'}), 403
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
def api_preventivi_stima_base(preventivo_id, articolo_id):
    """Calcola stima costo base laser per un articolo (richiede spessore+materiale).

    Body opzionale: { articolo: {area_dm2, perimetro_taglio_m, n_forature,
                                  spessore_mm, materiale} }
    Se body non fornito, legge dal DB l'articolo per id.
    """
    try:
        data = request.get_json(silent=True) or {}
        admin_id = data.get('admin_id') or request.args.get('admin_id') or ''
        if not _require_role(admin_id, _PREV_WRITE_ROLES):
            return jsonify({'success': False, 'error': 'Permesso negato'}), 403
        articolo = data.get('articolo')
        if not articolo:
            # Leggi articolo dal DB
            p = PreventivoManager.get(preventivo_id, include_children=True)
            if not p:
                return jsonify({'success': False, 'error': 'Preventivo non trovato'}), 404
            articolo = next((a for a in p['articoli'] if a['id'] == articolo_id), None)
            if not articolo:
                return jsonify({'success': False, 'error': 'Articolo non trovato'}), 404
        cfg = BarcodeManager.load_config()
        stima = _laser_estimator.stima_base(articolo, cfg)
        return jsonify({'success': True, 'stima': stima}), 200
    except Exception as e:
        logger.exception('preventivi stima-base failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/config', methods=['GET'])
def api_preventivi_config_get():
    """Coefficienti globali usati dallo stimatore + cost calculator.

    Ritorna laser_config (costi orari, €/kg, densità) + preventivi_config
    (costi lavorazioni post-taglio, sconti, margini default). Usato dalla tab
    Impostazioni per il form editabile.

    Accessibile a Commerciale + Amministratore + Capo.
    """
    try:
        cfg = BarcodeManager.load_config()
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
        }), 200
    except Exception as e:
        logger.exception('preventivi/config GET failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/config', methods=['PUT'])
def api_preventivi_config_put():
    """Aggiorna coefficienti globali. Salva quello che riceve senza validazione
    stretta sui valori (l'admin è responsabile). Loggato in audit.

    Body: {admin_id, laser_config?, preventivi_config?}. I singoli sub-oggetti
    sono opzionali: se assenti si mantiene quello attuale.
    """
    try:
        data = request.get_json(silent=True) or {}
        admin_id = data.get('admin_id') or ''
        if not _require_role(admin_id, _PREV_WRITE_ROLES):
            return jsonify({'success': False, 'error': 'Permesso negato'}), 403
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
        saved = BarcodeManager.save_config(updates)
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
        }), 200
    except Exception as e:
        logger.exception('preventivi/config PUT failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/calcola', methods=['POST'])
def api_preventivi_calcola(preventivo_id):
    """Ricalcola totali del preventivo (chiama cost_calculator)."""
    try:
        data = request.get_json(silent=True) or {}
        admin_id = data.get('admin_id') or ''
        if not _require_role(admin_id, _PREV_WRITE_ROLES):
            return jsonify({'success': False, 'error': 'Permesso negato'}), 403
        # Implementazione completa rimandata a Fase 3 (richiede integrazione editor articoli)
        # Per ora ritorna i totali correnti dal DB (placeholder funzionale)
        p = PreventivoManager.get(preventivo_id, include_children=False)
        if not p:
            return jsonify({'success': False, 'error': 'Preventivo non trovato'}), 404
        return jsonify({
            'success': True,
            'preventivo': p,
            '_note': 'Ricalcolo completo via cost_calculator implementato in Fase 3',
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
        numero = (order.get('numero_ordine') if isinstance(order, dict)
                  else getattr(order, 'numero_ordine', None)) or ''
        cliente = (order.get('cliente') if isinstance(order, dict)
                   else getattr(order, 'cliente', None)) or ''
        root = ((BarcodeManager.load_config() or {}).get('disegni_export_root') or '').strip()
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
        numero = (order.get('numero_ordine') if isinstance(order, dict)
                  else getattr(order, 'numero_ordine', None)) or ''
        cliente = (order.get('cliente') if isinstance(order, dict)
                   else getattr(order, 'cliente', None)) or ''
        oid = (order.get('id') if isinstance(order, dict)
               else getattr(order, 'id', None)) or ''
        root = ((BarcodeManager.load_config() or {}).get('disegni_export_root') or '').strip()
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


def _esporta_disegni_per_officina(order_id: str, cliente: str, numero_ordine: str) -> dict:
    """Copia i disegni dell'ordine nella cartella di rete, con nome leggibile.

    Serve perche' l'operatore apre quella cartella in Lantek: `uploads/drawings/
    <uuid>` non e' un posto dove uno va a cercare.
    """
    esito = {'esportati': 0, 'percorso': None, 'error': None}
    root = ((BarcodeManager.load_config() or {}).get('disegni_export_root') or '').strip()
    if not root:
        esito['error'] = 'cartella di rete non configurata'
        return esito
    sorgente = os.path.join(DRAWINGS_FOLDER, order_id)
    if not os.path.isdir(sorgente):
        esito['error'] = 'nessun disegno da esportare'
        return esito
    try:
        import shutil
        from .preventivi.dxf_cleanup import _sanitize_path_part
        destinazione = os.path.join(root,
                                    _sanitize_path_part(cliente, 'cliente_sconosciuto'),
                                    _sanitize_path_part(numero_ordine or order_id[:8], 'ordine'))
        os.makedirs(destinazione, exist_ok=True)
        n = 0
        for nome in os.listdir(sorgente):
            src = os.path.join(sorgente, nome)
            if not os.path.isfile(src):
                continue
            shutil.copy2(src, os.path.join(destinazione, nome))
            n += 1
        esito['esportati'] = n
        esito['percorso'] = destinazione
        logger.info('Disegni ordine %s esportati in %s (%d file)', order_id, destinazione, n)
    except Exception as e:
        esito['error'] = str(e)
        logger.exception('export disegni in cartella di rete fallito')
    return esito


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
    # Densità materiali per calcolo peso al volo dei componenti DXF (idem frontend).
    # Allineato con DEFAULT_LASER_CONFIG in laser_cost_estimator.py.
    _DENSITA = {'S235': 7.85, 'ZINCATO': 7.85, 'INOX_304': 8.0, 'INOX_316': 8.0,
                'ALU': 2.7, 'ALU_5754': 2.7, 'ALU_5083': 2.7, 'OTTONE': 8.5}
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
            base_pz = float(art.get('costo_base_override') if art.get('costo_base_override') is not None
                            else (art.get('costo_base_stimato') or art.get('costo_materiale') or 0))
            lav_pz = sum(float(art.get(k) or 0) for k in (
                'costo_piega', 'costo_saldatura', 'costo_filettatura',
                'costo_svasatura', 'costo_apporto', 'costo_pulizia'))
            tot_pz = base_pz + lav_pz
            rho = _DENSITA.get((art.get('materiale') or '').upper(), 7.85)
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
            dt = datetime.fromisoformat(p['data_creazione'])
            data_str = dt.strftime('%d/%m/%Y')
        except Exception:
            data_str = p['data_creazione']
    else:
        data_str = datetime.now().strftime('%d/%m/%Y')

    # Ogni articolo per PDF vuole 'costo' = costo unitario totale
    articoli_pdf = []
    for a in articoli:
        costo_base = a.get('costo_base_override') if a.get('costo_base_override') is not None else (a.get('costo_base_stimato') or 0)
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
            'costo_materiale': a.get('costo_materiale') or costo_base,
            'costo_piega': a.get('costo_piega') or 0,
            'costo_saldatura': a.get('costo_saldatura') or 0,
            'costo_filettatura': a.get('costo_filettatura') or 0,
            'costo_svasatura': a.get('costo_svasatura') or 0,
            'costo_apporto': a.get('costo_apporto') or 0,
            'costo_pulizia': a.get('costo_pulizia') or 0,
        })

    # Info azienda: da app_config sezione 'azienda' se presente
    app_cfg = BarcodeManager.load_config() or {}
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
        margine_pct = float(_snap.get('ricarico_pct') or margine_pct)
    else:
        _generali_pct = float((app_cfg.get('preventivi_config') or {}).get('costo_generali_pct', 0))

    # Fattore prezzo finale = generali (overhead) × ricarico. Incorpora TUTTO
    # ciò che il cliente non deve vedere scomposto (costi + margine).
    _gen_f = 1 + _generali_pct / 100.0
    _f_finale = _gen_f * (1 + margine_pct / 100.0)

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
    totale_pezzo_calc = 0.0
    for a in articoli:
        if a.get('codice_assieme'):
            continue  # articoli linkati ad assieme già dentro pricing assieme
        base = float(a.get('costo_base_override') if a.get('costo_base_override') is not None
                     else (a.get('costo_base_stimato') or a.get('costo_materiale') or 0))
        lav = sum(float(a.get(k) or 0) for k in (
            'costo_piega', 'costo_saldatura', 'costo_filettatura',
            'costo_svasatura', 'costo_apporto', 'costo_pulizia'))
        qty_art = int(a.get('quantita') or 1)
        totale_pezzo_calc += (base + lav) * qty_art
        prezzo_unit_finale = (base + lav) * _f_finale
        qty_tot = qty_art * qty_preventivo
        righe_cliente.append({
            'codice': a.get('codice') or '—',
            'descrizione': _desc_articolo(a),
            'quantita': qty_tot,
            'prezzo_unitario': round(prezzo_unit_finale, 2),
            'importo': round(prezzo_unit_finale * qty_tot, 2),
        })

    # Costo assiemi (totale pricing rollup)
    costo_assiemi_calc = 0.0
    for asm in assiemi_list:
        cod = asm.get('codice_assieme') or asm.get('id') or ''
        info = costi_montaggio.get(cod, {})
        # Somma componenti articoli DXF (contributo_su_1_ass già calcolato sopra)
        cost_articoli_ass = sum(item.get('contributo_su_1_ass', 0) for item in info.get('bom_articoli', []))
        # Tubolari/piastre nell'assieme
        cost_tubolari_ass = sum(float(t.get('costo_materiale') or 0) + float(t.get('costo_taglio_totale') or 0)
                                 for t in info.get('bom_tubolari', []))
        cost_piastre_ass = sum(float(pl.get('costo') or 0) for pl in info.get('bom_piastre', []))
        # Costo intrinseco assieme (montaggio + puntatura + saldatura)
        cost_intrinseco = (float(info.get('costo') or 0)
                           + float(info.get('costo_puntatura') or 0)
                           + float(info.get('costo_saldatura_assieme') or 0))
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
    costo_tubolari_std = sum(float(t.get('costo_materiale') or 0) + float(t.get('costo_taglio_totale') or 0)
                              for t in tubolari_list if not t.get('codice_assieme'))
    costo_piastre_std = sum(float(pl.get('costo') or 0)
                             for pl in piastre_list if not pl.get('codice_assieme'))
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
    try:
        from .preventivi.calcolo import calcola as _calcola_autorevole
        _cfg_prezzo = ({'costo_generali_pct': _generali_pct} if _snap
                       else (app_cfg.get('preventivi_config') or {}))
        _tot = _calcola_autorevole(p, _cfg_prezzo)
        _scarto = abs(_tot['totale_lotto_lordo'] - totale_lotto_calc)
        if _scarto > 0.5:
            logger.warning(
                'PDF preventivo %s: righe %.2f vs calcolo autorevole %.2f',
                p.get('id'), totale_lotto_calc, _tot['totale_lotto_lordo'])
        totale_lotto_calc = _tot['totale_lotto_lordo']
        # Uno sconto deve comparire come riga, altrimenti le righe non
        # sommerebbero piu' al totale e il cliente non capirebbe il numero.
        if _tot['sconto_pct']:
            _sconto_eur = round(_tot['totale_lotto'] - _tot['totale_lotto_lordo'], 2)
            righe_cliente.append({
                'codice': 'Sconto',
                'descrizione': f"Sconto {_tot['sconto_pct']:g}%",
                'quantita': 1, 'prezzo_unitario': _sconto_eur, 'importo': _sconto_eur,
            })
            totale_lotto_calc = _tot['totale_lotto']
    except Exception as _e:
        logger.exception('calcolo autorevole per il PDF fallito: %s', _e)

    return {
        'cliente': p.get('cliente') or '',
        'numero_ordine': p.get('numero_ordine_cliente') or f"PREV-{p.get('id', '')[:8]}",
        'data': data_str,
        'articoli': articoli_pdf,
        'quantita': qty_preventivo,
        'margine': margine_pct,
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

        app_cfg = BarcodeManager.load_config() or {}
        exporter = _pdf_exporter.PDFPreventivo(app_cfg)
        exporter.genera_pdf(pdf_path, dati_pdf, interno=interno)

        return send_file(pdf_path, mimetype='application/pdf',
                         as_attachment=not inline, download_name=filename)
    except Exception as e:
        logger.exception('preventivi pdf failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/storico-prezzo', methods=['GET'])
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
    for a in (prev.get('articoli') or []):
        codice = a.get('codice') or '(senza codice)'
        # 1) Geometria confermata nel CAD interno (fail-safe): se l'articolo ha
        #    un DXF ma non è stato confermato dall'operatore → blocco.
        if a.get('dxf_filename') and not a.get('geometria_manuale_confermata'):
            invalidi.append({'codice': codice, 'motivo': 'geometria non confermata nel CAD (apri e conferma il pezzo)'})
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
def api_preventivi_invia(preventivo_id):
    """Transizione BOZZA → INVIATO. (Snapshot versioning sarà aggiunto in Fase 3.)"""
    try:
        data = request.get_json(silent=True) or {}
        user_id = data.get('user_id') or ''
        if not _require_role(user_id, _PREV_WRITE_ROLES):
            return jsonify({'success': False, 'error': 'Permesso negato'}), 403
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
    app_cfg = BarcodeManager.load_config() or {}
    _pdf_exporter.PDFPreventivo(app_cfg).genera_pdf(pdf_path, dati_pdf, interno=False)
    with open(pdf_path, 'rb') as fp:
        return fp.read(), filename


_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def _azienda_info() -> dict:
    """Dati azienda (per firma email / intestazioni), da app_config.json."""
    cfg = BarcodeManager.load_config() or {}
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
def api_preventivi_ultima_email_cliente():
    """Ultimo indirizzo email usato per un cliente, per riproporlo all'invio."""
    try:
        cliente = request.args.get('cliente', '')
        return jsonify({'success': True, 'email': PreventivoManager.ultima_email_cliente(cliente)}), 200
    except Exception as e:
        logger.exception('ultima-email-cliente failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/preventivi/<preventivo_id>/invia-email', methods=['POST'])
def api_preventivi_invia_email(preventivo_id):
    """Spedisce il PDF cliente via email e, se il preventivo è BOZZA, lo porta a
    INVIATO (immutabile). La transizione avviene SOLO se la mail parte davvero.

    Body: {user_id, to, subject?, message?}
    Se SMTP non è configurato → {success:False, email_non_configurata:True}.
    """
    try:
        data = request.get_json(silent=True) or {}
        user_id = data.get('user_id') or ''
        if not _require_role(user_id, _PREV_WRITE_ROLES):
            return jsonify({'success': False, 'error': 'Permesso negato'}), 403

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

    Output: {success, order_id, numero_ordine, cartellino_url, preventivo}.
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
        user_id = data.get('user_id') or ''
        # Anche l'Impiegata (Elena) conferma: è lei che vede la risposta via mail
        # del cliente e fa partire il ciclo produttivo.
        if not _require_role(user_id, _PREV_WRITE_ROLES + ['Impiegata']):
            return jsonify({'success': False, 'error': 'Permesso negato'}), 403
        # Se il payload include articoli override, prima li salva così la validazione
        # server-side controlla lo stato AGGIORNATO (evita race con edit non salvato).
        if data.get('articoli'):
            try:
                PreventivoManager.replace_articoli(preventivo_id, data['articoli'])
            except Exception:
                pass  # se fallisce, la validazione userà i dati DB e comunque bloccherà
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
                    result.get('numero_ordine') or '')
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
def api_preventivi_rifiuta(preventivo_id):
    """Transizione INVIATO → RIFIUTATO."""
    try:
        data = request.get_json(silent=True) or {}
        user_id = data.get('user_id') or ''
        # Anche l'Impiegata (Elena) può rifiutare: vede la risposta del cliente.
        if not _require_role(user_id, _PREV_WRITE_ROLES + ['Impiegata']):
            return jsonify({'success': False, 'error': 'Permesso negato'}), 403
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
            if creatore and creatore != user_id:
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
def api_admin_laser_config_get():
    """Ritorna sezione laser_config dal app_config.json (coefficienti stimatore)."""
    try:
        cfg = BarcodeManager.load_config()
        return jsonify({
            'success': True,
            'laser_config': cfg.get('laser_config') or _laser_estimator.DEFAULT_LASER_CONFIG,
        }), 200
    except Exception as e:
        logger.exception('admin laser-config GET failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/admin/laser-config', methods=['PUT'])
def api_admin_laser_config_put():
    """Aggiorna coefficienti stimatore laser (solo admin/capi)."""
    try:
        data = request.get_json(silent=True) or {}
        admin_id = data.get('admin_id') or ''
        if not _require_role(admin_id, ['Amministratore', 'CAPO']):
            return jsonify({'success': False, 'error': 'Permesso negato'}), 403
        new_config = data.get('laser_config')
        if not isinstance(new_config, dict):
            return jsonify({'success': False, 'error': 'laser_config deve essere un oggetto'}), 400
        saved = BarcodeManager.save_config({'laser_config': new_config})
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
def api_admin_export_orders():
    """Export CSV ordini per gestionale esterno (cliente non ha Odoo ma userà altro gestionale).

    Query: ?format=csv|json (default: csv), ?from=YYYY-MM-DD, ?to=YYYY-MM-DD
    """
    try:
        admin_id = request.args.get('admin_id') or ''
        if not _require_role(admin_id, ['Amministratore', 'CAPO']):
            return jsonify({'success': False, 'error': 'Permesso negato'}), 403
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
    app.run(debug=True, host='0.0.0.0', port=5000)
