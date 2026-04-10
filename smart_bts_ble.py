#!/usr/bin/env python3
"""
Smart BTS Battery BLE Client
Protocolo reverse-engineered desde btsnoop capture de la app Smart BTS

Basado en el analisis del archivo snoop.log:
- Los comandos se envian al handle 0x0041 (caracteristica de escritura)
- Las respuestas llegan via notificaciones en handle 0x0042
- El servicio principal parece usar UUIDs custom basados en 0xFFF0/0xFFE0
"""

import asyncio
import struct
from dataclasses import dataclass
from typing import Optional, Callable, List
from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic


# UUIDs comunes para BMS de baterias de litio (basados en el analisis)
# Estos son los UUIDs tipicos usados por muchos BMS chinos
SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
CHAR_WRITE_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"  # Para enviar comandos
CHAR_NOTIFY_UUID = "0000fff2-0000-1000-8000-00805f9b34fb"  # Para recibir datos

# UUIDs alternativos (algunos BMS usan estos)
ALT_SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
ALT_CHAR_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"


@dataclass
class BatteryData:
    """Datos de la bateria de litio"""
    voltage: float = 0.0  # Voltaje total en V
    current: float = 0.0  # Corriente en A (positivo = carga, negativo = descarga)
    soc: int = 0  # State of Charge (%)
    capacity_remaining: float = 0.0  # Capacidad restante en Ah
    capacity_full: float = 0.0  # Capacidad total en Ah
    cycles: int = 0  # Ciclos de carga
    temperature: float = 0.0  # Temperatura en C
    cell_count: int = 0  # Numero de celdas
    cell_voltages: List[float] = None  # Voltaje de cada celda
    
    def __post_init__(self):
        if self.cell_voltages is None:
            self.cell_voltages = []


