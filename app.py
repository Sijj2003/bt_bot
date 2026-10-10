import os
import json
import time
import base64
import queue
import threading
import logging
from datetime import datetime, timezone
from flask import Flask
import firebase_admin
from firebase_admin import credentials, firestore
from dotenv import load_dotenv

# --- IMPORTACIÓN DE MÓDULOS ---
import pago_movil_bot
import binance_bot
import mrw_bot

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# =========================================================
# 1. SERVIDOR FLASK (Health Check para Render)
# =========================================================
app = Flask(__name__)

@app.route('/', methods=['GET'])
def health_check():
    return "Gymenez Bot Worker is Active and Listening!", 200

# =========================================================
# 2. INICIALIZACIÓN DE FIREBASE ADMIN
# =========================================================
firebase_credentials_raw = os.environ.get('FIREBASE_CREDENTIALS')

if firebase_credentials_raw:
    try:
        if not firebase_credentials_raw.strip().startswith('{'):
            decoded_bytes = base64.b64decode(firebase_credentials_raw)
            cred_dict = json.loads(decoded_bytes.decode('utf-8'))
        else:
            cred_dict = json.loads(firebase_credentials_raw)
            
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
        logger.info("[FIREBASE] Inicializado correctamente desde variable de entorno.")
    except Exception as e:
        logger.error(f"[FIREBASE ERROR] Error parseando FIREBASE_CREDENTIALS: {e}")
        raise e
else:
    if os.path.exists('serviceAccountKey.json'):
        cred = credentials.Certificate('serviceAccountKey.json')
        firebase_admin.initialize_app(cred)
        logger.info("[FIREBASE] Inicializado desde serviceAccountKey.json local.")
    else:
        raise ValueError("Falta la variable FIREBASE_CREDENTIALS o el archivo local serviceAccountKey.json.")

db = firestore.client()

# =========================================================
# 3. COLAS Y WORKERS
# =========================================================
pm_queue = queue.Queue()
binance_queue = queue.Queue()
mrw_queue = queue.Queue()
processed_in_flight = set()
lock_in_flight = threading.Lock()

def pm_worker_loop():
    """Hilo para Pago Móvil (Conserva tu lógica de cierre de sesión a los 120s)"""
    logger.info("👷 [WORKER BANCO] Hilo de procesamiento continuo iniciado.")
    while True:
        try:
            try:
                order_id, order_data = pm_queue.get(timeout=30)
            except queue.Empty:
                if pago_movil_bot.bank_manager.is_logged_in and (time.time() - pago_movil_bot.bank_manager.last_activity > 120):
                    pago_movil_bot.bank_manager.logout()
                continue

            logger.info(f"📥 [WORKER] Extrayendo orden de la cola (Pago Móvil): {order_id}")
            pago_movil_bot.procesar_validacion_en_banco(order_id, order_data, db)
            
            pm_queue.task_done()
            with lock_in_flight:
                processed_in_flight.discard(order_id)
        except Exception as e:
            logger.error(f"❌ [WORKER ERROR] Excepción no controlada en el worker loop (Banco): {e}")
            time.sleep(2)

def binance_worker_loop(worker_id):
    """Hilos paralelos para Binance"""
    logger.info(f"🟡 [WORKER BINANCE {worker_id}] Hilo paralelo iniciado.")
    while True:
        try:
            order_id, order_data = binance_queue.get()
            binance_bot.procesar_validacion_binance(order_id, order_data)
        except Exception as e:
            logger.error(f"❌ [WORKER BINANCE ERROR] Error crítico en {order_id}: {e}")
        finally:
            with lock_in_flight:
                processed_in_flight.discard(order_id)
            binance_queue.task_done()

