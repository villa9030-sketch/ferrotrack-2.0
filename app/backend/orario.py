"""Ora UTC salvata, ora LOCALE mostrata: le due regole in un posto solo.

I timestamp si salvano con `datetime.utcnow()` (naive, senza fuso). Finche' si
serializzavano con un semplice `.isoformat()`, il browser li leggeva come ora
LOCALE: un taglio segnato alle 22:19 compariva alle 20:19, e vicino alla
mezzanotte "tagliati oggi" e le date di DDT/consegna cadevano sul giorno
sbagliato.

Le regole:

 * un ISTANTE (quando e' successo qualcosa) resta salvato in UTC ma esce con
   la "Z" finale: il browser lo converte da solo nell'ora locale. Si usa "Z" e
   non "+00:00" perche' qualche pagina aggiunge gia' una "Z" quando manca, e
   riconosce solo quella forma.
 * una DATA di calendario (consegna prevista, data DDT, data fattura) non e'
   un istante: si salva a mezzanotte e si serializza SENZA fuso, cosi' le
   pagine che ne prendono i primi 10 caratteri continuano a funzionare.
 * "oggi" lato server e' il giorno di Europe/Rome, non quello di Greenwich.
"""
from datetime import date, datetime, time, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    FUSO = ZoneInfo('Europe/Rome')
except Exception:  # pragma: no cover - tzdata assente: si usa l'ora del PC
    FUSO = None


def iso_utc(dt):
    """ISO di un istante salvato in UTC naive, con la 'Z' finale. None se vuoto."""
    if not dt:
        return None
    if isinstance(dt, datetime) and dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat() + 'Z'


def ora_locale() -> datetime:
    """Adesso, in ora locale (naive)."""
    if FUSO is not None:
        return datetime.now(FUSO).replace(tzinfo=None)
    return datetime.now()


def oggi_locale() -> date:
    """La data di OGGI in Italia (non UTC: fra le 00 e le 02 sarebbe ieri)."""
    return ora_locale().date()


def utc_a_locale(dt):
    """Converte un istante UTC naive in ora locale naive."""
    if not dt:
        return dt
    aware = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    if FUSO is not None:
        return aware.astimezone(FUSO).replace(tzinfo=None)
    return aware.astimezone().replace(tzinfo=None)


def locale_a_utc(dt):
    """Converte un'ora locale naive nell'istante UTC naive corrispondente."""
    if not dt:
        return dt
    if FUSO is not None:
        aware = dt.replace(tzinfo=FUSO)
    else:
        aware = dt.astimezone()
    return aware.astimezone(timezone.utc).replace(tzinfo=None)


def data_locale(dt):
    """Il giorno di calendario italiano in cui e' avvenuto un istante UTC."""
    if not dt:
        return None
    return utc_a_locale(dt).date()


def giorno_locale_in_utc(giorno: date = None):
    """Estremi [inizio, fine) di un giorno italiano, espressi in UTC naive.

    Serve a filtrare nel database (dove tutto e' UTC) cio' che e' successo
    "oggi" per chi lavora in officina.
    """
    giorno = giorno or oggi_locale()
    inizio = locale_a_utc(datetime.combine(giorno, time.min))
    fine = locale_a_utc(datetime.combine(giorno + timedelta(days=1), time.min))
    return inizio, fine


def data_calendario(valore=None):
    """Una data di calendario da salvare: mezzanotte, senza fuso.

    Accetta 'YYYY-MM-DD' (o un ISO piu' lungo, di cui conta il giorno), una
    date o una datetime; senza valore e' OGGI in Italia. Solleva ValueError se
    la stringa non e' una data.
    """
    if valore is None or valore == '':
        return datetime.combine(oggi_locale(), time.min)
    if isinstance(valore, datetime):
        return datetime.combine(valore.date(), time.min)
    if isinstance(valore, date):
        return datetime.combine(valore, time.min)
    testo = str(valore).strip()
    return datetime.combine(datetime.strptime(testo[:10], '%Y-%m-%d').date(), time.min)


def iso_data(dt):
    """ISO di una data di calendario (salvata a mezzanotte, senza fuso)."""
    return dt.isoformat() if dt else None
