#!/usr/bin/env python3
"""
Smart BTS Battery Protocol - Basado en el analisis del btsnoop capturado

Protocolo reverse-engineered:
- Handle de escritura: 0x0041 (65 decimal)
- Handle de notificacion: 0x0042 (66 decimal)
- El servicio GATT usa UUIDs custom (posiblemente 0xFFF0 o 0xFFE0)

Comandos detectados:
- 01 00 01 01 12 00 12 00 - Solicitar datos basicos
- 01 00 05 01 12 00 12 00 01 12 00 12 00 - Solicitar datos extendidos
- 01 00 01 01 99 19 99 19 - Comando de configuracion (timestamp?)
- 01 00 01 01 90 04 24 01 - Solicitar datos de voltaje/corriente
"""

import asyncio
import struct
from dataclasses import dataclass, field
from typing import Optional, List, Callable
from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic


@dataclass
class SmartBTSData:
    """Datos parseados del BMS Smart BTS"""
    voltage: float = 0.0           # Voltaje total en V
    current: float = 0.0           # Corriente en A
    power: float = 0.0             # Potencia en W
    soc: int = 0                   # State of Charge (%)
    capacity_remaining: float = 0.0 # Ah
    capacity_full: float = 0.0      # Ah
    temperature: float = 0.0        # Temperatura en C
    cycles: int = 0                 # Ciclos de carga
    cell_count: int = 0
    cell_voltages: List[float] = field(default_factory=list)
    raw_data: bytes = b''          # Datos crudos para debug


# Comandos del protocolo JBD/Xiaoxiang (usado por muchos BMS con servicio fff0)
class SmartBTSCommands:
    """Comandos del protocolo Smart BTS / JBD / Xiaoxiang"""
    
    # Protocolo JBD: DD A5 [cmd] 00 FF [checksum] 77
    # Checksum = 0xFF - cmd
    
    # Comando 0x03: Leer info basica (voltaje, corriente, SOC, etc)
    READ_BASIC = bytes([0xDD, 0xA5, 0x03, 0x00, 0xFF, 0xFD, 0x77])
    
    # Comando 0x04: Leer voltajes de celdas
    READ_CELLS = bytes([0xDD, 0xA5, 0x04, 0x00, 0xFF, 0xFC, 0x77])
    
    # Comando 0x05: Leer info del hardware
    READ_HARDWARE = bytes([0xDD, 0xA5, 0x05, 0x00, 0xFF, 0xFB, 0x77])
    
    # Comandos originales del btsnoop (por si el BMS usa protocolo diferente)
    BTSNOOP_BASIC = bytes([0x01, 0x00, 0x01, 0x01, 0x12, 0x00, 0x12, 0x00])
    BTSNOOP_EXTENDED = bytes([0x01, 0x00, 0x05, 0x01, 0x12, 0x00, 0x12, 0x00, 
                              0x01, 0x12, 0x00, 0x12, 0x00])
    BTSNOOP_SYNC = bytes([0x01, 0x00, 0x01, 0x01, 0x99, 0x19, 0x99, 0x19])
    BTSNOOP_VOLTAGE = bytes([0x01, 0x00, 0x01, 0x01, 0x90, 0x04, 0x24, 0x01])