class SmartBTSClient:
    """Cliente BLE para baterias Smart BTS"""
    
    def __init__(self):
        self.client: Optional[BleakClient] = None
        self.battery_data = BatteryData()
        self.data_callback: Optional[Callable] = None
        self._response_buffer = bytearray()
        self._write_char = None
        self._notify_char = None
        
    async def scan(self, timeout: float = 10.0) -> List[dict]:
        """Escanea dispositivos BLE y filtra los que parecen ser BMS"""
        print(f"Escaneando dispositivos BLE por {timeout} segundos...")
        
        # Usar return_adv=True para obtener RSSI en versiones nuevas de bleak
        devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
        
        bms_devices = []
        for address, (device, adv_data) in devices.items():
            # Filtrar por nombre comun de BMS o por servicios
            name = device.name or adv_data.local_name or ""
            rssi = adv_data.rssi if hasattr(adv_data, 'rssi') else -100
            
            if any(keyword in name.lower() for keyword in 
                   ["bms", "battery", "smart", "jbd", "daly", "ant", "jk", "xiaoxiang"]):
                bms_devices.append({
                    "address": device.address,
                    "name": name,
                    "rssi": rssi
                })
            # Tambien incluir dispositivos sin nombre que podrian ser BMS
            elif not name and rssi > -80:
                bms_devices.append({
                    "address": device.address,
                    "name": f"Unknown ({device.address})",
                    "rssi": rssi
                })
                
        # Ordenar por RSSI (mas fuerte primero)
        bms_devices.sort(key=lambda x: x["rssi"], reverse=True)
        
        print(f"Encontrados {len(bms_devices)} posibles dispositivos BMS:")
        for i, dev in enumerate(bms_devices):
            print(f"  {i+1}. {dev['name']} [{dev['address']}] RSSI: {dev['rssi']}")
            
        return bms_devices
    
    async def connect(self, address: str) -> bool:
        """Conecta al dispositivo BLE"""
        print(f"Conectando a {address}...")
        
        try:
            self.client = BleakClient(address)
            await self.client.connect()
            
            if not self.client.is_connected:
                print("Error: No se pudo conectar")
                return False
                
            print("Conectado! Descubriendo servicios...")
            
            # Listar todos los servicios y caracteristicas
            for service in self.client.services:
                print(f"\nServicio: {service.uuid}")
                for char in service.characteristics:
                    props = ", ".join(char.properties)
                    print(f"  Caracteristica: {char.uuid} [{props}]")
                    
                    # Buscar caracteristica de escritura
                    if "write" in char.properties or "write-without-response" in char.properties:
                        self._write_char = char
                        print(f"    -> Caracteristica de escritura encontrada!")
                        
                    # Buscar caracteristica de notificacion
                    if "notify" in char.properties:
                        self._notify_char = char
                        print(f"    -> Caracteristica de notificacion encontrada!")
            
            # Suscribirse a notificaciones si encontramos la caracteristica
            if self._notify_char:
                await self.client.start_notify(
                    self._notify_char.uuid, 
                    self._notification_handler
                )
                print(f"\nSuscrito a notificaciones en {self._notify_char.uuid}")
                
            return True
            
        except Exception as e:
            print(f"Error de conexion: {e}")
            return False
    
    def _notification_handler(self, sender: BleakGATTCharacteristic, data: bytearray):
        """Maneja las notificaciones recibidas del BMS"""
        print(f"\n[NOTIFY] Recibido {len(data)} bytes: {data.hex()}")
        
        self._response_buffer.extend(data)
        
        # Intentar parsear los datos
        self._parse_response(self._response_buffer)
        
        if self.data_callback:
            self.data_callback(self.battery_data)
    
    def _parse_response(self, data: bytearray):
        """Parsea la respuesta del BMS
        
        Basado en el analisis del btsnoop, el protocolo parece ser similar a JBD/Xiaoxiang:
        - Byte 0: Start byte (0xDD)
        - Byte 1: Command response
        - Byte 2: Status (0x00 = OK)
        - Byte 3: Data length
        - Bytes 4-N: Data
        - Last 2 bytes: Checksum
        - Last byte: End byte (0x77)
        """
        if len(data) < 7:
            return
            
        # Intentar detectar el formato del protocolo
        # Formato JBD/Xiaoxiang
        if data[0] == 0xDD:
            self._parse_jbd_response(data)
        # Formato Daly
        elif data[0] == 0xA5:
            self._parse_daly_response(data)
        # Formato generico basado en el btsnoop
        else:
            self._parse_generic_response(data)
    
    def _parse_jbd_response(self, data: bytearray):
        """Parsea respuesta formato JBD/Xiaoxiang BMS"""
        if len(data) < 7:
            return
            
        cmd = data[1]
        status = data[2]
        length = data[3]
        
        if status != 0x00:
            print(f"Error en respuesta: status={status:#x}")
            return
            
        payload = data[4:4+length]
        
        # Comando 0x03: Info basica
        if cmd == 0x03 and len(payload) >= 23:
            self.battery_data.voltage = struct.unpack('>H', payload[0:2])[0] / 100.0
            self.battery_data.current = struct.unpack('>h', payload[2:4])[0] / 100.0
            self.battery_data.capacity_remaining = struct.unpack('>H', payload[4:6])[0] / 100.0
            self.battery_data.capacity_full = struct.unpack('>H', payload[6:8])[0] / 100.0
            self.battery_data.cycles = struct.unpack('>H', payload[8:10])[0]
            # Mas campos...
            self.battery_data.soc = payload[19]
            
            print(f"\n=== Datos de Bateria ===")
            print(f"Voltaje: {self.battery_data.voltage:.2f} V")
            print(f"Corriente: {self.battery_data.current:.2f} A")
            print(f"SOC: {self.battery_data.soc}%")
            print(f"Capacidad: {self.battery_data.capacity_remaining:.2f}/{self.battery_data.capacity_full:.2f} Ah")
            print(f"Ciclos: {self.battery_data.cycles}")
            
        # Comando 0x04: Voltajes de celdas
        elif cmd == 0x04:
            cell_count = len(payload) // 2
            self.battery_data.cell_count = cell_count
            self.battery_data.cell_voltages = []
            
            print(f"\n=== Voltajes de Celdas ({cell_count} celdas) ===")
            for i in range(cell_count):
                voltage = struct.unpack('>H', payload[i*2:i*2+2])[0] / 1000.0
                self.battery_data.cell_voltages.append(voltage)
                print(f"Celda {i+1}: {voltage:.3f} V")
    
    def _parse_daly_response(self, data: bytearray):
        """Parsea respuesta formato Daly BMS"""
        # Implementar segun el protocolo Daly
        pass
    
    def _parse_generic_response(self, data: bytearray):
        """Parsea respuesta generica basada en el btsnoop analizado"""
        print(f"Respuesta generica: {data.hex()}")
        
        # Buscar patrones de datos de bateria
        # Los valores de voltaje tipicamente estan en el rango 0x0C00-0x1000 (3.0-4.2V por celda)
        # Los valores de corriente pueden ser signed
        
        # Intentar extraer datos basicos si encontramos patrones conocidos
        if len(data) >= 20:
            # Buscar posibles valores de voltaje (12-60V tipico para packs)
            for i in range(len(data) - 1):
                val = struct.unpack('>H', data[i:i+2])[0]
                voltage = val / 100.0
                if 10.0 < voltage < 100.0:
                    print(f"  Posible voltaje en offset {i}: {voltage:.2f} V")
    
    async def send_command(self, command: bytes) -> bool:
        """Envia un comando al BMS"""
        if not self.client or not self.client.is_connected:
            print("Error: No conectado")
            return False
            
        if not self._write_char:
            print("Error: No se encontro caracteristica de escritura")
            return False
            
        print(f"[SEND] Enviando {len(command)} bytes: {command.hex()}")
        
        try:
            # Limpiar buffer de respuesta
            self._response_buffer.clear()
            
            if "write-without-response" in self._write_char.properties:
                await self.client.write_gatt_char(
                    self._write_char.uuid, 
                    command, 
                    response=False
                )
            else:
                await self.client.write_gatt_char(
                    self._write_char.uuid, 
                    command, 
                    response=True
                )
            return True
            
        except Exception as e:
            print(f"Error enviando comando: {e}")
            return False
    
    async def request_basic_info(self):
        """Solicita informacion basica del BMS (formato JBD)"""
        # Comando JBD para info basica: DD A5 03 00 FF FD 77
        cmd = bytes([0xDD, 0xA5, 0x03, 0x00, 0xFF, 0xFD, 0x77])
        await self.send_command(cmd)
        await asyncio.sleep(0.5)  # Esperar respuesta
        
    async def request_cell_voltages(self):
        """Solicita voltajes de celdas (formato JBD)"""
        # Comando JBD para voltajes: DD A5 04 00 FF FC 77
        cmd = bytes([0xDD, 0xA5, 0x04, 0x00, 0xFF, 0xFC, 0x77])
        await self.send_command(cmd)
        await asyncio.sleep(0.5)
        
    async def request_all_data(self):
        """Solicita todos los datos del BMS"""
        await self.request_basic_info()
        await self.request_cell_voltages()
    
    async def disconnect(self):
        """Desconecta del dispositivo"""
        if self.client and self.client.is_connected:
            if self._notify_char:
                try:
                    await self.client.stop_notify(self._notify_char.uuid)
                except:
                    pass
            await self.client.disconnect()
            print("Desconectado")