def mrw_worker_loop():
    """Hilo logístico para rastreo de MRW usando Batched Writes y Validación de Identidad Anti-Fraude"""
    import re
    logger.info("📦 [WORKER MRW] Hilo logístico iniciado.")
    while True:
        try:
            # 1. Queda en pausa automáticamente hasta que entre un paquete a la cola
            order_id, order_data = mrw_queue.get()
            items = [(order_id, order_data)]
            
            # 2. Vaciamos el resto de la cola por si entraron varios de golpe
            try:
                while True:
                    items.append(mrw_queue.get_nowait())
            except queue.Empty:
                pass

            if items:
                batch = db.batch()
                requiere_escritura = False
                ahora_ts = datetime.now(timezone.utc).timestamp()

                for oid, odata in items:
                    nro_guia = odata.get('nro_guia')
                    if nro_guia:
                        resultado = mrw_bot.consultar_guia(nro_guia)
                        ref_doc = db.collection('store_orders').document(oid)

                        if resultado.get("valido"):
                            # ========================================================
                            # 🛡️ VALIDACIÓN DE IDENTIDAD ZERO-TRUST (Nombre y Cédula)
                            # ========================================================
                            mrw_destinatario = resultado.get("destinatario", "").upper()
                            buyer_name = str(odata.get("buyer_name", "")).upper()
                            buyer_doc = str(odata.get("buyer_doc", ""))
                            
                            es_autentica = True
                            
                            # Si MRW devolvió un nombre real (no está vacío) procedemos a auditar
                            if mrw_destinatario and len(mrw_destinatario) > 3:
                                # 1. Auditar Cédula: Extraemos solo los números de la CI del cliente
                                doc_digits = re.sub(r'\D', '', buyer_doc)
                                coincide_cedula = bool(doc_digits) and (doc_digits in mrw_destinatario)
                                
                                # 2. Auditar Nombre: Buscamos si alguna palabra del nombre (>3 letras) está en MRW
                                palabras_nombre = [p for p in buyer_name.split() if len(p) > 3]
                                coincide_nombre = any(p in mrw_destinatario for p in palabras_nombre)
                                
                                # Si ni la cédula ni el nombre cruzaron información, es una guía de otro cliente
                                if not (coincide_cedula or coincide_nombre):
                                    es_autentica = False
                            
                            if es_autentica:
                                # ✅ GUÍA VERÍDICA Y CONFIRMADA: Actualizamos historial
                                nuevo_estatus = "completado" if resultado["estatus_actual"] == "Entregado" else "enviado"
                                update_data = {
                                    "status": nuevo_estatus,
                                    "historial_envio": resultado["historial"],
                                    "ubicacion_paquete": resultado["ubicacion_actual"],
                                    "ultima_revision_mrw": ahora_ts
                                }
                                if not odata.get('fecha_inicio_tracking'):
                                    update_data['fecha_inicio_tracking'] = ahora_ts
                                    
                                batch.update(ref_doc, update_data)
                                requiere_escritura = True
                            else:
                                # 🚨 FRAUDE DETECTADO: Revertimos la acción y borramos la guía
                                logger.warning(f"🚨 [FRAUDE DETECTADO] Guía {nro_guia} rechazada. Le pertenece a {mrw_destinatario}, no a {buyer_name}.")
                                bad_store = None
                                guide_obj_to_remove = None
                                # Buscamos qué partner inyectó esta guía falsa
                                for s_name, s_data in odata.get('store_splits', {}).items():
                                    for g in s_data.get('tracking_guides', []):
                                        if g.get('guide_number') == str(nro_guia):
                                            bad_store = s_name
                                            guide_obj_to_remove = g
                                            break
                                
                                if bad_store:
                                    batch.update(ref_doc, {
                                        "status": "processing",  # ⬅️ Devuelve la orden a la pantalla del Partner
                                        "nro_guia": firestore.DELETE_FIELD,
                                        f"store_splits.{bad_store}.shipping_status": "pending",
                                        f"store_splits.{bad_store}.tracking_guides": firestore.ArrayRemove([guide_obj_to_remove]),
                                        f"store_splits.{bad_store}.fraud_alert": f"Rechazada por Bot: La guía {nro_guia} le pertenece a {mrw_destinatario}."
                                    })
                                    requiere_escritura = True

                        else:
                            # 🚨 LA GUÍA NO EXISTE O ES FALSA
                            logger.warning(f"🚨 [GUÍA FALSA] {nro_guia} no está registrada en MRW.")
                            bad_store = None
                            guide_obj_to_remove = None
                            for s_name, s_data in odata.get('store_splits', {}).items():
                                for g in s_data.get('tracking_guides', []):
                                    if g.get('guide_number') == str(nro_guia):
                                        bad_store = s_name
                                        guide_obj_to_remove = g
                                        break
                            
                            if bad_store:
                                batch.update(ref_doc, {
                                    "status": "processing",
                                    "nro_guia": firestore.DELETE_FIELD,
                                    f"store_splits.{bad_store}.shipping_status": "pending",
                                    f"store_splits.{bad_store}.tracking_guides": firestore.ArrayRemove([guide_obj_to_remove]),
                                    f"store_splits.{bad_store}.fraud_alert": "Rechazada por Bot: La guía ingresada no existe en el sistema de MRW."
                                })
                                requiere_escritura = True

                    mrw_queue.task_done()
                    with lock_in_flight:
                        processed_in_flight.discard(oid)

                if requiere_escritura:
                    batch.commit()
                    logger.info(f"✅ [BATCH WRITE] Operaciones logísticas procesadas en Firebase.")

        except Exception as e:
            logger.error(f"❌ [WORKER MRW ERROR] Excepción crítica: {e}")
            time.sleep(5)
            
