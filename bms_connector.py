#!/usr/bin/env python3
"""
BMS Bluetooth Connector
Script para conectar con BMS (Battery Management System) vía Bluetooth
y capturar datos como voltaje, corriente, temperatura, SOC, etc.

Compatible con BMS que usan el protocolo Smart BMS (común en BMS de
marca Xiaoxiang, JBD, etc.)

Requisitos:
    pip install bleak

Uso:
    python bms_connector.py
    python bms_connector.py --mac XX:XX:XX:XX:XX:XX
    python bms_connector.py --mac XX:XX:XX:XX:XX:XX --discover  # Ver servicios
"""

import asyncio
import struct
import sys
import argparse
from typing import Optional, Callable, Dict, Any, List
from dataclasses import dataclass
from datetime import datetime

try:
    from bleak import BleakClient, BleakScanner
    from bleak.backends.characteristic import BleakGATTCharacteristic
except ImportError:
    print("Error: Se requiere instalar 'bleak'")
    print("Ejecuta: pip install bleak")
    sys.exit(1)


@dataclass
class BMSData:
    """Datos del BMS"""
    timestamp: datetime
    voltage_v: float
    current_a: float
    capacity_ah: float
    capacity_percent: int
    temperature_c: List[float]
    cell_voltages: List[float]
    protection_status: Dict[str, bool]
    charge_cycles: int
    
    def __str__(self) -> str:
        temps_str = ", ".join([f"{t:.1f}°C" for t in self.temperature_c])
        cells_str = ", ".join([f"{v:.3f}V" for v in self.cell_voltages[:8]])
        if len(self.cell_voltages) > 8:
            cells_str += f" ... ({len(self.cell_voltages)} celdas)"
        return f"""
=== Datos BMS [{self.timestamp.strftime('%Y-%m-%d %H:%M:%S')}] ===
Voltaje Total:     {self.voltage_v:.3f} V
Corriente:         {self.current_a:.3f} A
Capacidad:         {self.capacity_ah:.3f} Ah
SOC:               {self.capacity_percent}%
Temperaturas:      {temps_str}
Ciclos de Carga:   {self.charge_cycles}
Celdas:            {cells_str}
======================================="""