class SmartBTSClient:
    """Cliente BLE para baterias Smart BTS"""
    
    # UUIDs comunes de BMS
    SERVICE_UUIDS = [
        "0000fff0-0000-1000-8000-00805f9b34fb",
        "0000ffe0-0000-1000-8000-00805f9b34fb", 
        "0000ff00-0000-1000-8000-00805f9b34fb",
    ]
    
    def __init__(self):
        self.client: Optional[BleakClient] = None
        self.data = SmartBTSData()
        self._write_char = None
        self._notify_char = None
        self._write_handle = None
        self._notify_handle = None
        self._response_buffer = bytearray()
        self._on_data_callback: Optional[Callable] = None
        
    async def scan(self, timeout: float = 10.0, name_filter: str = None) -> List[dict]:
        """Escanea dispositivos BLE buscando BMS"""
        print(f"Escaneando dispositivos BLE ({timeout}s)...")
        
        # Usar return_adv=True para obtener RSSI en versiones nuevas de bleak
        devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
        results = []
        
        for address, (device, adv_data) in devices.items():
            name = device.name or adv_data.local_name or ""
            
            # Obtener RSSI del advertisement data
            rssi = adv_data.rssi if hasattr(adv_data, 'rssi') else -100
            
            # Aplicar filtro de nombre si se especifica
            if name_filter:
                if name_filter.lower() not in name.lower():
                    continue
            
            # Keywords comunes de BMS
            keywords = ["bms", "battery", "smart", "jbd", "daly", "ant", "jk", 
                       "xiaoxiang", "lithium", "lipo", "lifepo"]
            
            is_bms = any(kw in name.lower() for kw in keywords)
            
            results.append({
                "address": device.address,
                "name": name or f"[Unknown: {device.address}]",
                "rssi": rssi,
                "likely_bms": is_bms
            })
        
        # Ordenar: BMS probables primero, luego por RSSI
        results.sort(key=lambda x: (not x["likely_bms"], -x["rssi"]))
        
        print(f"\nEncontrados {len(results)} dispositivos:")
        for i, dev in enumerate(results, 1):
            bms_tag = " [BMS?]" if dev["likely_bms"] else ""
            print(f"  {i}. {dev['name']}{bms_tag} [{dev['address']}] RSSI: {dev['rssi']}")
        
        return results
    
    async def connect(self, address: str, pin: str = None) -> bool:
        """Conecta al BMS"""
        print(f"\nConectando a {address}...")
        
        try:
            self.client = BleakClient(address)
            await self.client.connect()
            
            if not self.client.is_connected:
                print("Error: No se pudo establecer conexion")
                return False
            
            print("Conectado! Descubriendo servicios...")
            await self._discover_characteristics()
            
            # Si hay PIN, intentar autenticacion
            if pin:
                print(f"\nEnviando PIN de autenticacion: {pin}")
                await self._send_pin(pin)
                await asyncio.sleep(0.5)
            
            if self._notify_char:
                await self.client.start_notify(
                    self._notify_char.uuid,
                    self._handle_notification
                )
                print(f"Suscrito a notificaciones en {self._notify_char.uuid}")
            
            # Tambien suscribirse a otras caracteristicas con notify
            for service in self.client.services:
                for char in service.characteristics:
                    if "notify" in char.properties and char != self._notify_char:
                        try:
                            await self.client.start_notify(char.uuid, self._handle_notification)
                            print(f"Suscrito tambien a {char.uuid}")
                        except Exception as e:
                            print(f"No se pudo suscribir a {char.uuid}: {e}")
            
            return True
            
        except Exception as e:
            print(f"Error de conexion: {e}")
            return False
    
    async def _send_pin(self, pin: str):
        """Envia el PIN de autenticacion al BMS"""
        # Metodo 1: PIN como bytes ASCII
        pin_bytes = pin.encode('ascii')
        print(f"  Enviando PIN como ASCII: {pin_bytes.hex()}")
        await self.send_command(pin_bytes, "PIN_ASCII")
        await asyncio.sleep(0.3)
        
        # Metodo 2: PIN como bytes numericos
        try:
            pin_numeric = bytes([int(pin)])
            print(f"  Enviando PIN como numero: {pin_numeric.hex()}")
            await self.send_command(pin_numeric, "PIN_NUMERIC")
            await asyncio.sleep(0.3)
        except:
            pass
        
        # Metodo 3: PIN con prefijo comun de BMS
        # Algunos BMS usan formato: 0x00 + PIN
        pin_with_prefix = bytes([0x00]) + pin_bytes
        print(f"  Enviando PIN con prefijo: {pin_with_prefix.hex()}")
        await self.send_command(pin_with_prefix, "PIN_PREFIX")
        await asyncio.sleep(0.3)
    
    async def _discover_characteristics(self):
        """Descubre y muestra las caracteristicas GATT"""
        print("\nServicios y caracteristicas:")
        print("-" * 50)
        
        # UUIDs conocidos del BMS Smart BTS (servicio 0000fff0)
        TARGET_SERVICE = "0000fff0-0000-1000-8000-00805f9b34fb"
        TARGET_WRITE_CHAR = "0000fff2-0000-1000-8000-00805f9b34fb"  # Handle 19
        TARGET_NOTIFY_CHAR = "0000fff1-0000-1000-8000-00805f9b34fb"  # Handle 15
        
        for service in self.client.services:
            print(f"\n[Servicio] {service.uuid}")
            
            for char in service.characteristics:
                props = ", ".join(char.properties)
                print(f"  [Char] {char.uuid}")
                print(f"         Handle: {char.handle} ({hex(char.handle)})")
                print(f"         Props: {props}")
                
                # Buscar especificamente las caracteristicas del servicio fff0
                if char.uuid.lower() == TARGET_WRITE_CHAR:
                    self._write_char = char
                    self._write_handle = char.handle
                    print(f"         --> ESCRITURA (fff2)")
                
                if char.uuid.lower() == TARGET_NOTIFY_CHAR:
                    self._notify_char = char
                    self._notify_handle = char.handle
                    print(f"         --> NOTIFICACION (fff1)")
        
        # Si no encontramos las caracteristicas especificas, usar fallback
        if not self._write_char or not self._notify_char:
            print("\nNo se encontraron caracteristicas fff1/fff2, buscando alternativas...")
            for service in self.client.services:
                for char in service.characteristics:
                    if not self._write_char and ("write" in char.properties or "write-without-response" in char.properties):
                        if "notify" not in char.properties:  # Preferir char solo de escritura
                            self._write_char = char
                            self._write_handle = char.handle
                    
                    if not self._notify_char and "notify" in char.properties:
                        self._notify_char = char
                        self._notify_handle = char.handle
        
        print("-" * 50)
        if self._write_char:
            print(f"Caracteristica de escritura: {self._write_char.uuid} (handle {self._write_handle})")
        else:
            print("ADVERTENCIA: No se encontro caracteristica de escritura")
            
        if self._notify_char:
            print(f"Caracteristica de notificacion: {self._notify_char.uuid} (handle {self._notify_handle})")
        else:
            print("ADVERTENCIA: No se encontro caracteristica de notificacion")
    
    def _handle_notification(self, sender: BleakGATTCharacteristic, data: bytearray):
        """Procesa notificaciones del BMS"""
        print(f"\n[RX] {len(data)} bytes: {data.hex()}")
        
        self._response_buffer.extend(data)
        self.data.raw_data = bytes(self._response_buffer)
        
        # Intentar parsear
        self._parse_response(self._response_buffer)
        
        if self._on_data_callback:
            self._on_data_callback(self.data)
    
    def _parse_response(self, data: bytearray):
        """Parsea la respuesta del BMS"""
        if len(data) < 4:
            return
        
        print(f"  Parseando {len(data)} bytes...")
        
        # El protocolo Smart BTS parece usar un formato propietario
        # Intentar detectar el formato basado en los datos capturados
        
        # Buscar voltajes (tipicamente en formato big-endian, 2 bytes)
        # Valores tipicos: 3000-4200 mV por celda
        found_voltages = []
        for i in range(0, len(data) - 1, 2):
            val = struct.unpack('>H', data[i:i+2])[0]
            if 2800 < val < 4300:  # Rango de celda Li-ion
                found_voltages.append((i, val))
        
        if found_voltages:
            print(f"  Posibles voltajes de celda encontrados:")
            for offset, mv in found_voltages[:8]:  # Max 8
                print(f"    Offset {offset}: {mv}mV ({mv/1000:.3f}V)")
            
            # Actualizar datos si encontramos voltajes validos
            if len(found_voltages) >= 4:  # Al menos 4 celdas
                self.data.cell_voltages = [mv/1000.0 for _, mv in found_voltages[:16]]
                self.data.cell_count = len(self.data.cell_voltages)
                self.data.voltage = sum(self.data.cell_voltages)
        
        # Buscar voltaje total del pack (tipicamente 10-60V)
        for i in range(0, len(data) - 1, 2):
            val = struct.unpack('>H', data[i:i+2])[0]
            pack_v = val / 100.0
            if 10.0 < pack_v < 80.0:
                print(f"  Posible voltaje de pack en offset {i}: {pack_v:.2f}V")
        
        # Buscar corriente (signed, puede ser negativo durante descarga)
        for i in range(0, len(data) - 1, 2):
            val = struct.unpack('>h', data[i:i+2])[0]  # signed
            current = val / 100.0
            if -100.0 < current < 100.0 and current != 0:
                print(f"  Posible corriente en offset {i}: {current:.2f}A")
    
    async def send_command(self, command: bytes, description: str = "") -> bool:
        """Envia un comando al BMS"""
        if not self.client or not self.client.is_connected:
            print("Error: No conectado")
            return False
        
        if not self._write_char:
            print("Error: No hay caracteristica de escritura")
            return False
        
        desc = f" ({description})" if description else ""
        print(f"\n[TX{desc}] {len(command)} bytes: {command.hex()}")
        
        try:
            # Limpiar buffer
            self._response_buffer.clear()
            
            # Determinar si usar write con o sin respuesta
            if "write-without-response" in self._write_char.properties:
                await self.client.write_gatt_char(
                    self._write_char.uuid, command, response=False
                )
            else:
                await self.client.write_gatt_char(
                    self._write_char.uuid, command, response=True
                )
            
            # Esperar respuesta
            await asyncio.sleep(0.5)
            return True
            
        except Exception as e:
            print(f"Error enviando comando: {e}")
            return False
    
    async def read_basic_info(self):
        """Lee informacion basica del BMS (protocolo JBD)"""
        await self.send_command(SmartBTSCommands.READ_BASIC, "JBD_BASIC (0x03)")
        
    async def read_cell_voltages(self):
        """Lee voltajes de celdas (protocolo JBD)"""
        await self.send_command(SmartBTSCommands.READ_CELLS, "JBD_CELLS (0x04)")
    
    async def read_hardware_info(self):
        """Lee info del hardware (protocolo JBD)"""
        await self.send_command(SmartBTSCommands.READ_HARDWARE, "JBD_HARDWARE (0x05)")
        
    async def read_btsnoop_basic(self):
        """Comando original del btsnoop"""
        await self.send_command(SmartBTSCommands.BTSNOOP_BASIC, "BTSNOOP_BASIC")
        
    async def read_btsnoop_voltage(self):
        """Comando de voltaje del btsnoop"""
        await self.send_command(SmartBTSCommands.BTSNOOP_VOLTAGE, "BTSNOOP_VOLTAGE")
    
    async def read_all_data(self):
        """Lee todos los datos disponibles"""
        print("\n=== Leyendo datos del BMS (protocolo JBD) ===")
        
        await self.read_basic_info()
        await asyncio.sleep(0.5)
        
        await self.read_cell_voltages()
        await asyncio.sleep(0.5)
        
        await self.read_hardware_info()
        await asyncio.sleep(0.5)
        
        self._print_summary()
    
    async def try_all_protocols(self):
        """Prueba diferentes protocolos para ver cual responde"""
        print("\n=== Probando diferentes protocolos ===\n")
        
        print("1. Protocolo JBD/Xiaoxiang...")
        await self.send_command(SmartBTSCommands.READ_BASIC, "JBD cmd 0x03")
        await asyncio.sleep(1.0)
        
        print("\n2. Protocolo btsnoop capturado...")
        await self.send_command(SmartBTSCommands.BTSNOOP_BASIC, "BTSNOOP basic")
        await asyncio.sleep(1.0)
        
        print("\n3. Protocolo btsnoop voltage...")
        await self.send_command(SmartBTSCommands.BTSNOOP_VOLTAGE, "BTSNOOP voltage")
        await asyncio.sleep(1.0)
        
        self._print_summary()
    
    def _print_summary(self):
        """Imprime resumen de los datos"""
        print("\n" + "=" * 50)
        print("RESUMEN DE DATOS DE BATERIA")
        print("=" * 50)
        
        if self.data.voltage > 0:
            print(f"Voltaje total: {self.data.voltage:.2f}V")
        if self.data.current != 0:
            print(f"Corriente: {self.data.current:.2f}A")
        if self.data.soc > 0:
            print(f"SOC: {self.data.soc}%")
        if self.data.cell_count > 0:
            print(f"\nVoltajes de celdas ({self.data.cell_count} celdas):")
            for i, v in enumerate(self.data.cell_voltages, 1):
                print(f"  Celda {i}: {v:.3f}V")
        
        if self.data.raw_data:
            print(f"\nDatos crudos ({len(self.data.raw_data)} bytes):")
            # Mostrar en formato hex dump
            for i in range(0, len(self.data.raw_data), 16):
                chunk = self.data.raw_data[i:i+16]
                hex_str = ' '.join(f'{b:02x}' for b in chunk)
                ascii_str = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
                print(f"  {i:04x}: {hex_str:<48} {ascii_str}")
    
    async def disconnect(self):
        """Desconecta del BMS"""
        if self.client and self.client.is_connected:
            if self._notify_char:
                try:
                    await self.client.stop_notify(self._notify_char.uuid)
                except:
                    pass
            await self.client.disconnect()
            print("\nDesconectado")
    
    async def interactive_mode(self):
        """Modo interactivo para probar comandos"""
        print("\n=== MODO INTERACTIVO ===")
        print("Comandos disponibles:")
        print("  1 - JBD: Leer info basica (DD A5 03...)")
        print("  2 - JBD: Leer voltajes celdas (DD A5 04...)")
        print("  3 - JBD: Leer hardware (DD A5 05...)")
        print("  4 - BTSNOOP: Comando basico capturado")
        print("  5 - BTSNOOP: Comando voltage capturado")
        print("  6 - Probar TODOS los protocolos")
        print("  7 - Leer todo (JBD)")
        print("  h XX XX XX - Enviar comando hex personalizado")
        print("  r - Leer caracteristicas (debug)")
        print("  q - Salir")
        print()
        
        while True:
            try:
                cmd = input("> ").strip().lower()
                
                if cmd == 'q':
                    break
                elif cmd == '1':
                    await self.read_basic_info()
                elif cmd == '2':
                    await self.read_cell_voltages()
                elif cmd == '3':
                    await self.read_hardware_info()
                elif cmd == '4':
                    await self.read_btsnoop_basic()
                elif cmd == '5':
                    await self.read_btsnoop_voltage()
                elif cmd == '6':
                    await self.try_all_protocols()
                elif cmd == '7':
                    await self.read_all_data()
                elif cmd.startswith('h '):
                    # Comando hex personalizado
                    try:
                        hex_str = cmd[2:].replace(' ', '')
                        custom_cmd = bytes.fromhex(hex_str)
                        await self.send_command(custom_cmd, "CUSTOM")
                    except ValueError:
                        print("Error: Formato hex invalido")
                elif cmd == 'r':
                    # Debug: leer caracteristicas
                    print("\nIntentando leer caracteristicas...")
                    for service in self.client.services:
                        for char in service.characteristics:
                            if "read" in char.properties:
                                try:
                                    value = await self.client.read_gatt_char(char.uuid)
                                    print(f"  {char.uuid}: {value.hex()}")
                                except Exception as e:
                                    print(f"  {char.uuid}: Error - {e}")
                else:
                    print("Comando no reconocido. Usa 'h' para ver opciones.")
                    
            except KeyboardInterrupt:
                break
            except EOFError:
                break


