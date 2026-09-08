"""CRUD operations for Order management"""
from datetime import datetime, timedelta
from sqlalchemy import func, or_
from sqlalchemy.orm import joinedload
from sqlalchemy.orm.attributes import flag_modified
from .models import (
    Order, OrderFile, ProcessingStep, OrderNotification, PhaseSession,
    FaseCorrente, get_session, User, AuditLog, Notification, OperatorClient,
    PhaseDelegation, SupportRequest, OfficinaScan, Pistola,
    Preventivo, PreventivoArticolo, PreventivoAssieme, PreventivoTubolare, PreventivoPiastra,
)
import uuid
import json
import logging

logger = logging.getLogger(__name__)

class OrderManager:
    """Gestore operazioni su ordini con articoli"""

    @staticmethod
    def add_order_file(order_id: str, filename: str, filepath: str,
                       file_type: str = 'DXF', sha256: str | None = None) -> dict:
        """Registra un file (DXF/PDF) associato a un ordine, con impronta SHA256
        opzionale (integrità: file preventivato ≡ file prodotto)."""
        session = get_session()
        try:
            of = OrderFile(
                id=str(uuid.uuid4()),
                order_id=order_id,
                filename=filename,
                filepath=filepath,
                file_type=file_type,
                sha256=sha256,
            )
            session.add(of)
            session.commit()
            return {'success': True, 'id': of.id}
        except Exception as e:
            session.rollback()
            return {'error': str(e)}
        finally:
            session.close()

    @staticmethod
    def _format_duration(td: timedelta) -> str:
        """Formatta un timedelta in stringa leggibile (es: '2h 30min')"""
        if not td:
            return "0min"
        total_seconds = int(td.total_seconds())
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60

        if hours > 0 and minutes > 0:
            return f"{hours}h {minutes}min"
        elif hours > 0:
            return f"{hours}h"
        else:
            return f"{minutes}min"

    @staticmethod
    def _calculate_order_total_time(order_id: str, session) -> str:
        """Calcola il tempo totale di tutte le fasi completate di un ordine"""
        try:
            steps = session.query(ProcessingStep).filter(
                ProcessingStep.order_id == order_id,
                ProcessingStep.timestamp_inizio.isnot(None),
                ProcessingStep.timestamp_fine.isnot(None)
            ).all()

            if not steps:
                return "0min"

            total_duration = timedelta(0)
            for step in steps:
                duration = step.timestamp_fine - step.timestamp_inizio
                total_duration += duration

            return OrderManager._format_duration(total_duration)
        except Exception:
            return "Errore calcolo"

    @staticmethod
    def _calculate_total_hours(order_id: str, session) -> float:
        """Calcola le ore totali di lavorazione officina (escluso LASER)"""
        try:
            steps = session.query(ProcessingStep).filter(
                ProcessingStep.order_id == order_id,
                ProcessingStep.fase != "LASER",
                ProcessingStep.timestamp_inizio.isnot(None),
                ProcessingStep.timestamp_fine.isnot(None)
            ).all()

            total_seconds = 0
            for step in steps:
                duration = step.timestamp_fine - step.timestamp_inizio
                total_seconds += int(duration.total_seconds())

            return total_seconds / 3600.0
        except Exception:
            return 0.0

    @staticmethod
    def create_order(cliente: str, data_consegna: str,
                     numero_ordine: str = "", note: str = "",
                     destinazione: str = None) -> Order:
        """Crea un nuovo ordine.

        Non assegna fase né operatore: il sistema barcode rileva chi
        scansiona, e la chiusura è una decisione del capo.
        Il parametro 'destinazione' è kept per backward-compat di chiamate
        legacy ma viene IGNORATO.
        """
        session = get_session()
        try:
            order = Order(
                id=str(uuid.uuid4()),
                cliente=cliente,
                numero_ordine=numero_ordine,
                data_consegna=datetime.fromisoformat(data_consegna),
                fase_corrente=None,        # legacy column, no fase nel nuovo flusso
                operatore_assegnato=None,  # legacy, no assegnazione automatica
                status='RICEVUTO',
                note=note,
            )
            session.add(order)
            session.commit()
            session.refresh(order)
            return order
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()

    @staticmethod
    def next_numero_preventivo() -> str:
        """Genera prossimo numero progressivo PREV-{anno}-{NNNN} per ordini da preventivo.

        Query MAX numero_ordine LIKE 'PREV-{anno}-%', estrae il progressivo,
        ritorna il successivo zero-padded a 4 cifre.
        """
        import re
        session = get_session()
        try:
            year = datetime.utcnow().year
            prefix = f'PREV-{year}-'
            rows = session.query(Order.numero_ordine).filter(
                Order.numero_ordine.like(prefix + '%')
            ).all()
            max_n = 0
            pattern = re.compile(rf'^PREV-{year}-(\d+)$')
            for (num,) in rows:
                m = pattern.match(num or '')
                if m:
                    max_n = max(max_n, int(m.group(1)))
            return f'{prefix}{max_n + 1:04d}'
        finally:
            session.close()

    @staticmethod
    def create_order_from_preventivo(preventivo: dict, data_consegna: str,
                                     numero_ordine: str, note_aggiuntive: str = '') -> Order:
        """Crea Order FerroTrack a partire da un preventivo accettato.

        Setta origine='PREVENTIVO' + preventivo_id_origine. Identico per il resto
        al flusso "Elena carica PDF" (cartellino + notifica capi sono nel chiamante).
        """
        session = get_session()
        try:
            note_full = preventivo.get('note') or ''
            if note_aggiuntive:
                note_full = (note_full + '\n--\n' + note_aggiuntive).strip()
            order = Order(
                id=str(uuid.uuid4()),
                cliente=preventivo['cliente'],
                numero_ordine=numero_ordine,
                data_consegna=datetime.fromisoformat(data_consegna),
                fase_corrente=None,
                operatore_assegnato=None,
                status='RICEVUTO',
                note=note_full,
                origine='PREVENTIVO',
                preventivo_id_origine=preventivo['id'],
                # Il prezzo concordato accompagna l'ordine: senza questo il
                # riepilogo dell'ufficio mostrava commesse senza valore.
                prezzo_quotato=_totale_concordato(preventivo),
            )
            session.add(order)
            session.commit()
            session.refresh(order)
            return order
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()

    @staticmethod
    def soft_delete_order(order_id: str, motivo: str = '') -> bool:
        """Annulla un ordine senza cancellarlo: serve a rimediare a un ordine
        creato durante un'operazione poi fallita. I dati restano consultabili."""
        session = get_session()
        try:
            order = session.query(Order).filter(Order.id == order_id).first()
            if not order:
                return False
            order.is_deleted = True
            if motivo:
                order.note = ((order.note or '') + chr(10) + '[annullato] ' + motivo).strip()
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.exception('soft_delete_order fallita per %s: %s', order_id, e)
            return False
        finally:
            session.close()

    @staticmethod
    def close_order(order_id: str, user_id: str = '') -> dict:
        """Capo officina dichiara 'lavoro fisico finito': l'ordine passa a
        DA_FATTURARE e finisce nella tab di Elena per la chiusura amministrativa.

        - Setta status='DA_FATTURARE' (NON CHIUSO — quello è dopo DDT/fattura)
        - Chiude tutte le OfficinaScan ancora aperte (motivo='ordine_chiuso')
        - Audit log
        """
        session = get_session()
        try:
            order = session.query(Order).filter(Order.id == order_id).first()
            if not order:
                return {'success': False, 'error': 'Ordine non trovato'}
            if order.status in ('DA_FATTURARE', 'CHIUSO', 'SPEDITO'):
                return {'success': False, 'error': f'Ordine già in stato {order.status}'}
            order.status = 'DA_FATTURARE'
            session.commit()
        except Exception as e:
            session.rollback()
            return {'success': False, 'error': str(e)}
        finally:
            session.close()

        # Chiude scan aperte
        try:
            n = BarcodeManager.close_open_scans(order_id, motivo='ordine_chiuso')
        except Exception as exc:
            logger.warning('close_open_scans failed in close_order: %s', exc)
            n = 0

        try:
            AuditManager.log(
                user_id=user_id, action='CLOSE_ORDER',
                entity_type='order', entity_id=order_id,
                detail=f'Lavorazione finita → DA_FATTURARE, scan chiuse: {n}',
            )
        except Exception:
            pass
        return {'success': True, 'order_id': order_id, 'scan_chiuse': n, 'nuovo_status': 'DA_FATTURARE'}

    @staticmethod
    def mark_laser_done(order_id: str, user_id: str = '') -> dict:
        """Marca il taglio laser come completato: da questo momento gli operai
        officina possono scansionare il cartellino col barcode.
        """
        session = get_session()
        try:
            order = session.query(Order).filter(Order.id == order_id).first()
            if not order:
                return {'success': False, 'error': 'Ordine non trovato'}
            if order.taglio_completato:
                return {'success': False, 'error': 'Taglio già marcato come completato'}
            order.taglio_completato = True
            order.data_taglio_completato = datetime.utcnow()
            order.taglio_completato_da = user_id or None
            session.commit()
            try:
                AuditManager.log(
                    user_id=user_id, action='MARK_LASER_DONE',
                    entity_type='order', entity_id=order_id, detail='Taglio completato',
                )
            except Exception:
                pass
            return {'success': True, 'order_id': order_id}
        except Exception as e:
            session.rollback()
            return {'success': False, 'error': str(e)}
        finally:
            session.close()

    @staticmethod
    def mark_laser_undone(order_id: str, user_id: str = '') -> dict:
        """Rollback: l'ordine torna 'da tagliare'. Utile in caso di errore."""
        session = get_session()
        try:
            order = session.query(Order).filter(Order.id == order_id).first()
            if not order:
                return {'success': False, 'error': 'Ordine non trovato'}
            order.taglio_completato = False
            order.data_taglio_completato = None
            order.taglio_completato_da = None
            session.commit()
            try:
                AuditManager.log(
                    user_id=user_id, action='MARK_LASER_UNDONE',
                    entity_type='order', entity_id=order_id, detail='Taglio annullato',
                )
            except Exception:
                pass
            return {'success': True, 'order_id': order_id}
        except Exception as e:
            session.rollback()
            return {'success': False, 'error': str(e)}
        finally:
            session.close()

    @staticmethod
    def alert_ordini_taglio_fermo(soglia_ore: float = 4.0, work_start: int = 7, work_end: int = 19) -> int:
        """Workflow A — rete di sicurezza: notifica ai CAPI gli ordini accettati/ricevuti
        da più di `soglia_ore` che NON hanno ancora il taglio confermato
        (`taglio_completato=False`). UNA sola notifica per ordine (dedup su
        notification_type='taglio_fermo'), così niente spam. Alert inviato solo in
        orario di lavoro [work_start, work_end) per evitare notifiche notturne.
        Pensato per girare periodicamente da un thread in run.py. Ritorna il numero
        di NUOVI ordini segnalati."""
        # Senza pistole nessuno auto-conferma piu' il taglio: l'alert scatterebbe
        # su OGNI ordine per sempre. Il controllo degli ordini fermi passa a
        # get_ordini_sospetti_finiti (criterio basato sull'anzianita' dell'ordine).
        if not BarcodeManager.pistole_attive():
            return 0
        now = datetime.utcnow()
        if not (work_start <= now.hour < work_end):
            return 0
        soglia = now - timedelta(hours=soglia_ore)
        session = get_session()
        creati = 0
        try:
            fermi = session.query(Order).filter(
                Order.is_deleted == False,          # noqa: E712
                Order.taglio_completato == False,   # noqa: E712
                Order.status.notin_(['CHIUSO', 'SPEDITO']),
                Order.data_ricezione < soglia,
            ).all()
            if not fermi:
                return 0
            capi = session.query(User).filter(
                User.is_capo == True,               # noqa: E712
                User.is_active == True,             # noqa: E712
            ).all()
            if not capi:
                return 0
            for order in fermi:
                gia = session.query(Notification).filter(
                    Notification.order_id == order.id,
                    Notification.notification_type == 'taglio_fermo',
                    Notification.is_deleted == False,   # noqa: E712
                ).first()
                if gia:
                    continue
                ore = int((now - order.data_ricezione).total_seconds() // 3600)
                num = order.numero_ordine or order.id[:8]
                for capo in capi:
                    session.add(Notification(
                        id=str(uuid.uuid4()),
                        user_id=capo.id,
                        order_id=order.id,
                        title='Ordine fermo: taglio non confermato',
                        message=f'Ordine #{num} ({order.cliente}) ricevuto da ~{ore}h e non ancora '
                                f'tagliato/scansionato. Verificare col laser.',
                        notification_type='taglio_fermo',
                        notification_category='attiva',
                        is_read=False,
                        is_deleted=False,
                    ))
                creati += 1
            session.commit()
            if creati:
                logger.info('Alert taglio-fermo: %d ordini segnalati ai capi', creati)
            return creati
        except Exception as e:
            session.rollback()
            logger.error('alert_ordini_taglio_fermo: %s', e)
            return 0
        finally:
            session.close()

    @staticmethod
    def alert_consegne_a_rischio(giorni: int = 1, work_start: int = 7, work_end: int = 19) -> int:
        """Vigilanza: notifica ai CAPI gli ordini con consegna imminente/scaduta ancora NON
        completati (status non in DA_FATTURARE/CHIUSO/SPEDITO). Prima esisteva solo come
        conteggio KPI/dashboard (pull) → un ritardo passava inosservato se nessuno guardava.
        Una notifica per ordine (dedup 'consegna_rischio'), solo in orario lavorativo."""
        now = datetime.utcnow()
        if not (work_start <= now.hour < work_end):
            return 0
        limite = now + timedelta(days=giorni)
        session = get_session()
        creati = 0
        try:
            a_rischio = session.query(Order).filter(
                Order.is_deleted == False,  # noqa: E712
                Order.status.notin_(['DA_FATTURARE', 'CHIUSO', 'SPEDITO']),
                Order.data_consegna != None,  # noqa: E711
                Order.data_consegna <= limite,
            ).all()
            if not a_rischio:
                return 0
            capi = session.query(User).filter(
                User.is_capo == True, User.is_active == True,  # noqa: E712
            ).all()
            if not capi:
                return 0
            for order in a_rischio:
                gia = session.query(Notification).filter(
                    Notification.order_id == order.id,
                    Notification.notification_type == 'consegna_rischio',
                    Notification.is_deleted == False,  # noqa: E712
                ).first()
                if gia:
                    continue
                num = order.numero_ordine or order.id[:8]
                scaduta = order.data_consegna < now
                quando = 'SCADUTA' if scaduta else 'in scadenza'
                dstr = order.data_consegna.strftime('%d/%m') if order.data_consegna else '?'
                for capo in capi:
                    session.add(Notification(
                        id=str(uuid.uuid4()), user_id=capo.id, order_id=order.id,
                        title=f'Consegna {quando}: ordine non pronto',
                        message=f'Ordine #{num} ({order.cliente}) consegna {dstr}, non ancora completato.',
                        notification_type='consegna_rischio', notification_category='attiva',
                        is_read=False, is_deleted=False,
                    ))
                creati += 1
            session.commit()
            if creati:
                logger.info('Alert consegne a rischio: %d ordini segnalati ai capi', creati)
            return creati
        except Exception as e:
            session.rollback()
            logger.error('alert_consegne_a_rischio: %s', e)
            return 0
        finally:
            session.close()

    @staticmethod
    def alert_sospetti_finiti_push(work_start: int = 7, work_end: int = 19) -> int:
        """Vigilanza: trasforma in notifica PUSH la lista 'sospetti finiti' (ordini
        probabilmente lavorati ma mai chiusi con close_order), prima solo badge pull.
        Notifica capi + Impiegata. Una notifica per ordine (dedup 'sospetto_finito')."""
        now = datetime.utcnow()
        if not (work_start <= now.hour < work_end):
            return 0
        try:
            sospetti = BarcodeManager.get_ordini_sospetti_finiti()
        except Exception as e:
            logger.error('alert_sospetti_finiti_push (lookup): %s', e)
            return 0
        if not sospetti:
            return 0
        session = get_session()
        creati = 0
        try:
            dest = session.query(User).filter(
                User.is_active == True,  # noqa: E712
            ).filter(
                (User.is_capo == True) | (User.role == 'Impiegata')  # noqa: E712
            ).all()
            if not dest:
                return 0
            for s in sospetti:
                oid = s.get('id')
                gia = session.query(Notification).filter(
                    Notification.order_id == oid,
                    Notification.notification_type == 'sospetto_finito',
                    Notification.is_deleted == False,  # noqa: E712
                ).first()
                if gia:
                    continue
                num = s.get('numero_ordine'); cli = s.get('cliente') or ''
                gg = s.get('giorni_inattivo')
                for u in dest:
                    session.add(Notification(
                        id=str(uuid.uuid4()), user_id=u.id, order_id=oid,
                        title='Ordine forse finito ma non chiuso',
                        message=f'Ordine #{num} ({cli}) fermo da ~{gg}g dopo il taglio. Chiudere se completato.',
                        notification_type='sospetto_finito', notification_category='attiva',
                        is_read=False, is_deleted=False,
                    ))
                creati += 1
            session.commit()
            if creati:
                logger.info('Alert sospetti-finiti: %d ordini segnalati (capi+impiegata)', creati)
            return creati
        except Exception as e:
            session.rollback()
            logger.error('alert_sospetti_finiti_push: %s', e)
            return 0
        finally:
            session.close()

    @staticmethod
    def get_order(order_id: str) -> Order:
        """Recupera un ordine per ID"""
        session = get_session()
        try:
            order = session.query(Order).filter(Order.id == order_id).first()
            if order:
                # Carica relazioni
                for step in order.processing_steps:
                    pass  # Force load
            return order
        finally:
            session.close()
    
    @staticmethod
    def get_all_orders(cliente: str = None) -> list:
        """Recupera ordini non eliminati, opzionalmente filtrati per cliente"""
        session = get_session()
        try:
            query = session.query(Order).filter(Order.is_deleted == False)
            if cliente:
                query = query.filter(Order.cliente == cliente)
            return query.all()
        finally:
            session.close()
    
    @staticmethod
    def get_all_orders_dict(cliente: str = None, status: str = None,
                            fase_corrente: str = None, operatore: str = None,
                            order_ids: list = None) -> list:
        """Recupera ordini non eliminati come dizionari con filtri per il nuovo workflow"""
        session = get_session()
        try:
            query = session.query(Order).filter(Order.is_deleted == False)
            if order_ids:
                query = query.filter(Order.id.in_(order_ids))
            if cliente:
                query = query.filter(Order.cliente == cliente)
            if status:
                query = query.filter(Order.status == status)
            if fase_corrente:
                query = query.filter(Order.fase_corrente == fase_corrente)
            if operatore:
                query = query.filter(Order.operatore_assegnato == operatore)

            orders = query.options(
                joinedload(Order.files),
                joinedload(Order.processing_steps)
            ).order_by(Order.data_consegna.asc()).all()

            # De-duplica ordini (joinedload può duplicare)
            seen_ids = set()
            unique_orders = []
            for o in orders:
                if o.id not in seen_ids:
                    seen_ids.add(o.id)
                    unique_orders.append(o)
            orders = unique_orders

            # Pre-carica tutte le sessioni per gli ordini trovati
            order_ids = [o.id for o in orders]
            all_sessions = session.query(PhaseSession).filter(
                PhaseSession.order_id.in_(order_ids)
            ).all() if order_ids else []
            step_sessions = {}
            for ps in all_sessions:
                step_sessions.setdefault(ps.step_id, []).append(ps)

            # Pre-carica tutti gli utenti (evita N+1 queries)
            all_users = session.query(User).all()
            name_to_id = {u.name: u.id for u in all_users}
            id_to_name = {u.id: u.name for u in all_users}

            # Pre-carica tutte le support requests attive
            all_support = session.query(SupportRequest).filter(
                SupportRequest.order_id.in_(order_ids),
                SupportRequest.stato.in_(['pending', 'accepted'])
            ).all() if order_ids else []
            support_by_order = {}
            for sr in all_support:
                support_by_order.setdefault(sr.order_id, []).append(sr)

            result = []
            for order in orders:
                pdf_file = None
                dxf_files = []
                if order.files:
                    for f in order.files:
                        if f.file_type == 'PDF':
                            pdf_file = f.filename
                        elif f.file_type == 'DXF':
                            dxf_files.append({'filename': f.filename})

                operatore_nome = id_to_name.get(order.operatore_assegnato)

                steps_data = []
                for ps in (order.processing_steps or []):
                    ss = step_sessions.get(ps.id, [])
                    closed = [x for x in ss if x.timestamp_fine]
                    cumul = sum(int((x.timestamp_fine - x.timestamp_inizio).total_seconds()) for x in closed)
                    active_list = [x for x in ss if x.timestamp_fine is None]
                    active = active_list[0] if active_list else None

                    # Info per-operatore: tempo e stato indipendenti (chiave = user ID)
                    op_groups = {}
                    for x in ss:
                        op = x.operatore or 'unknown'
                        op_groups.setdefault(op, []).append(x)
                    operatori_info = {}
                    for op_name, op_ss in op_groups.items():
                        op_closed = [x for x in op_ss if x.timestamp_fine]
                        op_active = [x for x in op_ss if x.timestamp_fine is None]
                        op_cumul = sum(int((x.timestamp_fine - x.timestamp_inizio).total_seconds()) for x in op_closed)
                        op_key = name_to_id.get(op_name, op_name)  # Usa ID utente come chiave
                        op_confermato = any(x.tipo_chiusura == 'totale' for x in op_ss)
                        operatori_info[op_key] = {
                            'sessione_attiva': len(op_active) > 0,
                            'sessione_attiva_inizio': op_active[0].timestamp_inizio.isoformat() if op_active else None,
                            'in_pausa': len(op_active) == 0 and len(op_closed) > 0,
                            'confermato': op_confermato,
                            'tempo_cumulativo_secondi': op_cumul,
                            'sessioni_count': len(op_ss)
                        }

                    steps_data.append({
                        'id': ps.id,
                        'fase': ps.fase,
                        'timestamp_inizio': ps.timestamp_inizio.isoformat() if ps.timestamp_inizio else None,
                        'timestamp_fine': ps.timestamp_fine.isoformat() if ps.timestamp_fine else None,
                        'operatore': ps.operatore,
                        'fase_successiva': ps.fase_successiva,
                        'completamento_parziale': ps.completamento_parziale,
                        'note': ps.note,
                        'sessione_attiva': len(active_list) > 0,
                        'sessione_attiva_inizio': active.timestamp_inizio.isoformat() if active else None,
                        'sessioni_count': len(ss),
                        'tempo_cumulativo_secondi': cumul,
                        'in_pausa': (ps.timestamp_fine is None and len(ss) > 0 and len(active_list) == 0),
                        'sessioni_attive': [{'operatore': a.operatore, 'inizio': a.timestamp_inizio.isoformat()} for a in active_list],
                        'operatori_info': operatori_info,
                    })

                # Support requests attivi per quest'ordine (pre-caricati)
                support_data = []
                for sr in support_by_order.get(order.id, []):
                    support_data.append({
                        'id': sr.id,
                        'operatore_principale': sr.operatore_principale,
                        'nome_principale': id_to_name.get(sr.operatore_principale, ''),
                        'operatore_supporto': sr.operatore_supporto,
                        'nome_supporto': id_to_name.get(sr.operatore_supporto, ''),
                        'stato': sr.stato,
                        'forzata': sr.forzata
                    })

                # Info lotti figli (pre-caricati)
                lotti_data = []
                lotti_count = 0
                all_lotti_completed = False
                if order.lotto_numero and order.lotto_numero > 0:
                    child_lotti = session.query(Order).filter(
                        Order.parent_order_id == order.id,
                        Order.is_deleted == False
                    ).order_by(Order.lotto_numero.asc()).all()
                    for l in child_lotti:
                        lotti_data.append({
                            'id': l.id,
                            'lotto_numero': l.lotto_numero,
                            'lotto_nome': l.lotto_nome or '',
                            'fase_corrente': l.fase_corrente,
                            'status': l.status
                        })
                    lotti_count = len(child_lotti) + 1
                    all_phases = [l.fase_corrente for l in child_lotti] + [order.fase_corrente]
                    all_lotti_completed = all(f == 'COMPLETATO' for f in all_phases)

                result.append({
                    'id': order.id,
                    'cliente': order.cliente,
                    'numero_ordine': order.numero_ordine,
                    'data_ricezione': order.data_ricezione.isoformat() if order.data_ricezione else None,
                    'data_consegna': order.data_consegna.isoformat(),
                    'status': order.status,
                    'fase_corrente': order.fase_corrente,
                    'operatore_assegnato': order.operatore_assegnato,
                    'operatore_nome': operatore_nome,
                    'prezzo_quotato': order.prezzo_quotato,
                    'origine': order.origine or 'PDF',
                    'preventivo_id_origine': order.preventivo_id_origine,
                    'pdf_file': pdf_file,
                    'dxf_files': dxf_files,
                    'note': order.note,
                    'processing_steps': steps_data,
                    'support_requests': support_data,
                    'parent_order_id': order.parent_order_id,
                    'lotto_numero': order.lotto_numero or 0,
                    'lotto_nome': order.lotto_nome or '',
                    'lotti': lotti_data,
                    'lotti_count': lotti_count,
                    'all_lotti_completed': all_lotti_completed,
                    'visto_da_operatore': bool(order.visto_da_operatore),
                    'data_presa_visione': order.data_presa_visione.isoformat() if order.data_presa_visione else None,
                    'taglio_completato': bool(getattr(order, 'taglio_completato', False)),
                    'data_taglio_completato': order.data_taglio_completato.isoformat() if getattr(order, 'data_taglio_completato', None) else None,
                    'taglio_completato_da': getattr(order, 'taglio_completato_da', None),
                    'fase': _fase_ordine(order),
                    # Cartella da aprire in Lantek: il percorso serve
                    # all'operatore, i singoli file scaricati no.
                    'cartella_disegni': _cartella_disegni(order),
                })

            return result
        finally:
            session.close()

    @staticmethod
    def get_orders_by_ids(order_ids: list) -> list:
        """Recupera ordini specifici per ID come dizionari"""
        if not order_ids:
            return []
        return OrderManager.get_all_orders_dict(order_ids=order_ids)

    @staticmethod
    def get_orders_by_phase(phase: str, operatore_id: str = None) -> list:
        """Recupera ordini per fase corrente (e opzionalmente per operatore)"""
        session = get_session()
        try:
            query = session.query(Order).filter(Order.fase_corrente == phase, Order.is_deleted == False)
            if operatore_id:
                query = query.filter(Order.operatore_assegnato == operatore_id)
            return query.order_by(Order.data_consegna.asc()).all()
        finally:
            session.close()

    @staticmethod
    def update_delivery_date(order_id: str, new_date: datetime) -> dict:
        """Aggiorna la data di consegna di un ordine (riprogrammazione calendario)"""
        session = get_session()
        try:
            order = session.query(Order).filter(Order.id == order_id).first()
            if not order:
                return {'success': False, 'error': 'Ordine non trovato'}
            old_date = order.data_consegna
            order.data_consegna = new_date
            session.commit()
            return {
                'success': True,
                'order_id': order_id,
                'data_consegna': order.data_consegna.isoformat(),
                'old_date': old_date.isoformat() if old_date else None
            }
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()

    @staticmethod
    def update_order(order_id: str, updates: dict) -> dict:
        """Aggiorna i dati modificabili di un ordine: cliente, note, data_consegna, numero_ordine."""
        session = get_session()
        try:
            order = session.query(Order).filter(Order.id == order_id).first()
            if not order:
                return {'success': False, 'error': 'Ordine non trovato'}

            # Prima si diceva 'success' anche quando non era stato applicato
            # nulla: un campo fuori elenco o una data illeggibile sparivano in
            # silenzio e chi chiamava credeva di aver salvato.
            allowed = ['cliente', 'note', 'data_consegna', 'numero_ordine']
            applicati, ignorati = [], []
            for campo, valore in (updates or {}).items():
                if campo in ('order_id', 'user_id', 'id'):
                    continue
                if campo not in allowed:
                    ignorati.append(campo)
                    continue
                if valore is None:
                    continue
                if campo == 'data_consegna':
                    try:
                        valore = datetime.strptime(str(valore)[:10], '%Y-%m-%d')
                    except ValueError:
                        return {'success': False,
                                'error': f'Data di consegna non valida: {updates[campo]}'}
                setattr(order, campo, valore)
                applicati.append(campo)

            if not applicati:
                return {'success': False,
                        'error': 'Nessun campo modificabile nella richiesta',
                        'ignorati': ignorati}

            session.commit()
            return {'success': True, 'order_id': order_id,
                    'applicati': applicati, 'ignorati': ignorati}
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()

    @staticmethod
    def start_phase(order_id: str, phase: str, operatore: str = "") -> bool:
        """Inizia una fase di lavorazione — crea/riprende ProcessingStep + crea PhaseSession.
        LASER: nessun time tracking (gestito da Lantek), solo ProcessingStep senza timestamp."""
        session = get_session()
        try:
            now = datetime.utcnow()

            # Verifica che la fase richiesta corrisponda alla fase corrente dell'ordine
            order = session.query(Order).filter(Order.id == order_id).first()
            if not order:
                return False
            if order.fase_corrente != phase:
                raise ValueError(f"Fase '{phase}' non corrisponde alla fase corrente '{order.fase_corrente}' dell'ordine")

            # LASER: niente time tracking — Lantek gestisce i tempi
            if phase == 'LASER':
                existing_step = session.query(ProcessingStep).filter(
                    ProcessingStep.order_id == order_id,
                    ProcessingStep.fase == 'LASER',
                    ProcessingStep.timestamp_fine.is_(None)
                ).first()
                if not existing_step:
                    step = ProcessingStep(
                        id=str(uuid.uuid4()),
                        order_id=order_id,
                        fase='LASER',
                        timestamp_inizio=None,  # Nessun timestamp per LASER
                        operatore=operatore
                    )
                    session.add(step)
                # Nessuna PhaseSession per LASER
                order.fase_corrente = phase
                if order.status in ("RICEVUTO", "PARZIALE"):
                    order.status = "IN_LAVORAZIONE"
                session.commit()
                return True

            # Cerca ProcessingStep aperto per questa fase (potrebbe essere in pausa)
            existing_step = session.query(ProcessingStep).filter(
                ProcessingStep.order_id == order_id,
                ProcessingStep.fase == phase,
                ProcessingStep.timestamp_fine.is_(None)
            ).order_by(ProcessingStep.timestamp_inizio.desc()).first()

            if existing_step:
                # Controlla se QUESTO operatore ha già una sessione attiva
                my_active = session.query(PhaseSession).filter(
                    PhaseSession.step_id == existing_step.id,
                    PhaseSession.operatore == operatore,
                    PhaseSession.timestamp_fine.is_(None)
                ).first()
                if my_active:
                    return True  # Sessione già attiva per questo operatore
                # Se un altro operatore ha una sessione attiva, permetti sessione parallela
                step = existing_step
            else:
                # Crea nuovo ProcessingStep
                step = ProcessingStep(
                    id=str(uuid.uuid4()),
                    order_id=order_id,
                    fase=phase,
                    timestamp_inizio=now,
                    operatore=operatore
                )
                session.add(step)
                session.flush()

            # Crea nuova PhaseSession (solo per fasi NON-LASER)
            new_sess = PhaseSession(
                id=str(uuid.uuid4()),
                step_id=step.id,
                order_id=order_id,
                fase=phase,
                operatore=operatore,
                timestamp_inizio=now
            )
            session.add(new_sess)

            # Aggiorna stato dell'ordine (order già caricato all'inizio)
            order.fase_corrente = phase
            if order.status in ("RICEVUTO", "PARZIALE"):
                order.status = "IN_LAVORAZIONE"

            session.commit()
            return True
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()
    
    @staticmethod
    def complete_phase(order_id: str, phase: str, fase_successiva: str = None,
                       completamento_parziale: bool = False, note: str = "",
                       operatore: str = "") -> dict:
        """
        Completa una fase con routing dinamico.
        Se completamento_parziale=True → salva parziale (chiude sessione, non lo step).
        """
        session = get_session()
        try:
            # Trova l'ultimo step attivo (non completato) per questa fase
            processing_step = session.query(ProcessingStep).filter(
                ProcessingStep.order_id == order_id,
                ProcessingStep.fase == phase,
                ProcessingStep.timestamp_fine.is_(None)
            ).order_by(ProcessingStep.timestamp_inizio.desc()).first()

            if not processing_step:
                return {"success": False, "error": "Fase non trovata o già completata"}

            now = datetime.utcnow()

            # LASER: niente time tracking — Lantek gestisce i tempi
            # Completa lo step senza toccare PhaseSession
            if phase == 'LASER':
                processing_step.timestamp_fine = now
                processing_step.note = note
                processing_step.fase_successiva = fase_successiva
                processing_step.completamento_parziale = False
                if operatore and not processing_step.operatore:
                    processing_step.operatore = operatore

                order = session.query(Order).filter(Order.id == order_id).first()
                if not order:
                    return {"success": False, "error": "Ordine non trovato"}

                if not fase_successiva:
                    fase_successiva = 'PIEGA'
                    processing_step.fase_successiva = fase_successiva

                if fase_successiva == "COMPLETATO":
                    order.fase_corrente = "COMPLETATO"
                    order.status = "DA_FATTURARE"
                else:
                    order.fase_corrente = fase_successiva
                    order.status = "IN_LAVORAZIONE"

                order.visto_da_operatore = False
                order.data_presa_visione = None

                # Auto-assegna operatore quando ordine esce dal laser
                if fase_successiva not in ('LASER', 'COMPLETATO') and not order.operatore_assegnato:
                    from sqlalchemy import func
                    assignments = session.query(OperatorClient).filter(
                        func.lower(OperatorClient.client_name) == func.lower(order.cliente)
                    ).all()
                    for assignment in assignments:
                        op_user = session.query(User).filter(User.id == assignment.operator_id).first()
                        if op_user and op_user.phase != 'LASER':
                            order.operatore_assegnato = assignment.operator_id
                            break

                session.commit()

                numero_display = order.numero_ordine or order.id[:8]
                cliente = order.cliente
                op_id = order.operatore_assegnato
                if op_id and fase_successiva not in ('COMPLETATO', 'LASER'):
                    NotificationManager.create_notification(
                        user_id=op_id, order_id=order_id,
                        title='Ordine pronto',
                        message=f'Ordine #{numero_display} ({cliente}): LASER completato → {fase_successiva}',
                        notification_type='phase_ready',
                        notification_category='attiva'
                    )

                return {
                    "success": True,
                    "order_id": order_id,
                    "phase": "LASER",
                    "next_phase": fase_successiva
                }

            # Trova la sessione attiva di QUESTO operatore (per supporto parallelo)
            active_sess = None
            if operatore:
                active_sess = session.query(PhaseSession).filter(
                    PhaseSession.step_id == processing_step.id,
                    PhaseSession.operatore == operatore,
                    PhaseSession.timestamp_fine.is_(None)
                ).first()
            if not active_sess:
                # Fallback: qualsiasi sessione attiva
                active_sess = session.query(PhaseSession).filter(
                    PhaseSession.step_id == processing_step.id,
                    PhaseSession.timestamp_fine.is_(None)
                ).first()

            # --- BACKWARD COMPAT: completamento_parziale → save_partial ---
            if completamento_parziale:
                if active_sess:
                    active_sess.timestamp_fine = now
                    active_sess.tipo_chiusura = 'parziale'
                    if note:
                        active_sess.note = note
                # NON chiudere il ProcessingStep, NON cambiare fase_corrente
                session.commit()
                all_sess = session.query(PhaseSession).filter(
                    PhaseSession.step_id == processing_step.id,
                    PhaseSession.timestamp_fine.isnot(None)
                ).all()
                tempo_sec = sum(
                    int((s.timestamp_fine - s.timestamp_inizio).total_seconds())
                    for s in all_sess
                )
                return {
                    "success": True,
                    "order_id": order_id,
                    "phase": phase,
                    "completamento_parziale": True,
                    "sessioni_count": len(all_sess),
                    "tempo_cumulativo_secondi": tempo_sec,
                    "paused": True
                }

            # --- VERIFICA MULTI-OPERATORE ---
            # Conta operatori distinti su questo step
            distinct_ops = set(
                s[0] for s in session.query(PhaseSession.operatore).filter(
                    PhaseSession.step_id == processing_step.id,
                    PhaseSession.operatore.isnot(None)
                ).distinct().all()
            )

            if len(distinct_ops) > 1 and operatore:
                # Multi-operatore: conferma individuale
                # Chiudi solo la sessione di QUESTO operatore
                my_active = session.query(PhaseSession).filter(
                    PhaseSession.step_id == processing_step.id,
                    PhaseSession.operatore == operatore,
                    PhaseSession.timestamp_fine.is_(None)
                ).first()
                if my_active:
                    my_active.timestamp_fine = now
                    my_active.tipo_chiusura = 'totale'
                    if note:
                        my_active.note = note
                else:
                    # Operatore in pausa: segna ultima sessione come confermata
                    last_sess = session.query(PhaseSession).filter(
                        PhaseSession.step_id == processing_step.id,
                        PhaseSession.operatore == operatore
                    ).order_by(PhaseSession.timestamp_fine.desc()).first()
                    if last_sess:
                        last_sess.tipo_chiusura = 'totale'

                session.flush()

                # Verifica se TUTTI gli operatori hanno confermato
                all_confirmed = True
                pending_names = []
                for op_name in distinct_ops:
                    has_confirm = session.query(PhaseSession).filter(
                        PhaseSession.step_id == processing_step.id,
                        PhaseSession.operatore == op_name,
                        PhaseSession.tipo_chiusura == 'totale'
                    ).first()
                    if not has_confirm:
                        all_confirmed = False
                        pending_names.append(op_name)

                if not all_confirmed:
                    session.commit()
                    return {
                        "success": True,
                        "order_id": order_id,
                        "phase": phase,
                        "waiting_for_others": True,
                        "pending_operators": pending_names
                    }

                # Tutti hanno confermato → chiudi sessioni rimaste e prosegui
                remaining = session.query(PhaseSession).filter(
                    PhaseSession.step_id == processing_step.id,
                    PhaseSession.timestamp_fine.is_(None)
                ).all()
                for s in remaining:
                    s.timestamp_fine = now
                    s.tipo_chiusura = 'totale'
            else:
                # Singolo operatore: chiudi tutte le sessioni come prima
                all_active_sessions = session.query(PhaseSession).filter(
                    PhaseSession.step_id == processing_step.id,
                    PhaseSession.timestamp_fine.is_(None)
                ).all()
                for sess in all_active_sessions:
                    sess.timestamp_fine = now
                    sess.tipo_chiusura = 'totale'
                    if note and sess == active_sess:
                        sess.note = note

            # Completa lo step
            processing_step.timestamp_fine = now
            processing_step.note = note
            processing_step.fase_successiva = fase_successiva
            processing_step.completamento_parziale = False
            if operatore and not processing_step.operatore:
                processing_step.operatore = operatore

            # Aggiorna l'ordine
            order = session.query(Order).filter(Order.id == order_id).first()
            if not order:
                return {"success": False, "error": "Ordine non trovato"}

            # Auto-routing
            if not fase_successiva:
                phase_flow = {
                    'LASER': 'PIEGA',
                    'PIEGA': 'SALDATURA',
                    'SALDATURA': 'PULIZIA',
                    'PULIZIA': 'COMPLETATO'
                }
                fase_successiva = phase_flow.get(phase, 'COMPLETATO')
                processing_step.fase_successiva = fase_successiva

            if fase_successiva == "COMPLETATO":
                order.fase_corrente = "COMPLETATO"
                order.status = "DA_FATTURARE"
                total_time = OrderManager._calculate_order_total_time(order_id, session)
                notification = OrderNotification(
                    id=str(uuid.uuid4()),
                    order_id=order_id,
                    tempi_totali=total_time
                )
                session.add(notification)
            elif fase_successiva == "LASER":
                order.fase_corrente = "LASER"
                order.status = "IN_LAVORAZIONE"
            elif fase_successiva:
                order.fase_corrente = fase_successiva
                order.status = "IN_LAVORAZIONE"

            # Reset "visto" quando ordine cambia fase — il prossimo operatore lo vedrà come NUOVO
            if fase_successiva and fase_successiva != phase:
                order.visto_da_operatore = False
                order.data_presa_visione = None

            # Auto-assegna operatore quando ordine esce dal laser e non ha operatore
            if phase == 'LASER' and fase_successiva not in ('LASER', 'COMPLETATO') and not order.operatore_assegnato:
                # Cerca mapping cliente→operatore, ma solo operatori officina (non LASER)
                assignments = session.query(OperatorClient).filter(
                    func.lower(OperatorClient.client_name) == func.lower(order.cliente)
                ).all()
                assigned = False
                for assignment in assignments:
                    op_user = session.query(User).filter(User.id == assignment.operator_id).first()
                    if op_user and op_user.phase != 'LASER':
                        order.operatore_assegnato = assignment.operator_id
                        assigned = True
                        break
                # Fallback: assegna a un capo se nessun mapping trovato
                if not assigned:
                    capo = session.query(User).filter(User.is_capo == True).first()
                    if capo:
                        order.operatore_assegnato = capo.id
                        logger.warning(f"Ordine {order.numero_ordine} ({order.cliente}): nessun operatore mappato, assegnato al capo {capo.name}")

            session.commit()

            # Calcola tempi per-operatore per il riepilogo
            all_step_sessions = session.query(PhaseSession).filter(
                PhaseSession.step_id == processing_step.id
            ).all()
            all_users_db = session.query(User).all()
            n2id = {u.name: u.id for u in all_users_db}
            id2name = {u.id: u.name for u in all_users_db}
            op_tempi = {}
            for s in all_step_sessions:
                if s.timestamp_fine and s.timestamp_inizio:
                    op = s.operatore or 'unknown'
                    op_id = n2id.get(op, op)
                    op_tempi.setdefault(op_id, 0)
                    op_tempi[op_id] += int((s.timestamp_fine - s.timestamp_inizio).total_seconds())

            operatori_tempi = []
            for op_id, sec in op_tempi.items():
                operatori_tempi.append({
                    'operatore_id': op_id,
                    'nome': id2name.get(op_id, op_id),
                    'secondi': sec
                })

            return {
                "success": True,
                "order_id": order_id,
                "phase": phase,
                "fase_successiva": fase_successiva,
                "completamento_parziale": False,
                "all_completed": fase_successiva == "COMPLETATO",
                "operatori_tempi": operatori_tempi
            }

        except Exception as e:
            session.rollback()
            return {"success": False, "error": str(e)}
        finally:
            session.close()

    @staticmethod
    def save_partial(order_id: str, phase: str, note: str = "", operatore: str = "") -> dict:
        """Salva parziale: chiude sessione corrente, NON chiude il ProcessingStep."""
        session = get_session()
        try:
            processing_step = session.query(ProcessingStep).filter(
                ProcessingStep.order_id == order_id,
                ProcessingStep.fase == phase,
                ProcessingStep.timestamp_fine.is_(None)
            ).order_by(ProcessingStep.timestamp_inizio.desc()).first()

            if not processing_step:
                return {"success": False, "error": "Nessuna fase attiva trovata"}

            # Cerca sessione attiva di QUESTO operatore (supporto parallelo)
            active_sess = None
            if operatore:
                active_sess = session.query(PhaseSession).filter(
                    PhaseSession.step_id == processing_step.id,
                    PhaseSession.operatore == operatore,
                    PhaseSession.timestamp_fine.is_(None)
                ).first()
            if not active_sess:
                # Fallback: qualsiasi sessione attiva
                active_sess = session.query(PhaseSession).filter(
                    PhaseSession.step_id == processing_step.id,
                    PhaseSession.timestamp_fine.is_(None)
                ).first()

            if not active_sess:
                return {"success": False, "error": "Nessuna sessione attiva trovata"}

            now = datetime.utcnow()
            active_sess.timestamp_fine = now
            active_sess.tipo_chiusura = 'parziale'
            if note:
                active_sess.note = note
            if operatore and not active_sess.operatore:
                active_sess.operatore = operatore

            # Calcola tempo cumulativo
            all_sess = session.query(PhaseSession).filter(
                PhaseSession.step_id == processing_step.id,
                PhaseSession.timestamp_fine.isnot(None)
            ).all()
            tempo_sec = sum(
                int((s.timestamp_fine - s.timestamp_inizio).total_seconds())
                for s in all_sess
            )

            session.commit()
            return {
                "success": True,
                "order_id": order_id,
                "phase": phase,
                "sessioni_count": len(all_sess),
                "tempo_cumulativo_secondi": tempo_sec,
                "tempo_cumulativo": OrderManager._format_duration(timedelta(seconds=tempo_sec))
            }
        except Exception as e:
            session.rollback()
            return {"success": False, "error": str(e)}
        finally:
            session.close()
    
    @staticmethod
    def split_order(order_id: str, operatore_id: str, lotto_nome: str = None) -> dict:
        """
        Divide un ordine in lotti: il padre resta nella fase corrente (L1),
        viene creato un figlio (L2+) che avanza alla fase successiva.
        """
        session = get_session()
        try:
            order = session.query(Order).filter(Order.id == order_id).first()
            if not order:
                return {"success": False, "error": "Ordine non trovato"}

            if order.fase_corrente in ("COMPLETATO", "PARZIALE"):
                return {"success": False, "error": "Non puoi dividere un ordine completato"}

            # Verifica permessi: operatore assegnato al cliente o capo
            user = session.query(User).filter(User.id == operatore_id).first()
            if not user:
                return {"success": False, "error": "Utente non trovato"}
            if not user.is_capo:
                is_assigned = session.query(OperatorClient).filter(
                    func.lower(OperatorClient.client_name) == func.lower(order.cliente),
                    OperatorClient.operator_id == operatore_id
                ).first()
                if not is_assigned:
                    return {"success": False, "error": "Non hai i permessi per dividere questo ordine"}

            # Determina il padre reale (se si splitta un lotto, il padre è il root)
            root_id = order.parent_order_id if order.parent_order_id else order.id

            # Se il padre non è ancora un lotto, diventa L1
            root_order = session.query(Order).filter(Order.id == root_id).first()
            if root_order and root_order.lotto_numero == 0:
                root_order.lotto_numero = 1

            # Se anche l'ordine corrente non è ancora un lotto (primo split), diventa L1
            if order.lotto_numero == 0:
                order.lotto_numero = 1

            # Calcola prossimo numero lotto
            max_lotto = session.query(func.max(Order.lotto_numero)).filter(
                ((Order.id == root_id) | (Order.parent_order_id == root_id))
            ).scalar() or 1
            nuovo_lotto_num = max_lotto + 1

            # Fase successiva per il nuovo lotto
            phase_flow = {
                'LASER': 'PIEGA',
                'PIEGA': 'SALDATURA',
                'SALDATURA': 'PULIZIA',
                'PULIZIA': 'COMPLETATO'
            }
            fase_succ = phase_flow.get(order.fase_corrente, 'COMPLETATO')
            if fase_succ == 'COMPLETATO':
                return {"success": False, "error": "Non puoi dividere un ordine nell'ultima fase"}

            # Copia i file PDF dell'ordine originale
            original_files = session.query(OrderFile).filter(
                OrderFile.order_id == order.id,
                OrderFile.file_type == 'PDF'
            ).all()

            # Auto-assign operatore per il nuovo lotto (se esce da LASER)
            new_operatore = order.operatore_assegnato
            if order.fase_corrente == 'LASER' and not new_operatore:
                assignments = session.query(OperatorClient).filter(
                    func.lower(OperatorClient.client_name) == func.lower(order.cliente)
                ).all()
                for assignment in assignments:
                    op_user = session.query(User).filter(User.id == assignment.operator_id).first()
                    if op_user and op_user.phase != 'LASER':
                        new_operatore = assignment.operator_id
                        break
                if not new_operatore:
                    capo = session.query(User).filter(User.is_capo == True).first()
                    if capo:
                        new_operatore = capo.id

            # Crea il nuovo lotto
            new_order = Order(
                id=str(uuid.uuid4()),
                cliente=order.cliente,
                numero_ordine=order.numero_ordine,
                data_ricezione=order.data_ricezione,
                data_consegna=order.data_consegna,
                status="RICEVUTO",
                fase_corrente=fase_succ,
                operatore_assegnato=new_operatore,
                prezzo_quotato=None,  # Il prezzo resta solo sul padre
                note=f"Lotto {nuovo_lotto_num} — creato da split",
                parent_order_id=root_id,
                lotto_numero=nuovo_lotto_num,
                lotto_nome=lotto_nome or None
            )
            session.add(new_order)

            # Copia riferimenti file PDF al nuovo lotto
            for f in original_files:
                new_file = OrderFile(
                    id=str(uuid.uuid4()),
                    order_id=new_order.id,
                    filename=f.filename,
                    filepath=f.filepath,
                    file_type=f.file_type
                )
                session.add(new_file)

            session.commit()

            return {
                "success": True,
                "parent_order_id": root_id,
                "new_lotto_id": new_order.id,
                "new_lotto_numero": nuovo_lotto_num,
                "new_lotto_nome": new_order.lotto_nome or f"Lotto {nuovo_lotto_num}",
                "fase_corrente_padre": order.fase_corrente,
                "fase_corrente_lotto": fase_succ
            }

        except Exception as e:
            session.rollback()
            return {"success": False, "error": str(e)}
        finally:
            session.close()

    @staticmethod
    def confirm_lotti_completion(order_id: str, operatore_id: str) -> dict:
        """
        Conferma manuale completamento ordine quando tutti i lotti sono COMPLETATO.
        Solo l'operatore assegnato al cliente o un capo può confermare.
        """
        session = get_session()
        try:
            order = session.query(Order).filter(Order.id == order_id).first()
            if not order:
                return {"success": False, "error": "Ordine non trovato"}

            # Verifica permessi
            user = session.query(User).filter(User.id == operatore_id).first()
            if not user:
                return {"success": False, "error": "Utente non trovato"}
            if not user.is_capo:
                is_assigned = session.query(OperatorClient).filter(
                    func.lower(OperatorClient.client_name) == func.lower(order.cliente),
                    OperatorClient.operator_id == operatore_id
                ).first()
                if not is_assigned:
                    return {"success": False, "error": "Non hai i permessi"}

            # Verifica che il padre (L1) sia COMPLETATO
            if order.fase_corrente != "COMPLETATO":
                return {"success": False, "error": "L'ordine padre non è ancora completato"}

            # Verifica che TUTTI i lotti figli siano COMPLETATO
            lotti = session.query(Order).filter(
                Order.parent_order_id == order_id
            ).all()
            non_completati = [l for l in lotti if l.fase_corrente != "COMPLETATO"]
            if non_completati:
                nomi = [f"L{l.lotto_numero}" for l in non_completati]
                return {"success": False, "error": f"Lotti non completati: {', '.join(nomi)}"}

            # Calcola tempo totale (padre + tutti i lotti)
            all_order_ids = [order_id] + [l.id for l in lotti]
            total_seconds = 0
            for oid in all_order_ids:
                steps = session.query(ProcessingStep).filter(
                    ProcessingStep.order_id == oid,
                    ProcessingStep.timestamp_inizio.isnot(None),
                    ProcessingStep.timestamp_fine.isnot(None)
                ).all()
                for step in steps:
                    total_seconds += int((step.timestamp_fine - step.timestamp_inizio).total_seconds())

            total_time = OrderManager._format_duration(timedelta(seconds=total_seconds))

            # Crea notifica di completamento
            notification = OrderNotification(
                id=str(uuid.uuid4()),
                order_id=order_id,
                tempi_totali=total_time
            )
            session.add(notification)

            # Marca ordine come da fatturare (chiusura amministrativa necessaria)
            order.status = "DA_FATTURARE"
            session.commit()

            return {"success": True, "tempi_totali": total_time}

        except Exception as e:
            session.rollback()
            return {"success": False, "error": str(e)}
        finally:
            session.close()

    @staticmethod
    def complete_order(order_id: str, current_phase: str, note: str = "", operatore: str = "") -> dict:
        """
        Completa un ordine anticipatamente dalla fase corrente.
        Chiude step/sessione attivi, marca ordine COMPLETATO,
        le fasi senza ProcessingStep sono 'non necessarie' per assenza.
        """
        session = get_session()
        try:
            order = session.query(Order).filter(Order.id == order_id).first()
            if not order:
                return {"success": False, "error": "Ordine non trovato"}

            if order.status in ("COMPLETATO", "DA_FATTURARE", "CHIUSO"):
                return {"success": False, "error": "Ordine già completato"}

            now = datetime.utcnow()

            # Chiudi eventuali step aperti (potrebbe essere la fase corrente)
            open_steps = session.query(ProcessingStep).filter(
                ProcessingStep.order_id == order_id,
                ProcessingStep.timestamp_fine.is_(None)
            ).all()

            for step in open_steps:
                # Chiudi TUTTE le sessioni attive (anche parallele da supporto)
                active_sessions = session.query(PhaseSession).filter(
                    PhaseSession.step_id == step.id,
                    PhaseSession.timestamp_fine.is_(None)
                ).all()
                for active_sess in active_sessions:
                    active_sess.timestamp_fine = now
                    active_sess.tipo_chiusura = 'totale'

                step.timestamp_fine = now
                step.fase_successiva = 'COMPLETATO'
                step.completamento_parziale = False
                if operatore and not step.operatore:
                    step.operatore = operatore
                if note:
                    step.note = note

            # Marca ordine come da fatturare (chiusura amministrativa necessaria)
            order.fase_corrente = "COMPLETATO"
            order.status = "DA_FATTURARE"

            # Crea OrderNotification con tempo totale
            total_time = OrderManager._calculate_order_total_time(order_id, session)
            notification = OrderNotification(
                id=str(uuid.uuid4()),
                order_id=order_id,
                tempi_totali=total_time
            )
            session.add(notification)

            # Riepilogo fasi
            all_phases = ['LASER', 'PIEGA', 'SALDATURA', 'PULIZIA']
            all_steps = session.query(ProcessingStep).filter(
                ProcessingStep.order_id == order_id,
                ProcessingStep.timestamp_fine.isnot(None)
            ).all()

            executed_set = set(s.fase for s in all_steps)
            fasi_eseguite = []
            for s in all_steps:
                dur = int((s.timestamp_fine - s.timestamp_inizio).total_seconds()) if s.timestamp_inizio and s.timestamp_fine else 0
                fasi_eseguite.append({
                    'fase': s.fase,
                    'operatore': s.operatore or '',
                    'durata_secondi': dur
                })

            fasi_non_necessarie = [p for p in all_phases if p not in executed_set]

            session.commit()
            return {
                "success": True,
                "all_completed": True,
                "order_id": order_id,
                "fasi_eseguite": fasi_eseguite,
                "fasi_non_necessarie": fasi_non_necessarie
            }

        except Exception as e:
            session.rollback()
            return {"success": False, "error": str(e)}
        finally:
            session.close()

    @staticmethod
    def complete_laser(order_id: str, operatore: str = "") -> dict:
        """Operatore laser: segna taglio completato (no timer, solo completamento)"""
        session = get_session()
        try:
            order = session.query(Order).filter(Order.id == order_id).first()
            if not order:
                return {"success": False, "error": "Ordine non trovato"}

            # Crea step LASER senza timestamp_inizio (no time tracking per laser)
            step = ProcessingStep(
                id=str(uuid.uuid4()),
                order_id=order_id,
                fase="LASER",
                timestamp_fine=datetime.utcnow(),
                operatore=operatore,
                fase_successiva="OFFICINA"
            )
            session.add(step)

            # L'ordine torna all'operatore officina assegnato
            # fase_corrente va a una fase generica "OFFICINA" che l'operatore poi specifica
            order.fase_corrente = "PIEGA"  # Default: dopo laser va in piega (operatore poi sceglie)

            session.commit()
            return {"success": True, "order_id": order_id}

        except Exception as e:
            session.rollback()
            return {"success": False, "error": str(e)}
        finally:
            session.close()

    @staticmethod
    def send_to_laser(order_id: str, operatore: str = "") -> dict:
        """Operatore officina rimanda ordine al laser"""
        session = get_session()
        try:
            order = session.query(Order).filter(Order.id == order_id).first()
            if not order:
                return {"success": False, "error": "Ordine non trovato"}

            order.fase_corrente = "LASER"
            session.commit()
            return {"success": True, "order_id": order_id}

        except Exception as e:
            session.rollback()
            return {"success": False, "error": str(e)}
        finally:
            session.close()

    @staticmethod
    def reassign_order(order_id: str, new_operator_id: str) -> dict:
        """Capo officina: riassegna ordine a un altro operatore"""
        session = get_session()
        try:
            order = session.query(Order).filter(Order.id == order_id).first()
            if not order:
                return {"success": False, "error": "Ordine non trovato"}

            order.operatore_assegnato = new_operator_id
            session.commit()
            return {"success": True, "order_id": order_id, "new_operator": new_operator_id}

        except Exception as e:
            session.rollback()
            return {"success": False, "error": str(e)}
        finally:
            session.close()

    @staticmethod
    def correct_time(step_id: str, new_start: str = None, new_end: str = None) -> dict:
        """Capo officina: corregge timestamp di un processing step"""
        session = get_session()
        try:
            step = session.query(ProcessingStep).filter(ProcessingStep.id == step_id).first()
            if not step:
                return {"success": False, "error": "Step non trovato"}

            if new_start:
                step.timestamp_inizio = datetime.fromisoformat(new_start)
            if new_end:
                step.timestamp_fine = datetime.fromisoformat(new_end)

            session.commit()
            return {"success": True, "step_id": step_id}

        except Exception as e:
            session.rollback()
            return {"success": False, "error": str(e)}
        finally:
            session.close()

    @staticmethod
    def move_phase(order_id: str, new_phase: str) -> dict:
        """Capo officina: sposta ordine a qualsiasi fase"""
        session = get_session()
        try:
            order = session.query(Order).filter(Order.id == order_id).first()
            if not order:
                return {"success": False, "error": "Ordine non trovato"}

            old_phase = order.fase_corrente
            order.fase_corrente = new_phase
            if new_phase == "COMPLETATO":
                order.status = "DA_FATTURARE"
            elif new_phase == "PARZIALE":
                order.status = "PARZIALE"
            else:
                order.status = "IN_LAVORAZIONE"
            # Reset "visto" quando fase cambia
            if new_phase != old_phase:
                order.visto_da_operatore = False
                order.data_presa_visione = None

            session.commit()
            return {"success": True, "order_id": order_id, "new_phase": new_phase}

        except Exception as e:
            session.rollback()
            return {"success": False, "error": str(e)}
        finally:
            session.close()

    @staticmethod
    def get_order_details(order_id: str) -> dict:
        """Recupera dettagli completi ordine con storico fasi"""
        session = get_session()
        try:
            order = session.query(Order).filter(Order.id == order_id).first()
            if not order:
                return {"error": "Ordine non trovato"}

            processing_steps = session.query(ProcessingStep).filter(
                ProcessingStep.order_id == order_id
            ).order_by(ProcessingStep.timestamp_inizio.asc()).all()

            # Carica tutte le sessioni per questo ordine
            all_sessions = session.query(PhaseSession).filter(
                PhaseSession.order_id == order_id
            ).order_by(PhaseSession.timestamp_inizio.asc()).all()
            step_sessions = {}
            for ps in all_sessions:
                step_sessions.setdefault(ps.step_id, []).append(ps)

            # Mapping nome→ID utente per operatori_info
            all_users_detail = session.query(User).all()
            name_to_id = {u.name: u.id for u in all_users_detail}

            pdf_file = None
            dxf_files = []
            if order.files:
                for f in order.files:
                    if f.file_type == 'PDF':
                        pdf_file = f.filename
                    elif f.file_type == 'DXF':
                        dxf_files.append({'filename': f.filename})

            # Recupera nome operatore assegnato
            operatore_nome = None
            if order.operatore_assegnato:
                user = session.query(User).filter(User.id == order.operatore_assegnato).first()
                if user:
                    operatore_nome = user.name

            def _step_session_data(s):
                ss = step_sessions.get(s.id, [])
                closed = [x for x in ss if x.timestamp_fine]
                cumul = sum(int((x.timestamp_fine - x.timestamp_inizio).total_seconds()) for x in closed)
                active_list = [x for x in ss if x.timestamp_fine is None]
                active = active_list[0] if active_list else None

                # Info per-operatore: tempo e stato indipendenti (chiave = user ID)
                op_groups = {}
                for x in ss:
                    op = x.operatore or 'unknown'
                    op_groups.setdefault(op, []).append(x)
                operatori_info = {}
                for op_name, op_ss in op_groups.items():
                    op_closed = [x for x in op_ss if x.timestamp_fine]
                    op_active = [x for x in op_ss if x.timestamp_fine is None]
                    op_cumul = sum(int((x.timestamp_fine - x.timestamp_inizio).total_seconds()) for x in op_closed)
                    op_key = name_to_id.get(op_name, op_name)
                    op_confermato2 = any(x.tipo_chiusura == 'totale' for x in op_ss)
                    operatori_info[op_key] = {
                        'sessione_attiva': len(op_active) > 0,
                        'sessione_attiva_inizio': (op_active[0].timestamp_inizio.isoformat() + 'Z') if op_active else None,
                        'in_pausa': len(op_active) == 0 and len(op_closed) > 0,
                        'confermato': op_confermato2,
                        'tempo_cumulativo_secondi': op_cumul,
                        'sessioni_count': len(op_ss)
                    }

                return {
                    "id": s.id,
                    "fase": s.fase,
                    "timestamp_inizio": s.timestamp_inizio.isoformat() + 'Z' if s.timestamp_inizio else None,
                    "timestamp_fine": s.timestamp_fine.isoformat() + 'Z' if s.timestamp_fine else None,
                    "operatore": s.operatore,
                    "note": s.note,
                    "fase_successiva": s.fase_successiva,
                    "completamento_parziale": s.completamento_parziale,
                    "durata": OrderManager._format_duration(
                        s.timestamp_fine - s.timestamp_inizio
                    ) if s.timestamp_inizio and s.timestamp_fine else None,
                    "sessione_attiva": len(active_list) > 0,
                    "sessione_attiva_inizio": active.timestamp_inizio.isoformat() + 'Z' if active else None,
                    "sessioni_count": len(ss),
                    "tempo_cumulativo_secondi": cumul,
                    "in_pausa": (s.timestamp_fine is None and len(ss) > 0 and len(active_list) == 0),
                    "sessioni_attive": [{'operatore': a.operatore, 'inizio': a.timestamp_inizio.isoformat() + 'Z'} for a in active_list],
                    "operatori_info": operatori_info,
                    "sessioni": [
                        {
                            "id": x.id,
                            "timestamp_inizio": x.timestamp_inizio.isoformat() + 'Z',
                            "timestamp_fine": x.timestamp_fine.isoformat() + 'Z' if x.timestamp_fine else None,
                            "tipo_chiusura": x.tipo_chiusura,
                            "operatore": x.operatore,
                            "note": x.note,
                            "durata": OrderManager._format_duration(
                                x.timestamp_fine - x.timestamp_inizio
                            ) if x.timestamp_fine else None,
                            "durata_secondi": int((x.timestamp_fine - x.timestamp_inizio).total_seconds()) if x.timestamp_fine else None
                        }
                        for x in ss
                    ]
                }

            # Support requests attivi
            active_support = session.query(SupportRequest).filter(
                SupportRequest.order_id == order.id,
                SupportRequest.stato.in_(['pending', 'accepted'])
            ).all()
            support_data = []
            for sr in active_support:
                sr_principale = session.query(User).filter(User.id == sr.operatore_principale).first()
                sr_supporto = session.query(User).filter(User.id == sr.operatore_supporto).first()
                support_data.append({
                    'id': sr.id,
                    'operatore_principale': sr.operatore_principale,
                    'nome_principale': sr_principale.name if sr_principale else '',
                    'operatore_supporto': sr.operatore_supporto,
                    'nome_supporto': sr_supporto.name if sr_supporto else '',
                    'stato': sr.stato,
                    'forzata': sr.forzata
                })

            # Info lotti
            lotti_data = []
            lotti_count = 0
            all_lotti_completed = False
            root_id = order.parent_order_id or order.id
            if order.lotto_numero > 0 or order.parent_order_id:
                # Questo ordine fa parte di un sistema di lotti
                lotti_query = session.query(Order).filter(
                    ((Order.id == root_id) | (Order.parent_order_id == root_id)),
                    Order.id != order.id
                ).order_by(Order.lotto_numero.asc()).all()
                for l in lotti_query:
                    lotti_data.append({
                        'id': l.id,
                        'lotto_numero': l.lotto_numero,
                        'lotto_nome': l.lotto_nome or '',
                        'fase_corrente': l.fase_corrente,
                        'status': l.status
                    })
                lotti_count = len(lotti_query) + 1  # +1 per l'ordine corrente
                # Controlla se tutti i lotti (compreso questo) sono completati
                all_phases = [l.fase_corrente for l in lotti_query] + [order.fase_corrente]
                all_lotti_completed = all(f == 'COMPLETATO' for f in all_phases)

            return {
                "id": order.id,
                "cliente": order.cliente,
                "numero_ordine": order.numero_ordine,
                "data_ricezione": order.data_ricezione.isoformat() if order.data_ricezione else None,
                "data_consegna": order.data_consegna.isoformat(),
                "status": order.status,
                "fase_corrente": order.fase_corrente,
                "operatore_assegnato": order.operatore_assegnato,
                "operatore_nome": operatore_nome,
                "prezzo_quotato": order.prezzo_quotato,
                "origine": order.origine or 'PDF',
                "preventivo_id_origine": order.preventivo_id_origine,
                "pdf_file": pdf_file,
                "dxf_files": dxf_files,
                "note": order.note,
                "processing_steps": [_step_session_data(s) for s in processing_steps],
                "support_requests": support_data,
                "parent_order_id": order.parent_order_id,
                "lotto_numero": order.lotto_numero,
                "lotto_nome": order.lotto_nome or '',
                "lotti": lotti_data,
                "lotti_count": lotti_count,
                "all_lotti_completed": all_lotti_completed,
                "visto_da_operatore": bool(order.visto_da_operatore),
                "data_presa_visione": order.data_presa_visione.isoformat() if order.data_presa_visione else None,
                "taglio_completato": bool(getattr(order, 'taglio_completato', False)),
                "data_taglio_completato": order.data_taglio_completato.isoformat() if getattr(order, 'data_taglio_completato', None) else None,
                "taglio_completato_da": getattr(order, 'taglio_completato_da', None),
            }
        finally:
            session.close()

    @staticmethod
    def mark_order_seen(order_id: str) -> dict:
        """Marca un ordine come visto dall'operatore"""
        session = get_session()
        try:
            order = session.query(Order).filter(Order.id == order_id).first()
            if not order:
                return {"success": False, "error": "Ordine non trovato"}
            if not order.visto_da_operatore:
                order.visto_da_operatore = True
                order.data_presa_visione = datetime.utcnow()
                session.commit()
            return {"success": True}
        except Exception as e:
            session.rollback()
            return {"success": False, "error": str(e)}
        finally:
            session.close()


class UserManager:
    """Gestore operazioni su utenti"""

    @staticmethod
    def _serialize_user(user) -> dict:
        """Serializza un utente in dict"""
        # Recupera clienti assegnati da operator_clients
        session = get_session()
        try:
            assigned = session.query(OperatorClient.client_name).filter(
                OperatorClient.operator_id == user.id
            ).all()
            assigned_clients = [a.client_name for a in assigned]
        except Exception:
            assigned_clients = []
        finally:
            session.close()

        return {
            'id': user.id,
            'name': user.name,
            'role': user.role,
            'initials': user.initials,
            'phase': user.phase,
            'permissions': user.permissions,
            'machines': user.machines,
            'is_capo': user.is_capo,
            'is_active': user.is_active,
            'assigned_clients': assigned_clients,
            'last_login': user.last_login.isoformat() if user.last_login else None,
            'created_at': user.created_at.isoformat() if user.created_at else None
        }

    @staticmethod
    def get_user(user_id: str) -> dict | None:
        """Recupera un utente per ID, restituisce dict serializzabile"""
        session = get_session()
        try:
            user = session.query(User).filter(User.id == user_id).first()
            if user:
                return UserManager._serialize_user(user)
            return None
        finally:
            session.close()

    @staticmethod
    def get_all_users(include_inactive: bool = False) -> list[dict]:
        """Recupera tutti gli utenti (attivi, o tutti se include_inactive=True)"""
        session = get_session()
        try:
            query = session.query(User)
            if not include_inactive:
                query = query.filter(User.is_active == True)
            users = query.all()
            return [UserManager._serialize_user(u) for u in users]
        finally:
            session.close()

    @staticmethod
    def authenticate(user_id: str) -> dict | None:
        """Autentica un utente (mock): aggiorna last_login e registra login nell'audit log"""
        session = get_session()
        try:
            user = session.query(User).filter(User.id == user_id, User.is_active == True).first()
            if user:
                user.last_login = datetime.utcnow()
                session.commit()

                AuditManager.log(
                    user_id=user.id,
                    user_name=user.name,
                    action='LOGIN'
                )

                return UserManager._serialize_user(user)
            return None
        except Exception as e:
            session.rollback()
            return None
        finally:
            session.close()

    @staticmethod
    def create_user(user_id: str, name: str, role: str, phase: str,
                   permissions: list = None, machines: list = None,
                   initials: str = None) -> dict | None:
        """Crea un nuovo utente"""
        session = get_session()
        try:
            # Verifica che l'utente non esista già
            existing = session.query(User).filter(User.id == user_id).first()
            if existing:
                return None  # Utente esiste già

            user = User(
                id=user_id,
                name=name,
                role=role,
                phase=phase,
                permissions=permissions or [],
                machines=machines or [],
                initials=initials or name[:2].upper(),
                is_active=True,
                created_at=datetime.utcnow()
            )
            session.add(user)
            session.commit()

            AuditManager.log(
                user_id='admin',
                user_name='Sistema',
                action='CREA_UTENTE',
                entity_type='user',
                entity_id=user_id,
                detail=f'Creato utente {name} ({role})'
            )

            return UserManager._serialize_user(user)
        except Exception as e:
            session.rollback()
            return None
        finally:
            session.close()

    @staticmethod
    def update_user(user_id: str, name: str = None, role: str = None,
                   phase: str = None, permissions: list = None,
                   machines: list = None, is_active: bool = None) -> dict | None:
        """Modifica un utente esistente"""
        session = get_session()
        try:
            user = session.query(User).filter(User.id == user_id).first()
            if not user:
                return None

            # Aggiorna solo i campi forniti
            if name:
                user.name = name
            if role:
                user.role = role
            if phase:
                user.phase = phase
            if permissions is not None:
                user.permissions = permissions
            if machines is not None:
                user.machines = machines
            if is_active is not None:
                user.is_active = is_active

            session.commit()

            AuditManager.log(
                user_id='admin',
                user_name='Sistema',
                action='MODIFICA_UTENTE',
                entity_type='user',
                entity_id=user_id,
                detail=f'Modificato utente {user.name}'
            )

            return UserManager._serialize_user(user)
        except Exception as e:
            session.rollback()
            return None
        finally:
            session.close()

    @staticmethod
    def delete_user(user_id: str) -> bool:
        """Disattiva un utente (soft delete)"""
        session = get_session()
        try:
            user = session.query(User).filter(User.id == user_id).first()
            if not user:
                return False

            user.is_active = False
            session.commit()

            # Log cancellazione utente
            AuditManager.log(
                user_id='admin',
                user_name='Sistema',
                action='DISATTIVA_UTENTE',
                entity_type='user',
                entity_id=user_id,
                detail=f'Disattivato utente {user.name}'
            )

            return True
        except Exception as e:
            session.rollback()
            return False
        finally:
            session.close()

class AuditManager:
    """Gestore operazioni di audit log"""

    @staticmethod
    def log(user_id: str = None, user_name: str = None, action: str = None,
            entity_type: str = None, entity_id: str = None, detail: str = None,
            ip_address: str = None) -> None:
        """Crea un record audit log (try/except silenzioso per non bloccare l'app)"""
        session = get_session()
        try:
            log_entry = AuditLog(
                id=str(uuid.uuid4()),
                timestamp=datetime.utcnow(),
                user_id=user_id,
                user_name=user_name,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                detail=detail,
                ip_address=ip_address
            )
            session.add(log_entry)
            session.commit()
        except Exception as e:
            session.rollback()
            # Silenzioso: non bloccare l'app se l'audit fallisce
        finally:
            session.close()

    @staticmethod
    def get_recent(limit: int = 100, user_id: str = None) -> list[dict]:
        """Recupera log recenti, opzionalmente filtrati per utente"""
        session = get_session()
        try:
            query = session.query(AuditLog)
            if user_id:
                query = query.filter(AuditLog.user_id == user_id)

            logs = query.order_by(AuditLog.timestamp.desc()).limit(limit).all()
            return [
                {
                    'id': log.id,
                    'timestamp': log.timestamp.isoformat(),
                    'user_id': log.user_id,
                    'user_name': log.user_name,
                    'action': log.action,
                    'entity_type': log.entity_type,
                    'entity_id': log.entity_id,
                    'detail': log.detail,
                    'ip_address': log.ip_address
                }
                for log in logs
            ]
        finally:
            session.close()

    @staticmethod
    def get_kpi_operai() -> list[dict]:
        """
        Calcola KPI reali per ogni operaio:
        - ordini completati (count distinct order_id)
        - tempo medio per ordine (da PhaseSession o fallback ProcessingStep)
        - puntualita: % ordini completati entro data_consegna
        - ritardi: ordini attivi scaduti assegnati all'operatore
        - saturazione: ore lavorate oggi / 8h turno
        """
        session = get_session()
        try:
            from sqlalchemy import func, and_
            now = datetime.utcnow()
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            TURNO_ORE = 8

            # Pre-carica dati per evitare N+1
            all_orders = {o.id: o for o in session.query(Order).all()}
            all_steps = session.query(ProcessingStep).all()
            steps_by_order = {}
            for s in all_steps:
                steps_by_order.setdefault(s.order_id, []).append(s)
            steps_by_operator = {}
            for s in all_steps:
                if s.operatore:
                    steps_by_operator.setdefault(s.operatore, []).append(s)

            # Sessioni chiuse per tempo reale
            all_closed_sessions = session.query(PhaseSession).filter(
                PhaseSession.timestamp_fine != None
            ).all()
            sessions_by_step = {}
            for ps in all_closed_sessions:
                sessions_by_step.setdefault(ps.step_id, []).append(ps)
            sessions_by_operator = {}
            for ps in all_closed_sessions:
                op_name = ps.operatore or 'unknown'
                sessions_by_operator.setdefault(op_name, []).append(ps)

            users = session.query(User).filter(User.is_active == True).all()

            kpi_list = []
            for user in users:
                name = user.name
                op_steps = steps_by_operator.get(name, [])
                completed_steps = [s for s in op_steps if s.timestamp_fine]

                # Ordini unici completati
                ordini_completati = len(set(s.order_id for s in completed_steps))

                # Tempo medio reale per step (da sessioni)
                step_durations = []
                for s in completed_steps:
                    step_ss = sessions_by_step.get(s.id, [])
                    op_ss = [x for x in step_ss if x.operatore == name]
                    if op_ss:
                        secs = sum((x.timestamp_fine - x.timestamp_inizio).total_seconds() for x in op_ss)
                        step_durations.append(secs)
                    elif s.timestamp_inizio and s.timestamp_fine:
                        step_durations.append((s.timestamp_fine - s.timestamp_inizio).total_seconds())
                if step_durations:
                    avg_secs = sum(step_durations) / len(step_durations)
                    tempo_medio = OrderManager._format_duration(timedelta(seconds=avg_secs))
                else:
                    tempo_medio = "N/A"

                # Puntualita: % ordini COMPLETATI in tempo
                op_order_ids = set(s.order_id for s in completed_steps)
                tot_completati = 0
                in_tempo = 0
                for oid in op_order_ids:
                    order = all_orders.get(oid)
                    if not order or order.status != 'COMPLETATO':
                        continue
                    tot_completati += 1
                    if not order.data_consegna:
                        continue
                    order_steps = steps_by_order.get(oid, [])
                    finished = [st.timestamp_fine for st in order_steps if st.timestamp_fine]
                    if finished and max(finished) <= order.data_consegna:
                        in_tempo += 1
                puntualita = round((in_tempo / tot_completati * 100) if tot_completati > 0 else 100)

                # Ritardi: ordini attivi scaduti dove l'operatore ha step non completato
                active_steps = [s for s in op_steps if s.timestamp_inizio and not s.timestamp_fine]
                ritardi = 0
                for s in active_steps:
                    order = all_orders.get(s.order_id)
                    if order and order.data_consegna and order.data_consegna < now and order.status not in ('COMPLETATO', 'SPEDITO'):
                        ritardi += 1

                # Saturazione oggi: ore lavorate / 8h turno
                op_sessions_oggi = [x for x in sessions_by_operator.get(name, []) if x.timestamp_fine >= today_start]
                tempo_oggi_sec = sum((x.timestamp_fine - x.timestamp_inizio).total_seconds() for x in op_sessions_oggi)
                saturazione = min(100, round(tempo_oggi_sec / (TURNO_ORE * 3600) * 100)) if tempo_oggi_sec > 0 else 0

                kpi_list.append({
                    'operaio': name,
                    'user_id': user.id,
                    'role': user.role,
                    'initials': user.initials,
                    'ordini_completati': ordini_completati,
                    'tempo_medio': tempo_medio,
                    'ultimo_accesso': user.last_login.isoformat() if user.last_login else 'Mai',
                    'puntualita': puntualita,
                    'ritardi': ritardi,
                    'saturazione': saturazione
                })

            return kpi_list
        finally:
            session.close()


class ArchiveManager:
    """Gestore operazioni su archivio ordini completati"""

    @staticmethod
    def _calculate_phase_times(order_id: str, session) -> dict:
        """
        Calcola i tempi per ogni fase completata di un ordine
        Ritorna dict con fasi come chiavi e durate come valori (formato stringa)
        """
        try:
            steps = session.query(ProcessingStep).filter(
                ProcessingStep.order_id == order_id,
                ProcessingStep.timestamp_inizio.isnot(None),
                ProcessingStep.timestamp_fine.isnot(None)
            ).all()

            phase_times = {}
            for step in steps:
                duration = step.timestamp_fine - step.timestamp_inizio
                phase_times[step.fase] = OrderManager._format_duration(duration)

            return phase_times
        except Exception:
            return {}

    @staticmethod
    def _apply_archive_filters(query, filters, session):
        """Applica filtri comuni alle query archivio"""
        if not filters:
            return query
        if filters.get('cliente'):
            query = query.filter(Order.cliente.ilike(f"%{filters['cliente']}%"))
        if filters.get('date_from'):
            date_from = datetime.fromisoformat(filters['date_from'])
            query = query.filter(Order.data_consegna >= date_from)
        if filters.get('date_to'):
            date_to = datetime.fromisoformat(filters['date_to'])
            date_to = date_to.replace(hour=23, minute=59, second=59)
            query = query.filter(Order.data_consegna <= date_to)
        if filters.get('operatore'):
            op_name = filters['operatore']
            order_ids = [s.order_id for s in session.query(ProcessingStep.order_id).filter(
                ProcessingStep.operatore.ilike(f"%{op_name}%")
            ).distinct()]
            query = query.filter(Order.id.in_(order_ids))
        return query

    @staticmethod
    def get_completed_orders(filters: dict = None, page: int = 1, limit: int = 10,
                            sort_by: str = 'data_consegna', sort_dir: str = 'desc') -> dict:
        """
        Recupera ordini completati con paginazione e filtri
        """
        session = get_session()
        try:
            from sqlalchemy import func

            query = session.query(Order).filter(
                Order.status.in_(["CHIUSO", "PARZIALE"]),
                Order.parent_order_id.is_(None)  # Escludi lotti figli
            )

            query = ArchiveManager._apply_archive_filters(query, filters, session)

            total = query.count()

            if sort_dir.lower() == 'asc':
                query = query.order_by(getattr(Order, sort_by).asc())
            else:
                query = query.order_by(getattr(Order, sort_by).desc())

            offset = (page - 1) * limit
            orders = query.offset(offset).limit(limit).all()

            orders_data = []
            for order in orders:
                notification = session.query(OrderNotification).filter(
                    OrderNotification.order_id == order.id
                ).first()

                total_time = notification.tempi_totali if notification else "N/A"

                last_step = session.query(ProcessingStep).filter(
                    ProcessingStep.order_id == order.id,
                    ProcessingStep.timestamp_fine.isnot(None)
                ).order_by(ProcessingStep.timestamp_fine.desc()).first()

                completion_date = last_step.timestamp_fine if last_step else None

                # Calcola tempi per fase e margine
                phase_times = ArchiveManager._calculate_phase_times(order.id, session)
                total_hours = OrderManager._calculate_total_hours(order.id, session)
                costo_orario = 25.0  # Configurabile
                costo_manodopera = total_hours * costo_orario
                margine = None
                margine_pct = None
                if order.prezzo_quotato and order.prezzo_quotato > 0:
                    margine = order.prezzo_quotato - costo_manodopera
                    margine_pct = round((margine / order.prezzo_quotato) * 100, 1)

                # Info lotti per archivio (tempi aggregati)
                lotti_detail = []
                if order.lotto_numero and order.lotto_numero > 0:
                    child_lotti = session.query(Order).filter(
                        Order.parent_order_id == order.id,
                        Order.is_deleted == False
                    ).order_by(Order.lotto_numero.asc()).all()
                    # Aggiungi tempi dei lotti figli ai tempi fase del padre
                    for l in child_lotti:
                        l_phase_times = ArchiveManager._calculate_phase_times(l.id, session)
                        l_total_hours = OrderManager._calculate_total_hours(l.id, session)
                        lotti_detail.append({
                            'lotto_numero': l.lotto_numero,
                            'lotto_nome': l.lotto_nome or '',
                            'phase_times': l_phase_times,
                            'total_hours': round(l_total_hours, 2)
                        })
                        # Somma ai tempi totali
                        for fase_key, tempo in l_phase_times.items():
                            if fase_key in phase_times:
                                # Somma secondi
                                phase_times[fase_key] = (phase_times.get(fase_key, 0) or 0) + (tempo or 0) if isinstance(tempo, (int, float)) else phase_times[fase_key]
                        total_hours += l_total_hours
                    # Ricalcola costo e margine con lotti inclusi
                    costo_manodopera = total_hours * costo_orario
                    if order.prezzo_quotato and order.prezzo_quotato > 0:
                        margine = order.prezzo_quotato - costo_manodopera
                        margine_pct = round((margine / order.prezzo_quotato) * 100, 1)

                orders_data.append({
                    'id': order.id,
                    'numero_ordine': order.numero_ordine or order.id[:8],
                    'cliente': order.cliente,
                    'data_consegna': order.data_consegna.isoformat(),
                    'data_completamento': completion_date.isoformat() if completion_date else None,
                    'tempo_totale': total_time,
                    'prezzo_quotato': order.prezzo_quotato,
                    'costo_manodopera': round(costo_manodopera, 2),
                    'margine': round(margine, 2) if margine is not None else None,
                    'margine_pct': margine_pct,
                    'phase_times': phase_times,
                    'status': order.status,
                    'lotto_numero': order.lotto_numero or 0,
                    'lotto_nome': order.lotto_nome or '',
                    'lotti_detail': lotti_detail,
                    # Dati chiusura amministrativa
                    'numero_ddt': order.numero_ddt or '',
                    'data_ddt': order.data_ddt.isoformat() if order.data_ddt else '',
                    'numero_fattura': order.numero_fattura or '',
                    'data_fattura': order.data_fattura.isoformat() if order.data_fattura else '',
                    'note_chiusura': order.note_chiusura or '',
                    'data_chiusura_amministrativa': order.data_chiusura_amministrativa.isoformat() if order.data_chiusura_amministrativa else '',
                })

            total_pages = (total + limit - 1) // limit

            return {
                'orders': orders_data,
                'total': total,
                'page': page,
                'pages': total_pages
            }
        finally:
            session.close()

    @staticmethod
    def get_order_details(order_id: str) -> dict | None:
        """
        Recupera dettagli completi di un ordine con fasi e tempi
        """
        session = get_session()
        try:
            order = session.query(Order).filter(Order.id == order_id).first()
            if not order:
                return None

            # Recupera processing steps
            steps = session.query(ProcessingStep).filter(
                ProcessingStep.order_id == order_id
            ).order_by(ProcessingStep.fase).all()

            # Calcola tempi per fase
            phase_times = ArchiveManager._calculate_phase_times(order_id, session)

            # Recupera notifica di completamento
            notification = session.query(OrderNotification).filter(
                OrderNotification.order_id == order_id
            ).first()

            # Calcola data di completamento (ultima fase)
            completion_date = None
            if steps:
                last_completed = [s for s in steps if s.timestamp_fine]
                if last_completed:
                    completion_date = max(s.timestamp_fine for s in last_completed)

            # Calcola margine
            total_hours = OrderManager._calculate_total_hours(order_id, session)
            costo_orario = 25.0
            costo_manodopera = total_hours * costo_orario
            margine = None
            margine_pct = None
            if order.prezzo_quotato and order.prezzo_quotato > 0:
                margine = order.prezzo_quotato - costo_manodopera
                margine_pct = round((margine / order.prezzo_quotato) * 100, 1)

            return {
                'id': order.id,
                'numero_ordine': order.numero_ordine or order.id[:8],
                'cliente': order.cliente,
                'data_consegna': order.data_consegna.isoformat(),
                'data_completamento': completion_date.isoformat() if completion_date else None,
                'tempo_totale': notification.tempi_totali if notification else "N/A",
                'prezzo_quotato': order.prezzo_quotato,
                'costo_manodopera': round(costo_manodopera, 2),
                'margine': round(margine, 2) if margine is not None else None,
                'margine_pct': margine_pct,
                'note': order.note or '',
                'fasi': [
                    {
                        'fase': step.fase,
                        'operatore': step.operatore or 'N/A',
                        'data_inizio': step.timestamp_inizio.isoformat() if step.timestamp_inizio else None,
                        'data_fine': step.timestamp_fine.isoformat() if step.timestamp_fine else None,
                        'tempo': phase_times.get(step.fase, 'N/A'),
                        'fase_successiva': step.fase_successiva,
                        'completamento_parziale': step.completamento_parziale,
                        'sessioni': [
                            {
                                'timestamp_inizio': s.timestamp_inizio.isoformat() if s.timestamp_inizio else None,
                                'timestamp_fine': s.timestamp_fine.isoformat() if s.timestamp_fine else None,
                                'tipo_chiusura': s.tipo_chiusura,
                                'operatore': s.operatore,
                                'durata_secondi': int((s.timestamp_fine - s.timestamp_inizio).total_seconds()) if s.timestamp_fine and s.timestamp_inizio else None
                            }
                            for s in session.query(PhaseSession).filter(
                                PhaseSession.step_id == step.id
                            ).order_by(PhaseSession.timestamp_inizio).all()
                        ]
                    }
                    for step in steps
                ],
                'status': order.status,
                # Dati chiusura amministrativa
                'numero_ddt': order.numero_ddt or '',
                'data_ddt': order.data_ddt.isoformat() if order.data_ddt else '',
                'numero_fattura': order.numero_fattura or '',
                'data_fattura': order.data_fattura.isoformat() if order.data_fattura else '',
                'note_chiusura': order.note_chiusura or '',
                'data_chiusura_amministrativa': order.data_chiusura_amministrativa.isoformat() if order.data_chiusura_amministrativa else '',
                'support_requests': [{
                    'id': sr.id,
                    'operatore_principale': sr.operatore_principale,
                    'nome_principale': (session.query(User).filter(User.id == sr.operatore_principale).first() or User(name='')).name,
                    'operatore_supporto': sr.operatore_supporto,
                    'nome_supporto': (session.query(User).filter(User.id == sr.operatore_supporto).first() or User(name='')).name,
                    'stato': sr.stato
                } for sr in session.query(SupportRequest).filter(
                    SupportRequest.order_id == order.id,
                    SupportRequest.stato.in_(['pending', 'accepted'])
                ).all()]
            }
        finally:
            session.close()

    @staticmethod
    def export_csv_data(filters: dict = None) -> list[dict]:
        """Esporta dati di archivio in formato CSV (una riga per ordine)"""
        session = get_session()
        try:
            query = session.query(Order).filter(
                Order.status.in_(["CHIUSO", "PARZIALE"])
            )
            query = ArchiveManager._apply_archive_filters(query, filters, session)
            orders = query.order_by(Order.data_consegna.desc()).all()

            csv_data = []
            for order in orders:
                notification = session.query(OrderNotification).filter(
                    OrderNotification.order_id == order.id
                ).first()

                last_step = session.query(ProcessingStep).filter(
                    ProcessingStep.order_id == order.id,
                    ProcessingStep.timestamp_fine.isnot(None)
                ).order_by(ProcessingStep.timestamp_fine.desc()).first()

                completion_date = last_step.timestamp_fine if last_step else None
                phase_times = ArchiveManager._calculate_phase_times(order.id, session)
                total_hours = OrderManager._calculate_total_hours(order.id, session)
                costo_orario = 25.0
                costo_manodopera = total_hours * costo_orario
                margine = None
                if order.prezzo_quotato and order.prezzo_quotato > 0:
                    margine = order.prezzo_quotato - costo_manodopera

                csv_data.append({
                    'ID Ordine': order.numero_ordine or order.id[:8],
                    'Cliente': order.cliente,
                    'Data Consegna': order.data_consegna.strftime('%Y-%m-%d'),
                    'Data Completamento': completion_date.strftime('%Y-%m-%d %H:%M') if completion_date else 'N/A',
                    'PIEGA': phase_times.get('PIEGA', '-'),
                    'SALDATURA': phase_times.get('SALDATURA', '-'),
                    'PULIZIA': phase_times.get('PULIZIA', '-'),
                    'Tempo Totale': notification.tempi_totali if notification else 'N/A',
                    'Prezzo Quotato': f"{order.prezzo_quotato:.2f}" if order.prezzo_quotato else 'N/A',
                    'Costo Manodopera': f"{costo_manodopera:.2f}",
                    'Margine': f"{margine:.2f}" if margine is not None else 'N/A',
                    'Status': order.status,
                    'Note': order.note or ''
                })

            return csv_data
        finally:
            session.close()

    @staticmethod
    def export_excel_data(filters: dict = None) -> list[dict]:
        """
        Esporta dati di archivio con UNA RIGA PER OPERATORE PER FASE.
        Ogni operatore (principale, supporto, delegato) ha la sua riga.
        A fine ordine, riga riepilogativa con somma tempi.
        Ritorna (rows, summary_row_indices) per formattazione Excel.
        """
        session = get_session()
        try:
            query = session.query(Order).filter(
                Order.status.in_(["CHIUSO", "PARZIALE"]),
                Order.parent_order_id.is_(None)  # Solo ordini padre/normali
            )
            query = ArchiveManager._apply_archive_filters(query, filters, session)
            orders = query.order_by(Order.data_consegna.desc()).all()

            rows = []
            summary_indices = []  # indici righe riepilogo

            for order in orders:
                # Raccoglie tutti gli ordini da processare (padre + eventuali lotti figli)
                all_order_objs = [order]
                if order.lotto_numero and order.lotto_numero > 0:
                    child_lotti = session.query(Order).filter(
                        Order.parent_order_id == order.id,
                        Order.is_deleted == False
                    ).order_by(Order.lotto_numero.asc()).all()
                    all_order_objs.extend(child_lotti)
                order_total_sec = 0

                for current_order in all_order_objs:
                    lotto_label = f" (L{current_order.lotto_numero})" if current_order.lotto_numero and current_order.lotto_numero > 0 else ""
                    lotto_col = (current_order.lotto_nome or f'L{current_order.lotto_numero}') if current_order.lotto_numero and current_order.lotto_numero > 0 else ''

                    notification = session.query(OrderNotification).filter(
                        OrderNotification.order_id == current_order.id
                    ).first()

                    last_step = session.query(ProcessingStep).filter(
                        ProcessingStep.order_id == current_order.id,
                        ProcessingStep.timestamp_fine.isnot(None)
                    ).order_by(ProcessingStep.timestamp_fine.desc()).first()
                    completion_date = last_step.timestamp_fine if last_step else None

                    steps = session.query(ProcessingStep).filter(
                        ProcessingStep.order_id == current_order.id
                    ).order_by(ProcessingStep.timestamp_inizio).all()

                    base_info = {
                        'Cliente': order.cliente,
                        'Numero Ordine': (order.numero_ordine or order.id[:8]) + lotto_label,
                        'Lotto': lotto_col,
                        'Data Caricamento': order.data_ricezione.strftime('%d/%m/%Y %H:%M') if order.data_ricezione else '',
                        'Data Completamento': completion_date.strftime('%d/%m/%Y %H:%M') if completion_date else '',
                    }

                    if not steps:
                        continue

                    for step in steps:
                        sessions_list = session.query(PhaseSession).filter(
                            PhaseSession.step_id == step.id
                        ).order_by(PhaseSession.timestamp_inizio).all()

                        # Raggruppa sessioni per operatore
                        op_sessions = {}
                        for s in sessions_list:
                            op_name = s.operatore or 'Sconosciuto'
                            op_sessions.setdefault(op_name, []).append(s)

                        main_op = step.operatore or ''
                        fase_inizio = step.timestamp_inizio.strftime('%d/%m/%Y %H:%M') if step.timestamp_inizio else ''
                        fase_fine = step.timestamp_fine.strftime('%d/%m/%Y %H:%M') if step.timestamp_fine else ''

                        if op_sessions:
                            for op_name, op_ss in op_sessions.items():
                                op_sec = sum(
                                    int((s.timestamp_fine - s.timestamp_inizio).total_seconds())
                                    for s in op_ss if s.timestamp_fine
                                )
                                order_total_sec += op_sec
                                ruolo = 'Principale' if op_name == main_op else 'Supporto'

                                rows.append({
                                    **base_info,
                                    'Fase': step.fase + lotto_label,
                                    'Operatore': op_name,
                                    'Ruolo': ruolo,
                                    'Inizio': fase_inizio,
                                    'Fine': fase_fine,
                                    'Tempo Lavorato': ArchiveManager._format_seconds(op_sec),
                                    'Sessioni': str(len(op_ss)),
                                })
                        elif step.timestamp_inizio and step.timestamp_fine:
                            dur_sec = int((step.timestamp_fine - step.timestamp_inizio).total_seconds())
                            order_total_sec += dur_sec
                            rows.append({
                                **base_info,
                                'Fase': step.fase + lotto_label,
                                'Operatore': main_op,
                                'Ruolo': 'Principale',
                                'Inizio': fase_inizio,
                                'Fine': fase_fine,
                                'Tempo Lavorato': ArchiveManager._format_seconds(dur_sec),
                                'Sessioni': '1',
                            })
                        else:
                            rows.append({
                                **base_info,
                                'Fase': step.fase + lotto_label,
                                'Operatore': main_op,
                                'Ruolo': 'Principale',
                                'Inizio': fase_inizio,
                                'Fine': fase_fine,
                                'Tempo Lavorato': '',
                                'Sessioni': '',
                            })

                        # Riga delegato se presente
                        delega = session.query(PhaseDelegation).filter(
                            PhaseDelegation.order_id == current_order.id,
                            PhaseDelegation.fase == step.fase,
                            PhaseDelegation.stato == 'completed'
                        ).first()
                        if delega:
                            delegato_user = session.query(User).filter(User.id == delega.operatore_delegato).first()
                            del_name = delegato_user.name if delegato_user else delega.operatore_delegato
                            del_sec = delega.durata_effettiva or 0
                            order_total_sec += del_sec
                            rows.append({
                                **base_info,
                                'Fase': step.fase + lotto_label,
                                'Operatore': del_name,
                                'Ruolo': 'Delegato',
                                'Inizio': '',
                                'Fine': '',
                                'Tempo Lavorato': ArchiveManager._format_seconds(del_sec) if del_sec else '',
                                'Sessioni': '',
                            })

                # Riga riepilogativa ordine (totale di tutti i lotti)
                summary_indices.append(len(rows))
                rows.append({
                    'Cliente': '',
                    'Numero Ordine': '',
                    'Lotto': '',
                    'Data Caricamento': '',
                    'Data Completamento': '',
                    'Fase': 'RIEPILOGO',
                    'Operatore': f"{order.cliente} #{order.numero_ordine or order.id[:8]}",
                    'Ruolo': '',
                    'Inizio': '',
                    'Fine': '',
                    'Tempo Lavorato': ArchiveManager._format_seconds(order_total_sec) if order_total_sec > 0 else '',
                    'Sessioni': '',
                })

            return rows, summary_indices
        finally:
            session.close()

    @staticmethod
    def _format_seconds(total_sec: int) -> str:
        """Formatta secondi in 'Xh YYmin'"""
        if total_sec <= 0:
            return '0min'
        hours = total_sec // 3600
        minutes = (total_sec % 3600) // 60
        if hours > 0:
            return f"{hours}h {minutes:02d}min"
        return f"{minutes}min"

    @staticmethod
    def get_archive_operators() -> list[str]:
        """Ritorna lista nomi operatori che hanno lavorato su ordini completati"""
        session = get_session()
        try:
            operators = session.query(ProcessingStep.operatore).join(
                Order, Order.id == ProcessingStep.order_id
            ).filter(
                Order.status.in_(["CHIUSO", "PARZIALE"]),
                ProcessingStep.operatore.isnot(None)
            ).distinct().all()
            return sorted([op[0] for op in operators if op[0]])
        finally:
            session.close()

    @staticmethod
    def get_archive_clients() -> list[str]:
        """Ritorna lista clienti con ordini completati"""
        session = get_session()
        try:
            clients = session.query(Order.cliente).filter(
                Order.status.in_(["CHIUSO", "PARZIALE"])
            ).distinct().all()
            return sorted([c[0] for c in clients if c[0]])
        finally:
            session.close()


class FatturazioneManager:
    """Gestore ordini da fatturare — chiusura amministrativa (DDT/Fattura)"""

    @staticmethod
    def get_ordini_da_fatturare(filters: dict = None, page: int = 1, limit: int = 20,
                                 sort_by: str = 'data_consegna', sort_dir: str = 'asc') -> dict:
        """Recupera ordini in stato DA_FATTURARE con paginazione e filtri"""
        session = get_session()
        try:
            query = session.query(Order).filter(
                Order.status == "DA_FATTURARE",
                Order.is_deleted == False,
                Order.parent_order_id.is_(None)
            )

            # Filtri
            if filters:
                if filters.get('cliente'):
                    query = query.filter(Order.cliente.ilike(f"%{filters['cliente']}%"))
                if filters.get('numero_ordine'):
                    query = query.filter(Order.numero_ordine.ilike(f"%{filters['numero_ordine']}%"))
                if filters.get('date_from'):
                    date_from = datetime.fromisoformat(filters['date_from'])
                    query = query.filter(Order.data_consegna >= date_from)
                if filters.get('date_to'):
                    date_to = datetime.fromisoformat(filters['date_to'])
                    date_to = date_to.replace(hour=23, minute=59, second=59)
                    query = query.filter(Order.data_consegna <= date_to)

            total = query.count()

            # Ordinamento
            ALLOWED_SORT = {'data_consegna', 'cliente', 'numero_ordine', 'data_ricezione'}
            if sort_by not in ALLOWED_SORT:
                sort_by = 'data_consegna'
            if sort_dir.lower() == 'desc':
                query = query.order_by(getattr(Order, sort_by).desc())
            else:
                query = query.order_by(getattr(Order, sort_by).asc())

            offset = (page - 1) * limit
            orders = query.offset(offset).limit(limit).all()

            orders_data = []
            for order in orders:
                notification = session.query(OrderNotification).filter(
                    OrderNotification.order_id == order.id
                ).first()
                total_time = notification.tempi_totali if notification else "N/A"

                last_step = session.query(ProcessingStep).filter(
                    ProcessingStep.order_id == order.id,
                    ProcessingStep.timestamp_fine.isnot(None)
                ).order_by(ProcessingStep.timestamp_fine.desc()).first()
                completion_date = last_step.timestamp_fine if last_step else None

                phase_times = ArchiveManager._calculate_phase_times(order.id, session)
                total_hours = OrderManager._calculate_total_hours(order.id, session)
                costo_orario = 25.0
                costo_manodopera = total_hours * costo_orario
                margine = None
                margine_pct = None
                if order.prezzo_quotato and order.prezzo_quotato > 0:
                    margine = order.prezzo_quotato - costo_manodopera
                    margine_pct = round((margine / order.prezzo_quotato) * 100, 1)

                # File PDF allegato
                pdf_file = session.query(OrderFile).filter(
                    OrderFile.order_id == order.id,
                    OrderFile.file_type == 'PDF'
                ).first()

                orders_data.append({
                    'id': order.id,
                    'numero_ordine': order.numero_ordine or order.id[:8],
                    'cliente': order.cliente,
                    'data_consegna': order.data_consegna.isoformat() if order.data_consegna else None,
                    'data_ricezione': order.data_ricezione.isoformat() if order.data_ricezione else None,
                    'data_completamento': completion_date.isoformat() if completion_date else None,
                    'tempo_totale': total_time,
                    'prezzo_quotato': order.prezzo_quotato,
                    'costo_manodopera': round(costo_manodopera, 2),
                    'margine': round(margine, 2) if margine is not None else None,
                    'margine_pct': margine_pct,
                    'phase_times': phase_times,
                    'note': order.note or '',
                    'has_pdf': pdf_file is not None,
                    # Dati bozza DDT/fattura (se salvati in precedenza)
                    'numero_ddt': order.numero_ddt or '',
                    'data_ddt': order.data_ddt.isoformat() if order.data_ddt else '',
                    'numero_fattura': order.numero_fattura or '',
                    'data_fattura': order.data_fattura.isoformat() if order.data_fattura else '',
                    'note_chiusura': order.note_chiusura or '',
                })

            total_pages = (total + limit - 1) // limit if total > 0 else 1

            return {
                'orders': orders_data,
                'total': total,
                'page': page,
                'pages': total_pages
            }
        finally:
            session.close()

    @staticmethod
    def get_count() -> int:
        """Ritorna il conteggio ordini DA_FATTURARE (per badge)"""
        session = get_session()
        try:
            return session.query(Order).filter(
                Order.status == "DA_FATTURARE",
                Order.is_deleted == False,
                Order.parent_order_id.is_(None)
            ).count()
        finally:
            session.close()

    @staticmethod
    def salva_bozza(order_id: str, data: dict) -> dict:
        """Salva dati DDT/fattura come bozza senza chiudere l'ordine"""
        session = get_session()
        try:
            order = session.query(Order).filter(Order.id == order_id).first()
            if not order:
                return {"success": False, "error": "Ordine non trovato"}
            if order.status not in ("DA_FATTURARE",):
                return {"success": False, "error": "Ordine non in stato DA_FATTURARE"}

            if 'numero_ddt' in data:
                order.numero_ddt = data['numero_ddt'] or None
            if 'data_ddt' in data and data['data_ddt']:
                order.data_ddt = datetime.fromisoformat(data['data_ddt'])
            elif 'data_ddt' in data:
                order.data_ddt = None
            if 'numero_fattura' in data:
                order.numero_fattura = data['numero_fattura'] or None
            if 'data_fattura' in data and data['data_fattura']:
                order.data_fattura = datetime.fromisoformat(data['data_fattura'])
            elif 'data_fattura' in data:
                order.data_fattura = None
            if 'note_chiusura' in data:
                order.note_chiusura = data['note_chiusura'] or None

            session.commit()
            return {"success": True, "order_id": order_id}

        except Exception as e:
            session.rollback()
            return {"success": False, "error": str(e)}
        finally:
            session.close()

    @staticmethod
    def chiudi_ordine(order_id: str, data: dict, user_id: str) -> dict:
        """Chiude ordine amministrativamente — DDT/fattura + status → CHIUSO"""
        session = get_session()
        try:
            order = session.query(Order).filter(Order.id == order_id).first()
            if not order:
                return {"success": False, "error": "Ordine non trovato"}
            if order.status not in ("DA_FATTURARE",):
                return {"success": False, "error": "Ordine non in stato DA_FATTURARE"}

            now = datetime.utcnow()

            # Salva dati amministrativi
            if data.get('numero_ddt'):
                order.numero_ddt = data['numero_ddt']
            if data.get('data_ddt'):
                order.data_ddt = datetime.fromisoformat(data['data_ddt'])
            if data.get('numero_fattura'):
                order.numero_fattura = data['numero_fattura']
            if data.get('data_fattura'):
                order.data_fattura = datetime.fromisoformat(data['data_fattura'])
            if data.get('note_chiusura'):
                order.note_chiusura = data['note_chiusura']

            order.data_chiusura_amministrativa = now
            order.chiuso_da = user_id
            order.status = "CHIUSO"

            session.commit()
            return {
                "success": True,
                "order_id": order_id,
                "status": "CHIUSO",
                "data_chiusura": now.isoformat()
            }

        except Exception as e:
            session.rollback()
            return {"success": False, "error": str(e)}
        finally:
            session.close()

    @staticmethod
    def riapri_ordine(order_id: str) -> dict:
        """Riapre un ordine CHIUSO riportandolo a DA_FATTURARE"""
        session = get_session()
        try:
            order = session.query(Order).filter(Order.id == order_id).first()
            if not order:
                return {"success": False, "error": "Ordine non trovato"}
            if order.status != "CHIUSO":
                return {"success": False, "error": "Solo ordini CHIUSO possono essere riaperti"}

            order.status = "DA_FATTURARE"
            order.data_chiusura_amministrativa = None
            order.chiuso_da = None

            session.commit()
            return {"success": True, "order_id": order_id, "status": "DA_FATTURARE"}

        except Exception as e:
            session.rollback()
            return {"success": False, "error": str(e)}
        finally:
            session.close()


class NotificationManager:
    """Gestore notifiche UI tipo WhatsApp per supervisore/admin"""

    @staticmethod
    def create_notification(user_id: str, order_id: str, title: str, message: str,
                           notification_type: str = 'order', notification_category: str = 'informativa') -> dict:
        """Crea una notifica e la salva nel DB"""
        session = get_session()
        try:
            notification = Notification(
                id=str(uuid.uuid4()),
                user_id=user_id,
                order_id=order_id,
                title=title,
                message=message,
                notification_type=notification_type,
                notification_category=notification_category,
                is_read=False,
                is_deleted=False
            )
            session.add(notification)
            session.commit()
            return {
                'id': notification.id,
                'timestamp': notification.timestamp.isoformat() + 'Z',
                'user_id': user_id,
                'order_id': order_id,
                'title': title,
                'message': message,
                'notification_type': notification_type,
                'notification_category': notification_category,
                'is_read': notification.is_read,
                'is_deleted': notification.is_deleted
            }
        except Exception as e:
            session.rollback()
            logger.error(f"create_notification: {e}")
            return None
        finally:
            session.close()

    @staticmethod
    def get_notifications(user_id: str, limit: int = 50, unread_only: bool = False) -> list:
        """Recupera notifiche per un utente (non cancellate)"""
        session = get_session()
        try:
            query = session.query(Notification).filter(
                Notification.user_id == user_id,
                Notification.is_deleted == False
            )

            if unread_only:
                query = query.filter(Notification.is_read == False)

            notifications = query.order_by(Notification.timestamp.desc()).limit(limit).all()

            result = []
            for n in notifications:
                result.append({
                    'id': n.id,
                    'timestamp': n.timestamp.isoformat() + 'Z',
                    'user_id': n.user_id,
                    'order_id': n.order_id,
                    'title': n.title,
                    'message': n.message,
                    'notification_type': n.notification_type,
                    'notification_category': getattr(n, 'notification_category', 'informativa') or 'informativa',
                    'is_read': n.is_read,
                    'is_deleted': n.is_deleted
                })
            return result
        except Exception as e:
            logger.error(f"get_notifications: {e}")
            return []
        finally:
            session.close()

    @staticmethod
    def delete_notification(notification_id: str) -> bool:
        """Soft delete di una notifica (is_deleted = True)"""
        session = get_session()
        try:
            notification = session.query(Notification).filter(
                Notification.id == notification_id
            ).first()

            if notification:
                notification.is_deleted = True
                session.commit()
                return True
            return False
        except Exception as e:
            session.rollback()
            logger.error(f"delete_notification: {e}")
            return False
        finally:
            session.close()

    @staticmethod
    def delete_all_notifications(user_id: str) -> bool:
        """Cancella tutte le notifiche di un utente (soft delete)"""
        session = get_session()
        try:
            notifications = session.query(Notification).filter(
                Notification.user_id == user_id,
                Notification.is_deleted == False
            ).all()

            for n in notifications:
                n.is_deleted = True

            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"delete_all_notifications: {e}")
            return False
        finally:
            session.close()

    @staticmethod
    def mark_as_read(notification_id: str) -> bool:
        """Marca una notifica come letta"""
        session = get_session()
        try:
            notification = session.query(Notification).filter(
                Notification.id == notification_id
            ).first()

            if notification:
                notification.is_read = True
                session.commit()
                return True
            return False
        except Exception as e:
            session.rollback()
            logger.error(f"mark_as_read: {e}")
            return False
        finally:
            session.close()

    @staticmethod
    def get_unread_count(user_id: str) -> int:
        """Conta notifiche non lette per un utente"""
        session = get_session()
        try:
            count = session.query(Notification).filter(
                Notification.user_id == user_id,
                Notification.is_read == False,
                Notification.is_deleted == False
            ).count()
            return count
        except Exception as e:
            logger.error(f"get_unread_count: {e}")
            return 0
        finally:
            session.close()

    @staticmethod
    def cleanup_old_notifications(days: int = 30) -> int:
        """Elimina notifiche lette più vecchie di N giorni. Ritorna il numero eliminato."""
        session = get_session()
        try:
            cutoff = datetime.utcnow() - timedelta(days=days)
            deleted = session.query(Notification).filter(
                Notification.is_read == True,
                Notification.timestamp < cutoff
            ).delete()
            session.commit()
            return deleted
        except Exception as e:
            session.rollback()
            logging.error(f"[NOTIF] Errore cleanup: {e}")
            return 0
        finally:
            session.close()


