"""Riaddestramento periodico del modello del motore con gli esempi confermati.

Da lanciare con il Python dello studio (scikit-learn), dalla cartella di questo file:

    banco_motore\\.venv_studio\\Scripts\\python.exe periodico.py [--workers 1] [--banco <cartella dump>]

Passi (vedi PERIODICO.md): copia del DB (sqlite backup, sola lettura), raccolta
esempi, dump del motore sugli esempi, area esterna e tenuti fuori, dump del banco
(solo se il motore e' cambiato), due addestramenti (solo Lantek / Lantek + esempi),
misure, verdetto. NON installa niente: se il modello e' migliore lo dice e lascia
il file in <uscita>/modelli_nuovo/modello_motore.json.
"""
import argparse
import datetime
import json
import os
import sqlite3
import subprocess
import sys

QUI = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(os.path.dirname(QUI))
RADICE = os.path.dirname(os.path.dirname(APP))
BANCO = os.environ.get('FT_BANCO') or os.path.join(RADICE, 'banco_motore')
PROD = os.environ.get('FT_PRODUZIONE') or os.path.join(RADICE, 'ferrotrack-2.0', 'app')
PY_APP = os.path.join(PROD, '.venv', 'Scripts', 'python.exe')
MODELLO_ATTUALE = os.path.join(APP, 'backend', 'preventivi', 'modello_motore.json')
REGRESSIONE = os.path.join(BANCO, 'liste', 'regressione_1500.txt')


def corri(cmd, log, env=None):
    print('>', ' '.join(os.path.basename(c) if os.sep in c else c for c in cmd[:3]), '...', flush=True)
    with open(log, 'a', encoding='utf-8') as fh:
        r = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, cwd=QUI,
                           env={**os.environ, 'PYTHONIOENCODING': 'utf-8', **(env or {})},
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if r.returncode:
        sys.exit(f'fallito ({r.returncode}): vedi {log}')


