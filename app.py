from flask import Flask, request, jsonify
import requests
from bs4 import BeautifulSoup
import threading
import os
import time
from datetime import datetime

app = Flask(__name__)

# =========================================================
# CONFIGURACIÓN (Variables de entorno de Render)
# =========================================================
# Reemplaza la URL de abajo con la URL de tu backend en PythonAnywhere si es diferente
API_BACKEND_URL = os.environ.get("API_BACKEND_URL", "https://sijj2003.pythonanywhere.com")
BOT_SECRET_KEY = os.environ.get("BOT_SECRET", "Gymenez2026Secure")
TESORO_SUCURSAL = os.environ.get("TESORO_SUCURSAL", "01334301")
TESORO_CAJA = os.environ.get("TESORO_CAJA", "03")
TESORO_PASS = os.environ.get("TESORO_PASS", "31103356")

# =========================================================
# LÓGICA DEL BOT BANCARIO
# =========================================================
def procesar_validacion_en_banco(datos_orden):
    """
    Se ejecuta en un hilo separado. 
    Inicia sesión, verifica la referencia en la API del banco y envía el resultado a PythonAnywhere.
    """
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'Accept-Encoding': 'gzip, deflate, br, zstd',
        'Accept-Language': 'es-ES,es;q=0.9',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1'
    })
    
    api_token_global = None
    resultado_final = {"status": "ERROR", "mensaje": "Fallo desconocido durante la ejecución del bot."}

    try:
        print(f"[BOT] Iniciando proceso para la orden {datos_orden.get('id_pedido')}...")
        session.cookies.clear()
        
        # --- PASO 1: LOGIN ---
        print("[BOT] Solicitando página de login...")
        res_get = session.get('https://tesoropagos.bt.com.ve/login')
        soup = BeautifulSoup(res_get.text, 'html.parser')
        
        payload_login = {}
        for inp in soup.find_all('input', type='hidden'):
            if inp.get('name'):
                payload_login[inp.get('name')] = inp.get('value', '')
                
        payload_login['security_code'] = TESORO_SUCURSAL
        payload_login['box_number'] = TESORO_CAJA
        payload_login['password'] = TESORO_PASS
        
        time.sleep(1) # Breve pausa para no saturar al banco
        
        print("[BOT] Enviando credenciales...")
        session.headers.update({'Referer': 'https://tesoropagos.bt.com.ve/login'})
        res_login = session.post('https://tesoropagos.bt.com.ve/login', data=payload_login, allow_redirects=False)
        
        location = res_login.headers.get('Location', '')
        if res_login.status_code != 302 or 'login' in location:
            raise Exception("Credenciales inválidas, usuario bloqueado, o sesión atorada en el banco.")
            
        print("[BOT] Login exitoso. Preparando API...")
        
        # --- PASO 2: OBTENER TOKEN PARA API ---
        res_dash = session.get('https://tesoropagos.bt.com.ve/pago-movil')
        soup_dash = BeautifulSoup(res_dash.text, 'html.parser')
        meta_token = soup_dash.find('meta', {'name': 'csrf-token'})
        api_token = meta_token['content'] if meta_token else payload_login.get('_token')
        api_token_global = api_token
        
        # --- PASO 3: CONSULTAR API DE MOVIMIENTOS ---
        print("[BOT] Consultando movimientos en el banco...")
        hoy = datetime.now().strftime("%d/%m/%Y")
        
        payload_api = {
            "fechaDesde": hoy,
            "fechaHasta": hoy
        }
        
        session.headers.update({
            'Accept': 'application/json, text/plain, */*',
            'Content-Type': 'application/json',
            'X-CSRF-TOKEN': api_token,
            'X-Requested-With': 'XMLHttpRequest',
            'Referer': 'https://tesoropagos.bt.com.ve/pago-movil'
        })
        
        res_api = session.post('https://tesoropagos.bt.com.ve/pago-movil/movimientos', json=payload_api)
        
        if res_api.status_code != 200:
             raise Exception(f"La API del banco devolvió error HTTP {res_api.status_code}.")
             
        movimientos = res_api.json()
        
        # --- PASO 4: BUSCAR REFERENCIA ---
        print("[BOT] Analizando JSON de respuesta...")
        referencia_a_buscar = str(datos_orden.get('referencia', ''))[-6:] # Aseguramos buscar últimos 6 dígitos
        pago_encontrado = None
        
        # Extraemos la lista de movimientos dependiendo de cómo la devuelva el banco
        lista_movs = movimientos if isinstance(movimientos, list) else movimientos.get('data', []) if isinstance(movimientos, dict) else []
        
        for mov in lista_movs:
            if isinstance(mov, dict) and 'referencia' in mov:
                ref_banco = str(mov.get('referencia', '')).strip().lstrip('0')
                ref_orden = str(referencia_a_buscar).strip().lstrip('0')
                if ref_banco == ref_orden:
                    pago_encontrado = mov
                    break
        
        if pago_encontrado:
             # Opcional: Podrías verificar también el monto aquí si el JSON lo incluye.
             resultado_final = {"status": "APROBADO", "mensaje": "Pago verificado exitosamente en los movimientos de hoy."}
             print("[BOT] ¡Referencia Encontrada!")
        else:
             resultado_final = {"status": "NO_ENCONTRADO", "mensaje": f"La referencia no figura en los movimientos de hoy del banco."}
             print("[BOT] Referencia NO encontrada.")
             
    except Exception as e:
        print(f"[BOT ERROR] {e}")
        resultado_final = {"status": "ERROR", "mensaje": str(e)}
        
    finally:
        # --- PASO 5: LOGOUT SEGURO ---
        if api_token_global:
            try:
                print("[BOT] Cerrando sesión bancaria...")
                session.headers.update({'Accept': 'text/html', 'Content-Type': 'application/x-www-form-urlencoded', 'X-Requested-With': None})
                session.post('https://tesoropagos.bt.com.ve/logout', data={'_token': api_token_global}, allow_redirects=False)
            except Exception as e:
                print(f"[BOT ERROR LOGOUT] No se pudo cerrar la sesión: {e}")
                
    # --- PASO 6: ENVIAR RESPUESTA FINAL A PYTHONANYWHERE ---
    print(f"[BOT] Enviando webhook de regreso a {API_BACKEND_URL}...")
    try:
        requests.post(
            f"{API_BACKEND_URL}/api/store/bot/update",
            json={
                "id_pedido": datos_orden['id_pedido'], 
                "status": resultado_final['status'], 
                "mensaje": resultado_final['mensaje']
            },
            headers={"X-Bot-Secret": BOT_SECRET_KEY},
            timeout=10 # Aquí sí podemos esperar a que PythonAnywhere conteste
        )
        print("[BOT] Proceso finalizado.")
    except Exception as e:
        print(f"[BOT ERROR WEBHOOK] Falló el envío a PythonAnywhere: {e}")