class AlertManager:
    """Gestore alert automatici per capo officina"""

    @staticmethod
    def check_alerts() -> list[dict]:
        """Controlla condizioni di alert e genera notifiche se necessario"""
        session = get_session()
        alerts = []
        now = datetime.utcnow()

        try:
            # 1. Timer attivo da > 4 ore
            active_steps = session.query(ProcessingStep).filter(
                ProcessingStep.timestamp_inizio.isnot(None),
                ProcessingStep.timestamp_fine.is_(None)
            ).all()

            for step in active_steps:
                elapsed = now - step.timestamp_inizio
                hours = elapsed.total_seconds() / 3600

                if hours > 4:
                    order = session.query(Order).filter(Order.id == step.order_id).first()
                    alerts.append({
                        'type': 'warning',
                        'category': 'timer_lungo',
                        'order_id': step.order_id,
                        'cliente': order.cliente if order else 'N/A',
                        'fase': step.fase,
                        'operatore': step.operatore,
                        'ore': round(hours, 1),
                        'message': f'Timer attivo da {round(hours, 1)}h su {step.fase} — {order.cliente if order else "N/A"}'
                    })

            # 2. Ordine fermo nella stessa fase > 8 ore (senza step attivi)
            active_orders = session.query(Order).filter(
                Order.fase_corrente.notin_(['COMPLETATO', 'PARZIALE']),
                Order.status != 'ARCHIVIATO'
            ).all()

            for order in active_orders:
                # Ultimo step per questo ordine
                last_step = session.query(ProcessingStep).filter(
                    ProcessingStep.order_id == order.id
                ).order_by(ProcessingStep.timestamp_inizio.desc()).first()

                if last_step and last_step.timestamp_fine:
                    idle = now - last_step.timestamp_fine
                    idle_hours = idle.total_seconds() / 3600

                    if idle_hours > 8:
                        alerts.append({
                            'type': 'danger',
                            'category': 'ordine_fermo',
                            'order_id': order.id,
                            'cliente': order.cliente,
                            'fase': order.fase_corrente,
                            'operatore': order.operatore_assegnato,
                            'ore': round(idle_hours, 1),
                            'message': f'Ordine fermo in {order.fase_corrente} da {round(idle_hours, 1)}h — {order.cliente}'
                        })

            # 3. Scadenza domani e ordine non completato
            tomorrow = now.replace(hour=23, minute=59, second=59) + timedelta(days=1)

            urgent_orders = session.query(Order).filter(
                Order.data_consegna <= tomorrow,
                Order.fase_corrente.notin_(['COMPLETATO', 'PARZIALE']),
                Order.status != 'ARCHIVIATO'
            ).all()

            for order in urgent_orders:
                days_left = (order.data_consegna - now).days
                if days_left <= 1:
                    alerts.append({
                        'type': 'urgent',
                        'category': 'scadenza',
                        'order_id': order.id,
                        'cliente': order.cliente,
                        'fase': order.fase_corrente,
                        'operatore': order.operatore_assegnato,
                        'scadenza': order.data_consegna.isoformat(),
                        'message': f'Scadenza {"OGGI" if days_left <= 0 else "domani"}: {order.cliente} (fase {order.fase_corrente})'
                    })

            return alerts

        except Exception as e:
            logger.error(f"check_alerts: {e}")
            return []
        finally:
            session.close()


