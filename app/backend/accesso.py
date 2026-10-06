"""Chi sta usando FerroTrack, deciso dal SERVER e mai dal browser.

IL PROBLEMA
-----------
Fino a qui l'identita' la dichiarava la pagina: un'intestazione X-User-Id o
uno `user_id` nel corpo della richiesta. Chiunque sulla rete del capannone
poteva scrivere "postazione-amministrazione" e diventare l'amministrazione:
creare utenti, cambiare i prezzi, accettare e spedire preventivi, chiudere
ordini, scaricare l'archivio.

COME FUNZIONA ORA
-----------------
Tre cose, tutte verificate qui:

1. IL DISPOSITIVO. Ogni PC o tablet viene registrato UNA volta come una delle
   cinque stazioni (Commerciale, Ufficio, Laser, Tablet officina,
   Timbratrice), col PIN di un amministratore. Riceve un segreto lungo che
   il browser tiene in un cookie HttpOnly (JavaScript non lo legge, quindi
   uno script malevolo non lo ruba) e SameSite=Strict (un altro sito non lo
   fa partire). Nel database c'e' solo l'impronta del segreto. I tablet gia'
   configurati col vecchio token nell'intestazione X-Device-Token continuano
   a funzionare: alla prima richiesta il server lo copia nel cookie.

2. LA PERSONA, solo negli uffici. In Commerciale e Ufficio ci sono prezzi e
   fatture: si entra col PIN personale, che dice CHI e' (Elena, Stefano...),
   cosi' il registro delle attivita' scrive il nome vero. La sessione dura
   tutta la giornata e finisce alle 3 di notte, ora italiana: di giorno il
   PIN non si richiede mai. Al laser, al tablet e alla timbratrice si entra
   subito: lo dice il dispositivo.

3. L'AMMINISTRATORE. Registrare o cambiare un dispositivo, rimettere un PIN,
   gestire persone, prezzi e impostazioni richiede il PIN di una persona
   amministratore digitato AL MOMENTO, su qualunque dispositivo (al laser non
   c'e' un PIN d'ingresso, quindi non basta "essere al laser"). Vale qualche
   minuto, poi si richiede.

Ogni rotta /api dichiara con @richiede(...) quali stazioni la possono usare;
un unico controllo prima della richiesta (vedi `controlla_richiesta`) decide.
Una rotta senza regola viene RIFIUTATA: dimenticarsene chiude, non apre.

PERIODO DI TRANSIZIONE
----------------------
Sull'installazione in uso il giorno dell'aggiornamento nessun dispositivo e'
ancora registrato. Con "accesso": {"modalita": "transizione"} in
app_config.json i dispositivi non registrati continuano col vecchio modo (la
postazione scelta all'ingresso) e ogni pagina mostra una fascia gialla; le
operazioni di amministrazione chiedono gia' il PIN. Con "protetto" il
vecchio modo e' rifiutato del tutto. Le installazioni nuove partono protette.
"""
import base64
import hashlib
import hmac
import logging
import secrets
import uuid
from collections import namedtuple
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from flask import g, jsonify, request

from . import orario
from .database import get_session
from .models import User
from .models_ore import DeviceToken, SessioneAccesso, TentativoPin

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Le stazioni
# ---------------------------------------------------------------------------
# `utente` e' la vecchia utenza di postazione: resta il "chi" dello storico
# quando non c'e' una persona (al laser, in officina), cosi' le colonne che
# puntano a users (avvisi, registro attivita', smistato_da...) restano valide.
STAZIONI = {
    'commerciale': {'nome': 'Commerciale', 'descrizione': 'Preventivi',
                    'pagina': '/preventivi.html', 'con_pin': True,
                    'utente': 'postazione-commerciale'},
    'ufficio': {'nome': 'Ufficio', 'descrizione': 'Ordini, DDT, fatture, ore',
                'pagina': '/impiegata.html', 'con_pin': True,
                'utente': 'postazione-amministrazione'},
    'laser': {'nome': 'Laser', 'descrizione': 'Preparazione per Lantek',
              'pagina': '/laser.html', 'con_pin': False,
              'utente': 'postazione-laser'},
    'reparto': {'nome': 'Tablet officina', 'descrizione': 'Ordini in lavorazione',
                'pagina': '/operaio-info.html', 'con_pin': False,
                'utente': 'postazione-visione'},
    'ore': {'nome': 'Timbratrice', 'descrizione': 'Ore degli operai',
            'pagina': '/ore.html', 'con_pin': False,
            'utente': 'postazione-timbratrice'},
}
UFFICI = ('commerciale', 'ufficio')
OFFICINA = ('laser', 'reparto', 'ore')
TUTTE = tuple(STAZIONI)