async def main():
    """Funcion principal"""
    client = SmartBTSClient()
    
    print("=" * 50)
    print("SMART BTS BATTERY CLIENT")
    print("=" * 50)
    
    # Escanear
    devices = await client.scan(timeout=10.0)
    
    if not devices:
        print("\nNo se encontraron dispositivos")
        print("Verifica que:")
        print("  - Bluetooth esta activado")
        print("  - El BMS esta encendido")
        print("  - Tienes permisos (sudo en Linux)")
        return
    
    # Seleccionar dispositivo
    print("\nSelecciona dispositivo (numero), MAC directa, o 'q' para salir:")
    selection = input("> ").strip()
    
    if selection.lower() == 'q':
        return
    
    try:
        idx = int(selection) - 1
        if 0 <= idx < len(devices):
            address = devices[idx]["address"]
        else:
            print("Indice invalido")
            return
    except ValueError:
        # Asumir que es una MAC directa
        address = selection
    
    # Preguntar por PIN
    print("\nIngresa el PIN del BMS (Enter para omitir, default 123456):")
    pin_input = input("> ").strip()
    pin = pin_input if pin_input else "123456"
    
    # Conectar
    if not await client.connect(address, pin=pin):
        return
    
    try:
        # Modo interactivo
        await client.interactive_mode()
        
    except KeyboardInterrupt:
        print("\nInterrumpido")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