class KPIManager:
    """KPI dashboard semplificato.

    NOTA: la versione precedente calcolava medie su PhaseSession (fasi
    produttive ormai dismesse). Ora ritorna conteggi raw sugli ordini —
    i KPI veri ora vivono in BarcodeManager.get_kpi_operai().
    """

    @staticmethod
    def get_dashboard_kpi() -> dict:
        session = get_session()
        try:
            STATI_FINALI = ('COMPLETATO', 'DA_FATTURARE', 'CHIUSO', 'SPEDITO')
            now = datetime.utcnow()
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            all_orders = session.query(Order).filter(Order.is_deleted == False).all()  # noqa: E712
            tot = len(all_orders)
            aperti = sum(1 for o in all_orders if o.status not in STATI_FINALI)
            chiusi = tot - aperti
            in_ritardo = sum(
                1 for o in all_orders
                if o.status not in STATI_FINALI and o.data_consegna and o.data_consegna < now
            )
            ricevuti_oggi = sum(
                1 for o in all_orders
                if o.data_ricezione and o.data_ricezione >= today_start
            )
            return {
                'totale_ordini': tot,
                'ordini_aperti': aperti,
                'ordini_chiusi': chiusi,
                'in_ritardo': in_ritardo,
                'ricevuti_oggi': ricevuti_oggi,
                'operai': [],  # backward compat: chi consuma il vecchio shape non rompe
            }
        finally:
            session.close()