# Pseudo-stazioni usabili in @richiede
ADMIN = 'admin'          # basta un amministratore col PIN appena digitato
PUBBLICO = 'pubblico'    # nessun controllo (salute del server, ingresso)

# Chi nasce amministratore (decisione del titolare: chi lavora in
# amministrazione o al laser). Poi lo decide un altro amministratore.
RUOLI_ADMIN_DI_PARTENZA = ('Amministrazione', 'Impiegata', 'Laser', 'Operaio Laser')

# Ruoli delle persone che si possono scegliere nella pagina Amministrazione.
RUOLI_PERSONA = ('Amministrazione', 'Commerciale', 'Laser', 'Operaio')

# Vecchie utenze di postazione -> stazione (solo in transizione, solo per i
# dispositivi non ancora registrati).
_STAZIONE_DA_RUOLO = {
    'Timbratrice': 'ore', 'Visione': 'reparto',
    'Amministrazione': 'ufficio', 'Impiegata': 'ufficio',
    'Laser': 'laser', 'Operaio Laser': 'laser',
    'Commerciale': 'commerciale',
}

COOKIE_DISP = 'ft_disp'
COOKIE_SESS = 'ft_sess'
COOKIE_ADMIN = 'ft_admin'
COOKIE_VECCHIO = 'ft_postazione'   # solo transizione: la vecchia postazione scelta
_HEADER_TOKEN = 'X-Device-Token'

MODALITA = ('transizione', 'protetto')

# Quanto vale il PIN di amministratore appena digitato.
DURATA_ADMIN = timedelta(minutes=15)
# A che ora finisce la giornata di lavoro (ora italiana).
ORA_FINE_GIORNATA = time(3, 0)

# Blocco dei tentativi: 5 PIN sbagliati di fila sullo stesso dispositivo e
# si aspetta 5 minuti. In piu' un limite su TUTTI i dispositivi insieme, per
# chi provasse da piu' postazioni. Col PIN di sole cifre e' quello che rende
# inutile tirare a indovinare.
MAX_ERRORI = 5
BLOCCO = timedelta(minutes=5)
FINESTRA = timedelta(minutes=15)
MAX_ERRORI_TOTALI = 20

PIN_MIN, PIN_MAX = 4, 8


def adesso() -> datetime:
    """Adesso in UTC naive. Una funzione sola, cosi' i test spostano l'orologio."""
    return datetime.utcnow()


# ---------------------------------------------------------------------------
# PIN: impronta con scrypt e sale per persona
# ---------------------------------------------------------------------------
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode('ascii')


