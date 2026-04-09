#!/usr/bin/env python3
"""
Script final de diagnóstico usando gatttool
Última alternativa si bleak no funciona
"""

import subprocess
import sys
import time

def test_with_gatttool(mac_address: str):
    """Probar con gatttool de bluez"""
    
    print("="*60)
    print("DIAGNÓSTICO FINAL CON GATTTOOL")
    print("="*60)
    print(f"\nMAC: {mac_address}")
    print("\nEste script intenta usar gatttool que es más básico pero")
    print("a veces funciona cuando bleak/python no lo hace.\n")
    
    # Desconectar primero
    print("1. Desconectando del sistema...")
    subprocess.run(['bluetoothctl', 'disconnect', mac_address], 
                   capture_output=True)
    time.sleep(1)
    
    # Intentar leer características
    print("\n2. Leyendo características del BMS...")
    result = subprocess.run(
        ['gatttool', '-b', mac_address, '--characteristics'],
        capture_output=True,
        text=True,
        timeout=10
    )
    
    if result.returncode == 0:
        print("✓ Características:")
        print(result.stdout)
    else:
        print("✗ Error leyendo características:")
        print(result.stderr)
    
    # Intentar escribir comando básico
    print("\n3. Enviando comando básico (0x03)...")
    # Comando JBD: DD A5 03 00 03 77
    cmd_hex = "dda503000377"
    
    # Usar gatttool en modo interactivo para notificaciones
    print("   Iniciando conexión interactiva...")
    print("   (Presiona Ctrl+C después de 5 segundos)\n")
    
    try:
        # Este comando requiere interacción manual
        print("Ejecuta manualmente:")
        print(f"  gatttool -b {mac_address} -I")
        print("\nLuego en el prompt de gatttool:")
        print("  connect")
        print("  char-write-req 0x0011 dda503000377")
        print("  notification-enable")
        print("\n(Nota: 0x0011 es un ejemplo, el handle real puede variar)")
        
    except Exception as e:
        print(f"Error: {e}")
    
    print("\n" + "="*60)
    print("ALTERNATIVA: Usar Android para sniffing")
    print("="*60)
    print("\nDado que ningún método funciona desde Linux,")
    print("la única opción restante es sniffear el protocolo desde Android:")
    print("\n1. Instalar 'nRF Connect' o 'LightBlue' en tu celular")
    print("2. Conectar al BMS con la app Smart BMS")
    print("3. Anotar los handles y valores hex que se envían")
    print("4. Con nRF Connect también puedes exportar el log")
    print("\nO si tienes Android con root:")
    print("  - Usar 'Bluetooth HCI Snoop Log' en Opciones de Desarrollador")
    print("  - Analizar con Wireshark")
    print("="*60)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python bms_final_test.py <MAC_ADDRESS>")
        print("Ejemplo: python bms_final_test.py 41:19:09:01:50:D4")
        sys.exit(1)
    
    test_with_gatttool(sys.argv[1])