async def main():
    """Funcion principal"""
    client = SmartBTSClient()
    
    # Escanear dispositivos
    devices = await client.scan(timeout=10.0)
    
    if not devices:
        print("\nNo se encontraron dispositivos BMS")
        print("Asegurate de que:")
        print("  1. El Bluetooth esta activado")
        print("  2. El BMS esta encendido y en rango")
        print("  3. Tienes permisos de Bluetooth (ejecutar con sudo en Linux)")
        return
    
    # Seleccionar dispositivo
    print("\nSelecciona un dispositivo (numero) o 'q' para salir:")
    selection = input("> ").strip()
    
    if selection.lower() == 'q':
        return
        
    try:
        idx = int(selection) - 1
        if 0 <= idx < len(devices):
            address = devices[idx]["address"]
        else:
            print("Seleccion invalida")
            return
    except ValueError:
        # Usar el input como direccion MAC directa
        address = selection
    
    # Conectar
    if await client.connect(address):
        print("\nEsperando 2 segundos para estabilizar conexion...")
        await asyncio.sleep(2)
        
        # Intentar comandos comunes de BMS
        print("\n--- Probando comandos JBD/Xiaoxiang ---")
        await client.request_all_data()
        
        # Mantener conexion para recibir mas datos
        print("\nPresiona Ctrl+C para desconectar...")
        try:
            while True:
                await asyncio.sleep(5)
                await client.request_basic_info()
        except KeyboardInterrupt:
            pass
        
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
