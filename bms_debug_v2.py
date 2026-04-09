#!/usr/bin/env python3
"""
Script de diagnóstico v2 - Prueba múltiples formatos de comandos
para encontrar el protocolo correcto del Smart BMS
"""

import asyncio
import struct
import sys
from typing import Optional, List
from bleak import BleakClient, BleakScanner

# UUIDs del BMS
WRITE_UUID = "02f00000-0000-0000-0000-00000000ff01"
NOTIFY_UUID = "02f00000-0000-0000-0000-00000000ff02"


class BMSDebuggerV2:
    def __init__(self, mac_address: str):
        self.mac_address = mac_address
        self.client: Optional[BleakClient] = None
        self.response_buffer = bytearray()
        self.command_event = asyncio.Event()
        self.last_response: Optional[bytes] = None
        
    def notification_handler(self, sender, data: bytearray):
        """Manejar notificaciones BLE"""
        print(f"  [NOTIFY] {len(data)} bytes: {data.hex()}")
        self.response_buffer.extend(data)
        
        # Algunos BMS envían respuesta de una vez, otros en chunks
        # Vamos a esperar un poco y luego marcar como completo
        if len(self.response_buffer) >= 20:
            self.last_response = bytes(self.response_buffer)
            self.command_event.set()
    
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
                print(f"✓ Notificaciones activadas")
                return True
        except Exception as e:
            print(f"✗ Error: {e}")
        return False
    
    async def send_command(self, command_bytes: bytes, description: str, timeout: float = 5.0):
        """Enviar comando y esperar respuesta"""
        print(f"\n{'='*60}")
        print(f"{description}")
        print(f"Hex: {command_bytes.hex()}")
        
        self.command_event.clear()
        self.response_buffer = bytearray()
        self.last_response = None
        
        try:
            await self.client.write_gatt_char(WRITE_UUID, command_bytes, response=False)
            print(f"✓ Enviado")
            
            try:
                await asyncio.wait_for(self.command_event.wait(), timeout=timeout)
                print(f"✓ Respuesta recibida: {self.last_response.hex() if self.last_response else 'None'}")
                return True
            except asyncio.TimeoutError:
                print(f"✗ Timeout")
                return False
                
        except Exception as e:
            print(f"✗ Error: {e}")
            return False
    
    async def run_tests(self):
        """Ejecutar batería de pruebas con diferentes formatos"""
        
        print("\n" + "="*60)
        print("PRUEBA 1: Comando simple sin CRC (algunos BMS aceptan esto)")
        print("="*60)
        
        # Formato más simple posible
        await self.send_command(bytes([0xDD, 0xA5, 0x03]), "Simple: DD A5 03")
        await asyncio.sleep(0.5)
        
        # Con length=0 pero sin CRC/end
        await self.send_command(bytes([0xDD, 0xA5, 0x03, 0x00]), "Simple+len: DD A5 03 00")
        await asyncio.sleep(0.5)
        
        print("\n" + "="*60)
        print("PRUEBA 2: Comando con CRC simple (suma de bytes)")
        print("="*60)
        
        # Calcular CRC simple (suma de bytes del comando)
        for cmd in [0x03, 0x04]:
            data = bytes([0xDD, 0xA5, cmd, 0x00])
            crc = sum([0xA5, cmd, 0x00]) & 0xFF  # Solo sumar desde el comando
            packet = data + bytes([crc, 0x77])
            await self.send_command(packet, f"CRC(sum desde A5) cmd=0x{cmd:02X}: {packet.hex()}")
            await asyncio.sleep(0.5)
        
        print("\n" + "="*60)
        print("PRUEBA 3: Comando con CRC incluyendo header DD")
        print("="*60)
        
        for cmd in [0x03, 0x04]:
            data = bytes([0xDD, 0xA5, cmd, 0x00])
            crc = sum([0xDD, 0xA5, cmd, 0x00]) & 0xFF
            packet = data + bytes([crc, 0x77])
            await self.send_command(packet, f"CRC(sum todo) cmd=0x{cmd:02X}: {packet.hex()}")
            await asyncio.sleep(0.5)
        
        print("\n" + "="*60)
        print("PRUEBA 4: Comando sin byte de fin (0x77)")
        print("="*60)
        
        for cmd in [0x03, 0x04]:
            data = bytes([0xDD, 0xA5, cmd, 0x00])
            crc = sum([0xA5, cmd, 0x00]) & 0xFF
            packet = data + bytes([crc])  # Sin 0x77 al final
            await self.send_command(packet, f"Sin 0x77 cmd=0x{cmd:02X}: {packet.hex()}")
            await asyncio.sleep(0.5)
        
        print("\n" + "="*60)
        print("PRUEBA 5: Comando con length=1")
        print("="*60)
        
        for cmd in [0x03, 0x04]:
            data = bytes([0xDD, 0xA5, cmd, 0x01, 0x00])
            crc = sum([0xA5, cmd, 0x01, 0x00]) & 0xFF
            packet = data + bytes([crc, 0x77])
            await self.send_command(packet, f"Len=1 cmd=0x{cmd:02X}: {packet.hex()}")
            await asyncio.sleep(0.5)
        
        print("\n" + "="*60)
        print("PRUEBA 6: Protocolo ANT/BLE alternativo")
        print("="*60)
        
        # Algunos BMS usan protocolo similar a UART serial
        for cmd in [0x03, 0x04]:
            packet = bytes([0xA5, cmd, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
            await self.send_command(packet, f"Protocolo A5 cmd=0x{cmd:02X}: {packet.hex()}")
            await asyncio.sleep(0.5)
        
        print("\n" + "="*60)
        print("PRUEBA 7: Comando mínimo (solo trigger)")
        print("="*60)
        
        # Algunos BMS solo necesitan un byte específico
        for trigger in [0x01, 0x03, 0xA5, 0xDD]:
            await self.send_command(bytes([trigger]), f"Trigger 0x{trigger:02X}")
            await asyncio.sleep(0.5)
        
        print("\n" + "="*60)
        print("PRUEBA 8: Comando JK/BLE directo")
        print("="*60)
        
        # Protocolo JK BMS
        jk_cmd = bytes([0x4E, 0x57, 0x00, 0x13, 0x00, 0x00, 0x00, 0x00, 
                        0x06, 0x03, 0x00, 0x00, 0x00, 0x00, 0x00, 0x68, 
                        0x00, 0x00, 0x01, 0x29])
        await self.send_command(jk_cmd, "JK BMS comando")
        await asyncio.sleep(0.5)
        
        print("\n" + "="*60)
        print("PRUEBA 9: Comando con 0x00 padding")
        print("="*60)
        
        # Algunos BMS necesitan padding
        for cmd in [0x03, 0x04]:
            packet = bytes([0xDD, 0xA5, cmd, 0x00, 0x00, 0x00, 0x00, 0x77])
            await self.send_command(packet, f"Padding cmd=0x{cmd:02X}: {packet.hex()}")
            await asyncio.sleep(0.5)
        
        print("\n" + "="*60)
        print("PRUEBA 10: Comando DALY estándar")
        print("="*60)
        
        # Protocolo DALY
        for cmd in [0x90, 0x91, 0x92]:
            packet = bytes([0xA5, 0x01, cmd, 0x08, 0x00, 0x00, 0x00, 0x00, 
                          0x00, 0x00, 0x00, 0x00])  # Simplificado
            await self.send_command(packet, f"DALY cmd=0x{cmd:02X}: {packet.hex()}")
            await asyncio.sleep(0.5)
    
    async def disconnect(self):
        if self.client and self.client.is_connected:
            await self.client.disconnect()
            print("\n✓ Desconectado")


async def main():
    if len(sys.argv) < 2:
        print("Uso: python bms_debug_v2.py <MAC_ADDRESS>")
        print("Ejemplo: python bms_debug_v2.py A4:C1:38:XX:XX:XX")
        sys.exit(1)
    
    mac = sys.argv[1]
    debugger = BMSDebuggerV2(mac)
    
    if await debugger.connect():
        try:
            await debugger.run_tests()
        except KeyboardInterrupt:
            print("\n\nInterrumpido")
        finally:
            await debugger.disconnect()
    else:
        print("No se pudo conectar")


if __name__ == "__main__":
    asyncio.run(main())
