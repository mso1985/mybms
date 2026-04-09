#!/usr/bin/env python3
"""
BMS Connector con PIN pairing
Para BMS JBD/Xiaoxiang que requieren emparejamiento con PIN (ej: 123456)
"""

import asyncio
import struct
import sys
import argparse
from typing import Optional, List
from dataclasses import dataclass
from datetime import datetime
from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

@dataclass
class BMSData:
    timestamp: datetime
    voltage_v: float
    current_a: float
    capacity_remain_ah: float
    capacity_total_ah: float
    soc_percent: int
    cycle_count: int
    temperature_c: List[float]
    cell_count: int
    cell_voltages: List[float]
    
    def __str__(self) -> str:
        temps_str = ", ".join([f"{t:.1f}" for t in self.temperature_c]) if self.temperature_c else "N/A"
        cells_str = ", ".join([f"{v:.3f}V" for v in self.cell_voltages[:8]])
        if len(self.cell_voltages) > 8:
            cells_str += f" ... ({len(self.cell_voltages)} total)"
        return f"""
=== BMS [{self.timestamp.strftime('%Y-%m-%d %H:%M:%S')}] ===
Voltaje:      {self.voltage_v:.2f} V
Corriente:    {self.current_a:.3f} A ({'Carga' if self.current_a > 0 else 'Descarga' if self.current_a < 0 else 'Reposo'})
Capacidad:    {self.capacity_remain_ah:.2f} / {self.capacity_total_ah:.2f} Ah
SOC:          {self.soc_percent}%
Ciclos:       {self.cycle_count}
Temperaturas: {temps_str} °C
Celdas:       {self.cell_count} ({cells_str})
================================================="""


