#!/usr/bin/env python3
"""
Script de sniffing - Solo escucha notificaciones del BMS
Algunos BMS envían datos periódicamente sin necesidad de comandos
"""

import asyncio
import struct
import sys
from datetime import datetime
from bleak import BleakClient, BleakScanner

NOTIFY_UUID = "02f00000-0000-0000-0000-00000000ff02"
ALT_NOTIFY_UUID = "02f00000-0000-0000-0000-00000000ff04"


class BMSSniffer:
    def __init__(self, mac_address: str):
        self.mac_address = mac_address
        self.client = None
        self.packet_count = 0
        
    def notification_handler(self, sender, data: bytearray):
        """Manejar notificaciones BLE"""
        self.packet_count += 1
        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        
        print(f"\n[{timestamp}] Paquete #{self.packet_count} - {len(data)} bytes")
        print(f"  Hex: {data.hex()}")
        print(f"  Raw: {list(data)}")
        
        # Intentar analizar
        self.analyze_packet(data)
    
    def analyze_packet(self, data: bytes):
        """Analizar contenido del paquete"""
        if len(data) < 4:
            return
        
        print(f"  Análisis:")
        
        # Verificar headers conocidos
        if data[0] == 0xDD:
            print(f"    ✓ Header 0xDD detectado (Smart BMS)")
            if len(data) > 1:
                print(f"    - Comando respuesta: 0x{data[1]:02X}")
            if len(data) > 3:
                payload_len = data[3]
                print(f"    - Payload length: {payload_len}")
                if len(data) >= 4 + payload_len + 2:
                    payload = data[4:4+payload_len]
                    crc = data[4+payload_len]
                    end = data[4+payload_len+1]
                    print(f"    - CRC: 0x{crc:02X}")
                    print(f"    - End byte: 0x{end:02X} (expected 0x77: {'✓' if end == 0x77 else '✗'})")
                    print(f"    - Payload: {payload.hex()}")
        
        elif data[0] == 0xA5:
            print(f"    ✓ Header 0xA5 detectado (posible DALY)")
        
        elif data[0] == 0x4E and len(data) > 1 and data[1] == 0x57:
            print(f"    ✓ Header 0x4E 0x57 detectado (JK BMS)")
        
        else:
            print(f"    ? Header desconocido: 0x{data[0]:02X}")
        
        # Intentar encontrar valores que parezcan voltaje (en mV o en 0.01V)
        if len(data) >= 20:
            print(f"\n    Posibles voltajes encontrados:")
            for i in range(len(data) - 1):
                try:
                    val = struct.unpack('>H', data[i:i+2])[0]
                    # Si el valor está entre 2000 y 8000, podría ser voltaje en 0.1V (200V-800V)
                    # o entre 20000 y 80000 para mV (20V-80V)
                    if 200 <= val <= 800:  # 20.0V - 80.0V en 0.1V
                        print(f"      Offset {i}: {val/10:.1f}V (raw: {val})")
                    elif 20000 <= val <= 80000:  # 20V - 80V en mV
                        print(f"      Offset {i}: {val/1000:.3f}V (raw: {val})")
                except:
                    pass
    
    async def connect_and_sniff(self, duration: int = 30):
        """Conectar y escuchar notificaciones"""
        print(f"Conectando a {self.mac_address}...")
        
        try:
            self.client = BleakClient(self.mac_address)
            await self.client.connect()
            
            if not self.client.is_connected:
                print("✗ No se pudo conectar")
                return
            
            print(f"✓ Conectado a {self.client.name or 'BMS'}")
            print(f"\nEscuchando notificaciones por {duration} segundos...")
            print("(Algunos BMS envían datos automáticamente cada pocos segundos)")
            print("Presiona Ctrl+C para detener\n")
            
            # Configurar notificaciones
            await self.client.start_notify(NOTIFY_UUID, self.notification_handler)
            
            # También probar el otro UUID por si acaso
            try:
                await self.client.start_notify(ALT_NOTIFY_UUID, self.notification_handler)
                print("✓ Notificaciones activadas en ambos UUIDs")
            except:
                print("✓ Notificaciones activadas")
            
            # Esperar
            await asyncio.sleep(duration)
            
        except KeyboardInterrupt:
            print(f"\n\nDetenido. Total de paquetes recibidos: {self.packet_count}")
        except Exception as e:
            print(f"✗ Error: {e}")
        finally:
            if self.client and self.client.is_connected:
                await self.client.disconnect()
                print("✓ Desconectado")


async def main():
    if len(sys.argv) < 2:
        print("Uso: python bms_sniffer.py <MAC_ADDRESS> [duracion_segundos]")
        print("Ejemplo: python bms_sniffer.py A4:C1:38:XX:XX:XX 60")
        sys.exit(1)
    
    mac = sys.argv[1]
    duration = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    
    sniffer = BMSSniffer(mac)
    await sniffer.connect_and_sniff(duration)


if __name__ == "__main__":
    asyncio.run(main())