# === LEGACY (deprecated 2026-06-29) ===
# SupportManager: il sistema non assegna più clienti agli operatori
# e non usa più deleghe/support a fasi. Classe lasciata per
# retrocompat solo lettura. Non chiamare i metodi di scrittura.
class SupportManager:
    """Gestione richieste di supporto — collaborazione su ordini interi"""

    @staticmethod
    def create_support_request(order_id: str, op_principale: str, op_supporto: str,
                               forzata: bool = False, note: str = "") -> dict:
        """Crea una richiesta di supporto per un ordine"""
        session = get_session()
        try:
            order = session.query(Order).filter(Order.id == order_id).first()
            if not order:
                return {'success': False, 'error': 'Ordine non trovato'}
            if order.status in ('COMPLETATO', 'DA_FATTURARE', 'CHIUSO'):
                return {'success': False, 'error': 'Ordine già completato'}

            # Verifica duplicati attivi
            existing = session.query(SupportRequest).filter(
                SupportRequest.order_id == order_id,
                SupportRequest.operatore_supporto == op_supporto,
                SupportRequest.stato.in_(['pending', 'accepted'])
            ).first()
            if existing:
                return {'success': False, 'error': 'Richiesta di supporto già attiva per questo operatore'}

            req_id = str(uuid.uuid4())
            sr = SupportRequest(
                id=req_id,
                order_id=order_id,
                operatore_principale=op_principale,
                operatore_supporto=op_supporto,
                stato='accepted' if forzata else 'pending',
                forzata=forzata,
                data_richiesta=datetime.utcnow(),
                data_risposta=datetime.utcnow() if forzata else None,
                note=note
            )
            session.add(sr)

            # Nomi per notifiche
            principale = session.query(User).filter(User.id == op_principale).first()
            supporto = session.query(User).filter(User.id == op_supporto).first()
            nome_p = principale.name if principale else op_principale
            nome_s = supporto.name if supporto else op_supporto
            cliente = order.cliente
            numero = order.numero_ordine or order.id[:8]

            if forzata:
                # Notifica al supporto: assegnato dal responsabile
                NotificationManager.create_notification(
                    user_id=op_supporto, order_id=order_id,
                    title='Supporto assegnato',
                    message=f'Sei stato assegnato in supporto a {nome_p} per ordine {cliente} #{numero} (assegnato dal responsabile)',
                    notification_type='order', notification_category='attiva'
                )
            else:
                # Notifica al supporto: richiesta di aiuto
                NotificationManager.create_notification(
                    user_id=op_supporto, order_id=order_id,
                    title='Richiesta di aiuto',
                    message=f'{nome_p} ti chiede aiuto per ordine {cliente} #{numero}',
                    notification_type='order', notification_category='attiva'
                )

            session.commit()

            # Se forzata (auto-accepted), avvia fase per il supporter
            if forzata and order.fase_corrente:
                try:
                    OrderManager.start_phase(order_id, order.fase_corrente, nome_s)
                except Exception as e:
                    logger.warning(f"auto-start phase for forced supporter: {e}")

            return {'success': True, 'support_request_id': req_id}
        except Exception as e:
            session.rollback()
            logger.error(f"create_support_request: {e}")
            return {'success': False, 'error': str(e)}
        finally:
            session.close()

    @staticmethod
    def accept_support_request(request_id: str, operatore_id: str) -> dict:
        """Accetta una richiesta di supporto"""
        session = get_session()
        try:
            sr = session.query(SupportRequest).filter(SupportRequest.id == request_id).first()
            if not sr:
                return {'success': False, 'error': 'Richiesta non trovata'}
            if sr.operatore_supporto != operatore_id:
                return {'success': False, 'error': 'Non autorizzato'}
            if sr.stato != 'pending':
                return {'success': False, 'error': f'Richiesta non in stato pending (stato: {sr.stato})'}

            sr.stato = 'accepted'
            sr.data_risposta = datetime.utcnow()

            # Notifica al principale
            supporto = session.query(User).filter(User.id == sr.operatore_supporto).first()
            order = session.query(Order).filter(Order.id == sr.order_id).first()
            nome_s = supporto.name if supporto else sr.operatore_supporto
            cliente = order.cliente if order else ''
            numero = (order.numero_ordine or order.id[:8]) if order else ''

            NotificationManager.create_notification(
                user_id=sr.operatore_principale, order_id=sr.order_id,
                title='Supporto accettato',
                message=f'{nome_s} ha accettato di aiutarti per ordine {cliente} #{numero}',
                notification_type='order', notification_category='attiva'
            )

            session.commit()

            # Auto-avvia fase per l'operatore di supporto (timer personale indipendente)
            if order and order.fase_corrente:
                try:
                    OrderManager.start_phase(sr.order_id, order.fase_corrente, nome_s)
                except Exception as e:
                    logger.warning(f"auto-start phase for supporter failed: {e}")

            return {'success': True}
        except Exception as e:
            session.rollback()
            logger.error(f"accept_support_request: {e}")
            return {'success': False, 'error': str(e)}
        finally:
            session.close()

    @staticmethod
    def reject_support_request(request_id: str, operatore_id: str) -> dict:
        """Rifiuta una richiesta di supporto"""
        session = get_session()
        try:
            sr = session.query(SupportRequest).filter(SupportRequest.id == request_id).first()
            if not sr:
                return {'success': False, 'error': 'Richiesta non trovata'}
            if sr.operatore_supporto != operatore_id:
                return {'success': False, 'error': 'Non autorizzato'}
            if sr.stato != 'pending':
                return {'success': False, 'error': f'Richiesta non in stato pending (stato: {sr.stato})'}

            sr.stato = 'rejected'
            sr.data_risposta = datetime.utcnow()

            # Notifica al principale
            supporto = session.query(User).filter(User.id == sr.operatore_supporto).first()
            order = session.query(Order).filter(Order.id == sr.order_id).first()
            nome_s = supporto.name if supporto else sr.operatore_supporto
            cliente = order.cliente if order else ''
            numero = (order.numero_ordine or order.id[:8]) if order else ''

            NotificationManager.create_notification(
                user_id=sr.operatore_principale, order_id=sr.order_id,
                title='Supporto rifiutato',
                message=f'{nome_s} ha rifiutato il supporto per ordine {cliente} #{numero}',
                notification_type='order', notification_category='attiva'
            )

            session.commit()
            return {'success': True}
        except Exception as e:
            session.rollback()
            logger.error(f"reject_support_request: {e}")
            return {'success': False, 'error': str(e)}
        finally:
            session.close()

    @staticmethod
    def revoke_support_request(request_id: str) -> dict:
        """Revoca una richiesta di supporto (capo o operatore principale)"""
        session = get_session()
        try:
            sr = session.query(SupportRequest).filter(SupportRequest.id == request_id).first()
            if not sr:
                return {'success': False, 'error': 'Richiesta non trovata'}
            if sr.stato not in ['pending', 'accepted']:
                return {'success': False, 'error': 'Richiesta non revocabile'}

            sr.stato = 'revoked'
            sr.data_risposta = datetime.utcnow()

            # Notifica al supporto
            principale = session.query(User).filter(User.id == sr.operatore_principale).first()
            order = session.query(Order).filter(Order.id == sr.order_id).first()
            nome_p = principale.name if principale else sr.operatore_principale
            cliente = order.cliente if order else ''

            NotificationManager.create_notification(
                user_id=sr.operatore_supporto, order_id=sr.order_id,
                title='Supporto revocato',
                message=f'Il supporto per ordine {cliente} di {nome_p} è stato revocato',
                notification_type='order', notification_category='informativa'
            )

            session.commit()
            return {'success': True}
        except Exception as e:
            session.rollback()
            logger.error(f"revoke_support_request: {e}")
            return {'success': False, 'error': str(e)}
        finally:
            session.close()

    @staticmethod
    def get_support_requests(order_id: str = None, op_principale: str = None,
                             op_supporto: str = None, stato: str = None,
                             active_only: bool = False) -> list:
        """Recupera richieste di supporto con filtri"""
        session = get_session()
        try:
            query = session.query(SupportRequest)
            if order_id:
                query = query.filter(SupportRequest.order_id == order_id)
            if op_principale:
                query = query.filter(SupportRequest.operatore_principale == op_principale)
            if op_supporto:
                query = query.filter(SupportRequest.operatore_supporto == op_supporto)
            if stato:
                query = query.filter(SupportRequest.stato == stato)
            if active_only:
                query = query.filter(SupportRequest.stato.in_(['pending', 'accepted']))

            requests = query.order_by(SupportRequest.data_richiesta.desc()).all()
            return [SupportManager._serialize_support_request(sr, session) for sr in requests]
        except Exception as e:
            logger.error(f"get_support_requests: {e}")
            return []
        finally:
            session.close()

    @staticmethod
    def get_supported_order_ids(operatore_id: str) -> list:
        """Restituisce gli order_id per cui l'operatore è in supporto attivo"""
        session = get_session()
        try:
            requests = session.query(SupportRequest.order_id).filter(
                SupportRequest.operatore_supporto == operatore_id,
                SupportRequest.stato == 'accepted'
            ).all()
            return [r[0] for r in requests]
        except Exception as e:
            logger.error(f"get_supported_order_ids: {e}")
            return []
        finally:
            session.close()

    @staticmethod
    def _serialize_support_request(sr, session) -> dict:
        """Serializza una richiesta di supporto"""
        order = session.query(Order).filter(Order.id == sr.order_id).first()
        principale = session.query(User).filter(User.id == sr.operatore_principale).first()
        supporto = session.query(User).filter(User.id == sr.operatore_supporto).first()
        return {
            'id': sr.id,
            'order_id': sr.order_id,
            'cliente': order.cliente if order else '',
            'numero_ordine': (order.numero_ordine or order.id[:8]) if order else '',
            'operatore_principale': sr.operatore_principale,
            'nome_principale': principale.name if principale else '',
            'operatore_supporto': sr.operatore_supporto,
            'nome_supporto': supporto.name if supporto else '',
            'stato': sr.stato,
            'forzata': sr.forzata,
            'data_richiesta': sr.data_richiesta.isoformat() + 'Z' if sr.data_richiesta else None,
            'data_risposta': sr.data_risposta.isoformat() + 'Z' if sr.data_risposta else None,
            'note': sr.note
        }