def impronta_pin(pin: str) -> str:
    """Impronta del PIN da salvare. scrypt e' lento e usa memoria apposta:
    con un PIN di poche cifre e' cio' che rende costoso provarle tutte su
    una copia rubata del database. Il sale e' diverso per ogni persona."""
    sale = secrets.token_bytes(16)
    h = hashlib.scrypt(str(pin).encode('utf-8'), salt=sale, n=_SCRYPT_N,
                       r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return 'scrypt$%d$%d$%d$%s$%s' % (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _b64(sale), _b64(h))


def pin_corrisponde(pin: str, impronta: str) -> bool:
    """Confronto a tempo costante (hmac.compare_digest)."""
    try:
        tipo, n, r, p, sale, h = (impronta or '').split('$')
        if tipo != 'scrypt':
            return False
        atteso = base64.b64decode(h)
        calcolato = hashlib.scrypt(str(pin).encode('utf-8'), salt=base64.b64decode(sale),
                                   n=int(n), r=int(r), p=int(p), dklen=len(atteso))
        return hmac.compare_digest(atteso, calcolato)
    except Exception:
        return False


def errore_formato_pin(pin) -> str:
    """Il motivo per cui un PIN nuovo non va bene, o '' se va bene."""
    pin = str(pin or '').strip()
    if not pin.isdigit() or not (PIN_MIN <= len(pin) <= PIN_MAX):
        return 'Il PIN deve avere da %d a %d cifre.' % (PIN_MIN, PIN_MAX)
    if len(set(pin)) == 1:
        return 'Il PIN non può essere la stessa cifra ripetuta.'
    cifre = [int(c) for c in pin]
    passi = {b - a for a, b in zip(cifre, cifre[1:])}
    if passi in ({1}, {-1}):
        return 'Il PIN non può essere una sequenza come 1234.'
    return ''


def _hash_segreto(segreto: str) -> str:
    # Stesso calcolo di auth_device: i segreti di dispositivo e di sessione
    # sono lunghi e casuali, uno sha256 basta (non sono indovinabili).
    return hashlib.sha256((segreto or '').encode('utf-8')).hexdigest()


# ---------------------------------------------------------------------------
# Tentativi e blocco
# ---------------------------------------------------------------------------
def _chiave_tentativi(dispositivo_id=None) -> str:
    if dispositivo_id:
        return 'disp:' + dispositivo_id
    return 'ip:' + (request.remote_addr or '?')


def _bloccato_fino(s, chiave: str, ora: datetime):
    """Fino a quando questo dispositivo deve aspettare, o None."""
    ultimo_ok = s.query(TentativoPin.quando).filter(
        TentativoPin.chiave == chiave, TentativoPin.riuscito == True  # noqa: E712
    ).order_by(TentativoPin.quando.desc()).first()
    da = ora - FINESTRA
    if ultimo_ok and ultimo_ok[0] > da:
        da = ultimo_ok[0]
    errori = s.query(TentativoPin.quando).filter(
        TentativoPin.chiave == chiave, TentativoPin.riuscito == False,  # noqa: E712
        TentativoPin.quando > da).order_by(TentativoPin.quando.desc()).all()
    if len(errori) >= MAX_ERRORI:
        fino = errori[0][0] + BLOCCO
        if fino > ora:
            return fino
    tutti = s.query(TentativoPin.quando).filter(
        TentativoPin.riuscito == False, TentativoPin.quando > ora - FINESTRA  # noqa: E712
    ).order_by(TentativoPin.quando.desc()).all()
    if len(tutti) >= MAX_ERRORI_TOTALI:
        fino = tutti[0][0] + BLOCCO
        if fino > ora:
            return fino
    return None


def _errori_rimasti(s, chiave, ora) -> int:
    ultimo_ok = s.query(TentativoPin.quando).filter(
        TentativoPin.chiave == chiave, TentativoPin.riuscito == True  # noqa: E712
    ).order_by(TentativoPin.quando.desc()).first()
    da = ora - FINESTRA
    if ultimo_ok and ultimo_ok[0] > da:
        da = ultimo_ok[0]
    n = s.query(TentativoPin).filter(
        TentativoPin.chiave == chiave, TentativoPin.riuscito == False,  # noqa: E712
        TentativoPin.quando > da).count()
    return max(0, MAX_ERRORI - n)


def _messaggio_blocco(fino: datetime) -> dict:
    minuti = max(1, int((fino - adesso()).total_seconds() + 59) // 60)
    return {'success': False, 'codice': 'bloccato',
            'error': 'Troppi PIN sbagliati. Riprova fra %d minut%s.' % (minuti, 'o' if minuti == 1 else 'i'),
            'riprova_fra_s': max(1, int((fino - adesso()).total_seconds()))}


def _persona(u) -> dict:
    return {'id': u.id, 'nome': u.name, 'ruolo': u.role, 'admin': bool(u.e_admin)}


def persone_con_pin(s):
    return [u for u in s.query(User).filter(User.is_active == True,  # noqa: E712
                                              User.pin_hash.isnot(None)).all()
            if not u.e_postazione]


def verifica_pin(pin, chiave: str, serve_admin: bool = False):
    """(persona, None) se il PIN e' di una persona attiva (amministratore se
    serve_admin), altrimenti (None, risposta_json). Registra il tentativo.

    Il PIN da solo dice chi e' la persona (decisione: niente elenchi di nomi
    all'ingresso), per questo i PIN sono unici. Un PIN valido ma di una
    persona non amministratore, chiesto per un'operazione da amministratore,
    conta come sbagliato e riceve la stessa risposta: altrimenti dal tablet
    d'officina si potrebbe scoprire quali PIN esistono.
    """
    s = get_session()
    try:
        ora = adesso()
        fino = _bloccato_fino(s, chiave, ora)
        if fino:
            return None, _messaggio_blocco(fino)
        pin = str(pin or '').strip()
        trovato = None
        if pin.isdigit() and PIN_MIN <= len(pin) <= PIN_MAX:
            # Si provano tutte le persone, anche dopo averla trovata: il tempo
            # di risposta non dice a che punto dell'elenco sta il PIN.
            for u in persone_con_pin(s):
                if pin_corrisponde(pin, u.pin_hash) and trovato is None:
                    trovato = u
        ok = trovato is not None and (not serve_admin or bool(trovato.e_admin))
        s.add(TentativoPin(id=str(uuid.uuid4()), chiave=chiave, quando=ora, riuscito=ok))
        # i tentativi vecchi non servono piu' a niente
        s.query(TentativoPin).filter(TentativoPin.quando < ora - timedelta(days=2)).delete()
        s.commit()
        if not ok:
            logger.warning('PIN sbagliato da %s (%s)', chiave, 'admin' if serve_admin else 'ingresso')
            fino = _bloccato_fino(s, chiave, ora)
            if fino:
                return None, _messaggio_blocco(fino)
            testo = ('PIN sbagliato, oppure non è di un amministratore.' if serve_admin
                     else 'PIN sbagliato.')
            rimasti = _errori_rimasti(s, chiave, ora)
            if rimasti <= 2:
                testo += ' Ancora %d tentativ%s, poi bisogna aspettare.' % (rimasti, 'o' if rimasti == 1 else 'i')
            return None, {'success': False, 'codice': 'pin_sbagliato', 'error': testo,
                          'tentativi_rimasti': rimasti}
        return _persona(trovato), None
    finally:
        s.close()


def pin_gia_usato(s, pin: str, tranne_id: str = None) -> bool:
    for u in persone_con_pin(s):
        if u.id != tranne_id and pin_corrisponde(pin, u.pin_hash):
            return True
    return False


# ---------------------------------------------------------------------------
# Modalita' (transizione / protetto)
# ---------------------------------------------------------------------------
def _modalita_di_partenza() -> str:
    """Senza scelta esplicita: un'installazione GIA' IN USO (ci sono ordini o
    preventivi) parte in transizione, per non chiudere fuori l'officina il
    giorno dell'aggiornamento; una nuova parte protetta."""
    from .models import Order, Preventivo
    s = get_session()
    try:
        in_uso = s.query(Order.id).first() is not None or s.query(Preventivo.id).first() is not None
        return 'transizione' if in_uso else 'protetto'
    except Exception:
        return 'protetto'
    finally:
        s.close()


def modalita() -> str:
    try:
        if 'ft_modalita' in g:
            return g.ft_modalita
    except RuntimeError:
        pass
    from .database import ConfigManager
    m = ((ConfigManager.load_config() or {}).get('accesso') or {}).get('modalita')
    if m not in MODALITA:
        m = _modalita_di_partenza()
    try:
        g.ft_modalita = m
    except RuntimeError:
        pass
    return m


def imposta_modalita(m: str) -> dict:
    """Scrive la modalita' in app_config.json. Non passa da save_config: e'
    una chiave che dalle impostazioni generali non si puo' toccare."""
    if m not in MODALITA:
        return {'error': 'Modalità sconosciuta: %s' % m}
    from .database import ConfigManager
    return ConfigManager.save_riservata({'accesso': {'modalita': m}})


def fissa_modalita_di_partenza():
    """All'avvio: se in app_config.json la modalita' non c'e', ci scrive
    quella di partenza, cosi' non cambia da sola quando arriva il primo ordine."""
    try:
        from .database import ConfigManager
        acc = (ConfigManager.load_config() or {}).get('accesso') or {}
        if acc.get('modalita') not in MODALITA:
            m = _modalita_di_partenza()
            imposta_modalita(m)
            logger.info('accesso: modalita\' di partenza "%s"', m)
    except Exception:
        logger.exception('accesso: modalita\' di partenza non scritta')


# ---------------------------------------------------------------------------
# Sessioni
# ---------------------------------------------------------------------------
def fine_giornata(ora_utc: datetime = None) -> datetime:
    """Le prossime 3 di notte italiane, in UTC naive (anche col cambio d'ora)."""
    ora_utc = ora_utc or adesso()
    locale = orario.utc_a_locale(ora_utc)
    tre = datetime.combine(locale.date(), ORA_FINE_GIORNATA)
    if locale >= tre:
        tre = datetime.combine(locale.date() + timedelta(days=1), ORA_FINE_GIORNATA)
    return orario.locale_a_utc(tre)


def apri_sessione(tipo: str, user_id: str, device_id=None):
    """(segreto in chiaro, scadenza UTC). Il segreto va nel cookie e basta."""
    ora = adesso()
    scade = fine_giornata(ora) if tipo == 'ufficio' else ora + DURATA_ADMIN
    segreto = secrets.token_urlsafe(32)
    s = get_session()
    try:
        s.add(SessioneAccesso(id=str(uuid.uuid4()), token_hash=_hash_segreto(segreto), tipo=tipo,
                              device_id=device_id, user_id=user_id, creata_il=ora, scade_il=scade,
                              ultimo_uso_il=ora, ip=request.remote_addr if request else None))
        if tipo == 'ufficio':
            u = s.get(User, user_id)
            if u is not None:
                u.last_login = ora
        s.commit()
    finally:
        s.close()
    return segreto, scade


def chiudi_sessione(segreto: str):
    if not segreto:
        return
    s = get_session()
    try:
        r = s.query(SessioneAccesso).filter(SessioneAccesso.token_hash == _hash_segreto(segreto)).first()
        if r is not None and r.chiusa_il is None:
            r.chiusa_il = adesso()
            s.commit()
    finally:
        s.close()


def chiudi_sessioni(device_id: str = None, user_id: str = None):
    """Chiude tutte le sessioni aperte di un dispositivo o di una persona
    (revoca del dispositivo, persona disattivata, PIN cambiato)."""
    s = get_session()
    try:
        q = s.query(SessioneAccesso).filter(SessioneAccesso.chiusa_il.is_(None))
        if device_id:
            q = q.filter(SessioneAccesso.device_id == device_id)
        if user_id:
            q = q.filter(SessioneAccesso.user_id == user_id)
        n = 0
        for r in q.all():
            r.chiusa_il = adesso()
            n += 1
        s.commit()
        return n
    finally:
        s.close()


def _sessione_valida(s, segreto, tipo, device_id, ora):
    r = s.query(SessioneAccesso).filter(SessioneAccesso.token_hash == _hash_segreto(segreto)).first()
    if r is None or r.tipo != tipo or r.chiusa_il is not None or r.scade_il <= ora:
        return None
    # La sessione vale solo sul dispositivo dove si e' digitato il PIN: un
    # cookie copiato su un altro PC non porta la persona con se'.
    if (r.device_id or None) != (device_id or None):
        return None
    u = s.get(User, r.user_id)
    if u is None or not u.is_active or not u.pin_hash or u.e_postazione:
        return None
    if tipo == 'admin' and not u.e_admin:
        return None
    # Un PIN cambiato dopo l'apertura chiude la sessione.
    if u.pin_impostato_il and r.creata_il < u.pin_impostato_il:
        return None
    if r.ultimo_uso_il is None or (ora - r.ultimo_uso_il).total_seconds() > 60:
        r.ultimo_uso_il = ora
    return u


# ---------------------------------------------------------------------------
# Dispositivi
# ---------------------------------------------------------------------------
def _token_header() -> str:
    tok = (request.headers.get(_HEADER_TOKEN) or '').strip()
    if tok:
        return tok
    auth = (request.headers.get('Authorization') or '').strip()
    if auth.lower().startswith('bearer '):
        return auth[7:].strip()
    return ''


def _dispositivo(s, segreto):
    if not segreto:
        return None
    return s.query(DeviceToken).filter(DeviceToken.token_hash == _hash_segreto(segreto),
                                       DeviceToken.is_active == True).first()  # noqa: E712


def registra_dispositivo(stazione: str, nome: str, da: str, esistente_id: str = None):
    """Registra un dispositivo nuovo, o cambia stazione a uno gia' registrato.

    Ritorna (segreto in chiaro o None se e' lo stesso dispositivo, riga dict).
    Cambiando stazione si tiene lo stesso segreto ma si chiudono le sessioni:
    chi era entrato in Ufficio non deve ritrovarsi in Commerciale."""
    if stazione not in STAZIONI:
        raise ValueError('stazione sconosciuta: %s' % stazione)
    nome = (nome or '').strip()[:60] or STAZIONI[stazione]['nome']
    s = get_session()
    try:
        ora = adesso()
        if esistente_id:
            r = s.get(DeviceToken, esistente_id)
            if r is not None and r.is_active:
                r.scope = stazione
                r.label = nome
                s.commit()
                out = _dispositivo_dict(r)
                s.close()
                chiudi_sessioni(device_id=esistente_id)
                return None, out
        segreto = secrets.token_urlsafe(32)
        r = DeviceToken(id=str(uuid.uuid4()), token_hash=_hash_segreto(segreto), label=nome,
                        scope=stazione, is_active=True, created_at=ora, created_by=da,
                        last_used_at=ora)
        s.add(r)
        s.commit()
        return segreto, _dispositivo_dict(r)
    finally:
        s.close()


def _dispositivo_dict(r) -> dict:
    return {'id': r.id, 'nome': r.label, 'stazione': r.scope,
            'stazione_nome': (STAZIONI.get(r.scope) or {}).get('nome', r.scope),
            'attivo': bool(r.is_active),
            'registrato_il': orario.iso_utc(r.created_at),
            'registrato_da': r.created_by,
            'ultimo_uso': orario.iso_utc(r.last_used_at),
            'revocato_il': orario.iso_utc(r.revoked_at)}


# ---------------------------------------------------------------------------
# L'identita' della richiesta
# ---------------------------------------------------------------------------
@dataclass
class Identita:
    dispositivo: dict = None     # {'id','label','scope','via_header'}
    stazione: str = None
    persona: dict = None         # chi e' entrato col PIN (solo uffici)
    admin: dict = None           # amministratore col PIN appena digitato
    vecchio: dict = None         # postazione dichiarata dal browser (transizione)

    @property
    def utente_id(self) -> str:
        """Chi ha agito, per lo storico: la persona, altrimenti la postazione."""
        if self.persona:
            return self.persona['id']
        if self.stazione:
            return STAZIONI[self.stazione]['utente']
        return None

    @property
    def nome(self) -> str:
        if self.persona:
            return self.persona['nome']
        if self.stazione:
            return STAZIONI[self.stazione]['nome']
        return ''

    @property
    def registrato(self) -> bool:
        return self.dispositivo is not None


def identita() -> Identita:
    """Chi sta facendo questa richiesta. Calcolata una volta per richiesta."""
    if 'ft_identita' in g:
        return g.ft_identita
    ident = _risolvi()
    g.ft_identita = ident
    return ident


def _risolvi() -> Identita:
    ident = Identita()
    s = get_session()
    try:
        ora = adesso()
        riga, via_header = None, False
        cookie = request.cookies.get(COOKIE_DISP)
        if cookie:
            riga = _dispositivo(s, cookie)
        if riga is None:
            tok = _token_header()
            if tok:
                riga = _dispositivo(s, tok)
                if riga is not None:
                    via_header = True
                    # Tablet configurato col vecchio token: da qui in avanti
                    # il token sta anche nel cookie, cosi' le pagine aperte
                    # dentro un riquadro (foglio d'ordine, PDF) lo portano.
                    g.ft_cookie_dispositivo = tok
        if riga is not None and riga.scope in STAZIONI:
            ident.dispositivo = {'id': riga.id, 'label': riga.label, 'scope': riga.scope,
                                 'via_header': via_header}
            ident.stazione = riga.scope
            if riga.last_used_at is None or (ora - riga.last_used_at).total_seconds() > 60:
                riga.last_used_at = ora
            if ident.stazione in UFFICI:
                seg = request.cookies.get(COOKIE_SESS)
                u = _sessione_valida(s, seg, 'ufficio', riga.id, ora) if seg else None
                if u is not None:
                    ident.persona = _persona(u)
        seg_admin = request.cookies.get(COOKIE_ADMIN)
        if seg_admin:
            u = _sessione_valida(s, seg_admin, 'admin', riga.id if riga is not None else None, ora)
            if u is not None:
                ident.admin = _persona(u)
        if riga is None and modalita() == 'transizione':
            # Solo le postazioni, solo in transizione: e' il vecchio modo,
            # tenuto finche' i dispositivi non sono registrati. Mai un
            # amministratore, mai una persona. Oltre all'intestazione vale il
            # cookie COOKIE_VECCHIO (lo scrive la pagina): senza, i PDF, i
            # disegni e il foglio d'ordine aperti in un riquadro o in una
            # scheda nuova (niente intestazioni) non si aprirebbero piu'.
            # E' falsificabile come l'intestazione: per questo esiste solo
            # in transizione.
            uid = (request.headers.get('X-User-Id') or request.cookies.get(COOKIE_VECCHIO) or '').strip()
            if uid:
                u = s.get(User, uid)
                if u is not None and u.is_active and u.e_postazione and u.role in _STAZIONE_DA_RUOLO:
                    ident.stazione = _STAZIONE_DA_RUOLO[u.role]
                    ident.vecchio = {'id': u.id, 'nome': u.name}
        s.commit()
    except Exception:
        s.rollback()
        logger.exception('accesso: identita\' non risolta')
    finally:
        s.close()
    return ident


def dispositivo_compat() -> dict:
    """Il dispositivo nella forma che usava auth_device ({'id','label','scope'})."""
    ident = identita()
    if ident.dispositivo:
        label = ident.dispositivo['label']
        if ident.persona:
            label = '%s (%s)' % (label, ident.persona['nome'])
        return {'id': ident.dispositivo['id'], 'label': label, 'scope': ident.stazione}
    if ident.vecchio:
        return {'id': None, 'label': ident.vecchio['nome'], 'scope': ident.stazione}
    return {}


# ---------------------------------------------------------------------------
# Le regole delle rotte
# ---------------------------------------------------------------------------
Regola = namedtuple('Regola', 'stazioni admin pubblico vecchio_accesso')


def richiede(*stazioni, metodi=None, vecchio_accesso=True):
    """Dichiara chi puo' usare una rotta.

        @richiede('ufficio')                      solo la stazione Ufficio
        @richiede('ufficio', 'laser')             l'una o l'altra
        @richiede(ADMIN)                          solo un amministratore col PIN
        @richiede('ufficio', ADMIN)               l'Ufficio oppure un amministratore
        @richiede(PUBBLICO)                       nessun controllo
        @richiede('laser', metodi=('PUT',))       regola solo per quel metodo

    vecchio_accesso=False: nemmeno in transizione basta la postazione
    dichiarata dal browser (le ore, che gia' prima volevano il dispositivo).

    Non avvolge la funzione: le appende la regola, e il controllo lo fa
    `controlla_richiesta` prima di ogni richiesta. Cosi' l'elenco delle rotte
    si puo' verificare tutto in un test, e una rotta senza regola e' chiusa.
    """
    ignote = [x for x in stazioni if x not in STAZIONI and x not in (ADMIN, PUBBLICO)]
    if ignote:
        raise ValueError('stazioni sconosciute in @richiede: %s' % ignote)
    regola = Regola(frozenset(x for x in stazioni if x in STAZIONI), ADMIN in stazioni,
                    PUBBLICO in stazioni, vecchio_accesso)

    def deco(fn):
        regole = dict(getattr(fn, '_ft_regole', None) or {})
        for m in (metodi or ('*',)):
            regole[m.upper()] = regola
        fn._ft_regole = regole
        return fn
    return deco


def regola_per(view, metodo: str):
    regole = getattr(view, '_ft_regole', None) or {}
    if metodo == 'HEAD':
        metodo = 'GET'
    return regole.get(metodo) or regole.get('*')


def _no(codice_http, codice, testo):
    return jsonify({'success': False, 'codice': codice, 'error': testo}), codice_http


def valuta(regola: Regola, ident: Identita = None):
    """None se la richiesta passa, altrimenti la risposta di rifiuto."""
    if regola.pubblico:
        return None
    ident = ident or identita()
    if regola.admin and ident.admin:
        return None
    if not regola.stazioni:
        return _no(403, 'serve_pin_admin', 'Serve il PIN di un amministratore.')
    st = ident.stazione
    if st is None:
        return _no(401, 'non_registrato',
                   'Questo dispositivo non è registrato: chiedi all’amministrazione.')
    if ident.vecchio and not regola.vecchio_accesso:
        # Codice diverso da 'non_registrato': la pagina non deve rimandare
        # all'ingresso un dispositivo che col vecchio modo per il resto lavora.
        return _no(401, 'serve_dispositivo',
                   'Questa funzione vuole il dispositivo registrato: chiedi all’amministrazione.')
    if st not in regola.stazioni:
        if regola.admin:
            return _no(403, 'serve_pin_admin', 'Serve il PIN di un amministratore.')
        logger.warning('accesso negato: %s (%s) su %s %s', ident.nome, st, request.method, request.path)
        return _no(403, 'stazione_non_ammessa', 'Questa postazione non può farlo.')
    if STAZIONI[st]['con_pin'] and ident.persona is None:
        transizione = modalita() == 'transizione'
        if ident.vecchio and transizione:
            return None
        if ident.dispositivo and ident.dispositivo.get('via_header') and transizione:
            return None
        return _no(401, 'serve_pin', 'Inserisci il PIN per continuare.')
    return None


def controlla_richiesta(app):
    """Il controllo unico, prima di ogni richiesta /api."""
    if request.method == 'OPTIONS' or not request.path.startswith('/api/'):
        return None
    if request.endpoint is None or request.url_rule is None:
        return None          # 404 / 405: ci pensa Flask
    if not request.url_rule.rule.startswith('/api/'):
        # /api/qualcosa-che-non-esiste finisce sulla rotta dei file delle
        # pagine: non e' un'API, e risponde 404 da sola.
        return None
    view = app.view_functions.get(request.endpoint)
    if view is None:
        return None
    regola = regola_per(view, request.method)
    if regola is None:
        logger.error('rotta senza regola di accesso: %s %s', request.method, request.endpoint)
        return _no(403, 'senza_regola', 'Operazione non consentita.')
    return valuta(regola)


def imposta_cookie(resp, nome, valore, scade: datetime = None, max_age: int = None):
    resp.set_cookie(nome, valore, max_age=max_age,
                    expires=(scade.replace(tzinfo=None) if scade else None),
                    httponly=True, samesite='Strict', secure=request.is_secure, path='/')


def cancella_cookie(resp, nome):
    resp.delete_cookie(nome, path='/', samesite='Strict', httponly=True, secure=request.is_secure)


def dopo_la_richiesta(resp):
    """Copia nel cookie il vecchio token arrivato nell'intestazione."""
    tok = g.get('ft_cookie_dispositivo')
    if tok and not request.cookies.get(COOKIE_DISP):
        imposta_cookie(resp, COOKIE_DISP, tok, max_age=10 * 365 * 86400)
    return resp


def installa(app):
    """Aggancia il controllo all'applicazione. Va PRIMA degli altri
    before_request: chi non ha diritto non deve nemmeno sapere se un id e'
    valido o se un preventivo e' tecnico."""
    def _controllo():
        return controlla_richiesta(app)
    app.before_request_funcs.setdefault(None, []).insert(0, _controllo)
    app.after_request(dopo_la_richiesta)
