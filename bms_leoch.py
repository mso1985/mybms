#!/usr/bin/env python3
"""
Último intento - Protocolo JBD para Leoch LFP24100
Basado en documentación oficial de esphome-jbd-bms
"""

import asyncio
import struct
import sys
import argparse
from typing import Optional, List
from dataclasses import dataclass
from datetime import datetime
from bleak import BleakClient
import subprocess

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
        cells_str = ", ".join([f"{v:.3f}V" for v in self.cell_voltages]) if self.cell_voltages else "N/A"
        return f"""
=== Leoch LFP24100 [{self.timestamp.strftime('%Y-%m-%d %H:%M:%S')}] ===
Voltaje:      {self.voltage_v:.2f} V
Corriente:    {self.current_a:.3f} A
Capacidad:    {self.capacity_remain_ah:.2f} / {self.capacity_total_ah:.2f} Ah
SOC:          {self.soc_percent}%
Ciclos:       {self.cycle_count}
Temperaturas: {temps_str} °C
Celdas:       {self.cell_count} ({cells_str})
================================================="""


class LeochBMSConnector:
    """Conector específico para Leoch LFP24100 (BMS JBD)"""
    
    # UUIDs del BMS Leoch
    SERVICE_UUID = "02f00000-0000-0000-0000-00000000fe00"
    
    # ff01 = Write only
    WRITE_UUID = "02f00000-0000-0000-0000-00000000ff01"
    
    # ff02 = Read + Notify (para recibir datos)
    NOTIFY_UUID = "02f00000-0000-0000-0000-00000000ff02"
    
    # ff04 = Read + Write + Notify (alternativa)
    RW_NOTIFY_UUID = "02f00000-0000-0000-0000-00000000ff04"
    
    def __init__(self, mac_address: str):
        self.mac_address = mac_address
        self.client: Optional[BleakClient] = None
        self.response_data = bytearray()
        self.command_event = asyncio.Event()
        self.last_response: Optional[bytes] = None
        self.use_alt_uuid = False
        
    def calculate_crc(self, data: bytes) -> int:
        """CRC suma de bytes"""
        return sum(data) & 0xFF
    
    def build_command(self, register: int, data: bytes = b'') -> bytes:
        """Construir comando JBD según documentación oficial"""
        # Formato: DD A5 [register] [length] [data] [CRC] 77
        length = len(data)
        header = bytes([0xDD, 0xA5, register, length])
        if data:
            header += data
        crc = self.calculate_crc(header[2:])  # CRC desde A5
        return header + bytes([crc, 0x77])
    
    def notification_handler(self, sender, data: bytearray):
        """Manejar notificaciones"""
        self.response_data.extend(data)
        print(f"  [RECV] {len(data)} bytes: {data.hex()}")
        
        # JBD: mensaje termina con 0x77
        if len(self.response_data) >= 4:
            for i in range(len(self.response_data) - 1, 2, -1):
                if self.response_data[i] == 0x77:
                    # Verificar header
                    if self.response_data[0] == 0xDD:
                        self.last_response = bytes(self.response_data[:i+1])
                        self.response_data = self.response_data[i+1:]
                        self.command_event.set()
                        print(f"  [MSG] Completo: {self.last_response.hex()}")
                        return
    
    def disconnect_system(self):
        """Desconectar del sistema BlueZ"""
        try:
            subprocess.run(['bluetoothctl', 'disconnect', self.mac_address], 
                          capture_output=True, timeout=5)
            print("  Desconectado del sistema")
        except:
            pass
    
    async def connect(self) -> bool:
        """Conectar al BMS Leoch"""
        print(f"\n{'='*60}")
        print(f"Conectando a Leoch LFP24100 ({self.mac_address})")
        print(f"{'='*60}")
        
        # Desconectar del sistema primero
        self.disconnect_system()
        await asyncio.sleep(1)
        
        try:
            self.client = BleakClient(self.mac_address, timeout=15.0)
            await self.client.connect()
            
            if not self.client.is_connected:
                print("✗ No se pudo conectar")
                return False
            
            print(f"✓ Conectado: {self.client.name or 'Leoch BMS'}")
            
            # Configurar notificaciones en ff02
            try:
                await self.client.start_notify(self.NOTIFY_UUID, self.notification_handler)
                print(f"✓ Notificaciones en ff02")
            except Exception as e:
                print(f"  Nota: {e}")
                # Intentar con ff04
                try:
                    await self.client.start_notify(self.RW_NOTIFY_UUID, self.notification_handler)
                    print(f"✓ Notificaciones en ff04")
                    self.use_alt_uuid = True
                except Exception as e2:
                    print(f"✗ No se pudieron configurar notificaciones: {e2}")
                    return False
            
            return True
            
        except Exception as e:
            print(f"✗ Error: {e}")
            return False
    
    async def send_command(self, register: int, description: str, timeout: float = 5.0) -> Optional[bytes]:
        """Enviar comando"""
        cmd = self.build_command(register)
        write_uuid = self.RW_NOTIFY_UUID if self.use_alt_uuid else self.WRITE_UUID
        
        print(f"\n→ {description}")
        print(f"   Comando: {cmd.hex()}")
        print(f"   UUID escritura: {write_uuid}")
        
        self.command_event.clear()
        self.response_data.clear()
        self.last_response = None
        
        try:
            await self.client.write_gatt_char(write_uuid, cmd, response=False)
            print(f"   ✓ Enviado")
            
            try:
                await asyncio.wait_for(self.command_event.wait(), timeout=timeout)
                return self.last_response
            except asyncio.TimeoutError:
                print(f"   ✗ Timeout (sin respuesta)")
                return None
                
        except Exception as e:
            print(f"   ✗ Error: {e}")
            return None
    
    def parse_basic_info(self, data: bytes) -> Optional[BMSData]:
        """Parsear respuesta registro 0x03"""
        try:
            if len(data) < 4:
                return None
            
            if data[0] != 0xDD or data[1] != 0x03:
                print(f"   ? Header inesperado: {data[:4].hex()}")
                return None
            
            length = data[3]
            if 4 + length + 2 > len(data):
                print(f"   ? Datos incompletos")
                return None
            
            payload = data[4:4+length]
            
            # Parsear según especificación JBD
            # Verificar que tengamos suficientes bytes
            if len(payload) < 27:
                print(f"   ? Payload corto: {len(payload)} bytes")
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
            print(f"   ✗ Error parseando: {e}")
            return None
    
    def parse_cells(self, data: bytes, cell_count: int) -> List[float]:
        """Parsear voltajes de celdas (registro 0x04)"""
        voltages = []
        try:
            if data[1] != 0x04:
                return voltages
            length = data[3]
            payload = data[4:4+length]
            for i in range(min(cell_count, 48)):  # Hasta 48 celdas
                if i*2+2 <= len(payload):
                    v = struct.unpack('>H', payload[i*2:i*2+2])[0] / 1000.0
                    voltages.append(v)
        except:
            pass
        return voltages
    
    async def read_data(self) -> Optional[BMSData]:
        """Leer datos del BMS"""
        print(f"\n{'='*60}")
        print("LEYENDO DATOS DEL BMS LEOCH")
        print(f"{'='*60}")
        
        # Registro 0x03: Información básica
        response = await self.send_command(0x03, "Registro 0x03: Info básica")
        
        if not response:
            print("\n✗ EL BMS NO RESPONDE")
            print("\nEsto es inusual para un Leoch LFP24100.")
            print("Posibles causas:")
            print("1. El BMS está en modo de bajo consumo (sleep)")
            print("2. Requiere un comando de 'wake' previo")
            print("3. La conexión BLE no está establecida correctamente")
            print("4. Usa un protocolo diferente al JBD estándar")
            print("\nRecomendación: Verificar con app Smart BMS que funcione")
            return None
        
        print(f"\n   Respuesta: {len(response)} bytes")
        print(f"   Hex: {response.hex()}")
        
        data = self.parse_basic_info(response)
        if not data:
            print("\n✗ No se pudo parsear la respuesta")
            return None
        
        print(f"\n   ✓ Datos parseados:")
        print(f"     Voltaje: {data.voltage_v:.2f}V")
        print(f"     Corriente: {data.current_a:.2f}A")
        print(f"     SOC: {data.soc_percent}%")
        
        # Leer celdas
        if data.cell_count > 0:
            print(f"\n   Leyendo {data.cell_count} celdas...")
            response = await self.send_command(0x04, "Registro 0x04: Celdas")
            if response:
                data.cell_voltages = self.parse_cells(response, data.cell_count)
                print(f"   ✓ {len(data.cell_voltages)} celdas: {', '.join([f'{v:.3f}V' for v in data.cell_voltages])}")
        
        return data
    
    async def disconnect(self):
        if self.client and self.client.is_connected:
            await self.client.disconnect()
            print("\n✓ Desconectado")


