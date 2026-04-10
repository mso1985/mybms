#!/usr/bin/env python3
"""
Daly BMS Client con MQTT
Lee datos del BMS via BLE y los publica en MQTT
"""

import asyncio
import struct
import json
import signal
import sys
from dataclasses import dataclass, field, asdict
from typing import Optional, List
from datetime import datetime

from bleak import BleakClient, BleakScanner
import paho.mqtt.client as mqtt


# ============================================================================
# CONFIGURACION - Modificar según tu setup
# ============================================================================

MQTT_CONFIG = {
    "broker": "192.168.1.107",
    "port": 1883,
    "username": "testuser",
    "password": "mariano",
    "topic_base": "bms/daly",          # Topic base para publicar
    "client_id": "daly_bms_client",
}

BMS_CONFIG = {
    "address": "41:19:09:01:50:D4",  # MAC del BMS
    "poll_interval": 10,  # Segundos entre lecturas
}

# ============================================================================


@dataclass
class DalyBMSData:
    """Datos del BMS Daly"""
    voltage: float = 0.0
    current: float = 0.0
    power: float = 0.0
    soc: int = 0
    max_cell_voltage: float = 0.0
    min_cell_voltage: float = 0.0
    cell_voltage_diff: float = 0.0
    max_cell_number: int = 0
    min_cell_number: int = 0
    temperature_max: float = 0.0
    temperature_min: float = 0.0
    cell_count: int = 0
    cell_voltages: List[float] = field(default_factory=list)
    timestamp: str = ""
    
    def to_dict(self) -> dict:
        """Convierte a diccionario para JSON"""
        return {
            "voltage": round(self.voltage, 1),
            "current": round(self.current, 1),
            "power": round(self.power, 1),
            "soc": self.soc,
            "max_cell_voltage": round(self.max_cell_voltage, 3),
            "min_cell_voltage": round(self.min_cell_voltage, 3),
            "cell_voltage_diff_mv": round(self.cell_voltage_diff * 1000, 0),
            "max_cell_number": self.max_cell_number,
            "min_cell_number": self.min_cell_number,
            "temperature_max": self.temperature_max,
            "temperature_min": self.temperature_min,
            "cell_count": self.cell_count,
            "cell_voltages": [round(v, 3) for v in self.cell_voltages],
            "timestamp": self.timestamp,
        }