# ====================================================================
# ENDPOINT WEBHOOK (Escucha las peticiones de PythonAnywhere)
# ====================================================================
@app.route('/webhook/verificar', methods=['POST'])
def recibir_orden():
    # Seguridad: Solo tu backend puede llamar a este bot
    if request.headers.get("X-Bot-Secret") != BOT_SECRET_KEY:
        print("[WEBHOOK] Acceso denegado. Secreto incorrecto.")
        return jsonify({"error": "No autorizado"}), 401
        
    datos_orden = request.json
    
    if not datos_orden or 'id_pedido' not in datos_orden:
        return jsonify({"error": "Datos inválidos"}), 400
        
    print(f"[WEBHOOK] Recibida orden {datos_orden['id_pedido']} para verificar referencia {datos_orden.get('referencia')}")
    
    # Lanzar hilo en segundo plano (No bloquea a PythonAnywhere)
    threading.Thread(target=procesar_validacion_en_banco, args=(datos_orden,)).start()
    
    # Responder Inmediatamente
    return jsonify({"success": True, "mensaje": "Bot iniciado y trabajando en segundo plano"}), 200

# Ruta de Salud (Para que Render sepa que el servidor está vivo)
@app.route('/', methods=['GET'])
def health_check():
    return "Gymenez Bot is Running!", 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
