# BMS Bluetooth Connector

Script en Python para conectar con BMS (Battery Management System) vía Bluetooth y capturar datos de la batería.

## Características

- Compatible con BMS que usan protocolo Smart BMS (Xiaoxiang, JBD, etc.)
- Lectura de voltaje total de batería
- Lectura de corriente (carga/descarga)
- Nivel de carga (SOC)
- Temperatura de la batería
- Voltaje de celdas individuales
- Ciclos de carga
- Modo de escaneo de dispositivos
- Lectura continua o single-shot

## Instalación

```bash
# Instalar dependencias
pip install -r requirements.txt

# O instalar directamente
pip install bleak
```

## Uso

### Escanear dispositivos BMS cercanos
```bash
python bms_connector.py --scan
```

### Conectar a un BMS específico
```bash
python bms_connector.py --mac XX:XX:XX:XX:XX:XX
```

### Lectura continua con intervalo personalizado
```bash
python bms_connector.py --mac XX:XX:XX:XX:XX:XX --interval 10
```

### Lectura única
```bash
python bms_connector.py --mac XX:XX:XX:XX:XX:XX --once
```

### Modo interactivo (seleccionar dispositivo)
```bash
python bms_connector.py
```

## Datos obtenidos

- **Voltaje total**: Voltaje de la batería completa (V)
- **Corriente**: Corriente de carga (+) o descarga (-) (A)
- **Capacidad**: Capacidad restante (Ah)
- **SOC**: State of Charge, porcentaje de carga (%)
- **Temperatura**: Temperatura(s) del pack de baterías (°C)
- **Voltajes de celdas**: Voltaje individual de cada celda (V)
- **Ciclos de carga**: Número de ciclos completos realizados

## Notas importantes

1. **Permisos**: En Linux, es posible que necesites ejecutar con `sudo` o configurar permisos de Bluetooth.

2. **Compatibilidad**: Este script está diseñado para BMS que usan el protocolo Smart BMS (también conocido como protocolo JBD/Xiaoxiang). Si tu BMS usa otro protocolo (como Daly o JK), podría requerir modificaciones.

3. **Conocer la MAC**: La dirección MAC del dispositivo se puede obtener:
   - Usando el modo `--scan` del script
   - Desde la configuración Bluetooth de tu sistema operativo
   - Desde la app Smart BMS (en ajustes > información del dispositivo)

4. **Protocolo alternativo**: Algunos BMS usan UUIDs BLE diferentes. El script intenta los UUIDs más comunes, pero si no funciona, verifica la documentación de tu BMS específico.

## Uso como módulo

```python
import asyncio
from bms_connector import BMSBluetoothConnector

async def main():
    connector = BMSBluetoothConnector(mac_address="XX:XX:XX:XX:XX:XX")
    
    # Conectar
    if await connector.connect():
        # Leer datos
        data = await connector.request_basic_info()
        print(f"Voltaje: {data.voltage_v}V")
        print(f"Corriente: {data.current_a}A")
        print(f"SOC: {data.capacity_percent}%")
        
        # Desconectar
        await connector.disconnect()

asyncio.run(main())
```

## Archivos disponibles

| Archivo | Descripción |
|---------|-------------|
| `bms_connector.py` | Script principal con detección automática |
| `bms_connector_paired.py` | Para usar después de emparejar con bluetoothctl |
| `bms_auth.py` | Intenta autenticación con PIN |
| `bms_h21.py` | Específico para firmware H2.1_103E_30XF |
| `bms_debug.py` | Debug básico de comandos |
| `bms_debug_v2.py` | Prueba múltiples formatos de comandos |
| `bms_sniffer.py` | Escucha notificaciones pasivamente |

## Troubleshooting

### Error: "No se pudo conectar"
- Verifica que el BMS esté encendido y Bluetooth activado
- Asegúrate de que la dirección MAC sea correcta
- Intenta acercarte más al dispositivo

### Error: "Timeout esperando respuesta"
- Algunos BMS requieren cierto tiempo de espera entre comandos
- Intenta aumentar el intervalo de lectura
- Verifica que el BMS use el protocolo Smart BMS

### Sin datos de celdas
- El comando de voltajes de celdas puede variar según el modelo
- Algunos BMS requieren un comando diferente para obtener celdas

### Error: "Characteristic was not found"
- El BMS usa UUIDs diferentes. Ejecuta: `python bms_connector.py --mac XX:XX:XX:XX:XX:XX --discover`

### Problemas con PIN
- Algunos BMS requieren emparejamiento con PIN antes de usar
- Usa `bluetoothctl` para emparejar primero:
  ```
  bluetoothctl
  scan on
  pair XX:XX:XX:XX:XX:XX
  # Ingresa PIN (ej: 123456)
  trust XX:XX:XX:XX:XX:XX
  disconnect XX:XX:XX:XX:XX:XX
  exit
  ```
- Luego usa: `python bms_connector_paired.py XX:XX:XX:XX:XX:XX`

## Protocolo no compatible

Si ningún script funciona, es posible que tu BMS use un **protocolo propietario** diferente al JBD/Smart BMS estándar. Algunos BMS chinos usan protocolos encriptados o modificados que requieren ingeniería inversa específica.

## Licencia

MIT License - Usa libremente y modifica según tus necesidades.
