"""Confronto PREZZI prima/dopo le correzioni al preventivatore.

Serve a sapere, prima di mandare un'offerta a un cliente, di quanto cambiano i
numeri rispetto a come li calcolava il programma fino a ieri.

Le due correzioni che spostano i prezzi:
  1. il costo del materiale d'apporto (filo + gas) veniva calcolato e poi perso
     perche' finiva in un campo che nessuna somma leggeva;
  2. l'accettazione sommava TUTTI gli articoli (anche quelli gia' contati dentro
     un assieme) e ignorava assiemi, tubolari, piastre e costi generali.

Legge e basta: apre una COPIA del database, non tocca quello di lavoro.

    python app/tools/confronto_prezzi.py            # copia del db corrente
    python app/tools/confronto_prezzi.py <file.db>  # un altro database
"""
import os
import shutil
import sys
import tempfile

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)

os.environ['FERROTRACK_SKIP_DB_INIT'] = '1'
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from sqlalchemy import create_engine  # noqa: E402

from backend import models  # noqa: E402
from backend.models import Base  # noqa: E402
from backend import models_ore  # noqa: E402


def _num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def totale_vecchio(p: dict) -> float:
    """La formula che usava l'ACCETTAZIONE fino a ieri.

    Tutti gli articoli, compresi quelli dentro un assieme; niente assiemi,
    niente tubolari, niente piastre, niente costi generali, e senza il costo
    d'apporto (finiva in un campo che nessuno leggeva).
    """
    totale_pezzo = 0.0
    for a in (p.get('articoli') or []):
        base = (_num(a.get('costo_base_override'))
                if a.get('costo_base_override') is not None
                else (_num(a.get('costo_base_stimato')) or _num(a.get('costo_materiale'))))
        lavorazioni = (_num(a.get('costo_piega')) + _num(a.get('costo_saldatura'))
                       + _num(a.get('costo_filettatura')) + _num(a.get('costo_svasatura'))
                       + _num(a.get('costo_pulizia')))
        # il costo d'apporto NON c'era
        qta = int(a.get('quantita') or 1)
        totale_pezzo += (base + lavorazioni) * qta
    con_margine = totale_pezzo * (1 + _num(p.get('margine_pct')) / 100.0)
    return round(con_margine * int(p.get('quantita') or 1), 2)


def main():
    origine = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        _APP, 'database', 'scheduler.db')
    if not os.path.exists(origine):
        raise SystemExit(f'Database non trovato: {origine}')

    copia = os.path.join(tempfile.gettempdir(), 'confronto_prezzi.db')
    shutil.copy2(origine, copia)
    eng = create_engine('sqlite:///' + copia.replace(chr(92), '/'),
                        connect_args={'check_same_thread': False})
    models.engine = eng
    models.SessionLocal.configure(bind=eng)
    Base.metadata.create_all(bind=eng)
    # Il database di partenza puo' essere anteriore alle colonne nuove:
    # la copia viene allineata, l'originale non si tocca.
    from backend.migrations_ore import migrate_ore
    migrate_ore(eng)

    from backend.database import PreventivoManager, BarcodeManager  # noqa: E402
    from backend.preventivi.calcolo import calcola  # noqa: E402

    cfg = (BarcodeManager.load_config() or {}).get('preventivi_config') or {}
    generali = cfg.get('costo_generali_pct', 0)

    elenco = PreventivoManager.list(limit=500)
    if isinstance(elenco, dict):
        elenco = elenco.get('preventivi') or []

    print(f'Database: {os.path.basename(origine)} (copia di lavoro)')
    print(f'Costi generali in configurazione: {generali}%')
    print()
    print(f"{'Preventivo':<22} {'Cliente':<26} {'Stato':<10} "
          f"{'PRIMA':>12} {'ADESSO':>12} {'differenza':>14}")
    print('-' * 100)

    righe, tot_prima, tot_dopo = [], 0.0, 0.0
    for x in elenco:
        pid = x.get('id')
        p = PreventivoManager.get(pid)
        if not p:
            continue
        if not (p.get('articoli') or p.get('assiemi')
                or p.get('tubolari') or p.get('piastre')):
            continue
        vecchio = totale_vecchio(p)
        nuovo = calcola(p, cfg)['totale_lotto']
        if vecchio == 0 and nuovo == 0:
            continue
        righe.append((p, vecchio, nuovo))
        tot_prima += vecchio
        tot_dopo += nuovo

    righe.sort(key=lambda r: abs(r[2] - r[1]), reverse=True)
    a_zero = 0
    for p, vecchio, nuovo in righe:
        diff = nuovo - vecchio
        if vecchio == 0:
            a_zero += 1
            nota = 'era A ZERO!'
        else:
            nota = f"{'+' if diff >= 0 else ''}{diff / vecchio * 100:.0f}%"
        segno = '+' if diff >= 0 else ''
        etichetta = (p.get('numero_ordine_cliente') or p.get('id', '')[:8])
        print(f"{etichetta[:22]:<22} {(p.get('cliente') or '')[:26]:<26} "
              f"{(p.get('status') or ''):<10} "
              f"{vecchio:>12,.2f} {nuovo:>12,.2f} "
              f"{segno}{diff:>9,.2f}  {nota}")

    print('-' * 100)
    diff = tot_dopo - tot_prima
    pct = (diff / tot_prima * 100) if tot_prima else 0
    print(f"{'TOTALE su ' + str(len(righe)) + ' preventivi':<60} "
          f"{tot_prima:>12,.2f} {tot_dopo:>12,.2f} "
          f"{'+' if diff >= 0 else ''}{diff:>9,.2f} "
          f"({'+' if diff >= 0 else ''}{pct:.0f}%)")
    print()
    print('PRIMA  = come calcolava l\'accettazione fino a ieri')
    print('ADESSO = calcolo unico del server (apporto incluso, assiemi e')
    print('         tubolari contati, costi generali applicati)')
    print()
    print('I preventivi con la differenza piu\' grande sono quelli con assiemi,')
    print('tubolari o piastre: erano i piu\' sottostimati.')
    if a_zero:
        print()
        print(f'ATTENZIONE: {a_zero} preventivi risultavano a ZERO con la vecchia')
        print('formula. Sono quelli fatti di soli assiemi o tubolari, senza articoli')
        print('sciolti: accettandoli sarebbe nato un ordine a valore nullo.')

    eng.dispose()
    try:
        os.remove(copia)
    except OSError:
        pass


if __name__ == '__main__':
    main()
