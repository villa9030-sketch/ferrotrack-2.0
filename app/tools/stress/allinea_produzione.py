# -*- coding: utf-8 -*-
"""Prima della prova notturna: porta in ferrotrack-stress (branch prove/stress)
il codice che gira in produzione, cosi' la notte prova quello vero.

    python allinea_produzione.py [--cartella <run_dir>] [--solo-controllo]

La produzione e' il commit attuale di ferrotrack-2.0 (qualunque branch abbia):
lo si LEGGE soltanto (git rev-parse), non si tocca niente li'. In
ferrotrack-stress si fa `git merge --no-edit <commit>`:
  - gia' dentro            -> niente da fare
  - modifiche non salvate  -> non si unisce (si prova il codice com'e')
  - conflitti              -> `git merge --abort`, si prova il codice com'e'
L'esito va in <run_dir>/allineamento.json (lo legge esegui_notte.py).
Uscita sempre 0: un merge non riuscito non deve fermare la prova.

A mano, dal terminale di VS Code, e' lo stesso di:
    cd ferrotrack-stress
    git merge --no-edit <commit di ferrotrack-2.0>
"""
import argparse
import json
import os
import subprocess
from datetime import datetime

QUI = os.path.dirname(os.path.abspath(__file__))
STRESS = os.path.dirname(os.path.dirname(os.path.dirname(QUI)))      # ferrotrack-stress
PRODUZIONE = os.path.join(os.path.dirname(STRESS), 'ferrotrack-2.0')
_NO_FINESTRA = 0x08000000 if os.name == 'nt' else 0


def git(cartella, *arg):
    r = subprocess.run(['git', '-C', cartella] + list(arg), capture_output=True, text=True,
                       encoding='utf-8', errors='replace', creationflags=_NO_FINESTRA, timeout=300)
    return r.returncode, (r.stdout or '') + (r.stderr or '')


def allinea(solo_controllo=False):
    es = {'quando': datetime.now().isoformat(timespec='seconds')}
    c, sha = git(PRODUZIONE, 'rev-parse', 'HEAD')
    if c:
        return {**es, 'esito': 'errore', 'dettagli': 'commit di produzione non letto: ' + sha[:300]}
    sha = sha.strip()
    _, ramo = git(PRODUZIONE, 'rev-parse', '--abbrev-ref', 'HEAD')
    es.update(produzione=sha, ramo_produzione=ramo.strip())
    _, prima = git(STRESS, 'rev-parse', 'HEAD')
    es['stress_prima'] = prima.strip()
    if git(STRESS, 'merge-base', '--is-ancestor', sha, 'HEAD')[0] == 0:
        return {**es, 'esito': 'gia_allineato'}
    _, sporco = git(STRESS, 'status', '--porcelain', '--untracked-files=no')
    if sporco.strip():
        return {**es, 'esito': 'non_unito', 'dettagli': 'modifiche non salvate in ferrotrack-stress:\n' + sporco[:800]}
    if solo_controllo:
        return {**es, 'esito': 'da_unire'}
    msg = 'Merge produzione (%s %s) nella prova notturna' % (ramo.strip(), sha[:7])
    c, out = git(STRESS, 'merge', '--no-edit', '-m', msg, sha)
    if c:
        git(STRESS, 'merge', '--abort')
        return {**es, 'esito': 'conflitti', 'dettagli': out[-1500:]}
    _, dopo = git(STRESS, 'rev-parse', 'HEAD')
    return {**es, 'esito': 'unito', 'stress_dopo': dopo.strip()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cartella', default='')
    ap.add_argument('--solo-controllo', action='store_true', help='dice solo se serve unire')
    a = ap.parse_args()
    try:
        es = allinea(a.solo_controllo)
    except Exception as e:
        es = {'esito': 'errore', 'dettagli': '%s: %s' % (type(e).__name__, e)}
    if a.cartella:
        os.makedirs(a.cartella, exist_ok=True)
        with open(os.path.join(a.cartella, 'allineamento.json'), 'w', encoding='utf-8') as f:
            json.dump(es, f, ensure_ascii=False, indent=1)
    print(json.dumps(es, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
