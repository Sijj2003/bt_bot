import os
import json
import time
import base64
import queue
import threading
import logging
from flask import Flask
import firebase_admin
from firebase_admin import credentials, firestore
from dotenv import load_dotenv

# --- IMPORTACIÓN DE MÓDULOS ---
import pago_movil_bot
import binance_bot

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
# 2. INICIALIZACIÓN DE FIREBASE ADMIN (Tu lógica base64)
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

# =========================================================
# 🧹 BARREDOR ANTI-LIMBO (PÉGALO EXACTAMENTE AQUÍ)
# =========================================================
from datetime import datetime, timezone

def recuperador_ordenes_pendientes():
    """Barredor Anti-Limbo que NO requiere índice compuesto de Firebase"""
    logger.info("🧹 [SWEEPER] Hilo recuperador iniciado (Anti-Limbo).")
    while True:
        try:
            time.sleep(60) 
            ahora_ts = datetime.now(timezone.utc).timestamp()
            
            # Buscamos solo por estado para evitar errores de índices de Firebase
            ordenes = db.collection('store_orders').where('status', '==', 'pending_retry').get()

            for doc in ordenes:
                order_id = doc.id
                datos_orden = doc.to_dict()
                
                proximo = datos_orden.get('proximo_reintento')
                if proximo:
                    # Comparamos las fechas internamente en Python
                    proximo_ts = proximo.timestamp()
                    
                    if proximo_ts <= ahora_ts:
                        payment_method = datos_orden.get('paymentMethod') or datos_orden.get('payment_method') or datos_orden.get('payment_details', {}).get('payment_method', '')
                        
                        with lock_in_flight:
                            if order_id not in processed_in_flight:
                                processed_in_flight.add(order_id)
                                logger.info(f"🔄 [SWEEPER] Rescatando orden del limbo: {order_id}")
                                
                                if payment_method == 'pago_movil':
                                    pm_queue.put((order_id, datos_orden))
                                elif payment_method == 'binance':
                                    binance_queue.put((order_id, datos_orden))
        except Exception as e:
            logger.error(f"❌ [SWEEPER ERROR] Fallo: {e}")

# =========================================================
# 4. LISTENER EN TIEMPO REAL (FIRESTORE)
# =========================================================
def on_snapshot(col_snapshot, changes, read_time):
    for change in changes:
        if change.type.name == 'ADDED':
            order_id = change.document.id
            order_data = change.document.to_dict()
            
            # Búsqueda robusta de método de pago
            payment_method = order_data.get('paymentMethod') or order_data.get('payment_method') or order_data.get('payment_details', {}).get('payment_method', '')
            
            if payment_method not in ['pago_movil', 'binance']:
                logger.info(f"⏭️ [LISTENER] Orden {order_id} ignorada. Método: '{payment_method}'")
                continue 

            with lock_in_flight:
                if order_id in processed_in_flight:
                    continue
                processed_in_flight.add(order_id)

            logger.info(f"⚡ [FIRESTORE EVENT] Nueva orden encolada ({payment_method}): {order_id}")
            
            if payment_method == 'pago_movil':
                pm_queue.put((order_id, order_data))
            elif payment_method == 'binance':
                binance_queue.put((order_id, order_data))

def start_bot_services():
    # Hilo exclusivo para Pago Móvil
    threading.Thread(target=pm_worker_loop, daemon=True).start()
    
    # 3 Hilos paralelos para Binance
    for i in range(3):
        threading.Thread(target=binance_worker_loop, args=(i+1,), daemon=True).start()

    # 🧹 EL NUEVO BARREDOR PARA EVITAR EL LIMBO (Universal: Pago Móvil y Binance)
    threading.Thread(target=recuperador_ordenes_pendientes, daemon=True).start()

    try:
        logger.info("🚀 [BOT] Iniciando Listener de Firestore para 'store_orders'...")
        orders_ref = db.collection('store_orders').where('status', '==', 'pending_verification')
        orders_ref.on_snapshot(on_snapshot)
        logger.info("✅ [BOT] Listener activo y escuchando compras pendientes.")
    except Exception as e:
        logger.error(f"❌ [BOT ERROR] Falló al iniciar el Listener de Firestore: {e}")

# Iniciar servicios al cargar el script
start_bot_services()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