async def main():
    parser = argparse.ArgumentParser(description='Leoch LFP24100 BMS Connector')
    parser.add_argument('mac', help='Dirección MAC (ej: 41:19:09:01:50:D4)')
    parser.add_argument('-c', '--continuous', action='store_true', help='Lectura continua')
    
    args = parser.parse_args()
    
    bms = LeochBMSConnector(args.mac)
    
    if await bms.connect():
        try:
            if args.continuous:
                while True:
                    data = await bms.read_data()
                    if data:
                        print(data)
                    else:
                        print("\n⚠ Reintentando en 5 segundos...")
                    await asyncio.sleep(5)
            else:
                data = await bms.read_data()
                if data:
                    print(data)
                else:
                    print("\n✗ No se pudieron obtener datos")
                    print("\n" + "="*60)
                    print("DIAGNÓSTICO:")
                    print("="*60)
                    print("\nEl BMS Leoch LFP24100 debería usar protocolo JBD.")
                    print("Si no responde, posibles causas:")
                    print("\n1. El BMS está 'dormido' - intentar:")
                    print("   - Desconectar la carga/descarga por 10 segundos")
                    print("   - Conectar una carga ligera para 'despertarlo'")
                    print("\n2. Protocolo diferente - La app Smart BMS podría usar")
                    print("   un protocolo propietario de Leoch")
                    print("\n3. Emparejamiento incorrecto - Verificar:")
                    print("   bluetoothctl info", args.mac)
                    print("\n4. Para ver el protocolo real necesitas:")
                    print("   - Android con HCI Snoop Log + Wireshark")
                    print("   - O usar app nRF Connect para ver tráfico BLE")
        except KeyboardInterrupt:
            print("\n\nInterrumpido")
        finally:
            await bms.disconnect()
    else:
        print("\n✗ No se pudo conectar")


if __name__ == "__main__":
    asyncio.run(main())