class BMSBluetoothConnector:
    """
    Conector para BMS Bluetooth usando protocolo Smart BMS/JBD
    """
    
    # UUIDs comunes para BMS Xiaoxiang/JBD (modo 1)
    UART_SERVICE_UUID = "0000ff00-0000-1000-8000-00805f9b34fb"
    TX_CHARACTERISTIC_UUID = "0000ff02-0000-1000-8000-00805f9b34fb"  # Write
    RX_CHARACTERISTIC_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"  # Notify
    
    # UUIDs alternativos (modo 2 - HM-10/BL module)
    UART_SERVICE_UUID_ALT1 = "0000ffe0-0000-1000-8000-00805f9b34fb"
    UART_CHAR_UUID_ALT1 = "0000ffe1-0000-1000-8000-00805f9b34fb"  # Read/Write/Notify
    
    # UUIDs para algunos BMS DALY
    DALY_SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
    DALY_WRITE_UUID = "0000fff2-0000-1000-8000-00805f9b34fb"
    DALY_NOTIFY_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"
    
    # UUIDs para BMS JK (Jikong)
    JK_SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
    JK_CHAR_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"
    
    def __init__(self, mac_address: Optional[str] = None):
        self.mac_address = mac_address
        self.client: Optional[BleakClient] = None
        self.data_callback: Optional[Callable[[BMSData], None]] = None
        self.response_buffer = bytearray()
        self.expected_length = 0
        self.command_event = asyncio.Event()
        self.last_response: Optional[bytes] = None
        
        # UUIDs detectados dinámicamente
        self.write_uuid: Optional[str] = None
        self.notify_uuid: Optional[str] = None
        self.mode = "auto"  # auto, jbd, daly, jk, hm10
        
    def set_data_callback(self, callback: Callable[[BMSData], None]):
        """Establecer callback para recibir datos del BMS"""
        self.data_callback = callback
        
    def _calculate_crc(self, data: bytes) -> int:
        """Calcular CRC para el protocolo BMS JBD"""
        crc = 0
        for byte in data:
            crc += byte
        return crc & 0xFF
    
    def _build_command_jbd(self, command: int, data: bytes = b'') -> bytes:
        """Construir comando BMS JBD/Smart BMS con headers y CRC"""
        length = len(data)
        packet = bytes([0xDD, 0xA5, command, length]) + data
        crc = self._calculate_crc(packet[2:])  # CRC desde command
        packet += bytes([crc, 0x77])
        return packet
    
    def _build_command_daly(self, command: int) -> bytes:
        """Construir comando BMS DALY"""
        # Protocolo DALY: Start(0xA5) + Command + Length + Data + CRC + End(0xA5)
        # Implementación básica
        return bytes([0xA5, command, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
    
    def _build_command_jk(self, command: int) -> bytes:
        """Construir comando BMS JK (Jikong)"""
        # Protocolo JK tiene un formato diferente
        return bytes([0x4E, 0x57, 0x00, 0x13, 0x00, 0x00, 0x00, 0x00, 
                      0x06, command, 0x00, 0x00, 0x00, 0x00, 0x00, 0x68, 0x00, 0x00, 0x01, 0x29])
    
    def _build_command(self, command: int, data: bytes = b'') -> bytes:
        """Construir comando según el modo detectado"""
        if self.mode == "daly":
            return self._build_command_daly(command)
        elif self.mode == "jk":
            return self._build_command_jk(command)
        else:
            # Default JBD/Smart BMS
            return self._build_command_jbd(command, data)
    
    def _parse_jbd_basic_info(self, data: bytes) -> Optional[BMSData]:
        """Parsear información básica del BMS JBD (comando 0x03)"""
        try:
            if len(data) < 25:
                return None
                
            # Parsear según protocolo JBD/Smart BMS
            voltage = struct.unpack('>H', data[0:2])[0] / 100.0  # mV to V
            current = struct.unpack('>h', data[2:4])[0] / 100.0   # mA to A (signed)
            capacity_remain = struct.unpack('>H', data[4:6])[0] / 100.0  # mAh to Ah
            
            # Número de celdas y temperaturas
            num_cells = data[21]
            num_temps = data[22]
            
            # Capacidad total y ciclos
            capacity_total = struct.unpack('>H', data[6:8])[0] / 100.0
            cycles = struct.unpack('>H', data[8:10])[0]
            
            # Calcular SOC
            soc = int((capacity_remain / capacity_total) * 100) if capacity_total > 0 else 0
            
            # Parsear temperaturas (2 temperaturas típicamente)
            temperatures = []
            for i in range(min(num_temps, 4)):
                temp_raw = struct.unpack('>h', data[23 + i*2:25 + i*2])[0]
                temp = (temp_raw - 2731) / 10.0  # Kelvin*10 to Celsius
                temperatures.append(temp)
            
            return BMSData(
                timestamp=datetime.now(),
                voltage_v=voltage,
                current_a=current,
                capacity_ah=capacity_remain,
                capacity_percent=soc,
                temperature_c=temperatures,
                cell_voltages=[],
                protection_status={},
                charge_cycles=cycles
            )
        except Exception as e:
            print(f"Error parseando datos básicos JBD: {e}")
            return None
    
    def _parse_daly_basic_info(self, data: bytes) -> Optional[BMSData]:
        """Parsear información básica del BMS DALY"""
        try:
            if len(data) < 13:
                return None
            
            voltage = struct.unpack('>H', data[0:2])[0] / 10.0  # 0.1V to V
            current_raw = struct.unpack('>h', data[2:4])[0]
            current = (current_raw - 30000) / 10.0  # Offset 30000, 0.1A
            soc = data[12]
            
            return BMSData(
                timestamp=datetime.now(),
                voltage_v=voltage,
                current_a=current,
                capacity_ah=0.0,
                capacity_percent=soc,
                temperature_c=[],
                cell_voltages=[],
                protection_status={},
                charge_cycles=0
            )
        except Exception as e:
            print(f"Error parseando datos DALY: {e}")
            return None
    
    def _parse_cell_voltages_jbd(self, data: bytes) -> List[float]:
        """Parsear voltajes de celdas JBD (comando 0x04)"""
        voltages = []
        try:
            for i in range(0, len(data) - 1, 2):
                if i + 1 < len(data):
                    v = struct.unpack('>H', data[i:i+2])[0] / 1000.0
                    voltages.append(v)
        except Exception as e:
            print(f"Error parseando voltajes de celdas: {e}")
        return voltages
    
    def _notification_handler(self, sender: BleakGATTCharacteristic, data: bytearray):
        """Manejar notificaciones BLE del BMS"""
        self.response_buffer.extend(data)
        
        # Para JBD: mensaje termina con 0x77
        if len(self.response_buffer) >= 4 and self.response_buffer[-1] == 0x77:
            self.last_response = bytes(self.response_buffer)
            self.response_buffer = bytearray()
            self.command_event.set()
            self._process_response(self.last_response)
        # Para DALY/JK: mensaje tiene longitud fija o header específico
        elif len(self.response_buffer) >= 13:
            self.last_response = bytes(self.response_buffer)
            self.response_buffer = bytearray()
            self.command_event.set()
            self._process_response(self.last_response)
    
    def _process_response(self, data: bytes):
        """Procesar respuesta del BMS"""
        if len(data) < 4:
            return
        
        bms_data = None
        
        # Detectar tipo de respuesta
        if data[0] == 0xDD and data[1] == 0x03:
            # Respuesta JBD comando 0x03 (info básica)
            payload_length = data[3]
            payload = data[4:4+payload_length]
            bms_data = self._parse_jbd_basic_info(payload)
        elif data[0] == 0xDD and data[1] == 0x04:
            # Respuesta JBD comando 0x04 (celdas)
            pass  # Procesar celdas por separado
        elif data[0] == 0xA5:
            # Respuesta DALY
            bms_data = self._parse_daly_basic_info(data[4:])
        
        if bms_data and self.data_callback:
            self.data_callback(bms_data)
    
    async def discover_services(self) -> Dict[str, Any]:
        """Descubrir servicios y características del BMS"""
        if not self.client or not self.client.is_connected:
            print("Error: No hay conexión activa")
            return {}
        
        services_info = {}
        print("\n=== Servicios BLE descubiertos ===")
        
        for service in self.client.services:
            print(f"\nServicio: {service.uuid}")
            chars = []
            for char in service.characteristics:
                props = []
                if "read" in char.properties:
                    props.append("R")
                if "write" in char.properties:
                    props.append("W")
                if "notify" in char.properties:
                    props.append("N")
                if "indicate" in char.properties:
                    props.append("I")
                
                print(f"  Característica: {char.uuid}")
                print(f"    Propiedades: {', '.join(props)}")
                chars.append({
                    'uuid': char.uuid,
                    'properties': char.properties
                })
            
            services_info[service.uuid] = chars
        
        return services_info
    
    async def _detect_mode_and_uuids(self):
        """Detectar automáticamente el modo y UUIDs del BMS"""
        if not self.client:
            return False
        
        print("Detectando tipo de BMS y UUIDs...")
        
        for service in self.client.services:
            service_uuid = service.uuid.lower()
            
            # Detectar JBD/Smart BMS
            if "0000ff00" in service_uuid:
                print("  → Detectado: BMS JBD/Smart BMS")
                self.mode = "jbd"
                for char in service.characteristics:
                    char_uuid = char.uuid.lower()
                    if "0000ff02" in char_uuid and "write" in char.properties:
                        self.write_uuid = char.uuid
                        print(f"     Write UUID: {self.write_uuid}")
                    if "0000ff01" in char_uuid and "notify" in char.properties:
                        self.notify_uuid = char.uuid
                        print(f"     Notify UUID: {self.notify_uuid}")
                return True
            
            # Detectar HM-10/BL tipo
            elif "0000ffe0" in service_uuid:
                print("  → Detectado: Módulo BLE HM-10/BL (probablemente JBD)")
                self.mode = "hm10"
                for char in service.characteristics:
                    char_uuid = char.uuid.lower()
                    if "0000ffe1" in char_uuid:
                        if "write" in char.properties:
                            self.write_uuid = char.uuid
                        if "notify" in char.properties:
                            self.notify_uuid = char.uuid
                        print(f"     Read/Write/Notify UUID: {char.uuid}")
                return True
            
            # Detectar DALY
            elif "0000fff0" in service_uuid:
                print("  → Detectado: BMS DALY")
                self.mode = "daly"
                for char in service.characteristics:
                    char_uuid = char.uuid.lower()
                    if "0000fff2" in char_uuid and "write" in char.properties:
                        self.write_uuid = char.uuid
                        print(f"     Write UUID: {self.write_uuid}")
                    if "0000fff1" in char_uuid and "notify" in char.properties:
                        self.notify_uuid = char.uuid
                        print(f"     Notify UUID: {self.notify_uuid}")
                return True
            
            # Detectar JK (Jikong)
            elif self.client.name and "jk" in self.client.name.lower():
                print("  → Detectado: BMS JK (por nombre)")
                self.mode = "jk"
                for char in service.characteristics:
                    if "notify" in char.properties and "write" in char.properties:
                        self.write_uuid = char.uuid
                        self.notify_uuid = char.uuid
                        print(f"     Read/Write/Notify UUID: {char.uuid}")
                return True
        
        # Fallback: buscar cualquier característica write/notify
        print("  → Modo no detectado, probando UUIDs genéricos...")
        for service in self.client.services:
            for char in service.characteristics:
                if "write" in char.properties and not self.write_uuid:
                    self.write_uuid = char.uuid
                if "notify" in char.properties and not self.notify_uuid:
                    self.notify_uuid = char.uuid
        
        if self.write_uuid and self.notify_uuid:
            print(f"     Write UUID: {self.write_uuid}")
            print(f"     Notify UUID: {self.notify_uuid}")
            self.mode = "generic"
            return True
        
        return False
    
    async def scan_devices(self, timeout: float = 5.0) -> List[Dict[str, Any]]:
        """Escanear dispositivos BLE BMS cercanos"""
        print("Escaneando dispositivos BLE...")
        devices = await BleakScanner.discover(timeout=timeout)
        
        bms_devices = []
        for device in devices:
            name = device.name or "Unknown"
            if name and any(keyword in name.lower() for keyword in 
                          ['bms', 'battery', 'smart', 'xiaoxiang', 'jbd', 'daly', 'jk']):
                bms_devices.append({
                    'name': name,
                    'address': device.address,
                    'rssi': device.rssi
                })
                print(f"  Encontrado: {name} [{device.address}] RSSI: {device.rssi}")
        
        return bms_devices
    
    async def connect(self, mac_address: Optional[str] = None) -> bool:
        """Conectar al BMS"""
        target_mac = mac_address or self.mac_address
        
        if not target_mac:
            print("Error: No se especificó dirección MAC")
            return False
        
        print(f"Conectando a {target_mac}...")
        
        try:
            self.client = BleakClient(target_mac)
            await self.client.connect()
            
            if not self.client.is_connected:
                print("Error: No se pudo conectar")
                return False
            
            print("Conectado exitosamente!")
            print(f"Nombre del dispositivo: {self.client.name or 'N/A'}")
            
            # Detectar modo y UUIDs automáticamente
            if not await self._detect_mode_and_uuids():
                print("ADVERTENCIA: No se pudieron detectar UUIDs automáticamente")
                print("Usa --discover para ver los servicios disponibles")
            
            # Configurar notificaciones
            await self._setup_notifications()
            
            return True
            
        except Exception as e:
            print(f"Error de conexión: {e}")
            return False
    
    async def _setup_notifications(self):
        """Configurar notificaciones BLE"""
        if not self.notify_uuid:
            print("No se pudo configurar notificaciones: UUID no detectado")
            return
        
        try:
            await self.client.start_notify(
                self.notify_uuid, 
                self._notification_handler
            )
            print("Notificaciones configuradas correctamente")
        except Exception as e:
            print(f"Error configurando notificaciones: {e}")
    
    async def request_basic_info(self) -> Optional[BMSData]:
        """Solicitar información básica del BMS"""
        if not self.client or not self.client.is_connected:
            print("Error: No hay conexión activa")
            return None
        
        if not self.write_uuid:
            print("Error: No se detectó UUID de escritura")
            return None
        
        self.command_event.clear()
        self.response_buffer = bytearray()
        
        # Comando según el modo detectado
        if self.mode == "daly":
            command = self._build_command(0x90)  # Comando DALY para info básica
        elif self.mode == "jk":
            command = self._build_command(0x03)  # Comando JK
        else:
            command = self._build_command(0x03)  # Comando JBD estándar
        
        try:
            await self.client.write_gatt_char(
                self.write_uuid, 
                command,
                response=False
            )
            
            try:
                await asyncio.wait_for(self.command_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                print("Timeout esperando respuesta")
                return None
            
            if self.last_response:
                if self.mode == "daly":
                    return self._parse_daly_basic_info(self.last_response)
                else:
                    # JBD/JK parsing
                    if len(self.last_response) > 4:
                        payload_length = self.last_response[3]
                        payload = self.last_response[4:4+payload_length]
                        return self._parse_jbd_basic_info(payload)
                        
        except Exception as e:
            print(f"Error solicitando información: {e}")
        
        return None
    
    async def request_cell_voltages(self) -> List[float]:
        """Solicitar voltajes de celdas"""
        if not self.client or not self.client.is_connected:
            return []
        
        if not self.write_uuid:
            return []
        
        self.command_event.clear()
        self.response_buffer = bytearray()
        
        command = self._build_command(0x04)  # Comando para celdas
        
        try:
            await self.client.write_gatt_char(
                self.write_uuid, 
                command,
                response=False
            )
            
            await asyncio.wait_for(self.command_event.wait(), timeout=5.0)
            
            if self.last_response:
                payload_length = self.last_response[3]
                payload = self.last_response[4:4+payload_length]
                return self._parse_cell_voltages_jbd(payload)
                
        except Exception as e:
            print(f"Error solicitando voltajes de celdas: {e}")
        
        return []
    
    async def read_voltage_continuous(self, interval: float = 5.0):
        """Leer voltaje continuamente"""
        print(f"\nIniciando lectura continua cada {interval} segundos...")
        print("Presiona Ctrl+C para detener\n")
        
        try:
            while True:
                data = await self.request_basic_info()
                if data:
                    print(f"[{data.timestamp.strftime('%H:%M:%S')}] "
                          f"Voltaje: {data.voltage_v:.3f}V | "
                          f"Corriente: {data.current_a:.3f}A | "
                          f"SOC: {data.capacity_percent}%")
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            print("\nLectura detenida")
    
    async def disconnect(self):
        """Desconectar del BMS"""
        if self.client and self.client.is_connected:
            await self.client.disconnect()
            print("Desconectado del BMS")


async def interactive_mode(connector: BMSBluetoothConnector):
    """Modo interactivo para seleccionar dispositivo"""
    devices = await connector.scan_devices(timeout=5.0)
    
    if not devices:
        print("No se encontraron dispositivos BMS específicos")
        print("Escaneando todos los dispositivos BLE...")
        all_devices = await BleakScanner.discover(timeout=5.0)
        for i, d in enumerate(all_devices[:10]):
            print(f"  {i+1}. {d.name or 'Unknown'} [{d.address}] RSSI: {d.rssi}")
        
        if all_devices:
            choice = input("\nSelecciona el número del dispositivo (o 'q' para salir): ")
            if choice.lower() == 'q':
                return
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(all_devices):
                    connector.mac_address = all_devices[idx].address
            except ValueError:
                print("Selección inválida")
                return
    else:
        print("\nDispositivos BMS encontrados:")
        for i, dev in enumerate(devices):
            print(f"  {i+1}. {dev['name']} [{dev['address']}] RSSI: {dev['rssi']}")
        
        choice = input("\nSelecciona el número del dispositivo (o 'q' para salir): ")
        if choice.lower() == 'q':
            return
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(devices):
                connector.mac_address = devices[idx]['address']
        except ValueError:
            print("Selección inválida")
            return


def print_data(data: BMSData):
    """Callback para imprimir datos del BMS"""
    print(data)


async def main():
    parser = argparse.ArgumentParser(
        description='Conector BMS Bluetooth - Captura de datos de batería',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  python bms_connector.py --scan
  python bms_connector.py --mac XX:XX:XX:XX:XX:XX --discover
  python bms_connector.py --mac XX:XX:XX:XX:XX:XX --once
  python bms_connector.py --mac XX:XX:XX:XX:XX:XX --interval 10
        """
    )
    parser.add_argument(
        '--mac', '-m',
        help='Dirección MAC del dispositivo BMS (ej: XX:XX:XX:XX:XX:XX)'
    )
    parser.add_argument(
        '--scan', '-s',
        action='store_true',
        help='Solo escanear dispositivos sin conectar'
    )
    parser.add_argument(
        '--discover', '-d',
        action='store_true',
        help='Descubrir servicios BLE y salir'
    )
    parser.add_argument(
        '--interval', '-i',
        type=float,
        default=5.0,
        help='Intervalo de lectura en segundos (default: 5.0)'
    )
    parser.add_argument(
        '--once', '-o',
        action='store_true',
        help='Leer una sola vez y salir'
    )
    
    args = parser.parse_args()
    
    connector = BMSBluetoothConnector(mac_address=args.mac)
    
    # Modo escaneo
    if args.scan:
        await connector.scan_devices()
        return
    
    # Modo interactivo si no hay MAC
    if not args.mac:
        await interactive_mode(connector)
        if not connector.mac_address:
            return
    
    # Conectar
    if not await connector.connect():
        return
    
    try:
        # Modo descubrimiento
        if args.discover:
            await connector.discover_services()
            return
        
        # Leer datos
        if args.once:
            data = await connector.request_basic_info()
            if data:
                # Intentar obtener voltajes de celdas
                cells = await connector.request_cell_voltages()
                if cells:
                    data.cell_voltages = cells
                print(data)
        else:
            # Modo continuo
            await connector.read_voltage_continuous(interval=args.interval)
            
    except KeyboardInterrupt:
        print("\nInterrumpido por el usuario")
    finally:
        await connector.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