# =========================================================
# 🧹 BARREDOR ANTI-LIMBO Y CRONJOB LOGÍSTICO
# =========================================================
def recuperador_ordenes_pendientes():
    """Barredor Anti-Limbo y CronJob Logístico"""
    logger.info("🧹 [SWEEPER] Hilo barredor iniciado (Pagos y Logística).")
    while True:
        try:
            time.sleep(60) 
            ahora_ts = datetime.now(timezone.utc).timestamp()
            
            # 1. 🧹 BARREDOR FINANCIERO (Anti-Limbo)
            ordenes_pago = db.collection('store_orders').where('status', '==', 'pending_retry').get()
            for doc in ordenes_pago:
                order_id = doc.id
                datos_orden = doc.to_dict()
                proximo = datos_orden.get('proximo_reintento')
                if proximo:
                    proximo_ts = proximo.timestamp()
                    if proximo_ts <= ahora_ts:
                        payment_method = datos_orden.get('paymentMethod') or datos_orden.get('payment_method') or datos_orden.get('payment_details', {}).get('payment_method', '')
                        with lock_in_flight:
                            if order_id not in processed_in_flight:
                                processed_in_flight.add(order_id)
                                logger.info(f"🔄 [SWEEPER] Rescatando orden de pago: {order_id}")
                                if payment_method == 'pago_movil':
                                    pm_queue.put((order_id, datos_orden))
                                elif payment_method == 'binance':
                                    binance_queue.put((order_id, datos_orden))

            # 2. 🚚 CRONJOB LOGÍSTICO (14 Días Máximo / Chequeo 8 Horas)
            ordenes_logistica = db.collection('store_orders').where('status', '==', 'enviado').get()
            for doc in ordenes_logistica:
                order_id = doc.id
                datos_orden = doc.to_dict()
                
                ultima_rev = datos_orden.get('ultima_revision_mrw', 0)
                # Si no tiene fecha de inicio, asumimos el momento actual para no cerrarla por error
                fecha_inicio = datos_orden.get('fecha_inicio_tracking', ahora_ts) 
                
                # Convertimos la diferencia de segundos a días (86400 segundos = 1 día)
                dias_transcurridos = (ahora_ts - fecha_inicio) / 86400 
                
                # 🛑 REGLA DE PROTECCIÓN AL VENDEDOR: Cierre automático a los 14 días
                if dias_transcurridos >= 14:
                    logger.info(f"⏳ [AUTO-CIERRE] Orden {order_id} superó 14 días en tránsito. Marcando como completada.")
                    db.collection('store_orders').document(order_id).update({
                        "status": "completado",
                        "ubicacion_paquete": "Entregado (Cierre automático por tiempo máximo)"
                    })
                    continue # Terminamos aquí, ya no la encolamos para buscar en MRW
                
                # ⏱️ REGLA DE CONSULTA: Si no han pasado 14 días, revisamos cada 8 horas (28800 segundos)
                if (ahora_ts - ultima_rev) > 28800:
                    with lock_in_flight:
                        if order_id not in processed_in_flight:
                            processed_in_flight.add(order_id)
                            logger.info(f"🚚 [CRONJOB] Chequeo logístico de 8H para: {order_id}")
                            mrw_queue.put((order_id, datos_orden))

        except Exception as e:
            logger.error(f"❌ [SWEEPER ERROR] Fallo: {e}")

