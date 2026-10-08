import requests
import logging
from datetime import datetime, timedelta, timezone
from firebase_admin import firestore

logger = logging.getLogger(__name__)

# --- EXCEPCIONES PERSONALIZADAS ---
class BancoCajaBloqueadaException(Exception):
    pass

class BancoMantenimientoException(Exception):
    pass

# --- GESTOR DE SESIÓN DEL BANCO ---
class BankSessionManager:
    def __init__(self):
        self.session = requests.Session()
        self.is_logged_in = False
        self.base_url = "https://tesoropagos.bt.com.ve/login" # <- AJUSTA ESTO

    def login(self):
        try:
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
        if self.is_logged_in:
            logger.info("🔒 Cerrando sesión del banco por inactividad.")
            self.session.close()
            self.session = requests.Session()
            self.is_logged_in = False

bank_manager = BankSessionManager()

# --- FUNCIONES DE EVALUACIÓN ---
def evaluar_respuesta_banco(res_val):
    return "NO_ENCONTRADO", "Aún no se ha implementado el scraping real."

def resolver_falso_positivo_ya_utilizado(order_id, ref_6_digitos, datos_orden):
    return False, "Referencia ya fue usada por otra orden."

# --- NÚCLEO DE VALIDACIÓN ---
def procesar_validacion_en_banco(order_id, datos_orden, db):
    logger.info(f"⚙️ [PROCESANDO PAGO MÓVIL] Iniciando validación para orden {order_id}...")
    order_ref = db.collection('store_orders').document(order_id)
    
    # Extraemos los datos basándonos en tu estructura de base de datos
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
            order_ref.update({
                'status': 'pending_retry',
                'reintentos': intentos_actuales,
                'proximo_reintento': proximo_reintento,
                'bot_verification_msg': f"Banco ocupado/caído. Reintento {intentos_actuales}/{max_reintentos} en breve."
            })
        else:
            logger.error(f"🚨 [SISTEMA] Orden {order_id} agotó reintentos.")
            order_ref.update({
                'status': 'manual_review',
                'bot_verification_msg': "Exceso de reintentos: Banco no responde o caja permanentemente bloqueada."
            })
