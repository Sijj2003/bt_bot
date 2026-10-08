import os
import time
import threading
import queue
import requests
import logging
from datetime import datetime, timedelta, timezone
import firebase_admin
from firebase_admin import credentials, firestore

# --- IMPORTAMOS EL NUEVO MÓDULO DE BINANCE ---
import binance_bot 

# --- 1. CONFIGURACIÓN Y LOGS ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- 2. INICIALIZACIÓN DE FIREBASE ---
try:
    # <- AJUSTA ESTO: Pon el nombre real de tu archivo JSON de credenciales de Firebase
    cred = credentials.Certificate("credenciales_firebase.json") 
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    logger.info("✅ Firebase inicializado correctamente.")
except Exception as e:
    logger.error(f"❌ Error al inicializar Firebase: {e}")

# --- 3. VARIABLES GLOBALES Y CONTROL DE CONCURRENCIA (DOBLE COLA) ---
pm_queue = queue.Queue()       # Fila exclusiva para Pago Móvil (1 hilo)
binance_queue = queue.Queue()  # Fila paralela para Binance (3 hilos)

lock_in_flight = threading.Lock()
processed_in_flight = set()

# --- 4. EXCEPCIONES PERSONALIZADAS ---
class BancoCajaBloqueadaException(Exception):
    pass

class BancoMantenimientoException(Exception):
    pass

# --- 5. GESTOR DE SESIÓN DEL BANCO ---
class BankSessionManager:
    def __init__(self):
        self.session = requests.Session()
        self.is_logged_in = False
        self.base_url = "https://URL_DE_TU_BANCO.com" # <- AJUSTA ESTO

    def login(self):
        try:
            # <- AJUSTA ESTO: Aquí va tu lógica real de inicio de sesión
            res_login = self.session.post(
                f"{self.base_url}/login", 
                data={"usuario": "tu_usuario", "clave": "tu_clave"}, 
                allow_redirects=False
            )
            location = res_login.headers.get('Location', '')
            
            if res_login.status_code != 302 or 'login' in location:
                self.is_logged_in = False
                raise BancoCajaBloqueadaException("Credenciales rechazadas o la caja del banco está ocupada.")
            
            self.is_logged_in = True
            logger.info("✅ Login exitoso en el banco.")
        except requests.exceptions.RequestException as e:
            raise BancoMantenimientoException(f"Error de red al intentar login: {e}")

    def consultar(self, payload):
        if not self.is_logged_in:
            self.login()
        res = self.session.post(f"{self.base_url}/consultar", data=payload)
        return res

    def logout(self):
        """Cierra la sesión de forma limpia para liberar la caja del banco"""
        if self.is_logged_in:
            logger.info("🔒 Cerrando sesión del banco por inactividad (2 min sin pagos móviles).")
            # self.session.post(f"{self.base_url}/logout") # Descomenta y ajusta si tu banco exige un endpoint de logout
            self.session.close()
            self.session = requests.Session() # Reinicia la sesión limpia
            self.is_logged_in = False

bank_manager = BankSessionManager()

# --- 6. FUNCIONES DE EVALUACIÓN ---
def evaluar_respuesta_banco(res_val):
    # Lógica simulada por ahora:
    return "NO_ENCONTRADO", "Aún no se ha implementado el scraping real."

def resolver_falso_positivo_ya_utilizado(order_id, ref_6_digitos, datos_orden):
    return False, "Referencia ya fue usada por otra orden."

# --- 7. NÚCLEO DE VALIDACIÓN Y REINTENTOS (BANCARIO) ---
def procesar_validacion_en_banco(order_id, datos_orden):
    logger.info(f"⚙️ [PROCESANDO] Iniciando validación para orden {order_id}...")
    order_ref = db.collection('store_orders').document(order_id)
    
    payment_details = datos_orden.get('payment_details', {})
    
    payload_val = {
        'monto': payment_details.get('monto_bot'),
        'banco': payment_details.get('banco'),
        'telefono': payment_details.get('telefono'),
        'referencia': payment_details.get('referencia')
    }

    max_reintentos = 3
    intentos_actuales = datos_orden.get("reintentos", 0)

    try:
        res_val = bank_manager.consultar(payload_val)
        dictamen, mensaje_banco = evaluar_respuesta_banco(res_val)
        
        if dictamen == "BANCO_DOWN":
            raise BancoMantenimientoException(mensaje_banco)

        if dictamen == "APROBADO":
            order_ref.update({
                'status': 'approved',
                'bot_verification_msg': mensaje_banco,
                'verified_at': firestore.SERVER_TIMESTAMP
            })
            logger.info(f"🎉 [FIRESTORE] Orden {order_id} APROBADA.")

        elif dictamen == "YA_UTILIZADO":
            es_aprobado, msg_res = resolver_falso_positivo_ya_utilizado(order_id, payload_val['referencia'], datos_orden)
            if es_aprobado:
                order_ref.update({'status': 'approved', 'bot_verification_msg': msg_res, 'verified_at': firestore.SERVER_TIMESTAMP})
                logger.info(f"🎉 [FIRESTORE] Orden {order_id} APROBADA (Resolución de conflicto).")
            else:
                order_ref.update({'status': 'rejected', 'bot_verification_msg': msg_res})
                logger.warning(f"🚨 [FIRESTORE] Orden {order_id} RECHAZADA (Doble uso).")

        elif dictamen == "NO_ENCONTRADO":
            order_ref.update({'status': 'rejected', 'bot_verification_msg': mensaje_banco})
            logger.warning(f"🚫 [FIRESTORE] Orden {order_id} RECHAZADA (No encontrado).")

    except (BancoCajaBloqueadaException, BancoMantenimientoException, requests.exceptions.RequestException) as e:
        intentos_actuales += 1
        logger.warning(f"⚠ [INFRAESTRUCTURA] {str(e)}. Intento {intentos_actuales}/{max_reintentos}.")
        
        if intentos_actuales < max_reintentos:
            minutos_espera = 3
            proximo_reintento = datetime.now(timezone.utc) + timedelta(minutes=minutos_espera)
            
            logger.info(f"⏳ [REINTENTO] Orden {order_id} programada en {minutos_espera} min.")
            order_ref.update({
                'status': 'pending_retry',
                'reintentos': intentos_actuales,
                'proximo_reintento': proximo_reintento,
                'bot_verification_msg': f"Banco ocupado/caído. Reintento {intentos_actuales}/{max_reintentos} en breve."
            })
        else:
            logger.error(f"🚨 [SISTEMA] Orden {order_id} agotó reintentos. Revisión manual requerida.")
            order_ref.update({
                'status': 'manual_review',
                'bot_verification_msg': "Exceso de reintentos: Banco no responde o caja permanentemente bloqueada."
            })
    finally:
        with lock_in_flight:
            if order_id in processed_in_flight:
                processed_in_flight.remove(order_id)