# =========================================================
# 4. LISTENER EN TIEMPO REAL (FIRESTORE)
# =========================================================
def on_snapshot(col_snapshot, changes, read_time):
    for change in changes:
        if change.type.name in ['ADDED', 'MODIFIED']:
            order_id = change.document.id
            order_data = change.document.to_dict()
            status = order_data.get('status')
            
            with lock_in_flight:
                if order_id in processed_in_flight:
                    continue
                processed_in_flight.add(order_id)

            # --- RUTA LOGÍSTICA (MRW) ---
            if status == 'enviado' and order_data.get('nro_guia'):
                # 🚀 Si no tiene sello de tiempo, es nueva. Se procesa instantáneamente.
                if not order_data.get('ultima_revision_mrw'):
                    logger.info(f"🚚 [FIRESTORE EVENT] Nueva guía detectada, procesando en vivo: {order_id}")
                    mrw_queue.put((order_id, order_data))
                else:
                    # Ya se revisó antes. Dejamos que el Barredor lo haga periódicamente (8 horas).
                    with lock_in_flight:
                        processed_in_flight.discard(order_id)
                continue

            # --- RUTA FINANCIERA (Pagos) ---
            if status == 'pending_verification':
                payment_method = order_data.get('paymentMethod') or order_data.get('payment_method') or order_data.get('payment_details', {}).get('payment_method', '')
                
                if payment_method == 'pago_movil':
                    logger.info(f"⚡ [FIRESTORE EVENT] Pago Móvil encolado: {order_id}")
                    pm_queue.put((order_id, order_data))
                elif payment_method == 'binance':
                    logger.info(f"⚡ [FIRESTORE EVENT] Binance encolado: {order_id}")
                    binance_queue.put((order_id, order_data))
                else:
                    with lock_in_flight:
                        processed_in_flight.discard(order_id)
            else:
                with lock_in_flight:
                    processed_in_flight.discard(order_id)

def start_bot_services():
    # Hilo exclusivo para Pago Móvil
    threading.Thread(target=pm_worker_loop, daemon=True).start()
    
    # 3 Hilos paralelos para Binance
    for i in range(3):
        threading.Thread(target=binance_worker_loop, args=(i+1,), daemon=True).start()

    # Hilo logístico por Lotes para MRW
    threading.Thread(target=mrw_worker_loop, daemon=True).start()

    # 🧹 EL NUEVO BARREDOR PARA EVITAR EL LIMBO (Universal: Pago Móvil, Binance y MRW)
    threading.Thread(target=recuperador_ordenes_pendientes, daemon=True).start()

    try:
        logger.info("🚀 [BOT] Iniciando Listener de Firestore para 'store_orders'...")
        # Modificado para escuchar ambos estados ('pending_verification' y 'enviado')
        orders_ref = db.collection('store_orders').where('status', 'in', ['pending_verification', 'enviado'])
        orders_ref.on_snapshot(on_snapshot)
        logger.info("✅ [BOT] Listener activo y escuchando compras pendientes y envíos logísticos.")
    except Exception as e:
        logger.error(f"❌ [BOT ERROR] Falló al iniciar el Listener de Firestore: {e}")

# Iniciar servicios al cargar el script
start_bot_services()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
