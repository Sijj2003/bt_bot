import os
import time
import imaplib
import email
import re
import logging
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup
from firebase_admin import firestore
from google.api_core.exceptions import AlreadyExists

logger = logging.getLogger(__name__)

EMAIL_ACCOUNT = os.environ.get("BINANCE_EMAIL", "sijj2003@gmail.com")
EMAIL_PASSWORD = os.environ.get("BINANCE_EMAIL_PASSWORD", "")
IMAP_SERVER = "imap.gmail.com"

# 🍎 NOTA: Le pasamos 'db' y 'order_id' para comparar exactamente quién usó el correo
def buscar_recibo_en_correo(referencia, monto_esperado, db, order_id):
    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER)
        mail.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
        mail.select('inbox')

        meses = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        ayer = datetime.now(timezone.utc) - timedelta(days=1)
        fecha_ayer = f"{ayer.day:02d}-{meses[ayer.month - 1]}-{ayer.year}"

        status, mensajes = mail.search(None, f'(FROM "binance" SINCE "{fecha_ayer}")')

        if status != 'OK' or not mensajes[0]:
            return False, "Bandeja vacía o sin correos de Binance.", None

        lista_ids = mensajes[0].split()
        
        for i in reversed(lista_ids[-15:]):
            res, msg_data = mail.fetch(i, '(RFC822)')
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    
                    # ==========================================
                    # 🛡️ 1. FILTRO ANTI-SPOOFING (REMITENTE REAL)
                    # ==========================================
                    sender = msg.get('From', '').lower()
                    if 'binance.com' not in sender:
                        # Si no viene del dominio oficial de Binance, es falso o reenviado.
                        continue 
                    
                    # Extraemos el ID único del correo
                    msg_id = msg.get('Message-ID', str(i)).strip()
                    
                    cuerpo = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() == "text/plain":
                                cuerpo += part.get_payload(decode=True).decode('utf-8', errors='ignore') + " "
                            elif part.get_content_type() == "text/html":
                                html_content = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                                cuerpo += BeautifulSoup(html_content, "html.parser").get_text(separator=' ') + " "
                    else:
                        cuerpo = msg.get_payload(decode=True).decode('utf-8', errors='ignore')
                        if msg.get_content_type() == "text/html":
                            cuerpo = BeautifulSoup(cuerpo, "html.parser").get_text(separator=' ')

                    cuerpo_limpio = re.sub(r'\s+', ' ', cuerpo).lower()
                    referencia_limpia = str(referencia).strip().lower()
                    
                    if monto_esperado % 1 == 0:
                        monto_str = str(int(monto_esperado))
                    else:
                        monto_str = str(monto_esperado)

                    # Si el correo tiene el Nickname y el Monto correctos...
                    if referencia_limpia in cuerpo_limpio and monto_str in cuerpo_limpio:
                        
                        # ==========================================
                        # 🛡️ 2. FILTRO ANTI-DOBLE USO (BÚSQUEDA SIMPLE)
                        # ==========================================
                        docs_usados = db.collection('store_orders').where('binance_email_id', '==', msg_id).limit(1).get()
                        
                        ya_usado_en_otra_orden = False
                        for d in docs_usados:
                            if d.id != order_id:
                                ya_usado_en_otra_orden = True
                        
                        if ya_usado_en_otra_orden:
                            logger.warning(f"🚨 [BINANCE FRAUDE] Intento de doble uso detectado. El correo de {referencia_limpia} ya fue asignado a otra orden.")
                            continue # El correo ya fue usado por otra persona/orden. Saltamos al siguiente correo.
                        
                        # Si llegamos aquí, el correo es OFICIAL de Binance y NO HA SIDO USADO antes.
                        mail.logout()
                        return True, f"Pago de {monto_str} USDT confirmado de {referencia_limpia}.", msg_id
        
        mail.logout()
        return False, "No se encontró un recibo válido o nuevo en el correo.", None

    except Exception as e:
        logger.error(f"Error conectando al correo: {e}")
        return False, f"Fallo de conexión IMAP: {e}", None

def procesar_validacion_binance(order_id, datos_orden):
    db = firestore.client()
    
    logger.info(f"🟡 [BINANCE WORKER] Iniciando validación para orden {order_id}...")
    order_ref = db.collection('store_orders').document(order_id)
    
    payment_details = datos_orden.get('payment_details', {})
    referencia_usuario = payment_details.get('referencia', '').strip()
    monto_esperado = float(datos_orden.get('total_usd', 0))

    max_reintentos = 5
    intentos_actuales = datos_orden.get("reintentos", 0)

    try:
        # 🍎 Ahora pasamos el order_id y la db para evitar conflictos y buscar el historial
        encontrado, mensaje, msg_id = buscar_recibo_en_correo(referencia_usuario, monto_esperado, db, order_id)
        
        if encontrado:
            # ========================================================
            # 🛡️ EL CANDADO ATÓMICO (Anti-Condición de Carrera)
            # ========================================================
            try:
                # Reemplazamos cualquier barra inclinada por un guion bajo por si acaso
                safe_msg_id = msg_id.replace('/', '_')
                
                db.collection('used_payments_registry').document(f"binance_{safe_msg_id}").create({
                    'order_id': order_id,
                    'method': 'binance',
                    'timestamp': firestore.SERVER_TIMESTAMP
                })
                
                logger.info(f"🎉 [BINANCE] Orden {order_id} APROBADA.")
                order_ref.update({
                    'status': 'approved',
                    'bot_verification_msg': mensaje,
                    'binance_email_id': msg_id,
                    'verified_at': firestore.SERVER_TIMESTAMP
                })
                
            except AlreadyExists:
                # ¡Hacker atrapado! Otro hilo reclamó este pago en el mismo milisegundo.
                logger.warning(f"🚨 [ATAQUE SIMULTÁNEO] El recibo Binance {msg_id} ya fue reclamado.")
                order_ref.update({
                    'status': 'rejected',
                    'bot_verification_msg': "Pago rechazado: Intento de doble uso simultáneo detectado."
                })
        else:
            raise Exception("NoEncontrado")

    except Exception as e:
        intentos_actuales += 1
        
        if intentos_actuales < max_reintentos:
            minutos_espera = 2 
            proximo_reintento = datetime.now(timezone.utc) + timedelta(minutes=minutos_espera)
            
            logger.info(f"⏳ [BINANCE] Orden {order_id}: Correo no detectado o ya usado. Intento {intentos_actuales}/{max_reintentos}.")
            
            order_ref.update({
                'status': 'pending_retry',
                'reintentos': intentos_actuales,
                'proximo_reintento': proximo_reintento,
                'bot_verification_msg': f"Esperando confirmación oficial de Binance... Intento {intentos_actuales} de {max_reintentos}."
            })
        else:
            logger.warning(f"🚫 [BINANCE] Orden {order_id} RECHAZADA. No hay recibos nuevos oficiales.")
            order_ref.update({
                'status': 'rejected',
                'bot_verification_msg': "No se recibió confirmación oficial nueva de Binance tras 10 minutos."
            })