class BMSConnector:
    """Conector para BMS con PIN pairing"""
    
    # UUIDs específicos del BMS
    SERVICE_UUID = "02f00000-0000-0000-0000-00000000fe00"
    WRITE_UUID = "02f00000-0000-0000-0000-00000000ff01"
    NOTIFY_UUID = "02f00000-0000-0000-0000-00000000ff02"
    
    def __init__(self, mac_address: str, pin: str = "123456"):
        self.mac_address = mac_address
        self.pin = pin
        self.client: Optional[BleakClient] = None
        self.response_data = bytearray()
        self.command_event = asyncio.Event()
        self.last_response: Optional[bytes] = None
        self._connected = False
        
    def calculate_crc(self, data: bytes) -> int:
        """Calcular CRC sumando todos los bytes"""
        return sum(data) & 0xFF
    
    def build_command(self, register: int, data: bytes = b'') -> bytes:
        """Construir comando JBD"""
        length = len(data)
        header = bytes([0xDD, 0xA5, register, length])
        if data:
            header += data
        crc = self.calculate_crc(header[2:])
        return header + bytes([crc, 0x77])
    
    def notification_handler(self, sender, data: bytearray):
        """Manejar notificaciones BLE"""
        self.response_data.extend(data)
        
        # Buscar mensaje completo JBD
        if len(self.response_data) >= 4:
            for i in range(3, len(self.response_data)):
                if self.response_data[i] == 0x77 and self.response_data[0] == 0xDD:
                    self.last_response = bytes(self.response_data[:i+1])
                    self.response_data = self.response_data[i+1:]
                    self.command_event.set()
                    return
    
    async def connect_with_pin(self) -> bool:
        """Conectar al BMS con PIN pairing"""
        print(f"Conectando a {self.mac_address}...")
        print(f"Usando PIN: {self.pin}")
        
        try:
            self.client = BleakClient(self.mac_address)
            await self.client.connect()
            
            if not self.client.is_connected:
                print("✗ No se pudo conectar")
                return False
            
            print(f"✓ Conectado a {self.client.name or 'BMS'}")
            
            # Hacer pairing con PIN
            print("Intentando emparejamiento seguro...")
            try:
                # En Linux/BlueZ, el PIN se maneja a nivel del agente
                # Intentamos el pairing
                await self.client.pair(protection_level=2)  # Encrypted pairing
                print("✓ Emparejamiento completado")
            except Exception as e:
                print(f"  Nota: {e}")
                print("  Intentando conexión sin emparejamiento adicional...")
            
            # Configurar notificaciones
            await self.client.start_notify(self.NOTIFY_UUID, self.notification_handler)
            print("✓ Notificaciones configuradas")
            
            # Enviar comando de "despertar" o handshake
            await self.send_wakeup()
            
            return True
            
        except Exception as e:
            print(f"✗ Error: {e}")
            return False
    
    async def send_wakeup(self):
        """Enviar comandos de inicio/wakeup"""
        # Algunos BMS necesitan un comando inicial especial
        # Intentar registro 0x00 (estado) como wake-up
        print("  Enviando handshake inicial...")
        
        # Comando de estado - a veces actúa como handshake
        cmd = self.build_command(0x00)
        try:
            await self.client.write_gatt_char(self.WRITE_UUID, cmd, response=False)
            await asyncio.sleep(0.1)
        except:
            pass
    
    async def send_command(self, register: int, description: str, timeout: float = 3.0) -> Optional[bytes]:
        """Enviar comando y esperar respuesta"""
        if not self.client or not self.client.is_connected:
            print("✗ No conectado")
            return None
        
        cmd = self.build_command(register)
        print(f"  → {description} (0x{register:02X})")
        
        self.command_event.clear()
        self.response_data.clear()
        self.last_response = None
        
        try:
            await self.client.write_gatt_char(self.WRITE_UUID, cmd, response=False)
            
            try:
                await asyncio.wait_for(self.command_event.wait(), timeout=timeout)
                return self.last_response
            except asyncio.TimeoutError:
                print(f"    ✗ Timeout")
                return None
                
        except Exception as e:
            print(f"    ✗ Error: {e}")
            return None
    
    def parse_basic_info(self, data: bytes) -> Optional[BMSData]:
        """Parsear información básica"""
        try:
            if len(data) < 4 or data[0] != 0xDD or data[1] != 0x03:
                print(f"    ? Respuesta no válida: {data[:8].hex()}...")
                return None
            
            length = data[3]
            payload = data[4:4+length]
            
            if len(payload) < 27:
                print(f"    ? Payload muy corto: {len(payload)} bytes")
                return None
            
            # Parsear datos
            voltage = struct.unpack('>H', payload[0:2])[0] / 100.0
            current_raw = struct.unpack('>h', payload[2:4])[0]
            current = current_raw / 100.0
            capacity_remain = struct.unpack('>H', payload[4:6])[0] / 100.0
            capacity_total = struct.unpack('>H', payload[6:8])[0] / 100.0
            cycle_count = struct.unpack('>H', payload[8:10])[0]
            
            soc = int((capacity_remain / capacity_total) * 100) if capacity_total > 0 else 0
            
            cell_count = payload[21]
            temp_count = payload[22]
            
            temperatures = []
            for i in range(min(temp_count, 6)):
                offset = 23 + (i * 2)
                if offset + 2 <= len(payload):
                    temp_raw = struct.unpack('>h', payload[offset:offset+2])[0]
                    temp = (temp_raw - 2731) / 10.0
                    temperatures.append(temp)
            
            return BMSData(
                timestamp=datetime.now(),
                voltage_v=voltage,
                current_a=current,
                capacity_remain_ah=capacity_remain,
                capacity_total_ah=capacity_total,
                soc_percent=soc,
                cycle_count=cycle_count,
                temperature_c=temperatures,
                cell_count=cell_count,
                cell_voltages=[]
            )
            
        except Exception as e:
            print(f"    ✗ Error parseando: {e}")
            return None
    
    def parse_cell_voltages(self, data: bytes, cell_count: int) -> List[float]:
        """Parsear voltajes de celdas"""
        voltages = []
        try:
            if len(data) < 4 or data[1] != 0x04:
                return voltages
            
            length = data[3]
            payload = data[4:4+length]
            
            for i in range(min(cell_count, 32)):
                offset = i * 2
                if offset + 2 <= len(payload):
                    v = struct.unpack('>H', payload[offset:offset+2])[0] / 1000.0
                    voltages.append(v)
                    
        except Exception as e:
            print(f"    ✗ Error: {e}")
        
        return voltages
    
    async def read_data(self) -> Optional[BMSData]:
        """Leer datos del BMS"""
        print("\n" + "="*50)
        print("Leyendo datos...")
        print("="*50)
        
        # Intentar leer información básica
        response = await self.send_command(0x03, "Información básica")
        
        if not response:
            print("\n✗ BMS no responde. Posibles causas:")
            print("  - El BMS requiere un paso de autenticación previo")
            print("  - El BMS está en modo de bajo consumo")
            print("  - Necesita un comando de 'wake up' específico")
            return None
        
        print(f"  ← {len(response)} bytes: {response.hex()[:50]}...")
        
        data = self.parse_basic_info(response)
        if not data:
            return None
        
        print(f"\n  ✓ Parseado:")
        print(f"    Voltaje: {data.voltage_v:.2f}V")
        print(f"    Corriente: {data.current_a:.2f}A")
        print(f"    SOC: {data.soc_percent}%")
        
        # Leer celdas
        if data.cell_count > 0:
            print(f"\n  → Leyendo {data.cell_count} celdas...")
            response = await self.send_command(0x04, "Voltajes de celdas")
            if response:
                data.cell_voltages = self.parse_cell_voltages(response, data.cell_count)
                print(f"    ✓ {len(data.cell_voltages)} celdas leídas")
        
        return data
    
    async def disconnect(self):
        """Desconectar"""
        if self.client and self.client.is_connected:
            await self.client.disconnect()
            print("\n✓ Desconectado")


async def main():
    parser = argparse.ArgumentParser(description='BMS Connector con PIN')
    parser.add_argument('mac', help='Dirección MAC del BMS')
    parser.add_argument('-p', '--pin', default='123456', help='PIN de emparejamiento (default: 123456)')
    parser.add_argument('-c', '--continuous', action='store_true', help='Lectura continua')
    parser.add_argument('-i', '--interval', type=int, default=5, help='Intervalo (segundos)')
    
    args = parser.parse_args()
    
    bms = BMSConnector(args.mac, pin=args.pin)
    
    if await bms.connect_with_pin():
        try:
            if args.continuous:
                while True:
                    data = await bms.read_data()
                    if data:
                        print(data)
                    await asyncio.sleep(args.interval)
            else:
                data = await bms.read_data()
                if data:
                    print(data)
                else:
                    print("\n✗ No se pudieron obtener datos")
        except KeyboardInterrupt:
            print("\n\nInterrumpido")
        finally:
            await bms.disconnect()
    else:
        print("\n✗ No se pudo conectar")


if __name__ == "__main__":
    asyncio.run(main())
