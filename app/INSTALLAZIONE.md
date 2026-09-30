# Installare e aggiornare FerroTrack

Vale sia per questo PC sia per un altro PC Windows della rete. I comandi si danno da PowerShell nella cartella `app`.

## Installazione (la prima volta)
1. **Python 3.12** da python.org (spunta "Add to PATH").
2. **Codice:**
   ```
   git clone https://github.com/villa9030-sketch/ferrotrack-2.0.git
   cd ferrotrack-2.0\app
   git checkout stefano/refactor-ore-officina
   ```
3. **Ambiente Python:**
   ```
   py -3.12 -m venv .venv
   .venv\Scripts\python.exe -m pip install -r requirements.txt
   ```
4. **Dati dal vecchio PC**, con il server del vecchio PC spento:
   - `database\scheduler.db`: meglio l'ultima copia di `database\backups`, rinominata `scheduler.db`;
   - `app_config.json`: tariffe e impostazioni, non è su GitHub;
   - `backup_config.json` e la cartella `uploads\` (disegni e PDF di preventivi e ordini);
   - `.env`, se c'è (posta).
5. **Avvio automatico:**
   ```
   powershell -ExecutionPolicy Bypass -File tools\installa_avvio.ps1
   ```
   Con `-Accensione` (da amministratore) parte anche senza che nessuno acceda a Windows.
6. **Prova.** `tools\installa_avvio.ps1 -Avvia`, poi apri `http://<indirizzo del PC>:5000`. Per l'indirizzo, `FIND_IP.bat`.
7. **Copia esterna dei backup.** In `backup_config.json` imposta `remote_path` su una cartella di un altro disco o NAS che esista già.

## Aggiornamento da GitHub
1. Copia di sicurezza: `.venv\Scripts\python.exe backup_db.py`. Finisce in `database\backups\manuali`.
2. `powershell -ExecutionPolicy Bypass -File tools\installa_avvio.ps1 -Ferma`
3. `git pull`
4. Solo se è cambiato `requirements.txt`: `.venv\Scripts\python.exe -m pip install -r requirements.txt`
5. `powershell -ExecutionPolicy Bypass -File tools\installa_avvio.ps1 -Avvia`
6. Controlla la fine di `logs\ferrotrack.log`: nessun ERROR, e la riga "Snapshot pre-migrazione".

Se qualcosa va storto, vedi `RIPRISTINO.md`.

## Avvio a mano
- `START_FERROTRACK.bat`: senza finestra, come in produzione.
- `START_FERROTRACK_CONSOLE.bat`: con la finestra dei messaggi, per capire un problema.

Se è già acceso, non parte una seconda volta. Il debug di Flask (`FLASK_DEBUG=true`) risponde solo da questo PC, mai in rete.