# --- 8. HILOS (WORKERS Y SWEEPER ENRUTADORES) ---
def pm_worker_loop():
    logger.info("🏦 [WORKER BANCO] Hilo exclusivo de Pago Móvil iniciado (Sesión única).")
    while True:
        try:
            # Esperamos máximo 120 segundos (2 minutos) por una orden en la fila
            order_id, datos_orden = pm_queue.get(timeout=120)
            try:
                procesar_validacion_en_banco(order_id, datos_orden)
            except Exception as e:
                logger.error(f"❌ [WORKER BANCO ERROR] Error crítico en {order_id}: {e}")
                with lock_in_flight:
                    if order_id in processed_in_flight:
                        processed_in_flight.remove(order_id)
            finally:
                pm_queue.task_done()
        except queue.Empty:
            # Si pasan 2 minutos y la fila está vacía (timeout), cerramos la sesión
            bank_manager.logout()

def binance_worker_loop(worker_id):
    logger.info(f"🟡 [WORKER BINANCE {worker_id}] Hilo paralelo iniciado.")
    while True:
        order_id, datos_orden = binance_queue.get()
        try:
            binance_bot.procesar_validacion_binance(order_id, datos_orden)
        except Exception as e:
            logger.error(f"❌ [WORKER BINANCE ERROR] Error crítico en {order_id}: {e}")
            with lock_in_flight:
                if order_id in processed_in_flight:
                    processed_in_flight.remove(order_id)
        finally:
            binance_queue.task_done()

def recuperador_ordenes_pendientes():
    logger.info("🧹 [SWEEPER] Hilo recuperador iniciado.")
    while True:
        try:
            time.sleep(60) 
            ahora = datetime.now(timezone.utc)
            
            ordenes = db.collection('store_orders') \
                .where('status', '==', 'pending_retry') \
                .where('proximo_reintento', '<=', ahora) \
                .get()

            for doc in ordenes:
                order_id = doc.id
                datos_orden = doc.to_dict()
                
                payment_details = datos_orden.get('payment_details', {})
                payment_method = payment_details.get('payment_method', '')
                
                if payment_method not in ['pago_movil', 'binance']:
                    continue
                
                with lock_in_flight:
                    if order_id not in processed_in_flight:
                        processed_in_flight.add(order_id)
                        
                        if payment_method == 'pago_movil':
                            logger.info(f"🔄 [SWEEPER] Re-encolando Pago Móvil atascado: {order_id}")
                            pm_queue.put((order_id, datos_orden))
                        elif payment_method == 'binance':
                            logger.info(f"🔄 [SWEEPER] Re-encolando Binance atascado: {order_id}")
                            binance_queue.put((order_id, datos_orden))
        except Exception as e:
            logger.error(f"❌ [SWEEPER ERROR] Fallo: {e}")

# --- 9. LISTENER Y ARRANQUE ---
def on_snapshot(col_snapshot, changes, read_time):
    for change in changes:
        if change.type.name == 'ADDED':
            doc = change.document
            order_id = doc.id
            datos_orden = doc.to_dict()
            
            payment_details = datos_orden.get('payment_details', {})
            payment_method = payment_details.get('payment_method', '')
            
            if payment_method not in ['pago_movil', 'binance']:
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

def start_bot_services():
    # 1. Hilo exclusivo del Banco (gestiona la sesión única y su timeout)
    threading.Thread(target=pm_worker_loop, daemon=True, name="Worker-Banco").start()
    
    # 2. Hilos de Binance (3 procesos en paralelo para rapidez)
    for i in range(3):
        threading.Thread(target=binance_worker_loop, args=(i+1,), daemon=True, name=f"Worker-Binance-{i+1}").start()

    # 3. Barredor de reintentos
    threading.Thread(target=recuperador_ordenes_pendientes, daemon=True, name="Sweeper").start()

    try:
        logger.info("🚀 [BOT] Conectando a Firestore...")
        db.collection('store_orders').where('status', '==', 'pending_verification').on_snapshot(on_snapshot)
        logger.info("✅ [BOT] Sistema operativo. Escuchando compras...")
    except Exception as e:
        logger.error(f"❌ [BOT ERROR] Listener falló: {e}")
    
    while True:
        time.sleep(3600)

if __name__ == "__main__":
    start_bot_services()
