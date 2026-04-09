#!/usr/bin/env python3
"""
BMS Connector - Versión específica para H2.1_103E_30XF
Basado en el protocolo JBD/Xiaoxiang para Smart BMS
"""

import asyncio
import struct
import sys
import argparse
from typing import Optional, List, Dict, Any
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
        return f"""
=== BMS H2.1_103E_30XF [{self.timestamp.strftime('%Y-%m-%d %H:%M:%S')}] ===
Voltaje:      {self.voltage_v:.2f} V
Corriente:    {self.current_a:.3f} A ({'Carga' if self.current_a > 0 else 'Descarga' if self.current_a < 0 else 'Reposo'})
Capacidad:    {self.capacity_remain_ah:.2f} / {self.capacity_total_ah:.2f} Ah
SOC:          {self.soc_percent}%
Ciclos:       {self.cycle_count}
Temperaturas: {temps_str} °C
Celdas:       {self.cell_count} ({', '.join([f'{v:.3f}V' for v in self.cell_voltages])})
================================================="""


class BMSConnector:
    """Conector para BMS JBD/Xiaoxiang versión H2.1"""
    
    # UUIDs específicos para este modelo
    SERVICE_UUID = "02f00000-0000-0000-0000-00000000fe00"
    WRITE_UUID = "02f00000-0000-0000-0000-00000000ff01"
    NOTIFY_UUID = "02f00000-0000-0000-0000-00000000ff02"
    
    def __init__(self, mac_address: str):
        self.mac_address = mac_address
        self.client: Optional[BleakClient] = None
        self.response_data = bytearray()
        self.command_event = asyncio.Event()
        self.last_response: Optional[bytes] = None
        
    def calculate_crc(self, data: bytes) -> int:
        """Calcular CRC sumando todos los bytes"""
        return sum(data) & 0xFF
    
    def build_command(self, register: int, data: bytes = b'') -> bytes:
        """Construir comando JBD según especificación H2.1"""
        # Estructura: DD A5 [registro] [longitud] [datos] [CRC] 77
        length = len(data)
        header = bytes([0xDD, 0xA5, register, length])
        if data:
            header += data
        crc = self.calculate_crc(header[2:])  # CRC desde byte 2 (A5)
        return header + bytes([crc, 0x77])
    
    def notification_handler(self, sender, data: bytearray):
        """Manejar notificaciones BLE"""
        self.response_data.extend(data)
        
        # Verificar si tenemos un mensaje completo
        # El mensaje JBD termina con 0x77
        if len(self.response_data) >= 4:
            # Buscar el final del mensaje (0x77)
            for i in range(3, len(self.response_data)):
                if self.response_data[i] == 0x77:
                    # Verificar que sea un mensaje válido (empieza con DD)
                    if self.response_data[0] == 0xDD:
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
            print("✓ Notificaciones configuradas")
            
            return True
            
        except Exception as e:
            print(f"✗ Error: {e}")
            return False
    
    async def send_command(self, register: int, description: str, timeout: float = 3.0) -> Optional[bytes]:
        """Enviar comando y esperar respuesta"""
        cmd = self.build_command(register)
        print(f"  → {description} (reg 0x{register:02X})")
        
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
        """Parsear información básica (registro 0x03)"""
        try:
            if len(data) < 4:
                return None
            
            # Verificar formato
            if data[0] != 0xDD or data[1] != 0x03:
                print(f"    ? Header inesperado: {data[:4].hex()}")
                return None
            
            length = data[3]
            payload = data[4:4+length]
            
            if len(payload) < 27:
                print(f"    ? Payload muy corto: {len(payload)} bytes")
                return None
            
            # Parsear según especificación JBD H2.1
            voltage = struct.unpack('>H', payload[0:2])[0] / 100.0  # V
            current_raw = struct.unpack('>h', payload[2:4])[0]      # mA con signo
            current = current_raw / 100.0                           # A
            capacity_remain = struct.unpack('>H', payload[4:6])[0] / 100.0  # Ah
            capacity_total = struct.unpack('>H', payload[6:8])[0] / 100.0    # Ah
            cycle_count = struct.unpack('>H', payload[8:10])[0]
            
            # Calcular SOC
            soc = int((capacity_remain / capacity_total) * 100) if capacity_total > 0 else 0
            
            # Balance status (bytes 10-13)
            # Protection status (bytes 14-15)
            
            # Version de software (bytes 16-17)
            sw_version = struct.unpack('>H', payload[16:18])[0]
            
            # Configuración (bytes 18-19)
            
            # Número de celdas
            cell_count = payload[21]
            
            # Número de temperaturas
            temp_count = payload[22]
            
            # Temperaturas (cada una es 2 bytes, en 0.1K offset)
            temperatures = []
            for i in range(min(temp_count, 6)):  # Máximo 6 temperaturas
                offset = 23 + (i * 2)
                if offset + 2 <= len(payload):
                    temp_raw = struct.unpack('>h', payload[offset:offset+2])[0]
                    temp = (temp_raw - 2731) / 10.0  # Convertir de 0.1K a °C
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
        """Parsear voltajes de celdas (registro 0x04)"""
        voltages = []
        try:
            if len(data) < 4 or data[1] != 0x04:
                return voltages
            
            length = data[3]
            payload = data[4:4+length]
            
            # Cada celda son 2 bytes en mV
            for i in range(min(cell_count, 32)):  # Máximo 32 celdas
                offset = i * 2
                if offset + 2 <= len(payload):
                    v = struct.unpack('>H', payload[offset:offset+2])[0] / 1000.0
                    voltages.append(v)
                    
        except Exception as e:
            print(f"    ✗ Error parseando celdas: {e}")
        
        return voltages
    
    async def read_data(self) -> Optional[BMSData]:
        """Leer todos los datos del BMS"""
        print("\n" + "="*50)
        print("Leyendo datos del BMS...")
        print("="*50)
        
        # 1. Leer información básica
        response = await self.send_command(0x03, "Información básica")
        if not response:
            print("✗ No se pudo obtener información básica")
            return None
        
        print(f"  ← Respuesta: {len(response)} bytes")
        print(f"      Hex: {response.hex()}")
        
        data = self.parse_basic_info(response)
        if not data:
            return None
        
        print(f"\n  ✓ Datos básicos parseados")
        print(f"    Voltaje: {data.voltage_v:.2f}V")
        print(f"    Corriente: {data.current_a:.2f}A")
        print(f"    SOC: {data.soc_percent}%")
        
        # 2. Leer voltajes de celdas
        if data.cell_count > 0:
            print(f"\n  → Leyendo {data.cell_count} celdas...")
            response = await self.send_command(0x04, "Voltajes de celdas")
            if response:
                print(f"  ← Respuesta: {len(response)} bytes")
                data.cell_voltages = self.parse_cell_voltages(response, data.cell_count)
                print(f"    ✓ Celdas: {', '.join([f'{v:.3f}V' for v in data.cell_voltages])}")
        
        return data
    
    async def read_continuous(self, interval: int = 5):
        """Leer datos continuamente"""
        print(f"\nLectura continua cada {interval} segundos")
        print("Presiona Ctrl+C para detener\n")
        
        try:
            while True:
                data = await self.read_data()
                if data:
                    print(data)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            pass
    
    async def disconnect(self):
        """Desconectar del BMS"""
        if self.client and self.client.is_connected:
            await self.client.disconnect()
            print("\n✓ Desconectado")


async def main():
    parser = argparse.ArgumentParser(description='BMS Connector H2.1_103E_30XF')
    parser.add_argument('mac', help='Dirección MAC del BMS')
    parser.add_argument('-c', '--continuous', action='store_true', help='Lectura continua')
    parser.add_argument('-i', '--interval', type=int, default=5, help='Intervalo de lectura (segundos)')
    
    args = parser.parse_args()
    
    bms = BMSConnector(args.mac)
    
    if await bms.connect():
        try:
            if args.continuous:
                await bms.read_continuous(args.interval)
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
        print("\n✗ No se pudo conectar al BMS")


if __name__ == "__main__":
    asyncio.run(main())
