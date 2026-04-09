#!/usr/bin/env python3
"""
Script de diagnóstico para BMS Smart BMS
Prueba diferentes comandos y formatos para encontrar el correcto
"""

import asyncio
import struct
import sys
from typing import Optional, List
from bleak import BleakClient, BleakScanner

# UUIDs del BMS del usuario
WRITE_UUID = "02f00000-0000-0000-0000-00000000ff01"
NOTIFY_UUID = "02f00000-0000-0000-0000-00000000ff02"
ALT_RW_NOTIFY_UUID = "02f00000-0000-0000-0000-00000000ff04"


class BMSDebugger:
    def __init__(self, mac_address: str):
        self.mac_address = mac_address
        self.client: Optional[BleakClient] = None
        self.responses = []
        self.command_event = asyncio.Event()
        self.response_buffer = bytearray()
        
    def notification_handler(self, sender, data: bytearray):
        """Manejar notificaciones BLE"""
        print(f"  [NOTIFY] Recibidos {len(data)} bytes: {data.hex()}")
        self.response_buffer.extend(data)
        
        # Verificar fin de mensaje (0x77 para protocolo JBD)
        if len(self.response_buffer) >= 4 and self.response_buffer[-1] == 0x77:
            print(f"  [NOTIFY] Mensaje completo: {self.response_buffer.hex()}")
            self.responses.append(bytes(self.response_buffer))
            self.command_event.set()
    
    def calculate_crc(self, data: bytes) -> int:
        """Calcular CRC para protocolo JBD"""
        return sum(data) & 0xFF
    
    def build_command_jbd(self, command: int, data: bytes = b'') -> bytes:
        """Construir comando JBD estándar"""
        length = len(data)
        packet = bytes([0xDD, 0xA5, command, length]) + data
        crc = self.calculate_crc(packet[2:])
        packet += bytes([crc, 0x77])
        return packet
    
    def build_command_jbd_alt(self, command: int) -> bytes:
        """Alternativa de comando JBD"""
        return bytes([0xDD, 0xA5, command, 0x00, 0xFF, 0x77])
    
    def build_command_simple(self, command: int) -> bytes:
        """Comando simple sin CRC complejo"""
        return bytes([0xDD, 0xA5, command, 0x00, 0xFF, 0x77])
    
    async def connect(self) -> bool:
        """Conectar al BMS"""
        print(f"Conectando a {self.mac_address}...")
        try:
            self.client = BleakClient(self.mac_address)
            await self.client.connect()
            if self.client.is_connected:
                print(f"✓ Conectado a {self.client.name or 'BMS'}")
                
                # Configurar notificaciones
                await self.client.start_notify(NOTIFY_UUID, self.notification_handler)
                print(f"✓ Notificaciones activadas en {NOTIFY_UUID}")
                return True
        except Exception as e:
            print(f"✗ Error de conexión: {e}")
        return False
    
    async def send_command(self, command_bytes: bytes, description: str, timeout: float = 3.0):
        """Enviar comando y esperar respuesta"""
        print(f"\n{'='*60}")
        print(f"Probando: {description}")
        print(f"Comando (hex): {command_bytes.hex()}")
        print(f"Comando (bytes): {list(command_bytes)}")
        
        self.command_event.clear()
        self.response_buffer = bytearray()
        self.responses = []
        
        try:
            await self.client.write_gatt_char(WRITE_UUID, command_bytes, response=False)
            print(f"✓ Comando enviado")
            
            # Esperar respuesta
            try:
                await asyncio.wait_for(self.command_event.wait(), timeout=timeout)
                print(f"✓ Respuesta recibida!")
                
                # Analizar respuesta
                if self.responses:
                    self.analyze_response(self.responses[-1])
                    
            except asyncio.TimeoutError:
                print(f"✗ Timeout - no se recibió respuesta en {timeout}s")
                
        except Exception as e:
            print(f"✗ Error enviando comando: {e}")
    
    def analyze_response(self, data: bytes):
        """Analizar respuesta del BMS"""
        print(f"\n--- Análisis de respuesta ---")
        print(f"Total bytes: {len(data)}")
        print(f"Hex: {data.hex()}")
        print(f"Raw bytes: {list(data)}")
        
        if len(data) < 4:
            print("Respuesta muy corta para analizar")
            return
        
        # Verificar header JBD
        if data[0] == 0xDD and data[1] == 0x03:
            print("✓ Header JBD detectado (0xDD 0x03)")
            payload_len = data[3]
            print(f"  Payload length: {payload_len}")
            
            if len(data) >= 4 + payload_len + 2:
                payload = data[4:4+payload_len]
                crc_received = data[4+payload_len]
                end_byte = data[4+payload_len+1]
                
                print(f"  Payload: {payload.hex()}")
                print(f"  CRC recibido: 0x{crc_received:02X}")
                print(f"  End byte: 0x{end_byte:02X} (esperado 0x77)")
                
                # Intentar parsear datos
                self.try_parse_basic_info(payload)
        elif data[0] == 0xDD:
            print(f"? Header parcial JBD (0xDD), segundo byte: 0x{data[1]:02X}")
        else:
            print(f"? Header desconocido: 0x{data[0]:02X} 0x{data[1]:02X}")
    
    def try_parse_basic_info(self, data: bytes):
        """Intentar parsear información básica"""
        print(f"\n--- Intentando parsear datos ---")
        
        if len(data) < 20:
            print(f"Datos insuficientes ({len(data)} bytes, necesito >= 20)")
            return
        
        try:
            # Intentar diferentes offsets y formatos
            
            # Formato JBD estándar
            print("\nIntentando formato JBD estándar:")
            voltage = struct.unpack('>H', data[0:2])[0] / 100.0
            current_raw = struct.unpack('>h', data[2:4])[0]
            current = current_raw / 100.0
            capacity_remain = struct.unpack('>H', data[4:6])[0] / 100.0
            capacity_total = struct.unpack('>H', data[6:8])[0] / 100.0
            cycles = struct.unpack('>H', data[8:10])[0]
            
            print(f"  Voltaje: {voltage:.2f} V")
            print(f"  Corriente: {current:.2f} A (raw: {current_raw})")
            print(f"  Capacidad restante: {capacity_remain:.2f} Ah")
            print(f"  Capacidad total: {capacity_total:.2f} Ah")
            print(f"  Ciclos: {cycles}")
            
            if capacity_total > 0:
                soc = int((capacity_remain / capacity_total) * 100)
                print(f"  SOC: {soc}%")
            
            # Temperaturas
            if len(data) >= 24:
                num_temps = data[22]
                print(f"  Número de temperaturas: {num_temps}")
                for i in range(min(num_temps, 4)):
                    if 23 + i*2 + 2 <= len(data):
                        temp_raw = struct.unpack('>h', data[23 + i*2:25 + i*2])[0]
                        temp = (temp_raw - 2731) / 10.0
                        print(f"    Temp {i+1}: {temp:.1f}°C (raw: {temp_raw})")
                        
        except Exception as e:
            print(f"  Error parseando: {e}")
    
    async def run_tests(self):
        """Ejecutar batería de pruebas"""
        
        # Comandos JBD estándar
        await self.send_command(
            self.build_command_jbd(0x03),
            "JBD: Información básica (comando 0x03)"
        )
        
        await asyncio.sleep(1)
        
        await self.send_command(
            self.build_command_jbd(0x04),
            "JBD: Voltajes de celdas (comando 0x04)"
        )
        
        await asyncio.sleep(1)
        
        # Comando versión/hardware
        await self.send_command(
            self.build_command_jbd(0x05),
            "JBD: Información del BMS (comando 0x05)"
        )
        
        await asyncio.sleep(1)
        
        # Comando estado de protección
        await self.send_command(
            self.build_command_jbd(0x00),
            "JBD: Estado de protección (comando 0x00)"
        )
        
        await asyncio.sleep(1)
        
        # Probar con formato alternativo
        await self.send_command(
            self.build_command_jbd_alt(0x03),
            "JBD ALT: Información básica (formato alternativo)"
        )
        
    async def disconnect(self):
        if self.client and self.client.is_connected:
            await self.client.disconnect()
            print("\nDesconectado")


async def main():
    if len(sys.argv) < 2:
        print("Uso: python bms_debug.py <MAC_ADDRESS>")
        print("Ejemplo: python bms_debug.py A4:C1:38:XX:XX:XX")
        sys.exit(1)
    
    mac = sys.argv[1]
    debugger = BMSDebugger(mac)
    
    if await debugger.connect():
        try:
            await debugger.run_tests()
        except KeyboardInterrupt:
            print("\n\nInterrumpido por el usuario")
        finally:
            await debugger.disconnect()
    else:
        print("No se pudo conectar")


if __name__ == "__main__":
    asyncio.run(main())
