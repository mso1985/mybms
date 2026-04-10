#!/usr/bin/env python3
"""
Daly BMS Client - Protocolo confirmado por captura btsnoop
Característica de escritura: fff2 o fff3 (Handle 19 o 23)
Característica de notificación: fff1 (Handle 15)
"""

import asyncio
import struct
from dataclasses import dataclass, field
from typing import Optional, List
from bleak import BleakClient, BleakScanner


@dataclass
class DalyBMSData:
    """Datos del BMS Daly"""
    voltage: float = 0.0          # Voltaje total en V
    current: float = 0.0          # Corriente en A (30000 = 0A, >30000 carga, <30000 descarga)
    soc: int = 0                  # State of Charge (%)
    max_cell_voltage: float = 0.0
    min_cell_voltage: float = 0.0
    max_cell_number: int = 0
    min_cell_number: int = 0
    temperature: float = 0.0
    cell_count: int = 0
    cell_voltages: List[float] = field(default_factory=list)
    cycles: int = 0
    capacity_remaining: float = 0.0
    capacity_full: float = 0.0


class DalyBMS:
    """Cliente para Daly BMS"""
    
    # UUIDs confirmados
    SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
    NOTIFY_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"
    WRITE_UUID = "0000fff2-0000-1000-8000-00805f9b34fb"
    
    # Comandos Daly BMS
    # Formato: A5 [addr] [cmd] 08 00 00 00 00 00 00 00 00 [checksum]
    # Checksum = suma de todos los bytes & 0xFF
    
    COMMANDS = {
        0x90: "SOC",           # Voltaje, corriente, SOC
        0x91: "MIN_MAX_VOLT",  # Voltaje min/max de celdas
        0x92: "MIN_MAX_TEMP",  # Temperatura min/max
        0x93: "CHARGE_DISCHARGE",  # Estado carga/descarga
        0x94: "STATUS",        # Estado del BMS
        0x95: "CELL_VOLTAGES", # Voltajes de celdas
        0x96: "CELL_TEMPS",    # Temperaturas
        0x97: "CELL_BALANCE",  # Estado de balanceo
        0x98: "FAILURE_CODES", # Códigos de falla
    }
    
    def __init__(self):
        self.client: Optional[BleakClient] = None
        self.data = DalyBMSData()
        self._response_buffer = bytearray()
        self._last_response = None
        self._response_event = asyncio.Event()
        
    def _calc_checksum(self, data: bytes) -> int:
        """Calcula checksum Daly (suma de bytes & 0xFF)"""
        return sum(data) & 0xFF
    
    def _build_command(self, cmd: int, address: int = 0x40) -> bytes:
        """Construye un comando Daly"""
        # Formato: A5 [addr] [cmd] 08 00 00 00 00 00 00 00 00 [checksum]
        packet = bytes([0xA5, address, cmd, 0x08, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
        checksum = self._calc_checksum(packet)
        return packet + bytes([checksum])
    
    async def scan(self, timeout: float = 10.0) -> List[dict]:
        """Escanea dispositivos BLE"""
        print(f"Escaneando ({timeout}s)...")
        devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
        
        results = []
        for addr, (device, adv) in devices.items():
            name = device.name or adv.local_name or ""
            rssi = adv.rssi if hasattr(adv, 'rssi') else -100
            results.append({"address": addr, "name": name, "rssi": rssi})
        
        results.sort(key=lambda x: -x["rssi"])
        
        print(f"\nDispositivos ({len(results)}):")
        for i, dev in enumerate(results, 1):
            print(f"  {i}. {dev['name'] or '[Sin nombre]'} [{dev['address']}] RSSI: {dev['rssi']}")
        
        return results
    
    def _notification_handler(self, sender, data: bytearray):
        """Maneja notificaciones del BMS"""
        print(f"\n[RX] {len(data)} bytes: {data.hex()}")
        self._response_buffer.extend(data)
        
        # Verificar si tenemos un paquete completo Daly (13 bytes típico)
        if len(self._response_buffer) >= 13:
            self._parse_response(bytes(self._response_buffer))
            self._response_buffer.clear()
            self._response_event.set()
    
    def _parse_response(self, data: bytes):
        """Parsea respuesta Daly"""
        if len(data) < 13 or data[0] != 0xA5:
            print(f"  Respuesta no válida o incompleta")
            return
        
        address = data[1]
        cmd = data[2]
        length = data[3]
        payload = data[4:4+length]
        checksum = data[4+length] if len(data) > 4+length else 0
        
        cmd_name = self.COMMANDS.get(cmd, f"UNKNOWN_{cmd:02X}")
        print(f"  Comando: {cmd_name} (0x{cmd:02X})")
        print(f"  Address: 0x{address:02X}")
        print(f"  Payload: {payload.hex()}")
        
        # Parsear según el comando
        if cmd == 0x90:  # SOC - Voltaje, corriente, SOC
            self._parse_soc(payload)
        elif cmd == 0x91:  # Min/Max voltages
            self._parse_min_max_volt(payload)
        elif cmd == 0x92:  # Min/Max temps
            self._parse_min_max_temp(payload)
        elif cmd == 0x95:  # Cell voltages
            self._parse_cell_voltages(payload)
    
    def _parse_soc(self, data: bytes):
        """Parsea comando 0x90 - SOC"""
        if len(data) < 8:
            return
        
        # Bytes 0-1: Voltaje (0.1V)
        voltage_raw = struct.unpack('>H', data[0:2])[0]
        self.data.voltage = voltage_raw / 10.0
        
        # Bytes 2-3: Acquisition voltage (ignorar por ahora)
        
        # Bytes 4-5: Corriente (offset 30000 = 0A, en 0.1A)
        current_raw = struct.unpack('>H', data[4:6])[0]
        self.data.current = (current_raw - 30000) / 10.0
        
        # Bytes 6-7: SOC (0.1%)
        soc_raw = struct.unpack('>H', data[6:8])[0]
        self.data.soc = soc_raw // 10
        
        print(f"\n  === DATOS SOC ===")
        print(f"  Voltaje: {self.data.voltage:.1f} V")
        print(f"  Corriente: {self.data.current:.1f} A")
        print(f"  SOC: {self.data.soc}%")
    
    def _parse_min_max_volt(self, data: bytes):
        """Parsea comando 0x91 - Min/Max voltajes"""
        if len(data) < 8:
            return
        
        # Bytes 0-1: Max cell voltage (mV)
        max_v = struct.unpack('>H', data[0:2])[0]
        self.data.max_cell_voltage = max_v / 1000.0
        
        # Byte 2: Max cell number
        self.data.max_cell_number = data[2]
        
        # Bytes 3-4: Min cell voltage (mV)
        min_v = struct.unpack('>H', data[3:5])[0]
        self.data.min_cell_voltage = min_v / 1000.0
        
        # Byte 5: Min cell number
        self.data.min_cell_number = data[5]
        
        print(f"\n  === VOLTAJES MIN/MAX ===")
        print(f"  Max: {self.data.max_cell_voltage:.3f}V (celda {self.data.max_cell_number})")
        print(f"  Min: {self.data.min_cell_voltage:.3f}V (celda {self.data.min_cell_number})")
        print(f"  Diferencia: {(self.data.max_cell_voltage - self.data.min_cell_voltage)*1000:.0f}mV")
    
    def _parse_min_max_temp(self, data: bytes):
        """Parsea comando 0x92 - Min/Max temperaturas"""
        if len(data) < 4:
            return
        
        # Byte 0: Max temp (offset -40°C)
        max_temp = data[0] - 40
        # Byte 2: Min temp (offset -40°C)  
        min_temp = data[2] - 40
        
        self.data.temperature = max_temp
        
        print(f"\n  === TEMPERATURAS ===")
        print(f"  Max: {max_temp}°C")
        print(f"  Min: {min_temp}°C")
    
    def _parse_cell_voltages(self, data: bytes):
        """Parsea comando 0x95 - Voltajes de celdas"""
        if len(data) < 3:
            return
        
        frame_num = data[0]
        print(f"\n  === VOLTAJES CELDAS (frame {frame_num}) ===")
        
        # Cada frame tiene hasta 3 voltajes de celda (2 bytes cada uno)
        for i in range(3):
            offset = 1 + i * 2
            if offset + 2 <= len(data):
                cell_mv = struct.unpack('>H', data[offset:offset+2])[0]
                if cell_mv > 0 and cell_mv < 5000:  # Validar rango
                    cell_num = (frame_num - 1) * 3 + i + 1
                    cell_v = cell_mv / 1000.0
                    print(f"  Celda {cell_num}: {cell_v:.3f}V")
    
    async def connect(self, address: str) -> bool:
        """Conecta al BMS"""
        print(f"\nConectando a {address}...")
        
        try:
            self.client = BleakClient(address)
            await self.client.connect()
            
            if not self.client.is_connected:
                print("Error: No conectado")
                return False
            
            print("Conectado!")
            
            # Suscribirse a notificaciones
            await self.client.start_notify(self.NOTIFY_UUID, self._notification_handler)
            print(f"Suscrito a notificaciones en {self.NOTIFY_UUID}")
            
            return True
            
        except Exception as e:
            print(f"Error: {e}")
            return False
    
    async def send_command(self, cmd: int, description: str = ""):
        """Envía un comando y espera respuesta"""
        packet = self._build_command(cmd)
        desc = description or self.COMMANDS.get(cmd, f"0x{cmd:02X}")
        
        print(f"\n[TX] {desc}: {packet.hex()}")
        
        self._response_event.clear()
        self._response_buffer.clear()
        
        try:
            await self.client.write_gatt_char(self.WRITE_UUID, packet, response=False)
            
            # Esperar respuesta
            try:
                await asyncio.wait_for(self._response_event.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                print("  (sin respuesta)")
                
        except Exception as e:
            print(f"  Error: {e}")
    
    async def read_all(self):
        """Lee todos los datos del BMS"""
        print("\n" + "=" * 50)
        print("LEYENDO DATOS DEL BMS DALY")
        print("=" * 50)
        
        await self.send_command(0x90, "SOC/Voltaje/Corriente")
        await asyncio.sleep(0.5)
        
        await self.send_command(0x91, "Min/Max Voltajes")
        await asyncio.sleep(0.5)
        
        await self.send_command(0x92, "Min/Max Temperaturas")
        await asyncio.sleep(0.5)
        
        await self.send_command(0x95, "Voltajes de Celdas")
        await asyncio.sleep(0.5)
        
        await self.send_command(0x93, "Estado Carga/Descarga")
        await asyncio.sleep(0.5)
        
        self._print_summary()
    
    def _print_summary(self):
        """Imprime resumen de datos"""
        print("\n" + "=" * 50)
        print("RESUMEN")
        print("=" * 50)
        print(f"Voltaje:     {self.data.voltage:.1f} V")
        print(f"Corriente:   {self.data.current:.1f} A")
        print(f"SOC:         {self.data.soc}%")
        print(f"Temperatura: {self.data.temperature}°C")
        if self.data.max_cell_voltage > 0:
            print(f"Celda Max:   {self.data.max_cell_voltage:.3f}V (#{self.data.max_cell_number})")
            print(f"Celda Min:   {self.data.min_cell_voltage:.3f}V (#{self.data.min_cell_number})")
    
    async def disconnect(self):
        """Desconecta"""
        if self.client and self.client.is_connected:
            try:
                await self.client.stop_notify(self.NOTIFY_UUID)
            except:
                pass
            await self.client.disconnect()
            print("\nDesconectado")
    
    async def interactive(self):
        """Modo interactivo"""
        print("\n=== MODO INTERACTIVO ===")
        print("Comandos:")
        print("  1 - Leer SOC (voltaje, corriente, %)")
        print("  2 - Leer Min/Max voltajes")
        print("  3 - Leer temperaturas")
        print("  4 - Leer voltajes de celdas")
        print("  5 - Leer TODO")
        print("  h XX - Enviar comando hex (ej: h 90)")
        print("  q - Salir")
        
        while True:
            try:
                cmd = input("\n> ").strip().lower()
                
                if cmd == 'q':
                    break
                elif cmd == '1':
                    await self.send_command(0x90)
                elif cmd == '2':
                    await self.send_command(0x91)
                elif cmd == '3':
                    await self.send_command(0x92)
                elif cmd == '4':
                    await self.send_command(0x95)
                elif cmd == '5':
                    await self.read_all()
                elif cmd.startswith('h '):
                    try:
                        cmd_byte = int(cmd[2:], 16)
                        await self.send_command(cmd_byte)
                    except ValueError:
                        print("Formato: h XX (ej: h 90)")
                else:
                    print("Comando no reconocido")
                    
            except KeyboardInterrupt:
                break
            except EOFError:
                break


async def main():
    bms = DalyBMS()
    
    devices = await bms.scan()
    
    if not devices:
        print("No se encontraron dispositivos")
        return
    
    print("\nSelecciona dispositivo (numero o MAC):")
    sel = input("> ").strip()
    
    try:
        idx = int(sel) - 1
        address = devices[idx]["address"]
    except:
        address = sel
    
    if await bms.connect(address):
        try:
            await bms.interactive()
        finally:
            await bms.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
