#!/usr/bin/env python3
"""
BMS Connector con logging a archivo
Para debuggear problemas de conexión
"""

import asyncio
import struct
import sys
import argparse
import logging
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional, List
from dataclasses import dataclass
from bleak import BleakClient

# Configurar logging
log_dir = Path.home() / ".bms_logs"
log_dir.mkdir(exist_ok=True)
log_file = log_dir / f"bms_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

print(f"Log guardado en: {log_file}")


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


class BMSConnector:
    """Conector BMS con logging completo"""
    
    WRITE_UUID = "02f00000-0000-0000-0000-00000000ff01"
    NOTIFY_UUID = "02f00000-0000-0000-0000-00000000ff02"
    ALT_NOTIFY_UUID = "02f00000-0000-0000-0000-00000000ff04"
    
    def __init__(self, mac_address: str):
        self.mac_address = mac_address
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
        logger.debug(f"Notification received: {len(data)} bytes, data: {data.hex()}")
        self.response_data.extend(data)
        
        # Verificar mensaje completo
        if len(self.response_data) >= 4:
            for i in range(len(self.response_data) - 1, 2, -1):
                if self.response_data[i] == 0x77 and self.response_data[0] == 0xDD:
                    self.last_response = bytes(self.response_data[:i+1])
                    remaining = bytes(self.response_data[i+1:])
                    self.response_data = bytearray(remaining)
                    logger.info(f"Complete message received: {self.last_response.hex()}")
                    self.command_event.set()
                    return
    
    async def connect(self) -> bool:
        """Conectar al BMS"""
        logger.info(f"Starting connection to {self.mac_address}")
        
        # Desconectar del sistema
        try:
            subprocess.run(['bluetoothctl', 'disconnect', self.mac_address], 
                          capture_output=True, timeout=5)
            logger.info("Disconnected from system")
            await asyncio.sleep(1)
        except Exception as e:
            logger.warning(f"Could not disconnect from system: {e}")
        
        try:
            self.client = BleakClient(self.mac_address, timeout=15.0)
            logger.debug("BleakClient created")
            
            await self.client.connect()
            logger.info(f"Connected: {self.client.is_connected}")
            
            if not self.client.is_connected:
                logger.error("Connection failed")
                return False
            
            logger.info(f"Device name: {self.client.name}")
            
            # Log services
            logger.info("Discovered services:")
            for service in self.client.services:
                logger.info(f"  Service: {service.uuid}")
                for char in service.characteristics:
                    props = ','.join(char.properties)
                    logger.info(f"    Characteristic: {char.uuid} [{props}]")
            
            # Configurar notificaciones
            try:
                await self.client.start_notify(self.NOTIFY_UUID, self.notification_handler)
                logger.info(f"Notifications enabled on {self.NOTIFY_UUID}")
            except Exception as e:
                logger.warning(f"Could not enable notifications on ff02: {e}")
                try:
                    await self.client.start_notify(self.ALT_NOTIFY_UUID, self.notification_handler)
                    logger.info(f"Notifications enabled on {self.ALT_NOTIFY_UUID}")
                    self.use_alt_uuid = True
                except Exception as e2:
                    logger.error(f"Could not enable notifications: {e2}")
                    return False
            
            return True
            
        except Exception as e:
            logger.exception(f"Connection error: {e}")
            return False
    
    async def send_command(self, register: int, description: str, timeout: float = 5.0) -> Optional[bytes]:
        """Enviar comando"""
        cmd = self.build_command(register)
        write_uuid = self.ALT_NOTIFY_UUID if self.use_alt_uuid else self.WRITE_UUID
        
        logger.info(f"Sending command: {description}")
        logger.debug(f"  Command bytes: {cmd.hex()}")
        logger.debug(f"  Write UUID: {write_uuid}")
        
        self.command_event.clear()
        self.response_data.clear()
        self.last_response = None
        
        try:
            await self.client.write_gatt_char(write_uuid, cmd, response=False)
            logger.debug("Command sent successfully")
            
            try:
                await asyncio.wait_for(self.command_event.wait(), timeout=timeout)
                logger.info(f"Response received: {len(self.last_response)} bytes")
                logger.debug(f"  Response hex: {self.last_response.hex()}")
                return self.last_response
            except asyncio.TimeoutError:
                logger.warning("Timeout waiting for response")
                return None
                
        except Exception as e:
            logger.exception(f"Error sending command: {e}")
            return None
    
    def parse_basic_info(self, data: bytes) -> Optional[BMSData]:
        """Parsear información básica"""
        logger.debug(f"Parsing basic info from {len(data)} bytes")
        
        try:
            if len(data) < 4:
                logger.warning(f"Data too short: {len(data)} bytes")
                return None
            
            if data[0] != 0xDD:
                logger.warning(f"Invalid header: 0x{data[0]:02X} (expected 0xDD)")
                return None
            
            if data[1] != 0x03:
                logger.warning(f"Unexpected response register: 0x{data[1]:02X} (expected 0x03)")
            
            length = data[3]
            logger.debug(f"Payload length: {length}")
            
            if 4 + length + 2 > len(data):
                logger.warning(f"Incomplete data: need {4+length+2}, have {len(data)}")
                return None
            
            payload = data[4:4+length]
            crc_received = data[4+length]
            end_byte = data[4+length+1]
            
            # Verificar CRC
            crc_calc = self.calculate_crc(data[2:2+2+length])
            if crc_received != crc_calc:
                logger.warning(f"CRC mismatch: received 0x{crc_received:02X}, calculated 0x{crc_calc:02X}")
            
            if end_byte != 0x77:
                logger.warning(f"Invalid end byte: 0x{end_byte:02X} (expected 0x77)")
            
            if len(payload) < 27:
                logger.warning(f"Payload too short: {len(payload)} bytes (expected >= 27)")
                return None
            
            # Parsear campos
            voltage = struct.unpack('>H', payload[0:2])[0] / 100.0
            current_raw = struct.unpack('>h', payload[2:4])[0]
            current = current_raw / 100.0
            capacity_remain = struct.unpack('>H', payload[4:6])[0] / 100.0
            capacity_total = struct.unpack('>H', payload[6:8])[0] / 100.0
            cycle_count = struct.unpack('>H', payload[8:10])[0]
            
            soc = int((capacity_remain / capacity_total) * 100) if capacity_total > 0 else 0
            
            cell_count = payload[21]
            temp_count = payload[22]
            
            logger.debug(f"Parsed: voltage={voltage}V, current={current}A, soc={soc}%, cells={cell_count}, temps={temp_count}")
            
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
            logger.exception(f"Error parsing basic info: {e}")
            return None
    
    async def read_data(self) -> Optional[BMSData]:
        """Leer datos del BMS"""
        logger.info("="*50)
        logger.info("Reading BMS data")
        logger.info("="*50)
        
        response = await self.send_command(0x03, "Basic info (register 0x03)")
        
        if not response:
            logger.error("No response from BMS")
            return None
        
        data = self.parse_basic_info(response)
        if not data:
            logger.error("Failed to parse response")
            logger.info(f"Raw response for manual analysis: {response.hex()}")
            return None
        
        # Leer celdas
        if data.cell_count > 0:
            logger.info(f"Reading {data.cell_count} cell voltages...")
            response = await self.send_command(0x04, "Cell voltages (register 0x04)")
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
                    logger.info(f"Cell voltages: {[f'{v:.3f}V' for v in voltages]}")
                except Exception as e:
                    logger.exception(f"Error parsing cell voltages: {e}")
        
        return data
    
    async def disconnect(self):
        if self.client and self.client.is_connected:
            await self.client.disconnect()
            logger.info("Disconnected")


async def main():
    parser = argparse.ArgumentParser(description='BMS Connector with logging')
    parser.add_argument('mac', help='MAC address')
    parser.add_argument('-c', '--continuous', action='store_true', help='Continuous reading')
    
    args = parser.parse_args()
    
    logger.info(f"Log file: {log_file}")
    logger.info(f"Target MAC: {args.mac}")
    
    bms = BMSConnector(args.mac)
    
    if await bms.connect():
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
                else:
                    logger.error("Failed to get data")
        except KeyboardInterrupt:
            logger.info("Interrupted by user")
        finally:
            await bms.disconnect()
    else:
        logger.error("Failed to connect")
    
    logger.info(f"Log saved to: {log_file}")
    print(f"\nLog saved to: {log_file}")


if __name__ == "__main__":
    asyncio.run(main())
