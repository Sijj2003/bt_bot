import requests

def hacer_peticion_mrw(nro_tracking):
    """Capa de Red: Se conecta a la API de MRW y obtiene el JSON crudo."""
    url = "https://mrwve.com/api/tracking"
    
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
    }
    
    data = {
        "nro_tracking": str(nro_tracking)
    }
    
    try:
        response = requests.post(url, json=data, headers=headers, timeout=10)
        if response.status_code == 200:
            return response.json()
        else:
            return {"error_http": f"MRW rechazó la conexión. Código: {response.status_code}"}
            
    except Exception as e:
        return {"error_http": f"Fallo de red: {str(e)}"}


def procesar_respuesta_mrw(datos_json):
    """Capa de Datos: Extrae toda la información del paquete y el trayecto punto a punto."""
    
    if "error_http" in datos_json:
        return {"valido": False, "mensaje": datos_json["error_http"], "estatus_actual": "Error de Conexión"}

    if datos_json.get("codigo") == "03" or "error" in datos_json:
        return {"valido": False, "mensaje": "Número de guía inválido, falso o no registrado.", "estatus_actual": "Desconocido"}
    
    if "tracking" in datos_json and len(datos_json["tracking"]) > 0:
        
        # 1. Recorrer el array de tracking para armar el trayecto completo
        trayecto_completo = []
        for movimiento in datos_json["tracking"]:
            trayecto_completo.append({
                "fecha": movimiento.get("fecha"),
                "fecha_scan": movimiento.get("fecha_scan"),
                "estatus": movimiento.get("estatus"),
                "ubicacion": movimiento.get("estado") or movimiento.get("agencia", "No especificada"),
                "agencia": movimiento.get("agencia")
            })
        
        # 2. Extraer el último movimiento para el resumen rápido
        ultimo_movimiento = datos_json["tracking"][-1]
        
        # 3. Organizar toda la data disponible de forma estructurada
        informacion_limpia = {
            "valido": True,
            "nro_guia": datos_json.get("nro_envio"),
            "destinatario": datos_json.get("destinatario"),
            "ruta": f"{datos_json.get('agencia_origen')} -> {datos_json.get('agencia_destino')}",
            "descripcion": datos_json.get("descripcion", "Sin descripción"),
            "peso": datos_json.get("peso", "0"),
            "tipo_paquete": datos_json.get("tipo_paquete", "N/A"),
            
            # Resumen del estado actual (ideal para el dashboard interno)
            "estatus_actual": ultimo_movimiento.get("estatus"),
            "fecha_actualizacion": ultimo_movimiento.get("fecha"),
            "ubicacion_actual": ultimo_movimiento.get("estado") or ultimo_movimiento.get("agencia", "No especificada"),
            
            # El trayecto punto a punto (ideal para renderizar la línea de tiempo en el frontend del cliente)
            "historial": trayecto_completo
        }
        return informacion_limpia

    return {"valido": False, "mensaje": "Respuesta irreconocible del servidor de MRW.", "estatus_actual": "Desconocido"}


def consultar_guia(nro_tracking):
    json_crudo = hacer_peticion_mrw(nro_tracking)
    resultado_limpio = procesar_respuesta_mrw(json_crudo)
    return resultado_limpio


# ==========================================
# BLOQUE DE PRUEBAS LOCALES
# ==========================================
if __name__ == "__main__":
    
    print("--- PRUEBA: TRAYECTO PUNTO A PUNTO ---")
    resultado_real = consultar_guia("300120701008777")
    
    if resultado_real["valido"]:
        print(f"Guía: {resultado_real['nro_guia']}")
        print(f"Destinatario: {resultado_real['destinatario']}")
        print(f"Estatus Actual: {resultado_real['estatus_actual']} en {resultado_real['ubicacion_actual']}\n")
        
        print("--- HISTORIAL DE MOVIMIENTOS ---")
        for paso in resultado_real["historial"]:
            print(f"[{paso['fecha']}] {paso['estatus']} - {paso['ubicacion']}")
    else:
        print(resultado_real["mensaje"])
