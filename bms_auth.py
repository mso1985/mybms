#!/usr/bin/env python3
"""
BMS Connector - Protocolo JBD con autenticación PIN
Algunos BMS requieren enviar el PIN como comando antes de aceptar lecturas
"""

import asyncio
import struct
import sys
import argparse
from typing import Optional, List
from dataclasses import dataclass
from datetime import datetime
from bleak import BleakClient, BleakScanner

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


class BMSConnectorAuth:
    """Conector para BMS con autenticación PIN vía protocolo"""
    
    WRITE_UUID = "02f00000-0000-0000-0000-00000000ff01"
    NOTIFY_UUID = "02f00000-0000-0000-0000-00000000ff02"
    
    def __init__(self, mac_address: str, pin: str = "123456"):
        self.mac_address = mac_address
        self.pin = pin
        self.pin_bytes = pin.encode('ascii')
        self.client: Optional[BleakClient] = None
        self.response_data = bytearray()
        self.command_event = asyncio.Event()
        self.last_response: Optional[bytes] = None
        self._authenticated = False
        
    def calculate_crc(self, data: bytes) -> int:
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
        """Manejar notificaciones"""
        self.response_data.extend(data)
        
        if len(self.response_data) >= 4:
            for i in range(3, len(self.response_data)):
                if self.response_data[i] == 0x77 and self.response_data[0] == 0xDD:
                    self.last_response = bytes(self.response_data[:i+1])
                    self.response_data = self.response_data[i+1:]
                    self.command_event.set()
                    return
    
    async def connect(self) -> bool:
        """Conectar al BMS"""
        print(f"Conectando a {self.mac_address}...")
        
        try:
            self.client = BleakClient(self.mac_address)
            await self.client.connect()
            
            if not self.client.is_connected:
                print("✗ No se pudo conectar")
                return False
            
            print(f"✓ Conectado a {self.client.name or 'BMS'}")
            
            # Configurar notificaciones
            await self.client.start_notify(self.NOTIFY_UUID, self.notification_handler)
            
            # Intentar autenticación con PIN
            if await self.authenticate():
                print("✓ Autenticación exitosa")
            else:
                print("⚠ No se pudo autenticar, intentando lectura directa...")
            
            return True
            
        except Exception as e:
            print(f"✗ Error: {e}")
            return False
    
    async def authenticate(self) -> bool:
        """Enviar PIN como comando de autenticación"""
        print(f"  Autenticando con PIN {self.pin}...")
        
        # El PIN se envía típicamente en el registro 0x00 con el PIN como datos
        # o en el registro 0x20 según algunas implementaciones
        
        # Intentar diferentes métodos de autenticación
        
        # Método 1: PIN en registro 0x00
        cmd1 = self.build_command(0x00, self.pin_bytes)
        if await self.send_raw(cmd1, "Auth método 1", timeout=2.0):
            return True
        
        # Método 2: PIN en registro 0x20 (común en JBD)
        cmd2 = self.build_command(0x20, self.pin_bytes)
        if await self.send_raw(cmd2, "Auth método 2", timeout=2.0):
            return True
        
        # Método 3: PIN como comando especial
        # Algunos BMS usan formato: DD A5 [PIN length] [PIN] [CRC] 77
        pin_cmd = bytes([0xDD, 0xA5, len(self.pin_bytes)]) + self.pin_bytes
        crc = self.calculate_crc(pin_cmd[2:])
        pin_cmd += bytes([crc, 0x77])
        if await self.send_raw(pin_cmd, "Auth método 3", timeout=2.0):
            return True
        
        return False
    
    async def send_raw(self, data: bytes, description: str, timeout: float = 3.0) -> bool:
        """Enviar datos raw y verificar respuesta"""
        self.command_event.clear()
        self.response_data.clear()
        self.last_response = None
        
        try:
            await self.client.write_gatt_char(self.WRITE_UUID, data, response=False)
            await asyncio.wait_for(self.command_event.wait(), timeout=timeout)
            return self.last_response is not None
        except:
            return False
    
    async def send_command(self, register: int, description: str, timeout: float = 3.0) -> Optional[bytes]:
        """Enviar comando y esperar respuesta"""
        cmd = self.build_command(register)
        print(f"  → {description}")
        
        self.command_event.clear()
        self.response_data.clear()
        self.last_response = None
        
        try:
            await self.client.write_gatt_char(self.WRITE_UUID, cmd, response=False)
            await asyncio.wait_for(self.command_event.wait(), timeout=timeout)
            return self.last_response
        except asyncio.TimeoutError:
            print(f"    ✗ Timeout")
            return None
    
    def parse_basic_info(self, data: bytes) -> Optional[BMSData]:
        """Parsear información básica"""
        try:
            if len(data) < 4 or data[0] != 0xDD or data[1] != 0x03:
                print(f"    ? Respuesta: {data[:20].hex()}...")
                return None
            
            length = data[3]
            payload = data[4:4+length]
            
            if len(payload) < 27:
                return None
            
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
            print(f"    ✗ Error: {e}")
            return None
    
    def parse_cells(self, data: bytes, cell_count: int) -> List[float]:
        """Parsear voltajes de celdas"""
        voltages = []
        try:
            if data[1] != 0x04:
                return voltages
            length = data[3]
            payload = data[4:4+length]
            for i in range(min(cell_count, 32)):
                if i*2+2 <= len(payload):
                    v = struct.unpack('>H', payload[i*2:i*2+2])[0] / 1000.0
                    voltages.append(v)
        except:
            pass
        return voltages
    
    async def read_data(self) -> Optional[BMSData]:
        """Leer datos del BMS"""
        print("\n" + "="*50)
        print("Leyendo datos del BMS...")
        print("="*50)
        
        response = await self.send_command(0x03, "Información básica")
        
        if not response:
            print("\n✗ El BMS no responde a comandos")
            print("  Posibles causas:")
            print("  - El protocolo es diferente al JBD estándar")
            print("  - Requiere un handshake especial antes")
            print("  - El BMS está en modo bajo consumo")
            return None
        
        print(f"  ← {len(response)} bytes recibidos")
        
        data = self.parse_basic_info(response)
        if not data:
            print("\n✗ No se pudo parsear la respuesta")
            print(f"  Datos raw: {response.hex()}")
            return None
        
        # Leer celdas
        if data.cell_count > 0:
            response = await self.send_command(0x04, "Voltajes de celdas")
            if response:
                data.cell_voltages = self.parse_cells(response, data.cell_count)
        
        return data
    
    async def disconnect(self):
        if self.client and self.client.is_connected:
            await self.client.disconnect()
            print("\n✓ Desconectado")


async def main():
    parser = argparse.ArgumentParser(description='BMS Connector con autenticación PIN')
    parser.add_argument('mac', help='Dirección MAC del BMS')
    parser.add_argument('-p', '--pin', default='123456', help='PIN (default: 123456)')
    
    args = parser.parse_args()
    
    bms = BMSConnectorAuth(args.mac, pin=args.pin)
    
    if await bms.connect():
        try:
            data = await bms.read_data()
            if data:
                print(data)
            else:
                print("\n✗ No se pudieron obtener datos")
        except KeyboardInterrupt:
            pass
        finally:
            await bms.disconnect()
    else:
        print("\n✗ No se pudo conectar")


if __name__ == "__main__":
    asyncio.run(main())