# ============================================================================
#  BARCODE / OFFICINA SCAN — rilevazione tempi via pistola WiFi
# ============================================================================

def _valida(errori):
    """Passa attraverso: serve solo a rendere leggibili le chiamate sopra."""
    return errori


def _msg_valida(errori):
    from .preventivi.validazione import messaggio
    return messaggio(errori)


def _v_articoli(x):
    from .preventivi.validazione import valida_articoli
    return valida_articoli(x)


def _v_assiemi(x):
    from .preventivi.validazione import valida_assiemi
    return valida_assiemi(x)


def _v_tubolari(x):
    from .preventivi.validazione import valida_tubolari
    return valida_tubolari(x)


def _v_piastre(x):
    from .preventivi.validazione import valida_piastre
    return valida_piastre(x)


def _totale_concordato(preventivo: dict):
    """Prezzo su cui il cliente ha detto di si'.

    E' il "TOTALE ORDINE" del PDF, cioe' `totale_lotto`. Se manca si ripiega
    sul totale pezzo per la quantita'; se non c'e' nemmeno quello si lascia
    vuoto invece di scrivere zero, che sarebbe un prezzo confermato falso.
    """
    try:
        v = preventivo.get('totale_lotto')
        if v not in (None, '', 0):
            return float(v)
        pezzo = preventivo.get('totale_pezzo_con_margine') or preventivo.get('totale_pezzo')
        qta = preventivo.get('quantita') or 1
        if pezzo:
            return round(float(pezzo) * int(qta), 2)
    except (TypeError, ValueError):
        pass
    return None


def _cartella_disegni(order) -> str:
    """Percorso della cartella disegni dell'ordine. Import differito: la logica
    sta in app.py, che conosce configurazione e percorsi."""
    try:
        from .app import _cartella_disegni_ordine
        return _cartella_disegni_ordine(order)
    except Exception:
        return ''


def _fase_ordine(order) -> str:
    """Fase amministrativa dell'ordine (aperto / pronto_ddt / consegnato /
    archivio). Import differito: ordini_service dipende da questo modulo."""
    try:
        from .ordini_service import fase
        return fase(order)
    except Exception:
        return 'aperto'


