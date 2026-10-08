import os
import time
import threading
import queue
import requests
import logging
from datetime import datetime, timedelta, timezone
import firebase_admin
from firebase_admin import credentials, firestore

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

# --- 3. VARIABLES GLOBALES Y CONTROL DE CONCURRENCIA ---
order_queue = queue.Queue()
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
        self.base_url = "https://URL_DE_TU_BANCO.com" # <- AJUSTA ESTO: URL base del banco

    def login(self):
        try:
            # <- AJUSTA ESTO: Aquí va tu lógica real de inicio de sesión con requests
            res_login = self.session.post(
                f"{self.base_url}/login", 
                data={"usuario": "tu_usuario", "clave": "tu_clave"}, 
                allow_redirects=False
            )
            location = res_login.headers.get('Location', '')
            
            # DETECCIÓN DE CAJA BLOQUEADA
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
        # <- AJUSTA ESTO: Aquí va tu petición real de consulta al banco
        res = self.session.post(f"{self.base_url}/consultar", data=payload)
        return res

bank_manager = BankSessionManager()

# --- 6. FUNCIONES DE EVALUACIÓN ---
def evaluar_respuesta_banco(res_val):
    """
    <- AJUSTA ESTO: Pega aquí tu código que lee la respuesta del banco (BeautifulSoup o JSON).
    Debe retornar dos cosas: Un dictamen y un mensaje.
    Ejemplo de retorno: return "APROBADO", "Pago encontrado"
    """
    # Lógica simulada por ahora:
    return "NO_ENCONTRADO", "Aún no se ha implementado el scraping real."

def resolver_falso_positivo_ya_utilizado(order_id, ref_6_digitos, datos_orden):
    """
    <- AJUSTA ESTO: Pega aquí tu lógica para saber si un pago usado te pertenece.
    Debe retornar un booleano (True/False) y un mensaje.
    """
    return False, "Referencia ya fue usada por otra orden."

# --- 7. NÚCLEO DE VALIDACIÓN Y REINTENTOS ---
def procesar_validacion_en_banco(order_id, datos_orden):
    logger.info(f"⚙️ [PROCESANDO] Iniciando validación para orden {order_id}...")
    order_ref = db.collection('store_orders').document(order_id)
    
    # --- CORRECCIÓN: Extraer del mapa 'payment_details' ---
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
        # Ejecutar consulta
        res_val = bank_manager.consultar(payload_val)
        dictamen, mensaje_banco = evaluar_respuesta_banco(res_val)
        
        if dictamen == "BANCO_DOWN":
            raise BancoMantenimientoException(mensaje_banco)

        # Lógica de estados en Firestore
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
        # MANEJO DE BLOQUEOS (Caja ocupada o Red caída)
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
        # IMPORTANTE: Liberar la orden de la memoria sin importar si falló o tuvo éxito
        with lock_in_flight:
            if order_id in processed_in_flight:
                processed_in_flight.remove(order_id)

# --- 8. HILOS (WORKER Y SWEEPER) ---
def worker_loop():
    logger.info("👷 [WORKER] Hilo procesador iniciado.")
    while True:
        order_id, datos_orden = order_queue.get()
        try:
            procesar_validacion_en_banco(order_id, datos_orden)
        except Exception as e:
            logger.error(f"❌ [WORKER ERROR] Error crítico en {order_id}: {e}")
            with lock_in_flight:
                if order_id in processed_in_flight:
                    processed_in_flight.remove(order_id)
        finally:
            order_queue.task_done()

def recuperador_ordenes_pendientes():
    logger.info("🧹 [SWEEPER] Hilo recuperador iniciado.")
    while True:
        try:
            time.sleep(60) # Revisa Firestore cada 60 segundos
            ahora = datetime.now(timezone.utc)
            
            # Busca órdenes que estaban en pausa y ya pasó su tiempo
            ordenes = db.collection('store_orders') \
                .where('status', '==', 'pending_retry') \
                .where('proximo_reintento', '<=', ahora) \
                .get()

            for doc in ordenes:
                order_id = doc.id
                with lock_in_flight:
                    if order_id not in processed_in_flight:
                        processed_in_flight.add(order_id)
                        logger.info(f"🔄 [SWEEPER] Re-encolando orden atascada: {order_id}")
                        order_queue.put((order_id, doc.to_dict()))
        except Exception as e:
            logger.error(f"❌ [SWEEPER ERROR] Fallo: {e}")

# --- 9. LISTENER Y ARRANQUE ---
def on_snapshot(col_snapshot, changes, read_time):
    for change in changes:
        if change.type.name == 'ADDED':
            doc = change.document
            order_id = doc.id
            datos_orden = doc.to_dict()
            
            # --- NUEVO: FILTRO ESTRICTO DE MÉTODO DE PAGO ---
            payment_details = datos_orden.get('payment_details', {})
            payment_method = payment_details.get('payment_method', '')
            
            if payment_method != 'pago_movil':
                logger.info(f"⏭️ [LISTENER] Orden {order_id} ignorada. Es método: {payment_method}")
                continue # Salta a la siguiente orden sin procesarla
            # ------------------------------------------------
            
            with lock_in_flight:
                if order_id not in processed_in_flight:
                    processed_in_flight.add(order_id)
                    logger.info(f"📥 [LISTENER] Nueva orden de Pago Móvil ingresada: {order_id}")
                    order_queue.put((order_id, datos_orden))

def start_bot_services():
    threading.Thread(target=worker_loop, daemon=True).start()
    threading.Thread(target=recuperador_ordenes_pendientes, daemon=True).start()

    try:
        logger.info("🚀 [BOT] Conectando a Firestore...")
        db.collection('store_orders').where('status', '==', 'pending_verification').on_snapshot(on_snapshot)
        logger.info("✅ [BOT] Sistema operativo. Escuchando compras...")
    except Exception as e:
        logger.error(f"❌ [BOT ERROR] Listener falló: {e}")
    
    # Mantiene el bot vivo
    while True:
        time.sleep(3600)

if __name__ == "__main__":
    start_bot_services()
