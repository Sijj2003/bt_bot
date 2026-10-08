import os
import time
import threading
import queue
import logging
import json
from datetime import datetime, timezone
import firebase_admin
from firebase_admin import credentials, firestore
from flask import Flask

# --- IMPORTAMOS LOS MÓDULOS AISLADOS ---
import pago_movil_bot
import binance_bot 

# --- APLICACIÓN WEB PARA RENDER ---
app = Flask(__name__)

@app.route('/')
def health_check():
    return "✅ Bot Orquestador operativo y escuchando a Firestore.", 200

# --- CONFIGURACIÓN Y LOGS ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- INICIALIZACIÓN DE FIREBASE ---
try:
    firebase_creds_str = os.environ.get("FIREBASE_CREDENTIALS")
    if not firebase_creds_str:
        raise ValueError("La variable de entorno FIREBASE_CREDENTIALS está vacía.")
        
    cred_dict = json.loads(firebase_creds_str)
    cred = credentials.Certificate(cred_dict) 
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    logger.info("✅ Firebase inicializado correctamente.")
except Exception as e:
    logger.error(f"❌ Error al inicializar Firebase: {e}")

# --- VARIABLES GLOBALES Y CONTROL DE CONCURRENCIA ---
pm_queue = queue.Queue()       
binance_queue = queue.Queue()  

lock_in_flight = threading.Lock()
processed_in_flight = set()

# --- HILOS TRABAJADORES (WORKERS) ---
def pm_worker_loop():
    logger.info("🏦 [WORKER BANCO] Hilo exclusivo de Pago Móvil iniciado.")
    while True:
        try:
            # Si pasan 2 minutos sin pagos, se libera la cola y se cierra la sesión
            order_id, datos_orden = pm_queue.get(timeout=120)
            try:
                # LLAMADA AL MÓDULO AISLADO
                pago_movil_bot.procesar_validacion_en_banco(order_id, datos_orden, db)
            except Exception as e:
                logger.error(f"❌ [WORKER BANCO ERROR] Error crítico en {order_id}: {e}")
            finally:
                with lock_in_flight:
                    if order_id in processed_in_flight:
                        processed_in_flight.remove(order_id)
                pm_queue.task_done()
        except queue.Empty:
            pago_movil_bot.bank_manager.logout()

def binance_worker_loop(worker_id):
    logger.info(f"🟡 [WORKER BINANCE {worker_id}] Hilo paralelo iniciado.")
    while True:
        order_id, datos_orden = binance_queue.get()
        try:
            # LLAMADA AL MÓDULO AISLADO
            binance_bot.procesar_validacion_binance(order_id, datos_orden)
        except Exception as e:
            logger.error(f"❌ [WORKER BINANCE ERROR] Error crítico en {order_id}: {e}")
        finally:
            with lock_in_flight:
                if order_id in processed_in_flight:
                    processed_in_flight.remove(order_id)
            binance_queue.task_done()

# --- EL BARREDOR (SWEEPER) ---
def recuperador_ordenes_pendientes():
    logger.info("🧹 [SWEEPER] Hilo recuperador iniciado.")
    while True:
        try:
            time.sleep(60) 
            ahora = datetime.now(timezone.utc)
            
            ordenes = db.collection('store_orders') \
                .where(filter=firestore.FieldFilter('status', '==', 'pending_retry')) \
                .where(filter=firestore.FieldFilter('proximo_reintento', '<=', ahora)) \
                .get()

            for doc in ordenes:
                order_id = doc.id
                datos_orden = doc.to_dict()
                
                # --- CORRECCIÓN AQUÍ ---
                payment_method = datos_orden.get('payment_method') or datos_orden.get('payment_details', {}).get('payment_method', '')
                
                if payment_method not in ['pago_movil', 'binance']:
                    continue
                
                with lock_in_flight:
                    if order_id not in processed_in_flight:
                        processed_in_flight.add(order_id)
                        
                        if payment_method == 'pago_movil':
                            pm_queue.put((order_id, datos_orden))
                        elif payment_method == 'binance':
                            binance_queue.put((order_id, datos_orden))
        except Exception as e:
            logger.error(f"❌ [SWEEPER ERROR] Fallo: {e}")

# --- EL ESCUCHADOR (LISTENER) ---
def on_snapshot(col_snapshot, changes, read_time):
    for change in changes:
        if change.type.name == 'ADDED':
            doc = change.document
            order_id = doc.id
            datos_orden = doc.to_dict()
            
            # --- CORRECCIÓN AQUÍ ---
            payment_method = datos_orden.get('payment_method') or datos_orden.get('payment_details', {}).get('payment_method', '')
            
            # REGLA ESTRICTA: Si no es un método conocido, se ignora y se deja en el limbo.
            if payment_method not in ['pago_movil', 'binance']:
                logger.info(f"⏭️ [LISTENER] Orden {order_id} ignorada. Método desconocido o vacío: '{payment_method}'")
                continue 
            
            with lock_in_flight:
                if order_id not in processed_in_flight:
                    processed_in_flight.add(order_id)
                    
                    if payment_method == 'pago_movil':
                        logger.info(f"📥 [LISTENER] Orden a fila de Banco: {order_id}")
                        pm_queue.put((order_id, datos_orden))
                    elif payment_method == 'binance':
                        logger.info(f"📥 [LISTENER] Orden a fila de Binance: {order_id}")
                        binance_queue.put((order_id, datos_orden))

# --- INICIO DEL SISTEMA ---
def start_bot_services():
    # 1 hilo para el banco, 3 hilos paralelos para Binance
    threading.Thread(target=pm_worker_loop, daemon=True, name="Worker-Banco").start()
    for i in range(3):
        threading.Thread(target=binance_worker_loop, args=(i+1,), daemon=True, name=f"Worker-Binance-{i+1}").start()

    threading.Thread(target=recuperador_ordenes_pendientes, daemon=True, name="Sweeper").start()

    try:
        logger.info("🚀 [BOT] Conectando a Firestore...")
        db.collection('store_orders').where(filter=firestore.FieldFilter('status', '==', 'pending_verification')).on_snapshot(on_snapshot)
        logger.info("✅ [BOT] Sistema operativo. Escuchando compras...")
    except Exception as e:
        logger.error(f"❌ [BOT ERROR] Listener falló: {e}")

start_bot_services()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
