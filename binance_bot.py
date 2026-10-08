import os
import time
import imaplib
import email
import re
import logging
from datetime import datetime, timedelta, timezone
from firebase_admin import firestore

logger = logging.getLogger(__name__)

# Credenciales de correo configuradas en Render
EMAIL_ACCOUNT = os.environ.get("BINANCE_EMAIL", "sijj2003@gmail.com")
EMAIL_PASSWORD = os.environ.get("BINANCE_EMAIL_PASSWORD", "")
IMAP_SERVER = "imap.gmail.com"

def buscar_recibo_en_correo(referencia, monto_esperado):
    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER)
        mail.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
        mail.select('inbox')

        fecha_ayer = (datetime.now() - timedelta(days=1)).strftime("%d-%b-%Y")
        status, mensajes = mail.search(None, f'(FROM "binance" SINCE "{fecha_ayer}")')

        if status != 'OK':
            return False, "No se pudo acceder a la bandeja de entrada."

        lista_ids = mensajes[0].split()
        
        for i in reversed(lista_ids[-15:]):
            res, msg_data = mail.fetch(i, '(RFC822)')
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    
                    cuerpo = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() in ["text/plain", "text/html"]:
                                cuerpo += part.get_payload(decode=True).decode('utf-8', errors='ignore')
                    else:
                        cuerpo = msg.get_payload(decode=True).decode('utf-8', errors='ignore')

                    cuerpo_limpio = re.sub(r'\s+', ' ', cuerpo).lower()
                    referencia_limpia = str(referencia).strip().lower()
                    
                    if monto_esperado % 1 == 0:
                        monto_str = str(int(monto_esperado))
                    else:
                        monto_str = str(monto_esperado)

                    if referencia_limpia in cuerpo_limpio and monto_str in cuerpo_limpio:
                        mail.logout()
                        return True, f"Pago de {monto_str} USDT confirmado de {referencia_limpia}."
        
        mail.logout()
        return False, "No se encontró el recibo en el correo."

    except Exception as e:
        logger.error(f"Error conectando al correo: {e}")
        raise e

def procesar_validacion_binance(order_id, datos_orden):
    # INICIALIZACIÓN DIFERIDA: Se conecta a Firebase solo cuando se llama a la función
    db = firestore.client()
    
    logger.info(f"🟡 [BINANCE WORKER] Iniciando validación para orden {order_id}...")
    order_ref = db.collection('store_orders').document(order_id)
    
    payment_details = datos_orden.get('payment_details', {})
    
    referencia_usuario = payment_details.get('referencia', '').strip()
    monto_esperado = float(datos_orden.get('total_usd', 0))

    max_reintentos = 5
    intentos_actuales = datos_orden.get("reintentos", 0)

    try:
        encontrado, mensaje = buscar_recibo_en_correo(referencia_usuario, monto_esperado)
        
        if encontrado:
            logger.info(f"🎉 [BINANCE] Orden {order_id} APROBADA.")
            order_ref.update({
                'status': 'approved',
                'bot_verification_msg': mensaje,
                'verified_at': firestore.SERVER_TIMESTAMP
            })
        else:
            raise Exception("NoEncontrado")

    except Exception as e:
        intentos_actuales += 1
        
        if intentos_actuales < max_reintentos:
            minutos_espera = 2 
            proximo_reintento = datetime.now(timezone.utc) + timedelta(minutes=minutos_espera)
            
            logger.info(f"⏳ [BINANCE] Orden {order_id}: Correo no recibido aún. Intento {intentos_actuales}/{max_reintentos}.")
            
            order_ref.update({
                'status': 'pending_retry',
                'reintentos': intentos_actuales,
                'proximo_reintento': proximo_reintento,
                'bot_verification_msg': f"Esperando notificación del pago... Intento {intentos_actuales} de {max_reintentos}."
            })
        else:
            logger.warning(f"🚫 [BINANCE] Orden {order_id} RECHAZADA. El recibo nunca llegó.")
            order_ref.update({
                'status': 'rejected',
                'bot_verification_msg': "No se recibió confirmación de Binance Pay tras 10 minutos de espera. Revise que su Nickname esté bien escrito."
            })
