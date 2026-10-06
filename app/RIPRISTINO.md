# Backup e ripristino del database

## Dove sono le copie
- `database\backups\` contiene le copie automatiche:
  - **schedulato**: una all'ora;
  - **pre_migration**: una a ogni avvio del server.
- `database\backups\manuali\` contiene le copie fatte a mano: dal pulsante "Backup" della pagina, prima di un aggiornamento o prima di un ripristino. **Non vengono mai cancellate in automatico.**
- **Copia esterna** su un altro PC della rete (`remote_path` in `backup_config.json`, per esempio `\\PC-UFFICIO\FerroTrackBackup`). Si fa a ogni backup, solo se la cartella è raggiungibile, e contiene:
  - `database\`: le copie del database, ruotate come quelle qui;
  - `impostazioni\`: `app_config.json` (tariffe e impostazioni) e `backup_config.json`;
  - `disegni\`: tutta la cartella `uploads` (DXF, PDF di ordini e preventivi). Si copiano solo i file nuovi o cambiati e non si cancella mai niente.

  Se la cartella non risponde, il backup locale si fa lo stesso e l'amministrazione riceve un avviso, al massimo uno ogni 12 ore.

### Se si rompe il PC server
Su un PC nuovo, installato come in `INSTALLAZIONE.md` e con il server spento:
1. dalla copia esterna, prendi l'ultimo file di `database\` e mettilo come `database\scheduler.db`;
2. copia il contenuto di `impostazioni\` nella cartella `app`;
3. copia il contenuto di `disegni\` in `app\uploads`;
4. avvia il server.

Quante copie restano:
- **schedulato**: le ultime 48 (due giorni) più una al giorno per 30 giorni;
- **pre_migration**: le ultime 20.

Se il database non passa il controllo di integrità, il backup automatico **si ferma**, per non sostituire le copie buone. Nel log compare un errore e l'amministrazione riceve una notifica "Database da controllare".

## Fare una copia a mano
Dalla pagina, con il pulsante Backup. Oppure dalla cartella `app`:

```
.venv\Scripts\python.exe backup_db.py
```

## Ripristinare
1. **Spegni il server**: Gestione attività → `python` / `pythonw` che esegue `run.py`, oppure `powershell -ExecutionPolicy Bypass -File tools\installa_avvio.ps1 -Ferma`.
2. Dalla cartella `app`, guarda le copie:
   ```
   .venv\Scripts\python.exe tools\ripristina_backup.py
   ```
3. Scegli il numero della copia (di solito la più recente prima del problema):
   ```
   .venv\Scripts\python.exe tools\ripristina_backup.py 3
   ```
   Il programma:
   - si rifiuta se il server è ancora acceso;
   - controlla che la copia sia integra;
   - mette il database di adesso in `backups\manuali\scheduler_prima_ripristino_<data>.db`, quindi niente va perso;
   - chiede conferma: scrivi `SI`.
4. **Riaccendi il server** e controlla gli ultimi ordini e preventivi.

Tutto quello che è stato fatto dopo l'ora della copia va ripetuto. Il database "di prima" resta in `manuali\` se serve recuperare qualcosa.
