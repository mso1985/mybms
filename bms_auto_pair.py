#!/usr/bin/env python3
"""
BMS Connector con emparejamiento automático (PIN 123456)
Intenta hacer pair automáticamente antes de conectar
"""

import asyncio
import struct
import sys
import argparse
import subprocess
import time
from typing import Optional, List
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from bleak import BleakClient

# Crear directorio de logs
log_dir = Path.home() / ".bms_logs"
log_dir.mkdir(exist_ok=True)

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
=== BMS [{self.timestamp.strftime('%Y-%m-%d %H:%M:%S')}] ===
Voltaje:      {self.voltage_v:.2f} V
Corriente:    {self.current_a:.3f} A
Capacidad:    {self.capacity_remain_ah:.2f} / {self.capacity_total_ah:.2f} Ah
SOC:          {self.soc_percent}%
Ciclos:       {self.cycle_count}
Temperaturas: {temps_str} °C
Celdas:       {self.cell_count} ({cells_str})
================================================="""


def pair_with_pin(mac_address: str, pin: str = "123456", timeout: int = 30) -> bool:
    """
    Emparejar dispositivo Bluetooth usando bluetoothctl con PIN
    """
    print(f"\n{'='*60}")
    print(f"Emparejando con PIN: {pin}")
    print(f"{'='*60}")
    
    try:
        import pexpect
        
        # Usar pexpect para interactuar con bluetoothctl
        print("Iniciando bluetoothctl...")
        child = pexpect.spawn("bluetoothctl", timeout=timeout)
        
        # Esperar prompt
        child.expect("#")
        
        # Remover dispositivo si existe
        print("Removiendo emparejamiento anterior...")
        child.sendline(f"remove {mac_address}")
        time.sleep(1)
        
        # Escanear
        print("Escaneando...")
        child.sendline("scan on")
        time.sleep(3)
        
        # Intentar pair
        print(f"Emparejando con {mac_address}...")
        child.sendline(f"pair {mac_address}")
        
        # Esperar solicitud de PIN
        index = child.expect([
            "PIN code:",
             "Pairing successful",
            "Failed to pair",
            "Connection timed out",
            pexpect.TIMEOUT
        ], timeout=20)
        
        if index == 0:
            # Se solicitó PIN
            print(f"Ingresando PIN: {pin}")
            child.sendline(pin)
            
            # Esperar resultado
            index2 = child.expect([
                "Pairing successful",
                "Failed to pair",
                pexpect.TIMEOUT
            ], timeout=10)
            
            if index2 == 0:
                print("✓ Emparejamiento exitoso")
                # Trust y disconnect
                child.sendline(f"trust {mac_address}")
                time.sleep(1)
                child.sendline(f"disconnect {mac_address}")
                time.sleep(1)
                child.sendline("quit")
                return True
            else:
                print("✗ Emparejamiento falló después de ingresar PIN")
                child.sendline("quit")
                return False
                
        elif index == 1:
            print("✓ Ya estaba emparejado")
            child.sendline(f"trust {mac_address}")
            time.sleep(1)
            child.sendline(f"disconnect {mac_address}")
            time.sleep(1)
            child.sendline("quit")
            return True
            
        else:
            print("✗ Timeout o error en emparejamiento")
            child.sendline("quit")
            return False
            
    except ImportError:
        print("pexpect no instalado, intentando método alternativo...")
        return pair_with_pin_simple(mac_address, pin)
    except Exception as e:
        print(f"Error en emparejamiento: {e}")
        return False


def pair_with_pin_simple(mac_address: str, pin: str) -> bool:
    """
    Método alternativo usando comandos bluetoothctl básicos
    """
    print("\nIntentando emparejamiento simple...")
    
    try:
        # Ejecutar comandos secuenciales
        commands = [
            ["bluetoothctl", "scan", "on"],
            ["sleep", "3"],
            ["bluetoothctl", "pair", mac_address],
        ]
        
        # Remover primero
        subprocess.run(["bluetoothctl", "remove", mac_address], 
                      capture_output=True, timeout=5)
        
        # Escanear
        subprocess.Popen(["bluetoothctl", "scan", "on"], 
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(4)
        
        # Pair - esto normalmente pide PIN interactivamente
        print(f"Ejecutando: bluetoothctl pair {mac_address}")
        print("NOTA: Si pide PIN, ingresa manualmente: 123456")
        
        result = subprocess.run(
            ["bluetoothctl", "pair", mac_address],
            capture_output=True,
            text=True,
            timeout=15
        )
        
        print(f"Salida: {result.stdout}")
        print(f"Errores: {result.stderr}")
        
        # Trust y disconnect
        subprocess.run(["bluetoothctl", "trust", mac_address], 
                      capture_output=True, timeout=5)
        subprocess.run(["bluetoothctl", "disconnect", mac_address], 
                      capture_output=True, timeout=5)
        
        # Detener scan
        subprocess.run(["bluetoothctl", "scan", "off"], 
                      capture_output=True, timeout=5)
        
        return True
        
    except Exception as e:
        print(f"Error: {e}")
        return False


class BMSConnector:
    """Conector BMS con pairing automático"""
    
    WRITE_UUID = "02f00000-0000-0000-0000-00000000ff01"
    NOTIFY_UUID = "02f00000-0000-0000-0000-00000000ff02"
    ALT_NOTIFY_UUID = "02f00000-0000-0000-0000-00000000ff04"
    
    def __init__(self, mac_address: str, pin: str = "123456"):
        self.mac_address = mac_address
        self.pin = pin
        self.client: Optional[BleakClient] = None
        self.response_data = bytearray()
        self.command_event = asyncio.Event()
        self.last_response: Optional[bytes] = None
        self.use_alt_uuid = False
        
    def calculate_crc(self, data: bytes) -> int:
        return sum(data) & 0xFF
    
    def build_command(self, register: int, data: bytes = b'') -> bytes:
        length = len(data)
        header = bytes([0xDD, 0xA5, register, length])
        if data:
            header += data
        crc = self.calculate_crc(header[2:])
        return header + bytes([crc, 0x77])
    
    def notification_handler(self, sender, data: bytearray):
        self.response_data.extend(data)
        
        if len(self.response_data) >= 4:
            for i in range(len(self.response_data) - 1, 2, -1):
                if self.response_data[i] == 0x77 and self.response_data[0] == 0xDD:
                    self.last_response = bytes(self.response_data[:i+1])
                    self.response_data = self.response_data[i+1:]
                    self.command_event.set()
                    return
    
    async def connect(self, skip_pairing: bool = False) -> bool:
        """Conectar al BMS (con pairing previo si es necesario)"""
        print(f"\nConectando a {self.mac_address}...")
        
        # Intentar emparejamiento si no se salta
        if not skip_pairing:
            paired = pair_with_pin(self.mac_address, self.pin)
            if paired:
                print("✓ Emparejamiento completado")
                time.sleep(2)  # Esperar estabilización
            else:
                print("⚠ No se pudo emparejar, intentando conexión directa...")
        
        try:
            self.client = BleakClient(self.mac_address, timeout=15.0)
            await self.client.connect()
            
            if not self.client.is_connected:
                print("✗ No se pudo conectar")
                return False
            
            print(f"✓ Conectado a {self.client.name or 'BMS'}")
            
            # Configurar notificaciones
            try:
                await self.client.start_notify(self.NOTIFY_UUID, self.notification_handler)
                print("✓ Notificaciones activadas")
            except Exception as e:
                try:
                    await self.client.start_notify(self.ALT_NOTIFY_UUID, self.notification_handler)
                    self.use_alt_uuid = True
                    print("✓ Notificaciones activadas (UUID alternativo)")
                except Exception as e2:
                    print(f"✗ No se pudieron activar notificaciones: {e2}")
                    return False
            
            return True
            
        except Exception as e:
            print(f"✗ Error de conexión: {e}")
            return False
    
    async def send_command(self, register: int, description: str, timeout: float = 5.0) -> Optional[bytes]:
        cmd = self.build_command(register)
        write_uuid = self.ALT_NOTIFY_UUID if self.use_alt_uuid else self.WRITE_UUID
        
        print(f"\n→ {description}")
        print(f"   Comando: {cmd.hex()}")
        
        self.command_event.clear()
        self.response_data.clear()
        self.last_response = None
        
        try:
            await self.client.write_gatt_char(write_uuid, cmd, response=False)
            
            try:
                await asyncio.wait_for(self.command_event.wait(), timeout=timeout)
                print(f"   ← Respuesta: {len(self.last_response)} bytes")
                return self.last_response
            except asyncio.TimeoutError:
                print("   ✗ Timeout")
                return None
                
        except Exception as e:
            print(f"   ✗ Error: {e}")
            return None
    
    def parse_basic_info(self, data: bytes) -> Optional[BMSData]:
        try:
            if len(data) < 4 or data[0] != 0xDD or data[1] != 0x03:
                return None
            
            length = data[3]
            if 4 + length > len(data):
                return None
            
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
            return None
    
    async def read_data(self) -> Optional[BMSData]:
        print("\n" + "="*60)
        print("LEYENDO DATOS DEL BMS")
        print("="*60)
        
        response = await self.send_command(0x03, "Información básica")
        
        if not response:
            print("\n✗ El BMS no responde")
            return None
        
        print(f"   Hex: {response.hex()}")
        
        data = self.parse_basic_info(response)
        if not data:
            print("✗ No se pudo parsear")
            return None
        
        print(f"\n   ✓ Datos:")
        print(f"     Voltaje: {data.voltage_v:.2f}V")
        print(f"     Corriente: {data.current_a:.2f}A")
        print(f"     SOC: {data.soc_percent}%")
        
        # Celdas
        if data.cell_count > 0:
            response = await self.send_command(0x04, "Voltajes de celdas")
            if response:
                try:
                    length = response[3]
                    payload = response[4:4+length]
                    voltages = []
                    for i in range(min(data.cell_count, 48)):
                        if i*2+2 <= len(payload):
                            v = struct.unpack('>H', payload[i*2:i*2+2])[0] / 1000.0
                            voltages.append(v)
                    data.cell_voltages = voltages
                except:
                    pass
        
        return data
    
    async def disconnect(self):
        if self.client and self.client.is_connected:
            await self.client.disconnect()
            print("\n✓ Desconectado")


async def main():
    parser = argparse.ArgumentParser(description='BMS Connector con Pairing automático')
    parser.add_argument('mac', help='Dirección MAC')
    parser.add_argument('-p', '--pin', default='123456', help='PIN (default: 123456)')
    parser.add_argument('--skip-pairing', action='store_true', help='Saltar emparejamiento')
    parser.add_argument('-c', '--continuous', action='store_true', help='Lectura continua')
    
    args = parser.parse_args()
    
    bms = BMSConnector(args.mac, pin=args.pin)
    
    if await bms.connect(skip_pairing=args.skip_pairing):
        try:
            if args.continuous:
                while True:
                    data = await bms.read_data()
                    if data:
                        print(data)
                    await asyncio.sleep(5)
            else:
                data = await bms.read_data()
                if data:
                    print(data)
        except KeyboardInterrupt:
            print("\nInterrumpido")
        finally:
            await bms.disconnect()
    else:
        print("\n✗ No se pudo conectar")


if __name__ == "__main__":
    asyncio.run(main())