class DalyBMSMQTT:
    """Cliente Daly BMS con publicación MQTT"""
    
    SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
    NOTIFY_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"
    WRITE_UUID = "0000fff2-0000-1000-8000-00805f9b34fb"
    
    COMMANDS = {
        0x90: "SOC",
        0x91: "MIN_MAX_VOLT",
        0x92: "MIN_MAX_TEMP",
        0x93: "CHARGE_DISCHARGE",
        0x94: "STATUS",
        0x95: "CELL_VOLTAGES",
        0x96: "CELL_TEMPS",
        0x97: "CELL_BALANCE",
        0x98: "FAILURE_CODES",
    }
    
    def __init__(self, mqtt_config: dict):
        self.mqtt_config = mqtt_config
        self.mqtt_client: Optional[mqtt.Client] = None
        self.ble_client: Optional[BleakClient] = None
        self.data = DalyBMSData()
        self._response_buffer = bytearray()
        self._response_event = asyncio.Event()
        self._running = True
        
    # ========== MQTT ==========
    
    def _setup_mqtt(self) -> bool:
        """Configura conexión MQTT"""
        try:
            self.mqtt_client = mqtt.Client(client_id=self.mqtt_config["client_id"])
            
            if self.mqtt_config.get("username"):
                self.mqtt_client.username_pw_set(
                    self.mqtt_config["username"],
                    self.mqtt_config["password"]
                )
            
            self.mqtt_client.on_connect = self._on_mqtt_connect
            self.mqtt_client.on_disconnect = self._on_mqtt_disconnect
            
            print(f"Conectando a MQTT {self.mqtt_config['broker']}:{self.mqtt_config['port']}...")
            self.mqtt_client.connect(
                self.mqtt_config["broker"],
                self.mqtt_config["port"],
                keepalive=60
            )
            self.mqtt_client.loop_start()
            return True
            
        except Exception as e:
            print(f"Error MQTT: {e}")
            return False
    
    def _on_mqtt_connect(self, client, userdata, flags, rc):
        """Callback conexión MQTT"""
        if rc == 0:
            print("MQTT: Conectado!")
            # Publicar estado online
            self._publish("status", "online", retain=True)
        else:
            print(f"MQTT: Error de conexión, código {rc}")
    
    def _on_mqtt_disconnect(self, client, userdata, rc):
        """Callback desconexión MQTT"""
        print(f"MQTT: Desconectado (rc={rc})")
    
    def _publish(self, subtopic: str, payload, retain: bool = False):
        """Publica mensaje MQTT"""
        if not self.mqtt_client:
            return
        
        topic = f"{self.mqtt_config['topic_base']}/{subtopic}"
        
        if isinstance(payload, dict):
            payload = json.dumps(payload)
        elif not isinstance(payload, str):
            payload = str(payload)
        
        self.mqtt_client.publish(topic, payload, retain=retain)
        print(f"MQTT: {topic} <- {payload[:100]}{'...' if len(payload) > 100 else ''}")
    
    def _publish_data(self):
        """Publica todos los datos del BMS"""
        self.data.timestamp = datetime.now().isoformat()
        self.data.power = round(self.data.voltage * self.data.current, 1)
        self.data.cell_voltage_diff = self.data.max_cell_voltage - self.data.min_cell_voltage
        
        # Publicar JSON completo
        self._publish("state", self.data.to_dict())
    
    # ========== BLE ==========
    
    def _calc_checksum(self, data: bytes) -> int:
        return sum(data) & 0xFF
    
    def _build_command(self, cmd: int, address: int = 0x40) -> bytes:
        packet = bytes([0xA5, address, cmd, 0x08, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
        checksum = self._calc_checksum(packet)
        return packet + bytes([checksum])
    
    async def scan(self, timeout: float = 10.0) -> List[dict]:
        """Escanea dispositivos BLE"""
        print(f"Escaneando BLE ({timeout}s)...")
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
        """Maneja notificaciones BLE"""
        self._response_buffer.extend(data)
        
        if len(self._response_buffer) >= 13:
            self._parse_response(bytes(self._response_buffer))
            self._response_buffer.clear()
            self._response_event.set()
    
    def _parse_response(self, data: bytes):
        """Parsea respuesta Daly"""
        if len(data) < 13 or data[0] != 0xA5:
            return
        
        cmd = data[2]
        payload = data[4:12]
        
        if cmd == 0x90:
            self._parse_soc(payload)
        elif cmd == 0x91:
            self._parse_min_max_volt(payload)
        elif cmd == 0x92:
            self._parse_min_max_temp(payload)
        elif cmd == 0x95:
            self._parse_cell_voltages(payload)
    
    def _parse_soc(self, data: bytes):
        if len(data) < 8:
            return
        
        voltage_raw = struct.unpack('>H', data[0:2])[0]
        self.data.voltage = voltage_raw / 10.0
        
        current_raw = struct.unpack('>H', data[4:6])[0]
        self.data.current = (current_raw - 30000) / 10.0
        
        soc_raw = struct.unpack('>H', data[6:8])[0]
        self.data.soc = soc_raw // 10
        
        print(f"  SOC: {self.data.voltage:.1f}V, {self.data.current:.1f}A, {self.data.soc}%")
    
    def _parse_min_max_volt(self, data: bytes):
        if len(data) < 6:
            return
        
        max_v = struct.unpack('>H', data[0:2])[0]
        self.data.max_cell_voltage = max_v / 1000.0
        self.data.max_cell_number = data[2]
        
        min_v = struct.unpack('>H', data[3:5])[0]
        self.data.min_cell_voltage = min_v / 1000.0
        self.data.min_cell_number = data[5]
        
        print(f"  Celdas: Max {self.data.max_cell_voltage:.3f}V (#{self.data.max_cell_number}), Min {self.data.min_cell_voltage:.3f}V (#{self.data.min_cell_number})")
    
    def _parse_min_max_temp(self, data: bytes):
        if len(data) < 4:
            return
        
        self.data.temperature_max = data[0] - 40
        self.data.temperature_min = data[2] - 40
        
        print(f"  Temp: {self.data.temperature_max}°C / {self.data.temperature_min}°C")
    
    def _parse_cell_voltages(self, data: bytes):
        if len(data) < 7:
            return
        
        frame_num = data[0]
        
        for i in range(3):
            offset = 1 + i * 2
            if offset + 2 <= len(data):
                cell_mv = struct.unpack('>H', data[offset:offset+2])[0]
                if 0 < cell_mv < 5000:
                    cell_num = (frame_num - 1) * 3 + i
                    while len(self.data.cell_voltages) <= cell_num:
                        self.data.cell_voltages.append(0.0)
                    self.data.cell_voltages[cell_num] = cell_mv / 1000.0
        
        self.data.cell_count = len([v for v in self.data.cell_voltages if v > 0])
    
    async def connect_ble(self, address: str) -> bool:
        """Conecta al BMS via BLE"""
        print(f"\nConectando BLE a {address}...")
        
        try:
            self.ble_client = BleakClient(address)
            await self.ble_client.connect()
            
            if not self.ble_client.is_connected:
                print("Error: No conectado")
                return False
            
            print("BLE: Conectado!")
            
            await self.ble_client.start_notify(self.NOTIFY_UUID, self._notification_handler)
            print(f"BLE: Notificaciones activas")
            
            return True
            
        except Exception as e:
            print(f"Error BLE: {e}")
            return False
    
    async def send_command(self, cmd: int):
        """Envía comando al BMS"""
        packet = self._build_command(cmd)
        
        self._response_event.clear()
        self._response_buffer.clear()
        
        try:
            await self.ble_client.write_gatt_char(self.WRITE_UUID, packet, response=False)
            
            try:
                await asyncio.wait_for(self._response_event.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                pass
                
        except Exception as e:
            print(f"Error enviando comando: {e}")
    
    async def read_all(self) -> bool:
        """Lee todos los datos del BMS. Retorna True si la lectura fue exitosa."""
        print("\nLeyendo BMS...")
        
        # Limpiar voltajes anteriores
        self.data.cell_voltages.clear()
        
        # Guardar valores anteriores para detectar si hubo respuesta
        old_voltage = self.data.voltage
        self.data.voltage = 0.0
        
        try:
            await self.send_command(0x90)  # SOC
            await asyncio.sleep(0.3)
            
            await self.send_command(0x91)  # Min/Max volt
            await asyncio.sleep(0.3)
            
            await self.send_command(0x92)  # Temps
            await asyncio.sleep(0.3)
            
            # Leer voltajes de celdas (múltiples frames)
            for _ in range(5):  # Hasta 15 celdas
                await self.send_command(0x95)
                await asyncio.sleep(0.2)
            
            # Verificar si se recibieron datos válidos (voltage > 0 indica respuesta del BMS)
            if self.data.voltage > 0:
                return True
            else:
                print("Error: No se recibieron datos del BMS")
                self.data.voltage = old_voltage  # Restaurar valor anterior
                return False
                
        except Exception as e:
            print(f"Error leyendo datos del BMS: {e}")
            self.data.voltage = old_voltage  # Restaurar valor anterior
            return False
    
    async def disconnect(self):
        """Desconecta BLE y MQTT"""
        if self.ble_client and self.ble_client.is_connected:
            try:
                await self.ble_client.stop_notify(self.NOTIFY_UUID)
            except:
                pass
            await self.ble_client.disconnect()
            print("BLE: Desconectado")
        
        if self.mqtt_client:
            self._publish("status", "offline", retain=True)
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()
            print("MQTT: Desconectado")
    
    def stop(self):
        """Detiene el loop principal"""
        self._running = False
    
    async def run(self, address: str, poll_interval: int = 10):
        """Loop principal: lee BMS y publica MQTT"""
        
        # Conectar MQTT
        if not self._setup_mqtt():
            print("Error: No se pudo conectar a MQTT")
            return
        
        await asyncio.sleep(1)  # Esperar conexión MQTT
        
        # Conectar BLE
        if not await self.connect_ble(address):
            print("Error: No se pudo conectar al BMS")
            return
        
        print(f"\n{'='*50}")
        print(f"Iniciando loop de lectura (cada {poll_interval}s)")
        print(f"Topic MQTT: {self.mqtt_config['topic_base']}/state")
        print(f"Presiona Ctrl+C para detener")
        print(f"{'='*50}\n")
        
        try:
            while self._running:
                try:
                    # Leer datos del BMS
                    read_success = await self.read_all()
                    
                    # Solo publicar en MQTT si la lectura fue exitosa
                    if read_success:
                        self._publish_data()
                    else:
                        print("Omitiendo publicación MQTT (lectura fallida)")
                    
                    # Esperar hasta próxima lectura
                    await asyncio.sleep(poll_interval)
                    
                except Exception as e:
                    print(f"Error en loop: {e}")
                    await asyncio.sleep(5)
                    
                    # Intentar reconectar BLE si se perdió conexión
                    if not self.ble_client.is_connected:
                        print("Reconectando BLE...")
                        await self.connect_ble(address)
        
        except asyncio.CancelledError:
            pass
        
        finally:
            await self.disconnect()


async def main():
    bms = DalyBMSMQTT(MQTT_CONFIG)
    
    # Manejar Ctrl+C
    def signal_handler(sig, frame):
        print("\n\nDeteniendo...")
        bms.stop()
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Obtener dirección del BMS
    address = BMS_CONFIG.get("address")
    
    if not address:
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
    
    # Ejecutar loop principal
    await bms.run(address, BMS_CONFIG["poll_interval"])


if __name__ == "__main__":
    asyncio.run(main())