def impronta_motore():
    """Hash git del codice del motore (backend/preventivi) di questo worktree."""
    try:
        return subprocess.run(['git', 'rev-parse', 'HEAD:app/backend/preventivi'], cwd=APP, capture_output=True,
                              text=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout.strip()
    except Exception:
        return '?'


def dump(gt, out, nome, workers, log):
    corri([PY_APP, os.path.join(APP, 'tools', 'banco_lantek.py'), '--gt', gt, '--out', out, '--nome', nome,
           '--workers', str(workers), '--modello', 'dump'], log)
    return os.path.join(out, nome + '.jsonl')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--workers', type=int, default=1, help='processi del motore (il PC fa anche da server)')
    ap.add_argument('--banco', help='cartella con dump_dxf.jsonl / dump_dwg.jsonl / motore.txt da riusare')
    ap.add_argument('--uscita')
    ap.add_argument('--peso-es', type=float, default=3.0)
    a = ap.parse_args()
    oggi = datetime.date.today().strftime('%Y%m%d')
    out = a.uscita or os.path.join(BANCO, f'riaddestra_{oggi}')
    os.makedirs(out, exist_ok=True)
    log = os.path.join(out, 'periodico.log')

    # 1. copia del DB di produzione (backup sqlite, sola lettura)
    db = os.path.join(out, 'scheduler_copia.db')
    src = sqlite3.connect(f"file:{os.path.join(PROD, 'database', 'scheduler.db')}?mode=ro", uri=True)
    dst = sqlite3.connect(db)
    src.backup(dst)
    dst.close()
    src.close()

    # 2. esempi confermati
    corri([sys.executable, 'raccogli_esempi.py', '--esempi', os.path.join(PROD, 'dati_motore', 'esempi.jsonl'),
           '--db', db, '--gt', os.path.join(BANCO, 'lantek_gt_corretto.csv'), '--out', out], log)
    es = dump(os.path.join(out, 'esempi_gt.csv'), out, 'dump_es', a.workers, log)
    gt_es = os.path.join(out, 'esempi_gt_completo.csv')
    corri([sys.executable, 'prepara_esempi.py', '--gt', os.path.join(out, 'esempi_gt.csv'), '--dump', es,
           '--out', gt_es], log)

    # 3. banco completo: si rifa' solo se il codice del motore e' cambiato
    imp = impronta_motore()
    banco = a.banco or out
    f_imp = os.path.join(banco, 'motore.txt')
    if not (os.path.isfile(f_imp) and open(f_imp).read().strip() == imp):
        banco = out
        for f in ('dump_dxf.jsonl', 'dump_dwg.jsonl'):
            if os.path.isfile(os.path.join(out, f)):
                os.remove(os.path.join(out, f))
        dump(os.path.join(BANCO, 'lantek_gt_corretto.csv'), out, 'dump_dxf', a.workers, log)
        dump(os.path.join(BANCO, 'lantek_gt_dwg.csv'), out, 'dump_dwg', a.workers, log)
        open(os.path.join(out, 'motore.txt'), 'w').write(imp)
    dxf, dwg = os.path.join(banco, 'dump_dxf.jsonl'), os.path.join(banco, 'dump_dwg.jsonl')

    # 4. due addestramenti sugli stessi dump: solo Lantek, Lantek + esempi
    env = {'FT_GT_ESEMPI': gt_es, 'FT_BANCO': BANCO}
    corri([sys.executable, 'addestra.py', '--dump', dxf, '--dump-dwg', dwg, '--out', os.path.join(out, 'modelli_base')],
          os.path.join(out, 'addestra_base.txt'), env)
    corri([sys.executable, 'addestra.py', '--dump', dxf, '--dump-dwg', dwg, '--dump-es', es,
           '--peso-es', str(a.peso_es), '--out', os.path.join(out, 'modelli_nuovo')],
          os.path.join(out, 'addestra_nuovo.txt'), env)

    # 5. misure e verdetto
    os.environ.update(env)
    import valuta_modelli as vm
    att = vm.Modello('attuale', MODELLO_ATTUALE)
    nuovo = vm.Modello('nuovo', os.path.join(out, 'modelli_nuovo', 'modello_motore.json'))
    base_cv = vm.Modello('base_cv', os.path.join(out, 'modelli_base'))
    nuovo_cv = vm.Modello('nuovo_cv', os.path.join(out, 'modelli_nuovo'))
    reg = {l.strip().lower() for l in open(REGRESSIONE, encoding='utf-8') if l.strip()}
    misure = {
        'banco': vm.misura({'dxf': dxf, 'dwg': dwg}, [att, nuovo]),
        'regressione_1500': vm.misura({'dxf': dxf, 'dwg': dwg}, [att, nuovo], voluti=reg),
        'esempi_tenuti_fuori': vm.misura({'es': es}, [att, nuovo], solo_es='tenuti'),
        'banco_fold_tenuti_fuori': vm.misura({'dxf': dxf, 'dwg': dwg}, [base_cv, nuovo_cv]),
    }
    righe, ok = [], True
    for nome, r in misure.items():
        prima, dopo = ('base_cv', 'nuovo_cv') if 'fold' in nome else ('attuale', 'nuovo')
        p0 = r[prima]['sic_ok'] / max(r[prima]['sic'], 1)
        p1 = r[dopo]['sic_ok'] / max(r[dopo]['sic'], 1)
        nv = vm.nuovi_silenzi(r, prima, dopo)
        cond = p1 >= p0 - 1e-12 and not nv
        ok &= cond
        righe.append(vm.testo(r, nome))
        righe.append(f"  -> precisione dei sicuri {100 * p0:.2f}% -> {100 * p1:.2f}%, silenziosi nuovi {len(nv)} "
                     f"{nv[:10]}: {'OK' if cond else 'PEGGIO'}")
    b = misure['banco']
    meglio = (b['nuovo']['sic_ok'] > b['attuale']['sic_ok'] or b['nuovo']['geo'] > b['attuale']['geo']
              or len(b['nuovo']['silenzi']) < len(b['attuale']['silenzi']))
    verdetto = ok and meglio
    righe.append(f"VERDETTO: {'MIGLIORE: si puo proporre' if verdetto else 'NON migliore: tenere il modello attuale'}"
                 f" (condizioni {'ok' if ok else 'non rispettate'}, guadagno {'si' if meglio else 'no'})")
    testo = '\n'.join(righe)
    open(os.path.join(out, 'rapporto.txt'), 'w', encoding='utf-8').write(testo + '\n')
    json.dump(misure, open(os.path.join(out, 'misure.json'), 'w', encoding='utf-8'), default=list, indent=1)
    print(testo)


if __name__ == '__main__':
    main()