class BarcodeManager:
    """Gestione scan barcode officina e KPI derivate.

    Ogni operaio ha una pistola WiFi (registrata nella tabella Pistola). Quando
    scansiona il cartellino di un ordine, il sistema apre una OfficinaScan.
    Un solo ordine attivo per operaio: scansione di un ordine diverso chiude
    automaticamente il precedente.
    """

    # ---- Pistole CRUD --------------------------------------------------------

    @staticmethod
    def list_pistole(include_inactive: bool = True) -> list[dict]:
        session = get_session()
        try:
            q = session.query(Pistola)
            if not include_inactive:
                q = q.filter(Pistola.attiva == True)  # noqa: E712
            rows = q.order_by(Pistola.created_at.asc()).all()
            users = {u.id: u for u in session.query(User).all()}
            out = []
            for p in rows:
                u = users.get(p.operatore_id)
                out.append({
                    'id': p.id,
                    'pistola_id': p.pistola_id,
                    'operatore_id': p.operatore_id,
                    'operatore_name': u.name if u else '',
                    'attiva': bool(p.attiva),
                    'note': p.note or '',
                    'created_at': p.created_at.isoformat() + 'Z' if p.created_at else None,
                })
            return out
        finally:
            session.close()

    @staticmethod
    def create_pistola(pistola_id: str, operatore_id: str, note: str = '') -> dict:
        """Registra una nuova pistola. Errore se pistola_id già usato o operatore inesistente."""
        pistola_id = (pistola_id or '').strip()
        if not pistola_id:
            return {'error': 'pistola_id obbligatorio'}
        session = get_session()
        try:
            if session.query(Pistola).filter(Pistola.pistola_id == pistola_id).first():
                return {'error': f'pistola_id "{pistola_id}" già registrato'}
            if not session.query(User).filter(User.id == operatore_id).first():
                return {'error': f'operatore "{operatore_id}" non trovato'}
            p = Pistola(
                id=str(uuid.uuid4()),
                pistola_id=pistola_id,
                operatore_id=operatore_id,
                attiva=True,
                note=note or None,
            )
            session.add(p)
            session.commit()
            return {'ok': True, 'id': p.id, 'pistola_id': p.pistola_id}
        except Exception as e:
            session.rollback()
            logger.error('create_pistola failed: %s', e)
            return {'error': str(e)}
        finally:
            session.close()

    @staticmethod
    def update_pistola(pistola_uuid: str, operatore_id: str = None,
                        attiva: bool = None, note: str = None) -> dict:
        session = get_session()
        try:
            p = session.query(Pistola).filter(Pistola.id == pistola_uuid).first()
            if not p:
                return {'error': 'Pistola non trovata'}
            if operatore_id is not None:
                if not session.query(User).filter(User.id == operatore_id).first():
                    return {'error': f'operatore "{operatore_id}" non trovato'}
                p.operatore_id = operatore_id
            if attiva is not None:
                p.attiva = bool(attiva)
            if note is not None:
                p.note = note or None
            session.commit()
            return {'ok': True}
        except Exception as e:
            session.rollback()
            return {'error': str(e)}
        finally:
            session.close()

    @staticmethod
    def delete_pistola(pistola_uuid: str) -> dict:
        session = get_session()
        try:
            p = session.query(Pistola).filter(Pistola.id == pistola_uuid).first()
            if not p:
                return {'error': 'Pistola non trovata'}
            session.delete(p)
            session.commit()
            return {'ok': True}
        except Exception as e:
            session.rollback()
            return {'error': str(e)}
        finally:
            session.close()

    # ---- Scan ---------------------------------------------------------------

    @staticmethod
    def _find_order_by_code(session, codice: str) -> Order | None:
        """Trova ordine per numero_ordine esatto; fallback su id LIKE codice%."""
        codice = (codice or '').strip()
        if not codice:
            return None
        # Match esatto su numero_ordine
        o = session.query(Order).filter(
            Order.numero_ordine == codice,
            Order.is_deleted == False  # noqa: E712
        ).first()
        if o:
            return o
        # Fallback: prefisso UUID (utile in test)
        if len(codice) >= 6:
            o = session.query(Order).filter(
                Order.id.like(f'{codice}%'),
                Order.is_deleted == False  # noqa: E712
            ).first()
        return o

    @staticmethod
    def process_scan(pistola_id: str, codice: str) -> dict:
        """Logica cuore: apre/chiude OfficinaScan in base alla pistola+codice.

        Ritorna dict con status code suggerito ('status_code') e payload.
        """
        session = get_session()
        try:
            if not BarcodeManager.pistole_attive():
                logger.info('Scan ignorata (pistole dismesse): pistola=%s codice=%s',
                            pistola_id, codice)
                return {'status_code': 410,
                        'error': 'Rilevazione con pistola disattivata. '
                                 'Le ore si dichiarano dal tablet in officina.'}

            pistola_id = (pistola_id or '').strip()
            if not pistola_id:
                return {'status_code': 400, 'error': 'pistola_id mancante'}

            pist = session.query(Pistola).filter(
                Pistola.pistola_id == pistola_id,
                Pistola.attiva == True,  # noqa: E712
            ).first()
            if not pist:
                logger.warning('Scan rifiutata: pistola "%s" sconosciuta o inattiva', pistola_id)
                return {'status_code': 401, 'error': 'Pistola non registrata o inattiva'}

            operatore_id = pist.operatore_id
            order = BarcodeManager._find_order_by_code(session, codice)
            if not order:
                logger.warning('Scan rifiutata: codice "%s" non trovato', codice)
                return {'status_code': 404, 'error': f'Ordine "{codice}" non trovato'}

            # Blocco scansioni su ordini gia` chiusi
            if order.status in ('CHIUSO', 'SPEDITO'):
                logger.warning('Scan rifiutata: ordine "%s" gia` chiuso (%s)', codice, order.status)
                return {'status_code': 409,
                        'error': f'Ordine "{codice}" gia` chiuso'}

            now = datetime.utcnow()

            # AUTO-CONFERMA TAGLIO (workflow B): se un operaio officina scansiona il
            # cartellino, il bancale coi pezzi è già fisicamente davanti a lui → il
            # taglio è per forza avvenuto. Invece di bloccare la scan finché il laser
            # non preme "Taglio completato" (dipendenza manuale che congela l'ordine
            # se dimenticata), marchiamo qui il taglio come completato al primo scan a
            # valle, registrando quale operaio l'ha fatto partire. Il pulsante manuale
            # del laser/capo resta valido come pre-conferma.
            if not getattr(order, 'taglio_completato', False):
                order.taglio_completato = True
                order.data_taglio_completato = now
                order.taglio_completato_da = operatore_id or None
                logger.info('Taglio auto-confermato per ordine "%s" al primo scan officina (op=%s)',
                            codice, operatore_id)
                try:
                    AuditManager.log(
                        user_id=operatore_id, action='MARK_LASER_DONE',
                        entity_type='order', entity_id=order.id,
                        detail='Taglio auto-confermato al primo scan officina',
                    )
                except Exception:
                    pass

            # Sessione attiva di quest'operaio (max 1)
            active = session.query(OfficinaScan).filter(
                OfficinaScan.operatore_id == operatore_id,
                OfficinaScan.timestamp_fine == None,  # noqa: E711
            ).order_by(OfficinaScan.timestamp_inizio.desc()).first()

            azione = None
            if active is None:
                # Nessuna sessione attiva: apri nuova
                BarcodeManager._open_scan(session, order.id, operatore_id, pistola_id, now)
                azione = 'aperta'
            elif active.order_id == order.id:
                # Stesso ordine: idempotente (protegge da doppie pressioni)
                azione = 'idempotente'
            else:
                # Ordine diverso: chiudi precedente, apri nuova
                active.timestamp_fine = now
                active.chiusura_motivo = 'altro_ordine'
                session.flush()
                BarcodeManager._open_scan(session, order.id, operatore_id, pistola_id, now)
                azione = 'cambio_ordine'

            # Audit
            try:
                AuditManager.log(
                    user_id=operatore_id,
                    user_name=(pist.operatore_id),
                    action='SCAN_BARCODE',
                    entity_type='order',
                    entity_id=order.id,
                    detail=json.dumps({
                        'pistola_id': pistola_id,
                        'codice': codice,
                        'azione': azione,
                    }, ensure_ascii=False),
                )
            except Exception:
                pass

            session.commit()

            tempo_cumulato = BarcodeManager._tempo_cumulato_secondi(session, order.id)
            user = session.query(User).filter(User.id == operatore_id).first()

            return {
                'status_code': 200,
                'ok': True,
                'azione': azione,
                'operatore_id': operatore_id,
                'operatore_name': user.name if user else '',
                'ordine_id': order.id,
                'numero_ordine': order.numero_ordine or order.id[:8],
                'cliente': order.cliente,
                'tempo_cumulato_secondi': tempo_cumulato,
            }
        except Exception as e:
            session.rollback()
            logger.exception('process_scan failed')
            return {'status_code': 500, 'error': str(e)}
        finally:
            session.close()

    @staticmethod
    def _open_scan(session, order_id: str, operatore_id: str,
                    pistola_id: str, ts: datetime) -> OfficinaScan:
        s = OfficinaScan(
            id=str(uuid.uuid4()),
            order_id=order_id,
            operatore_id=operatore_id,
            pistola_id=pistola_id,
            timestamp_inizio=ts,
            timestamp_fine=None,
            chiusura_motivo=None,
        )
        session.add(s)
        session.flush()
        return s

    # ---- Orario lavorativo (per scorporo automatico pausa pranzo / notte) ----

    @staticmethod
    def _get_finestre_lavorative():
        """Ritorna le finestre lavorative giornaliere come [(h,m,h,m), ...].
        Default azienda: 07:30-12:00 + 13:30-17:00. Letto da app_config.json.
        """
        cfg = BarcodeManager.load_config()
        raw = cfg.get('orario_lavoro') or [['07:30', '12:00'], ['13:30', '17:00']]
        out = []
        for w in raw:
            try:
                s_hh, s_mm = (int(x) for x in str(w[0]).split(':'))
                e_hh, e_mm = (int(x) for x in str(w[1]).split(':'))
                if 0 <= s_hh <= 23 and 0 <= e_hh <= 23 and 0 <= s_mm <= 59 and 0 <= e_mm <= 59:
                    out.append((s_hh, s_mm, e_hh, e_mm))
            except Exception:
                pass
        return out or [(7, 30, 12, 0), (13, 30, 17, 0)]

    @staticmethod
    def _utc_to_local_offset():
        """Offset (timedelta) per convertire un timestamp UTC naive in ora locale del server.
        I timestamp delle scan sono salvati con datetime.utcnow(); le finestre sono in ora locale.
        """
        return datetime.now() - datetime.utcnow()

    @staticmethod
    def _durata_lavorativa_secondi(inizio_utc, fine_utc, finestre=None) -> int:
        """Secondi *lavorativi* tra due timestamp UTC, intersecando con le finestre
        lavorative giornaliere (esclude pausa pranzo e ore non lavorative).

        Esempio: scan 11:30 → 14:30 con default 07:30-12:00+13:30-17:00
                 ritorna 5400 secondi (90 min), non 10800 (180 min).
        """
        if not fine_utc or not inizio_utc or fine_utc <= inizio_utc:
            return 0
        if finestre is None:
            finestre = BarcodeManager._get_finestre_lavorative()
        offset = BarcodeManager._utc_to_local_offset()
        inizio = inizio_utc + offset
        fine = fine_utc + offset
        total = 0
        cur_date = inizio.date()
        end_date = fine.date()
        # Safety cap: scan multi-giorno > 60 giorni → tronca (anomalia, evita loop lunghi)
        if (end_date - cur_date).days > 60:
            end_date = cur_date + timedelta(days=60)
        while cur_date <= end_date:
            for (sh, sm, eh, em) in finestre:
                win_start = datetime(cur_date.year, cur_date.month, cur_date.day, sh, sm)
                win_end = datetime(cur_date.year, cur_date.month, cur_date.day, eh, em)
                seg_start = max(inizio, win_start)
                seg_end = min(fine, win_end)
                if seg_end > seg_start:
                    total += int((seg_end - seg_start).total_seconds())
            cur_date += timedelta(days=1)
        return total

    @staticmethod
    def _tempo_cumulato_secondi(session, order_id: str, include_open: bool = True) -> int:
        """Somma secondi *lavorativi* su tutte le scan dell'ordine. Include le scan
        aperte (calcolando now - inizio) se include_open=True.
        Pausa pranzo e ore non lavorative sono scorporate automaticamente.
        """
        rows = session.query(OfficinaScan).filter(
            OfficinaScan.order_id == order_id
        ).all()
        now = datetime.utcnow()
        tot = 0
        for r in rows:
            if r.timestamp_fine:
                tot += BarcodeManager._durata_lavorativa_secondi(r.timestamp_inizio, r.timestamp_fine)
            elif include_open and r.timestamp_inizio:
                tot += BarcodeManager._durata_lavorativa_secondi(r.timestamp_inizio, now)
        return tot

    # ---- Chiusura di gruppo --------------------------------------------------

    @staticmethod
    def close_open_scans(order_id: str, motivo: str = 'manuale') -> int:
        """Chiude tutte le scan aperte di un ordine. Ritorna numero chiuse."""
        session = get_session()
        try:
            now = datetime.utcnow()
            rows = session.query(OfficinaScan).filter(
                OfficinaScan.order_id == order_id,
                OfficinaScan.timestamp_fine == None,  # noqa: E711
            ).all()
            for r in rows:
                r.timestamp_fine = now
                r.chiusura_motivo = motivo
            session.commit()
            return len(rows)
        except Exception as e:
            session.rollback()
            logger.exception('close_open_scans failed: %s', e)
            return 0
        finally:
            session.close()

    @staticmethod
    def close_residual_scans(motivo: str = 'fine_turno') -> int:
        """Chiude tutte le scan ancora aperte nel sistema (es. fine turno)."""
        session = get_session()
        try:
            now = datetime.utcnow()
            rows = session.query(OfficinaScan).filter(
                OfficinaScan.timestamp_fine == None,  # noqa: E711
            ).all()
            for r in rows:
                r.timestamp_fine = now
                r.chiusura_motivo = motivo
            session.commit()
            return len(rows)
        except Exception as e:
            session.rollback()
            logger.exception('close_residual_scans failed: %s', e)
            return 0
        finally:
            session.close()

    # ---- Live status (per Elena) --------------------------------------------

    @staticmethod
    def get_live_status() -> dict:
        """Feed per la pagina 'Stato officina live' dell'impiegata."""
        session = get_session()
        try:
            now = datetime.utcnow()

            # Scan attive
            active_rows = session.query(OfficinaScan).filter(
                OfficinaScan.timestamp_fine == None,  # noqa: E711
            ).order_by(OfficinaScan.timestamp_inizio.asc()).all()

            users = {u.id: u for u in session.query(User).all()}
            order_ids = list({r.order_id for r in active_rows})
            orders = {o.id: o for o in session.query(Order).filter(Order.id.in_(order_ids)).all()} if order_ids else {}

            scan_attive = []
            for r in active_rows:
                u = users.get(r.operatore_id)
                o = orders.get(r.order_id)
                if not o:
                    continue
                minuti = BarcodeManager._durata_lavorativa_secondi(r.timestamp_inizio, now) // 60
                scan_attive.append({
                    'operatore_id': r.operatore_id,
                    'operatore_name': u.name if u else '',
                    'ordine_id': o.id,
                    'numero_ordine': o.numero_ordine or o.id[:8],
                    'cliente': o.cliente,
                    'fase_corrente': o.fase_corrente,
                    'minuti_correnti': minuti,
                    'timestamp_inizio': r.timestamp_inizio.isoformat() + 'Z',
                })

            # Ordini "in officina": tutti quelli con almeno una scan negli ultimi 30 giorni
            # e non ancora COMPLETATO/SPEDITO/CHIUSO
            cutoff = now - timedelta(days=30)
            recent_order_ids = [
                r[0] for r in session.query(OfficinaScan.order_id).filter(
                    OfficinaScan.timestamp_inizio >= cutoff
                ).distinct().all()
            ]
            if recent_order_ids:
                ord_q = session.query(Order).filter(
                    Order.id.in_(recent_order_ids),
                    Order.is_deleted == False,  # noqa: E712
                    ~Order.fase_corrente.in_(['COMPLETATO']),
                ).all()
                ordini_in_officina = []
                for o in ord_q:
                    sec = BarcodeManager._tempo_cumulato_secondi(session, o.id)
                    ultima = session.query(OfficinaScan).filter(
                        OfficinaScan.order_id == o.id
                    ).order_by(OfficinaScan.timestamp_inizio.desc()).first()
                    ordini_in_officina.append({
                        'ordine_id': o.id,
                        'numero_ordine': o.numero_ordine or o.id[:8],
                        'cliente': o.cliente,
                        'fase_corrente': o.fase_corrente,
                        'tempo_totale_minuti': sec // 60,
                        'ultima_scan': ultima.timestamp_inizio.isoformat() + 'Z' if ultima else None,
                    })
                ordini_in_officina.sort(key=lambda x: x['ultima_scan'] or '', reverse=True)
            else:
                ordini_in_officina = []

            return {
                'scan_attive': scan_attive,
                'ordini_in_officina': ordini_in_officina,
                'server_time': now.isoformat() + 'Z',
            }
        finally:
            session.close()

    # ---- KPI operai (per pagina capo) ---------------------------------------

    @staticmethod
    def _kpi_operai_da_dichiarazioni() -> list[dict]:
        """Ore per operaio prese dalle DICHIARAZIONI del tablet (non dalle scan).

        Unica fonte delle ore da quando le pistole sono dismesse. Include tutti
        gli operai attivi, anche quelli che non hanno dichiarato nulla: uno zero
        visibile e' piu' utile di un operaio che sparisce dalla lista.
        """
        from datetime import date as _date
        from .models_ore import GiornataOre, RigaOre

        session = get_session()
        try:
            oggi = _date.today()
            inizio_settimana = oggi - timedelta(days=oggi.weekday())
            inizio_mese = _date(oggi.year, oggi.month, 1)
            da = min(inizio_settimana, inizio_mese)

            users = session.query(User).filter(
                User.is_active == True,  # noqa: E712
                User.role.like('Operaio%'),
            ).all()
            if not users:
                return []

            righe = session.query(GiornataOre.operatore_id, GiornataOre.data,
                                  RigaOre.minuti).join(
                RigaOre, RigaOre.giornata_id == GiornataOre.id
            ).filter(GiornataOre.data >= da).all()

            acc = {}
            for op_id, giorno, minuti in righe:
                v = acc.setdefault(op_id, {'oggi': 0, 'sett': 0, 'mese': 0, 'gg': set()})
                m = int(minuti or 0)
                if giorno == oggi:
                    v['oggi'] += m
                if giorno >= inizio_settimana:
                    v['sett'] += m
                    v['gg'].add(giorno)
                if giorno >= inizio_mese:
                    v['mese'] += m

            out = []
            for u in users:
                v = acc.get(u.id, {'oggi': 0, 'sett': 0, 'mese': 0, 'gg': set()})
                ore_sett = round(v['sett'] / 60.0, 2)
                out.append({
                    'operatore_id': u.id,
                    'nome': u.name,
                    'ore_oggi': round(v['oggi'] / 60.0, 2),
                    'ore_settimana': ore_sett,
                    'ore_mese': round(v['mese'] / 60.0, 2),
                    'saturazione_settimana_pct': round(ore_sett / 40.0 * 100, 1) if ore_sett else 0.0,
                    'numero_scan_settimana': 0,
                    'giorni_dichiarati_settimana': len(v['gg']),
                    'fonte': 'dichiarazioni',
                })
            out.sort(key=lambda x: x['ore_settimana'], reverse=True)
            return out
        finally:
            session.close()

    @staticmethod
    def get_kpi_operai() -> list[dict]:
        """Ore lavorate per operaio: oggi / settimana / mese + saturazione %."""
        if not BarcodeManager.pistole_attive():
            return BarcodeManager._kpi_operai_da_dichiarazioni()
        session = get_session()
        try:
            now = datetime.utcnow()
            inizio_oggi = datetime(now.year, now.month, now.day)
            inizio_settimana = inizio_oggi - timedelta(days=inizio_oggi.weekday())
            inizio_mese = datetime(now.year, now.month, 1)

            # Solo operai con almeno una pistola registrata
            pistole_op_ids = {p.operatore_id for p in session.query(Pistola).all()}
            if not pistole_op_ids:
                return []

            users = session.query(User).filter(User.id.in_(pistole_op_ids)).all()

            out = []
            for u in users:
                rows = session.query(OfficinaScan).filter(
                    OfficinaScan.operatore_id == u.id,
                    OfficinaScan.timestamp_inizio >= inizio_mese,
                ).all()

                def _sec(scans, since):
                    """Somma secondi *lavorativi* delle scan a partire da `since`."""
                    tot = 0
                    for r in scans:
                        start = max(r.timestamp_inizio, since)
                        end = r.timestamp_fine or now
                        if end > start:
                            tot += BarcodeManager._durata_lavorativa_secondi(start, end)
                    return tot

                sec_oggi = _sec(rows, inizio_oggi)
                sec_sett = _sec(rows, inizio_settimana)
                sec_mese = _sec([r for r in rows if r.timestamp_inizio >= inizio_mese], inizio_mese)
                ore_sett = sec_sett / 3600.0
                saturazione = round(ore_sett / 40.0 * 100, 1) if ore_sett else 0.0
                out.append({
                    'operatore_id': u.id,
                    'nome': u.name,
                    'ore_oggi': round(sec_oggi / 3600.0, 2),
                    'ore_settimana': round(ore_sett, 2),
                    'ore_mese': round(sec_mese / 3600.0, 2),
                    'saturazione_settimana_pct': saturazione,
                    'numero_scan_settimana': sum(1 for r in rows if r.timestamp_inizio >= inizio_settimana),
                })
            out.sort(key=lambda x: x['ore_settimana'], reverse=True)
            return out
        finally:
            session.close()

    # ---- Calendario ordini per mese (per pagina capo) -----------------------

    @staticmethod
    def get_calendario_ordini(year: int, month: int) -> dict:
        """Ordini raggruppati per data_consegna nel mese richiesto."""
        session = get_session()
        try:
            from calendar import monthrange
            first = datetime(year, month, 1)
            last_day = monthrange(year, month)[1]
            last = datetime(year, month, last_day, 23, 59, 59)

            rows = session.query(Order).filter(
                Order.data_consegna >= first,
                Order.data_consegna <= last,
                Order.is_deleted == False,  # noqa: E712
            ).order_by(Order.data_consegna.asc()).all()

            out = {}
            for o in rows:
                key = o.data_consegna.strftime('%Y-%m-%d')
                out.setdefault(key, []).append({
                    'id': o.id,
                    'numero_ordine': o.numero_ordine or o.id[:8],
                    'cliente': o.cliente,
                    'fase_corrente': o.fase_corrente,
                    'status': o.status,
                })
            return out
        finally:
            session.close()

    # ---- Tempo officina dettagliato per ordine ------------------------------

    @staticmethod
    def get_tempo_officina(order_id: str) -> dict:
        session = get_session()
        try:
            rows = session.query(OfficinaScan).filter(
                OfficinaScan.order_id == order_id
            ).order_by(OfficinaScan.timestamp_inizio.asc()).all()
            users = {u.id: u for u in session.query(User).all()}
            sessions_out = []
            tot_sec = 0
            now = datetime.utcnow()
            for r in rows:
                end = r.timestamp_fine or now
                dur = BarcodeManager._durata_lavorativa_secondi(r.timestamp_inizio, end)
                tot_sec += dur
                u = users.get(r.operatore_id)
                sessions_out.append({
                    'id': r.id,
                    'operatore_id': r.operatore_id,
                    'operatore_name': u.name if u else '',
                    'pistola_id': r.pistola_id,
                    'inizio': r.timestamp_inizio.isoformat() + 'Z',
                    'fine': r.timestamp_fine.isoformat() + 'Z' if r.timestamp_fine else None,
                    'durata_secondi': dur,
                    'chiusura_motivo': r.chiusura_motivo,
                    'aperta': r.timestamp_fine is None,
                })
            # Conta sessioni attive e giorni distinti di lavorazione (utile
            # per ordini multi-giorno e per il badge "In pausa").
            n_aperte = sum(1 for s in sessions_out if s['aperta'])
            giorni_distinti = len({r.timestamp_inizio.date() for r in rows}) if rows else 0
            prima_scan = rows[0].timestamp_inizio.isoformat() + 'Z' if rows else None
            ultima_scan = rows[-1].timestamp_inizio.isoformat() + 'Z' if rows else None
            return {
                'ordine_id': order_id,
                'tempo_totale_secondi': tot_sec,
                'tempo_totale_minuti': tot_sec // 60,
                'numero_sessioni': len(sessions_out),
                'numero_sessioni_aperte': n_aperte,
                'giorni_distinti': giorni_distinti,
                'prima_scan': prima_scan,
                'ultima_scan': ultima_scan,
                'sessioni': sessions_out,
            }
        finally:
            session.close()

    # ---- Config app (soglie sospetto finito, ecc.) --------------------------

    _CONFIG_PATH = None  # set lazy

    @staticmethod
    def _get_config_path():
        import os
        if BarcodeManager._CONFIG_PATH is None:
            here = os.path.dirname(os.path.abspath(__file__))
            BarcodeManager._CONFIG_PATH = os.path.join(here, '..', 'app_config.json')
        return BarcodeManager._CONFIG_PATH

    @staticmethod
    def load_config() -> dict:
        """Carica config app da JSON. Ritorna default se file mancante/corrotto."""
        import os
        defaults = {
            # Le pistole barcode sono DISMESSE: le ore si dichiarano dal tablet
            # (vedi ore_service). Il flag resta per riattivarle e per non
            # rompere lo storico gia' registrato in officina_scan.
            'pistole_attive': False,
            'sospetto_giorni_dal_taglio': 5,
            'sospetto_giorni_da_ultima_scan': 3,
            # Criterio "ordine fermo" quando le pistole sono spente: giorni
            # dall'arrivo dell'ordine senza completamento operativo registrato.
            'sospetto_giorni_apertura': 10,
            'fine_turno_hhmm': '17:30',
            'orario_lavoro': [['07:30', '12:00'], ['13:30', '17:00']],
        }
        path = BarcodeManager._get_config_path()
        if not os.path.exists(path):
            return defaults
        try:
            with open(path, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
            # Merge con default per garantire chiavi minime
            return {**defaults, **{k: v for k, v in cfg.items() if not k.startswith('_')}}
        except Exception as e:
            logger.warning('load_config failed (using defaults): %s', e)
            return defaults

    @staticmethod
    def save_config(updates: dict) -> dict:
        """Aggiorna chiavi del config. Ritorna config aggiornata."""
        import os
        path = BarcodeManager._get_config_path()
        # Carica corrente preservando commenti _
        current = {}
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    current = json.load(f)
            except Exception:
                pass
        # Applica updates (allowlist per sicurezza)
        # - int scalar: chiavi di soglia timing
        # - dict object: sezioni di config strutturate (laser_config, preventivi_config,
        #   dxf_detection). Vengono sostituite in blocco.
        int_keys = {'sospetto_giorni_dal_taglio', 'sospetto_giorni_da_ultima_scan',
                    'sospetto_giorni_apertura'}
        dict_keys = {'laser_config', 'preventivi_config', 'dxf_detection'}
        str_keys = {'disegni_export_root'}  # path cartella export DXF puliti per officina
        bool_keys = {'pistole_attive'}
        for k, v in (updates or {}).items():
            if k in bool_keys:
                current[k] = bool(v) if not isinstance(v, str) else v.strip().lower() in ('1', 'true', 'si', 'on')
            elif k in int_keys:
                try:
                    current[k] = int(v)
                except (ValueError, TypeError):
                    pass
            elif k in dict_keys and isinstance(v, dict):
                current[k] = v
            elif k in str_keys:
                # String allowlist (paths). Trim + accetta stringa vuota per disabilitare.
                try:
                    current[k] = str(v).strip() if v is not None else ''
                except Exception:
                    pass
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(current, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error('save_config failed: %s', e)
            return {'error': str(e)}
        return BarcodeManager.load_config()

    @staticmethod
    def pistole_attive() -> bool:
        """True se la rilevazione tempi con pistola barcode e' ancora in uso.

        Da settembre 2026 e' DISATTIVATA: gli operai dichiarano le ore dal
        tablet (una riga per cliente), quindi la scansione sarebbe un
        passaggio in piu' che non alimenta piu' nulla di essenziale.
        Lo storico in officina_scan resta consultabile.
        """
        try:
            return bool(BarcodeManager.load_config().get('pistole_attive', False))
        except Exception:
            return False

    # ---- Ordini sospetti finiti --------------------------------------------

    @staticmethod
    def _ordini_fermi_senza_scan(cfg: dict) -> list[dict]:
        """Ordini aperti da troppo tempo, SENZA usare le scansioni.

        Dismesse le pistole non esiste piu' un segnale "ultima lavorazione":
        l'unico fatto certo e' che l'ordine e' arrivato da N giorni e nessuno
        in ufficio ne ha ancora registrato il completamento operativo.
        Stesso formato di ritorno del criterio storico, cosi' le pagine che lo
        consumano non cambiano.
        """
        gg = int(cfg.get('sospetto_giorni_apertura', 10) or 10)
        session = get_session()
        try:
            now = datetime.utcnow()
            soglia = now - timedelta(days=gg)
            orders = session.query(Order).filter(
                Order.is_deleted == False,  # noqa: E712
                Order.status == 'RICEVUTO',
                Order.data_completamento_operativo == None,  # noqa: E711
                Order.data_ricezione <= soglia,
            ).all()

            out = []
            for o in orders:
                giorni = int((now - o.data_ricezione).total_seconds() / 86400) if o.data_ricezione else gg
                out.append({
                    'id': o.id,
                    'numero_ordine': o.numero_ordine or o.id[:8],
                    'cliente': o.cliente,
                    'data_consegna': o.data_consegna.isoformat() if o.data_consegna else None,
                    'data_taglio_completato': o.data_taglio_completato.isoformat() + 'Z' if o.data_taglio_completato else None,
                    'ultima_scan': None,
                    'giorni_inattivo': giorni,
                    'tempo_totale_minuti': 0,
                    'numero_scan': 0,
                    'motivo': f'Aperto da {giorni} giorni senza completamento registrato',
                })
            out.sort(key=lambda x: x['giorni_inattivo'], reverse=True)
            return out
        finally:
            session.close()

    @staticmethod
    def get_ordini_sospetti_finiti() -> list[dict]:
        """Ritorna ordini che probabilmente sono finiti ma nessuno li ha chiusi.

        Criteri (configurabili da app_config.json):
        - status ancora 'RICEVUTO' (cioè non DA_FATTURARE/CHIUSO/SPEDITO)
        - taglio_completato = True (sennò non è mai entrato in officina)
        - taglio completato da almeno N giorni
        - ultima scansione officina da almeno M giorni (oppure mai scansionato dopo il taglio)
        """
        cfg = BarcodeManager.load_config()
        if not bool(cfg.get('pistole_attive', False)):
            return BarcodeManager._ordini_fermi_senza_scan(cfg)

        gg_taglio = int(cfg.get('sospetto_giorni_dal_taglio', 5))
        gg_scan = int(cfg.get('sospetto_giorni_da_ultima_scan', 3))

        session = get_session()
        try:
            now = datetime.utcnow()
            soglia_taglio = now - timedelta(days=gg_taglio)
            soglia_scan = now - timedelta(days=gg_scan)

            orders = session.query(Order).filter(
                Order.is_deleted == False,  # noqa: E712
                Order.status == 'RICEVUTO',
                Order.taglio_completato == True,  # noqa: E712
                Order.data_taglio_completato <= soglia_taglio,
            ).all()

            out = []
            for o in orders:
                # Verifica ultima scan
                ultima = session.query(OfficinaScan).filter(
                    OfficinaScan.order_id == o.id
                ).order_by(OfficinaScan.timestamp_inizio.desc()).first()

                if ultima:
                    # C'è almeno una scan: verifica che l'ultima sia abbastanza vecchia
                    if ultima.timestamp_inizio > soglia_scan:
                        continue
                    ultima_iso = ultima.timestamp_inizio.isoformat() + 'Z'
                    giorni_inattivo = int((now - ultima.timestamp_inizio).total_seconds() / 86400)
                else:
                    # Nessuna scan dopo il taglio: anche più sospetto
                    ultima_iso = None
                    giorni_inattivo = int((now - o.data_taglio_completato).total_seconds() / 86400)

                # Tempo totale officina (per dare contesto al capo)
                rows = session.query(OfficinaScan).filter(
                    OfficinaScan.order_id == o.id
                ).all()
                tot_sec = 0
                for r in rows:
                    if r.timestamp_fine:
                        tot_sec += int((r.timestamp_fine - r.timestamp_inizio).total_seconds())

                out.append({
                    'id': o.id,
                    'numero_ordine': o.numero_ordine or o.id[:8],
                    'cliente': o.cliente,
                    'data_consegna': o.data_consegna.isoformat() if o.data_consegna else None,
                    'data_taglio_completato': o.data_taglio_completato.isoformat() + 'Z' if o.data_taglio_completato else None,
                    'ultima_scan': ultima_iso,
                    'giorni_inattivo': giorni_inattivo,
                    'tempo_totale_minuti': tot_sec // 60,
                    'numero_scan': len(rows),
                })

            # Ordina dai più "inattivi" ai meno (probabilmente più urgenti)
            out.sort(key=lambda x: x['giorni_inattivo'], reverse=True)
            return out
        finally:
            session.close()


# ============================================================================
#  PREVENTIVI — gestione preventivi (porting Preventivatore desktop)
# ============================================================================

class PreventivoManager:
    """CRUD preventivi + workflow status (BOZZA → INVIATO → ACCETTATO).

    Vedi `backend/preventivi/contract.md` per il modello dati completo.
    L'integrazione con OrderManager (creazione ordine all'accettazione) sarà
    aggiunta in Fase 4 del merge.
    """

    VALID_STATUSES = ('BOZZA', 'INVIATO', 'ACCETTATO', 'RIFIUTATO')

    @staticmethod
    def create(cliente, created_by, *, quantita=1, numero_ordine_cliente=None,
               margine_pct=0.0, data_consegna_proposta=None, note=None,
               da_prezzare=False):
        """Crea un nuovo preventivo in BOZZA.

        da_prezzare=True → richiesta caricata da Elena, in attesa del commerciale.
        """
        session = get_session()
        try:
            p = Preventivo(
                id=str(uuid.uuid4()),
                cliente=cliente.strip(),
                numero_ordine_cliente=(numero_ordine_cliente or '').strip() or None,
                quantita=max(1, int(quantita)),
                margine_pct=float(margine_pct or 0.0),
                data_consegna_proposta=data_consegna_proposta,
                status='BOZZA',
                versione=1,
                created_by=created_by,
                note=note,
                da_prezzare=bool(da_prezzare),
            )
            session.add(p)
            session.commit()
            return PreventivoManager._serialize(p)
        finally:
            session.close()

    @staticmethod
    def duplicate(source_id: str, new_cliente: str, created_by: str,
                   *, copy_articoli: bool = True) -> dict | None:
        """Duplica un preventivo esistente in un nuovo BOZZA per cliente ricorrente.

        Args:
            source_id: id del preventivo sorgente
            new_cliente: nome del cliente per il nuovo preventivo (può essere lo stesso)
            created_by: user id del creatore
            copy_articoli: se True copia anche tutti gli articoli (default True)

        Returns:
            dict del nuovo preventivo con id, oppure None se sorgente non trovato.
            Il nuovo preventivo eredita: quantita, margine_pct, note; NON eredita
            numero_ordine_cliente, data_consegna_proposta (specifici alla commessa).
        """
        session = get_session()
        try:
            src = session.query(Preventivo).filter(
                Preventivo.id == source_id, Preventivo.is_deleted == False  # noqa: E712
            ).first()
            if not src:
                return None
            new_id = str(uuid.uuid4())
            new_p = Preventivo(
                id=new_id,
                cliente=(new_cliente or src.cliente).strip(),
                numero_ordine_cliente=None,
                quantita=src.quantita,
                margine_pct=src.margine_pct,
                data_consegna_proposta=None,  # nuovo preventivo, nuova data
                status='BOZZA',
                versione=1,
                created_by=created_by,
                note=f'[Duplicato da preventivo {source_id[:8]}] ' + (src.note or ''),
            )
            session.add(new_p)
            session.flush()  # per avere new_id disponibile
            if copy_articoli:
                for src_a in session.query(PreventivoArticolo).filter(
                    PreventivoArticolo.preventivo_id == source_id
                ).all():
                    session.add(PreventivoArticolo(
                        id=str(uuid.uuid4()),
                        preventivo_id=new_id,
                        codice=src_a.codice,
                        quantita=src_a.quantita,
                        codice_assieme=src_a.codice_assieme,
                        area=src_a.area,
                        area_dm2=src_a.area_dm2,
                        perimetro_taglio_m=src_a.perimetro_taglio_m,
                        n_forature=src_a.n_forature,
                        spessore_mm=src_a.spessore_mm,
                        materiale=src_a.materiale,
                        dxf_filename=src_a.dxf_filename,
                        cleaned_dxf_filename=src_a.cleaned_dxf_filename,
                        cleaned_status=src_a.cleaned_status,
                        costo_materiale=src_a.costo_materiale,
                        costo_base_stimato=src_a.costo_base_stimato,
                        costo_base_override=src_a.costo_base_override,
                        pieghe=src_a.pieghe,
                        saldatura_ml=src_a.saldatura_ml,
                        filettatura_pz=src_a.filettatura_pz,
                        svasatura_pz=src_a.svasatura_pz,
                        costo_piega=src_a.costo_piega,
                        costo_saldatura=src_a.costo_saldatura,
                        costo_filettatura=src_a.costo_filettatura,
                        costo_svasatura=src_a.costo_svasatura,
                        costo_apporto=src_a.costo_apporto,
                        costo_pulizia=src_a.costo_pulizia,
                    ))
            session.commit()
            return PreventivoManager._serialize(new_p)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def get(preventivo_id, include_children=True):
        """Ritorna preventivo + articoli/assiemi/tubolari/piastre (se include_children)."""
        session = get_session()
        try:
            p = session.query(Preventivo).filter(
                Preventivo.id == preventivo_id,
                Preventivo.is_deleted == False,  # noqa: E712
            ).first()
            if not p:
                return None
            data = PreventivoManager._serialize(p)
            if include_children:
                data['articoli'] = [PreventivoManager._serialize_articolo(a) for a in
                                    session.query(PreventivoArticolo)
                                    .filter(PreventivoArticolo.preventivo_id == preventivo_id)
                                    .order_by(PreventivoArticolo.codice).all()]
                data['assiemi'] = [PreventivoManager._serialize_assieme(a) for a in
                                   session.query(PreventivoAssieme)
                                   .filter(PreventivoAssieme.preventivo_id == preventivo_id).all()]
                data['tubolari'] = [PreventivoManager._serialize_tubolare(t) for t in
                                    session.query(PreventivoTubolare)
                                    .filter(PreventivoTubolare.preventivo_id == preventivo_id).all()]
                data['piastre'] = [PreventivoManager._serialize_piastra(pp) for pp in
                                   session.query(PreventivoPiastra)
                                   .filter(PreventivoPiastra.preventivo_id == preventivo_id).all()]
            return data
        finally:
            session.close()

    @staticmethod
    def list(cliente=None, status=None, limit=200):
        """Lista preventivi non eliminati, ordinati per data_creazione desc."""
        session = get_session()
        try:
            q = session.query(Preventivo).filter(Preventivo.is_deleted == False)  # noqa: E712
            if cliente:
                q = q.filter(Preventivo.cliente.ilike('%' + cliente + '%'))
            if status:
                q = q.filter(Preventivo.status == status)
            rows = q.order_by(Preventivo.data_creazione.desc()).limit(limit).all()
            return [PreventivoManager._serialize(p) for p in rows]
        finally:
            session.close()

    @staticmethod
    def storico_prezzo(codice=None, sha256=None, exclude_preventivo_id=None, limit=50):
        """Storico prezzi di un pezzo già visto in preventivi passati.

        Match: stesso `codice` OPPURE stessa impronta geometria
        (`canonical_dxf_sha256`, popolata quando il pezzo è confermato nel CAD).
        Il costo riportato è il COSTO BASE del pezzo (materiale/taglio +
        lavorazioni), confrontabile tra preventivi diversi: il margine varia per
        commessa, il costo base no. Serve per riconoscere un pezzo già prezzato
        ed evitare incoerenze.

        Returns:
            dict {occorrenze: [...], riepilogo: {...}|None}. `occorrenze` è
            ordinata dalla più recente. `riepilogo` aggrega n/ultimo/min/max/media.
        """
        codice = (codice or '').strip()
        sha256 = (sha256 or '').strip() or None
        if not codice and not sha256:
            return {'occorrenze': [], 'riepilogo': None}
        session = get_session()
        try:
            conds = []
            if codice:
                conds.append(PreventivoArticolo.codice == codice)
            if sha256:
                conds.append(PreventivoArticolo.canonical_dxf_sha256 == sha256)
            q = (session.query(PreventivoArticolo, Preventivo)
                 .join(Preventivo, PreventivoArticolo.preventivo_id == Preventivo.id)
                 .filter(Preventivo.is_deleted == False)  # noqa: E712
                 .filter(or_(*conds)))
            if exclude_preventivo_id:
                q = q.filter(PreventivoArticolo.preventivo_id != exclude_preventivo_id)
            rows = q.order_by(Preventivo.data_creazione.desc()).limit(limit).all()

            occorrenze = []
            for a, p in rows:
                base = (a.costo_base_override if a.costo_base_override is not None
                        else (a.costo_base_stimato or a.costo_materiale or 0.0))
                lav = (float(a.costo_piega or 0) + float(a.costo_saldatura or 0)
                       + float(a.costo_filettatura or 0) + float(a.costo_svasatura or 0)
                       + float(a.costo_apporto or 0) + float(a.costo_pulizia or 0))
                costo_base = round(float(base or 0) + lav, 2)
                # Distingui il tipo di match (utile per l'avviso "rinominato")
                match_tipo = 'codice' if (codice and a.codice == codice) else 'geometria'
                occorrenze.append({
                    'preventivo_id': p.id,
                    'numero_ordine_cliente': p.numero_ordine_cliente,
                    'cliente': p.cliente,
                    'status': p.status,
                    'data': p.data_creazione.strftime('%d/%m/%Y') if p.data_creazione else '',
                    'data_iso': p.data_creazione.isoformat() if p.data_creazione else '',
                    'codice': a.codice,
                    'materiale': a.materiale,
                    'spessore_mm': a.spessore_mm,
                    'area_dm2': a.area_dm2,
                    'quantita': a.quantita,
                    'costo_base': costo_base,
                    'margine_pct': p.margine_pct,
                    'match': match_tipo,
                    'geometria_confermata': bool(getattr(a, 'geometria_manuale_confermata', False)),
                })

            riepilogo = None
            if occorrenze:
                costi = [o['costo_base'] for o in occorrenze]
                riepilogo = {
                    'n': len(occorrenze),
                    'ultimo_costo': occorrenze[0]['costo_base'],
                    'ultimo_cliente': occorrenze[0]['cliente'],
                    'ultima_data': occorrenze[0]['data'],
                    'min': round(min(costi), 2),
                    'max': round(max(costi), 2),
                    'media': round(sum(costi) / len(costi), 2),
                }
            return {'occorrenze': occorrenze, 'riepilogo': riepilogo}
        finally:
            session.close()

    @staticmethod
    def storico_prezzo_batch(items, exclude_preventivo_id=None):
        """Versione batch di storico_prezzo per marcare la LISTA pezzi in una sola
        query (badge 'già prezzato' senza aprire il dettaglio).

        Args:
            items: lista di dict {codice, sha}. `sha` = canonical_dxf_sha256 (opz.).
            exclude_preventivo_id: preventivo da escludere (quello corrente).

        Returns:
            lista di riepiloghi ALLINEATA a `items` (stesso ordine): ciascuno
            {n, ultimo_costo, ultima_data, ultimo_cliente, min, max} oppure None
            se il pezzo non è mai stato prezzato altrove.
        """
        items = items or []
        codici = {(it.get('codice') or '').strip() for it in items}
        codici.discard('')
        shas = {(it.get('sha') or '').strip() for it in items}
        shas.discard('')
        if not codici and not shas:
            return [None] * len(items)
        session = get_session()
        try:
            conds = []
            if codici:
                conds.append(PreventivoArticolo.codice.in_(codici))
            if shas:
                conds.append(PreventivoArticolo.canonical_dxf_sha256.in_(shas))
            q = (session.query(PreventivoArticolo, Preventivo)
                 .join(Preventivo, PreventivoArticolo.preventivo_id == Preventivo.id)
                 .filter(Preventivo.is_deleted == False)  # noqa: E712
                 .filter(or_(*conds)))
            if exclude_preventivo_id:
                q = q.filter(PreventivoArticolo.preventivo_id != exclude_preventivo_id)
            rows = q.order_by(Preventivo.data_creazione.desc()).all()

            by_codice = {}
            by_sha = {}
            for a, p in rows:
                base = (a.costo_base_override if a.costo_base_override is not None
                        else (a.costo_base_stimato or a.costo_materiale or 0.0))
                lav = (float(a.costo_piega or 0) + float(a.costo_saldatura or 0)
                       + float(a.costo_filettatura or 0) + float(a.costo_svasatura or 0)
                       + float(a.costo_apporto or 0) + float(a.costo_pulizia or 0))
                occ = {
                    'art_id': a.id,
                    'costo_base': round(float(base or 0) + lav, 2),
                    'data_iso': p.data_creazione.isoformat() if p.data_creazione else '',
                    'data': p.data_creazione.strftime('%d/%m/%Y') if p.data_creazione else '',
                    'cliente': p.cliente,
                }
                by_codice.setdefault(a.codice, []).append(occ)
                if a.canonical_dxf_sha256:
                    by_sha.setdefault(a.canonical_dxf_sha256, []).append(occ)

            risultati = []
            for it in items:
                cod = (it.get('codice') or '').strip()
                sha = (it.get('sha') or '').strip()
                seen = set()
                matches = []
                for occ in ((by_codice.get(cod, []) if cod else [])
                            + (by_sha.get(sha, []) if sha else [])):
                    if occ['art_id'] in seen:
                        continue
                    seen.add(occ['art_id'])
                    matches.append(occ)
                if not matches:
                    risultati.append(None)
                    continue
                matches.sort(key=lambda o: o['data_iso'], reverse=True)
                costi = [m['costo_base'] for m in matches]
                risultati.append({
                    'n': len(matches),
                    'ultimo_costo': matches[0]['costo_base'],
                    'ultima_data': matches[0]['data'],
                    'ultimo_cliente': matches[0]['cliente'],
                    'min': round(min(costi), 2),
                    'max': round(max(costi), 2),
                })
            return risultati
        finally:
            session.close()

    @staticmethod
    def update(preventivo_id, updates):
        """Aggiorna campi del preventivo. NON cambia status (usare transition_status).
        Se status è 'INVIATO' o 'ACCETTATO' (snapshot immutabili), refuse.
        """
        session = get_session()
        try:
            p = session.query(Preventivo).filter(
                Preventivo.id == preventivo_id,
                Preventivo.is_deleted == False,  # noqa: E712
            ).first()
            if not p:
                return None
            if p.status in ('INVIATO', 'ACCETTATO'):
                return {'error': 'Preventivo ' + p.status + ': immutabile. Crea nuova versione.'}
            editable = {'cliente', 'numero_ordine_cliente', 'quantita', 'margine_pct',
                        'sconto_pct', 'data_consegna_proposta', 'note',
                        'totale_pezzo', 'totale_pezzo_con_margine', 'totale_pezzo_scontato',
                        'totale_lotto', 'costi_montaggio_totale', 'costi_tubolari_totale',
                        'costi_piastre_totale'}
            for k, v in updates.items():
                if k in editable:
                    setattr(p, k, v)
            session.commit()
            return PreventivoManager._serialize(p)
        finally:
            session.close()

    @staticmethod
    def set_email_inviata(preventivo_id, to_addr):
        """Registra l'invio email (indirizzo + timestamp). Metadato sull'invio:
        consentito anche su preventivo INVIATO (non è contenuto del preventivo)."""
        session = get_session()
        try:
            p = session.query(Preventivo).filter(Preventivo.id == preventivo_id).first()
            if not p:
                return None
            p.email_cliente = (to_addr or '').strip() or None
            p.email_inviata_il = datetime.utcnow()
            session.commit()
            return PreventivoManager._serialize(p)
        finally:
            session.close()

    @staticmethod
    def find_by_numero_ordine(numero):
        """Cerca un preventivo NON cancellato con lo stesso numero ordine cliente
        (match trimmato, case-insensitive). Per anti-doppione all'import.
        Ritorna il più recente {id, cliente, numero_ordine_cliente, status,
        data_creazione} o None."""
        n = (numero or '').strip()
        if not n:
            return None
        session = get_session()
        try:
            from sqlalchemy import func
            p = session.query(Preventivo).filter(
                Preventivo.is_deleted == False,  # noqa: E712
                func.lower(func.trim(Preventivo.numero_ordine_cliente)) == n.lower(),
            ).order_by(Preventivo.data_creazione.desc()).first()
            if not p:
                return None
            return {
                'id': p.id, 'cliente': p.cliente,
                'numero_ordine_cliente': p.numero_ordine_cliente,
                'status': p.status,
                'data_creazione': p.data_creazione.isoformat() if p.data_creazione else None,
            }
        finally:
            session.close()

    @staticmethod
    def ultima_email_cliente(cliente):
        """Ultimo indirizzo email usato per un cliente (match esatto sul nome),
        per riproporlo agli invii successivi. None se mai inviato."""
        if not cliente:
            return None
        session = get_session()
        try:
            p = session.query(Preventivo).filter(
                Preventivo.cliente == cliente.strip(),
                Preventivo.email_cliente.isnot(None),
            ).order_by(Preventivo.email_inviata_il.desc()).first()
            return p.email_cliente if p else None
        finally:
            session.close()

    @staticmethod
    def soft_delete(preventivo_id):
        session = get_session()
        try:
            p = session.query(Preventivo).filter(Preventivo.id == preventivo_id).first()
            if not p:
                return False
            p.is_deleted = True
            session.commit()
            return True
        finally:
            session.close()

    @staticmethod
    def replace_articoli(preventivo_id, articoli: list):
        """Sostituisce TUTTI gli articoli del preventivo con la lista passata.
        Usato dal frontend per persistere lo stato dell'editor articoli.
        Atomic: delete tutti i vecchi, insert i nuovi in una transazione.
        Bloccato se preventivo INVIATO/ACCETTATO (immutabile).
        """
        session = get_session()
        try:
            p = session.query(Preventivo).filter(
                Preventivo.id == preventivo_id,
                Preventivo.is_deleted == False,  # noqa: E712
            ).first()
            if not p:
                return {'error': 'Preventivo non trovato'}
            if p.status in ('INVIATO', 'ACCETTATO'):
                return {'error': 'Preventivo ' + p.status + ': immutabile'}
            # Costi negativi, numeri non finiti e quantita' assurde venivano
            # accettati in silenzio e si scoprivano solo da un totale sbagliato.
            _err = _valida(_v_articoli(articoli))
            if _err:
                return {'error': 'Dati non validi — ' + _msg_valida(_err),
                        'dettagli': _err}
            # Delete tutti gli articoli esistenti
            session.query(PreventivoArticolo).filter(
                PreventivoArticolo.preventivo_id == preventivo_id
            ).delete(synchronize_session=False)
            # Insert i nuovi
            for a in articoli or []:
                session.add(PreventivoArticolo(
                    id=str(uuid.uuid4()),
                    preventivo_id=preventivo_id,
                    codice=a.get('codice') or '',
                    quantita=int(a.get('quantita') or 1),
                    codice_assieme=a.get('codice_assieme'),
                    area=float(a.get('area') or 0),
                    area_dm2=float(a.get('area_dm2') or 0),
                    perimetro_taglio_m=float(a.get('perimetro_taglio_m') or 0),
                    n_forature=int(a.get('n_forature') or 0),
                    spessore_mm=a.get('spessore_mm') if a.get('spessore_mm') else None,
                    materiale=a.get('materiale') or None,
                    dxf_filename=a.get('dxf_filename') or a.get('disegno_dxf') or None,
                    cleaned_dxf_filename=a.get('cleaned_dxf_filename') or None,
                    cleaned_status=a.get('cleaned_status') or None,
                    costo_materiale=float(a.get('costo_materiale') or 0),
                    costo_base_stimato=float(a.get('costo_base_stimato') or 0),
                    costo_base_override=a.get('costo_base_override'),
                    pieghe=int(a.get('pieghe') or 0),
                    saldatura_ml=float(a.get('saldatura_ml') or 0),
                    saldatura_min=float(a.get('saldatura_min') or 0),
                    filettatura_pz=int(a.get('filettatura_pz') or 0),
                    svasatura_pz=int(a.get('svasatura_pz') or 0),
                    costo_piega=float(a.get('costo_piega') or 0),
                    costo_saldatura=float(a.get('costo_saldatura') or 0),
                    costo_filettatura=float(a.get('costo_filettatura') or 0),
                    costo_svasatura=float(a.get('costo_svasatura') or 0),
                    costo_apporto=float(a.get('costo_apporto') or 0),
                    costo_pulizia=float(a.get('costo_pulizia') or 0),
                    geometria_manuale_confermata=bool(a.get('geometria_manuale_confermata')),
                    geometry_source=a.get('geometry_source') or None,
                    area_stimata_piega=bool(a.get('area_stimata_piega')),
                    canonical_dxf_filename=a.get('canonical_dxf_filename') or None,
                    canonical_dxf_sha256=a.get('canonical_dxf_sha256') or None,
                ))
            session.commit()
            return {'success': True, 'count': len(articoli or [])}
        except Exception as e:
            session.rollback()
            return {'error': str(e)}
        finally:
            session.close()

    @staticmethod
    def replace_assiemi(preventivo_id, assiemi: list):
        """Sostituisce TUTTI gli assiemi del preventivo. Stesso pattern di replace_articoli."""
        session = get_session()
        try:
            p = session.query(Preventivo).filter(
                Preventivo.id == preventivo_id,
                Preventivo.is_deleted == False,  # noqa: E712
            ).first()
            if not p:
                return {'error': 'Preventivo non trovato'}
            if p.status in ('INVIATO', 'ACCETTATO'):
                return {'error': 'Preventivo ' + p.status + ': immutabile'}
            # Costi negativi, numeri non finiti e quantita' assurde venivano
            # accettati in silenzio e si scoprivano solo da un totale sbagliato.
            _err = _valida(_v_assiemi(assiemi))
            if _err:
                return {'error': 'Dati non validi — ' + _msg_valida(_err),
                        'dettagli': _err}
            session.query(PreventivoAssieme).filter(
                PreventivoAssieme.preventivo_id == preventivo_id
            ).delete(synchronize_session=False)
            for a in assiemi or []:
                session.add(PreventivoAssieme(
                    id=str(uuid.uuid4()),
                    preventivo_id=preventivo_id,
                    codice_assieme=a.get('codice_assieme') or 'ASS',
                    qty=int(a.get('qty') or 1),
                    ore_montaggio=float(a.get('ore_montaggio') or 0),
                    ore_puntatura=float(a.get('ore_puntatura') or 0),
                    costo=float(a.get('costo') or 0),
                    costo_puntatura=float(a.get('costo_puntatura') or 0),
                    costo_saldatura_assieme=float(a.get('costo_saldatura_assieme') or 0),
                    saldatura_mt=float(a.get('saldatura_mt') or 0),
                    peso_kg=float(a.get('peso_kg') or 0),
                    componenti_qty=a.get('componenti_qty') or {},
                ))
            session.commit()
            return {'success': True, 'count': len(assiemi or [])}
        except Exception as e:
            session.rollback()
            return {'error': str(e)}
        finally:
            session.close()

    @staticmethod
    def replace_tubolari(preventivo_id, tubolari: list):
        """Sostituisce TUTTI i tubolari del preventivo."""
        session = get_session()
        try:
            p = session.query(Preventivo).filter(
                Preventivo.id == preventivo_id,
                Preventivo.is_deleted == False,  # noqa: E712
            ).first()
            if not p:
                return {'error': 'Preventivo non trovato'}
            if p.status in ('INVIATO', 'ACCETTATO'):
                return {'error': 'Preventivo ' + p.status + ': immutabile'}
            # Costi negativi, numeri non finiti e quantita' assurde venivano
            # accettati in silenzio e si scoprivano solo da un totale sbagliato.
            _err = _valida(_v_tubolari(tubolari))
            if _err:
                return {'error': 'Dati non validi — ' + _msg_valida(_err),
                        'dettagli': _err}
            session.query(PreventivoTubolare).filter(
                PreventivoTubolare.preventivo_id == preventivo_id
            ).delete(synchronize_session=False)
            for t in tubolari or []:
                session.add(PreventivoTubolare(
                    id=str(uuid.uuid4()),
                    preventivo_id=preventivo_id,
                    codice_assieme=t.get('codice_assieme'),
                    profilo=t.get('profilo') or '',
                    tipo=t.get('tipo'),
                    materiale=t.get('materiale') or 'acciaio',
                    lunghezza_m=float(t.get('lunghezza_m') or 0),
                    peso_kg=float(t.get('peso_kg') or 0),
                    costo_materiale=float(t.get('costo_materiale') or 0),
                    costo_taglio_totale=float(t.get('costo_taglio_totale') or 0),
                    n_tagli_dritti=int(t.get('n_tagli_dritti') or 0),
                    n_tagli_obliqui=int(t.get('n_tagli_obliqui') or 0),
                ))
            session.commit()
            return {'success': True, 'count': len(tubolari or [])}
        except Exception as e:
            session.rollback()
            return {'error': str(e)}
        finally:
            session.close()

    @staticmethod
    def replace_piastre(preventivo_id, piastre: list):
        """Sostituisce TUTTE le piastre del preventivo."""
        session = get_session()
        try:
            p = session.query(Preventivo).filter(
                Preventivo.id == preventivo_id,
                Preventivo.is_deleted == False,  # noqa: E712
            ).first()
            if not p:
                return {'error': 'Preventivo non trovato'}
            if p.status in ('INVIATO', 'ACCETTATO'):
                return {'error': 'Preventivo ' + p.status + ': immutabile'}
            # Costi negativi, numeri non finiti e quantita' assurde venivano
            # accettati in silenzio e si scoprivano solo da un totale sbagliato.
            _err = _valida(_v_piastre(piastre))
            if _err:
                return {'error': 'Dati non validi — ' + _msg_valida(_err),
                        'dettagli': _err}
            session.query(PreventivoPiastra).filter(
                PreventivoPiastra.preventivo_id == preventivo_id
            ).delete(synchronize_session=False)
            for pi in piastre or []:
                session.add(PreventivoPiastra(
                    id=str(uuid.uuid4()),
                    preventivo_id=preventivo_id,
                    codice_assieme=pi.get('codice_assieme'),
                    spessore_mm=float(pi.get('spessore_mm') or 0),
                    area_dm2=float(pi.get('area_dm2') or 0),
                    peso_kg=float(pi.get('peso_kg') or 0),
                    costo=float(pi.get('costo') or 0),
                    materiale=pi.get('materiale') or 'acciaio',
                ))
            session.commit()
            return {'success': True, 'count': len(piastre or [])}
        except Exception as e:
            session.rollback()
            return {'error': str(e)}
        finally:
            session.close()

    @staticmethod
    def accetta_e_crea_ordine(preventivo_id, user_id, *,
                              articoli=None, tubolari=None, piastre=None, assiemi=None,
                              totali=None,
                              note_aggiuntive=None, data_consegna_override=None):
        """INVIATO -> ACCETTATO + creazione Order FerroTrack.

        Ordine delle operazioni scelto per non lasciare mai uno stato parziale:

          1. se un ordine per questo preventivo esiste gia', lo si restituisce
             (doppio clic o retry: nessun ordine doppio)
          2. persist di articoli/assiemi/tubolari/piastre, con lo status
             SEMPRE ripristinato anche se qualcosa esplode a meta'
          3. si crea PRIMA l'ordine, col preventivo ancora INVIATO
          4. solo a ordine creato si passa ad ACCETTATO; se questo fallisce
             l'ordine appena creato viene annullato e il preventivo resta
             INVIATO, quindi riprovabile

        Cosi' non puo' esistere un preventivo ACCETTATO senza ordine, che era
        il caso peggiore: la commessa risultava presa e in officina non
        arrivava niente.
        """
        session = get_session()
        try:
            p = session.query(Preventivo).filter(
                Preventivo.id == preventivo_id,
                Preventivo.is_deleted == False,  # noqa: E712
            ).first()
            if not p:
                return {'error': 'Preventivo non trovato'}

            # --- 1. Ordine gia' esistente per questo preventivo -------------
            gia = session.query(Order).filter(
                Order.preventivo_id_origine == preventivo_id,
                Order.is_deleted == False,  # noqa: E712
            ).first()
            if gia:
                if p.status != 'ACCETTATO':
                    # Stato incoerente da un tentativo precedente interrotto:
                    # l'ordine c'e', il preventivo no. Si allinea.
                    p.status = 'ACCETTATO'
                    session.commit()
                return {'success': True, 'gia_creato': True,
                        'order_id': gia.id, 'numero_ordine': gia.numero_ordine,
                        'cartellino_url': '/api/orders/' + gia.id + '/cartellino',
                        'preventivo': PreventivoManager._serialize(p)}

            # ACCETTATO senza ordine: e' lo stato rotto lasciato dalla vecchia
            # implementazione. Si consente il recupero creando l'ordine mancante.
            if p.status not in ('INVIATO', 'ACCETTATO'):
                return {'error': 'Preventivo deve essere INVIATO (attuale: ' + p.status + ')'}
            recupero = (p.status == 'ACCETTATO')
            status_iniziale = p.status

            # --- 2. Persist, con ripristino garantito dello status ----------
            if any(x is not None for x in (articoli, assiemi, tubolari, piastre)):
                p.status = 'BOZZA'   # i replace_* rifiutano di toccare un preventivo non in bozza
                session.commit()
                errors = []
                try:
                    if articoli is not None:
                        r = PreventivoManager.replace_articoli(preventivo_id, articoli)
                        if isinstance(r, dict) and r.get('error'):
                            errors.append('articoli: ' + r['error'])
                    if assiemi is not None:
                        r = PreventivoManager.replace_assiemi(preventivo_id, assiemi)
                        if isinstance(r, dict) and r.get('error'):
                            errors.append('assiemi: ' + r['error'])
                    if tubolari is not None:
                        r = PreventivoManager.replace_tubolari(preventivo_id, tubolari)
                        if isinstance(r, dict) and r.get('error'):
                            errors.append('tubolari: ' + r['error'])
                    if piastre is not None:
                        r = PreventivoManager.replace_piastre(preventivo_id, piastre)
                        if isinstance(r, dict) and r.get('error'):
                            errors.append('piastre: ' + r['error'])
                except Exception as exc:
                    logger.exception('persist in accettazione fallito: %s', exc)
                    errors.append('errore interno nel salvataggio degli articoli')
                finally:
                    # Qualunque cosa sia successa, il preventivo non resta in BOZZA.
                    p = session.query(Preventivo).filter(
                        Preventivo.id == preventivo_id).first()
                    if p is not None:
                        p.status = status_iniziale
                        session.commit()
                if errors:
                    return {'error': 'persist failed: ' + ' | '.join(errors)}

            # --- 3. Totali: decide il SERVER, non il browser ----------------
            # Le due formule nel JavaScript non coincidevano: l'accettazione
            # ignorava assiemi, tubolari, piastre e costi generali, e contava
            # due volte gli articoli gia' dentro un assieme. Il prezzo che
            # finiva sull'ordine era piu' basso di quello mostrato al cliente.
            preventivo_dict = PreventivoManager._serialize(p)
            try:
                from .preventivi.calcolo import verifica
                cfg = (BarcodeManager.load_config() or {}).get('preventivi_config') or {}
                # Serve il preventivo COMPLETO: _serialize non porta articoli,
                # assiemi, tubolari e piastre, e senza quelli il calcolo darebbe
                # zero e ci si accorgerebbe del guaio solo in fattura.
                completo = PreventivoManager.get(preventivo_id) or preventivo_dict
                # Il prezzo accettato deve essere quello COMUNICATO al cliente:
                # se c'e' la fotografia scattata all'invio, comandano le sue
                # percentuali, non quelle attuali della configurazione.
                snap = completo.get('snapshot_economico') or None
                if snap:
                    cfg = {'costo_generali_pct': snap.get('costo_generali_pct') or 0}
                    completo = {**completo,
                                'margine_pct': snap.get('ricarico_pct',
                                                        completo.get('margine_pct')),
                                'sconto_pct': snap.get('sconto_pct',
                                                       completo.get('sconto_pct'))}
                esito = verifica(completo, totali or {}, cfg)
                calcolati = esito['totali']
                if not esito['coerente']:
                    logger.warning(
                        'Accettazione %s: totali del browser diversi dal calcolo '
                        'del server, si usano quelli del server. Scostamenti: %s',
                        preventivo_id, esito['scostamenti'])
                if calcolati['totale_lotto'] <= 0 and float(p.totale_lotto or 0) > 0:
                    # Nessuna riga da cui calcolare, ma un prezzo salvato c'e':
                    # non lo si azzera. Uno zero calcolato non e' un prezzo
                    # confermato, e cancellarlo farebbe partire una commessa
                    # a valore nullo.
                    logger.warning(
                        'Accettazione %s: il calcolo da 0 ma il preventivo ha '
                        'un totale salvato di %.2f. Si tiene quello salvato.',
                        preventivo_id, float(p.totale_lotto or 0))
                else:
                    p.totale_pezzo = calcolati['totale_pezzo']
                    p.totale_pezzo_con_margine = calcolati['totale_pezzo_con_margine']
                    p.totale_lotto = calcolati['totale_lotto']
                session.commit()
                preventivo_dict = PreventivoManager._serialize(p)
            except Exception as exc:
                # Se il calcolo autorevole non e' disponibile non si inventa un
                # prezzo: si tiene quello gia' salvato sul preventivo.
                logger.exception('calcolo totali in accettazione fallito: %s', exc)
        finally:
            session.close()

        # --- 4. Ordine PRIMA del cambio di stato ---------------------------
        if data_consegna_override:
            data_cons_str = data_consegna_override[:10]
        elif preventivo_dict.get('data_consegna_proposta'):
            data_cons_str = preventivo_dict['data_consegna_proposta'][:10]
        else:
            data_cons_str = (datetime.utcnow() + timedelta(days=30)).strftime('%Y-%m-%d')

        numero = OrderManager.next_numero_preventivo()
        try:
            order = OrderManager.create_order_from_preventivo(
                preventivo=preventivo_dict,
                data_consegna=data_cons_str,
                numero_ordine=numero,
                note_aggiuntive=note_aggiuntive or '',
            )
        except Exception as e:
            logger.exception('create_order_from_preventivo failed: %s', e)
            # Il preventivo e' ancora INVIATO: si puo' riprovare senza rimediare a nulla.
            return {'error': 'Creazione ordine fallita: ' + str(e)}

        # --- 5. Ora, e solo ora, il preventivo e' ACCETTATO ----------------
        if not recupero:
            session = get_session()
            try:
                p = session.query(Preventivo).filter(
                    Preventivo.id == preventivo_id).first()
                p.status = 'ACCETTATO'
                session.commit()
                preventivo_dict = PreventivoManager._serialize(p)
            except Exception as e:
                session.rollback()
                logger.exception('transizione ad ACCETTATO fallita: %s', e)
                # Si annulla l'ordine appena creato: meglio nessun ordine che un
                # ordine orfano di un preventivo che risulta ancora da accettare.
                try:
                    OrderManager.soft_delete_order(order.id)
                except Exception:
                    logger.error('ordine %s creato ma non annullabile dopo errore', order.id)
                return {'error': 'Accettazione non riuscita, nessuna modifica applicata. Riprova.'}
            finally:
                session.close()

        # --- 6. Notifiche, eventi, audit (non bloccanti) -------------------
        try:
            for u in UserManager.get_all_users() or []:
                if not u.get('is_active', True):
                    continue
                if u.get('is_capo'):
                    NotificationManager.create_notification(
                        user_id=u['id'], order_id=order.id,
                        title='Nuovo ordine da preventivo',
                        message='Ordine #' + numero + ' (' + order.cliente + ') accettato dal commerciale',
                        notification_type='order', notification_category='informativa')
                elif u.get('role') == 'Impiegata':
                    NotificationManager.create_notification(
                        user_id=u['id'], order_id=order.id,
                        title='Nuovo ordine da protocollare',
                        message='Ordine #' + numero + ' (' + order.cliente + ') accettato dal commerciale \u2014 da registrare (DDT/fattura)',
                        notification_type='order', notification_category='attiva')
        except Exception as exc:
            logger.warning('notifica nuovo ordine da preventivo fallita: %s', exc)

        try:
            from .events import OrderEventBus
            OrderEventBus.publish('order.created', {
                'order_id': order.id, 'numero_ordine': numero,
                'cliente': order.cliente, 'origine': 'PREVENTIVO',
                'preventivo_id_origine': preventivo_id,
            })
            OrderEventBus.publish('preventivo.accepted', {
                'preventivo_id': preventivo_id, 'order_id': order.id,
            })
        except Exception:
            pass

        try:
            AuditManager.log(
                user_id=user_id, action='ACCEPT_PREVENTIVO_CREATE_ORDER',
                entity_type='preventivi', entity_id=preventivo_id,
                detail='Order ' + order.id + ' (' + numero + ') creato da preventivo'
                       + (' [recupero]' if recupero else ''),
            )
        except Exception:
            pass

        return {
            'success': True,
            'order_id': order.id,
            'numero_ordine': numero,
            'cartellino_url': '/api/orders/' + order.id + '/cartellino',
            'preventivo': preventivo_dict,
        }

    @staticmethod
    def transition_status(preventivo_id, new_status, user_id=None):
        """Cambia status del preventivo. Transizioni valide:
          BOZZA → INVIATO
          INVIATO → ACCETTATO (in Fase 4 crea anche l'Order FerroTrack)
          INVIATO → RIFIUTATO
        """
        if new_status not in PreventivoManager.VALID_STATUSES:
            return {'error': 'Status non valido: ' + str(new_status)}
        session = get_session()
        try:
            p = session.query(Preventivo).filter(
                Preventivo.id == preventivo_id,
                Preventivo.is_deleted == False,  # noqa: E712
            ).first()
            if not p:
                return None
            valid_transitions = {
                'BOZZA': {'INVIATO'},
                'INVIATO': {'ACCETTATO', 'RIFIUTATO'},
                'ACCETTATO': set(),
                'RIFIUTATO': set(),
            }
            if new_status not in valid_transitions.get(p.status, set()):
                return {'error': 'Transizione non valida: ' + p.status + ' -> ' + new_status}
            p.status = new_status
            # Handshake "da prezzare": quando il preventivo viene inviato (prezzato)
            # o rifiutato, il flag va spento — altrimenti resterebbe True per sempre
            # e la richiesta continuerebbe a risultare "da prezzare".
            if new_status in ('INVIATO', 'RIFIUTATO') and getattr(p, 'da_prezzare', False):
                p.da_prezzare = False

            # All'INVIO si fotografa il prezzo con le percentuali di OGGI: da
            # quel momento il PDF di quell'offerta non cambia piu' se qualcuno
            # ritocca i costi in configurazione. Il cliente ha in mano un numero.
            if new_status == 'INVIATO' and not getattr(p, 'snapshot_economico', None):
                try:
                    from .preventivi.calcolo import calcola
                    cfg = (BarcodeManager.load_config() or {}).get('preventivi_config') or {}
                    completo = PreventivoManager.get(preventivo_id) or {}
                    tot = calcola(completo, cfg)
                    p.snapshot_economico = json.dumps({
                        'congelato_il': datetime.utcnow().isoformat(),
                        'congelato_da': user_id or '',
                        'costo_generali_pct': tot['costi_generali_pct'],
                        'ricarico_pct': tot['ricarico_pct'],
                        'sconto_pct': tot['sconto_pct'],
                        'totale_pezzo': tot['totale_pezzo'],
                        'totale_pezzo_con_margine': tot['totale_pezzo_con_margine'],
                        'totale_lotto': tot['totale_lotto'],
                        'totale_lotto_lordo': tot['totale_lotto_lordo'],
                    }, ensure_ascii=False)
                    if tot['totale_lotto'] > 0:
                        p.totale_pezzo = tot['totale_pezzo']
                        p.totale_pezzo_con_margine = tot['totale_pezzo_con_margine']
                        p.totale_lotto = tot['totale_lotto']
                except Exception as exc:
                    logger.exception('congelamento prezzo all invio fallito: %s', exc)

            session.commit()
            return PreventivoManager._serialize(p)
        finally:
            session.close()

    # ---- Serializzatori ----------------------------------------------------

    @staticmethod
    def _serialize(p):
        return {
            'id': p.id,
            'cliente': p.cliente,
            'numero_ordine_cliente': p.numero_ordine_cliente,
            'quantita': p.quantita,
            'margine_pct': p.margine_pct,
            'sconto_pct': p.sconto_pct,
            'data_consegna_proposta': p.data_consegna_proposta.isoformat() if p.data_consegna_proposta else None,
            'status': p.status,
            'versione': p.versione,
            'parent_preventivo_id': p.parent_preventivo_id,
            'totale_pezzo': p.totale_pezzo,
            'totale_pezzo_con_margine': p.totale_pezzo_con_margine,
            'totale_pezzo_scontato': p.totale_pezzo_scontato,
            'totale_lotto': p.totale_lotto,
            'costi_montaggio_totale': p.costi_montaggio_totale,
            'costi_tubolari_totale': p.costi_tubolari_totale,
            'costi_piastre_totale': p.costi_piastre_totale,
            'snapshot_economico': (json.loads(p.snapshot_economico)
                                   if getattr(p, 'snapshot_economico', None) else None),
            'created_by': p.created_by,
            'data_creazione': p.data_creazione.isoformat() if p.data_creazione else None,
            'note': p.note,
            'da_prezzare': bool(getattr(p, 'da_prezzare', False)),
            'email_cliente': getattr(p, 'email_cliente', None),
            'email_inviata_il': (p.email_inviata_il.isoformat() if getattr(p, 'email_inviata_il', None) else None),
        }

    @staticmethod
    def _serialize_articolo(a):
        return {
            'id': a.id, 'codice': a.codice, 'quantita': a.quantita,
            'codice_assieme': a.codice_assieme,
            'area': a.area, 'area_dm2': a.area_dm2,
            'perimetro_taglio_m': a.perimetro_taglio_m, 'n_forature': a.n_forature,
            'spessore_mm': a.spessore_mm, 'materiale': a.materiale,
            'dxf_filename': a.dxf_filename,
            'cleaned_dxf_filename': a.cleaned_dxf_filename,
            'cleaned_status': a.cleaned_status,
            'costo_materiale': a.costo_materiale,
            'costo_base_stimato': a.costo_base_stimato,
            'costo_base_override': a.costo_base_override,
            'pieghe': a.pieghe, 'saldatura_ml': a.saldatura_ml,
            'saldatura_min': getattr(a, 'saldatura_min', 0.0) or 0.0,
            'filettatura_pz': a.filettatura_pz, 'svasatura_pz': a.svasatura_pz,
            'costo_piega': a.costo_piega, 'costo_saldatura': a.costo_saldatura,
            'costo_filettatura': a.costo_filettatura, 'costo_svasatura': a.costo_svasatura,
            'costo_apporto': a.costo_apporto, 'costo_pulizia': a.costo_pulizia,
            'geometria_manuale_confermata': bool(getattr(a, 'geometria_manuale_confermata', False)),
            'geometry_source': getattr(a, 'geometry_source', None),
            'area_stimata_piega': bool(getattr(a, 'area_stimata_piega', False)),
            'canonical_dxf_filename': getattr(a, 'canonical_dxf_filename', None),
            'canonical_dxf_sha256': getattr(a, 'canonical_dxf_sha256', None),
        }

    @staticmethod
    def _serialize_assieme(a):
        return {
            'id': a.id, 'codice_assieme': a.codice_assieme, 'qty': a.qty,
            'ore_montaggio': a.ore_montaggio, 'ore_puntatura': a.ore_puntatura,
            'costo': a.costo, 'costo_puntatura': a.costo_puntatura,
            'costo_saldatura_assieme': a.costo_saldatura_assieme,
            'saldatura_mt': a.saldatura_mt, 'peso_kg': a.peso_kg,
            'componenti_qty': a.componenti_qty,
        }

    @staticmethod
    def _serialize_tubolare(t):
        return {
            'id': t.id, 'codice_assieme': t.codice_assieme,
            'profilo': t.profilo, 'tipo': t.tipo, 'materiale': t.materiale,
            'lunghezza_m': t.lunghezza_m, 'peso_kg': t.peso_kg,
            'costo_materiale': t.costo_materiale,
            'costo_taglio_totale': t.costo_taglio_totale,
            'n_tagli_dritti': t.n_tagli_dritti, 'n_tagli_obliqui': t.n_tagli_obliqui,
        }

    @staticmethod
    def _serialize_piastra(p):
        return {
            'id': p.id, 'codice_assieme': p.codice_assieme,
            'spessore_mm': p.spessore_mm, 'area_dm2': p.area_dm2,
            'peso_kg': p.peso_kg, 'costo': p.costo, 'materiale': p.materiale,
        }
